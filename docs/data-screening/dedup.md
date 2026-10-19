# JSONL near-deduplication

This tool detects exact and near-duplicate JSONL rows with character n-gram
MinHash and locality-sensitive hashing (LSH). It accepts the same `text` or
`messages` records as the PII and toxicity scans, recursively discovers JSONL
files, and preserves source paths and line numbers in every decision.

The implementation adapts the MinHash/LSH approach documented by the
Apache-2.0-licensed Cerebras Model Zoo deduplication pipeline. Unlike that
source, this version has no hardcoded user paths, does not
require flat symlink inputs, wires connected components into final decisions,
and can write the deduplicated dataset.

## Deduplication policy

- Input order is deterministic: sorted relative file path, then one-based line.
  With `--input-manifest` the files are read in the order the manifest lists them
  (nothing sorts them again), then one-based line within each file.
- The earliest row in each connected duplicate component is retained, so the
  order the files are read in decides which copy counts as the original. The
  validator service uses this: it writes the manifest dataset by dataset in the
  order the user chose (first by path, a priority order, or protected datasets
  first).
- `--text-scope prompt` is the default. For chat records it excludes a final
  assistant message, so equal prompts with different generated answers are
  duplicates. Plain `text` records use the complete value.
- `--text-scope all` hashes every chat message instead.
- `--exclude-system` (env `DEDUP_EXCLUDE_SYSTEM=1`) drops every `system`-role
  message before `--text-scope` is applied. Off by default. System messages
  usually carry tool/function definitions rather than conversation content;
  comparing them can make unrelated rows that happen to share a similar tool
  schema look more similar than they actually are.
- Text is lowercased, punctuation is removed, and whitespace is collapsed
  before character n-grams are generated.
- The default n-gram width is 60 characters, matching the intended source
  configuration and roughly approximating 8–12 English words. It is not an
  exact word-sequence rule.
- The source-compatible LSH shape is 256 permutations split into 32 bands of 8
  rows. A match is an LSH band collision; it is approximate, not a guaranteed
  exact Jaccard cutoff. The 50% collision-probability midpoint is recorded in
  `summary.json` (`collision_probability_midpoint` in `lsh.py`, which does not need
  datasketch, so the validator service uses the same formula). The service offers
  three shapes, all with 256 permutations and 60-character n-grams: Strict (16
  bands of 16 rows, midpoint about 82%), Standard (32 of 8, about 62%) and Loose
  (64 of 4, about 32%).

The index is global and in memory. Do not split one dataset into independent
SLURM arrays: doing so would miss duplicates across shards. Use a pilot to
measure memory before increasing the full-run request.

## Run through SLURM

The job uses the `background` partition by default (low priority, and a job there can be preempted and restarted) and creates a dedicated `.venv` in this
directory on first use.

```bash
cd /path/to/qvac-model-safety
export DEDUP_INPUT_ROOT="$PWD/tmp/qa_scan_test/input"
export DEDUP_RUN_DIR="$PWD/tmp/qa_scan_test/dedup_out"
sbatch launchers/slurm/run_dedup_scan.sbatch
```

For a full post-quality-filter run:

```bash
export DEDUP_INPUT_ROOT="$PWD/output/medpsy2/pii_toxicity_cleaning/filtered"
export DEDUP_RUN_DIR="$PWD/output/medpsy2/dedup"
sbatch launchers/slurm/run_dedup_scan.sbatch
```

An existing non-empty run directory is rejected. Set `DEDUP_OVERWRITE=1` only
when intentionally replacing that run.

## Tool-team wrapper

`scripts/tool_team_dedup_scan.sh` wraps the global-mode launcher above for
the tool team's converted data (`output/tool_team_converted`), the same
input the PII and toxicity wrappers already use. It's interactive when run
with no arguments, and fully scriptable otherwise:

```bash
./scripts/tool_team_dedup_scan.sh pilot                       # ~50 files, first
./scripts/tool_team_dedup_scan.sh full --partition toolCall   # every file, one job
./scripts/tool_team_dedup_scan.sh report --run-dir output/tool_team_dedup_pilot
```

Two defaults differ from the launcher above, on purpose:

- **`--exclude-system` defaults to on** in this wrapper (the launcher and
  `run.py` still default it off). This data is tool-calling traces with a
  system prompt full of tool/function definitions; see the "Worth knowing"
  notes in this repo's data-readiness docs for why that text shouldn't count
  toward similarity.
- **Only global mode is exposed.** Cross/decontamination mode isn't offered
  by the wrapper — call `run.py` directly with `--reference-input` /
  `--candidate-input` for that, as described below.

