# Tool team data readiness: SFT mix, MOPD mix, RL mix

**Plain-language answer up front: this is good news.** All three datasets
you named convert to this repo's scannable format using commands that
**already exist and already work today** — no code changes needed for any
of them. I opened the real files, ran the real converter (`prepare_rows.py`)
and the real PII scanner's own text-extraction function against real rows
from each dataset (not guessed), and every number below came from that.

The three datasets:

| Name | Path | Size | Rows |
|---|---|---|---|
| **SFT mix** | `.../sft/processed/qwen3_5/s1_2_0824` | 47 GB | ~595K available, ~303K used in the actual mix |
| **MOPD mix** | `.../rl/processed/qwen35-xml/decontaminated/mopd_mixv4_0907` | 643 MB | 311,075 (310,537 train + 538 test) |
| **RL mix** | `.../rl/processed/qwen35-xml/decontaminated/rl_mix_v12_ifmt_0903` | 702 MB | 237,358 (236,641 train + 717 test) |

## The 30-second table

| Dataset | File types | Shape | Works today? | Command |
|---|---|---|---|---|
| SFT mix | `.arrow` (26,233), `.json` (126), `.jsonl` (1) | Already `messages` (`role`/`content` strings) | **Yes — 100%, zero changes** | `--input-type huggingface --require messages` |
| MOPD mix | `.parquet` (2), `.json` (2), `.jsonl` (1) | `prompt` column, `list<struct<role,content>>` | **Yes — 100%, zero changes** | `--input-type parquet --map messages=prompt` |
| RL mix | `.parquet` (2), `.json` (2), `.jsonl` (1) | Same as MOPD | **Yes — 100%, zero changes** | `--input-type parquet --map messages=prompt` |

No PDFs, no raw tokenized `.bin`/`.npy`, no CSVs in any of the three — those
are the formats this repo genuinely can't read yet, and none of them show up
here. That's a meaningfully easier situation than the general
`/home/shared/agentic_slm/data` audits in the other docs in this repo.

---

## Dataset 1 — SFT mix (`s1_2_0824`)

### What it actually is

This folder is **not raw training data** — it's a *recipe*. It has 3 small
JSON files describing which rows to pull and in what proportion
(`mix_recipe.json`, `train_shards.json`, `example_ids.json`), plus a
`train_shards_keep/` folder holding the real data: **61 separate sub-datasets**,
each one a Hugging Face `save_to_disk` cache folder (`dataset_info.json` +
`state.json` + a set of numbered `.arrow` shard files).

| File type | Count | What it is |
|---|---|---|
| `.arrow` | 26,233 | The actual row data, chunked into shards per sub-dataset |
| `.json` | 126 | 3 recipe files + `dataset_info.json`/`state.json` per sub-dataset (61 × 2) + 1 `summary.json` |
| `.jsonl` | 1 | `removed_rows.jsonl` — a decontamination log, not training data |

The recipe (`mix_recipe.json`) blends 8 named "buckets" (`multi_step`,
`multi_turn`, `parallel`, `single_call`, `abstain_robust`, `chat_nontool`,
`multilingual`, `env_verified_multi_turn`) by sampling ~303K rows total out
of the ~595K rows sitting in `train_shards_keep/`. For scanning purposes
this distinction barely matters — see the note at the end of this section.

### The shape — verified, not assumed

I read every one of the 61 sub-datasets' `dataset_info.json` files. **All
61, with no exceptions**, declare the exact same schema:

```
messages: List({role: string, content: string})
id: string
chat_template_kwargs: {enable_thinking: bool}
```

That `messages` field, with plain string `role`/`content`, is *exactly* the
shape `modelsafety/contract/textract.py`'s `extract_record_text()` — the
function every scanner in this repo calls — is built to read. Nothing to
rename, nothing to reshape.

