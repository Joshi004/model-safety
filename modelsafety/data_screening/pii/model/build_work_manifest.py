#!/usr/bin/env python3
"""Create deterministic byte-range work units for standalone PII scanning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Run as a script from this folder, so the repo root isn't on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from common import WorkUnit, make_unit_id  # noqa: E402
from modelsafety.contract.origin import check_first_line  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    DEFAULT_INPUT_MODE,
    INPUT_MODE_HELP,
    INPUT_MODES,
    SCAN_INPUTS_FILENAME,
    ScanSelection,
    check_fingerprints,
    folder_selection,
    pick_files,
    select_scan_files,
    summarize_scan_inputs,
    write_scan_inputs,
)


def resolve_sources(input_root: Path, source_manifest: Path | None) -> list[Path]:
    root = input_root.expanduser().resolve()
    if source_manifest is None:
        return sorted(path.resolve() for path in root.rglob("*.jsonl") if path.is_file())

    sources: list[Path] = []
    seen: set[Path] = set()
    with source_manifest.expanduser().resolve().open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            relative = line.strip()
            if not relative or relative.startswith("#"):
                continue
            source = (root / relative).resolve()
            try:
                source.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"{source_manifest}:{line_number}: path escapes input root"
                ) from exc
            if source.suffix != ".jsonl" or not source.is_file():
                raise ValueError(
                    f"{source_manifest}:{line_number}: missing JSONL: {relative}"
                )
            if source in seen:
                raise ValueError(f"{source_manifest}:{line_number}: duplicate path")
            seen.add(source)
            sources.append(source)
    return sources


def choose_sources(
    input_root: Path, source_manifest: Path | None, input_mode: str
) -> tuple[list[Path], ScanSelection]:
    """The source files, and the selection that records where each came from.

    In runs mode the files are those of finished runs under ``input_root``; a
    source manifest, if given, narrows them and must list only such files.
    """
    root = input_root.expanduser().resolve()
    if input_mode == "runs":
        selection = select_scan_files(root)
        if source_manifest is None:
            sources = [root / item.path for item in selection.files]
        else:
            sources = resolve_sources(root, source_manifest)
    else:
        sources = resolve_sources(root, source_manifest)
        selection = folder_selection(root, [source.relative_to(root).as_posix() for source in sources])
    return sources, pick_files(selection, [source.relative_to(root).as_posix() for source in sources])


def units_for_source(
    root: Path, source: Path, target_bytes: int, digest: hashlib._Hash
) -> Iterable[WorkUnit]:
    """Yield the work units of one source. ``digest`` is fed every byte of the file."""
    relative = source.relative_to(root).as_posix()
    stat = source.stat()
    start_byte = 0
    start_line = 1
    end_line = 0
    with source.open("rb") as handle:
        while raw_line := handle.readline():
            digest.update(raw_line)
            end_line += 1
            end_byte = handle.tell()
            if end_byte - start_byte < target_bytes:
                continue
            yield WorkUnit(
                unit_id=make_unit_id(relative, start_byte, end_byte),
                source_file=relative,
                start_byte=start_byte,
                end_byte=end_byte,
                start_line=start_line,
                end_line=end_line,
                source_size=stat.st_size,
                source_mtime_ns=stat.st_mtime_ns,
            )
            start_byte = end_byte
            start_line = end_line + 1

    if start_byte < stat.st_size:
        yield WorkUnit(
            unit_id=make_unit_id(relative, start_byte, stat.st_size),
            source_file=relative,
            start_byte=start_byte,
            end_byte=stat.st_size,
            start_line=start_line,
            end_line=end_line,
            source_size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
        )


def build_manifest(
    input_root: Path,
    output_dir: Path,
    source_manifest: Path | None,
    target_bytes: int,
    max_files: int | None = None,
    input_mode: str = DEFAULT_INPUT_MODE,
) -> dict[str, object]:
    root = input_root.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if target_bytes < 1:
        raise ValueError("target_bytes must be positive")
    sources, selection = choose_sources(root, source_manifest, input_mode)
    if max_files is not None:
        sources = sources[:max_files]
        selection = pick_files(selection, [source.relative_to(root).as_posix() for source in sources])
    if not sources:
        raise ValueError("No JSONL sources found")
    for source in sources:
        check_first_line(source)

    output.mkdir(parents=True, exist_ok=True)
    write_scan_inputs(output / SCAN_INPUTS_FILENAME, selection)
    print(summarize_scan_inputs(selection))
    expected_sha256 = {item.path: item.sha256 for item in selection.files}
    units_path = output / "work_units.jsonl"
    temp_path = output / f".work_units.{os.getpid()}.partial"
    unit_count = 0
    total_bytes = 0
    source_rows: list[dict[str, object]] = []
    with temp_path.open("w", encoding="utf-8") as handle:
        for source in sources:
            stat = source.stat()
            relative = source.relative_to(root).as_posix()
            source_unit_count = 0
            digest = hashlib.sha256()
            for unit in units_for_source(root, source, target_bytes, digest):
                handle.write(json.dumps(unit.to_dict(), sort_keys=True) + "\n")
                unit_count += 1
                source_unit_count += 1
            if selection.mode == "runs":
                check_fingerprints(
                    {relative: expected_sha256[relative]},
                    [{"path": relative, "sha256": digest.hexdigest()}],
                    what="The NER work manifest",
                )
            total_bytes += stat.st_size
            source_rows.append(
                {
                    "source_file": relative,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "unit_count": source_unit_count,
                    "sha256": digest.hexdigest(),
                }
            )
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(units_path)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(root),
        "input_mode": input_mode,
        "work_units": str(units_path),
        "target_bytes": target_bytes,
        "source_count": len(sources),
        "unit_count": unit_count,
        "total_bytes": total_bytes,
        "sources": source_rows,
    }
    summary_temp = output / f".manifest.{os.getpid()}.partial"
    summary_temp.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_temp.replace(output / "manifest.json")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--target-mib", type=int, default=256)
    parser.add_argument("--max-files", type=int)
    parser.add_argument(
        "--input-mode", choices=INPUT_MODES, default=DEFAULT_INPUT_MODE, help=INPUT_MODE_HELP
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = build_manifest(
            args.input_root,
            args.output_dir,
            args.source_manifest,
            args.target_mib * 1024 * 1024,
            args.max_files,
            args.input_mode,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Wrote {summary['unit_count']:,} work units for "
        f"{summary['source_count']:,} source files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

