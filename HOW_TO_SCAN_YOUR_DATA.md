# How to use this repo to scan your data

This is a plain-language walkthrough, written after actually reading the code
(not just the other docs) and actually running two of the scans end-to-end.
It's aimed at someone who wants to check the data at
`/home/shared/agentic_slm/data/` for private information (PII), unsafe
content, or duplicate rows, and doesn't want to dig through the code to
figure out how.

## 1. What this repo actually is

It's a toolbox, not one single program. Each tool is a small Python script
that reads text data (in a specific line-by-line format called JSONL — more
on that below) and writes a report about what it found. There is **no single
command** that "runs everything." You pick the tool you need and run it,
usually through the cluster's job queue (SLURM / `sbatch`) so it doesn't run
on the shared login node.

There are four tools that matter for "PII and other tests", plus one more
row below for a format-conversion step that fills in some of the gaps
between them:

| Tool | What it checks for | Ready to use today? |
|---|---|---|
| PII text scan | emails, phone numbers, street addresses, credit cards, IP addresses, crypto wallet addresses | **Yes** — CPU only, no extra setup |
| Duplicate scan | copy-pasted / near-identical rows in your data | **Yes** — CPU only, no extra setup |
| Toxicity scan | unsafe / harmful text | **Partly** — stage 1 needs a GPU; stage 2 needs a model server that isn't set up in this copy of the repo yet |
| AI-model PII scan | a second, smarter PII checker using an AI model instead of pattern-matching | **No, not yet** — needs a GPU, a large model download, and a Hugging Face access token |
| Format converter (turn `.parquet`/`.csv`/plain `.json`/`.arrow` into `.jsonl`) | lets the scanners above reach files that aren't already `.jsonl` | **Yes** — one dataset at a time, including `save_to_disk`-style `.arrow` folders now. See section 5. |

This repo's own checklist (`docs/porting-checklist.md`) is upfront that this
is a fresh copy of another team's tools, reorganised but not all wired up
yet. I'm repeating that warning here in plain words so you don't spend an
hour wondering why something doesn't run: **some of it genuinely isn't ready
yet, and that's expected, not something you're doing wrong.**

### About "the existing scripts"

There's a `scripts/` folder at the top of the repo — it's currently empty
(just a placeholder). The real, working entry points are:

- `launchers/slurm/*.sbatch` and `*.sh` — these are what you actually run.
  Each one sets sensible defaults and creates its own Python environment the
  first time it runs.
- `modelsafety/data_screening/**/*.py` — the actual scanning code. You only
  need to call these directly if you want more control than the launcher
  gives you.

So "using the existing scripts" mostly means: `cd` into this repo, and run
one of the files under `launchers/slurm/` with `sbatch`.

## 2. The data folder: what's actually in it

I looked at `/home/shared/agentic_slm/data/` directly. Here's the honest
picture:

- **33,514 files, about 780 GB total**, spread across six top folders:
  `sft` (484 GB), `mid` (202 GB), `rl` (93 GB), `tau2-bench-data` (615 MB),
  `areal_tau2` (92 MB), `passk_eval` (17 MB).
- Almost all of that is **not plain text**. Breakdown by file type:

| File type | Count | What it actually is |
|---|---|---|
| `.arrow` | 27,931 | Hugging Face's internal dataset cache format. Not readable as plain text directly, but the converter can now reach `save_to_disk`-style folders of it — see below and section 5. |
| `.json` | 2,056 | Regular JSON — a real mix: some are small config/metadata files, but some are genuinely large bulk training data (up to ~1 GB in one file) — see below. |
| `.npy` / `.bin` / `.idx` | ~1,370 | Already-tokenized numbers, ready to feed straight into model training. There's no text left in these at all — nothing for a PII/toxicity scanner to read. |
| `.parquet` | 966 | A compressed table format, same idea as `.arrow`. Not scannable by this repo's tools. |
| `.txt` / `.metadata` / `.lock` | ~870 | Small support files (logs, cache bookkeeping), not conversation data. |
| **`.jsonl`** | **153** | **This is the format our scanners actually read.** Combined size: about **143 GB**. |
| everything else (`.pdf`, `.md`, `.py`, `.csv`, git/cache bookkeeping files, etc.) | ~168 | Misc small files, not data to scan. |

**Looked inside a sample of each of these, not just counted them** — here's
what's actually in there, confirmed directly rather than guessed:

- **`.arrow`** isn't uniformly unreadable junk. Opened a few directly with
  the `datasets` library's `load_from_disk()` (see section 5 for the exact
  command) — some are already shaped exactly like the scanner's target
  (`messages` with `role`/`content` dicts), one turned out to be a flat
  pre-rendered chat-template string (`<|im_start|>system\n...`) instead of
  structured turns — a fourth format variant on top of the ones catalogued
  elsewhere in these docs. The converter can now open both kinds directly
  (see section 5 for the fix and the exact real folders this was tested
  against) — you still need to tell it which column holds the text, same
  as any other format here.