**One structural guarantee worth knowing:** because this is Arrow with a
declared schema (not freeform JSONL), that schema applies to *every* row in
every file — a row can't secretly have a different shape the way a raw
`.jsonl` file's rows could. There's also no separate `tool_calls` column
anywhere in that schema, which means the "PII hidden inside a `tool_calls`
block, invisible to the scanner" problem described elsewhere in this repo's
docs **cannot happen in this dataset** — every tool call and every model
response is necessarily inline text inside `content`. I confirmed this
directly by sampling real rows from the most tool-heavy sub-datasets
(`toolbench_v1`, `toolmind__xlam`, `sealtools_train`, `when2call_v2`,
`nemotron_interactive`, `smolagents_traces`): every single message has
`extra_keys=[]` — nothing beyond `role`/`content` — and the tool calls
themselves show up as plain text (e.g. `<tool_call>\n<function=...`,
or JSON embedded directly in the string).

**Real coverage numbers**, using `extract_record_text()` itself (the same
function the PII scanner uses) on 300 real rows sampled from each of the 61
sub-datasets (18,111 rows total, or all of it for the two sub-datasets under
300 rows):

```
18,111 / 18,111 rows have scannable text  ->  100.00%
```

Every one of the 61 sub-datasets individually scored 100%. Full per-dataset
breakdown is reproducible with the command in the appendix.

**Real end-to-end proof**, not just the function in isolation — I ran the
actual converter script against one real sub-dataset:

```
$ prepare_rows.py --input-path .../train_shards_keep/toolmind__when2call \
    --input-type huggingface --require messages --output-dir ./out
Prepared 111 rows; skipped 0 rows.
Text coverage: 111/111 rows (100.0%) have scannable text.
```

No `--map` even needed — the field is already called `messages`.

### How to run this today

```bash
cd /home/naresh/model-safety
for d in /home/shared/agentic_slm/data/sft/processed/qwen3_5/s1_2_0824/train_shards_keep/*/; do
  name=$(basename "$d")
  PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
    --input-path "$d" --input-type huggingface --require messages \
    --output-dir "output/sft_mix_converted/$name"
done

# Then point the PII / dedup scanner at the whole converted folder:
sbatch --export=ALL,PII_BASE_DIR=output/sft_mix_converted,PII_RUN_DIR=output/sft_mix_pii_run \
  launchers/slurm/run_pii_scan.sbatch
```

That's a loop over 61 already-working commands — not 61 different
problems to solve.

### Worth knowing (minor, not blocking)

- **The recipe selects a subset.** The actual trained mix uses ~303K of the
  ~595K rows available (roughly half). Scanning everything in
  `train_shards_keep/` is a safe superset — you'll never miss a row that's
  actually used — it just also scans some rows that aren't. If you want the
  *exact* trained mix only, `example_ids.json` has the selected IDs per
  bucket, but that's extra filtering work this repo doesn't automate today.
- **`removed_rows.jsonl` here carries no text** — just bookkeeping
  (`source`, `row`, `id`, `bench`, `rule`) — so there's nothing lost by not
  scanning it.
- **Traceability is good here specifically**: every row already has its own
  `id` (e.g. `toolbench_000000`), which survives into `metadata` after
  conversion. This is one of the gaps flagged as unsolved in
  `HOW_DATA_CONVERSION_WORKS.md` for data lake in general — it's already a
  non-issue for this dataset because the source data happens to have IDs.

---

## Dataset 2 — MOPD mix (`mopd_mixv4_0907`)

## Dataset 3 — RL mix (`rl_mix_v12_ifmt_0903`)

These two are covered together because they're built by the same pipeline
and share an identical shape — anything said about one applies to the
other. Differences are called out explicitly.

### What it actually is

Both are the **output of an RL-training-data build step**, already
decontaminated against benchmarks (`build_summary.json` records exactly
which benchmark rows were matched and removed — `mmlu_pro`, `ifeval`,
`bfcl`, etc.). Each folder has exactly:

| File type | Count | What it is |
|---|---|---|
| `.parquet` | 2 | `train.parquet` + `test.parquet` — the actual data |
| `.jsonl` | 1 | `removed_rows.jsonl` — decontamination log **with real text in it** (see below) |
| `.json` | 2 | `summary.json` (decontamination stats) + `build_summary.json` (build recipe) |

