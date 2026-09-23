# How the toxicity scan works

This is a walkthrough of the toxicity scan as it exists in this repo today:
what it uses, what it needs, how to run it, and where it falls short. It's
based on reading the actual scripts and checking this machine, not on the
older docs alone.

**Update: a small trial run has now been done, twice**, on the same 16 rows
(15 real rows from the tool team data plus one deliberately toxic line, to
make sure the whole path got exercised). The first attempt found a real bug
in the launcher script; it's now fixed, and a second run confirmed a clean
pass end to end. Details are folded in below, marked with **Update:**.

---

## The big picture

The scan works in two passes, like airport security:

1. **Pass 1 is the quick walk-through scanner.** A small, fast model looks at
   every single message and gives it a "how offensive does this sound" score.
   Anything that scores above a cut-off gets pulled aside.
2. **Pass 2 is the officer who opens the bag.** A much bigger, smarter model
   looks only at the messages pass 1 pulled aside, reads them with the
   conversation around them, and decides whether they're actually Safe,
   Unsafe, or Controversial.

The point of doing it this way is cost. The big model is slow and needs a
lot of GPU memory, so we don't want it reading millions of messages. The
small model does the bulk of the reading, and the big model only gets the
suspicious ones.

The same "cheap check first, careful check second" idea is used in the PII
scan too.

---

## Which models are used

| | Pass 1 (quick) | Pass 2 (careful) |
|---|---|---|
| Model | **Detoxify**, "multilingual" version | **Qwen3Guard-Gen-8B** |
| Made by | Unitary AI | Alibaba's Qwen team |
| Size | Small (about 280 million parameters) | Large (8 billion parameters) |
| What it is | A classifier: text goes in, a set of scores comes out | A chat model trained specifically to judge safety: a conversation goes in, a written verdict comes out |
| How it runs | Loaded directly inside our Python script | Runs as a separate model server (vLLM), and our script sends it requests |
| Where it's set | `TOX_STAGE1_MODEL` (default `multilingual`) | `GUARD_MODEL` (default `Qwen/Qwen3Guard-Gen-8B`) |

Detoxify was trained on the Jigsaw datasets, which are mostly comments from
Wikipedia talk pages and news sites. It's good at spotting rude, abusive, and
hateful *language*. The multilingual version covers English, French,
Spanish, Italian, Portuguese, Turkish, and Russian.

Qwen3Guard is a real safety judge. It understands *intent* in a way Detoxify
doesn't, which is why it's used as the final decision-maker.

The code for each pass:

- Pass 1: `modelsafety/data_screening/toxicity/run.py` (uses `detoxify.py`)
- Pass 2: `modelsafety/data_screening/toxicity/llm_check_guard.py`
- Summary report: `modelsafety/data_screening/toxicity/guard_report.py`

---

## What categories it looks for

### Pass 1 (Detoxify)

Detoxify gives every message a score between 0 and 1 for each of these:

- toxicity
- severe toxicity
- obscene
- threat
- insult
- identity attack (hate aimed at a group)
- sexually explicit

(This list is from Detoxify's own documentation. Our code doesn't hardcode
it; it reads the labels out of the model file when it loads, so the exact
list is whatever the model says.)

A message is pulled aside if its **highest** score across all of those is
above **0.5**. You can change that cut-off with `TOX_THRESHOLD`.

### Pass 2 (Qwen3Guard)

For each pulled-aside message, Qwen3Guard answers three things:

- **Overall verdict:** Safe, Controversial, or Unsafe
- **Which categories:** any of Violent, Non-violent Illegal Acts, Sexual
  Content or Sexual Acts, PII, Suicide & Self-Harm, Unethical Acts,
  Politically Sensitive Topics, Copyright Violation, Jailbreak (or None)
- **Did the assistant refuse?** Yes or No (only asked when the flagged
  message is from the assistant)

Pass 2's list is much broader than pass 1's. It covers things like
self-harm, illegal activity, and jailbreak attempts, which Detoxify was
never trained to spot. That gap matters, and it's the first item under
"What the scan misses" below.

---

## What goes in

The scan reads **`.jsonl` files** (one JSON record per line). Each record has
to have one of these:

- a `"text"` field with a plain string, or
- a `"messages"` field with a list of chat turns, each with a `role` and
  `content`

