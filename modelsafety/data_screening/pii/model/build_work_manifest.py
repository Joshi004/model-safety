#!/usr/bin/env python3
"""Create deterministic byte-range work units for standalone PII scanning."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from common import WorkUnit, make_unit_id


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


def units_for_source(root: Path, source: Path, target_bytes: int) -> Iterable[WorkUnit]:
    relative = source.relative_to(root).as_posix()
    stat = source.stat()
    start_byte = 0
    start_line = 1
    end_line = 0
    with source.open("rb") as handle:
        while raw_line := handle.readline():
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
) -> dict[str, object]:
    root = input_root.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if target_bytes < 1:
        raise ValueError("target_bytes must be positive")
    sources = resolve_sources(root, source_manifest)
    if max_files is not None:
        sources = sources[:max_files]
    if not sources:
        raise ValueError("No JSONL sources found")

    output.mkdir(parents=True, exist_ok=True)
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
            for unit in units_for_source(root, source, target_bytes):
                handle.write(json.dumps(unit.to_dict(), sort_keys=True) + "\n")
                unit_count += 1
                source_unit_count += 1
            total_bytes += stat.st_size
            source_rows.append(
                {
                    "source_file": relative,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "unit_count": source_unit_count,
                }
            )
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(units_path)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(root),
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