### The shape — verified, not assumed

Both `train.parquet` files use the same schema, one already documented
elsewhere in this repo as **fixed and working** — the "verl RL-training row
format":

```
prompt:       list<struct<role: string, content: string>>
reward_model: struct<ground_truth: string, style: string>
data_source:  string
ability:      string
extra_info:   struct<...>
tools, conversations, answer, raw_system, teacher_group: mostly null
```

`prompt` is the field that matters — it's the model's input context
(system + tool definitions + user turns), and it's already `role`/`content`
named, just like the SFT mix. This is the exact nested-list-column shape
that used to crash the converter (a `numpy.ndarray` `json.dumps` couldn't
serialize) — that crash is already fixed in `prepare_rows.py`, and I
re-confirmed it here on your actual files, not just the file used to prove
the original fix.

**This shape already has a name — no new format needed for it.** This repo
keeps a small reference catalog of every raw shape found across the data
lake at `format_samples/`. `format_samples/10_verl_rl_training_row_format.json`
already documents this *exact* 5-field row spec by name — **"verl
RL-training row format (volcengine/verl)"**, an actual open-source RL
training framework's documented row shape (`data_source`/`prompt`/`ability`/
`reward_model`/`extra_info`). MOPD and RL mix match it exactly, plus a few
extra bonus columns (`tools`, `conversations`, `answer`, `raw_system`,
`teacher_group`). So `--map messages=prompt` isn't a workaround for an
unrecognized format — it's the correct way to read an already-catalogued
one. (`conversations`, covered below, does *not* cleanly match any of the
other 11 cataloged samples — the two closest are
`05_sharegpt_parallel_arrays_format.json`, which uses the same `from`/`value`
naming but as two separate array columns instead of a proper list, and
`03_openai_conversations_format.json`, which is a proper list like this data
but uses `role`/`content` instead of `from`/`value`. It's a small,
uncatalogued hybrid of the two — not worth registering as a 13th format
given it's confirmed dead weight, see below.)

**Real coverage numbers**, sampling 3,000 real rows from each `train.parquet`
(footer-confirmed 310,537 / 236,641 total rows) via `extract_record_text()`:

```
MOPD  --map messages=prompt : 3,000 / 3,000 scannable (100.0%), prompt never null
RL    --map messages=prompt : 3,000 / 3,000 scannable (100.0%), prompt never null
```

**Real end-to-end proof** — ran the actual converter on both real
`test.parquet` files (smaller, so a full-file run, not a slice):

```
$ prepare_rows.py --input-path .../mopd_mixv4_0907/test.parquet \
    --input-type parquet --map messages=prompt --require messages --output-dir ./out
Prepared 538 rows; skipped 0 rows.
Text coverage: 538/538 rows (100.0%) have scannable text.

$ prepare_rows.py --input-path .../rl_mix_v12_ifmt_0903/test.parquet \
    --input-type parquet --map messages=prompt --require messages --output-dir ./out
Prepared 717 rows; skipped 0 rows.
Text coverage: 717/717 rows (100.0%) have scannable text.
```

Both runs passed every built-in check (row-count reconciliation, text
coverage) and wrote a real `_SUCCESS` marker.

**Same structural guarantee as the SFT mix applies here too**: parquet has
one fixed schema per file, so `prompt` is that exact shape in all 310,537 /
236,641 rows, not just the 3,000 sampled — and there's no `tool_calls`
column in the schema, so the same "invisible tool-call arguments" risk
doesn't apply to the `prompt` field either.

### How to run this today

```bash
cd /home/naresh/model-safety

for f in train test; do
  PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
    --input-path /home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/mopd_mixv4_0907/$f.parquet \
    --input-type parquet --map messages=prompt --require messages \
    --output-dir output/mopd_mix_converted/$f
done

for f in train test; do
  PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
    --input-path /home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/rl_mix_v12_ifmt_0903/$f.parquet \
    --input-type parquet --map messages=prompt --require messages \
    --output-dir output/rl_mix_converted/$f
done

sbatch --export=ALL,PII_BASE_DIR=output/mopd_mix_converted,PII_RUN_DIR=output/mopd_mix_pii_run \
  launchers/slurm/run_pii_scan.sbatch
sbatch --export=ALL,PII_BASE_DIR=output/rl_mix_converted,PII_RUN_DIR=output/rl_mix_pii_run \
  launchers/slurm/run_pii_scan.sbatch
```

### Worth knowing (new findings specific to this data, not covered elsewhere in this repo's docs)

- **The `conversations` field lives in the exact same row of the exact same
  file as `prompt`** — not a separate file or a separate dataset, just a
  second column sitting right next to `prompt` on every row. It's a
  **second, partial copy** of the conversation, and it's not scannable
  as-is. It's only populated on some rows (44/3,000 sampled in MOPD = 1.5%;
  867/3,000 sampled in RL = 28.9%). When it *is* present, it's already a
  proper list of turn-objects (good — not the parallel-array problem
  described elsewhere in this repo for `toolbench`), but each turn uses
  `{"from": ..., "value": ...}` instead of `{"role": ..., "content": ...}`,
  with role labels drawn from a 4-word vocabulary confirmed across 20,000
  sampled rows: `human`, `gpt`, `function_call`, `observation` (roughly:
  the user, the assistant, the assistant's tool call, and the tool's
  reply). `extract_record_text()` only recognizes `role`/`content`, so
  today: **0% scannable via a plain `--map messages=conversations`.**
  Renaming `from`→`role` and `value`→`content` first fixes it completely
  (verified: 100% scannable once renamed) — but that rename doesn't exist
  as a flag in `prepare_rows.py` yet.

  **Low priority in practice, and not just a guess.** Lining up a real
  multi-turn row's two fields side by side shows `conversations`'s 3 turns
  (`human`/`function_call`/`observation`) map turn-for-turn onto 3 of
  `prompt`'s 4 turns (`user`/`assistant`/`tool`), word-for-word identical
  content — `conversations` is only missing the `system` (tools list) turn
  that `prompt` has. This isn't only my read of it either: this repo's own
  `format_samples/10_verl_rl_training_row_format.json` — built earlier,
  from a *different* file in the same format family — independently notes
  **"conversations is an empty leftover field in this row."** I also
  grepped every scanning script in this repo for the word `conversations`;
  the only two hits are plain English inside prompt templates ("...including
  conversations, documents, and tools...", a toxicity-guard instruction),
  not a data field being read anywhere. Two independent checks agree: this
  is dead pipeline residue, not a second source of information. (Honest
  caveat: this only covers what *this* repo's scanners do — I can't see
  into the actual RL trainer code, which lives in a different repo,
  `qvac-research-tool-call`.)

- **`raw_system` and `tools` are confirmed pure duplicates, not new
  information.** Checked directly against `prompt`'s own `system` turn on
  real rows: `raw_system` holds the identical tool-list text, and `tools`
  holds the identical tool list, just serialized as its own string instead
  of living inside a message. Nothing lost by not scanning these
  separately, and most of `extra_info`'s ~19 sub-fields are the same story
  — IDs, counts, and flags (`orig_id`, `index`, `n_calls`, `prompt_type`,
  `split`, ...), not content.

- **The one field that genuinely isn't covered by scanning `prompt`: the
  "golden answer."** Every RL row needs a recorded correct answer so the
  reward function can grade the model later — real content that, by
  design, never appears in `prompt` (which is only the question, not the
  answer). It shows up under three different names depending on which
  source built the row:

  | Field | Present on | Reachable with `--map` today? |
  |---|---|---|
  | `reward_model.ground_truth` | Nearly every tool-use row, in both files | **No** — nested inside a struct; `--map` only reaches flat, top-level columns |
  | `answer` | Only `toolcall` / `xlam_irrelevance` rows | **Yes** — it's a flat, top-level string column |
  | `extra_info.output` | Only `rlla` rows (RL mix) | **No** — nested inside `extra_info`, same limit |

  All three hold the same *kind* of content — a tool name plus its
  arguments — with real ID/name/location-shaped strings, e.g.
  `{"name": "book_reservation", "arguments": {"user_id": "yusuf_thomas_7802", "order_id": "#W2091016"}}`.
  That's synthetic, not real user data, but it's exactly the shape PII
  scanning exists to catch, and today it's invisible unless someone
  deliberately scans it — not an automatic assumption that "scanning
  `prompt` is enough" quite covers.

  **How to actually add `answer` — and a sharp edge I tested before writing
  this down.** `--map text=answer` works today, no code change, for the
  rows where it's populated. But you **cannot** just add it to the same
  command as `--map messages=prompt` — verified directly against a real
  row:

  ```
  messages=prompt only               -> "SYSTEM:\n# Tools...you have access to..."   (the full prompt)
  text=answer only                   -> '[{"name": "getDanceTeacher", "arguments": {...}}]'
  BOTH mapped on the same output row -> '[{"name": "getDanceTeacher", "arguments": {...}}]'   <- prompt vanished
  ```

  `extract_record_text()` checks `text` first and returns immediately if
  it's non-empty — it never even looks at `messages` once `text` is set.
  **If you want to scan both `prompt` and `answer`, run two separate
  conversions into two separate output folders.** Combining them in one
  `--map` silently stops the real conversation from being scanned at all —
  an easy, dangerous-looking-fine mistake to make. `reward_model.ground_truth`
  and `extra_info.output` can't be reached this way regardless, since
  they're nested — that needs either a small custom script (pull the
  string out, write plain `{"text": ...}` rows) or a `prepare_rows.py`
  change to support nested paths, the same already-known, not-yet-built
  gap flagged in `HOW_DATA_CONVERSION_WORKS.md`.
