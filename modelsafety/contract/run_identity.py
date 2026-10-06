#!/usr/bin/env python3
"""Refuse to reuse a run folder that holds a different run.

A run writes ``run_identity.json`` into its folder: the tool, the mode, the
settings that change what the results mean, and the input files with their
sizes and last-modified times. Starting a run again in the same folder
carries on only if that identity is unchanged.

Standard library only, so shell launchers can call it with the system
``python3``:

    python3 modelsafety/contract/run_identity.py check --run-dir D --tool T \\
        --input-root R [--mode M] [--setting key=value ...] [--input-list FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

IDENTITY_FILENAME = "run_identity.json"

_CLOSING_SENTENCE = "Use a different folder, or delete this one yourself if you no longer need it."
_MAX_EXAMPLES = 5
_REQUIRED_KEYS = ("tool", "mode", "settings", "input_root", "input_files")
_MISSING = object()


class RunIdentityError(RuntimeError):
    """The run folder holds a different run, or something unrecognised."""


def describe_input_files(base_dir: Path, files: list[Path]) -> list[dict[str, Any]]:
    """Path (relative to ``base_dir``), size and last-modified time of each file."""
    described = []
    for file_path in files:
        stat = file_path.stat()
        described.append(
            {
                "path": file_path.relative_to(base_dir).as_posix(),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return sorted(described, key=lambda item: item["path"])


def make_identity(
    tool: str,
    mode: str | None,
    settings: dict[str, Any],
    input_root: str,
    input_files: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "tool": tool,
        "mode": mode,
        "settings": dict(sorted(settings.items())),
        "input_root": input_root,
        "input_files": input_files,
    }


def _show(value: Any) -> str:
    if value is _MISSING:
        return "(missing)"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _count_with_examples(count_word: str, paths: list[str]) -> str:
    text = f"{len(paths)} {count_word}"
    if paths:
        text += f" (e.g. {', '.join(paths[:_MAX_EXAMPLES])})"
    return text


def _describe_differences(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    differences = []

    for key in ("tool", "mode"):
        if old[key] != new[key]:
            differences.append(f"{key}: {_show(old[key])} -> {_show(new[key])}")

    for key in sorted(set(old["settings"]) | set(new["settings"])):
        old_value = old["settings"].get(key, _MISSING)
        new_value = new["settings"].get(key, _MISSING)
        if old_value != new_value:
            differences.append(f"{key}: {_show(old_value)} -> {_show(new_value)}")

    if old["input_root"] != new["input_root"]:
        differences.append(f"input root: {old['input_root']} -> {new['input_root']}")

    old_files = {item["path"]: item for item in old["input_files"]}
    new_files = {item["path"]: item for item in new["input_files"]}
    added = sorted(new_files.keys() - old_files.keys())
    removed = sorted(old_files.keys() - new_files.keys())
    changed = sorted(
        path
        for path in old_files.keys() & new_files.keys()
        if old_files[path] != new_files[path]
    )
    if added or removed or changed:
        differences.append(
            "input files: "
            + ", ".join(
                [
                    _count_with_examples("added", added),
                    _count_with_examples("removed", removed),
                    _count_with_examples("changed", changed),
                ]
            )
        )
    return differences


def _load_stored_identity(identity_path: Path) -> dict[str, Any]:
    try:
        stored = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunIdentityError(
            f"{identity_path} could not be read ({exc}). {_CLOSING_SENTENCE}"
        ) from exc
    well_formed = (
        isinstance(stored, dict)
        and all(key in stored for key in _REQUIRED_KEYS)
        and isinstance(stored["settings"], dict)
        and isinstance(stored["input_files"], list)
        and all(isinstance(item, dict) and "path" in item for item in stored["input_files"])
    )
    if not well_formed:
        raise RunIdentityError(
            f"{identity_path} does not look like a run identity file. {_CLOSING_SENTENCE}"
        )
    return stored


def check_run_identity(run_dir: str | Path, identity: dict[str, Any]) -> str:
    """Return ``"new"`` or ``"resume"``, or raise ``RunIdentityError``.

    A missing or empty folder gets ``identity`` written into it. Nothing else
    in the folder is ever changed.
    """
    run_dir = Path(run_dir)
    identity_path = run_dir / IDENTITY_FILENAME

    if run_dir.exists() and not run_dir.is_dir():
        raise RunIdentityError(f"{run_dir} exists and is not a folder.")

    if not run_dir.exists() or not any(run_dir.iterdir()):
        run_dir.mkdir(parents=True, exist_ok=True)
        stamped = {
            **identity,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        identity_path.write_text(json.dumps(stamped, indent=2) + "\n", encoding="utf-8")
        return "new"

    if not identity_path.exists():
        raise RunIdentityError(
            f"{run_dir} is not empty but has no {IDENTITY_FILENAME}, so it holds output "
            f"from before run identities existed (or something else). {_CLOSING_SENTENCE}"
        )

    stored = _load_stored_identity(identity_path)
    stored.pop("created_at", None)
    requested = json.loads(json.dumps(identity))
    if stored == requested:
        return "resume"

    differences = _describe_differences(stored, requested)
    raise RunIdentityError(
        f"{run_dir} already holds a different run (previous -> requested):\n"
        + "\n".join(f"  {line}" for line in differences)
        + f"\n{_CLOSING_SENTENCE}"
    )


def _parse_settings(items: list[str]) -> dict[str, str]:
    settings = {}
    for item in items:
        if "=" not in item:
            raise RunIdentityError(f"Invalid --setting '{item}'. Expected key=value.")
        key, value = item.split("=", 1)
        settings[key] = value
    return settings


def _input_files_from_list(root: Path, list_path: Path) -> list[Path]:
    files = []
    for line in list_path.read_text(encoding="utf-8").splitlines():
        relative = line.strip()
        if not relative:
            continue
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise RunIdentityError(
                f"{list_path} lists '{relative}'. Paths must be relative and stay inside {root}."
            )
        file_path = root / relative
        if not file_path.is_file():
            raise RunIdentityError(f"{list_path} lists '{relative}', which is not a file under {root}.")
        files.append(file_path)
    return files


def _run_check(args: argparse.Namespace) -> int:
    root = args.input_root.resolve()
    if not root.is_dir():
        raise RunIdentityError(f"Input root is not a directory: {root}")
    if args.input_list is not None:
        files = _input_files_from_list(root, args.input_list)
    else:
        files = sorted(path for path in root.rglob("*.jsonl") if path.is_file())

    identity = make_identity(
        tool=args.tool,
        mode=args.mode,
        settings=_parse_settings(args.setting),
        input_root=str(root),
        input_files=describe_input_files(root, files),
    )
    print(check_run_identity(args.run_dir, identity))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="Write or verify run_identity.json in a run folder.")
    check.add_argument("--run-dir", type=Path, required=True)
    check.add_argument("--tool", required=True)
    check.add_argument("--mode", default=None)
    check.add_argument(
        "--setting",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="A setting that changes what the results mean. Repeatable.",
    )
    check.add_argument("--input-root", type=Path, required=True)
    check.add_argument(
        "--input-list",
        type=Path,
        default=None,
        help="File of relative paths, one per line. Default: every *.jsonl under --input-root.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run_check(args)
    except RunIdentityError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