`report` reads an existing run's `summary.json` and `dedup_results.jsonl`
locally (no SLURM job) and adds a breakdown of duplicate rows by sub-dataset
(e.g. `sft_mix/toolbench_v1`) that the launcher's own output doesn't provide.
Run `./scripts/tool_team_dedup_scan.sh --help` for the full flag list.

## Cross-deduplicate one dataset against another

Cross mode treats one dataset as a read-only reference and filters only the
candidate dataset. A candidate row is removed when it collides with any
reference row. Reference rows are never removed or rewritten, even when the
reference contains internal duplicates.

Candidate rows are not compared with other candidate rows in this mode. Run a
normal global dedup pass afterward if the candidate output also needs internal
deduplication.

`DEDUP_INPUT_MODE` applies to the candidate folder only. By default it reads
the files of finished `prepare_rows.py` runs; if the candidate folder is a
plain folder of `.jsonl` files, set `DEDUP_INPUT_MODE=folder`. The reference
folder is always read as a plain folder.

```bash
cd /path/to/qvac-model-safety
export DEDUP_REFERENCE_ROOT="/path/to/dataset_always_retained"
export DEDUP_CANDIDATE_ROOT="/path/to/dataset_to_filter"
export DEDUP_RUN_DIR="$PWD/output/cross_dedup"
sbatch launchers/slurm/run_dedup_scan.sbatch
```

The output `filtered/` and `filtered_out/` trees contain candidate rows only.
Each decision's `duplicate_of.dataset` is `reference`, so equal relative paths
in the two datasets remain unambiguous.

### Test-set decontamination

Cross mode is also the test-set decontamination workflow: use evaluation or
benchmark data as the retained reference and training data as the candidate.

```bash
export DEDUP_REFERENCE_ROOT="/path/to/test_and_benchmark_sets"
export DEDUP_CANDIDATE_ROOT="/path/to/training_data"
export DEDUP_RUN_DIR="$PWD/output/train_test_decontamination"
export DEDUP_TEXT_SCOPE="prompt"
sbatch launchers/slurm/run_dedup_scan.sbatch
```

This is approximate lexical decontamination over 60-character shingles by
default. It catches exact and sufficiently similar records but does not
guarantee detection of every shared 10-word phrase or semantic paraphrase.

Mixed JSONL, CSV, and Parquet benchmark collections must first be normalized
to prompt-only JSONL:

```bash
srun --partition=background --ntasks=1 --cpus-per-task=4 --mem=16G --time=00:30:00 \
  modelsafety/data_screening/dedup/.venv/bin/python \
  modelsafety/data_screening/dedup/prepare_benchmarks.py \
  --input-root data/benchmarks/alex_health \
  --output-root data/benchmarks/alex_health_prepared
```

(`modelsafety/data_screening/dedup/.venv` is the dedup module's own venv,
created on first run of `launchers/slurm/run_dedup_scan.sbatch` -- there is no
shared root venv in this repo.)

The prepared rows retain their original benchmark file, line, format, dataset,
and record identifier under `metadata`. Empty source rows are skipped and
counted in `manifest.json`.

## Environment controls