With `messages`, pass 1 scores **each message on its own**, with the role
stuck on the front, like `"user: how do I ..."`. Every role gets scanned:
system, user, assistant, and tool.

Nothing else in the record gets looked at. Metadata, reward/answer fields,
and any other columns are ignored.

**For the tool team data:** the files we already converted in
`output/tool_team_converted/` (1,174 files across SFT, MOPD and RL) are in
exactly this `messages` shape, so they can go straight in. Tool calls in
this data are written inline as XML text inside the message content
(`<tool_call><function=...>`), so they get scanned along with everything
else.

Raw Parquet/Arrow won't work. It has to be converted to JSONL first, same as
for the PII scan.

---

## What comes out

After pass 1:

- `flagged.jsonl`: one line per pulled-aside message, with its scores, which
  category scored highest, where it came from (file + line + message
  number), and a copy of the full original record

After pass 2, in a `guard_results/` folder:

- `all.jsonl`: every checked message with Qwen3Guard's verdict attached
- `safe.jsonl`, `unsafe.jsonl`, `controversial.jsonl`: the same, split by
  verdict
- `summary.json`: counts per verdict and per category

Plus a readable report of how many Safe/Unsafe/Controversial came from users
vs assistants.

Every result points back to the exact source file and line number, so you
can always go look at the original.

---

## Setup: does it download and install things by itself?

Mostly yes, with one important exception.

| Piece | Automatic? | Details |
|---|---|---|
| Python environment | **Yes** | First run creates `modelsafety/data_screening/toxicity/.venv` and installs `requirements.txt` into it. Takes a few minutes the first time only. |
| Detoxify model | **Yes** | Downloaded on first use from Detoxify's GitHub releases, into `~/.cache/torch/hub/`. Small. |
| Qwen3Guard model | **Yes** | Downloaded by the model server on first use from HuggingFace into `cache/hf/` in the repo. About 16 GB, so the first run spends a while on this. |
| The model server (vLLM) itself | **No, and it's missing** | See below |

**The missing piece.** Pass 2 doesn't install vLLM. It expects a ready-made
container image at `containers/vllm-openai-v0.24.0-cu129.sqsh` inside the
repo. **That folder doesn't exist in this copy of the repo.** If you run as-is,
pass 1 will finish and then pass 2 will stop with
`FATAL: vLLM SquashFS image not found`.

There is a shared image on the cluster at
`/home/shared/containers/vllm-0.22.0-cu129.sqsh`. You can point the scan at
it by setting:

```bash
VLLM_SQUASHFS_PATH=/home/shared/containers/vllm-0.22.0-cu129.sqsh
```

That's an older vLLM version than the one the scripts were written for.
**Update: a trial run confirmed it serves Qwen3Guard-Gen-8B fine** — the
model loads, and the `/health` check passes. There's a separate bug that
stops requests from reaching it; see "What the scan misses" item 0 below.

Two more things worth knowing:

- Model downloads happen **on the GPU machine the job lands on**, so that
  machine needs internet access. If it doesn't, you'd have to download the
  models ahead of time.
- The 8-GPU helper script (`run_detoxify_multigpu.sbatch`, described below)
  does **not** create the Python environment. It expects one of the other
  scripts to have run once already.

---

## One pass or two?

**You can run both passes as a single job.** Both main scripts do that by
default: pass 1, then start the model server, then pass 2, then write the
report, then shut the server down. If pass 1 flags nothing, the scan skips
pass 2 completely and doesn't bother starting the server.

You can also split them if you want to:

- **Only pass 1:** with the sharded script, set `TOX_STAGE1_ONLY=1`. Useful
  to see how much gets flagged before committing to the expensive pass.
- **Only pass 2, reusing an earlier pass 1:** with the sharded script, set
  `TOX_REUSE_STAGE1=1`. It picks up the existing `flagged.jsonl` instead of
  redoing pass 1.
- **Pass 2 against a model server you started yourself:** run
  `llm_check_guard.py` directly with `--base_url` pointing at that server.

Pass 2 can't run without pass 1. It only reads pass 1's `flagged.jsonl`.

---

## GPUs and how the work is spread out

There are three launcher scripts, each suited to a different job size.

### 1. `run_toxicity_scan.sbatch`: one file, one GPU

- **1 GPU**, 8 CPU cores, 48 GB memory, 4-hour limit
- Pass 1 and pass 2 take turns on the same GPU: Detoxify finishes, then the
  Qwen3Guard server starts up on that GPU
