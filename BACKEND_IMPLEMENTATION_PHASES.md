# Backend implementation phases

From Original data to clean data, with every row traceable at every step.

## What this doc is for

It has two readers.

- **You.** Each phase opens with plain words: what we're doing, what we'll
  have afterwards, and how.
- **The models.** Opus 5.5 plans one phase at a time, and Sonnet 5.5 builds
  that plan. Give them this doc and `TRACKING_SCANNED_ROWS.md`. The tracking
  doc explains the words used here (Original, Prepared, run folder, finding,
  fingerprint, origin note) and what works today. Each phase below has a
  "Context for planning" section that lists the code to read first.

This doc covers the backend only. The UI gets its own doc later, but every
phase here is built so the UI can read its results (see "How the UI will use
this").

Everything here was checked against the code and `output/` on 8 Oct 2026.

### Handing a phase to a model

For planning:

```text
Read TRACKING_SCANNED_ROWS.md and BACKEND_IMPLEMENTATION_PHASES.md.
Start with "The overall picture" and "Rules for every phase".
Plan Phase N only. First check that the phase's "Context for planning"
still matches the code, and say plainly where it doesn't. List every
decision from the phase's "Decisions to settle" that you need from me,
each with your recommendation. Then write a step-by-step plan naming the
files you'll change and what changes in each.
```

For building:

```text
Carry out this plan for Phase N: <plan>.
Follow "Rules for every phase" in BACKEND_IMPLEMENTATION_PHASES.md.
If anything in the plan doesn't match the code, stop and ask.
```

---

## Decisions already made

1. **Rows read straight from the Hugging Face Hub won't be traced** back to
   their Original row. You can still convert a Hub dataset ID; tracing just
   reports `not_supported` for those rows.
2. **Start fresh.** The tool team's existing conversions and scan results
   are deleted. Everything is converted again with today's code and scanned
   again (option A in Part 5 of the tracking doc).
3. **Text inside `metadata` is not scanned, and not measured, for now.**
   This is a known limit (see below).
4. **An unfinished conversion under a scanned folder is skipped and
   reported.** The scan carries on with the finished runs and says clearly
   what it skipped and why.
5. **Scanners can run in a chain.** A later scanner can read the data that's
   left after an earlier step removed rows.
6. **Clean data is made by removing whole rows.** Nothing inside a row is
   ever changed.
7. **No UI work in these phases.**

Decisions that are still open are listed in each phase and collected at the
end.

---

## The overall picture

### The chain

```text
Original data (never changed)
  │  conversion (prepare_rows.py)
  ▼
Prepared runs ─────────────► scanner results, e.g. duplicates
  │                                 │
  │  filter step ◄──────────────────┘
  ▼
Filter runs, step 1 ───────► scanner results, e.g. NER, toxicity, regex PII
  │                                 │
  │  filter step ◄──────────────────┘
  ▼
Filter runs, step 2  =  the clean data
```

- **Scanners only find things.** Only the filter step removes rows.
- **Nothing is changed in place.** Every step writes a new folder, and the
  folder it read stays exactly as it was.
- **The order is a choice made per chain.** Any scanner can read Prepared
  runs or filter runs. Several scanners can read the same runs side by side,
  and one filter step can use all of their results at once.

### What identifies a row

At any step, a row is identified by four things: the run folder, the file
inside it, the line number, and the line fingerprint. The fingerprint comes
from `line_fingerprint()` in `modelsafety/contract/fingerprints.py`.

- A **finding** names the file it was found in, the line, and that line's
  fingerprint (after Phase 1, for all four scanners).
- A **filter run's line map** says which line of the run before it (its
  parent) each kept line was. The kept line is a byte-for-byte copy, so its
  fingerprint is the same as the parent line's.
- A **Prepared row's origin note** (`metadata._origin`) says where its
  Original row is.

So any finding, at any step, can be walked back: from the finding to its
line, through each line map to the parent line, to the Prepared line, then
through the origin note to the Original row. Each hop is checked by comparing
fingerprints. And because rows are never changed, every row still carries
its origin note, so you can jump straight to the Original row if you don't
need the steps in between.

**The Prepared row is a row's lasting identity.** Per-row status (Phase 7)
and the UI key everything on the Prepared run, file, line and fingerprint.

### The rule every step follows

Every step that writes data or results keeps the same books that conversion
keeps today:

1. It records exactly what it read: each file, with its fingerprint and row
   count.
2. It accounts for every row. For a filter step, rows read = rows kept + rows
   removed. For a scanner, rows read = rows with text + rows with no text.
3. It saves what it set aside, with the reason.
4. It writes `run_identity.json`, so one folder can't hold two different
   jobs.
5. It writes `_SUCCESS` only when every count adds up. Otherwise it fails
   loudly and names the file, line or count that's wrong.

The one allowed exception is skipping an unfinished run (decision 4), and
even that is always reported.

### The three kinds of folder

| Folder | Written by | Holds |
|---|---|---|
| Prepared run | `prepare_rows.py` (exists) | data files, `summary.json`, `skipped_rows.ndjson`, `run_identity.json`, `_SUCCESS` |
| Scanner results | each scanner (exists; Phase 1 completes them) | findings, files read, counts per file, `run_identity.json`, finished markers |
| Filter run | the filter step (new, Phase 4) | kept data files, removed rows, line map, `summary.json`, `run_identity.json`, `_SUCCESS` |

A filter run's `summary.json` uses the same `output.files_written` shape as
a Prepared run's. That way the scanners, the coverage report and the run
finder can treat both kinds the same.

### How the UI will use this (later)

The UI doc will be built on top of these phases. So the backend must:

- keep everything in files under `output/`, as JSON or NDJSON, with no
  database. Each phase that adds a file writes down its fields;
- put its logic in Python functions inside `modelsafety/`, not only in
  command-line scripts. The validator backend can then call the same code the
  batch tools use, the way it already calls `prepare_rows.py` through
  `validator/backend/reader_imports.py`;
- make it possible to read one row, or one page of rows, without reading a
  whole dataset. The tool team data is over a million rows and several GB;
- never need the UI to write anything, except when it starts a job.

What the UI will show, and which phase provides it:

| UI view | Comes from |
|---|---|
| The chain, with row counts at each step | Phases 2, 3, 4 |
| Whether every scan finished and read everything | Phase 3 (its JSON report) |
| One row with every scanner's findings highlighted | Phases 1 and 7 |
| A duplicate next to the row it duplicates | Phase 7 gives both locations; the UI works out the shared text when it shows them |
| Removed rows and why | Phase 4 |
| "Show original" from any finding | Phase 6 |
| Download the clean data | The last filter run's kept files (Phase 4) |

### Known limits we accept

- Scanners read only `text`, or the `role` and `content` of each message.
  Text inside `metadata`, `tool_calls`, `name`, and any part of `content`
  that isn't text is not scanned. "Clean" means clean as far as the scanners
  can see.
- Rows read straight from the Hub can't be looked up (decision 1).
- Duplicates are found by how similar whole rows are (MinHash). The scanner
  doesn't record which part of a row matched.

---

## Rules for every phase

These are for the planning and building models.

1. **Scope.** Do only the phase you're given. No UI. Don't write tests
   unless the user asks for them. Don't add features the phase doesn't need.
2. **Check before you trust.** This doc describes the code as of 8 Oct 2026.
   Confirm that the phase's "Context for planning" still holds, and say
   plainly where it doesn't.
3. **Safety.**
   - Never change or delete Original data.
   - Never delete or overwrite anything under `output/` without first listing
     the exact paths and their sizes and getting the user's explicit OK.
   - Ask before submitting any SLURM job.
   - Check GPU scanners in pilot mode, on a few files only.
4. **Reuse the shared contract code** in `modelsafety/contract/`:
   - `fingerprints.py`: `line_fingerprint`, `file_sha256`, `input_block`,
     `compare_files`;
   - `origin.py`: `check_line`, `check_first_line`;
   - `run_identity.py`: `make_identity`, `check_run_identity`,
     `describe_input_files`;
   - `textract.py`.

   Never write a second fingerprint recipe.
5. **Add, don't break.** Other code reads scanner outputs:
   - the medpsy pipeline: `launchers/slurm/submit_medpsy_quality_filter.sh`,
     `launchers/slurm/submit_privacy_filter.sh` and
     `modelsafety/decisions/quality_filter_finalize.py`;
   - the regurgitation probe: `scripts/tool_team_regurgitation.sh`;
   - the comparison scripts.

   Add new fields and files. Don't rename or remove existing ones unless the
   phase says so. List every reader you checked.
6. **Fail loudly.** When counts don't add up, exit with a non-zero code and
   don't write `_SUCCESS`. Never skip a bad line with only a warning.
7. **One definition.** Logic that a scanner, a batch tool and the validator
   all need goes in `modelsafety/` once, and everyone imports it.
8. **Code style.** Match the surrounding code. Write comments only for
   constraints the code itself can't show.
9. **When unsure, ask.** If you're less than 90% sure something is right,
   say so and ask.
10. **Keep the record current.** When a phase closes a gap described in
    `TRACKING_SCANNED_ROWS.md`, update that part of the doc so the next
    phase's planner sees the current state.

---

## The phases at a glance

| Phase | In short | Size |
|---|---|---|
| 1 | Every scanner writes a complete record | Small, for each of 4 scanners |
| 2 | Scanners read only finished runs, and report what they skipped | Medium |
| 3 | One coverage report that checks every scan | Medium |
| 4 | The filter step: remove rows and keep the books | Medium to large |
| 5 | A pilot chain on a few files, end to end | Small (mostly running jobs) |
| 6 | Trace tool: any finding back to its Original row | Medium |
| 7 | Per-row status across the chain | Large |
| 8 | Full run: start fresh with all the tool team data | Running jobs, GPU time |

**Why this order:**

- Phases 1 to 4 change what the tools write, so they come before any scan
  whose results we keep.
- Phase 5 proves the whole chain on a small sample before we spend GPU time.
- Phases 6 and 7 only read results, so they're built and checked against the
  pilot. If they find a missing field, it gets fixed before the big run.
- Phase 8 runs everything for real, once.

---

## Phase 1: Every scanner writes a complete record

### In plain words

Each scanner already writes down what it found. This phase makes each one
also write down what it read, in the same way: for each file, how many rows,
how many had text, how many had none, and how many findings. Toxicity's
second step must prove it judged everything the first step flagged. NER and
duplicates findings get the line fingerprint that regex PII and toxicity
already record, so we can always check that a finding still points at the
row the scanner read. Every scanner also marks itself finished only when it
really did finish.

### What we'll have after this phase

- For every file a scanner read, four numbers in its results: rows read,
  rows with text, rows with no text, findings. Rows read always equals rows
  with text plus rows with no text.
- Toxicity stage 2 stops on a line it can't read. It fails unless its
  verdicts plus errors plus unknowns equal the number stage 1 flagged.
- Every NER and duplicates finding carries a line fingerprint.
- No scanner or stage writes its finished marker unless it finished.

### How we'll do it

**1a. Counts for each file, in all four scanners**

- **Regex PII, stage 1** (`pii_scanner_fast.py`). It splits files into
  chunks of lines and processes them in parallel. `process_chunk()` returns
  the number of lines it processed, and `main()` adds those up into one
  `total_lines` for `metrics.txt`. Add the counts up for each file instead,
  along with each file's match count. A row "has text" when the text the
  scanner builds for matching isn't empty.
- **Toxicity, stage 1** (`toxicity/run.py`). `iter_candidates()` yields one
  item per message, and a row with no text yields nothing, so
  `count_candidates()` never sees it. Count rows with no text too, in each
  file's entry of `detoxify_summary.json`.
- **Duplicates** (`dedup/run.py`). `ScanStats` has only totals
  (`rows_total`, `rows_hashed`, `rows_without_text`). Add counts for each
  file to `summary.json`, in both the global and the cross (reference) modes.
- **NER** (`pii/model/scanner.py`). Each work unit's `summary.json` already
  has rows and messages, and the aggregate step adds them up per file. Add
  rows with no text (rows for which `iter_record_texts()` in `common.py`
  yields nothing) and findings.