- **`.json`**: the "config/metadata files" guess above was wrong for a
  meaningful chunk of these. Found real bulk training data stored as one
  giant JSON array — up to **989 MB** in one file
  (`rl/raw/envfactory_sft_filtered/mcp_factory_sft_nips.json`), plus a
  271 MB `sft/raw/glaive_function_calling_v2/glaive-function-calling-v2.json`
  and several more in the 90–130 MB range. Same kind of tool-calling data
  covered elsewhere in these docs, just packaged as one big array instead
  of one-line-per-record `.jsonl`.
- **`.npy` / `.bin` / `.idx`**: confirmed two genuinely different things
  share this label. `.npy` files are pure index/shuffle bookkeeping
  (Megatron's `GPTDataset`/`BlendedDataset` — "read document #4821, in this
  shuffled order") — plain integers, never any text. `.bin`/`.idx` pairs
  are the actual tokenized text — and traced a real, exact example:
  `mid/_megatron/full/jsonl/code_000.jsonl` (Family 1, already-scannable
  plain `text`) becomes
  `mid/_megatron/full/tokenized/Qwen3.5-0.8B-Base/code_000_text_document.bin`/`.idx`,
  one-to-one, same for every `web_*`, `pdf_*`, and `native-agent-traj_*`
  shard. **These tokenized files are a downstream product of `.jsonl` this
  repo already knows how to scan** — the fix isn't a new tool, it's making
  sure the `.jsonl` stage always gets scanned before tokenization runs.
- **`.pdf`** (27 files, a tiny slice): most are small result-chart images,
  but a few are real documents —
  `tau2-bench-data/domains/telecom/workflows/tech_support_path*.pdf`,
  30–45 KB workflow/procedure files. Nothing in this repo reads PDFs; that
  needs a text-extraction step (e.g. `pdftotext`) before any scanner could
  see them at all.
- **`.csv`** (2 files total): benchmark result numbers
  (`action_success_rates_telecom*.csv`), not conversation data.
- **`.lock` / `.metadata`**: confirmed empty or near-empty (0–125 bytes
  each) — Hugging Face's own download bookkeeping, nothing to read.

**The bottom line:** out of 33,514 files, the tools in this repo can directly
read about **153 of them** — the `.jsonl` files, which add up to roughly 143
GB. The rest (the huge majority by both count and size) is stored in
training-ready formats that were built for feeding a training run, not for
text scanning. The 2,056 plain `.json` files and 966 `.parquet` files can
now be converted to `.jsonl` first, one dataset at a time (see section 5) —
but the 27,931 `.arrow` files (by far the biggest group) still can't be,
today.

The good news: **you don't need to go find those 153 files yourself.** Every
scanner in this repo, when you point it at a folder, automatically searches
every sub-folder underneath it for `.jsonl` files and quietly skips
everything else. Pointing a scanner at the whole
`/home/shared/agentic_slm/data/` folder is safe and simple — it will not try
to open the giant `.arrow`/`.parquet` files, it just won't find anything to
check in them.

## 3. The one rule your data has to follow

Every scanner expects each line of a `.jsonl` file to be one JSON object,
shaped one of two ways:

```json
{"text": "some plain string to check"}
```

or a chat conversation:

```json
{"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello!"}]}
```

If a `.jsonl` file's rows don't have a `text` or `messages` field, the
scanner won't error out — it will just silently find nothing to check in
that file. Worth a quick look at a sample file before a big scan, so you're
not surprised by a report that says "0 hits" for the wrong reason.

## 4. How each job actually walks your folders

You asked specifically what happens with nested folders and mixed file
types, so here's what I found by reading each tool's code, not just its
docs:

| Tool | What you point it at | Digs into nested sub-folders? | Which files does it actually open? |
|---|---|---|---|
| PII text scan | one folder | **Yes, automatically, any depth** | every file anywhere underneath whose name ends in `.jsonl` |
| Duplicate scan | one folder | **Yes, automatically, any depth** | same — every `.jsonl` anywhere underneath |
| AI-model PII scan | one folder | **Yes, automatically, any depth** | same — every `.jsonl` anywhere underneath |
| Toxicity scan, stage 1 | **one specific file** (or a hand-written list of specific files) | **No** — it does not crawl folders at all | only the exact file(s) you name |
| Toxicity scan, stage 2 | one file, or one folder | **Only that folder's top level** — a `.jsonl` file one level deeper would be missed | `.jsonl` files directly inside the folder you give it |

So for the two tools you'd reach for first — PII and duplicates — you really
can just point them at the very top of `/home/shared/agentic_slm/data/` and
walk away. They will find all 153 `.jsonl` files no matter how deep they're
buried, and they will **not** even attempt to open the `.arrow`/`.parquet`/
`.npy` files sitting right next to them — matching a filename pattern is
all that happens before anything gets opened, so the huge non-`.jsonl`
files never slow the scan down or get read into memory.

**Inside** each `.jsonl` file, every line is checked on its own. A single
file can freely mix lines shaped like `{"text": ...}` with lines shaped
like `{"messages": [...]}` — that's normal and handled fine, no need to
separate them first.

**One catch worth knowing about before a big run:** a line that has valid
JSON but is simply missing both `text` and `messages` is skipped quietly —
it just contributes zero hits. But a line that isn't valid JSON at all (a
truncated row, stray binary bytes, something half-written) is **not**
skipped quietly — both the PII scanner and the duplicate scanner treat that
as a hard error, and **the whole job stops** with a traceback pointing at
that exact file and line number, rather than skipping the bad line and
carrying on. On a data lake this size and this varied, one damaged file
can stop an otherwise-fine scan of everything else. If a big scan ever dies
partway through, check the error log first
(`logs/scans/<tool>/run_<job id>/err/`) — it will usually name the exact
file and line that broke it, and you can exclude that one file (via
`PII_INPUT_MANIFEST` / `DEDUP_INPUT_MANIFEST`, see the environment-variable
tables below) and rerun.