- **Traceability is partial and inconsistent.** `extra_info.orig_id` is set
  on roughly 59% of sampled MOPD rows and 0% of sampled RL rows;
  `extra_info.index` is set on ~46% of sampled RL rows. There's no single
  reliable ID column across all rows in either file — for some
  `data_source` values you can trace a hit back to its origin, for others
  you can't, and nothing surfaces which case you're in.
- **`removed_rows.jsonl` here is the "good" sub-shape** — unlike the SFT
  mix's version, these actually carry real excerpted text
  (`train_text`/`bench_text` fields), and would scan correctly today with
  `--map text=train_text`, no code changes. Whether a decontamination audit
  log is in scope for PII screening at all is a judgment call, not a
  technical blocker.

---

## What this means for "our readers & scanners," in one place

| Tool | Reads these 3 datasets today? |
|---|---|
| `prepare_rows.py` (the converter) | **Yes, all three** — `huggingface` and `parquet` input types both already handle the exact shapes found here, including the nested-column case that used to crash |
| PII regex scanner | **Yes**, once converted — it only reads `.jsonl` with `text`/`messages`, and every converted row here will have one |
| Duplicate scanner | **Yes**, same reasoning — same `.jsonl` walk, same shape |
| Toxicity scanner (stage 1) | Would work on the converted `.jsonl` too, but needs a GPU and takes one file at a time — not blocked by *this* data's shape, blocked by infrastructure not covered in this report |
| AI-model PII scanner | Same shape would work, but needs a GPU + Hugging Face token not set up in this environment |