Use the same new field names in all four scanners, and keep the fields that
already exist.

What counts as a finding, per scanner:

- regex PII: stage 1 matches;
- NER: spans in `all_spans.jsonl`;
- toxicity: flagged messages;
- duplicates: rows marked as duplicates.

**1b. Check toxicity's second stage** (`toxicity/llm_check_guard.py` and
`launchers/slurm/run_toxicity_scan_array.sbatch`)

- `iter_jsonl()` prints a warning and skips any line it can't read. Make it
  stop and name the file and line.
- Stage 2 writes `guard_results/summary.json` with `total_records`,
  `safety`, `errors` and `unknown`. Compare these with `messages_flagged` in
  stage 1's `detoxify_summary.json`, and fail if the numbers don't match.
- Today the job script touches `_SUCCESS` after stage 2 no matter what
  happened. Make it do so only after the check passes.
- The check must also handle two special cases in the job script. First, an
  empty `flagged.jsonl`, where the script writes an empty summary itself.
  Second, `TOX_REUSE_STAGE1=1`, where an earlier `flagged.jsonl` is reused
  and the check must compare against that run's stage 1 summary.

**1c. Line fingerprints in NER and duplicates findings**

- **NER.** `iter_unit_rows()` in `pii/model/common.py` reads each raw line
  but passes on only the decoded `whole_line`. Work out
  `line_fingerprint(raw_line)` there, and add it to every record in
  `all_spans.jsonl` and every row of `llm_candidates.csv`.
- **Duplicates.** `Candidate` in `dedup/run.py` holds `source_file` and
  `source_line`. Add the fingerprint, and write it:
  - in `dedup_results.jsonl`, for the row and for its `duplicate_of`;
  - in `duplicate_pairs.jsonl`, for both sides.
- **Field name.** Toxicity already calls this field `source_sha256`. Use the
  same name.

**1d. Finished markers**

- Regex PII writes no `_SUCCESS` at any stage. Add one per stage, written
  last.
- Toxicity stage 1 can write `detoxify_summary.json` even when it stops
  partway. Make sure a partial summary can't be mistaken for a finished one.
- NER (`_SUCCESS` for each unit and for the aggregate) and duplicates
  (`_SUCCESS`): check that each marker is written last, and leave them alone
  if it is.

### Context for planning

Read first: the tracking doc's Part 2, Part 3, and Part 6 items 1, 2 and 6.

| Scanner | Code | Job scripts and wrapper |
|---|---|---|
| Regex PII | `modelsafety/data_screening/pii/regex/`: `pii_scanner_fast.py` (stage 1), `pii_validator.py` (stage 2), `pii_extractor.py` (stage 3), `pii_output.py` (folder layout, `write_stage1_metrics()`) | `launchers/slurm/run_pii_scan.sbatch`, `run_pii_scan_and_extract.sbatch`, `run_pii_scan_array.sbatch`; `scripts/tool_team_data.sh` |
| NER PII | `modelsafety/data_screening/pii/model/`: `scanner.py`, `common.py`, `build_work_manifest.py`, `llm_validate.py`, `policy.py` | `launchers/slurm/run_privacy_filter_*.sbatch`; `scripts/tool_team_ner_scan.sh` |
| Toxicity | `modelsafety/data_screening/toxicity/`: `run.py` (stage 1), `llm_check_guard.py` (stage 2), `guard_report.py`; `modelsafety/contract/manifest.py` | `launchers/slurm/run_toxicity_scan_array.sbatch`; `scripts/tool_team_toxicity_scan.sh` |
| Duplicates | `modelsafety/data_screening/dedup/`: `run.py`, `postprocess_matches.py`, `summarize_cross_matches.py` | `launchers/slurm/run_dedup_scan.sbatch`; `scripts/tool_team_dedup_scan.sh` |

Readers that must keep working:

- `modelsafety/decisions/quality_filter_finalize.py` (medpsy) reads regex
  PII's `input_files.json` and the toxicity outputs;
- `scripts/tool_team_regurgitation.sh` reads regex PII's stage 3 output;
- `pii/model/compare_regex.py`, `compare_models.py` and `llm_validate.py`
  read the NER outputs;
- `modelsafety/results/coverage.py` reads all the summaries;
- each wrapper's `report` stage.

### Limits

- Don't change the conversion.
- Don't change how scanners choose files (that's Phase 2).
- Don't change which text a scanner reads.
- Change the coverage report only where these changes would otherwise break
  it.

### Decisions to settle in planning

- **Where regex PII's per-file counts go:** inside
  `stage1_scan/input_files.json`, next to each file's fingerprint, or in a
  new file. `check_scan_input()` in `quality_filter_finalize.py` and
  `coverage.py` seem to read only `path` and `sha256` from each entry, so
  extra keys look safe, but confirm that.
- **The new field names,** shared by all four scanners.

### Done when

- On a pilot of two or three Prepared files, every scanner shows the four
  numbers for each file. Rows with text plus rows with no text equals rows
  read, and rows read equals that file's `rows` in the conversion's
  `output.files_written`.
- Toxicity stage 2, run on a copy of a `flagged.jsonl` with one broken line
  (in a scratch folder), fails and names that line. A normal run writes
  `_SUCCESS`, and its counts match stage 1's.
- Every record in NER's `all_spans.jsonl` and in duplicates'
  `dedup_results.jsonl` has `source_sha256`. For a few of them, working out
  the fingerprint of that Prepared line again gives the same value.
- The medpsy, regurgitation and comparison readers still read the new
  outputs.

---

## Phase 2: Scanners read only finished runs

### In plain words

Today a scanner reads every `.jsonl` file under the folder you give it, and
that includes a conversion that failed or is still writing. After this
phase, scanners take their file list from the runs themselves. They read
only runs that finished, and only the files those runs say they wrote.
Anything else under the folder is skipped and listed in a report next to the
scan results. That covers an unfinished run, a run whose checks failed, and
a stray `.jsonl` that doesn't belong to any run.

### What we'll have after this phase

- One shared run finder in `modelsafety/`. Given a folder, it lists every run
  under it at any depth, with the run's status and the files it wrote (path,
  rows and fingerprint). The validator's runs list uses the same code.
- All four scanners choose their files through it. Each one writes a report
  of what it skipped and why, and prints a short summary of that report.
- While reading, each scanner checks that every file's fingerprint matches
  what its run says it wrote.
- The old behaviour (read every `.jsonl` under the folder) still exists for
  the medpsy pipeline, but only behind an explicit option. The results record
  which behaviour was used.

### How we'll do it

1. **Move the run detection out of `validator/backend/runs.py`** and into
   `modelsafety/contract/`, and make the validator import it from there. That
   means `_run_entry()`, `list_runs()`, and the status logic built on
   `progress.json`, `summary.json`'s `checks.passed`, and `_SUCCESS`. Keep
   today's status words: `checks_passed`, `checks_failed`, `in_progress`,
   `may_have_stopped`, `failed`, `no_summary`.

   `list_runs()` stops looking deeper once it finds a run, and gives up
   after `MAX_RUN_FOLDERS` folders. For scanning, giving up must be an error,
   never a silently short list.
2. **Add one function** that takes a scan folder and returns two lists:
   - the files to read: each file's path relative to the scan folder, its
     fingerprint and its expected row count;
   - what was skipped: each run's path, status and a plain reason, plus any
     stray files.
3. **Allow more than one run type.** Today there's only `prepare_rows.py`.
   Phase 4 adds filter runs, and adding a type should mean adding one entry.
4. **Make each scanner use it.**

   | Scanner | Where it chooses files today |
   |---|---|
   | Regex PII | `discover_jsonl_files()` in `pii_scanner_fast.py` (it already accepts a file list) |
   | NER | `build_work_manifest.py` |
   | Toxicity | `discover_jsonl()` in `modelsafety/contract/manifest.py`, called by the manifest step in `tool_team_toxicity_scan.sh` and by medpsy |
   | Duplicates | The file selection in `dedup/run.py`, around its `rglob("*.jsonl")` |

   The simplest approach may be for the run finder to write a file list
   that each scanner's existing "list of files" input reads. The planner
   decides.
5. **Keep a plain-folder mode for medpsy.** The medpsy pipeline scans
   `output/medpsy2/baichuan_cleaning/filtered`, which isn't a run folder.
   Keep today's behaviour behind an explicit option there. The tool team
   wrappers default to reading runs.
6. **Fail if nothing is left to scan** because every run was skipped.

### Context for planning

- The tracking doc: Part 2 ("How they choose files"), Part 3 (the gap about
  folder depth), and Part 6 item 4.
- `validator/backend/runs.py`: the runs list, `open_run()` and
  `find_run_above()`. Also the routes in `validator/backend/app.py` that call
  them.
- What a Prepared run holds:
  - `summary.json` with `tool: "prepare_rows.py"`, `checks.passed`, and
    `output.files_written` (each entry has a path relative to the run folder,
    plus `rows`, `bytes` and `sha256`);
  - `run_identity.json` with `tool: "prepare_rows"`;
  - `_SUCCESS` when finished, and `progress.json` while it runs.
- Layouts:
  - `tool_team_data.sh` writes runs two folders deep, for example
    `<root>/rl_mix/train/`;
  - the UI writes runs three folders deep under `output/prepared`, for
    example `tools_team/rl_mixes/rl_mix_v12_ifmt_0903_train/`.

  The run finder must handle any depth.
- Each scanner records file paths in its own way, some relative and some
  absolute. Check each one.

### Limits

- Don't change what scanners do with a file once it's chosen, apart from the
  fingerprint check.
- Don't change the coverage report yet (that's Phase 3).

### Decisions to settle in planning

- The name and format of the skipped-runs report.
- The name of the plain-folder option.
- **A stray `.jsonl` outside any run:** skip it and report it (recommended,
  the same as an unfinished run), or fail the scan.

### Done when

- Point each scanner, in pilot mode, at a scratch folder that holds one
  finished run, a copy of a run with `_SUCCESS` removed, and a stray
  `.jsonl`. It reads only the finished run's files, and its report lists the
  other two with their reasons.
- The validator's runs list shows the same runs and statuses as before.
- In plain-folder mode, the medpsy wrappers select exactly the same files as
  before. Compare the file lists; no full scan is needed.

---

## Phase 3: One coverage report that checks every scan

### In plain words

Phase 1 makes every scanner write a complete record. This phase builds the
one report that reads all of those records. For each dataset and overall,
it says in plain words whether each scan read exactly the right files, read
every row, and finished. It also writes the same answers to a JSON file the
UI can read later.

### What we'll have after this phase

`modelsafety/results/coverage.py` that:

- finds runs at any depth with the Phase 2 run finder, so it works on
  Prepared runs now and on filter runs later;
- covers all four scanners, adding duplicates;
- checks every file: the scanner's rows read equal the run's rows, and rows
  with text plus rows with no text equal rows read;
- reads the finished markers, and says "this slice, unit or stage didn't
  finish" instead of "missing files";
- checks that toxicity stage 1's flagged count equals stage 2's verdicts
  plus errors plus unknowns;
- shows which runs each scanner skipped (from the Phase 2 reports);
- prints a table and writes a JSON report with the same verdicts;
- exits with a non-zero code if any check fails.

### How we'll do it

- **Start from today's `coverage.py`.** It has `load_conversion_summaries`,
  `load_ner_summary`, `load_toxicity_summaries`, `load_regex_pii_input`,
  `check_scan_files` and `build_rows`.
  - Replace its fixed `*/*/summary.json` search with the run finder.
  - Add a loader for duplicates.
  - Add the checks for each file.
- **Make every check a function you can call.** The filter step (Phase 4)
  uses them to refuse scan results that don't match, and the validator can
  show them later.
- **Arguments:** the folder to check, plus each scanner's results folder.
  Any of them can be missing, and a missing one is reported as "not run",
  never as passed.

### Context for planning

- The tracking doc: Part 3 (all of it), Part 5 (how the report behaved on
  the existing results), and Part 6 item 3.