## 5. Converting other file types to JSONL first — update: this is now fixed for most formats

**This section changed since I last wrote this doc.** The two missing
pieces I flagged before (`chunked_writer.py` and a `requirements.txt`) were
added to the repo. I re-tested the whole thing just now, for real, against
an actual file from `/home/shared/agentic_slm/data/` — not just `--help`
this time — so here's the current, verified state.

The script is `modelsafety/readers/prepare_rows.py`. It reads `.jsonl`,
`.json`, `.csv`, `.parquet`, or a proper Hugging Face dataset, lets you say
which of the source's columns should become `text` or `messages`, and
writes out numbered chunks of `.jsonl` files that the other tools can then
read.

**It has no automatic setup yet**, unlike the PII/toxicity/dedup scanners
(which build their own `.venv` the first time you `sbatch` them). You need
to build its environment by hand, once:

```bash
cd /home/naresh/model-safety/modelsafety/readers
python3 -m venv .venv
.venv/bin/pip install -U pip wheel
.venv/bin/pip install -r requirements.txt
```

**Then it genuinely works.** I ran it for real against one of the actual
parquet files buried in the shared data folder (a 6,150-row medical exam
question set, `openlifescienceai/medmcqa`, cached under `sft/curriculum/`):

```bash
cd /home/naresh/model-safety
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/sft/curriculum/mixes/general_warmup_sft_0903/.hfcache/hub/datasets--openlifescienceai--medmcqa/snapshots/91c6572c454088bf71b679ad90aa8dffcd0d5868/data/test-00000-of-00001.parquet \
  --input-type parquet \
  --map text=question \
  --require text \
  --output-dir /home/naresh/model-safety/output/converted_smoke/medmcqa_test \
  --num-chunks 1 \
  --rows-per-file 2000
```

Real output: `Prepared 6150 rows; skipped 0 rows.` — and a real `.jsonl`
file that looks exactly right, one row per exam question, every other
original column preserved under `metadata`:

```json
{"text": "Which of the following is derived from fibroblast cells ?", "metadata": {"id": "84f328d3-fca4-422d-8fb2-19d55eb31503", "opa": "TGF-13", "opb": "MMP2", "opc": "Collagen", "opd": "Angiopoietin", "cop": -1, "subject_name": "Pathology", "...": "..."}}
```

**Then I chained it straight into the PII scanner** (`sbatch
launchers/slurm/run_pii_scan.sbatch` pointed at that output folder) to prove
the whole path — real messy source data all the way through to a validated
report — actually works end to end. Real result on those 6,150 real
questions: 23 raw pattern matches, and only **1** survived stage 2's
filtering. I looked at that one surviving hit out of curiosity — it was the
string `"1991199219931994"` inside a question. That's four consecutive
years jammed together, not a credit card — it just happens to pass the same
checksum a real card number would. This is a good real example of exactly
the point made in section 6 below: a "stage 2 validated" hit is still worth
a human glance, not an automatic confirmed PII finding — that's what the
optional stage 4a (LLM double-check) is for.

**So, three things are now genuinely fixed:**
1. `python3 prepare_rows.py --help` no longer crashes.
2. Real conversion of a real `.parquet` file from the shared folder into
   correctly-shaped `.jsonl` works.
3. The converted output can be scanned by the PII tool immediately, with no
   extra steps.

**Update — this is now fixed.** The rest of this section originally
described a real limit and a workaround for it. Keeping the explanation
below since the "why" is still useful context, but the short version:
`prepare_rows.py` can now read these folders directly, no bypass needed.

