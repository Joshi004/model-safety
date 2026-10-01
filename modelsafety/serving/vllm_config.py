#!/usr/bin/env python3
"""
Derive `vllm serve` deployment settings from a checkpoint's own config files.

Reads only files that already ship with a Hugging Face checkpoint directory
(`config.json`, `generation_config.json`, `chat_template.jinja`) and prints
the settings a caller needs to serve it: served name, dtype, max sequence
length, family-specific `vllm serve` flags, whether the chat template
supports a thinking toggle, and a family sampling profile (temperature is
deliberately excluded -- that stays a probe setting, see below).

Two output modes:
  (default)  `export NAME=value` lines, one per setting, values shell-quoted
             with shlex.quote. Meant to be written to a file and `source`d
             by a caller, e.g.:
                 python3 vllm_config.py --checkpoint "$CKPT" > env.sh
                 source env.sh
             Compound values (flag lists, sampling profiles) are encoded as
             JSON *inside* a single shell string -- there is no portable way
             to export a bash array, so callers that need one should decode
             the *_JSON variable with a small `python3 -c` call, the same
             pattern already used elsewhere in this repo's shell scripts.
  --json     One pretty-printed JSON object with the same fields, for a
             human to read directly (`python3 vllm_config.py --checkpoint X`).

Everything here is a *derived default*, not a guarantee -- an unrecognized
`model_type` falls back to no extra flags with a warning on stderr rather
than guessing. Always sanity-check the printed settings before submitting a
real job; that's the whole point of printing them instead of hiding them
inside the launcher.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

# Weight-file extensions checked when estimating checkpoint size. Sharded
# checkpoints (model-00001-of-00004.safetensors, ...) are summed correctly
# because every shard matches the glob.
WEIGHT_FILE_GLOBS = ("*.safetensors", "*.bin")

# Per-family `vllm serve` flags, keyed on config.json's top-level
# "model_type". Only architecture-affecting flags belong here -- no
# --reasoning-parser or --tool-call-parser: the regurgitation probe reads
# the model's raw `message.content` (reasoning, answer, and any tool-call
# markup together) and needs nothing stripped out of it before it checks
# for withheld PII.
#
# "vision_flag": appended only when config.json also has a non-empty
# "vision_config" (a text+vision checkpoint served text-only).
#
# "sampling_thinking" / "sampling_instruct": the family's published sampling
# profile, with temperature and max_tokens removed on purpose -- temperature
# is the probe's own --temperature flag, and the token budget is decided by
# the caller from whether thinking is on, not baked in here.
FAMILIES: dict[str, dict[str, Any]] = {
    "qwen3_5": {
        "flags": ["--gdn-prefill-backend", "triton"],
        "vision_flag": "--language-model-only",
        "sampling_thinking": {
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
        },
        "sampling_instruct": {},
    },
}

DEFAULT_MAX_MODEL_LEN_CAP = 65536


class VllmConfigError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Derive vllm serve settings (dtype, max-model-len, family "
            "flags, sampling profile) from a checkpoint's own config files."
        )
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        type=Path,
        help="Path to a Hugging Face checkpoint directory (config.json, weights, tokenizer).",
    )
    parser.add_argument(
        "--max-model-len-cap",
        type=int,
        default=DEFAULT_MAX_MODEL_LEN_CAP,
        help=(
            "Upper bound applied to the checkpoint's own max_position_embeddings "
            f"(default: {DEFAULT_MAX_MODEL_LEN_CAP}). Keeps vLLM startup (KV cache "
            "allocation) fast on checkpoints that advertise a huge context window."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one pretty-printed JSON object instead of shell export lines.",
    )
    return parser.parse_args()


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise VllmConfigError(f"{path} does not contain a JSON object")
    return data


def load_optional_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return load_json_file(path)
    except (json.JSONDecodeError, VllmConfigError):
        return {}


def human_readable_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"


def total_weight_bytes(checkpoint_dir: Path) -> int:
    seen: set[Path] = set()
    for pattern in WEIGHT_FILE_GLOBS:
        seen.update(checkpoint_dir.rglob(pattern))
    return sum(path.stat().st_size for path in seen if path.is_file())


def suggest_tensor_parallel_size(weight_bytes: int) -> int:
    """A rough starting point, not a promise: one process per ~20 GiB of

    weights, capped at 8 (a full H100 node on this cluster). bf16 weights
    plus KV cache plus activations do not fit neatly into any fixed ratio,
    so treat this as a first guess to sanity-check against the checkpoint's
    actual size, not a load-bearing calculation.
    """
    weight_gib = weight_bytes / (1024**3)
    if weight_gib > 80:
        return 8
    if weight_gib > 40:
        return 4
    if weight_gib > 20:
        return 2
    return 1


def resolve_dtype(config: dict[str, Any], text_config: dict[str, Any], warnings: list[str]) -> str:
    dtype = text_config.get("dtype") or config.get("dtype")
    if isinstance(dtype, str) and dtype:
        return dtype
    warnings.append("no 'dtype' found in config.json -- defaulting to bfloat16")
    return "bfloat16"


def resolve_max_position_embeddings(
    config: dict[str, Any], text_config: dict[str, Any]
) -> int | None:
    value = text_config.get("max_position_embeddings")
    if isinstance(value, int) and value > 0:
        return value
    value = config.get("max_position_embeddings")
    if isinstance(value, int) and value > 0:
        return value
    return None


def resolve_model_type(config: dict[str, Any], text_config: dict[str, Any]) -> str:
    model_type = config.get("model_type") or text_config.get("model_type")
    return str(model_type) if model_type else "unknown"


def resolve_family(
    model_type: str, has_vision: bool, warnings: list[str]
) -> tuple[str, list[str], dict[str, float], dict[str, float]]:
    family = FAMILIES.get(model_type)
    if family is None:
        warnings.append(
            f"model_type '{model_type}' is not in a known family -- no extra "
            "vllm serve flags added. Check the checkpoint's README/config and "
            "add flags manually with the wrapper's extra-args setting if needed."
        )
        return "unknown", [], {}, {}

    flags = list(family["flags"])
    if has_vision and family.get("vision_flag"):
        flags.append(family["vision_flag"])
    return model_type, flags, dict(family["sampling_thinking"]), dict(family["sampling_instruct"])


def detect_thinking_support(checkpoint_dir: Path, warnings: list[str]) -> bool:
    template_path = checkpoint_dir / "chat_template.jinja"
    if not template_path.is_file():
        warnings.append(
            "no chat_template.jinja found -- assuming the chat template does not "
            "support an enable_thinking toggle"
        )
        return False
    try:
        template_text = template_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"could not read chat_template.jinja: {exc}")
        return False
    return "enable_thinking" in template_text


def derive_settings(checkpoint_dir: Path, max_model_len_cap: int) -> dict[str, Any]:
    warnings: list[str] = []

    config_path = checkpoint_dir / "config.json"
    if not config_path.is_file():
        raise VllmConfigError(f"no config.json under {checkpoint_dir} -- is this a checkpoint directory?")
    config = load_json_file(config_path)
    text_config = config.get("text_config")
    text_config = text_config if isinstance(text_config, dict) else {}

    generation_config = load_optional_json_file(checkpoint_dir / "generation_config.json")

    has_vision = bool(config.get("vision_config"))
    model_type = resolve_model_type(config, text_config)
    family, family_flags, sampling_thinking, sampling_instruct = resolve_family(
        model_type, has_vision, warnings
    )

    dtype = resolve_dtype(config, text_config, warnings)

    max_position_embeddings = resolve_max_position_embeddings(config, text_config)
    if max_position_embeddings is None:
        warnings.append(
            "no max_position_embeddings found in config.json -- using "
            f"--max-model-len-cap ({max_model_len_cap}) directly"
        )
        max_model_len = max_model_len_cap
    else:
        max_model_len = min(max_position_embeddings, max_model_len_cap)

    thinking_supported = detect_thinking_support(checkpoint_dir, warnings)

    weight_bytes = total_weight_bytes(checkpoint_dir)
    if weight_bytes == 0:
        warnings.append(
            "no *.safetensors or *.bin weight files found under the checkpoint "
            "directory -- weight size and the tensor-parallel suggestion are unavailable"
        )

    architectures = config.get("architectures")
    architectures = architectures if isinstance(architectures, list) else []

    # generation_config.json's sampling knobs, if the checkpoint author set
    # any. Present here purely as extra evidence for a human reviewing the
    # derived settings -- they are not merged into sampling_thinking /
    # sampling_instruct, since generation_config.json on this checkpoint
    # (and most merged checkpoints) only carries token ids, not a sampling
    # recipe.
    generation_config_sampling = {
        key: generation_config[key]
        for key in ("temperature", "top_p", "top_k", "repetition_penalty", "presence_penalty")
        if key in generation_config
    }

    return {
        "checkpoint_path": str(checkpoint_dir),
        "served_name": checkpoint_dir.name,
        "model_type": model_type,
        "architectures": architectures,
        "has_vision_config": has_vision,
        "family": family,
        "family_flags": family_flags,
        "dtype": dtype,
        "max_position_embeddings": max_position_embeddings,
        "max_model_len_cap": max_model_len_cap,
        "max_model_len": max_model_len,
        "thinking_supported": thinking_supported,
        "sampling_thinking": sampling_thinking,
        "sampling_instruct": sampling_instruct,
        "generation_config_sampling": generation_config_sampling,
        "weight_bytes": weight_bytes,
        "weight_human": human_readable_bytes(weight_bytes),
        "tensor_parallel_size_suggested": suggest_tensor_parallel_size(weight_bytes),
        "warnings": warnings,
    }


def print_shell_export_lines(settings: dict[str, Any]) -> None:
    def export(name: str, value: Any) -> None:
        print(f"export {name}={shlex.quote(str(value))}")

    export("CKPT_PATH_RESOLVED", settings["checkpoint_path"])
    export("CKPT_SERVED_NAME", settings["served_name"])
    export("CKPT_MODEL_TYPE", settings["model_type"])
    export("CKPT_FAMILY", settings["family"])
    export("CKPT_DTYPE", settings["dtype"])
    export("CKPT_MAX_MODEL_LEN", settings["max_model_len"])
    export("CKPT_FAMILY_FLAGS_JSON", json.dumps(settings["family_flags"]))
    export("CKPT_THINKING_SUPPORTED", 1 if settings["thinking_supported"] else 0)
    export("CKPT_SAMPLING_THINK_JSON", json.dumps(settings["sampling_thinking"]))
    export("CKPT_SAMPLING_INSTRUCT_JSON", json.dumps(settings["sampling_instruct"]))
    export("CKPT_WEIGHT_BYTES", settings["weight_bytes"])
    export("CKPT_WEIGHT_HUMAN", settings["weight_human"])
    export("CKPT_TP_SUGGESTED", settings["tensor_parallel_size_suggested"])
    for warning in settings["warnings"]:
        print(f"# warning: {warning}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    checkpoint_dir = args.checkpoint.expanduser().resolve()

    if not checkpoint_dir.is_dir():
        print(f"error: checkpoint path is not a directory: {checkpoint_dir}", file=sys.stderr)
        return 2

    try:
        settings = derive_settings(checkpoint_dir, args.max_model_len_cap)
    except VllmConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(settings, indent=2, ensure_ascii=False))
    else:
        print_shell_export_lines(settings)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