- What Phases 1 and 2 add.
- Where each scanner keeps its results:

  | Scanner | Results layout |
  |---|---|
  | Regex PII | `stage1_scan/`, `stage2_validated/`, `stage3_extract/` |
  | NER | `summary.json`, plus `units/<id>/_SUCCESS` for each piece of work |
  | Toxicity | `scans/toxicity/shards/shard_*/`, each with `detoxify_summary.json`, `_DETOXIFY_SUCCESS`, `guard_results/summary.json` and `_SUCCESS` |
  | Duplicates | `summary.json`, `_SUCCESS` |

### Limits

- Read-only. The report never changes or deletes anything it checks.
- It doesn't scan anything itself.

### Decisions to settle in planning

- **The shape of the JSON report.** Define it once, because the UI doc will
  rely on it.
- **A scanner that wasn't run:** fail the overall verdict, or report it as
  "not run". Recommended: report "not run" clearly, give the overall verdict
  only for the scanners that were run, and list the ones that weren't.

### Done when

- On the pilot from Phases 1 and 2, every check passes.
- On scratch copies that each have one problem, the report names that
  problem in plain words. The problems to try: a file missing from a
  scanner's list, a wrong row count, a missing `_SUCCESS`, and a stage 2
  count that's off.

---

## Phase 4: The filter step

### In plain words

This is a new tool. It takes finished runs, plus one or more scanners'
results on those runs, and writes new runs with the flagged rows removed.

- Kept rows are copied byte for byte.
- Removed rows are saved together with the reason.
- A line map records where each kept line came from.

To every other tool, its folder looks like a Prepared run. So the next
scanner in a chain can read it, the coverage report can check it, and the
trace tool can walk back through it.

### What we'll have after this phase

- A filter tool, both as a command and as a Python function. It can run
  directly, or as a CPU-only SLURM job the way conversion does.
- For each run under the input folder, one filter run at the same relative
  path under the output folder. Each filter run holds:
  - **kept data files** with the same names and layout as the parent run.
    Recommended: write a file even when nothing in it was kept, so every
    file is accounted for;
  - **`removed_rows.ndjson`**, one entry per removed row: the parent file,
    line, byte offset and line fingerprint, the reasons (which scanner
    results, which rule, which findings), and the whole line;
  - **a line map** giving, for every kept line, the parent file, line, byte
    offset and fingerprint;
  - **`summary.json`**, in a Prepared run's shape wherever the two overlap
    (`tool`, `created_at`, `output.files_written` with path, rows, bytes and
    sha256, and `checks.passed` and `checks.failures`). It also holds the
    parent run, the scanner results used, the rules, the counts for each file
    (in, kept, removed) and the number removed for each reason;
  - **`run_identity.json`**: the parent run and its file fingerprints, the
    scanner results, and the rules;
  - **`_SUCCESS`**, only if every check passed.
- The run finder (Phase 2) and the coverage report (Phase 3) recognise
  filter runs.

### How we'll do it

1. **Refuse to start** unless:
   - every parent run is finished;
   - every scanner result passes the Phase 3 checks for exactly those parent
     runs;
   - every finding's line fingerprint matches its line in the parent.
2. **Rules.** There's one rule per scanner, chosen by the user and recorded
   in `run_identity.json` (see the decisions below). A row is removed if any
   rule hits it, and every reason is recorded.
3. **Read each parent file once, in order.** Each removed line goes to
   `removed_rows.ndjson`. Each kept line's raw bytes are copied, and it gets
   a line map entry.
4. **Check before writing `_SUCCESS`:**
   - for each file, rows in = kept + removed;
   - every kept line's fingerprint equals its parent line's;
   - the number of entries in `removed_rows.ndjson` equals the number
     removed;
   - `output.files_written` matches what's on disk.
5. **Register the filter run type** in the run finder. Make sure scanners
   never read `removed_rows.ndjson` or the line map. Ending their names in
   `.ndjson` keeps scanners from picking them up. `check_first_line()` in
   `origin.py` already refuses files that look like skipped rows; check
   whether removed-row entries need the same protection.
6. **Duplicates' own `filtered/` and `filtered_out/` copies** (made by
   `write_partition()` in `dedup/run.py`) stay as they are, but chains don't
   use them.

Reuse ideas from `modelsafety/decisions/quality_filter_finalize.py`. For
medpsy, it already writes clean and rejected copies byte for byte, checks
input = clean + rejected for each file, and writes atomically
(`atomic_outputs()`). Share its code where it fits, but don't change how
that script behaves, because medpsy relies on it.

### Context for planning

- This doc's "The overall picture".
- The tracking doc's Part 1 (what a Prepared run holds) and Part 4.
- How `modelsafety/readers/prepare_rows.py` writes `summary.json` and its
  identity, and `modelsafety/contract/run_identity.py`.
- `modelsafety/decisions/quality_filter_finalize.py`.
- The finding formats after Phase 1:
  - regex PII: stage 2 and stage 3;
  - NER: `all_spans.jsonl` and `llm_candidates.csv`, plus `llm_validate.py`'s
    output if the NER rule uses confirmed results;
  - toxicity: `guard_results/all.jsonl`;
  - duplicates: `dedup_results.jsonl`.

### Limits

- Never change a row's bytes, and never change the parent run.
- Remove whole rows only.
- No scanning and no UI.

### Decisions settled in Phase 4

Every rule is a setting of the filter step (`FilterRules` in
`modelsafety/filter/rules.py`, and a flag on the command), recorded in the
filter run's `run_identity.json` and `summary.json`, so the UI can set them
later. The defaults:

- **Toxicity rule:** remove Unsafe and Controversial (configurable). Guard
  errors, and verdicts the guard didn't give, count as unsafe (`remove`).
  `--toxicity-guard-errors refuse` stops the filter while any exist, so they
  can be re-run first. Re-running is a scan job, outside this phase.
- **NER rule:** spans the NER policy marked `candidate_eligible` in
  `all_spans.jsonl` (the same set as `llm_candidates.csv`), with an optional
  list of labels to ignore. The LLM step isn't used: its output has no line
  fingerprint, `_SUCCESS` or run identity, and the tool team wrapper doesn't
  run it.
- **Regex PII rule:** stage 3 (`pii_extract.csv`), every category counts,
  with an optional list of categories to ignore. Stage 3 holds the same
  matches as stage 2, and has also re-checked each line and value. Today's
  10 categories include no zip codes or postcodes, so the medpsy exclusion
  doesn't apply.
- **Duplicates rule:** remove every row marked as a duplicate, which keeps
  the first copy. Cross mode is handled the same way (every matched candidate
  row is removed). The allowed modes are configurable. One duplicates
  results folder per filter step.
- **Unfinished runs:** the filter refuses to start if anything under the
  input folder was skipped, so the clean data can't quietly miss a dataset.
- **`removed_rows.ndjson`:** holds the whole line.
- **The line map:** one per data file, under `line_map/` with the same layout,
  line k of the map describing kept line k.

### What Phase 4 built

- `modelsafety/filter/filter_rows.py` (command and `filter_runs()`),
  `modelsafety/filter/rules.py`, and `launchers/slurm/filter_rows.sbatch`
  (CPU only). There is no wrapper script yet; Phase 5 writes the runbook.
- `run_finder.py` lists filter runs (a second entry in `RUN_TYPES`), and
  `origin.py` refuses lines that start `{"_parent"` (removed rows and line
  maps), as it does for skipped rows. A conversion of Original data that
  starts with that key would now be refused too; none is known.
- `coverage.py` needed no logic change.
- The validator's runs list shows filter runs, but its "open run" opens only
  Prepared runs. Keep filter output outside `output/prepared` until the UI
  doc decides what to do.
- How it was checked, on a scratch pilot of two small runs made from
  synthetic Original rows (199 rows, in `/tmp`), with the real duplicates scan: counts add up, kept lines are byte-identical to
  the parent lines their map names, removed rows carry the right reasons, the
  run finder and the duplicates scan and coverage read the filter output (also
  a file with no kept rows), a filter step reads a filter run, and every
  refusal listed in "Done when" works. The regex PII, NER and toxicity results
  in that pilot were hand-made in each scanner's format (and passed the real
  coverage checks), because those scans need a package that isn't installed
  or GPU jobs. Phase 5 runs the real ones.

### Done when

- On the pilot, filter runs are written and the counts for every file add
  up.
- Spot checks pass: a few kept lines are byte-identical to the parent lines
  their line map points to, and a few removed rows carry the right reasons.
- The run finder lists the filter runs as finished. A scanner pointed at the
  filter output reads only its kept data files, and the coverage report can
  check a scan made on it.
- Given scanner results from a different folder, the filter tool refuses to
  start.

---

## Phase 5: A pilot chain, end to end

### In plain words

We run the whole chain on a small sample, start to finish, to prove every
piece fits before spending GPU time on all the data.

### What we'll have after this phase

A small but complete chain (Prepared runs, then scans, a filter, more scans
and another filter) in a pilot folder, with every step passing the coverage
report. Phases 6 and 7 are built and checked against it. We'll also have the
exact commands written down, so Phase 8 can repeat them.

### How we'll do it

1. **Make a small sample of complete runs.** The filter accepts scanner
   results only if they pass the coverage report for exactly the runs it
   filters, so every scanner has to read every file of every run. That rules
   out the wrappers' pilot options, which read only a few files. So the
   pilot is four small Prepared runs, each scanned whole, under
   `output/pilot_chain/prepared/`:
   - the first 2,000 rows of the MOPD and the RL train sets (`--max-rows
     2000`, two files each);
   - the whole `smoltalk2_table_gpt` SFT shard (919 rows);
   - a hand-written run of 19 rows (`output/pilot_chain/original/synthetic_rows.jsonl`)
     that has what the tool team data lacks: plain `text` rows, a row with an
     empty `text`, a row with an empty `messages`, an exact duplicate, and an
     email address.
2. **Chain order: duplicates first** (see "Decisions settled in Phase 5").
3. **Run each step through the wrappers** where one fits, with every path
   absolute. Conversion and the filter have no wrapper that fits, so they
   use their job scripts. The commands are in "Running the chain" below.
4. **Check as you go:** run the coverage report after each scan, and check
   each filter run's counts.