- Pass 2 sends **8 requests at a time** to the server (`GUARD_CONCURRENCY`)
- **Only scans one file** (`TOX_INPUT`). It won't search a folder.

Good for: trying things out, or small datasets that fit in one file.

### 2. `run_toxicity_scan_array.sbatch`: many files, split into shards

- The file list gets split into **shards** (groups of files). Each shard is
  its own small job with **1 H100 GPU**, 16 CPU cores, 96 GB memory, 48-hour
  limit.
- By default **up to 8 shards run at the same time**, so up to 8 GPUs in
  use. Change it with `QUALITY_ARRAY_CONCURRENCY`.
- Each shard does its own pass 1, starts its **own copy** of the Qwen3Guard
  server, and does its own pass 2. Shards don't share anything.
- Needs a "prepare" step first that builds the shard lists (see "How to run
  it" below).
- Writes a `_SUCCESS` marker per shard when it's done.

Good for: whole datasets. This is the one to use for the tool team data.

### 3. `run_detoxify_multigpu.sbatch`: pass 1 only, full 8-GPU machine

- Takes a whole machine with **8 H100 GPUs** and runs one shard's pass 1 on
  each GPU at the same time
- **Pass 1 only.** You then run the array script with `TOX_REUSE_STAGE1=1`
  to do pass 2.
- Skips shards that already finished pass 1

Good for: catching up on pass 1 quickly when you have a lot of shards.

### Things to know about the parallel setup

- Pass 1 scores messages in batches of 32 (`TOX_BATCH_SIZE`). Long messages
  get cut into pieces the model can handle (about 512 tokens each), and the
  message keeps its worst piece's score, so nothing gets lost by being long.
- The pass 1 code *could* spread one job across several GPUs, but none of
  the launchers use that. It's always one GPU per job.
- 8 requests at a time to Qwen3Guard is on the cautious side for an H100. If
  pass 2 turns out slow, raising `GUARD_CONCURRENCY` is the first thing to
  try, after a trial run.

---

## How to run it

All launchers default to the `health` partition. To use a different one,
add `--partition=...` to the `sbatch` command; that wins over what's written
inside the script. (Partitions on this cluster: `main`, `health`,
`toolCall`, `VLM`, all with 8×H100 machines.)

### 1. For the medical data

The medical team runs toxicity as part of one big pipeline that also does
PII:

```bash
cd /home/naresh/model-safety
export QUALITY_INPUT_ROOT=/path/to/medical/jsonl
export QUALITY_RUN_ROOT=$PWD/output/medpsy2/pii_toxicity_cleaning
export QUALITY_SHARD_COUNT=16
bash launchers/slurm/submit_medpsy_quality_filter.sh
```

That one command submits a chain of jobs, each waiting for the one before:

```text
prepare (build shard lists)
   ├── PII scan (CPU, per shard) ──► PII check with Gemma (GPU, per shard) ──┐
   └── toxicity scan (GPU, per shard: pass 1 + pass 2) ──────────────────────┤
                                                                             ▼
                                          final filter (splits rows into kept / removed)
                                                                             ▼
                                                                          report
```

In that pipeline, a whole row gets **removed** if Qwen3Guard says Unsafe or
Controversial, or if it couldn't give a clear answer, or if the request
errored. It errs on the side of removing. Safe verdicts keep the row.

What's missing to run it here: the vLLM image (same fix as above, set
`VLLM_SQUASHFS_PATH`), and a HuggingFace token with access to Gemma for the
PII part (`HF_TOKEN`, or put it in a `.env` file at the repo root).

For a trial first, use a separate output folder and set
`QUALITY_PILOT_FILES=8 QUALITY_SHARD_COUNT=8`
(see `docs/operations/quality-filtering.md`).

### 2. For the tool team data (or any other dataset) -- use the wrapper script

There's now a wrapper for toxicity, the same shape as the PII one:
`scripts/tool_team_toxicity_scan.sh`. It shards the input, submits the
sharded launcher below for you, resumes an existing run instead of
resubmitting finished shards, and can summarize results without touching
the cluster.

**Step 0: make sure the data is JSONL** with `messages` or `text`. For the
tool team this is already done in `output/tool_team_converted/`, which is
the script's default `--input-root`.

