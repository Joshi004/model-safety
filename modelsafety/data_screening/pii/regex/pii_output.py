"""Shared PII scan output layout and metrics writers."""

from __future__ import annotations

import os
from pathlib import Path

PII_CATEGORIES = [
    "emails",
    "phones",
    "phones_with_exts",
    "ukphones",
    "street_addresses",
    "po_boxes",
    "credit_cards",
    "ips",
    "ipv6s",
    "btc_addresses",
]

STAGE1_NAME = "stage1_scan"
STAGE2_NAME = "stage2_validated"
HITS_DIRNAME = "hits"
WORKERS_DIRNAME = "workers"
METRICS_FILENAME = "metrics.txt"


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_run_dir() -> Path:
    return script_dir() / "out" / "pii_run"


def resolve_run_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Run root: parent folder that contains stage1_scan/ and stage2_validated/."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    for env_name in ("PII_RUN_DIR", "PII_OUTPUT_DIR"):
        env = os.environ.get(env_name)
        if env:
            return infer_run_dir_from_path(Path(env).expanduser().resolve())
    scan = os.environ.get("PII_SCAN_OUTPUT_DIR")
    if scan:
        return infer_run_dir_from_path(Path(scan).expanduser().resolve())
    return default_run_dir().resolve()


def workers_dir(stage_path: Path) -> Path:
    nested = stage_path / WORKERS_DIRNAME
    nested.mkdir(parents=True, exist_ok=True)
    return nested


def stage1_paths(run_dir: Path | None = None) -> dict[str, Path]:
    root = run_dir or resolve_run_dir()
    stage = stage_dir(root, STAGE1_NAME)
    hits = stage / HITS_DIRNAME
    return {
        "run_dir": root,
        "stage_dir": stage,
        "hits_dir": hits,
        "workers_dir": stage / WORKERS_DIRNAME,
        "metrics_file": stage / METRICS_FILENAME,
    }


def stage_dir(run_dir: Path, stage_name: str) -> Path:
    return run_dir / stage_name


def hits_dir(stage_path: Path) -> Path:
    """Return the directory containing category *.txt hit files."""
    nested = stage_path / HITS_DIRNAME
    if nested.is_dir():
        return nested
    if any((stage_path / f"{category}.txt").exists() for category in PII_CATEGORIES):
        return stage_path
    nested.mkdir(parents=True, exist_ok=True)
    return nested


def infer_run_dir_from_path(path: Path) -> Path:
    if path.name == HITS_DIRNAME:
        if path.parent.name in (STAGE1_NAME, STAGE2_NAME):
            return path.parent.parent
        return path.parent
    if path.name in (STAGE1_NAME, STAGE2_NAME):
        return path.parent
    if any((path / f"{category}.txt").exists() for category in PII_CATEGORIES):
        return path
    return path


def stage2_paths(run_dir: Path | None = None) -> dict[str, Path]:
    root = run_dir or resolve_run_dir()
    stage = stage_dir(root, STAGE2_NAME)
    return {
        "run_dir": root,
        "stage_dir": stage,
        "hits_dir": stage / HITS_DIRNAME,
        "metrics_file": stage / METRICS_FILENAME,
    }


def resolve_stage1_hits_dir() -> Path:
    override = os.environ.get("PII_SCAN_OUTPUT_DIR")
    if override:
        path = Path(override).expanduser().resolve()
        if path.name == HITS_DIRNAME:
            return path
        nested = path / HITS_DIRNAME
        if nested.is_dir():
            return nested
        if any((path / f"{category}.txt").exists() for category in PII_CATEGORIES):
            return path
        return nested
    paths = stage1_paths()
    return paths["hits_dir"]


def resolve_stage2_hits_dir() -> Path:
    override = os.environ.get("PII_VALIDATED_OUTPUT_DIR")
    if override:
        path = Path(override).expanduser().resolve()
        return hits_dir(path) if path.name != HITS_DIRNAME else path
    paths = stage2_paths()
    paths["hits_dir"].mkdir(parents=True, exist_ok=True)
    return paths["hits_dir"]


