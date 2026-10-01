#!/usr/bin/env python3
"""
Start one or more local vLLM replicas of a checkpoint, then either run a
command against them or send one test request.

    python3 -m modelsafety.serving.serve run   [options] -- COMMAND...
    python3 -m modelsafety.serving.serve smoke [options]

`run` is what launchers use: it starts vLLM, waits until every replica is
actually serving *our* model (not just answering on the port -- see
`served_model_ids` below), runs COMMAND, then stops every replica no matter
how COMMAND ended. `smoke` is the same minus COMMAND: it sends one short
chat request per replica and prints the reply, for finding out whether
serving itself works before wiring up a real caller.

Every setting has a default derived from the checkpoint's own config files
(see `vllm_config.py`, in this package) and every one of those defaults can
be overridden on the command line -- nothing here is hidden inside a
launcher script, on the same principle `vllm_config.py`'s own docstring
states.

Standard library only, so this runs under the plain system `python3`; it
never imports vLLM or torch itself, only starts `vllm serve` as a
subprocess and talks to it over HTTP.

Several pieces below (the port-block scheme, the `/v1/models` identity
check, the process-group shutdown, the flashinfer-cache-on-/tmp default)
are adapted from the tool team's own vLLM launcher,
`qvac-research-tool-call/evaluation/src/qvac_eval/serving.py` and
`environment.py` -- see `PROVENANCE.md` for the full list. This module is a
separate, smaller implementation for this repo's own launchers, not an
import of theirs: it has no dependency on their model catalog.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modelsafety.serving import vllm_config

# The tool team's vLLM environment (vLLM 0.19.0, Python 3.11, torch/cu128) --
# same default already used by scripts/tool_team_regurgitation.sh and
# launchers/slurm/run_regurgitation.sbatch. Kept as one constant so both
# places agree without repeating the string.
DEFAULT_VLLM_BIN = (
    "/home/shared/agentic_slm/qvac-research-tool-call/evaluation/services/vllm/.venv/bin/vllm"
)

# Package names looked up in the vLLM venv for the launch report. Best
# effort: a name that does not match anything installed there just shows
# "(not installed)" rather than failing the whole report -- flashinfer in
# particular ships as the "flashinfer-python" distribution (a separate
# "flashinfer-cubin" distribution also exists; not reported here).
REPORT_PACKAGES = ("vllm", "torch", "triton", "flashinfer-python", "transformers", "nvidia-cublas-cu12")

# Environment variables worth recording in the launch report -- everything
# that could plausibly change which CUDA/cuBLAS/NCCL library vLLM loads, or
# which cache/offline mode it runs in. Prefixes, not exact names, so e.g.
# CUDA_VISIBLE_DEVICES and CUDA_HOME are both caught.
REPORT_ENV_PREFIXES = ("CUDA_", "NVIDIA_", "VLLM_", "TORCH", "HF_", "FLASHINFER_", "TRITON_", "NCCL_")
REPORT_ENV_EXTRA_NAMES = ("LD_LIBRARY_PATH", "LD_PRELOAD")
# Any variable whose *name* contains one of these is a secret -- mask the
# value in the report even though the name matched a prefix above (a stray
# HF_TOKEN would otherwise land in a JSON file under logs/).
REPORT_ENV_MASK_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


class ServingError(RuntimeError):
    """Something went wrong starting, running, or serving vLLM."""


class UsageError(ServingError):
    """A bad argument or a checkpoint that cannot be served as configured.

    Kept as its own subclass so `main()` can exit 2 for these (a usage
    problem) and 1 for everything else (a runtime failure) -- see the exit
    code table in this module's docstring... actually see `main()` below.
    """


class _Interrupted(Exception):
    """Raised from the SIGTERM/SIGINT handler installed in `main()`.

    Turns "Slurm sent SIGTERM because the time limit hit" into an ordinary
    exception `run`/`smoke` already unwind through, so the `finally: stop_all(...)`
    below them still runs and the GPU does not sit held by a zombie vLLM.
    """


@dataclass
class Settings:
    """Every setting `run`/`smoke` need, after defaults and overrides are resolved.

    Built once by `resolve_settings()` and never mutated afterwards.
    """

    checkpoint: Path
    served_name: str
    dtype: str
    max_model_len: int
    tp_size: int
    replicas: int
    # thinking_value: what to put in {"enable_thinking": ...}; None means
    # "don't send the kwarg at all" (an --thinking auto checkpoint whose
    # template has no such kwarg to begin with).
    thinking_value: bool | None
    thinking_send: bool
    family_flags: list[str]
    sampling_json: dict[str, Any]
    seed: int
    max_num_seqs: int
    extra_args: list[str]
    vllm_bin: Path
    log_dir: Path
    ready_timeout: int
    base_port: int
    host: str = "127.0.0.1"

    def to_report_dict(self) -> dict[str, Any]:
        """A JSON-safe copy for `launch_report.json` (Paths become strings)."""
        return {
            "checkpoint": str(self.checkpoint),
            "served_name": self.served_name,
            "dtype": self.dtype,
            "max_model_len": self.max_model_len,
            "tp_size": self.tp_size,
            "replicas": self.replicas,
            "thinking_value": self.thinking_value,
            "thinking_send": self.thinking_send,
            "family_flags": self.family_flags,
            "sampling_json": self.sampling_json,
            "seed": self.seed,
            "max_num_seqs": self.max_num_seqs,
            "extra_args": self.extra_args,
            "vllm_bin": str(self.vllm_bin),
            "log_dir": str(self.log_dir),
            "ready_timeout": self.ready_timeout,
            "base_port": self.base_port,
            "host": self.host,
        }


@dataclass
class Replica:
    """One `vllm serve` process: where it listens, which GPUs, its log."""

    index: int
    port: int
    # None means "don't set CUDA_VISIBLE_DEVICES for this replica -- let it
    # inherit whatever the job already has" (the single-replica, whole-
    # allocation case; see `plan_replicas()`).
    gpu_ids: list[str] | None
    log_path: Path
    argv: list[str]
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modelsafety.serving.serve",
        description=(
            "Serve a local checkpoint with vLLM, then either run a command "
            "against it (run) or send one test request (smoke)."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--checkpoint", required=True, help="Path to a local HF checkpoint directory.")
        p.add_argument("--served-name", help="Default: the checkpoint directory's own name.")
        p.add_argument("--dtype", help="Default: derived from the checkpoint's config.json.")
        p.add_argument(
            "--max-model-len", type=int, help="Default: derived from config.json, capped at --max-model-len-cap."
        )
        p.add_argument(
            "--max-model-len-cap",
            type=int,
            default=vllm_config.DEFAULT_MAX_MODEL_LEN_CAP,
            help=f"Upper bound on a derived max-model-len (default: {vllm_config.DEFAULT_MAX_MODEL_LEN_CAP}).",
        )
        p.add_argument("--tp-size", type=int, default=1, help="GPUs per replica (default: 1).")
        p.add_argument(
            "--replicas",
            type=int,
            default=None,
            help="Number of replicas. Default: this job's visible GPUs // --tp-size (at least 1).",
        )
        p.add_argument(
            "--thinking",
            choices=("auto", "on", "off"),
            default="auto",
            help="auto (default): on iff the checkpoint's chat template supports it.",
        )
        p.add_argument(
            "--sampling-json",
            help='JSON object for --override-generation-config, e.g. \'{"top_p": 0.95}\'. '
            "Default: the checkpoint family's published profile for the resolved thinking mode.",
        )
        p.add_argument(
            "--family-flags-json",
            help='JSON array of extra vllm serve flags, e.g. \'["--gdn-prefill-backend", "triton"]\'. '
            "Default: the checkpoint family's flags.",
        )
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--max-num-seqs", type=int, default=128)
        p.add_argument(
            "--extra-args",
            default="",
            help="Extra vllm serve arguments as one shell-quoted string. Since the value itself "
            "starts with '-', use the --flag=value form: --extra-args='--trust-remote-code'. "
            "A space-separated --extra-args '--trust-remote-code' fails: argparse reads the "
            "second word as its own (unrecognized) flag, not as this flag's value.",
        )
        p.add_argument("--vllm-bin", default=DEFAULT_VLLM_BIN, help=f"Default: {DEFAULT_VLLM_BIN}")
        p.add_argument(
            "--log-dir", help="Default: output/serving/<served-name>/<Slurm job id or UTC timestamp>/"
        )
        p.add_argument("--ready-timeout", type=int, default=1800, help="Seconds to wait for startup (default: 1800).")
        p.add_argument(
            "--base-port",
            type=int,
            default=None,
            help="First port to try. Default: 20000 + (SLURM_JOB_ID %% 1000) * 8.",
        )

    run_parser = sub.add_parser(
        "run",
        help="Serve, run one command against every replica, then stop.",
        description=(
            "Serve, then run COMMAND (everything after a literal --). The token "
            "{base_urls} in COMMAND is replaced with the replicas' comma-separated "
            "OpenAI-compatible base URLs; SERVE_BASE_URLS and SERVE_MODEL_NAME are "
            "also set in COMMAND's environment."
        ),
    )
    add_common(run_parser)

    smoke_parser = sub.add_parser(
        "smoke",
        help="Serve, send one test chat request to every replica, then stop.",
    )
    add_common(smoke_parser)

    return parser


def split_command(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split CLI argv on the first literal "--" into (options, command).

    Done by hand rather than with argparse's own REMAINDER/"--" handling,
    which behaves inconsistently across Python versions when a subparser is
    involved. `command` is None when no "--" is present at all (smoke's
    normal case); it is an empty list for a trailing "--" with nothing after
    it (run's error case, reported by the caller, not here).
    """
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    return argv, None