**Step 1: a small trial first** (3 files, one from each of SFT/MOPD/RL):

```bash
cd /home/naresh/model-safety
./scripts/tool_team_toxicity_scan.sh all --pilot-files 3 --partition toolCall
```

**Step 2: check the trial worked, without touching the cluster yourself:**

```bash
./scripts/tool_team_toxicity_scan.sh report
```

This shows how many shards are fully done, screened-only, or not started
yet, how many messages pass 1 flagged in total, and -- importantly -- warns
if any shard hit a guard **error** (the 8,192-token limit is the likely
cause; see item 2 below). It also writes an aggregated guard report to
`<run-dir>/reports/guard_report.md`. Safe to run at any time, including
while a job is still on the cluster.

**Step 3: full run**, same run dir, just ask for everything:

```bash
./scripts/tool_team_toxicity_scan.sh all --partition toolCall
```

Since a manifest already exists at the default run dir, this resumes it:
only shards that aren't fully done yet get submitted, and `--shards` /
`--pilot-files` are ignored (a printed note says so). Running the exact
same command again after it finishes will say there's nothing left to do.

**For any other dataset:** `--input-root /path/to/other/jsonl --run-dir
/path/to/a/new/output/folder` -- those are the only two things that change.

**To split pass 1 and pass 2 apart** (e.g. to eyeball how much pass 1
flags before paying for pass 2 at all): `screen` first, `report` to check
the flagged rate, then `verify` once it looks reasonable. `verify` refuses
and tells you which shards aren't ready if any haven't finished `screen`
yet, rather than silently skipping them.

Run `./scripts/tool_team_toxicity_scan.sh --help` for the full flag list
(resources, shard count, concurrency, threshold, vLLM image, ...), or just
run it with no arguments for an interactive menu.

<details>
<summary>What the script does under the hood (manual commands, for reference)</summary>

```bash
cd /home/naresh/model-safety
export REPO=$PWD
export QUALITY_INPUT_ROOT=$PWD/output/tool_team_converted
export QUALITY_RUN_ROOT=$PWD/output/tool_team_toxicity_pilot
export QUALITY_MANIFEST_DIR=$QUALITY_RUN_ROOT/manifests
export QUALITY_SHARD_COUNT=3
export QUALITY_PILOT_FILES=3
export VLLM_SQUASHFS_PATH=/home/shared/containers/vllm-0.22.0-cu129.sqsh

prep=$(sbatch --parsable --partition=toolCall launchers/slurm/prepare_quality_filter.sbatch)
sbatch --partition=toolCall --dependency=afterok:$prep --array=0-2 \
  launchers/slurm/run_toxicity_scan_array.sbatch
```

Each shard folder under `$QUALITY_RUN_ROOT/scans/toxicity/shards/` gets a
`_DETOXIFY_SUCCESS` marker after pass 1 and a `_SUCCESS` marker after pass
2. The model server's own log is `vllm.log` in the same shard folder.