def count_lines_in_hits(hits_path: Path, categories: list[str] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for category in categories or PII_CATEGORIES:
        hit_file = hits_path / f"{category}.txt"
        if not hit_file.exists():
            counts[category] = 0
            continue
        with hit_file.open("r", encoding="utf-8") as handle:
            counts[category] = sum(1 for line in handle if line.strip())
    return counts


def unique_hit_locations(hits_path: Path) -> int:
    locations: set[tuple[str, str]] = set()
    for category in PII_CATEGORIES:
        hit_file = hits_path / f"{category}.txt"
        if not hit_file.exists():
            continue
        with hit_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split("|")
                if len(parts) >= 2:
                    locations.add((parts[0], parts[1]))
    return len(locations)


def write_stage1_metrics(
    metrics_file: Path,
    *,
    run_dir: Path,
    base_dir: Path,
    total_files: int,
    total_chunks: int,
    chunk_size: int,
    total_lines: int,
    processing_time: float,
    pii_counts: dict[str, int],
    num_workers: int,
) -> None:
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    hits_path = metrics_file.parent / HITS_DIRNAME
    total_pii = sum(pii_counts.values())

    with metrics_file.open("w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("PII STAGE 1 — REGEX SCAN\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Run directory:     {run_dir}\n")
        f.write(f"Input root:          {base_dir}\n")
        f.write(f"Hits directory:    {hits_path}\n")
        f.write(f"Workers directory: {metrics_file.parent / WORKERS_DIRNAME}\n\n")
        f.write(f"JSONL files scanned: {total_files:,}\n")
        f.write(f"Chunks processed:    {total_chunks:,}\n")
        f.write(f"Chunk size:            {chunk_size:,} lines\n")
        f.write(f"Lines scanned:         {total_lines:,}\n")
        f.write(f"Workers:               {num_workers}\n")
        f.write(f"Processing time:       {processing_time:.2f} s\n")
        f.write(f"Lines per second:      {total_lines / processing_time if processing_time else 0:.2f}\n\n")
        f.write("Raw regex hits by category:\n")
        f.write("-" * 60 + "\n")
        for category in PII_CATEGORIES:
            f.write(f"{category:20s}: {pii_counts.get(category, 0):,}\n")
        f.write("-" * 60 + "\n")
        f.write(f"{'TOTAL HITS':20s}: {total_pii:,}\n")
        f.write(f"{'UNIQUE LOCATIONS':20s}: {unique_hit_locations(hits_path):,}\n")
        f.write("\nNext step:\n")
        f.write(f"  export PII_RUN_DIR={run_dir}\n")
        f.write("  python pii_validator.py\n")


def write_stage2_metrics(
    metrics_file: Path,
    *,
    run_dir: Path,
    input_hits_dir: Path,
    output_hits_dir: Path,
    results: dict[str, tuple[int, int]],
    processing_time: float,
    num_workers: int,
) -> None:
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    grand_total = sum(total for total, _ in results.values())
    grand_valid = sum(valid for _, valid in results.values())
    grand_filtered = grand_total - grand_valid

    with metrics_file.open("w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("PII STAGE 2 — HEURISTIC VALIDATION\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Run directory:  {run_dir}\n")
        f.write(f"Input hits:     {input_hits_dir}\n")
        f.write(f"Validated hits: {output_hits_dir}\n")
        f.write(f"Workers:        {num_workers}\n")
        f.write(f"Processing time: {processing_time:.2f} s\n\n")
        f.write(f"{'Category':<22} {'Raw':>10} {'Kept':>10} {'Filtered':>10} {'Keep %':>8}\n")
        f.write("-" * 60 + "\n")
        for filename in sorted(results):
            total, valid = results[filename]
            filtered = total - valid
            keep_pct = (100 * valid / total) if total else 0.0
            category = filename.replace(".txt", "")
            f.write(f"{category:<22} {total:>10,} {valid:>10,} {filtered:>10,} {keep_pct:>7.1f}%\n")
        f.write("-" * 60 + "\n")
        keep_pct = (100 * grand_valid / grand_total) if grand_total else 0.0
        f.write(f"{'TOTAL':<22} {grand_total:>10,} {grand_valid:>10,} {grand_filtered:>10,} {keep_pct:>7.1f}%\n")
        f.write(f"\nUnique validated locations: {unique_hit_locations(output_hits_dir):,}\n")
        if grand_valid:
            f.write("\nNext step:\n")
            f.write("  python pii_extractor.py \\\n")
            f.write(f"    --input-dir {output_hits_dir} \\\n")
            f.write("    --source-root <jsonl-root>\n")
        else:
            f.write("\nNo validated hits — stage 3 (extractor) can be skipped.\n")


def print_run_summary(run_dir: Path) -> None:
    stage1_metrics = run_dir / STAGE1_NAME / METRICS_FILENAME
    stage2_metrics = run_dir / STAGE2_NAME / METRICS_FILENAME
    print("\nOutput layout:")
    print(f"  Run root: {run_dir}")
    print(f"  Stage 1:  {run_dir / STAGE1_NAME}/")
    print(f"    metrics -> {stage1_metrics}")
    print(f"    hits    -> {run_dir / STAGE1_NAME / HITS_DIRNAME}/")
    if stage2_metrics.exists():
        print(f"  Stage 2:  {run_dir / STAGE2_NAME}/")
        print(f"    metrics -> {stage2_metrics}")
        print(f"    hits    -> {run_dir / STAGE2_NAME / HITS_DIRNAME}/")
