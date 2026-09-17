#!/usr/bin/env python3
"""
PII Validator - Filter false positives from PII scanner results
Uses smart heuristics and validation rules per PII type
"""

import json
import os
import shutil
import time
from multiprocessing import Pool
from pathlib import Path

from tqdm import tqdm

from pii_output import (
    print_run_summary,
    resolve_run_dir,
    resolve_stage1_hits_dir,
    resolve_stage2_hits_dir,
    stage2_paths,
    write_stage2_metrics,
)

try:
    from validator.bitcoin import validate_bitcoin
    from validator.credit_card import validate_credit_card
    from validator.email import validate_email
    from validator.ip import validate_ip
    from validator.ipv6 import validate_ipv6
    from validator.phone import validate_phone
    from validator.po_box import validate_po_box
    from validator.street_address import validate_street_address
except ModuleNotFoundError:
    from regex_pii.validator.bitcoin import validate_bitcoin
    from regex_pii.validator.credit_card import validate_credit_card
    from regex_pii.validator.email import validate_email
    from regex_pii.validator.ip import validate_ip
    from validator.ipv6 import validate_ipv6
    from regex_pii.validator.phone import validate_phone
    from validator.po_box import validate_po_box
    from regex_pii.validator.street_address import validate_street_address

NUM_WORKERS = int(os.environ.get("PII_NUM_WORKERS", "100"))

VALIDATORS = {
    "emails.txt": validate_email,
    "phones.txt": validate_phone,
    "phones_with_exts.txt": validate_phone,
    "ukphones.txt": validate_phone,
    "street_addresses.txt": validate_street_address,
    "po_boxes.txt": validate_po_box,
    "credit_cards.txt": validate_credit_card,
    "ips.txt": validate_ip,
    "ipv6s.txt": validate_ipv6,
    "btc_addresses.txt": validate_bitcoin,
}


def process_chunk(args):
    lines, validator_func = args
    valid_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            raise ValueError(f"Malformed stage-1 hit row: {line[:200]}")
        source_sha256 = parts[2]
        if len(source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise ValueError(f"Invalid source SHA-256 in stage-1 hit row: {line[:200]}")
        encoded_value = parts[3].strip()
        try:
            pii_value = json.loads(encoded_value)
        except json.JSONDecodeError:
            pii_value = encoded_value
        if not isinstance(pii_value, str):
            pii_value = str(pii_value)
        if validator_func(pii_value):
            valid_lines.append(line + "\n")

    return valid_lines


def validate_pii_file(input_file, output_file, validator_func, chunk_size=10000):
    print(f"\nValidating {os.path.basename(input_file)} ...")

    with open(input_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total_lines = len(lines)
    print(f"  Raw hits: {total_lines:,}")

    if total_lines == 0:
        Path(output_file).write_text("", encoding="utf-8")
        return 0, 0

    chunks = [(lines[i : i + chunk_size], validator_func) for i in range(0, len(lines), chunk_size)]
    valid_results = []
    with Pool(processes=NUM_WORKERS) as pool:
        for result in tqdm(
            pool.imap(process_chunk, chunks),
            total=len(chunks),
            desc="  Filtering",
            unit="chunk",
        ):
            valid_results.extend(result)

    with open(output_file, "w", encoding="utf-8") as f:
        f.writelines(valid_results)

    valid_count = len(valid_results)
    print(f"  Kept: {valid_count:,}  |  Filtered: {total_lines - valid_count:,}")
    return total_lines, valid_count


def main():
    run_dir = resolve_run_dir()
    input_hits_dir = resolve_stage1_hits_dir()
    output_hits_dir = resolve_stage2_hits_dir()
    paths = stage2_paths(run_dir)
    if not input_hits_dir.is_dir():
        raise FileNotFoundError(f"Missing Stage-1 hits directory: {input_hits_dir}")
    if paths["hits_dir"].exists():
        shutil.rmtree(paths["hits_dir"])
    paths["hits_dir"].mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("PII Validator — stage 2 (heuristic filter)")
    print("=" * 60)
    print(f"Run root:       {run_dir}")
    print(f"Input hits:     {input_hits_dir}")
    print(f"Validated hits: {output_hits_dir}")
    print(f"Workers:        {NUM_WORKERS}")
    print()

    results = {}
    start_time = time.time()

    for filename, validator_func in VALIDATORS.items():
        input_file = input_hits_dir / filename
        output_file = output_hits_dir / filename

        if not input_file.exists():
            print(f"\nSkipping {filename} (not found in stage 1 hits)")
            output_file.write_text("", encoding="utf-8")
            results[filename] = (0, 0)
            continue

        total, valid = validate_pii_file(str(input_file), str(output_file), validator_func)
        results[filename] = (total, valid)

    processing_time = time.time() - start_time
    write_stage2_metrics(
        paths["metrics_file"],
        run_dir=run_dir,
        input_hits_dir=input_hits_dir,
        output_hits_dir=output_hits_dir,
        results=results,
        processing_time=processing_time,
        num_workers=NUM_WORKERS,
    )

    grand_total = sum(total for total, _ in results.values())
    grand_valid = sum(valid for _, valid in results.values())
    print("\n" + "=" * 60)
    print("Stage 2 summary")
    print("=" * 60)
    print(f"Raw hits:        {grand_total:,}")
    print(f"Validated hits:  {grand_valid:,}")
    print(f"Filtered out:    {grand_total - grand_valid:,}")
    if grand_total:
        print(f"Keep rate:       {100 * grand_valid / grand_total:.1f}%")
    print(f"\nMetrics written: {paths['metrics_file']}")
    print_run_summary(run_dir)
    print("\nStage 2 complete.")


if __name__ == "__main__":
    main()