5. **Write down every problem,** with the phase it belongs to (see "What
   Phase 5 ran").
6. **Add a "Running the chain" section** to this doc with the exact commands.

### Context for planning

*Checked against the code on 9 Oct 2026. This replaces what the first draft
of this phase said about the wrappers.*

- The wrappers:
  - `scripts/tool_team_data.sh` (convert, scan, extract);
  - `scripts/tool_team_ner_scan.sh`;
  - `scripts/tool_team_toxicity_scan.sh`;
  - `scripts/tool_team_dedup_scan.sh`.
- **They don't share flag names.** The folder to scan is `--input-root` for
  NER, toxicity and duplicates, but `--output-root` for `tool_team_data.sh`.
  The results folder is `--pii-run-dir` (regex PII), `--run-root` (NER) or
  `--run-dir` (toxicity and duplicates). Only toxicity and duplicates have a
  `report` stage. `tool_team_data.sh` has no `report` and no pilot option
  (its `summary` stage only totals the conversion logs), and NER has no
  `report`.
- **Their pilot options read only some files,** which fails coverage:
  NER's `pilot` reads 1 file and 1 work unit unless
  `PRIVACY_FILTER_MAX_FILES` and `PRIVACY_FILTER_MAX_UNITS=0` are set (an
  empty `PRIVACY_FILTER_MAX_FILES` still means 1); toxicity's
  `--pilot-files N` and duplicates' `pilot` (50 files) pick a subset; regex
  PII has no pilot option and always reads every file.
- **No wrapper makes a small conversion.** `tool_team_data.sh convert`
  converts all 65 datasets and has no `--max-rows`. Use
  `launchers/slurm/prepare_rows.sbatch`, the job script the wrapper and the
  validator both use.
- **The filter has no wrapper.** Use `launchers/slurm/filter_rows.sbatch`.
- The wrappers' default input is still `output/tool_team_converted`. Pass
  every path explicitly, and as absolute paths: the NER and regex PII jobs
  change folder before they use them.
- The tool team data has only `messages` rows, and every row has text. That
  is why the pilot adds a hand-written run.

### Limits

- Ask before each SLURM submission, and keep GPU jobs pilot-sized.
- Delete nothing outside the pilot folder.

### Decisions settled in Phase 5

- **Chain order: duplicates first.** Prepared, then duplicates, then filter
  step 1, then regex PII, NER and toxicity side by side on step 1's output,
  then filter step 2. The GPU scans read 52 fewer rows in the pilot (about
  1%). In the old results, 283,638 of 1,143,300 rows (about 25%) were marked
  as duplicates, so the saving in the full run is much larger. Rows removed
  early are never seen by later scanners.
- **Sample:** the four runs above, 4,938 rows.
- **Location: `output/pilot_chain/`,** outside `output/prepared`, so a later
  scan of `output/prepared/tools_team` never picks up pilot runs.
- **Rules:** the Phase 4 defaults, unchanged.
- **Conversions use `--require messages`,** as `tool_team_data.sh` does. The
  hand-written run uses no `--require`, so its two empty rows are kept. The
  two existing runs under `output/prepared/tools_team/rl_mixes/` were made from
  the UI without `--require`, so they have a different run identity; Phase 8
  picks one.
- **The email in the hand-written run isn't `example.com`.** Regex PII's
  stage 2 rejects reserved domains (`example.com`, `example.net`,
  `example.org`, `.test` and others, in `validator/email.py`), so such a
  row would never be removed.

### Done when

- Every step has `_SUCCESS`.
- The coverage report passes for every scan in the chain.
- The counts add up from the first step to the last: Prepared rows = final
  kept rows + the rows removed at every step.

### What Phase 5 ran

Run on 9 Oct 2026, from the repo root, with every SLURM submission approved
first. Everything is under `output/pilot_chain/`. The commands are in
"Running the chain" below. No code was changed.

| Step | Result |
|---|---|
| Conversion (4 CPU jobs) | 4 runs, 6 files, 4,938 rows (2,000, 2,000, 919 and 19). Each job took 4 to 14 seconds and under 1 GB. Every run has `_SUCCESS`, and the run finder reads all four with nothing skipped. The hand-written run has 17 of its 19 rows with text, as intended. |
| Duplicates (CPU, 30 s) | 4,938 rows read, 4,936 compared, 2 without text, 52 marked as duplicates. Coverage passes. |
| Filter step 1 (CPU, 2 s) | 52 rows removed (3, 3, 45 and 1), 4,886 kept. All four filter runs have `_SUCCESS`. |
| Regex PII, on step 1's output (CPU, 50 s) | 4,552 matches in stage 1, 637 kept by stage 2 (3,915 filtered out as not valid). All three stages have `_SUCCESS`. |
| NER, on step 1's output (1 GPU, 11 min) | 62,776 spans, 14,809 of them eligible under the NER policy. 6 of 6 work units done, about 31 messages a second, parity F1 1.0. |
| Toxicity, on step 1's output (1 GPU, 6 min) | Detoxify flagged 7 messages. The guard judged all 7: 6 Safe, 1 Unsafe, no errors, none unknown. |
| Coverage, step 1 and step 2 | Passes for duplicates (on the Prepared runs), and for regex PII, NER and toxicity (on step 1's output: 6 files, 4,886 rows). The reports are in `output/pilot_chain/coverage/`. |
| Filter step 2 (CPU, 2 s) | 2,243 rows removed, 2,643 kept, all four filter runs with `_SUCCESS`. Reasons: NER 2,232 rows, regex PII 308, toxicity 1 (a row can have more than one reason, so these add up to more than 2,243). |

The counts, from the Prepared run to the clean data:

| Run | Prepared | Removed, step 1 | Removed, step 2 | Kept |
|---|---|---|---|---|
| `rl_mixes/mopd_mixv4_0907_train` | 2,000 | 3 | 653 | 1,344 |
| `rl_mixes/rl_mix_v12_ifmt_0903_train` | 2,000 | 3 | 970 | 1,027 |
| `sft_mix/smoltalk2_table_gpt` | 919 | 45 | 618 | 256 |
| `synthetic/rows` | 19 | 1 | 2 | 16 |
| Total | 4,938 | 52 | 2,243 | 2,643 |

Spot checks:

- All 2,643 clean rows were followed back through both line maps to their
  Prepared line. The bytes are identical and the fingerprint matches at every
  hop (the second script in "Running the chain").
- In the hand-written run, the exact copy (line 19) was removed in step 1 and
  the row it copies (line 12) was kept. The row with the email address
  (line 3) was removed in step 2, by both NER and regex PII. The Kyoto trip
  row (line 11) was removed by NER only. The two empty rows are still there:
  no scanner found anything in them, so none removed them. They are "not
  scanned", not "clean", and Phase 7 has to show them that way. *(Phase 7
  found this isn't quite right for line 17, `{"text": ""}`: toxicity scores an
  empty `text`, so it did read that row. Duplicates, regex PII and NER had no
  text in it. Line 18, `{"messages": []}`, has no text for any of the four.)*
- Neither GPU job needed a Hugging Face token. Both models were already in
  `cache/hf`. The NER job printed a warning that its requests to the Hub were
  unauthenticated, and carried on.

Done when: every step has `_SUCCESS`, both coverage runs pass, and the counts
add up for every run.

**Problems found, and where they belong**

| # | What | Belongs to | Status |
|---|---|---|---|
| 1 | The wrappers' pilot options read only a few files, so their results fail coverage and the filter refuses them. The wrappers also use different flag names. | This phase's plan | Fixed: the plan and "Running the chain" use complete small runs and the right flags. |
| 2 | The hand-written run's email was first planned as `example.com`, which regex PII's stage 2 rejects, so it could never be removed. | This phase's sample | Fixed: the run uses another domain. |
| 3 | `tool_team_dedup_scan.sh report` prints "Kept copy" and "Removed copy" paths even when the run was made with `--write-filtered 0`, so the folders don't exist. | Duplicates wrapper (Phase 1) | Open, cosmetic. Proposed fix: print those two lines only when the folders exist. |
| 4 | `guard_report.py` splits its counts into user and assistant columns only. The one Unsafe message was a `system` message, so the table shows "unsafe 1, user 0, assistant 0", and the safe row (6, with user 2 and assistant 1) doesn't add up either. | Toxicity (Phase 1) | Open, cosmetic. Proposed fix: add a column for other roles. |
| 5 | Right after the duplicates job finished, listing its results folder failed for a few seconds, although the job's log named it. The shared filesystem is slow to show new folders. | Not a bug | Gotcha: wait and list again. |
| 6 | The wrappers write SLURM logs under the repo's `logs/`, not under the results folder. | Not a bug | Gotcha. |

Two things the pilot shows that are for you to decide before Phase 8. They are
choices about the Phase 4 rules, not bugs:

- **The NER rule removes 46% of the rows** (2,232 of 4,886), against 6% for
  regex PII. Of the 14,809 eligible spans, 4,680 are `first_name` and 2,734
  are `last_name`, then `postcode`, `url`, `email`, `user_name` and others.
  Names in tool-call and roleplay data are common, so the clean data may be
  much smaller than expected. The rule can be narrowed with
  `--ner-ignore-label`.
- **The one toxicity removal was a roleplay system prompt** ("You are a
  cunning thief, skilled in stealth and pickpocketing, in a bustling
  city."), judged Unsafe. The filter removes the whole row, and the scanners
  read system messages (the duplicates wrapper alone leaves them out).

---

## Running the chain

The exact commands of the Phase 5 pilot, so Phase 8 can repeat them. Run
everything from the repo root. Ask before each SLURM submission, and give
every path in full.

```bash
REPO=/home/naresh/model-safety
P=$REPO/output/pilot_chain        # everything for the pilot goes here
cd $REPO
mkdir -p $P/_logs $P/coverage
```

**1. Convert.** One CPU job for each run. `--max-rows` is for the pilot only.
For the hand-written run, `$P/original/synthetic_rows.jsonl` holds the 19
rows (it is under `output/`, so git doesn't keep it).

```bash
RL=/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated
SFT=/home/shared/agentic_slm/data/sft/processed/qwen3_5/s1_2_0824/train_shards_keep

sbatch --partition=health --cpus-per-task=4 --mem=128G --time=01:00:00 --job-name=pilot_prepare_mopd \
  --output=$P/_logs/prepare_mopd.log --error=$P/_logs/prepare_mopd.log --export=ALL,PREPARE_REPO=$REPO \
  launchers/slurm/prepare_rows.sbatch --input-path $RL/mopd_mixv4_0907/train.parquet --input-type parquet \
  --require messages --map messages=prompt --max-rows 2000 \
  --output-dir $P/prepared/rl_mixes/mopd_mixv4_0907_train

sbatch --partition=health --cpus-per-task=4 --mem=128G --time=01:00:00 --job-name=pilot_prepare_rl \
  --output=$P/_logs/prepare_rl.log --error=$P/_logs/prepare_rl.log --export=ALL,PREPARE_REPO=$REPO \
  launchers/slurm/prepare_rows.sbatch --input-path $RL/rl_mix_v12_ifmt_0903/train.parquet --input-type parquet \
  --require messages --map messages=prompt --max-rows 2000 \
  --output-dir $P/prepared/rl_mixes/rl_mix_v12_ifmt_0903_train

sbatch --partition=health --cpus-per-task=2 --mem=32G --time=01:00:00 --job-name=pilot_prepare_sft \
  --output=$P/_logs/prepare_sft.log --error=$P/_logs/prepare_sft.log --export=ALL,PREPARE_REPO=$REPO \
  launchers/slurm/prepare_rows.sbatch --input-path $SFT/smoltalk2_table_gpt --input-type huggingface \
  --require messages --output-dir $P/prepared/sft_mix/smoltalk2_table_gpt

sbatch --partition=health --cpus-per-task=2 --mem=8G --time=00:30:00 --job-name=pilot_prepare_synth \
  --output=$P/_logs/prepare_synthetic.log --error=$P/_logs/prepare_synthetic.log --export=ALL,PREPARE_REPO=$REPO \
  launchers/slurm/prepare_rows.sbatch --input-path $P/original/synthetic_rows.jsonl --input-type jsonl \
  --map text=text --map messages=messages --output-dir $P/prepared/synthetic/rows
```

Then check that every run finished and nothing is skipped (read-only):

```bash
python3 modelsafety/contract/run_finder.py files --scan-root $P/prepared > /dev/null
```

**2. Step 1: duplicates, then the first filter.**

```bash
./scripts/tool_team_dedup_scan.sh full --input-root $P/prepared --run-dir $P/scans/step1_dedup \
  --cpus 4 --mem 16G --time 01:00:00 --write-filtered 0 --partition health
./scripts/tool_team_dedup_scan.sh report --run-dir $P/scans/step1_dedup     # when the job is done
python3 modelsafety/results/coverage.py --root $P/prepared --dedup-run-dir $P/scans/step1_dedup \
  --json-out $P/coverage/step1_dedup.json

sbatch --partition=health --output=$P/_logs/filter_step1_%j.log --error=$P/_logs/filter_step1_%j.log \
  --export=ALL,FILTER_REPO=$REPO launchers/slurm/filter_rows.sbatch \
  --input-root $P/prepared --output-root $P/filter_step1 --dedup-run-dir $P/scans/step1_dedup
```

**3. Step 2: three scans of `filter_step1`.** All three can run at once.

```bash
# Regex PII (CPU). This wrapper calls the folder it scans --output-root.
./scripts/tool_team_data.sh scan extract --output-root $P/filter_step1 \
  --pii-run-dir $P/scans/step2_regex_pii --cpus 8 --mem 16G --time 01:00:00 --workers 8 --partition health

# NER (1 GPU, pilot mode). The two variables make it read every file and every
# work unit; without them it reads 1 file and 1 unit.
PRIVACY_FILTER_MAX_FILES=100000 PRIVACY_FILTER_MAX_UNITS=0 ./scripts/tool_team_ner_scan.sh pilot \
  --input-root $P/filter_step1 --run-root $P/scans/step2_ner --partition health

# Toxicity (1 GPU, one shard, both passes). --pilot-files 0 means every file.
./scripts/tool_team_toxicity_scan.sh all --input-root $P/filter_step1 \
  --run-dir $P/scans/step2_toxicity --pilot-files 0 --shards 1 --concurrency 1 --time 06:00:00 --partition health
```

When all three are done (the jobs print their own status commands; the NER
run is finished when `$P/scans/step2_ner/_SUCCESS` exists):

```bash
python3 modelsafety/results/coverage.py --root $P/filter_step1 \
  --pii-run-dir $P/scans/step2_regex_pii --ner-run-dir $P/scans/step2_ner \
  --toxicity-run-dir $P/scans/step2_toxicity --json-out $P/coverage/step2.json
./scripts/tool_team_toxicity_scan.sh report --run-dir $P/scans/step2_toxicity
```

**4. Step 2: the second filter.**

```bash
sbatch --partition=health --output=$P/_logs/filter_step2_%j.log --error=$P/_logs/filter_step2_%j.log \
  --export=ALL,FILTER_REPO=$REPO launchers/slurm/filter_rows.sbatch \
  --input-root $P/filter_step1 --output-root $P/filter_step2 \
  --pii-run-dir $P/scans/step2_regex_pii --ner-run-dir $P/scans/step2_ner \
  --toxicity-run-dir $P/scans/step2_toxicity
```

`$P/filter_step2` is the clean data.

**5. Check the counts, end to end (read-only).** Save this as a file and run
`python3 <file> $P`. It exits with 0 only if every run's counts add up.

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from modelsafety.contract.run_finder import find_runs

P = Path(sys.argv[1]).resolve()


def runs_of(root):
    found = find_runs(root)
    assert not found["truncated"], f"{root} holds too many folders"
    return {run["path"]: run for run in found["runs"]}


def summary_of(run):
    return json.loads((Path(run["run_dir"]) / "summary.json").read_text())


def file_rows(summary):
    return {e["path"]: e["rows"] for e in summary["output"]["files_written"]}


runs = [runs_of(P / name) for name in ("prepared", "filter_step1", "filter_step2")]
assert set(runs[0]) == set(runs[1]) == set(runs[2]), "the three steps hold different runs"

failed = False
for path in sorted(runs[0]):
    prepared, step1, step2 = (summary_of(run[path]) for run in runs)
    problems = []
    if [run[path]["status"] for run in runs] != ["checks_passed"] * 3:
        problems.append("a run is not finished")
    for label, parent, child in (("step 1", prepared, step1), ("step 2", step1, step2)):
        if file_rows(parent) != {name: v["rows_in"] for name, v in child["per_file"].items()}:
            problems.append(f"{label} read different files or row counts than its parent wrote")
        if child["source"]["rows_read"] != parent["rows"]["written"]:
            problems.append(f"{label} read a different total than its parent wrote")
    removed = step1["rows"]["removed"] + step2["rows"]["removed"]
    if prepared["rows"]["written"] != step2["rows"]["written"] + removed:
        problems.append("Prepared rows != final kept + removed at both steps")
    failed |= bool(problems)
    print(path, prepared["rows"]["written"], "->", step2["rows"]["written"], "kept,",
          removed, "removed:", "; ".join(problems) or "OK")
sys.exit(1 if failed else 0)
```

Optional, and also read-only: follow every clean row back to its Prepared line
and check the bytes and the fingerprint at each hop.

```python
import json
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, ".")
from modelsafety.contract.fingerprints import line_fingerprint
from modelsafety.contract.run_finder import find_runs

P = Path(sys.argv[1]).resolve()
PREPARED, STEP1, STEP2 = P / "prepared", P / "filter_step1", P / "filter_step2"


@lru_cache(maxsize=None)
def lines(path):
    return tuple(path.read_bytes().splitlines(keepends=True))


@lru_cache(maxsize=None)
def parents(run_dir, name):
    map_file = run_dir / "line_map" / Path(name).with_suffix(".ndjson")
    return tuple(json.loads(line)["_parent"] for line in lines(map_file))


checked = 0
for run in find_runs(STEP2)["runs"]:
    rel = run["path"]
    summary = json.loads((STEP2 / rel / "summary.json").read_text())
    for entry in summary["output"]["files_written"]:
        name = entry["path"]
        kept, map2 = lines(STEP2 / rel / name), parents(STEP2 / rel, name)
        assert len(kept) == len(map2), f"{rel}/{name}: kept lines and map entries differ"
        for number, (line, hop2) in enumerate(zip(kept, map2), 1):
            middle = lines(STEP1 / rel / hop2["file"])[hop2["line"] - 1]
            hop1 = parents(STEP1 / rel, hop2["file"])[hop2["line"] - 1]
            first = lines(PREPARED / rel / hop1["file"])[hop1["line"] - 1]
            assert line == middle == first, f"{rel}/{name} line {number}: bytes differ"
            assert line_fingerprint(line) == hop2["source_sha256"] == hop1["source_sha256"], (
                f"{rel}/{name} line {number}: fingerprint differs"
            )
            checked += 1
print(f"{checked:,} clean rows traced to their Prepared line")
```

Also check the clean data is readable by the other tools:
`python3 modelsafety/contract/run_finder.py files --scan-root $P/filter_step2 > /dev/null`.

**6. Trace every finding and every removed row to its Original row**
(Phase 6; read-only on everything it reads). The output folder must be new, or
hold a trace made from these same inputs, and can't be inside any folder the
trace reads. It needs the readers' packages, so use their Python. The pilot
takes about 8 seconds on CPU, so there is no SLURM script:

```bash
modelsafety/readers/.venv/bin/python modelsafety/trace/trace_findings.py --output-dir $P/trace \
  --dedup-run-dir $P/scans/step1_dedup --pii-run-dir $P/scans/step2_regex_pii \
  --ner-run-dir $P/scans/step2_ner --toxicity-run-dir $P/scans/step2_toxicity \
  --filter-root $P/filter_step1 --filter-root $P/filter_step2
```

It exits with 0 only if every finding is `unchanged` (or `not_supported`), and
`$P/trace/summary.json` has the totals for each source and status.

**7. Write the status of every Prepared row** (Phase 7; read-only on everything
it reads). Give it the output folder of the last filter step; it finds the
earlier steps and the scanner results from the filter runs' own
`run_identity.json`. The output folder must be new, or hold a status made from
these same inputs, and can't be inside any folder the tool reads. It uses the
trace module's code, so it needs the readers' Python. The pilot takes about 4
seconds on CPU, so there is no SLURM script:

```bash
modelsafety/readers/.venv/bin/python modelsafety/trace/row_status.py \
  --filter-root $P/filter_step2 --output-dir $P/row_status
```

It exits with 0 only if every check passed (and then writes `_SUCCESS`).
`$P/row_status/summary.json` has the counts for each run, step and scanner. To
read one row, or a page, without reading a whole file (standard library only):

```bash
python3 modelsafety/trace/row_status_read.py show --status-dir $P/row_status \
  --run synthetic/rows --file chunk-0/data_000000.jsonl --line 3 [--count 50]
```

**What changes for Phase 8**

- **Conversion:** no `--max-rows`, and every dataset instead of the sample.
  Settle where the runs live and whether to pass `--require messages` (see
  Phase 8's decisions). The UI converted the two full train files with 16
  CPUs and 256G. A capped run still reads the whole parquet file first, so
  don't take the pilot's small memory as a guide for the full files.
- **Duplicates:** the same command, with its default resources (16 CPUs,
  128G, 24 hours). Decide `--write-filtered` again: chains don't use the
  copies it writes, and for the full data they come to about 8.7 GB.
- **NER:** `full` mode (8 GPUs on one node, up to 7 days), without
  `PRIVACY_FILTER_MAX_FILES` and `PRIVACY_FILTER_MAX_UNITS`. Full mode skips
  the parity check. The pilot ran about 31 messages a second on one GPU.
- **Toxicity:** `--shards 8 --concurrency 8` (the defaults) and the default
  48-hour limit, still with `--pilot-files 0`.
- **Regex PII:** the same command, with the wrapper's default resources (32
  CPUs, 64G, 6 hours).
- **The filter and the checks:** the same commands. Keep the filter output
  outside `output/prepared` (see Phase 4).
- **The trace:** the same command, but not on the login node: NER's output
  alone could be 14 to 19 GB, and the walk keeps one entry in memory for each
  distinct line (see "What Phase 6 ran"). Run it as a SLURM job, and ask first.
- **The row status:** the same command, also as a SLURM job (ask first),
  after the trace. It keeps every row and finding in memory, so give it
  generous memory (see "What Phase 7 ran" for what is and isn't measured).
- **Hugging Face:** no token was needed in the pilot, because both models
  were in `cache/hf`. A different machine or an empty cache may need one.

**What Phase 8 ran** (full size; `T=$REPO/output/prepared/tools_team`,
`C=$REPO/output/tool_team_chain`, partition `health`):

```bash
./scripts/tool_team_data.sh convert --output-root $T --partition health        # 65 CPU jobs
./scripts/tool_team_dedup_scan.sh full --input-root $T --run-dir $C/scans/step1_dedup --write-filtered 0 --partition health
python3 modelsafety/results/coverage.py --root $T --dedup-run-dir $C/scans/step1_dedup --json-out $C/coverage/step1_dedup.json
sbatch --partition=health --output=$C/_logs/filter_step1_%j.log --error=$C/_logs/filter_step1_%j.log \
  --export=ALL,FILTER_REPO=$REPO launchers/slurm/filter_rows.sbatch \
  --input-root $T --output-root $C/filter_step1 --dedup-run-dir $C/scans/step1_dedup
./scripts/tool_team_data.sh scan extract --output-root $C/filter_step1 --pii-run-dir $C/scans/step2_regex_pii --partition health
./scripts/tool_team_ner_scan.sh full --input-root $C/filter_step1 --run-root $C/scans/step2_ner --partition health
./scripts/tool_team_toxicity_scan.sh all --input-root $C/filter_step1 --run-dir $C/scans/step2_toxicity --pilot-files 0 --partition health
python3 modelsafety/results/coverage.py --root $C/filter_step1 --pii-run-dir $C/scans/step2_regex_pii \
  --ner-run-dir $C/scans/step2_ner --toxicity-run-dir $C/scans/step2_toxicity --json-out $C/coverage/step2.json
sbatch --partition=health --mem=128G --time=12:00:00 --output=$C/_logs/filter_step2_%j.log --error=$C/_logs/filter_step2_%j.log \
  --export=ALL,FILTER_REPO=$REPO launchers/slurm/filter_rows.sbatch \
  --input-root $C/filter_step1 --output-root $C/filter_step2 --pii-run-dir $C/scans/step2_regex_pii \
  --ner-run-dir $C/scans/step2_ner --toxicity-run-dir $C/scans/step2_toxicity
# trace and row status: sbatch --wrap, 4 CPUs, 256G, 1 day, from $REPO, the row status
# with --dependency=afterok:<trace job>; the commands are in steps 6 and 7 above.
```

The counts check is the script in section 5 above, with the Prepared root
passed as its own argument because it isn't under `$C`.

---

## Phase 6: Trace tool

### In plain words

This tool takes any scanner's findings, from any step in the chain, and
walks each one back to its Original row. At every step it checks that the
row is still the same. It writes one line per finding, plus totals. The
validator's "Show original" uses the same lookup code, so the two can never
disagree.

### What we'll have after this phase

- **Shared lookup code in `modelsafety/`.** It moves out of `lookup()` in
  `validator/backend/runs.py`, and out of `read_original_row()` and the
  `_original_*` readers in `validator/backend/detect.py`. The validator then
  imports it.
- **A trace tool,** as a command and as a Python function. For each finding
  it writes:
  - the finding's own reference;
  - for each hop: the run, file, line, and whether the fingerprint matches;
  - the Prepared row, and the Original file and position;
  - a final status: `unchanged`, `changed`, `not_found`, `not_supported`
    (rows read from the Hub), or `broken_link` (a fingerprint didn't match
    at some hop, and the entry says which hop).
- Totals for each status.
- The same tracing for the filter step's `removed_rows.ndjson` entries.

### How we'll do it

1. **Move the lookup code.** The validator imports it, and "Show original"
   behaves exactly as before.
2. **Find each finding's run and file.** Take the scanned folder from the
   scanner's `run_identity.json` and the finding's file path, then find the
   run the way `find_run_above()` does.
3. **Walk the hops.** If the run is a filter run, use its line map to get
   the parent file and line, check the fingerprint, and repeat. At the
   Prepared run, read the origin note from the line and look up the Original
   row.
4. **Read each data file once, in order,** for all the findings in it. NER
   can produce a very large number of spans, and looking each one up
   separately would be far too slow.
5. **Read all four scanners' finding formats:**
   - regex PII: stage 1's `file|line|fingerprint|value` and stage 3's CSV;
   - NER: `all_spans.jsonl` and `llm_candidates.csv`;
   - toxicity: `flagged.jsonl` and `guard_results/all.jsonl`;
   - duplicates: `dedup_results.jsonl`, for both the row and its
     `duplicate_of`.

### Context for planning

- The tracking doc's Part 4 and Part 6 item 5.
- `validator/backend/runs.py`: `lookup`, `_check_origin_shape`,
  `_files_to_compare`, `_file_differences` and `_result`.
- `validator/backend/detect.py`: `read_original_row`, `_original_jsonl`,
  `_original_parquet_row`, `_original_arrow_row`, `_original_csv_row` and
  `_original_json_row`.
- `validator/backend/reader_imports.py`, which shows how the validator
  imports `modelsafety` code.
- The origin note: `modelsafety/contract/origin.py`, and where
  `prepare_rows.py` writes the note.

### Limits

- Read-only.
- Rows read from the Hub are reported as `not_supported`, with no attempt to
  look them up.

### Decisions settled in Phase 6

- **Output format and place.** One NDJSON file for each findings file, in a
  folder named with `--output-dir`, outside every folder the trace reads.
  Each line holds the whole walk (the finding, every hop, the Prepared row,
  the Original location, the status), so a line can be read on its own. The
  folder also has `summary.json`, `run_identity.json` and `_SUCCESS`.
- **Duplicates in cross mode.** The row a duplicate duplicates is in the
  reference folder, which isn't a run. It gets its own line, with status
  `not_supported` and the reason, so the totals still count both sides.
- **What is a `broken_link`.** A fingerprint that doesn't match; a line past
  the end of its file; a skipped-rows or filter entry where a row should be;
  an unfinished run, or a folder that isn't a run; a missing line map entry or
  `_origin` note; a note the run didn't read; and any other failure to read
  the Original (a CSV too large to load, an unreadable file). `broken_at_hop`
  says which hop, and `reason` why.
- **Exit code.** 0: every finding is `unchanged` or `not_supported`. 1: the
  trace finished, but some are `changed`, `not_found` or `broken_link`. 2: it
  refused to start, or a check failed (then there is no `_SUCCESS`).
- **Where the shared code lives:** `modelsafety/trace/`, not `contract/`,
  which is standard library only (the lookup needs pandas and pyarrow).
- **Regex PII is traced at stage 1 and stage 3,** not stage 2 (stage 3 holds
  the same matches as stage 2).
- **No SLURM script.** The pilot trace is a few seconds of CPU, so it runs
  directly. Phase 8 decides how to run it at full size.

### Done when

- On the pilot chain, every finding from every scanner at every step traces
  to `unchanged`, and the totals equal the number of findings.
- On a copy of a pilot run with one byte changed in one line, the trace
  reports `broken_link` at that hop.
- For a few rows, the validator's "Show original" gives the same answer as
  the trace tool.

### What Phase 6 built

- **`modelsafety/trace/original.py`**, the shared lookup. It holds what was
  `lookup()`, `open_run()` and the rest of the note checks in
  `validator/backend/runs.py`, and `read_original_row()` with the `_original_*`
  readers in `validator/backend/detect.py`. New in it:
  - `lookup_notes(run, notes)` looks up many notes of one run and reads each
    Original file once: a JSONL file is opened once, a Parquet file is read
    batch by batch up to the last wanted row (a batch that
    `may_convert_differently` takes its rows from the whole row group, read
    at most once), and a `save_to_disk` folder, a CSV and a JSON file are
    loaded once. `lookup()` is `lookup_notes()` with one note, so the
    validator and the trace run the same reading code.
  - `LookupNotSupported`, a subclass of `RunError`, for the "isn't supported
    yet" cases (Hub rows, `load_dataset` folders). Its messages are the same
    as before, and `app.py` still maps it to the same HTTP 400.
- **The validator** imports all of it through `reader_imports.py`.
  `runs.RunError`, `runs.open_run`, `runs.lookup` and `runs.find_run_above` are
  the same objects as before. `detect.py` keeps the preview's sampling code,
  and imports the Parquet helpers it shares (`may_convert_differently`,
  `PARQUET_BATCH_ROWS`), `HF_MARKER_FILES` and `load_json_rows`.
- **`modelsafety/trace/findings.py`** reads all the findings formats as one
  stream: regex PII stage 1 and stage 3, NER's `all_spans.jsonl` and
  `llm_candidates.csv`, toxicity's `flagged.jsonl` and `guard_results/all.jsonl`
  (for each slice in `manifests/manifest.json`), duplicates' `dedup_results.jsonl`
  (the row, then the row it duplicates), and each filter step's
  `removed_rows.ndjson`. It stops on a finding with no file, line or
  fingerprint, naming the file and line. `parse_hit()` moved from
  `pii_extractor.py` into `pii_output.py` so stage 1's parser can be imported.
- **`modelsafety/trace/trace_findings.py`**, the command and `trace_findings()`.
  Flags: `--pii-run-dir`, `--ner-run-dir`, `--toxicity-run-dir`,
  `--dedup-run-dir`, `--filter-root` (repeatable) and `--output-dir`. It reads
  every finding once to collect the distinct lines to walk, walks them all
  back one hop at a time (each data file and line map read once per hop),
  looks the Prepared rows' Original rows up, then reads the findings a second
  time to write one line each. It refuses before reading anything if a results
  folder has no `run_identity.json` or was made in `folder` input mode, if a
  filter root holds anything but finished filter runs, or if the output folder
  is the same as, inside or around any folder it reads (including every run the
  walk reached).
- **Checks before `_SUCCESS`:** for each source, the lines written equal the
  findings read both times, and the statuses add up to them. For regex PII
  stage 1, NER's `all_spans`, toxicity's `flagged` and duplicates' `row` side,
  the findings for each file equal the scan's own count of findings (read
  through `modelsafety/results/coverage.py`, the way the filter step does).
- **Where it differs from the plan:**
  - The run for a finding is found with `find_runs()` and by walking up from
    the finding's file to the nearest run, not with `select_scan_files()`.
    `select_scan_files()` would refuse the whole trace if any run under the
    scanned folder were damaged, and it can't give a reason for one finding.
    The walk gives each finding its own `broken_link` reason.
  - Each line's `finding` also has `scan_root`, the folder its `file` is
    relative to. For a removed row that is its parent run's folder.
  - The removed-row files are numbered by the order of `--filter-root`:
    `filter_removed_rows_1.ndjson`, `filter_removed_rows_2.ndjson`.
  - If regex PII has no `stage3_extract` folder (the extract stage didn't run),
    stage 3 is left out and listed in `summary.json` under `skipped_sources`.
  - For a CSV findings file, `results_line` is the line the record ends on.

### What Phase 6 ran

Run on 9 Oct 2026, on the login node (CPU, no SLURM job), with
`modelsafety/readers/.venv/bin/python`. Scratch work is under
`/tmp/phase6_check/`. The pilot trace is in `output/pilot_chain/trace/`.

- **The validator behaves as before.** The HEAD and new `runs.lookup` gave the
  same JSON for 8 runs (the four pilot Prepared runs, plus scratch conversions
  of a CSV file, a JSON file and two Parquet files, one with an integer column
  that has nulls so the whole-row-group read is used). Each run got notes of
  many kinds: rows at several positions, a wrong fingerprint, a row past the
  end, a `load_dataset` note, a note for a file the run didn't read, a wrong
  version, a malformed note, a note that isn't an object, and a note with a
  `split`. So did `detect.sample_rows`
  (Parquet, JSON, CSV, JSONL by line and by byte, Arrow) and `detect.inspect_path`.
  The one difference is the exception's class name on the `load_dataset` case
  (`LookupNotSupported` instead of `RunError`).
- **`lookup_notes()` with many notes equals `lookup()` with one,** for a spread
  sample of every run, mixed out of order with repeats and bad notes. With
  `keep_rows=False` the statuses are the same and no row bodies are kept. All
  3,000 rows of the nulls Parquet file come back `unchanged`.
- **The pilot trace** (7.5 s end to end), from the four scans and both filter
  steps. Every finding is `unchanged`, `_SUCCESS` is written, and the findings
  for each file match each scan's own count:

  | Source | Findings |
  |---|---|
  | `regex_pii_stage1` | 4,552 |
  | `regex_pii_stage3` | 637 |
  | `ner_all_spans` | 62,776 |
  | `ner_llm_candidates` | 9,610 |
  | `toxicity_flagged` | 7 |
  | `toxicity_guard` | 7 |
  | `dedup_results` | 104 (52 rows, 52 rows they duplicate) |
  | `filter_removed_rows_1` | 52 |
  | `filter_removed_rows_2` | 2,243 |
  | Total | 79,988 |

  The 79,988 findings come down to 6,049 distinct lines and 3,723 distinct
  Prepared rows. Every `file_differences` list is empty. The output is 106 MB;
  NER's spans are 83 MB of it, about 1.3 KB for each finding.
- **`broken_link`.** On a scratch copy of the hand-written run (Prepared, step 1
  and step 2), with one byte of Prepared line 11 changed, the removed row that
  came from line 11 is `broken_link` at hop 1 (the Prepared hop,
  `fingerprint_matches: false`), the one from line 3 is `unchanged`, and the
  exit code is 1.
- **Refusals** (exit 2, nothing written): an output folder inside a scanned
  folder, no inputs, a scan made in `folder` mode, a filter root with an
  unfinished run, and a results folder for the wrong scanner. A duplicates
  findings file one line short of the scan's own count wrote its results but
  no `_SUCCESS` and exited 2. In a scratch copy of the duplicates results with
  every `duplicate_of` marked as the reference folder (a real cross-mode scan
  wasn't run), the 52 `duplicate_of` lines came out `not_supported`, with the
  reason, and the exit code was 0. Running again into a trace folder with the
  same inputs works, and with different inputs it is refused.
- **The validator agrees with the trace.** For 16 rows (four from each Prepared
  run, from NER's spans), `runs.lookup()` gave the same status, the same
  `file_differences` and the same Original location. The validator's route
  functions `/api/origin` (with a run, with a Prepared file, and its refusals),
  `/api/run`, `/api/run/progress`, `/api/run/skipped`, `/api/runs`,
  `/api/inspect` and `/api/preview` all still run.

At full size, these are estimates from the pilot and haven't been measured.
The pilot had 12.8 NER spans for each row (62,776 spans, 4,886 rows) and about
1.3 KB for each output line. If the 1,143,300 rows of the old data (or about
859,000 after removing the 25% marked as duplicates) have the same density,
NER's output alone would be roughly 11 to 15 million lines and 14 to 19 GB. The
trace also keeps one entry in memory for each distinct line it walks, which
could be a few GB for a million rows. Phase 8 should run it as a SLURM job with
enough memory, and decide whether to trace NER's `llm_candidates.csv`, which is
a subset of its spans.

---

## Phase 7: Per-row status across the chain

### In plain words

This builds one place that answers, for every Prepared row:

- which scanners looked at it, and what they found, with enough detail to
  highlight it;
- whether it was removed, and if so, at which step and why;
- if it was kept, where it ended up in the clean data.

This is what the UI will read for its row views. It's the tool team's
version of what `quality_filter_finalize.py` does for medpsy, extended to
all four scanners and to chains.

### What we'll have after this phase

- **A tool,** as a command and as a function. It takes the last filter step
  of a chain and walks back to the Prepared runs through each filter run's
  `run_identity.json`. For each Prepared run, it writes a row-status file
  with one entry per Prepared row, plus counts for each step.
- **Each entry holds:**
  - the Prepared file, line and fingerprint, and a short form of the origin
    note;
  - for each step, the scanners that saw the row;
  - for each scanner, the number of findings plus enough detail to highlight
    them:
    - NER: label, start, end and message number;
    - regex PII: category and value;
    - toxicity: message number, score and verdict;
    - duplicates: the other row's Prepared location;
  - either `removed` (the step and the reasons), or `kept` with its file and
    line in the clean data;
  - the scanners that never saw it because it was removed earlier, so the UI
    can say "not scanned", never "clean".
- **Functions that read one row, or one page of rows,** by Prepared file and
  line range, without reading a whole file. One way: keep one entry per
  Prepared line, in order, plus an index of byte offsets.

### How we'll do it

- **Inputs:**
  - each filter run's `run_identity.json` (its parent and the scanner
    results it used), its `removed_rows.ndjson` and its line map;
  - the scanners' findings, checked with the Phase 6 code.
- **Map every finding to its Prepared row** through the line maps, reusing
  the Phase 6 code.
- **Write the status files atomically,** with a summary and `_SUCCESS`.
  Check that:
  - the number of entries equals the number of Prepared rows;
  - kept + removed across all steps equals the Prepared rows;
  - the final kept count equals the last filter run's rows.

### Context for planning

- `modelsafety/decisions/quality_filter_finalize.py`, which joins the regex
  PII and toxicity decisions for each row in medpsy and checks every line by
  fingerprint.
- What Phases 4 and 6 write.
- "How the UI will use this", in this doc.

### Limits

- Read-only on everything it reads.
- No UI.
- Don't work out the shared text between duplicates. The UI does that when
  it shows the two rows.

### Decisions settled in Phase 7

- **Where the status files live.** A new folder named with `--output-dir`,
  outside every folder the tool reads (the filter folders, the Prepared runs,
  and the scanners' results and scanned folders), the same rule as the trace.
  There is one status file for each Prepared data file, not for each run, so
  that line k of the status file is Prepared line k.
- **How much finding detail to copy into each entry.** What the filter rules
  read, kept short, plus a pointer (`at`: the results file and line, relative
  to that step's results folder for the scanner, which `summary.json` lists):
  - NER: every span, with label, message number, start, end, confidence, tier
    and whether the policy made it eligible;
  - regex PII: stage 3, with category and value (the matches stage 2 rejected
    are not in the status);
  - toxicity: the guard results, with message number, role, outcome and
    verdict, error, guard categories, and Detoxify's score and category;
  - duplicates: the other row's Prepared location, in both directions.
- **What "had text" means.** Each scanner's own rule, row by row, so the
  status agrees with the scanner's own counts (toxicity scores an empty `text`;
  the other three don't read it). Toxicity's and duplicates' rules moved out of
  their `run.py` into standard-library modules (`row_text.py`) that `run.py`
  imports, with no change in behaviour. Every file's totals are checked against
  the scanner's recorded `rows_with_text` and `rows_without_text`.
- **Python.** The readers' Python, like the trace, because the builder uses the
  trace module's helpers. Reading a row needs only the standard library. No
  SLURM script yet; the pilot takes about 4 seconds.
- **No Original lookups.** An entry carries the short form of the origin note
  only. The lookup is Phase 6's.

### Done when

- On the pilot chain, the number of entries equals the number of Prepared
  rows.
- The counts for each step match each filter run's `summary.json`.
- A few rows match the scanners' raw findings when checked by hand.
- Reading one row, or one page of rows, works without reading a whole file.

### What Phase 7 built

- **`modelsafety/trace/row_status.py`**, the command and `build_row_status()`.
  Give it `--filter-root` (the last filter step) and `--output-dir`. It follows
  every filter run under the root back to its Prepared run through the runs'
  `run_identity.json` and `summary.json` (which must agree), and takes the
  earlier steps and each step's scanner results from them. It writes, for each
  Prepared data file:

  ```text
  <output-dir>/<run>/rows/<file>.ndjson    one entry for every Prepared line, in order
  <output-dir>/<run>/rows/<file>.offsets   one line for every entry, plus one
  ```

  and `run_identity.json`, `summary.json` and `_SUCCESS` for the chain. An
  offsets line is the byte offset of an entry in the status file, as 20 digits
  and a newline (21 bytes); the last line is the file's size. So entry k is one
  seek into each file.
- **An entry** (one JSON line):

  | Field | Holds |
  |---|---|
  | `prepared` | `run`, `file`, `line`, `byte_offset`, `source_sha256` |
  | `origin` | the short origin note (`path`, `line`, `byte` or `row`, `split`, `config`), or null |
  | `steps` | one item for every step: `outcome` is `kept` (with `at`), `removed` (with `removed_entry`: the removed-rows file and line, and `reasons`, copied as `removed_rows.ndjson` has them) or `not_reached`; and `scanners` |
  | `final` | kept: `run`, `file`, `line`, `byte_offset` in the last filter run; removed: the `step` |
  | `not_scanned` | every `{step, scanner, why}` where the scanner had no text (`no_text`) or never saw the row (`removed_earlier`) |

  For each scanner in a step, `scanners` has `{"state": "scanned", "findings":
  [...]}`, `{"state": "no_text"}` or `{"state": "not_reached"}`. A scanned row
  with no findings is the only one that is clean for that scan. The duplicates
  entry also has `duplicated_by`: the rows that duplicate this one.
  A duplicate's `findings` item is `{"duplicate_of": {run, file, line} or
  {reference: true, file, line}, "at"}`.
- **`modelsafety/trace/row_status_read.py`**, standard library only:
  `read_summary()` (refuses a folder with no `_SUCCESS`), `read_row()`,
  `read_rows()` (a page of up to 1,000 rows, cut short at the end of the file),
  `status_paths()`, and the `show` command. The run and file asked for must be
  listed in `summary.json`.
- **Shared code.**
  - `Finding.detail` in `modelsafety/trace/findings.py` (not part of a finding's
    identity). The trace doesn't write it, and its output is unchanged.
  - `guard_outcome()` in `modelsafety/filter/rules.py`: the one definition of
    "guard error, or a verdict", now used by the filter and the status.
  - `row_text.py` in `toxicity/` and `dedup/` (see above).
- **Checks.** All are made in memory before the first file is written, and
  stop the tool with exit code 2 and nothing written; then every file is read
  back, and `_SUCCESS` is written only if that passes too.
  - Each Prepared file has the rows, bytes and SHA-256 its run wrote, with no
    blank line or skipped-rows entry.
  - At every step, each kept line has the fingerprint of the line it came
    from, its line map entry, and the line before it, and the byte offset its
    line map gives. Each removed row's saved line has its fingerprint. Every
    line of the run before is kept or removed, exactly once, and the filter
    run's rows, bytes, SHA-256, line maps and removed-rows file are the ones its
    `summary.json` lists.
  - For each step and file, rows in, kept, removed, and removed by reason equal
    the filter run's `summary.json`.
  - For each step, scanner and file, rows read, with text and without text
    equal the scan's own counts; findings equal the scan's own count of
    findings (NER, toxicity and duplicates), and every finding is on a line
    with its fingerprint, in a row that scanner had text in.
  - Kept + removed across all steps equals the Prepared rows, and the final
    kept count equals the last filter run's rows.
  - After writing: every entry starts where the index says and is the status
    of the line it should be.
- **It refuses to start** (nothing written) if the output folder is the same
  as, inside or around a folder it reads; the filter root holds anything but
  finished filter runs; the runs under it were made by different steps or
  settings, or a step didn't read the previous step's output; a scanner's
  results don't match the folder the step filtered, or were made in `folder`
  input mode; or the output folder holds a different status. It also stops if
  the duplicates scans of different steps used different text settings, because
  then one row could have text for one and not the other.
- **Where it differs from the plan.**
  - It takes only the last filter root, and finds the rest itself.
  - It does not reuse the trace's per-finding walk, which only follows the
    lines that have findings. It builds a map for every row, from the same line
    maps, with the same fingerprint check at every hop, and reuses the trace's
    helpers for reading runs, finding the run of a file and checking a results
    folder.
  - A Prepared row with no `_origin` note gets `origin: null`, and the summary
    counts them in `rows_without_origin` and warns; it doesn't fail the status
    (there are none in the pilot).
  - It removes an earlier `_SUCCESS` and `summary.json` from its own folder as
    soon as the checks start, so a failed run can't look finished.

### What Phase 7 ran

Run on 9 Oct 2026, on the login node (CPU, no SLURM job), with
`modelsafety/readers/.venv/bin/python`. Scratch work is under
`/tmp/phase7_check/`. The pilot status is in `output/pilot_chain/row_status/`.

- **The move of the text rules changes nothing.** In each scanner's own
  environment (the toxicity model package, `detoxify`, was stubbed out, as no
  model is used), the old (HEAD) and new `iter_candidates` gave identical
  candidates over the pilot's files: 17,925 toxicity candidates over 6 files,
  and 4,936 duplicates candidates over 6 files. The per-file counts equal the
  recorded `detoxify_summary.json` and `summary.json`. The duplicates text
  functions also gave the same text for edge-case rows, with each text scope
  and with and without system messages left out.
- **The shared code changes nothing else.** The trace, re-run into `/tmp`,
  gave all 9 findings files byte for byte as in `output/pilot_chain/trace/`, and
  the same summary apart from `created_at`. The second filter step, re-run
  into `/tmp` with the same inputs, wrote the same `removed_rows.ndjson` for all
  four runs (so the `guard_outcome()` move changed nothing).
- **The pilot status** has 4,938 entries (2,000, 2,000, 919 and 19 for the four
  runs) and passes every check. 2,643 rows are kept, 52 were removed at step 1
  and 2,243 at step 2, which match the Phase 5 table and each filter run's
  `summary.json`, run by run and step by step. No row is missing its origin
  note. It took 3 to 4 seconds, with a peak of 167 MB (mostly the imports, so
  not a guide for the full data). The status files come to 16.7 MB: about 3.4 KB
  for a row.
- **Checked against the raw files** (a separate script, which reads the status
  only through the reader): for all 4,938 rows, the Prepared line's fingerprint
  and byte offset, the origin note, and where a kept row is in the clean data
  (the same bytes, at the byte offset given); for all 63,472 findings (52
  duplicates, 637 regex PII, 62,776 NER, 7 toxicity), the findings of each
  scanner in each row equal the ones in the scanner's own file, and every
  duplicate pair is listed on both rows; no finding is on a row marked as
  having no text. Every removed row has reasons, and every later step of it is
  `not_reached`.
- **By hand, on the hand-written run.** Line 19 was removed at step 1 as a
  duplicate of line 12, line 12 lists line 19 in `duplicated_by`, and line 19's
  step 2 scans are `not_reached`. Line 3 was removed at step 2 by regex PII
  (the email) and NER (5 spans, 2 of them eligible: the email and the street
  address). Line 11 was removed by NER only. Line 17 shows `no_text` for
  duplicates, regex PII and NER, and `scanned` for toxicity. Line 18 shows
  `no_text` for all four.
- **Reading.** `read_row()` of line 937 of a MOPD file read 42 bytes of the
  index and 3,983 of the 2,138,136-byte status file. A page of 50 from line 960
  came back with 41 rows (the end of the file) after reading 882 and 71,804
  bytes. Refused: line 0, a line past the end, a page of 5,000, a file or run
  that isn't listed, and a folder with no `_SUCCESS`.
- **Refusals** (exit code 2, nothing written): an output folder inside the
  filter step 1 folder, and one around everything; the Prepared root as
  `--filter-root`; a folder that doesn't exist; the first filter step into the
  folder that holds the two-step status (a different chain); a scratch copy
  of the second filter step with one line-map fingerprint changed; and a scratch
  copy that pointed at a scratch NER summary with a file's `rows_with_text`
  one lower (the same copy, with the real NER summary, passes). The scratch
  folders are in `/tmp`; nothing under `output/` other than the new status
  folder was written. Running the same command again into the finished status
  folder works and writes the same files.

At full size, these are estimates from the pilot and haven't been measured.
Entries for all of the 1,143,300 Prepared rows at about 3.4 KB each would be
roughly 4 GB, mostly NER's spans (the Phase 6 estimate of NER's density was
about 11 to 15 million spans). The tool keeps every row and finding in memory,
so Phase 8 should run it as a SLURM job with generous memory, and ask first.

---

## Phase 8: Full run, starting fresh

### In plain words

We delete the old tool team data and results, convert every tool team
dataset with today's code, and run the chain for real. Then we run the
coverage report, the trace tool and the per-row status on the results. After
this, every finding and every removed row can be followed back to its
Original row, and the clean data is ready.

### What we'll have after this phase

- Every tool team dataset converted under one folder, each run with
  `_SUCCESS`.
- The full chain, with every step passing the coverage report.
- Trace results with their totals: everything `unchanged`, apart from any
  Hub rows, which are `not_supported`.
- Per-row status for every Prepared row.
- The clean data: the last filter step's kept files.
- Part 5 of the tracking doc rewritten for the new state.

### How we'll do it

1. **Decide where the converted data lives** (see the decision below).
2. **Delete the old data, but only after the user OKs the exact list,** with
   sizes shown first. The candidates on 8 Oct 2026 are:
   - `output/tool_team_converted/`
   - `output/tool_team_pii_run/`
   - `output/tool_team_ner_run/`
   - `output/tool_team_toxicity_run/`
   - `output/tool_team_dedup_run/`
   - `output/tool_team_dedup_pilot/` and
     `output/tool_team_dedup_pilot.files.txt`

   Ask separately about `output/tool_team_regurgitation/`, which was built
   from the old regex PII results.
3. **Convert.**
   - The old conversion had 65 datasets: the SFT mix datasets, plus the MOPD
     mix and the RL mix, each with train and test.
   - Two runs made on 8 Oct with today's code already exist under
     `output/prepared/tools_team/rl_mixes/` and have `_SUCCESS`. Keep them if
     they're in the chosen folder and the conversion code hasn't changed
     since.
   - The old conversion wrote 1,143,300 rows. Any difference in the new
     total must be explained, for example by changed Original data or a
     different set of datasets.
4. **Run the chain** using the commands from the Phase 5 runbook, at full
   size, with the coverage report after each scan.
5. **Run the trace tool** on every scanner's findings, then build the
   per-row status.
6. **Wrapper defaults:** if the user wants, change the wrappers' default
   input from `output/tool_team_converted` to the new folder.
7. **Update the tracking doc.**

### Decisions to settle in planning

- **Where the converted data lives, and what the runs are called.** Option
  one is `output/prepared/tools_team/`, where the UI already writes runs
  (for example `rl_mixes/rl_mix_v12_ifmt_0903_train`). Option two is the
  `tool_team_data.sh convert --output-root` layout (`sft_mix/<name>`,
  `rl_mix/train`). Pick one, so the same dataset isn't converted twice under
  two names. Recommended: the UI's folder, because the UI will read it.
- **When to delete the old data:** before converting, or after the new chain
  passes.
- **The rules (Phase 4) and the chain order (Phase 5),** fixed for this run.

### Done when

- The coverage report passes at every step.
- For every dataset, Prepared rows = final kept rows + the rows removed at
  every step.
- The trace totals show no `changed`, `not_found` or `broken_link`, and any
  `not_supported` rows are explained.

### Decisions settled in Phase 8

- **Where the converted data lives:** `output/prepared/tools_team/`, in the
  `tool_team_data.sh convert` layout (`sft_mix/<name>`, `mopd_mix/{train,test}`,
  `rl_mix/{train,test}`). The two UI runs under `rl_mixes/` held the same data
  under other names, so they were deleted first (4.4G, with the user's OK).
- **Where the chain lives:** `output/tool_team_chain/`, outside
  `output/prepared`.
- **Chain order and rules:** duplicates first (Phase 5), and the Phase 4
  defaults for filter step 2, with nothing ignored.
- **Old data:** not deleted. The user chose to keep `output/tool_team_converted`,
  the four old results folders, the dedup pilot and `tool_team_regurgitation`.
- **Wrapper defaults:** changed to `output/prepared/tools_team` in
  `tool_team_data.sh`, `tool_team_ner_scan.sh`, `tool_team_toxicity_scan.sh`,
  `tool_team_dedup_scan.sh` and `verify_sources.py`. The results-folder
  defaults (`output/tool_team_*_run`) are unchanged. Note that
  `tool_team_regurgitation.sh` still reads the old
  `output/tool_team_pii_run/stage3_extract/pii_extract.csv` by default.

### What Phase 8 ran

Run on 9 to 12 Oct 2026, from the repo root, with every SLURM submission
approved first. The code was the commit `05d01bc` plus the one NER fix below.

| Step | Result |
|---|---|
| Conversion (65 CPU jobs, 2 CPUs, 32G) | 65 runs, 1,174 files, 1,143,300 rows, nothing skipped. Every job took seconds to a few minutes. `verify_sources.py`: 65 of 65 match. The row count equals the old conversion's for every dataset. |
| Duplicates (CPU) | 25 min, 10.1 GB peak. 283,638 marked (24.81%), 41 rows without text. Coverage passes. |
| Filter step 1 (CPU) | 2 min, 19 GB peak (32G requested). 283,638 removed, 859,662 kept. `sft_mix/xlam_irrelevance_v2` has no rows left (every row duplicates another dataset). |
| Regex PII, on step 1's output (CPU) | 71 min. 688,458 matches in stage 1, 64,246 in stage 3 on 41,472 rows. |
| NER, on step 1's output (8 GPUs, one node) | 3 h. 1,107 of 1,107 work units, 6,426,482 spans, 1,809,936 eligible on 313,714 rows. |
| Toxicity, on step 1's output (8 shards) | About 40 min. 3,306 flagged, 3,219 judged, 87 guard errors, 0 unknown. |
| Coverage, step 2 | Passes for regex PII, NER and toxicity (1,174 files, 859,662 rows). Reports are in `output/tool_team_chain/coverage/`. |
| Filter step 2 (CPU, 128G requested) | 3 min 18 s, 19.5 GB peak. 315,874 removed, 543,788 kept. Reasons: NER 313,714 rows, regex PII 41,472, toxicity 1,360 (812 Unsafe, 461 Controversial, 87 guard errors). |
| Count check | 65 runs: 1,143,300 = 283,638 + 315,874 + 543,788. Every run OK. |
| Trace (CPU, 256G requested) | 19 min. 9,350,260 findings, 12 GB output, all `unchanged`, no `not_supported`. `sacct` reports a peak of only 637 MB for it, which can't be right for a job that holds a dict of every distinct line, so treat the peak memory as not measured. |
| Row status (CPU, 256G requested, after the trace) | 9 min 26 s, 34.4 GB peak. One entry for every Prepared row (2.4 GB), `checks.passed`, no warnings. |

Spot check: line 3 of `rl_mix/train/chunk-0/data_000000.jsonl` is kept at
step 1, removed at step 2 by NER (customer_id and medical_record_number), and
`row_status_read.py show` returns its origin (`train.parquet`, row 2), each
scanner's findings and the pointers to the scanner's own files.

**Problems found, and where they belong**

| # | What | Belongs to | Status |
|---|---|---|---|
| 1 | NER's coverage failed on 67 files that step 1 left with zero rows. A zero-byte file gets no work unit, and `aggregate()` built `summary.json`'s per-file `sources` from the units alone, so those files had no entry. The pilot had no empty files. | NER, Phase 1 | Fixed in `aggregate()` in `pii/model/scanner.py`: `empty_source_files()` finds the manifest files with `size_bytes` 0 and no units, and each gets an all-zero entry. Only the aggregate step was re-run (CPU, 5.5 min, 31.8 GB peak of 32G requested; the same 6,426,482 spans), which rewrote `summary.json`, `all_spans.jsonl`, `llm_candidates.csv` and `_SUCCESS` in `scans/step2_ner`. **Uncommitted.** |
| 2 | The NER aggregate came close to its memory request (31.8 of 32 GB). | NER | Gotcha: ask for more than 32G for a full-size aggregate. |
| 3 | `tool_team_regurgitation.sh` reads the old regex PII results by default. | Regurgitation probe | Open. Pass `INPUT_CSV` to use the new results, which cover the rows that survived step 1. |
| 4 | The UI's `PathForm.tsx` example paths point into `output/tool_team_converted`. | UI doc | Open, not touched (no UI work). |
| 5 | The `AskQuestion` prompt in the agent session twice returned answers to earlier questions. | Not a bug in this repo | Gotcha: the jobs were only submitted after matching answers. |

---

## Open decisions in one place

| Decision | Needed by | Recommendation |
|---|---|---|
| Where regex PII's per-file counts go | Phase 1 | Planner proposes after checking readers |
| Field names for the per-file counts | Phase 1 | The same names in all four scanners |
| A stray `.jsonl` outside any run | Phase 2 | Skip and report, like an unfinished run |
| Shape of the coverage JSON report | Phase 3 | **Settled in Phase 3.** One report per run and per scanner, with the full list of problems and each file listed only where something is wrong. Defined by `build_report()` in `modelsafety/results/coverage.py` (`REPORT_VERSION` 1) |
| A scanner that wasn't run | Phase 3 | **Settled in Phase 3.** Reported as "not run" and never counted as passed. The overall verdict covers only the scanners that ran, and fails if none ran |
| Toxicity rule, and what to do with guard errors | Phase 4 | **Settled in Phase 4.** Remove Unsafe and Controversial; guard errors count as unsafe, or the filter refuses (`--toxicity-guard-errors refuse`). All configurable |
| NER rule (labels, tiers, confirmed or raw) | Phase 4 | **Settled in Phase 4.** Policy-eligible spans (`candidate_eligible`), with labels to ignore configurable. The LLM step isn't used |
| Regex PII rule (stage, ignored categories) | Phase 4 | **Settled in Phase 4.** Stage 3, every category, ignored categories configurable. No zip or postcode category exists |
| Duplicates rule | Phase 4 | **Settled in Phase 4.** Remove every marked duplicate and keep the row it duplicates; global and cross modes, configurable |
| Whole line in `removed_rows.ndjson` | Phase 4 | **Settled in Phase 4.** Yes; one line map for each data file |
| Chain order | Phase 5 | **Settled in Phase 5.** Duplicates first, then regex PII, NER and toxicity side by side, then a second filter step. Rows removed early are never seen by later scanners |
| Where trace and status outputs go | Phases 6 and 7 | **Settled in Phases 6 and 7.** A new folder named with `--output-dir`, outside every folder the tool reads (the trace's, and the row status's). The status has one file for each Prepared data file |
| How much finding detail goes into the row status | Phase 7 | **Settled in Phase 7.** What the filter rules read, kept short (NER spans, regex PII category and value, the toxicity guard result, the duplicate's other row), plus a pointer to the scanner's own file and line |
| Where the converted data lives | Phase 8 | **Settled in Phase 8.** `output/prepared/tools_team/`, in the `tool_team_data.sh convert` layout |
| What to delete, and when | Phase 8 | **Settled in Phase 8.** The two UI runs were deleted before converting. The old tool team data was kept. |

---

## Where the tracking doc's items went

Part 6 of `TRACKING_SCANNED_ROWS.md` lists nine items. Here's where each one
ended up.

| Tracking doc item | Here |
|---|---|
| 1. Same counts for each file | Phase 1 |
| 2. Check toxicity's second stage | Phase 1 |
| 3. Coverage report covers everything | Phase 3 |
| 4. Don't scan unfinished conversions | Phase 2 (skip and report) |
| 5. Trace tool | Phase 6, extended to chains (done) |
| 6. Line fingerprints in NER and duplicates | Phase 1 |
| 7. Measure text left in `metadata` | Dropped for now (decision 3) |
| 8. One final answer per Prepared row | Phase 7, extended to chains and all four scanners (done) |
| 9. "Show original" for Hub rows | Dropped (decision 1) |
| Finished markers (Part 3) | Phase 1 |
| Part 5, options A and B | Option A, Phase 8 |
| Decision 3, toxicity guard errors | Still open, Phase 4 |
| New: the filter step and chains | Phase 4 |
