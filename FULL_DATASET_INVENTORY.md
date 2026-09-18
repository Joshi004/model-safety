# Every file under `/home/shared/agentic_slm/data/`, grouped, sized, and how to run each group

One place to see: how many files of each real kind exist, how big each group
actually is, roughly how many records are in it, and the exact command to
convert-and-scan a file from that group. Same rule as the other docs: every
number below was actually measured just now (`find`, `du`, real schema/row
reads), not estimated from memory. Where a number is a sample rather than an
exhaustive count, that's said explicitly.

This is a companion to the other three docs — it doesn't repeat their
reasoning, it's the numbers-and-commands index that sits on top of them:
[`HOW_TO_SCAN_YOUR_DATA.md`](HOW_TO_SCAN_YOUR_DATA.md),
[`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md),
[`DATA_FORMAT_INVENTORY.md`](DATA_FORMAT_INVENTORY.md) (the fine-grained,
9-family breakdown of the `.jsonl` files specifically).

## The whole thing, one table

| Group | Files | Size | Records | Works with existing readers today? |
|---|---|---|---|---|
| `.jsonl` — plain `text` | 36 | 82.6 GB | not split by family (see note) | **Yes** — scan directly |
| `.jsonl` — already `messages` | 8 | 23.7 GB | not split by family | **Yes** — scan directly |
| `.jsonl` — `responses_create_params` | 83 | 30.0 GB | not split by family | No — needs the nested-path adapter |
| `.jsonl` — everything else (9 more small families) | ~28 | ~7.3 GB | not split by family | Mixed — see `DATA_FORMAT_INVENTORY.md` |
| **`.jsonl` — all families combined** | **155** | **143.6 GB** | **16,689,866** | — |
| `.parquet` — verl RL-training row (nested `prompt`) | 674 | 60.8 GB | 23,699,693 | **Yes** — fixed, `prepare_rows.py` handles the nested column directly now |
| `.parquet` — already `messages` (nested column) | 144 | 21.0 GB | 2,005,103 | **Yes** — same fix |
| `.parquet` — raw source, flat `text` column | 118 | 39.8 GB | 11,225,711 | **Yes** — confirmed working |
| `.parquet` — manifest/audit, no text | 30 | 0.3 GB | 3,270,545 | N/A — nothing to scan |
| `.arrow` — already `messages` | 3,950 | 84.5 GB | ~40.7M (estimated, see note) | **Yes** — fixed, `prepare_rows.py` handles it directly now |
| `.arrow` — flat rendered template | 7,355 | 44.7 GB | ~31.3M (estimated) | **Yes** — same fix |
| `.arrow` — tokenized cache, no text | 16,717 | 310.1 GB | N/A | N/A — nothing to scan, ever |
| plain `.json` (bulk arrays) | 2,069 | 2.9 GB | not counted exactly | **Yes** — same converter as `.jsonl` |
| `.bin` + `.idx` (Megatron tokenized) | 88 | 78.1 GB | N/A | N/A — downstream of `.jsonl` already scanned |
| `.npy` (index/shuffle arrays) | 1,282 | 0.7 GB | N/A | N/A — pure integers, no text ever |
| `.txt` (cache descriptions) | 434 | ~0.01 GB | N/A | N/A — config text, not conversation data |
| `.pdf` | 27 | ~0.001 GB | N/A | No — needs text extraction first, not built |
| `.csv` | 2 | ~0 GB | N/A | N/A — benchmark result numbers |
| `.lock` / `.metadata` | 437 | ~0 GB | N/A | N/A — empty bookkeeping |

**Record-count update:** the `.jsonl` line count finished —
**16,689,866 total lines across all 155 files** (`find ... | xargs -P 16 wc -l`).
It took **23 minutes** to read all 143 GB on this filesystem, which is exactly
why the per-family split below still says "not counted exactly" rather than a
made-up breakdown — getting that split means re-reading the same 143 GB grouped
by family, another 20+ minute job I haven't re-run. The `.parquet` numbers above **are**
exact (parquet stores row counts in its own footer, so getting them doesn't
require reading the actual data — cheap for all 966 files). The `.arrow`
numbers are averages measured on one real file per unique directory,
multiplied by how many files are in that directory — a real measurement,
just not an exhaustive one across all ~28,000 files.

## `.jsonl` — 155 files, 143.64 GB total

Full 9-family breakdown with real examples is in
[`DATA_FORMAT_INVENTORY.md`](DATA_FORMAT_INVENTORY.md). Quick recap of the
two biggest:

```bash
# plain "text" -- 36 files, 82.6 GB, already the exact target shape
sbatch --export=ALL,PII_BASE_DIR=/home/shared/agentic_slm/data/mid/_megatron/full/jsonl,PII_RUN_DIR=output/scan1/pii_run \
  launchers/slurm/run_pii_scan.sbatch

# responses_create_params -- 83 files, 30.0 GB, needs the small bypass script
# from HOW_DATA_CONVERSION_WORKS.md section 5 (pull .responses_create_params.input
# out and rename to "messages") before it's scannable at all.
```

## `.parquet` — 966 files, 121.8 GB, 40,201,745 rows (all real, all counted)

**verl RL-training row format — 674 files, 60.8 GB, 23,699,693 rows.** This is
the biggest parquet group by far. `prompt` is a nested
`list<struct<role,content>>` column — used to crash the converter (see
`HOW_DATA_CONVERSION_WORKS.md`), now fixed. No bypass needed, just the
normal converter with `--map`:

```bash
cd /home/naresh/model-safety
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path YOUR_FILE.parquet \
  --input-type parquet --map messages=prompt \
  --output-dir output/my_conversion/converted
```

Real result, confirmed on a real slice of
`rl/processed/qwen35-xml/sft-stage1_0714/train.parquet` (the same 1.9 GB,
603,683-row file used to confirm the crash originally):
`Prepared 200 rows; skipped 0 rows.` — confirmed scanning correctly
afterward too.

**Already `messages`-shaped — 144 files, 21.0 GB, 2,005,103 rows** (smoltalk2,
toucan, a pre-converted `medmcqa_think`). Same nested-column problem, same
fix — the column's already correctly named, so use `--require messages`
instead of `--map`:

```bash
cd /home/naresh/model-safety
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/sft/curriculum/mixes/general_warmup_sft_0903/clean_parquet/medmcqa_think.parquet \
  --input-type parquet --require messages \
  --output-dir output/my_conversion/converted
```

Real result: `Prepared 29986 rows; skipped 0 rows.` — fed straight into
`run_pii_scan.sbatch` afterward, which scanned all 29,986 lines end to
end (1,370 raw hits, 6 validated after stage 2), confirming this isn't
just "didn't crash" but genuinely scans.

**Raw source, flat `text` column — 118 files, 39.8 GB, 11,225,711 rows**
(`mid/MidTool-Mix/{code,web,pdf,native-agent-traj}/*.parquet` — the literal
upstream of the `.jsonl` categories tokenized in `mid/_megatron/`). Flat
string column, no nesting — **this one works today, confirmed**:

```bash
cd /home/naresh/model-safety
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/mid/MidTool-Mix/code/part-00000.parquet \
  --input-type parquet --require text \
  --output-dir output/my_conversion/converted
```

**Manifest/audit files — 30 files, 0.3 GB, 3,270,545 rows.** Dedup/quality-gate
bookkeeping (`dedup_key`, `passed_gate`, `fail_codes`, ...) — no text column
at all. Nothing to convert or scan here.

## `.arrow` — ~28,022 files, 439.2 GB (file counts and sizes exact; row counts sampled)

**Update — `prepare_rows.py` now handles both text-bearing families below
directly.** It used to need a manual bypass script (shown further down in
this doc's edit history / still in `HOW_TO_SCAN_YOUR_DATA.md` section 5 for
reference) because it only called `load_dataset`, which refuses any folder
saved with `save_to_disk`. It now checks for the same `state.json` marker
the `datasets` library itself checks for, and calls `load_from_disk`
automatically — no new flag, same `--input-type huggingface`.

**Already `messages`-shaped — 3,950 files, 84.5 GB.** Sampled one real file
per directory (558 samples): 5,744,648 rows across those alone, ~10,295
rows/file average. Tested for real through the fixed converter, no bypass:

```bash
cd /home/naresh/model-safety
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/sft/curriculum/distill/envgen_solo_v1 \
  --input-type huggingface --require messages \
  --output-dir output/my_conversion/converted
```

Real result: `Prepared 999 rows; skipped 0 rows.` — confirmed scanning
correctly afterward.

**Flat rendered chat-template string — 7,355 files, 44.7 GB.** Sampled 33
real files (one per directory): 140,278 rows across those, ~4,251 rows/file
average. `prompt` here is one long pre-rendered string
(`<|im_start|>system\n...`), not a list — same fix, map `text` instead of
requiring `messages`:

```bash
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/sft/_sdft_cache/281f1a0ca6b5300c626f/eval \
  --input-type huggingface --map text=prompt --require text \
  --output-dir output/my_conversion/converted
```

Real result: `Prepared 243 rows; skipped 0 rows.`, confirmed scanning
correctly afterward too. If you need the underlying `pyarrow`/`load_from_disk`
mechanics explained, or the old manual-bypass version for a standalone
`.arrow` shard that has no `dataset_info.json` next to it (that specific
case still needs the manual `pyarrow.ipc` approach), see
`HOW_TO_SCAN_YOUR_DATA.md` section 5.

**Tokenized cache, no text — 16,717 files, 310.1 GB.** `input_ids` /
`assistant_masks` / `labels` / plain filter `indices` — already-tokenized
numbers or bookkeeping arrays. Confirmed nothing to scan here, ever; not
worth writing a reader for.

## Everything smaller

- **Plain `.json` bulk arrays** — 2,069 files, 2.9 GB. Same converter as
  `.jsonl`, just `--input-type json` instead of `jsonl`. Real examples up to
  989 MB exist (`envfactory_sft_filtered/mcp_factory_sft_nips.json`).
- **`.bin` + `.idx`** — 88 files, 78.1 GB. Megatron-tokenized text, proven
  downstream of `.jsonl` this repo already scans (see
  `HOW_TO_SCAN_YOUR_DATA.md` section 2) — don't build a separate scanner,
  just make sure the `.jsonl` stage runs first.
- **`.npy`** — 1,282 files, 0.7 GB. Pure index/shuffle integers. No text,
  ever.
- **`.txt`** — 434 files, ~11 MB total. Megatron `BlendedDataset` cache
  descriptions, not conversation data.
- **`.pdf`** — 27 files, ~1 MB total. A few real workflow documents exist
  (`tau2-bench-data/domains/telecom/workflows/*.pdf`); nothing in this repo
  reads PDFs, would need `pdftotext` first.
- **`.csv`** — 2 files. Benchmark result numbers, not conversation data.
- **`.lock` / `.metadata`** — 437 files, effectively 0 bytes. Hugging Face
  download bookkeeping.

## After converting anything above: scan it the same way every time

```bash
sbatch --export=ALL,PII_BASE_DIR=output/my_conversion/converted,PII_RUN_DIR=output/my_conversion/pii_run,PII_CHUNK_SIZE=200 \
  launchers/slurm/run_pii_scan.sbatch
```

Read results at `output/my_conversion/pii_run/stage2_validated/metrics.txt`
and `.../stage2_validated/hits/<category>.txt`. Set `PII_CHUNK_SIZE` low
(shown above) when converting a small number of very large rows (long
agentic traces), otherwise everything lands in one chunk and only one
worker ever does anything — the exact thing that made the live test in this
conversation take several minutes on fewer than 1,000 rows.
