#!/usr/bin/env python3
"""Join PII hit locations to source JSONL rows with bounded memory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from tqdm import tqdm

from pii_output import PII_CATEGORIES
from modelsafety.contract.textract import (
    load_source_snapshot,
    parse_record_text,
    verify_source_snapshot,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "out" / "pii_run" / "stage2_validated" / "hits"
DEFAULT_SOURCE_ROOT = SCRIPT_DIR.parent / "data"
DEFAULT_OUTPUT = SCRIPT_DIR / "out" / "pii_run" / "stage3_extract" / "pii_extract.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--snapshot-manifest", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delimiter", default=",")
    return parser.parse_args()


def parse_hit(line: str, path: Path, line_number: int) -> tuple[str, int, str, str]:
    parts = line.rstrip("\r\n").split("|", 3)
    if len(parts) != 4:
        raise ValueError(f"{path}:{line_number}: malformed hit row")
    source_file, source_line, source_sha256, encoded_pii_value = parts
    try:
        source_line_number = int(source_line)
    except ValueError as exc:
        raise ValueError(f"{path}:{line_number}: non-numeric source line") from exc
    if source_line_number < 1:
        raise ValueError(f"{path}:{line_number}: source line must be positive")
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValueError(f"{path}:{line_number}: invalid source SHA-256")
    try:
        pii_value = json.loads(encoded_pii_value)
    except json.JSONDecodeError:
        pii_value = encoded_pii_value
    if not isinstance(pii_value, str):
        pii_value = str(pii_value)
    return source_file, source_line_number, source_sha256, pii_value


def index_hits(
    input_dir: Path,
    connection: sqlite3.Connection,
) -> int:
    connection.execute(
        "CREATE TABLE hits (source_file TEXT NOT NULL, source_line INTEGER NOT NULL, "
        "source_sha256 TEXT NOT NULL, category TEXT NOT NULL, pii TEXT NOT NULL)"
    )
    insert_sql = "INSERT INTO hits VALUES (?, ?, ?, ?, ?)"
    total = 0
    batch = []
    hit_files = [
        input_dir / f"{category}.txt"
        for category in PII_CATEGORIES
        if (input_dir / f"{category}.txt").is_file()
    ]
    for pii_file in tqdm(hit_files, desc="Indexing PII hits", unit="file"):
        category = pii_file.stem
        with pii_file.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                source_file, source_line, source_sha256, pii_value = parse_hit(
                    line, pii_file, line_number
                )
                batch.append(
                    (source_file, source_line, source_sha256, category, pii_value)
                )
                total += 1
                if len(batch) >= 10_000:
                    connection.executemany(insert_sql, batch)
                    connection.commit()
                    batch.clear()
    if batch:
        connection.executemany(insert_sql, batch)
        connection.commit()
    connection.execute("CREATE INDEX hits_location ON hits(source_file, source_line)")
    connection.commit()
    return total


def resolve_source(source_root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValueError(f"Absolute source path is not allowed: {relative}")
    source = (source_root / relative_path).resolve()
    try:
        source.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(f"Source path escapes root: {relative}") from exc
    return source


def write_source_hits(
    *,
    connection: sqlite3.Connection,
    source_root: Path,
    source_file: str,
    writer: csv.writer,
    expected_snapshot: dict[str, object] | None = None,
) -> int:
    source = resolve_source(source_root, source_file)
    if not source.is_file():
        raise FileNotFoundError(f"Missing source file: {source}")
    if expected_snapshot is not None:
        verify_source_snapshot(source, expected_snapshot)

    cursor = connection.execute(
        "SELECT source_line, source_sha256, category, pii FROM hits "
        "WHERE source_file = ? ORDER BY source_line, rowid",
        (source_file,),
    )
    current = cursor.fetchone()
    written = 0
    with source.open("rb") as handle:
        for source_line, raw_line in enumerate(handle, 1):
            record_text = None
            while current is not None and current[0] == source_line:
                actual_sha256 = hashlib.sha256(
                    raw_line.rstrip(b"\r\n")
                ).hexdigest()
                if actual_sha256 != current[1]:
                    raise RuntimeError(
                        f"PII source mismatch at {source_file}:{source_line}: "
                        "source row hash differs from Stage 1"
                    )
                if record_text is None:
                    line = raw_line.decode("utf-8")
                    record_text = parse_record_text(line)
                candidate = current[3]
                if candidate not in record_text:
                    raise RuntimeError(
                        f"PII source mismatch at {source_file}:{source_line}: "
                        f"{candidate!r} is absent from the scanned text"
                    )
                writer.writerow(
                    [
                        source_file,
                        source_line,
                        current[1],
                        current[2],
                        candidate,
                        line.rstrip("\r\n"),
                    ]
                )
                written += 1
                current = cursor.fetchone()
            if current is None:
                break
    if current is not None:
        raise ValueError(
            f"Source line {current[0]} does not exist in {source_file}"
        )
    if expected_snapshot is not None:
        verify_source_snapshot(source, expected_snapshot)
    return written


def main() -> int:
    args = parse_args()
    delimiter = "\t" if args.delimiter == r"\t" else args.delimiter
    if len(delimiter) != 1:
        print("--delimiter must be exactly one character", file=sys.stderr)
        return 2

    input_dir = args.input_dir.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    snapshot_path = (
        args.snapshot_manifest.expanduser().resolve() if args.snapshot_manifest else None
    )
    output = args.output.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not source_root.is_dir():
        print(f"Source root does not exist: {source_root}", file=sys.stderr)
        return 2
    snapshots = (
        load_source_snapshot(snapshot_path, source_root) if snapshot_path else {}
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    database = output.with_name(f".{output.name}.sqlite3")
    output_temp = output.with_name(f".{output.name}.partial")
    database.unlink(missing_ok=True)
    output_temp.unlink(missing_ok=True)

    try:
        connection = sqlite3.connect(database)
        try:
            hit_count = index_hits(input_dir, connection)
            source_files = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT source_file FROM hits ORDER BY source_file"
                )
            ]
            written = 0
            with output_temp.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
                writer.writerow(
                    ["file", "line", "source_sha256", "category", "PII", "whole line"]
                )
                for source_file in tqdm(source_files, desc="Joining source rows", unit="file"):
                    expected = snapshots.get(source_file)
                    if snapshot_path and expected is None:
                        raise RuntimeError(f"Source is absent from snapshot: {source_file}")
                    written += write_source_hits(
                        connection=connection,
                        source_root=source_root,
                        source_file=source_file,
                        writer=writer,
                        expected_snapshot=expected,
                    )
            if written != hit_count:
                raise RuntimeError(f"Indexed {hit_count} hits but wrote {written}")
            output_temp.replace(output)
        finally:
            connection.close()
    except BaseException:
        output_temp.unlink(missing_ok=True)
        raise
    finally:
        database.unlink(missing_ok=True)

    print(f"Wrote {written:,} rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