**What was broken, originally:** the `huggingface` input type **could not**
read the raw `.arrow` cache folders that make up most of this data lake
(the `data-*.arrow` + `dataset_info.json` + `state.json` folders you see
under `mid/` and `sft/`). Pointing it at one of those folders directly got
this real error back from the `datasets` library itself:

```
ValueError: You are trying to load a dataset that was saved using
`save_to_disk`. Please use `load_from_disk` instead.
```

In plain words: those `.arrow` folders were written by a different saving
method (`save_to_disk`) than the one this script knew how to open
(`load_dataset`). They're two different Hugging Face APIs for two
different things, and the script only spoke one of them.

**The fix:** `prepare_rows.py` now checks for the exact same marker file
the `datasets` library itself checks for internally (`state.json` —
confirmed by reading the library's own installed source at
`modelsafety/readers/.venv/lib/python3.12/site-packages/datasets/load.py`),
and calls `load_from_disk` automatically when that file is present,
falling back to the original `load_dataset` behavior otherwise. No new
flag to learn — `--input-type huggingface` now just handles both cases.
Tested for real against the exact folder used throughout this
conversation:

```bash
$ python3 prepare_rows.py --input-path .../envgen_solo_v1 \
  --input-type huggingface --require messages --output-dir ...
Prepared 999 rows; skipped 0 rows.
```

No error, and the output scans correctly (checked with
`extract_record_text()`, same as every other example in these docs). Also
re-verified on the "flat rendered template" family
(`sft/_sdft_cache/.../eval`) — 243 rows, same clean result.

**One clarification that's still true either way:** `load_from_disk`
itself was never broken or missing — it always worked fine for *looking*
at these folders, it just wasn't being called from `prepare_rows.py`
before. If you just want to see what's inside one of these `.arrow` cache
folders without running the full converter:

```bash
modelsafety/readers/.venv/bin/python -c "
from datasets import load_from_disk
ds = load_from_disk('/path/to/the/folder')
print(ds.column_names)
print(ds[0])
"
```

Ran this for real against one of the shared folder's cache directories —
worked immediately: 243 rows, real columns, a real first row. This was
originally two separate gaps: you could already look at these files with a
few lines of Python, but couldn't run them through `prepare_rows.py`,
because that script only ever called `load_dataset` (which fails on
these), never `load_from_disk` (which works). The fix, described above,
was exactly that narrow — swap in the one Hugging Face function that
already worked, rather than writing something new.

For a standalone `.arrow` file that isn't a full cache folder (no
`dataset_info.json` sitting next to it, just a bare
`data-XXXXX-of-YYYYY.arrow`), the look-without-converting command uses
`pyarrow` directly instead:

```bash
modelsafety/readers/.venv/bin/python -c "
import pyarrow as pa
with pa.memory_map('/path/to/file.arrow', 'r') as source:
    table = pa.ipc.open_stream(source).read_all()
print(table.column_names)
print(table.slice(0, 2).to_pylist())
"
```

And the same idea for `.parquet`:

```bash
modelsafety/readers/.venv/bin/python -c "
import pyarrow.parquet as pq
pf = pq.ParquetFile('/path/to/file.parquet')
print(pf.schema_arrow.names)
print(pf.read_row_group(0).slice(0, 2).to_pylist())
"
```

Plain `head` doesn't work on any of these three — they're binary formats;
`head` just prints unreadable bytes. All three commands need the same
`modelsafety/readers/.venv` used everywhere in this document (it has
`pandas`/`pyarrow`/`datasets` installed) — plain `python3` won't have
these libraries.

**What the real procedure looks like today**, for the formats that do
work: for each dataset, look at it once to see which column holds the
actual text (like I did above with `question`), then run `prepare_rows.py`
with `--map text=<that column>` and an `--output-dir` of your choice, then
point the PII or duplicate scanner at that `--output-dir`. This is still a
**per-dataset, by-hand** step — you decide which column matters each time,
it can't guess that for 966 differently-shaped `.parquet` files on its own.
Also worth knowing: for `.csv`/`.parquet`/`.json`, `--input-path` has to be
**one single file** (e.g. one `test.parquet`), not a folder of several — if
a dataset has separate `train`/`test`/`validation` parquet files, that's
three separate runs. Only the `jsonl` input type accepts a whole folder and
searches it recursively on its own, the same way the PII/duplicate scanners
do.
For a file much bigger than the small one I tested (some of the real
parquet files here run into tens of MB), run this through `srun` instead of
directly on the login node, the same way you would anything else in this
repo — there just isn't a ready-made `sbatch` file for this particular
script yet.

