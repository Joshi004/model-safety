#!/usr/bin/env python3
"""Build deterministic, size-balanced JSONL manifests for QA scan arrays."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    size_bytes: int
    mtime_ns: int
    sha256: str = ""


def discover_jsonl(input_root: Path) -> list[SourceFile]:
    """Return JSONL files below ``input_root`` in stable relative-path order."""
    root = input_root.resolve()
    files = []
    for path in root.rglob("*.jsonl"):
        if not path.is_file():
            continue
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append(
            SourceFile(
                path.relative_to(root).as_posix(),
                stat.st_size,
                stat.st_mtime_ns,
                digest.hexdigest(),
            )
        )
    return sorted(files, key=lambda item: item.relative_path)


def stratified_pilot(files: list[SourceFile], limit: int) -> list[SourceFile]:
    """Select a deterministic pilot spread across top-level source groups."""
    if limit <= 0 or limit >= len(files):
        return files

    groups: dict[str, list[SourceFile]] = {}
    for item in files:
        group = item.relative_path.partition("/")[0]
        groups.setdefault(group, []).append(item)

    selected: list[SourceFile] = []
    positions = {group: 0 for group in groups}
    ordered_groups = sorted(groups)
    target_size = 128 * 1024 * 1024
    while len(selected) < limit:
        made_progress = False
        for group in ordered_groups:
            choices = sorted(
                groups[group],
                key=lambda item: (
                    abs(item.size_bytes - target_size),
                    item.relative_path,
                ),
            )
            position = positions[group]
            if position >= len(choices):
                continue
            selected.append(choices[position])
            positions[group] += 1
            made_progress = True
            if len(selected) == limit:
                break
        if not made_progress:
            break
    return sorted(selected, key=lambda item: item.relative_path)


def balance_files(files: Iterable[SourceFile], shard_count: int) -> list[list[SourceFile]]:
    """Greedily balance files by bytes while keeping every file whole."""
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")

    shards: list[list[SourceFile]] = [[] for _ in range(shard_count)]
    totals = [0] * shard_count
    for item in sorted(files, key=lambda value: (-value.size_bytes, value.relative_path)):
        index = min(range(shard_count), key=lambda i: (totals[i], i))
        shards[index].append(item)
        totals[index] += item.size_bytes

    for shard in shards:
        shard.sort(key=lambda item: item.relative_path)
    return shards


def write_manifests(
    input_root: Path,
    output_dir: Path,
    shards: list[list[SourceFile]],
    *,
    pilot_files: int,
) -> Path:
    """Write one relative-path manifest per shard and an aggregate metadata file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_dir = output_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for stale_manifest in shard_dir.glob("shard_*.txt"):
        stale_manifest.unlink()

    shard_records = []
    for index, shard in enumerate(shards):
        manifest_path = shard_dir / f"shard_{index:05d}.txt"
        manifest_path.write_text(
            "".join(f"{item.relative_path}\n" for item in shard),
            encoding="utf-8",
        )
        shard_records.append(
            {
                "index": index,
                "manifest": str(manifest_path.resolve()),
                "file_count": len(shard),
                "size_bytes": sum(item.size_bytes for item in shard),
            }
        )

    all_files = [item for shard in shards for item in shard]
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root.resolve()),
        "output_dir": str(output_dir.resolve()),
        "pilot_files": pilot_files,
        "file_count": len(all_files),
        "size_bytes": sum(item.size_bytes for item in all_files),
        "shard_count": len(shards),
        "shards": shard_records,
        "files": [asdict(item) for item in sorted(all_files, key=lambda item: item.relative_path)],
    }
    metadata_path = output_dir / "manifest.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument(
        "--pilot-files",
        type=int,
        default=0,
        help="Select at most this many files, spread across top-level groups.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input_root.is_dir():
        raise SystemExit(f"Input root is not a directory: {args.input_root}")
    if args.shards < 1:
        raise SystemExit("--shards must be at least 1")

    files = discover_jsonl(args.input_root)
    if not files:
        raise SystemExit(f"No JSONL files found under {args.input_root}")
    selected = stratified_pilot(files, args.pilot_files)
    if args.shards > len(selected):
        raise SystemExit(
            f"--shards ({args.shards}) exceeds selected file count ({len(selected)})"
        )
    metadata_path = write_manifests(
        args.input_root,
        args.output_dir,
        balance_files(selected, args.shards),
        pilot_files=args.pilot_files,
    )
    print(metadata_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