The honest bottom line: **for these three specific datasets, there is
nothing to build.** The gaps documented elsewhere in this repo
(`responses_create_params` nesting, parallel-array `conversations`, PDFs,
tokenized caches) are real problems for *other* parts of
`/home/shared/agentic_slm/data`, but none of them are present in the SFT
mix, MOPD mix, or RL mix. The only things worth deciding on are the three
"worth knowing" items above — all optional, none blocking.

## Recommended next steps

1. Run the four conversion loops above for real (they're cheap — under a
   few minutes total based on the timing of the demo runs) and point the
   PII scanner at each converted folder.
2. Decide whether the "golden answer" fields (`reward_model.ground_truth`,
   `answer`, `extra_info.output`) and the decontamination
   `removed_rows.jsonl` files are in scope for this round of screening.
   `--map text=train_text` on `removed_rows.jsonl` works today, no changes
   needed. `--map text=answer` also works today for the rows that have it
   — **run it as its own separate conversion pass**, never combined with
   `--map messages=prompt` in one command (see the short-circuit warning
   above — mixing them silently drops the real conversation from the scan).
   `reward_model.ground_truth` / `extra_info.output` need a small custom
   script first, since they're nested inside a struct and `--map` can't
   reach them yet.
3. Treat `conversations` as low priority — confirmed dead weight by two
   independent checks (this analysis, and `format_samples/`'s own notes),
   `prompt` already has everything it has, plus more.

## Reproduce every number in this document

```bash
cd /home/naresh/model-safety

# SFT mix — confirm all 61 sub-datasets share the same schema
modelsafety/readers/.venv/bin/python -c "
import json
from pathlib import Path
root = Path('/home/shared/agentic_slm/data/sft/processed/qwen3_5/s1_2_0824/train_shards_keep')
for d in sorted(p for p in root.iterdir() if p.is_dir()):
    info = json.loads((d / 'dataset_info.json').read_text())
    print(d.name, list(info.get('features', {}).keys()))
"

# SFT mix — real text-coverage sample across all 61 sub-datasets
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python -c "
from datasets import load_from_disk
from modelsafety.contract.textract import extract_record_text
from pathlib import Path
root = Path('/home/shared/agentic_slm/data/sft/processed/qwen3_5/s1_2_0824/train_shards_keep')
total = with_text = 0
for d in sorted(p for p in root.iterdir() if p.is_dir()):
    ds = load_from_disk(str(d))
    n = min(ds.num_rows, 300)
    sample = ds[:n]
    for i in range(n):
        row = {k: sample[k][i] for k in sample}
        total += 1
        with_text += 1 if extract_record_text(row) else 0
print(f'{with_text}/{total} = {100*with_text/total:.2f}%')
"

# MOPD / RL mix — real text-coverage sample via the prompt column
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python -c "
import pyarrow.parquet as pq
from modelsafety.contract.textract import extract_record_text
for path in [
    '/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/mopd_mixv4_0907/train.parquet',
    '/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/rl_mix_v12_ifmt_0903/train.parquet',
]:
    tbl = pq.ParquetFile(path).read(columns=['prompt']).slice(0, 3000).to_pylist()
    hits = sum(1 for r in tbl if extract_record_text({'messages': r['prompt']}))
    print(path, f'{hits}/{len(tbl)}')
"

# The three real end-to-end conversions quoted above are still on disk:
ls output/tool_team_readiness_demo/sft_toolmind_when2call/
ls output/tool_team_readiness_demo/mopd_test/
ls output/tool_team_readiness_demo/rl_test/
```

## Where the general background lives

This document is deliberately narrow — just these three datasets. For the
broader picture of what this repo's readers and scanners can and can't do
across the *entire* `/home/shared/agentic_slm/data` lake, see:

- [`HOW_TO_SCAN_YOUR_DATA.md`](HOW_TO_SCAN_YOUR_DATA.md) — how to actually
  run the scanners, environment variables, safety habits.
- [`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md) — the
  converter's real capabilities and limits, in depth.
- [`DATA_FORMAT_INVENTORY.md`](DATA_FORMAT_INVENTORY.md) /
  [`FULL_DATASET_INVENTORY.md`](FULL_DATASET_INVENTORY.md) — every format
  found anywhere in the data lake, not just these three datasets.
- [`DATA_VALIDATION_OPTIONS.md`](DATA_VALIDATION_OPTIONS.md) — why the
  text-coverage / row-count / marker-file checks quoted above exist and
  what they catch.
- [`format_samples/`](format_samples/) — one real, verbatim sample file per
  cataloged shape (12 files, matching `DATA_FORMAT_INVENTORY.md`'s families).
  `10_verl_rl_training_row_format.json` is the one that matches MOPD/RL
  mix's `prompt` column.