# ---------------------------------------------------------------------------
# Resolving settings: derived-from-the-checkpoint defaults, CLI overrides
# ---------------------------------------------------------------------------


def _load_json_object(raw: str, flag: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{flag} is not valid JSON: {raw!r} ({exc})") from exc
    if not isinstance(value, dict):
        raise UsageError(f"{flag} must be a JSON object, got {type(value).__name__}: {raw!r}")
    return value


def _load_json_str_list(raw: str, flag: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{flag} is not valid JSON: {raw!r} ({exc})") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UsageError(f"{flag} must be a JSON array of strings, got {raw!r}")
    return value


def resolve_thinking(mode: str, template_supports: bool, warnings: list[str]) -> tuple[bool | None, bool]:
    """Resolve --thinking {auto,on,off} against what the chat template supports.

    Returns (thinking_value, thinking_send). thinking_send is False only for
    "auto" on a template with nothing to toggle -- the one case where sending
    --default-chat-template-kwargs would be pure noise. Every other case,
    including an explicit "off", sends the kwarg: this is a deliberate change
    from the previous behaviour, where "off" meant "send nothing" and relied
    on the template's own default -- see PROVENANCE.md.
    """
    if mode == "on":
        if not template_supports:
            warnings.append(
                "--thinking on was requested but this checkpoint's chat template does not "
                "appear to read an enable_thinking kwarg -- proceeding anyway, but the flag "
                "may have no effect."
            )
        return True, True
    if mode == "off":
        return False, True
    # auto
    if template_supports:
        return True, True
    return None, False


def visible_gpu_ids() -> list[str]:
    """The CUDA device ids this process may use, one string per GPU.

    Adapted from qvac_eval.serving.visible_gpus()/._gpu_count() (see
    PROVENANCE.md). CUDA_VISIBLE_DEVICES is authoritative when set --
    Slurm points it at the GPUs it granted this job -- otherwise fall back
    to Slurm's own GPU count, then nvidia-smi. Returns [] (not an error) if
    none of these say anything; callers treat that as "unknown" rather than
    "zero".
    """
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cvd is not None and cvd.strip():
        return [d.strip() for d in cvd.split(",") if d.strip()]

    on_node = os.environ.get("SLURM_GPUS_ON_NODE", "")
    if on_node.isdigit():
        return [str(i) for i in range(int(on_node))]

    digits = "".join(ch for ch in os.environ.get("SLURM_GPUS", "") if ch.isdigit())
    if digits:
        return [str(i) for i in range(int(digits))]

    try:
        listed = subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [str(i) for i, line in enumerate(listed.stdout.splitlines()) if line.strip()]


def repo_root() -> Path:
    """Best-effort repo root for the default log directory.

    REPO is set by every launcher in this repo before it runs Python
    (e.g. `REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"` in run_regurgitation.sbatch);
    SLURM_SUBMIT_DIR is Slurm's own fallback for a job that forgot to. The
    current directory is the last resort for an interactive, non-Slurm run.
    """
    for var in ("REPO", "SLURM_SUBMIT_DIR"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    return Path.cwd()


def default_log_dir(served_name: str) -> Path:
    job = os.environ.get("SLURM_JOB_ID")
    slug = job if job else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return repo_root() / "output" / "serving" / served_name / slug


def base_port_for_job() -> int:
    """The first of this job's own block of 8 ports.

    Adapted from qvac_eval.serving._pick_port() (see PROVENANCE.md): a block
    keyed on the Slurm job id, not a shared counter, so two jobs that land on
    the same node never reach for the same port. 1000 slots of 8 ports each,
    based at 20000, stays clear of Slurm's and vLLM's own default ranges.
    """
    job = os.environ.get("SLURM_JOB_ID", "")
    slot = int(job) % 1000 if job.isdigit() else os.getpid() % 1000
    return 20000 + slot * 8


def resolve_settings(args: argparse.Namespace) -> Settings:
    checkpoint = Path(args.checkpoint).expanduser().resolve()
    if not checkpoint.is_dir():
        raise UsageError(f"checkpoint path is not a directory: {checkpoint}")

    warnings: list[str] = []
    try:
        derived = vllm_config.derive_settings(checkpoint, args.max_model_len_cap)
    except vllm_config.VllmConfigError as exc:
        raise UsageError(str(exc)) from exc
    warnings.extend(derived["warnings"])
    if derived["weight_bytes"] == 0:
        raise UsageError(f"no weight files (*.safetensors or *.bin) found under checkpoint: {checkpoint}")

    served_name = args.served_name or derived["served_name"]
    dtype = args.dtype or derived["dtype"]
    max_model_len = args.max_model_len or derived["max_model_len"]
    tp_size = args.tp_size

    thinking_value, thinking_send = resolve_thinking(args.thinking, derived["thinking_supported"], warnings)

    if args.sampling_json is not None:
        sampling_json = _load_json_object(args.sampling_json, "--sampling-json")
    else:
        sampling_json = derived["sampling_thinking"] if thinking_value else derived["sampling_instruct"]

    if args.family_flags_json is not None:
        family_flags = _load_json_str_list(args.family_flags_json, "--family-flags-json")
    else:
        family_flags = list(derived["family_flags"])

    extra_args = shlex.split(args.extra_args) if args.extra_args else []

    vllm_bin = Path(args.vllm_bin).expanduser()
    if not os.access(vllm_bin, os.X_OK):
        raise UsageError(f"vLLM binary not found or not executable: {vllm_bin}")

    replicas = args.replicas
    if replicas is None:
        visible_count = len(visible_gpu_ids())
        replicas = max(visible_count // tp_size, 1) if visible_count else 1

    log_dir = Path(args.log_dir).expanduser() if args.log_dir else default_log_dir(served_name)
    base_port = args.base_port if args.base_port is not None else base_port_for_job()

    for warning in warnings:
        print(f"[serve] note: {warning}", file=sys.stderr)

    return Settings(
        checkpoint=checkpoint,
        served_name=served_name,
        dtype=dtype,
        max_model_len=max_model_len,
        tp_size=tp_size,
        replicas=replicas,
        thinking_value=thinking_value,
        thinking_send=thinking_send,
        family_flags=family_flags,
        sampling_json=sampling_json,
        seed=args.seed,
        max_num_seqs=args.max_num_seqs,
        extra_args=extra_args,
        vllm_bin=vllm_bin,
        log_dir=log_dir,
        ready_timeout=args.ready_timeout,
        base_port=base_port,
    )


# ---------------------------------------------------------------------------
# Planning replicas: GPU slices, ports, the vllm serve command line
# ---------------------------------------------------------------------------


def build_argv(settings: Settings, port: int) -> list[str]:
    """The `vllm serve` command line for one replica.

    Deliberately does not add --trust-remote-code (vLLM has native support
    for the checkpoint families this repo derives settings for) or
    --download-dir (the checkpoint is always a local directory already).
    Either can be added back with --extra-args if a specific checkpoint
    needs it. See PROVENANCE.md for the reasoning.
    """
    argv = [
        str(settings.vllm_bin),
        "serve",
        str(settings.checkpoint),
        "--host",
        settings.host,
        "--port",
        str(port),
        "--served-model-name",
        settings.served_name,
        "--dtype",
        settings.dtype,
        "--tensor-parallel-size",
        str(settings.tp_size),
        "--max-model-len",
        str(settings.max_model_len),
        "--seed",
        str(settings.seed),
        "--max-num-seqs",
        str(settings.max_num_seqs),
    ]
    if settings.thinking_send:
        argv += ["--default-chat-template-kwargs", json.dumps({"enable_thinking": settings.thinking_value})]
    if settings.sampling_json:
        argv += ["--override-generation-config", json.dumps(settings.sampling_json)]
    argv += settings.family_flags
    argv += settings.extra_args
    return argv


def claim_ports(base_port: int, count: int, host: str) -> list[int]:
    """`count` free ports from the 8-port block starting at base_port.

    Adapted from qvac_eval.serving._pick_port() (see PROVENANCE.md): binding
    then immediately closing only proves a port was free at that instant,
    not that it stays free until vLLM binds it minutes later. That is the
    same accepted, documented race the tool team's own picker has -- the
    identity check in `wait_until_ready()` is what actually catches a port
    two servers ended up sharing.
    """
    claimed: list[int] = []
    for offset in range(8):
        if len(claimed) >= count:
            break
        port = base_port + offset
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        except OSError:
            continue
        finally:
            probe.close()
        claimed.append(port)
    if len(claimed) < count:
        raise ServingError(
            f"only found {len(claimed)} free port(s) in [{base_port}, {base_port + 8}) for {count} replica(s)"
        )
    return claimed


def plan_replicas(settings: Settings) -> list[Replica]:
    visible = visible_gpu_ids()
    needed = settings.tp_size * settings.replicas

    if visible and needed > len(visible):
        raise ServingError(
            f"need {needed} GPU(s) ({settings.replicas} replica(s) x tp-size {settings.tp_size}) "
            f"but only {len(visible)} visible ({','.join(visible)})"
        )
    if not visible:
        print(
            "[serve] warning: could not detect the number of visible GPUs (no "
            "CUDA_VISIBLE_DEVICES, SLURM_GPUS_ON_NODE/SLURM_GPUS, or nvidia-smi) "
            "-- proceeding without a GPU-count check",
            file=sys.stderr,
        )

    # A single replica using every GPU the job has: inherit whatever
    # CUDA_VISIBLE_DEVICES the job already has instead of restating it, the
    # same choice qvac_eval.serving.VLLMServer.start() makes for this case
    # (see PROVENANCE.md).
    whole_allocation = settings.replicas == 1 and bool(visible) and settings.tp_size == len(visible)

    ports = claim_ports(settings.base_port, settings.replicas, settings.host)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    replicas: list[Replica] = []
    for i in range(settings.replicas):
        if whole_allocation or not visible:
            gpu_ids = None
        else:
            gpu_ids = visible[i * settings.tp_size : (i + 1) * settings.tp_size]
        replicas.append(
            Replica(
                index=i,
                port=ports[i],
                gpu_ids=gpu_ids,
                log_path=settings.log_dir / f"vllm_replica_{i}.log",
                argv=build_argv(settings, ports[i]),
            )
        )
    return replicas


# ---------------------------------------------------------------------------
# Starting, watching, and stopping replicas
# ---------------------------------------------------------------------------


def default_flashinfer_base() -> Path:
    """Where flashinfer's JIT cache goes for replicas that don't already set one.

    Adapted from qvac_eval.environment._pin_flashinfer_cache() (see
    PROVENANCE.md): flashinfer's default cache is under $HOME, and NFS lockd
    rejects the file lock vLLM wraps around building it on some nodes,
    which silently kills the worker on the first real request rather than
    at startup. /tmp is a local filesystem and supports the lock.
    """
    user = os.environ.get("USER", "modelsafety")
    return Path(f"/tmp/{user}/flashinfer-model-safety")  # noqa: S108 - intentional, see docstring


def build_replica_env(settings: Settings, gpu_ids: list[str] | None, base_env: dict[str, str]) -> dict[str, str]:
    """The environment for one vLLM replica subprocess.

    This is only for the `vllm serve` process itself -- the command `run`
    launches afterwards keeps the plain inherited environment (plus
    SERVE_BASE_URLS/SERVE_MODEL_NAME); see `run_command()`.
    """
    env = dict(base_env)
    # vLLM's GDN JIT shells out to `ninja`, which lives next to the vllm
    # binary but nowhere else on these nodes.
    env["PATH"] = f"{settings.vllm_bin.parent}{os.pathsep}{env.get('PATH', '')}"
    # The checkpoint is always a local directory, so serving it needs no
    # network access -- offline mode turns an accidental Hub call into a
    # clear failure instead of a hang.
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["VLLM_NO_USAGE_STATS"] = "1"
    env["DO_NOT_TRACK"] = "1"
    env.setdefault("FLASHINFER_WORKSPACE_BASE", str(default_flashinfer_base()))
    Path(env["FLASHINFER_WORKSPACE_BASE"]).mkdir(parents=True, exist_ok=True)
    # vLLM does not need this repo on its import path.
    env.pop("PYTHONPATH", None)
    # The venv's torch finds its own CUDA libraries without LD_LIBRARY_PATH.
    # An inherited one that lists a system CUDA folder (/usr/local/cuda is
    # 12.9 here) makes the venv's libcublas load that folder's libcublasLt
    # instead of its own, and the mismatched pair fails every bf16 GEMM with
    # CUBLAS_STATUS_INVALID_VALUE.
    env.pop("LD_LIBRARY_PATH", None)
    if gpu_ids is None:
        env.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(gpu_ids)
    return env


def start_all(replicas: list[Replica], settings: Settings) -> None:
    base_env = dict(os.environ)
    for replica in replicas:
        env = build_replica_env(settings, replica.gpu_ids, base_env)
        gpu_desc = ",".join(replica.gpu_ids) if replica.gpu_ids else "(inherited from the job)"
        print(f"[serve] replica {replica.index}: port {replica.port}, GPU(s) {gpu_desc}, log {replica.log_path}")
        with replica.log_path.open("wb") as log:
            # start_new_session=True: vLLM 0.19 forks EngineCore workers
            # that survive a SIGTERM aimed only at this pid -- see
            # `_signal_group()` below, adapted from qvac_eval.serving (see
            # PROVENANCE.md).
            replica.process = subprocess.Popen(  # noqa: S603 - argv is built entirely in this module
                replica.argv, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
            )


def served_model_ids(url: str, timeout: float = 10.0) -> list[str] | None:
    """The model ids an OpenAI-compatible /v1/models answers with, or None if down.

    Reachability alone is not readiness: two vLLM servers can share one port
    (SO_REUSEPORT lets the second bind succeed), so the caller must also
    check *whose* models answered -- see `wait_until_ready()`.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "modelsafety-serve"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            if not 200 <= response.status < 300:
                return None
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    return [str(row.get("id", "")) for row in data if isinstance(row, dict)]


def tail_log(path: Path, lines: int = 60) -> str:
    if not path.is_file():
        return "(no log)"
    try:
        text = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(could not read log: {exc})"
    return "tail of " + str(path) + ":\n" + "\n".join(text[-lines:])


def wait_until_ready(replicas: list[Replica], settings: Settings) -> None:
    deadline = time.monotonic() + settings.ready_timeout
    pending = {replica.index for replica in replicas}
    print(
        f"[serve] waiting for {len(replicas)} replica(s) to serve "
        f"{settings.served_name!r} (timeout {settings.ready_timeout}s)..."
    )
    while pending:
        for index in list(pending):
            replica = replicas[index]
            if replica.process is not None and replica.process.poll() is not None:
                raise ServingError(
                    f"replica {index} exited during startup (exit code "
                    f"{replica.process.returncode}) -- inspect {replica.log_path}\n{tail_log(replica.log_path)}"
                )
            ids = served_model_ids(f"http://{settings.host}:{replica.port}/v1/models")
            if ids is None:
                continue
            if settings.served_name not in ids:
                raise ServingError(
                    f"port {replica.port} answers /v1/models but serves {ids}, not "
                    f"{settings.served_name!r} -- another server shares this port. Refusing "
                    "to run against a server that would answer for the wrong model."
                )
            print(f"[serve] replica {index} healthy (port {replica.port})")
            pending.discard(index)
        if not pending:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(5)
    if pending:
        for index in sorted(pending):
            replica = replicas[index]
            print(f"error: timed out waiting for replica {index} -- inspect {replica.log_path}", file=sys.stderr)
        raise ServingError(f"{len(pending)} replica(s) not ready within {settings.ready_timeout}s")
    print(f"[serve] all {len(replicas)} replica(s) healthy.")


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


def stop_replica(replica: Replica) -> None:
    process = replica.process
    if process is None or process.poll() is not None:
        return
    print(f"[serve] stopping replica {replica.index} (pid {process.pid})")
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=60)
        return
    except subprocess.TimeoutExpired:
        pass
    print(f"[serve] replica {replica.index} did not exit in 60s, sending SIGKILL")
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def stop_all(replicas: list[Replica]) -> None:
    for replica in replicas:
        stop_replica(replica)


# ---------------------------------------------------------------------------
# The launch report -- written before vLLM starts, so a crash always leaves
# context behind (see this module's docstring and PROVENANCE.md).
# ---------------------------------------------------------------------------


def nvidia_smi_report() -> list[dict[str, str]] | str:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,driver_version,memory.used,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(nvidia-smi unavailable: {exc})"
    fields = ("index", "uuid", "name", "driver_version", "memory_used", "memory_total")
    rows = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == len(fields):
            rows.append(dict(zip(fields, parts)))
    return rows


def _normalize_dist_name(name: str) -> str:
    """PEP 503 normalization: package names differ only by case/./-/_ ."""
    return re.sub(r"[-_.]+", "-", name).lower()


def venv_site_packages(vllm_bin: Path) -> Path | None:
    venv_root = vllm_bin.parent.parent  # .../.venv, from .../.venv/bin/vllm
    candidates = sorted(venv_root.glob("lib/python*/site-packages"))
    return candidates[0] if candidates else None


def venv_package_versions(vllm_bin: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Package versions from the vLLM venv, read from install metadata only.

    Deliberately does not import vllm/torch/etc. -- reading a
    `*.dist-info/METADATA` file cannot touch the GPU or trigger vLLM's own
    (slow, GPU-detecting) import side effects, which matters here because
    this report is written for *every* run, including ones that go on to
    fail.
    """
    site_packages = venv_site_packages(vllm_bin)
    if site_packages is None or not site_packages.is_dir():
        return {name: "(venv site-packages not found)" for name in names}
    try:
        installed = {
            dist.metadata["Name"]: dist.version
            for dist in importlib.metadata.distributions(path=[str(site_packages)])
            if dist.metadata and dist.metadata.get("Name")
        }
    except Exception as exc:  # noqa: BLE001 - best-effort diagnostic, never fatal
        return {name: f"(error reading venv metadata: {exc})" for name in names}
    normalized = {_normalize_dist_name(key): value for key, value in installed.items()}
    return {name: normalized.get(_normalize_dist_name(name), "(not installed)") for name in names}


def report_env_vars(env: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in sorted(env.items()):
        if name in REPORT_ENV_EXTRA_NAMES or name.startswith(REPORT_ENV_PREFIXES):
            masked = any(marker in name.upper() for marker in REPORT_ENV_MASK_MARKERS)
            out[name] = "***" if masked else value
    return out


def write_launch_report(settings: Settings, replicas: list[Replica]) -> Path:
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "slurm_partition": os.environ.get("SLURM_JOB_PARTITION"),
        "settings": settings.to_report_dict(),
        "replicas": [
            {
                "index": replica.index,
                "port": replica.port,
                "gpu_ids": replica.gpu_ids,
                "log_path": str(replica.log_path),
                "argv": replica.argv,
            }
            for replica in replicas
        ],
        "nvidia_smi": nvidia_smi_report(),
        "vllm_venv_package_versions": venv_package_versions(settings.vllm_bin, REPORT_PACKAGES),
        "environment": report_env_vars(os.environ),
    }
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    path = settings.log_dir / "launch_report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[serve] launch report: {path}")
    return path


# ---------------------------------------------------------------------------
# run / smoke
# ---------------------------------------------------------------------------


def run_command(command: list[str], replicas: list[Replica], settings: Settings) -> int:
    base_urls = ",".join(f"http://{settings.host}:{replica.port}/v1" for replica in replicas)
    resolved = [base_urls if token == "{base_urls}" else token for token in command]

    env = dict(os.environ)
    env["SERVE_BASE_URLS"] = base_urls
    env["SERVE_MODEL_NAME"] = settings.served_name

    print(f"[serve] running: {' '.join(shlex.quote(token) for token in resolved)}")
    completed = subprocess.run(resolved, env=env)  # noqa: S603 - command is the caller's own argv

    for replica in replicas:
        if replica.process is not None and replica.process.poll() is not None:
            print(
                f"error: replica {replica.index} exited while the command was running "
                f"-- inspect {replica.log_path}",
                file=sys.stderr,
            )
            return 1
    return completed.returncode


def probe_chat(settings: Settings, replica: Replica) -> bool:
    url = f"http://{settings.host}:{replica.port}/v1/chat/completions"
    payload = {
        "model": settings.served_name,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "max_tokens": 16,
        "temperature": 0.0,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "modelsafety-serve"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - loopback only
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[smoke] replica {replica.index} (port {replica.port}): FAILED -- {exc}")
        return False
    elapsed = time.monotonic() - started
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = repr(body)
    print(f"[smoke] replica {replica.index} (port {replica.port}): ok in {elapsed:.1f}s -- {text!r}")
    return True


def cmd_run(args: argparse.Namespace) -> int:
    replicas: list[Replica] = []
    try:
        settings = resolve_settings(args)
        replicas = plan_replicas(settings)
        write_launch_report(settings, replicas)
        start_all(replicas, settings)
        wait_until_ready(replicas, settings)
        return run_command(args.command, replicas, settings)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ServingError, _Interrupted) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_all(replicas)


def cmd_smoke(args: argparse.Namespace) -> int:
    replicas: list[Replica] = []
    try:
        settings = resolve_settings(args)
        replicas = plan_replicas(settings)
        write_launch_report(settings, replicas)
        start_all(replicas, settings)
        wait_until_ready(replicas, settings)
        ok = True
        for replica in replicas:
            ok = probe_chat(settings, replica) and ok
        return 0 if ok else 1
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ServingError, _Interrupted) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_all(replicas)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    option_argv, command = split_command(argv)

    parser = build_parser()
    args = parser.parse_args(option_argv)

    # Slurm's SIGTERM at the time limit, or an operator's Ctrl-C, must still
    # run the `finally: stop_all(...)` in cmd_run/cmd_smoke -- otherwise a
    # killed launcher leaves vLLM (and the GPU) running for nothing.
    signal.signal(signal.SIGTERM, lambda signum, frame: _raise_interrupted(signum))
    signal.signal(signal.SIGINT, lambda signum, frame: _raise_interrupted(signum))

    if args.subcommand == "run":
        if not command:
            parser.error(
                "run requires a command after '--', e.g. ... run --checkpoint X -- "
                "python3 checker.py --base-url {base_urls}"
            )
        args.command = command
        return cmd_run(args)
    if args.subcommand == "smoke":
        if command:
            parser.error("smoke does not take a trailing command")
        return cmd_smoke(args)
    parser.error(f"unknown subcommand: {args.subcommand}")
    return 2  # pragma: no cover - parser.error() above always raises SystemExit


def _raise_interrupted(signum: int) -> None:
    raise _Interrupted(f"received signal {signum}")


if __name__ == "__main__":
    raise SystemExit(main())