**So, to directly answer "is there a single command to do it all": still
no**, but it's a smaller "no" than before. Converting is a real, working,
one-command-per-dataset step now for `.json`/`.csv`/`.parquet`/`.jsonl`
sources, **and now `save_to_disk`-style `.arrow` folders too** (see the
update above) — it's just not automatic across all 33,514 files in one
shot; you still point it at one dataset at a time. The closest thing to
"one command for everything," `submit_medpsy_quality_filter.sh` (covered in
[`docs/operations/quality-filtering.md`](docs/operations/quality-filtering.md)),
chains the PII scan and the toxicity scan together into one dependency
chain — but it still expects its input to already be `.jsonl`; it doesn't
call the converter either.

## 6. Scanning the data for PII (the main tool)

This is the regex-based scanner (it looks for patterns like
`name@domain.com` or `###-###-####`, similar to a very strict "Find" in a
text editor). It's the most ready-to-use tool in the repo.

**Step 1 — submit the job.** Run this from the repo root, on the login node
— it's fine to type this here, the actual scanning work happens on a
separate worker machine, not this one:

```bash
cd /home/naresh/model-safety

sbatch --cpus-per-task=32 --mem=64G --time=06:00:00 \
  --export=ALL,PII_BASE_DIR=/home/shared/agentic_slm/data,PII_RUN_DIR=/home/naresh/model-safety/output/agentic_slm_pii_run,PII_NUM_WORKERS=32 \
  launchers/slurm/run_pii_scan.sbatch
```

A few notes on that command:
- `PII_BASE_DIR` is the folder to search — point it at the top of the data
  folder, or at a specific sub-folder if you only care about one dataset.
- `PII_RUN_DIR` is where the **results** go. I put it under this repo's own
  `output/` folder, not inside `/home/shared/...` — treat the shared data
  folder as read-only input, and keep your results somewhere that's yours.
- `PII_NUM_WORKERS=32` and `--cpus-per-task=32` control how many files get
  checked at once. The tool's own built-in default is only 4, which would be
  slow for 143 GB of data — 32 is a reasonable starting point on a shared
  cluster; go higher if it's still slow and the cluster has room.
- The first run will take a minute or two longer than later runs, because it
  builds its own small Python environment the first time (`pip install
  piiregex tqdm` into a private `.venv` folder next to the scanner code).

### What actually happens when you run that `sbatch` command

You asked directly about this, so here's the exact mechanics — with proof,
not guesswork, from the smoke test I ran earlier:

- **`sbatch` never runs your job on the login node** (the machine you land
  on when you connect to the cluster — on this machine, that's a computer
  called `login-2`). `sbatch` hands the job to SLURM's scheduler, which
  finds a free machine matching what you asked for (here: the `health`
  partition) and runs the *entire script* over there instead — `sbatch`
  itself just prints a job ID and gives you back control immediately. As
  proof: the two smoke-test jobs I ran earlier actually landed on machines
  called `health-49` and `health-27` — two different physical computers,
  neither one the login node. This is exactly why the docs insist on
  `sbatch` instead of just running the Python file straight on the login
  node — the real work always happens on a separate "worker" machine.
- **`--cpus-per-task=32`** is a request *to SLURM*: "whichever machine ends
  up running this, reserve 32 CPU cores on it for me." This specific job
  (`run_pii_scan.sbatch`) never asks SLURM for a GPU at all — there is no
  `--gres=gpu:...` line in it anywhere — so it is guaranteed to run on CPU
  only, no matter which machine it lands on.
