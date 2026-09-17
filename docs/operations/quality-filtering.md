# MedPsy PII and toxicity filtering

This pipeline scans the Baichuan-cleaned MedPsy JSONL tree, then writes an
unchanged-byte partition of accepted and rejected source rows.

## Decisions

- PII stages 1 and 2 both run. Stage 2 applies high-recall, category-specific
  format validation to every category; Stage 3 attaches source context only to
  candidates retained by those validators. Standalone ZIP codes and UK
  postcodes are excluded. Stages 1 and 3 verify the same source snapshot, and
  each manifest file and candidate row is SHA-256 bound across scanning,
  validation, and finalization. Stage 3 fails if a candidate is absent from its
  joined source text.
  Gemma 4 stage 4a runs as a per-shard GPU array and makes the final decision. A
  `keep` candidate means confirmed private-person PII and rejects its complete
  source row.
- Detoxify screens every message. Qwen3Guard reviews only Detoxify candidates.
  `Unsafe`, `Controversial`, unknown, and guard-error outcomes reject the
  complete source row. Candidates classified `Safe` do not.
- Confirmed PII rejects the complete source row. Unresolved PII validation
  errors are treated as safe; toxicity guard errors remain fail-closed.

## Requirements

- Submit from the login node; all work runs through `sbatch` on `health`.
- The existing vLLM SquashFS must be present at
  `containers/vllm-openai-v0.24.0-cu129.sqsh`.
- `google/gemma-4-31B-it` is gated. Export `HF_TOKEN` with accepted Gemma access,
  place it in `.env` at the repo root, or pre-populate `cache/hf`.
  The submission launcher sources that `.env` only when `HF_TOKEN` is unset.

## Pilot

Use a separate output root so the pilot success marker cannot be confused with
the full run:

```bash
cd /path/to/qvac-model-safety
export QUALITY_RUN_ROOT="$PWD/output/medpsy2/pii_toxicity_cleaning_pilot"
export QUALITY_PILOT_FILES=8
export QUALITY_SHARD_COUNT=8
export QUALITY_ARRAY_CONCURRENCY=4
export QUALITY_PII_ARRAY_CONCURRENCY=4
bash launchers/slurm/submit_medpsy_quality_filter.sh
```

## Full run

```bash
cd /path/to/qvac-model-safety
unset QUALITY_PILOT_FILES
export QUALITY_RUN_ROOT="$PWD/output/medpsy2/pii_toxicity_cleaning"
export QUALITY_SHARD_COUNT=16
export QUALITY_ARRAY_CONCURRENCY=8
export QUALITY_PII_ARRAY_CONCURRENCY=4
bash launchers/slurm/submit_medpsy_quality_filter.sh
```

Each PII CPU task requests 64 cores without exclusive-node allocation. At the
default PII array concurrency of four, the scan uses at most 256 allocated CPU
cores while leaving GPUs and unallocated node cores available to other jobs.
Override `PII_LLM_CONCURRENCY`, `TOX_BATCH_SIZE`, `GUARD_CONCURRENCY`, shard
count, or concurrency only after reading pilot throughput and memory logs.

## Outputs

Under `QUALITY_RUN_ROOT`:

- `filtered/`: accepted rows, mirroring input paths.
- `filtered_out/`: rejected rows, mirroring input paths.
- `review/rejected_rows/`: compact row-level reasons.
- `review/pii/` and `review/toxicity/`: compact model decisions that caused
  rejection.
- `scans/`: complete per-stage and per-shard scanner artifacts.
- `reports/summary.json`: aggregate counts and byte/row reconciliation.
- `reports/per_file.csv`: per-source-file counts.
- `_SUCCESS`: written only after every shard and aggregate check succeeds.

PII and toxicity branches are independent SLURM jobs and run concurrently.
Final filtering starts only after both branches complete successfully.

## Optional global deduplication

Deduplication is a separate global post-filter stage because independent shard
jobs would miss duplicates that occur in different shards. After this
pipeline's `_SUCCESS` marker exists, submit:

```bash
export DEDUP_INPUT_ROOT="$QUALITY_RUN_ROOT/filtered"
export DEDUP_RUN_DIR="$PWD/output/medpsy2/dedup"
sbatch launchers/slurm/run_dedup_scan.sbatch
```

The job writes its own `filtered/`, `filtered_out/`, decisions, summary, and
`_SUCCESS` marker. See [dedup.md](../data-screening/dedup.md) for policy and resource
controls.