**For a single file**, the simpler non-sharded launcher is still fine to
call directly (the wrapper script doesn't cover this case):

```bash
sbatch --partition=toolCall \
  --export=ALL,TOX_INPUT=/path/to/file.jsonl,TOX_RUN_DIR=$PWD/output/tox_one_file,VLLM_SQUASHFS_PATH=/home/shared/containers/vllm-0.22.0-cu129.sqsh \
  launchers/slurm/run_toxicity_scan.sbatch
```

Its logs land in `logs/scans/toxicity_scan/run_<jobid>/`, with separate
files for pass 1, pass 2, and the model server.

</details>

---

## What the scan misses

These are the gaps worth knowing about, roughly in order of how much they
matter.

**0. Update — found a real bug, now fixed and verified.** The first trial
run showed the single-file launcher (`run_toxicity_scan.sbatch`) couldn't
actually reach Qwen3Guard: it starts vLLM without telling it what name to
serve the model under (`--served-model-name`). The server started fine and
passed its health check, but every real classification request then got
rejected with `NotFoundError: ... The model 'Qwen/Qwen3Guard-Gen-8B' does
not exist.`. The sharded script (`run_toxicity_scan_array.sbatch`) already
did this correctly, passing `--model "${MODEL}" --served-model-name
"${MODEL}"` side by side — the single-file script only had `--model`.

That line has been added to `run_toxicity_scan.sbatch`, and a second trial
run on the same 16 rows confirmed it: 0 errors, and the synthetic toxic row
came back correctly classified as `Unsafe` / `Unethical Acts`. This is
fixed. It's listed here mainly as a reminder that **this bug would have
silently over-removed data** if it had gone unnoticed — in the medical
pipeline's rules, a guard error counts as unsafe and removes the whole row.

**1. Harmful content that's written politely never reaches pass 2.**
This is the big one. Pass 2 only sees what Detoxify flags, and Detoxify
flags rude *language*. A calm, polite request for how to make a weapon, a
jailbreak attempt, or self-harm advice written in a caring tone may score
low on "toxicity" and sail straight through. Qwen3Guard would catch those,
but it never gets to see them. So the scan is good at "is this abusive?" and
much weaker at "is this dangerous?"

**2. Long conversations will likely fail in pass 2, especially tool team data.**
When pass 2 checks a flagged message, it sends the whole conversation up to
that point, **including the system prompt with all the tool definitions**.
The model server is set to a limit of 8,192 tokens. In a sample of the tool
team data, the longest conversations were about 65,000 characters, roughly
16,000 tokens. Those requests will be rejected and counted as
"errors". In the medical pipeline an error means the row gets removed, so
this could quietly remove good rows. The trial run's error count is how
we'll find out how big this is. (`VLLM_MAX_MODEL_LEN` raises the limit, but
the scripts don't cut long conversations down to fit.)

**3. Only the `messages` / `text` fields are read.**
Same limit as the PII scan. For MOPD and RL, the "golden answer" fields
(`reward_model.ground_truth`, `answer`, and so on) aren't scanned. See
`TOOL_TEAM_DATA_READINESS.md` for more on those fields.

**4. Detoxify wasn't built for this kind of text.**
It learned from people arguing in comment sections. Tool team data is full
of JSON, XML, function schemas and code. How Detoxify scores that is hard to
predict. It could flag harmless code, or miss things hidden inside it. Also,
languages outside its seven are covered poorly.

**5. One cut-off for everything.**
The 0.5 threshold applies to the highest of all Detoxify's scores. There's
no way to be stricter on "threat" and looser on "obscene", for example.

**6. Some things could slip through quietly.**
- Pass 2 **skips** broken JSON lines with a warning instead of stopping.
  (Pass 1 stops on them, so this is unlikely in practice.)
- Qwen3Guard's categories are pulled out of its answer text by pattern
  matching. If it ever words something differently, that result becomes
  "unknown".
- I'm not certain how Qwen3Guard handles conversations with `tool` role
  messages or several system messages in them, since it's mainly built
  around user/assistant chats. Worth checking a handful of results by hand
  in the trial.

**7. Smaller practical limits.**
- The single-file script only takes one file, and reruns overwrite earlier
  results. There's no resuming.
- `flagged.jsonl` stores a full copy of the original record for every
  flagged *message*. So a long conversation with five flagged messages gets
  copied five times. Could get big if a lot is flagged.

---

## Anything else that helps

- **Pass 1 is cheap to run on its own.** On a new dataset it's worth doing
  pass 1 only first (`TOX_STAGE1_ONLY=1`) and looking at how much gets
  flagged. If 30% of messages are flagged, the threshold is wrong for that
  data and pass 2 would be expensive for no reason. Pass 1 prints a score
  breakdown at the end that shows this.
- **Qwen3Guard also flags PII and Jailbreak.** So pass 2 results can double
  as a second opinion on the PII findings, for whatever it happens to see.
- **There's also a medical-specific version** (`medical_guard_pilot.py`,
  run by `run_medical_guard_pilot.sbatch`). It compares two other judges
  (IBM Granite Guardian and Google Gemma) against a written medical policy in
  `modelsafety/data_screening/toxicity/policies/`. It's not useful for tool
  data as-is, but it shows how you'd build a judge with your own rules, for
  example "is this a real prompt-injection attempt or just a demo of one?"
- **The scan never changes your data.** It only writes new result files. In
  the medical pipeline, the separate "final filter" step is what makes the
  cleaned copy, and even that writes a new folder instead of editing the
  original.
- **Time estimates:** we don't have any yet, since it's never been run
  here. The trial run will give real numbers per shard.
- **More detail:** `docs/data-screening/toxicity.md` (script-level usage)
  and `docs/operations/quality-filtering.md` (the medical pipeline).