- **`PII_NUM_WORKERS=32`** is a completely different, unrelated setting. It
  is not a SLURM concept at all — it is a plain environment variable that
  the Python scanner itself reads once it's already running. Internally,
  the scanner uses it to start 32 separate copies of itself (plain CPU
  processes, using Python's built-in `multiprocessing`) to check files at
  the same time. There is no GPU anywhere in this scanner — it never
  imports anything GPU-related. You want this number to roughly match
  `--cpus-per-task`, so each of those 32 processes actually gets its own
  CPU core, instead of 32 processes fighting over a smaller number of
  cores.

**Short version for this exact command:** it runs on a separate worker
machine, never the login node; and "32" means 32 parallel CPU processes —
there is no GPU involved anywhere in this particular job.

**Step 2 — check on it:**

```bash
squeue --me
```

**Step 3 — read the results**, once it's done:

```bash
cat /home/naresh/model-safety/output/agentic_slm_pii_run/stage1_scan/metrics.txt
cat /home/naresh/model-safety/output/agentic_slm_pii_run/stage2_validated/metrics.txt
```

- **Stage 1** is the raw pattern-matching pass — every string that merely
  *looks* like an email/phone/etc.
- **Stage 2** filters out obvious false positives (things like `555-555-5555`
  test numbers, `example.com` emails, toll-free numbers). **Stage 2's
  numbers are the ones worth actually looking at.**

The actual flagged snippets live under
`stage2_validated/hits/<category>.txt`, one file per PII type (emails,
phones, street_addresses, credit_cards, and so on). Each line tells you
which source file and line number it came from, so you can go look at the
original context. Nothing in your original data files ever gets changed —
the scanner only ever writes to `PII_RUN_DIR`.

## 7. Finding duplicate rows (also ready to use)

Same idea, different question: "does this same content show up more than
once?" Useful for catching copy-pasted or near-identical training rows.

```bash
cd /home/naresh/model-safety

sbatch --cpus-per-task=16 --mem=128G \
  --export=ALL,DEDUP_INPUT_ROOT=/home/shared/agentic_slm/data,DEDUP_RUN_DIR=/home/naresh/model-safety/output/agentic_slm_dedup \
  launchers/slurm/run_dedup_scan.sbatch
```

Results land in `<DEDUP_RUN_DIR>/summary.json` (counts) and
`<DEDUP_RUN_DIR>/matched_samples.csv` (the actual duplicate pairs found,
side by side). It also writes a de-duplicated copy of your data into
`<DEDUP_RUN_DIR>/filtered/` if you want to use it directly — this is a
**new copy**, your original files are untouched.

## 8. The toxicity scanner (needs more setup first)

This one checks for unsafe/harmful content. It's split into two stages, and
it's more demanding than the two tools above:

- **Stage 1** (a fast filter model called Detoxify) needs a GPU
  (`--gres=gpu:1` is hard-coded into its launcher) and will download its own
  model + a chunk of the `torch`/`transformers` Python libraries (a few GB)
  the first time it runs.
- **Stage 2** (a more careful double-check using a model called Qwen3Guard)
  needs its own little AI model server running first. The launcher tries to
  start one **automatically** from a container image at
  `containers/vllm-openai-v0.24.0-cu129.sqsh` — **that file doesn't exist in
  this copy of the repo yet**, so stage 2 can't run until someone adds it.
  See section 10 below for exactly how that auto-start works.

One more difference worth knowing: unlike the PII and duplicate scanners,
the toxicity scanner does **not** automatically search a whole folder for
you — you point it at one `.jsonl` file at a time (or give it a list of
specific files). So this isn't a "point it at `/home/shared/...` and walk
away" tool the way the other two are.

If you still want to try stage 1 once a GPU and the extra Python packages
are available:

```bash
sbatch --export=ALL,TOX_INPUT=/path/to/one/file.jsonl,TOX_RUN_DIR=/home/naresh/model-safety/output/agentic_slm_toxicity \
  launchers/slurm/run_toxicity_scan.sbatch
```

I didn't run this one myself — it needs a GPU and multi-gigabyte downloads,
which felt like something to check with you first rather than just doing it.

## 9. The AI-model PII scanner (not ready in this copy of the repo)

There's a second, more thorough PII checker that uses an actual AI model
(`OpenMed/privacy-filter-nemotron-v2`) instead of pattern-matching. It's
meant to catch subtler PII that plain patterns miss. It needs:
- 8 GPUs for a full run (a "pilot"/small-test mode exists too),
- a Hugging Face access token (`HF_TOKEN`) since the model may be gated,
- and its own Python environment (`torch`, `transformers`, `openmed`).

None of that is set up in this environment yet. Full details are in
[`docs/data-screening/pii-model.md`](docs/data-screening/pii-model.md) if
you want to set it up later. Its optional LLM-confirmation step follows the
same auto-start pattern described in section 10.

## 10. How the "secondary model" steps actually get their AI model

Three parts of this repo need a second AI model running as its own little
web server, which the scanning code then talks to over HTTP: the toxicity
scanner's stage 2 (Qwen3Guard), the PII scanner's optional "double-check
with an LLM" stage (Gemma), and the AI-model PII scanner's optional LLM
confirmation step (also Gemma). You asked directly whether the code spins
that model server up for you automatically, or whether it expects you to
already have one running somewhere else, like your own inference API.

**Answer: it's designed to spin the model server up for you, completely
automatically, inside the very same job — you are not expected to deploy
your own separate inference API.** I read all three of the relevant
`sbatch` scripts
(`run_toxicity_scan.sbatch`, `run_pii_llm.sbatch`/`run_pii_llm_full_node.sbatch`,
`run_privacy_filter_llm.sbatch`) and they all do the same five things, in
this order, every time:

1. Ask SLURM for a GPU (or several) — the same kind of resource request as
   `--cpus-per-task`, just for GPUs instead (`--gres=gpu:...`).
2. Once that GPU is allocated, start a **container** on it — a
   self-contained, pre-built bundle that already has the model-serving
   software (`vLLM`) installed — using `srun --container-image=<file>.sqsh`
   to launch it. **This is the secondary model actually starting up, fully
   automatically, with no manual step from you.**
3. Wait, checking a `/health` web address every few seconds (for up to
   15–40 minutes depending on the script, since large models take a while
   to load), until that container reports the model is loaded and ready.
4. Only then run the actual scanning/validation script, which talks to
   that freshly-started model over `http://127.0.0.1:<port>/v1` — a server
   on the very same machine, that this very same job just started for
   itself.
5. When the job ends — success, failure, or you cancelling it — it shuts
   that model server back down automatically, so it doesn't sit there
   holding the GPU afterwards.

So "go deploy a model server" is not a manual step you need to do —
**provided two ingredients are already in place:**

- **The container image file itself:** a `.sqsh` file expected at
  `containers/vllm-openai-v0.24.0-cu129.sqsh`. This is the one missing
  ingredient in this copy of the repo right now — confirmed, that whole
  `containers/` folder doesn't exist yet. Someone needs to build or copy
  that file in before any of these three auto-start flows can succeed.
- **A Hugging Face access token (`HF_TOKEN`)** — only needed because the
  specific models used here (Gemma, and optionally Qwen3Guard) require
  Hugging Face's permission before they can be downloaded. Set it as an
  environment variable, or put it in a file named `.env` in this repo's
  top folder (also missing right now) — the scripts check for `.env`
  automatically whenever `HF_TOKEN` isn't already set.

**If you already have your own model server running elsewhere** — your own
inference API, already serving Gemma or Qwen3Guard — you can skip the
auto-start entirely. You'd call the underlying Python script directly
instead of the `sbatch` wrapper (the wrapper always tries to start its own
server), pointing `--base-url` (or the `OPENAI_BASE_URL` environment
variable) at your existing server's address instead. That's supported by
the scripts themselves — it's just not what the ready-made `sbatch`
launchers do by default.

## 11. Environment variables, explained in plain words

These are the settings each tool reads. I got this list by reading the
actual Python/shell code, not just the docs, so it should match what's
really there. "Default" is what happens if you don't set it.

### PII text scan (`launchers/slurm/run_pii_scan.sbatch`)

| Variable | What it means | Default |
|---|---|---|
| `PII_BASE_DIR` | Folder to search for `.jsonl` files | *(required — job fails without it)* |
| `PII_RUN_DIR` | Where to write results | `tmp/qa_scan_test/pii_run` under the repo |
| `PII_NUM_WORKERS` | How many files to check in parallel | 4 (the SLURM script's CPU count) |
| `PII_CHUNK_SIZE` | Advanced: how many lines each worker handles per batch | 10,000 — fine to leave alone |
| `PII_INPUT_MANIFEST` | Optional: a text file listing specific files to scan, instead of everything under `PII_BASE_DIR` | not set (scans everything) |
| `PII_LOG_ROOT` | Where the job's own log files go | `logs/scans/pii_scan/run_<job id>` |

Two more you'll only need for the LLM-based "is this candidate *really*
PII?" double-check step (`pii_llm_validator.py`), which needs your own
running AI model server:

| Variable | What it means | Default |
|---|---|---|
| `OPENAI_BASE_URL` | Address of the AI model server to ask | `http://127.0.0.1:8000/v1` |
| `OPENAI_MODEL` | Which model name to request from that server | `local-model` |
| `OPENAI_API_KEY` | Access key for that server, if it needs one | empty |

### Duplicate scan (`launchers/slurm/run_dedup_scan.sbatch`)

| Variable | What it means | Default |
|---|---|---|
| `DEDUP_INPUT_ROOT` | Folder to search for `.jsonl` files | `tmp/qa_scan_test/input` under the repo |
| `DEDUP_RUN_DIR` | Where to write results | `tmp/qa_scan_test/dedup_out` under the repo |
| `DEDUP_TEXT_SCOPE` | `prompt` = ignore the AI's final answer and only compare the question/setup; `all` = compare everything | `prompt` |
| `DEDUP_WORKERS` | How many files to check in parallel | 16 |
| `DEDUP_WRITE_FILTERED` | Whether to write out a clean, de-duplicated copy of the data | `1` (yes) |
| `DEDUP_REFERENCE_ROOT` / `DEDUP_CANDIDATE_ROOT` | Set both together instead of `DEDUP_INPUT_ROOT` if you want to check one dataset against a *different* dataset (e.g. "does our training data overlap with this benchmark?") | not set |
| `DEDUP_NGRAM_SIZE`, `DEDUP_NUM_PERM`, `DEDUP_BANDS`, `DEDUP_ROWS_PER_BAND` | Advanced tuning for "how similar counts as duplicate" | sensible defaults — leave alone unless you know why you're changing them |

### Toxicity scan (`launchers/slurm/run_toxicity_scan.sbatch`) — needs a GPU

| Variable | What it means | Default |
|---|---|---|
| `TOX_INPUT` | The single `.jsonl` file to check | a sample file under `tmp/qa_scan_test/` |
| `TOX_RUN_DIR` | Where results go | `tmp/qa_scan_test/toxicity_out` |
| `TOX_THRESHOLD` | How suspicious something must score (0–1) before it gets flagged for the closer look in stage 2 | `0.5` |
| `TOX_BATCH_SIZE` | How many lines to check at once | 32 |
| `GUARD_MODEL` | Which model does the stage-2 double-check | `Qwen/Qwen3Guard-Gen-8B` |
| `GUARD_CONCURRENCY` | How many stage-2 requests run at once | 8 |
| `VLLM_SQUASHFS_PATH` | Location of the container image that runs the stage-2 model | `containers/vllm-openai-v0.24.0-cu129.sqsh` — **missing right now** |
| `HF_HOME` / `HF_TOKEN` | Where downloaded AI models are cached, and your access key for models that require permission | `cache/hf` under the repo / not set |

## 12. The smoke test — I actually ran this, here's what happened

You asked whether we could run something like a smoke test to check the
whole flow actually works, before pointing anything at the real 780 GB of
data. So I did exactly that, for real, on this cluster:

**1. I made a tiny fake data file** at
`tmp/qa_scan_test/input/sample.jsonl` — this path is the *built-in default*
input location the launchers already look for, so no extra setup was
needed. It has 5 lines: one with a fake email/phone/address, one with a fake
(but properly-formatted) credit card number in a chat-message format, and
three plain sentences with no personal info — two of which are exact
duplicates of each other, to test the duplicate scanner too. Nothing in it
is real — the address is literally The Simpsons' house.

**2. I ran the PII scanner on it** with the plain, no-extra-settings command:

```bash
cd /home/naresh/model-safety
sbatch launchers/slurm/run_pii_scan.sbatch
```

It finished in under a minute. Real results:

```
Raw regex hits by category:
  emails              : 1
  phones              : 3
  credit_cards        : 1
  (everything else)   : 0
TOTAL HITS          : 5

After stage 2 filtering:
  emails        1 -> 1 kept (100%)
  phones        3 -> 1 kept (33%)
  credit_cards  1 -> 1 kept (100%)
TOTAL: 5 raw hits -> 3 kept
```

It correctly found the fake email, the fake phone number, and the fake
credit card, and correctly filtered out 2 of the 3 raw "phone-shaped" matches
as noise. (My fake street address didn't get picked up at all, even at
stage 1 — worth knowing that the address pattern this tool uses is fairly
narrow, so don't assume "0 addresses found" means your data has no
addresses in it.)

**3. I ran the duplicate scanner on the same file:**

```bash
sbatch launchers/slurm/run_dedup_scan.sbatch
```

It finished in about 2 minutes (first run builds its own Python
environment). Real result: it found exactly the one duplicate pair I'd
planted (the two identical sentences), correctly left the other 3 unique
lines alone, and wrote a de-duplicated copy of the file.

**What this proves:** the job-submission plumbing, the automatic setup of
each tool's Python environment, the actual scanning code, and the report
files all genuinely work end-to-end on this cluster right now, for the PII
and duplicate scanners. That's not guaranteed by just reading the docs — I
wanted to see it actually happen before telling you to trust it on real
data.

**Rerunning it yourself:** the fake file is still sitting at
`tmp/qa_scan_test/input/sample.jsonl`. Just run either `sbatch` command
above again any time you want to sanity-check the tools still work before a
big real scan — takes under a minute for the PII one.

## 13. A few safety habits worth keeping

- **Never run a real scan directly on the login node** (the machine you get
  when you SSH in). Always go through `sbatch`, like every example above
  does — that sends the work to a separate worker machine.
- **Write results outside of `/home/shared/agentic_slm/data/`.** Even though
  it turned out to be writable by your account, it's not your folder — keep
  the scanners' output under your own space (this repo's `output/`, or your
  home directory).
- **None of these scanners modify your original files.** They only ever
  read from the input folder and write to a separate results folder you
  choose.
- Every job writes its own log files under `logs/` in this repo — if
  something looks wrong, that's the first place to check
  (`logs/scans/<tool>/run_<job id>/`).

## 14. What's genuinely not built yet

Straight from this repo's own checklist, in plain words:
- No single guided command (like "run me and ask what you need") exists
  yet — you set environment variables yourself, as shown above.
- No automatic way to redact/remove found PII from your data yet — the
  scanners report *where* PII is, they don't edit anything for you.
- The `.parquet`/`.csv`/plain-`.json` → `.jsonl` converter
  (`prepare_rows.py`) now works, but only per-dataset by hand, with no
  `sbatch` launcher of its own yet — and it still cannot read the raw
  `.arrow` cache folders that make up most of this data lake (confirmed by
  actually trying it — see section 5 for the exact error and why).
- The toxicity double-check stage and the AI-model PII scanner need a
  container image and/or GPU access this environment doesn't have yet (see
  sections 8, 9, and 10 above).