| Variable | Default | Meaning |
|---|---:|---|
| `DEDUP_INPUT_ROOT` | `tmp/qa_scan_test/input` | Folder to scan |
| `DEDUP_INPUT_MODE` | `runs` | `runs` reads only the files of finished `prepare_rows.py` runs under the folder and lists what it skipped in `<run dir>/scan_inputs.json`; `folder` reads every `.jsonl` under it (use it for a plain folder that isn't a set of run folders) |
| `DEDUP_INPUT_MANIFEST` | empty | Optional root-relative file manifest |
| `DEDUP_REFERENCE_ROOT` | empty | Cross mode reference root |
| `DEDUP_CANDIDATE_ROOT` | empty | Cross mode candidate root |
| `DEDUP_RUN_DIR` | `tmp/qa_scan_test/dedup_out` | Output root |
| `DEDUP_TEXT_SCOPE` | `prompt` | `prompt` or `all` |
| `DEDUP_EXCLUDE_SYSTEM` | `0` | Set to `1` to drop `system`-role messages before comparing |
| `DEDUP_NGRAM_SIZE` | `60` | Character n-gram width (roughly 8–12 words) |
| `DEDUP_NUM_PERM` | `256` | MinHash permutations |
| `DEDUP_BANDS` | `32` | LSH bands |
| `DEDUP_ROWS_PER_BAND` | `8` | Rows per band; bands × rows must equal permutations |
| `DEDUP_WORKERS` | allocated CPUs | Parallel MinHash workers |
| `DEDUP_REPORT_TEXT_CHARS` | `2000` | Maximum text per matched-sample CSV cell; `0` keeps full text |
| `DEDUP_WRITE_FILTERED` | `1` | Write `filtered/` and `filtered_out/` |
| `DEDUP_OVERWRITE` | `0` | Replace an existing non-empty run |

For quick pilots, `DEDUP_NUM_PERM=64`, `DEDUP_BANDS=8`, and
`DEDUP_ROWS_PER_BAND=8` reduce work while retaining the same rows-per-band
shape. Production runs should use the defaults unless pilot evaluation supports
another policy.

## Outputs

Under `DEDUP_RUN_DIR`:

- `duplicate_pairs.jsonl`: every LSH candidate edge.
- `dedup_results.jsonl`: one decision per removed row, with `source_file`,
  `source_line`, and `duplicate_of`.
- `matched_samples.csv`: one row per removal with candidate/reference dataset,
  root, file, line, record ID, and compared text for manual review. In cross
  mode this contains every direct training/benchmark match.
- `contamination_by_benchmark.csv`: benchmark rows, unique contaminated
  benchmark rows, contamination percentage, and matched training rows per
  benchmark file.
- `contamination_by_training_dataset.csv`: training-dataset to benchmark-dataset
  relationships with unique rows and pair counts.
- `contamination_by_training_file.csv`: the same relationship at exact source
  file granularity.
- `filtered/`: retained source rows, mirroring relative input paths.
- `filtered_out/`: duplicate rows, mirroring relative input paths.
- `summary.json`: counts and the complete matching configuration.
- `_SUCCESS`: written only after decisions, filtering, and summary complete.

The partition files copy original bytes; source records are not reserialized.
Malformed JSON fails the job instead of silently skipping data. Rows without
usable `text` or `messages` content are retained and counted as unhashable.

## Cluster resources

Deduplication is CPU- and RAM-only; requesting GPUs does not accelerate it.
MinHash construction uses `DEDUP_WORKERS` processes, while LSH insertion and
queries are coordinated in one process. More than 8–16 workers can therefore
have diminishing returns.

Run one single-node job rather than independent arrays because arrays would
miss duplicates across shards. Global mode indexes all rows in memory; cross
mode indexes only the reference benchmark rows and streams the candidate
training data. Start with a representative pilot:

```bash
sbatch \
  --cpus-per-task=4 \
  --mem=16G \
  --time=01:00:00 \
  --export=ALL,DEDUP_INPUT_ROOT=/path/to/pilot,DEDUP_RUN_DIR=/path/to/pilot_output \
  launchers/slurm/run_dedup_scan.sbatch
```

For a full run, the launcher defaults to 16 CPUs and 128 GB RAM. Adjust memory
from pilot peak usage; corpus row count and LSH band count dominate memory.

## Direct CLI

Use direct execution only inside an allocated compute session, run from
`modelsafety/data_screening/dedup/` (all paths below are relative to it):

```bash
.venv/bin/python run.py \
  --input /path/to/jsonl/tree \
  --output-dir /path/to/dedup_run \
  --workers 16
```

With a quality-filter manifest:

```bash
.venv/bin/python run.py \
  --input-root /path/to/jsonl/tree \
  --input-manifest /path/to/shard.txt \
  --output-dir /path/to/dedup_run
```

Direct cross mode:

```bash
.venv/bin/python run.py \
  --reference-input /path/to/reference \
  --candidate-input /path/to/candidate \
  --output-dir /path/to/cross_dedup_run
```

## Selective post-processing without rerunning MinHash

`postprocess_matches.py` can build another byte-preserving filtered tree from an
existing `matched_samples.csv`. Repeated `--rule` arguments select only the
source/benchmark relationships that should remove training rows:

```bash
.venv/bin/python postprocess_matches.py \
  --input-root /path/to/original/training/filtered \
  --matched-samples /path/to/full_cross_run/matched_samples.csv \
  --output-dir /path/to/selective_cross_run \
  --rule 'training/source.jsonl::closed_ended/mmlu/**' \
  --rule 'training/source.jsonl::safety/medec_p1/**'
```

The default `--copy-mode hardlink` rewrites only source files containing
selected rows and hard-links unchanged JSONL files, so it avoids rehashing and
duplicating the full corpus. Treat the source and output trees as immutable
while hard links are in use. Use `--copy-mode copy` when a physically
independent tree is required.

Outputs include `filtered/`, `filtered_out/`, `selected_matches.csv`,
`selected_rows.jsonl`, `summary.json`, and `_SUCCESS`.
