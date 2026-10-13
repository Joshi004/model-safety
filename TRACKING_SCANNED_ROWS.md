# Tracking rows through conversion and scanning

This doc answers three questions in plain words:

1. Is it true that we can turn Original data into Prepared data from the UI,
   follow every Prepared row back to its Original row, and see exactly what
   was converted and what was missed?
2. One step further: when a scanner reads Prepared rows, how sure can we be
   about what it read and what it didn't? Can we follow a scanner's finding
   back to the Prepared row, and from there to the Original row?
3. What already works, and what still needs to be built? This is about the
   functionality only. The UI comes later.

Everything here was checked against the code and against the real results
in `output/` on 7 Oct 2026.

## The short answer

- **Original to Prepared: the claim holds,** for anything converted with
  today's code. Every line of every Original file is accounted for, skipped
  rows are kept with their reason, and every Prepared row carries a note
  saying exactly where its Original row is. There are a few small gaps,
  listed in Part 1.
- **Prepared to scanned: partly.** All four scanners record which files they
  read, and a report checks that list against what the conversion wrote. But
  the scanners don't all count rows the same way, two of them can't show
  that every row was read, and one stage-to-stage handoff isn't checked.
  Also, **no scanner reads anything inside `metadata`**, and some of our data
  keeps a lot of text there.
- **Finding to Prepared to Original: the trace tool joins them** (Phase 6).
  Every finding names a file and line, every filter run says where each kept
  line came from, and every new Prepared row says where its Original row is.
  `modelsafety/trace/trace_findings.py` takes any scanner's findings, from any
  step of a chain, and walks each one back, checking the fingerprint at every
  hop. See Part 4.
- **One answer for every Prepared row of a chain: the per-row status**
  (Phase 7). `modelsafety/trace/row_status.py` writes, for each Prepared row,
  what every scanner found, whether the row was removed (at which step, and
  why) or where it is in the clean data, and which scans never saw it. See
  Part 4.
- **The tool team's existing scan results can't be traced back to the
  Originals.** Their Prepared data was made on 22 Sep, before the "where did
  this row come from" note existed. The row counts do match everywhere
  (1,143,300 rows), so we can be fairly confident the NER and toxicity scans
  read every row.

## Words used in this doc

- **Original data:** the data as the teams gave it to us (JSONL, Parquet,
  Arrow folders, CSV, JSON). We never change it.
- **Prepared data:** what `prepare_rows.py` writes: JSONL files where every
  row has a `text` or `messages` column, plus a `metadata` column holding
  every other Original column.
- **Run folder:** the folder one conversion writes into. It holds the
  Prepared files (`chunk-*/data_*.jsonl`), `summary.json`,
  `skipped_rows.ndjson`, `run_identity.json`, and `_SUCCESS` if every check
  passed.
- **Scanner:** a tool that reads Prepared rows and reports what it finds
  (PII, toxic text, duplicates).
- **Finding:** one thing a scanner reports, such as "this email address, in
  this file, on this line".
- **Fingerprint:** a 64-character code worked out from the exact content of
  a line, row or file. Change one character and the code changes. If two
  fingerprints match, the content is the same.
- **Origin note:** `metadata._origin` on every Prepared row. It says which
  Original file the row came from, where in that file (line and byte, or
  row number), and the fingerprint of the Original row.

The whole chain looks like this:

```text
 Original row ──conversion──> Prepared row ──scanner──> Finding
      ▲                         │      ▲                  │
      └──── metadata._origin ───┘      └── file + line ───┘
```

Part 1 is about the left link. Parts 2 to 4 are about the right link, and
about walking both links together.

---

## Part 1: Original to Prepared (checking the claim)

### What's true today

1. **You can start a conversion from the UI.** The validator can check the
   output folder, start one SLURM job per dataset (CPU only), watch it, and
   cancel it. `scripts/tool_team_data.sh convert` submits through the same
   job script (`launchers/slurm/prepare_rows.sbatch`).
2. **Every line is accounted for.** For each Original file, `summary.json`
   records, and `prepare_rows.py` checks, that:
   - JSONL: lines = blank lines + rows read;
   - every format: rows read = written + skipped + left out (rows cut off by
     `--max-rows`);
   - Parquet and Arrow: the file's own row count (read separately, from the
     file itself) equals written + skipped + left out;
   - rows on disk = rows written, and entries in `skipped_rows.ndjson` = rows
     skipped.

   If any of these doesn't add up, the run fails and gets no `_SUCCESS`.
3. **Nothing is dropped quietly.** A line that isn't valid JSON stops the
   run and names the file and line. A row is never just ignored.
4. **Skipped rows are kept, with the reason.** `skipped_rows.ndjson` holds
   every skipped row in full, with its origin note, which column was missing
   or empty, and what it was meant to become.
5. **Every Prepared row points back to its Original row.** The origin note
   has the file path, the position (line and byte for JSONL, row number for
   the rest) and the Original row's fingerprint.
6. **"Show original" checks the row is still the same.** It reads the
   Original row again, works out its fingerprint, and says `unchanged`,
   `changed` or `not_found`. It also says if the file's size or date changed
   since the conversion read it. It only opens files the run says it read.
7. **A run folder can't hold two different runs.** `run_identity.json`
   records the settings and input files. Starting a different conversion in
   the same folder is refused.
8. **Scanners can't read skipped rows by mistake.** The skipped-rows file
   ends in `.ndjson`, so scanners never pick it up, and every scanner also
   refuses a file whose first line looks like a skipped-rows entry.

### Small gaps

- **Rows read straight from the Hugging Face Hub** (a dataset ID rather than
  a local folder) get an origin note, but "Show original" can't look them up
  yet, and there are no per-file counts for them.
- **CSV and plain JSON have no second count to check against,** because
  those formats don't store one. The counts still add up; they're just
  counted once.
- **The text check is only a warning.** `summary.json` says how many written
  rows have text a scanner can read (`text_coverage`). If "skip the row if
  this is empty" isn't ticked, rows with no readable text are still written.
- **Text left inside `metadata` is never scanned.** See Part 2. Nothing in
  `summary.json` tells you how much text ended up there.
- **All of this applies only to data converted with today's code.** The
  tool team's Prepared data in `output/tool_team_converted/` was made on
  22 Sep. Its rows have no origin note, its `summary.json` files have no
  per-file counts or list of written files, and its folders have no
  `run_identity.json`. See Part 5.

**Verdict:** for new conversions, the claim is true. What got converted and
what got skipped is fully visible and checked.

---

## Part 2: How the scanners read Prepared rows

| Scanner | How it's run for the tool team | What it looks for | GPU |
|---|---|---|---|
| Regex PII | `scripts/tool_team_data.sh scan extract` | emails, phones, cards, IPs… by pattern | No |
| NER PII (AI model) | `scripts/tool_team_ner_scan.sh` | names, IDs, addresses… with a model | Yes |
| Toxicity | `scripts/tool_team_toxicity_scan.sh` | harmful text: Detoxify first, then Qwen3Guard on what Detoxify flagged | Yes |
| Duplicates | `scripts/tool_team_dedup_scan.sh` | rows that are the same or nearly the same | No |

The regurgitation probe isn't covered here. It doesn't read Prepared rows;
it starts from PII findings and tests a model with them.

### How they choose files

*Updated after Phase 2 of `BACKEND_IMPLEMENTATION_PHASES.md`. This is what a
scan made with the current code does.*

All four scanners take their files from the runs themselves
(`--input-mode runs`, the default). The shared run finder,
`modelsafety/contract/run_finder.py` (the same code as the validator's runs
list), looks under the folder you give it, at any depth, for run folders. A
run is read only if it finished: its `summary.json` passed its checks,
`_SUCCESS` exists, and `output.files_written` lists the files it wrote, each
with its rows, size and fingerprint. The scanner reads only those files, and
checks each file's fingerprint against the one its run wrote:

- regex PII: right after its first pass over the files;
- NER: when the work manifest is built, which reads every byte (the GPU work
  units still check each file's size and date, as before);
- toxicity: when the shard manifest is built, and again after stage 1 has read
  a shard's files;
- duplicates: after reading every file, before any results are written.

A mismatch fails the scan and names the file.

Everything else under the folder is skipped, listed with a plain reason in
`scan_inputs.json` next to the scan's results, and summarised on screen:

| Scanner | Where `scan_inputs.json` is |
|---|---|
| Regex PII | `stage1_scan/` |
| NER PII | `manifest/` |
| Toxicity | `manifests/` |
| Duplicates | the results folder |

What gets skipped: an unfinished run (`checks_failed`, `in_progress`,
`may_have_stopped`, `failed`, `no_summary`, or no `_SUCCESS`); a run whose
`summary.json` doesn't list `output.files_written` (every conversion made
before runs recorded them, such as the tool team's 22 Sep data); and a stray
`.jsonl` that no run lists. If nothing is left to scan, the scan fails and
says why. A run whose listed file is missing or the wrong size fails the
scan, because the run is damaged.

The run finder reads two kinds of run: Prepared runs (`prepare_rows.py`) and
filter runs (`modelsafety/filter/filter_rows.py`, Phase 4). A filter run lists
the files it wrote in `output.files_written` the same way, so a scanner
pointed at filter output reads only its kept files (see "Filter runs" in
Part 4).

`--input-mode folder` keeps the old behaviour: every `.jsonl` under the
folder, at any depth (or a list of files you give it). The medpsy pipeline
uses it, because `output/medpsy2/baichuan_cleaning/filtered` isn't a run
folder. The medpsy submit scripts (`submit_medpsy_quality_filter.sh` and
`submit_privacy_filter.sh`) default to it; the tool team wrappers default to
`runs`. Each results folder records which mode was used (`input_mode` in
`run_identity.json` and in `scan_inputs.json`), so one folder can't mix the
two. Duplicates' cross mode applies the mode to the candidate folder only;
the reference folder (benchmarks) is always read as a plain folder.

### What text inside a row they read

Every scanner reads `text` first. Only if there's no `text` does it read
`messages`. Then they differ:

| Scanner | What it reads from `messages` |
|---|---|
| Regex PII | All messages joined into one block |
| NER PII | Each message on its own, any role |
| Toxicity | Each message on its own, as `role: content` |
| Duplicates | All messages joined, but by default without the final assistant reply. The tool team wrapper also leaves out system messages. |

Each scanner's rule for "this row has text" is code the per-row status
(Part 4) uses too: `textract.py` (regex PII), `pii/model/common.py` (NER),
and `row_text.py` next to `toxicity/run.py` and `dedup/run.py`. Toxicity
scores a plain `text` even when it is empty; the others don't read an empty
`text`.

**No scanner reads:**

- anything inside `metadata`;
- any part of a message other than `role` and `content` (for example
  `tool_calls` or `name`);
- parts of `content` that aren't text.

**This matters for the tool team data.** Measured on all four MOPD and RL
datasets:

- In `rl_mix/train`, 74,624 of 236,641 rows (32%) have `answer`,
  `raw_system` and `tools` inside `metadata`, and 68,691 also have
  `conversations` there.
- In `mopd_mix/train`, it's 3,453 of 310,537 rows.
- Across the four datasets, `metadata` holds about 0.8 billion characters,
  next to about 3.4 billion in `messages`. The metadata count is of the
  whole JSON, so it includes key names and punctuation, and not all of it is
  free text. `reward_model.ground_truth`, for example, holds tool-call
  arguments such as game IDs.

None of that is scanned. In a sample of 12,911 rows (200 from each of the 65
datasets), every message was just `role` plus a plain-text `content`, with
no `tool_calls`. So the other two blind spots don't affect this data today,
but they will on data shaped differently.

### What each scanner writes

*Updated after Phase 1 of `BACKEND_IMPLEMENTATION_PHASES.md`. This is what a
scan made with the current code writes. Results made earlier (Part 5) don't
have the new fields.*

Every scanner now gives each file it read four counts: `rows_read`,
`rows_with_text`, `rows_without_text` and `findings`. `rows_read` always
equals `rows_with_text` plus `rows_without_text`. A finding is a regex PII
stage 1 match, an NER span, a toxicity flagged message, or a duplicate row.
"Has text" is decided by each scanner's own reading of the row (see the
table above), and that hasn't changed.

- **Regex PII,** in its results folder:
  - stage 1 writes every match as `file|line|line fingerprint|value`, plus
    `stage1_scan/input_files.json` (each file read, with its fingerprint and
    the four counts) and `metrics.txt` (one total of lines read);
  - stage 2 keeps only the matches that pass simple rules;
  - stage 3 (`pii_extract.csv`) opens each Prepared line again, checks its
    fingerprint and that the value is really in its text, and saves the
    whole Prepared line next to the match;
  - each stage writes `_SUCCESS` in its own folder (`stage1_scan`,
    `stage2_validated`, `stage3_extract`) as its last step.
- **NER PII:** every span the model found (`all_spans.jsonl`: file, line,
  line fingerprint as `source_sha256`, message number, character positions,
  label, confidence), the subset to double-check (`llm_candidates.csv`, with
  the whole Prepared line and `source_sha256`), and `summary.json` with the
  four counts, rows and messages per file, and the list of files read. Each
  piece of work writes its own `_SUCCESS`, and the aggregate step writes one
  for the whole run, last, only when no piece is missing.
- **Toxicity,** per slice of files (each slice is one GPU job):
  - stage 1 (`flagged.jsonl`) writes only messages scoring above the
    threshold (0.5). Each one has the file, line, line fingerprint, message
    number, and the whole Prepared row. `detoxify_summary.json` has the
    four counts per file (next to the older `rows` and `messages`) and the
    list of files read. It is written only when stage 1 read every file to the
    end. Its top-level `rows_read` still counts only rows that had text.
  - stage 2 (`guard_results/all.jsonl`) is the same records with
    Qwen3Guard's verdict added, or an error if it couldn't judge one.
    `check_guard_counts.py` then checks that verdicts, errors and unknowns
    add up to the number stage 1 flagged. The slice's `_SUCCESS` is written
    only after that check passes.
- **Duplicates:** each duplicate row as file + line + line fingerprint
  (`source_sha256`), with the file + line + fingerprint of the row it
  duplicates. Also `summary.json` (rows, rows compared, rows with no text,
  `per_file` counts, list of files read), and `filtered/` and
  `filtered_out/`: copies of the Prepared files split into kept and
  duplicate lines. In cross mode, `per_file` is under `candidate` and
  `reference` (reference files have no `findings`, because their rows are
  never marked).

---

## Part 3: How sure can we be about what got scanned?

There are four questions, from the biggest to the finest:

1. Did the scanner read the right files?
2. Did it read every row in those files?
3. Inside each row, did it see the text?
4. When a scanner has two stages, did everything from stage 1 reach stage 2?

We don't need a list of every row a scanner read. If the file list matches
what the conversion wrote, and every file was read to the end, then every row
was read. That's how the checks below work.

### 1. The right files

| Scanner | Records each file it read, with fingerprint | Checked by the coverage report |
|---|---|---|
| Regex PII | Yes (`stage1_scan/input_files.json`) | Yes |
| NER PII | Yes (`summary.json`, `input`) | Yes |
| Toxicity | Yes (each slice's `detoxify_summary.json`) | Yes |
| Duplicates | Yes (`summary.json`, `input`) | Yes |

*Updated after Phase 3 of `BACKEND_IMPLEMENTATION_PHASES.md`.* The coverage
report is `modelsafety/results/coverage.py`. It compares each scanner's file
list with the files every finished conversion says it wrote
(`output.files_written` in `summary.json`). That catches a file the scanner
missed, an extra file (for example from an old or failed conversion in the
same folder), and a file that changed between conversion and scan. It also
checks that the scan was made on the folder being checked, and by the right
kind of scanner (`run_identity.json`). NER and toxicity also check, just
before reading each file, that its size and date haven't changed since their
file list was made.

The report finds runs at any depth with `run_finder.py`, the same code the
scanners and the validator's runs list use, so it works on the tool team
layout (`sft_mix/xlam_v2`), on the UI's catalog runs three folders deep
(`medpsy/<group>/<dataset>`), and on a single run. It checks only finished
runs. An unfinished run, and a stray `.jsonl`, are listed as "not checked" and
don't fail the report.

A scanner whose results folder isn't given is reported as "not run", never
as passed. The overall verdict covers only the scanners that ran.

### 2. Every row

*Scans made with the current code (Phase 1). Earlier results only have what
Part 5 lists.*

| Scanner | Counts rows per file | Checked automatically |
|---|---|---|
| Regex PII | Yes, in `stage1_scan/input_files.json` | Stage 1 fails if a file's rows scanned differ from its line count, or if per-file findings differ from the merged hits. The report compares each file's `rows_read` with the conversion. |
| NER PII | Yes | Yes. Its last step fails if any file's row count differs from that file's line count. The report compares each file's `rows_read` with the conversion. |
| Toxicity | Yes, for every row (`rows_read`). The older `rows` still counts only rows that had text. | The report compares each file's `rows_read` with the conversion |
| Duplicates | Yes, in `summary.json` `per_file` | Fails if rows with text per file don't add up to the rows hashed. The report compares each file's `rows_read` with the conversion. |

For every scanner and every file, the report also checks that rows with text
plus rows without text equal rows read. It uses these per-file counts and not
the older toxicity totals, so a row with no text is never mistaken for a
missed row.

### 3. Text inside the row

Every scanner now splits its rows into `rows_with_text` and
`rows_without_text`, per file. What counts as text is each scanner's own
rule, unchanged:

- **Regex PII:** the text it matches on is empty.
- **NER PII:** `iter_record_texts()` yields no message.
- **Toxicity:** no message to score. A plain `text` field is always scored,
  even an empty one, so such a row counts as having text here.
- **Duplicates:** the compared part is empty (`rows_without_text`). Blank
  lines aren't rows and aren't counted.

The coverage report uses these counts (Phase 3), so a row with no text can't
look like a missed row there.

In today's tool team data every row has some text, so this didn't show up.
It will on other data.

The conversion's `text_coverage` number uses the same text reading as the
regex PII scanner, so it's a good early warning. But nothing measures text
left in `metadata` (see Part 2).

### 4. Between stages

- **Regex PII:** stage 2 drops matches by rule, on purpose. The dropped ones
  aren't saved separately, but you can find them by comparing the stage 1
  and stage 2 files. Stage 3 checks it wrote one row for every stage 2 match,
  and fails otherwise.
- **Toxicity:** stage 2 should judge every message stage 1 flagged. With the
  current code, stage 2 stops on an unreadable line and names the file and
  line, and the slice fails (no `_SUCCESS`) unless verdicts + errors +
  unknowns equal the number stage 1 flagged. In the existing run (made
  before this check) the numbers do match in all 8 slices (4,088 flagged,
  4,088 judged), but I checked that by hand for this doc. 102 messages
  couldn't be judged (guard errors, most likely because the conversation was
  too long for the model). They're recorded, and someone needs to decide
  what they mean.
- **NER PII:** each piece of work marks itself done. The last step refuses to
  run if any piece isn't done, unless you tell it to allow that.

**"Finished" markers.** With the current code every scanner writes its
marker last, and removes an old one when it starts again:

- regex PII: `_SUCCESS` in each of `stage1_scan`, `stage2_validated` and
  `stage3_extract` (the array job's `_CPU_SUCCESS` is unchanged);
- NER: `_SUCCESS` for each piece of work, and one for the whole run (not
  written if the last step was told to allow missing pieces);
- toxicity: `_DETOXIFY_SUCCESS` after stage 1, `_SUCCESS` after stage 2 and
  its count check. Stage 1 writes `detoxify_summary.json` only when it
  finished, and stage 2 is only reused from a stage 1 that finished;
- duplicates: `_SUCCESS`, as before.

The coverage report reads these markers (Phase 3). A stage, slice or piece of
work that didn't finish, or hasn't run at all, is reported by name ("Slice
shard_00000, stage 2 didn't finish", "1 of 3 pieces of work didn't finish"),
and its files aren't also reported as missing. For toxicity it also runs
`check_guard_counts()` again on every slice, because that check's result
isn't saved anywhere else. Toxicity guard errors are shown from
`guard_results/summary.json` and never fail the report; Phase 4 decides what
they mean.

### Protections that already exist

- Every scanner's results folder has a `run_identity.json`, so one results
  folder can't mix two different scans.
- The skipped-rows check runs in all four scanners.
- The duplicates scanner and the toxicity wrapper script refuse to write
  results inside the folder being scanned. Regex PII and NER don't check
  this.

### Verdict

- **Files:** for scans made with the current code, all four scanners record
  the files they read, and the coverage report checks them against the
  finished runs by SHA-256.
- **Rows:** for scans made with the current code, every scanner records
  per-file counts and checks them against what it read, and the coverage
  report compares them with the conversion for every file. The existing
  results keep the weaknesses above (regex PII and duplicates have totals
  only).
- **Text inside rows:** counted per file by every scanner (current code), but
  text in `metadata` is never scanned or measured.
- **Between stages:** checked for regex PII and NER. Checked for toxicity in
  scans made with the current code, not in the existing results. The coverage
  report checks that every stage finished, and re-checks toxicity's stage 2
  against stage 1.

---

## Part 4: Following a finding back to the Original row

### Link 1: finding to Prepared row

Every finding names a Prepared file (relative to the folder that was
scanned) and a line number. The run folder is the nearest folder above that
file that has a `summary.json`; the validator already finds it this way.

**The risk with "file + line":** if a Prepared folder is deleted and
converted again (the UI offers "Delete and start fresh"), the same file and
line can hold a different row. So a finding can only be trusted if we can
check that the line is still the one the scanner read.

| Scanner | What a finding stores | Can we check it's still the same line? |
|---|---|---|
| Regex PII | file, line, line fingerprint, the value | Yes. Stage 3 already does, and saves the whole line. |
| NER PII | file, line, line fingerprint (`source_sha256`), message number, character positions, the text found | Yes, for scans made with the current code: the trace tool (Phase 6) checks it. Earlier results have no line fingerprint. |
| Toxicity | file, line, line fingerprint, message number, the whole Prepared row | Yes: the trace tool checks it. |
| Duplicates | file, line and line fingerprint (`source_sha256`) of the row, and of the row it duplicates | Yes, for scans made with the current code: the trace tool checks both rows. Earlier results have no line fingerprint. |

### Link 2: Prepared row to Original row

This is the origin note from Part 1. It works for every Prepared row made
with today's code, except rows read from the Hub.

Some findings carry the whole Prepared row, so the origin note comes with
them and you can read the Original location straight from the finding:

- toxicity's `flagged.jsonl` and `guard_results/all.jsonl`;
- regex PII's `pii_extract.csv` (the `whole line` column);
- NER's `llm_candidates.csv` (but not `all_spans.jsonl`, which has the line
  fingerprint but not the line);
- duplicates' `filtered/` and `filtered_out/` copies.

### Link through a filter step

*Added after Phase 4 of `BACKEND_IMPLEMENTATION_PHASES.md`.* The filter step
removes whole rows from finished runs and writes the rest as new runs. A
finding on a filter run's data walks back one more hop before it reaches the
Prepared run: from the kept line, through the filter run's line map, to the
parent line, which is checked by fingerprint.

See "Filter runs" below for the layout. Rows are never changed, so a kept
line is the same bytes as its parent line, and its origin note is still there.

### Filter runs

`modelsafety/filter/filter_rows.py` (a command, a CPU-only job at
`launchers/slurm/filter_rows.sbatch`, and the Python function `filter_runs()`)
takes a folder of finished runs plus one or more scanners' results on exactly
those runs. For each run it writes a run at the same relative path under the
output folder:

```text
<output>/<run>/
  chunk-0/data_000000.jsonl            kept lines, byte for byte (also written when empty)
  line_map/chunk-0/data_000000.ndjson  one entry per kept line, in the same order
  removed_rows.ndjson                  one entry per removed row
  run_identity.json, summary.json, _SUCCESS
```

- **Line map and removed rows.** Both start every entry with `_parent`:
  the parent file (relative to the parent run), the line, the byte offset and
  the line fingerprint. A removed row also has `reasons` and the whole parent
  line (`line`, as text). Scanners refuse any file whose first line starts
  this way, the same as skipped rows (`origin.py`), and both files end in
  `.ndjson` so they are never picked up as data.
- **Reasons.** `regex_pii`, `ner`, `toxicity_unsafe`, `toxicity_controversial`,
  `toxicity_guard_error` (a guard error, or a verdict the guard didn't give),
  `duplicate`, and `duplicate_of_reference` (cross mode). Each reason lists
  its findings: regex PII category and value; NER label, tier, message
  number, positions and confidence; toxicity message number, role, verdict or
  error, categories and Detoxify score; the location and fingerprint of the
  row a duplicate duplicates. A row removed for two reasons is one entry and
  counts once in `rows.removed`, and under both reasons in `removed_by_reason`.
- **`summary.json`** has a Prepared run's shape where the two overlap (`tool`
  is `filter_rows.py`; `source.rows_read`, `rows.written`, `output.files_written`,
  `checks`), plus `parent`, `scanner_results`, `rules`, `per_file` and
  `removed_by_reason`. `run_identity.json` records the parent run's files with
  their fingerprints, the scanner results and the rules, so a folder can't
  hold two different filter steps.
- **Rules** are set per step and recorded: which toxicity verdicts remove a
  row (default Unsafe and Controversial), whether guard errors count as unsafe
  (`remove`, the default) or stop the filter (`refuse`), regex PII categories
  and NER labels to ignore (default none), and which duplicates modes are
  allowed (default both). NER counts only spans the NER policy marked
  `candidate_eligible`. Regex PII reads stage 3.
- **It refuses to start** (nothing written) unless: every run under the input
  folder finished and nothing was skipped; every scanner result passes the
  coverage checks for exactly those runs (so results from another folder are
  refused); each findings file has the number of findings its scan counted for
  every file; every finding points at a real line with a valid fingerprint;
  the output folder isn't inside or around the input or results folders; and
  each output run folder is empty or already holds this same filter step.
- **While writing**, every line is read once. A line whose fingerprint doesn't
  match what a scanner recorded, a blank line, a skipped-rows entry, or a
  parent file that changed since its run finished stops the filter, names the
  file and line, and leaves no `_SUCCESS`. Afterwards every kept file is read
  again next to its line map, and the removed rows are checked against their
  recorded fingerprints; `_SUCCESS` is written only if all of that passed.

To the other tools a filter run looks like a Prepared run: the run finder, the
scanners and the coverage report read it. The validator's "open run" still
opens only Prepared runs.

### The trace tool

*Added after Phase 6 of `BACKEND_IMPLEMENTATION_PHASES.md`.*

`modelsafety/trace/trace_findings.py` (a command, and the Python function
`trace_findings()`) takes findings from any step of a chain and walks each one
back to its Original row. Give it the results folder of any of the four
scanners (made in `runs` input mode), and a `--filter-root` for each filter
step whose removed rows you want traced too. For each finding it:

1. takes the scanned folder from the scanner's `run_identity.json`
   (`input_root`), and finds the run that holds the finding's file, the way
   the run finder does (any depth, Prepared or filter);
2. opens that line, checks it isn't a skipped-rows or filter entry, and
   compares its fingerprint with the finding's;
3. in a filter run, reads the line map for the line in the parent run and
   repeats from there, with the fingerprint the line map gave;
4. at the Prepared run, reads the row's `_origin` note and looks the Original
   row up with `modelsafety/trace/original.py`, the code the validator's
   "Show original" uses. Each data file is read once per step, and each
   Original file once per run.

It writes one line per finding to `<output-dir>/<source>.ndjson`, in a folder
you name (outside every folder it reads). A line has the finding's own
reference, every hop (run, file, line, whether the fingerprint matched), the
Prepared row, the Original file and position, and a status:

| Status | Meaning |
|---|---|
| `unchanged` | Every fingerprint matched, down to the Original row. |
| `changed` | The Original row is no longer the one that was converted. |
| `not_found` | The Original file or row isn't there any more. |
| `not_supported` | The row can't be looked up: it was read from the Hub, or it is the row a duplicate duplicates in a cross-mode scan (the reference folder isn't a run). |
| `broken_link` | A fingerprint didn't match, or a step couldn't be made (an unfinished run, a missing line map, no `_origin` note). `broken_at_hop` says which hop, and `reason` why. |

The sources are: regex PII stage 1 and stage 3, NER's `all_spans.jsonl` and
`llm_candidates.csv`, toxicity's `flagged.jsonl` and `guard_results/all.jsonl`,
duplicates' `dedup_results.jsonl` (the row and the row it duplicates, each
traced), and each filter step's `removed_rows.ndjson` (traced from the line it
was removed from). `summary.json` has the totals for each source and status;
`_SUCCESS` is written only if every source wrote exactly the findings it read,
and the findings for each file equal the scan's own count.

It refuses to start, writing nothing, if a results folder has no
`run_identity.json` or was made in `folder` input mode, if a filter root holds
anything but finished filter runs, or if the output folder is inside or around
a folder it reads. Exit code: 0 if everything is `unchanged` or
`not_supported`, 1 if some are `changed`, `not_found` or `broken_link`, 2 if it
refused or a check failed.

### The per-row status

*Added after Phase 7 of `BACKEND_IMPLEMENTATION_PHASES.md`.*

The trace answers "where did this finding come from?". The per-row status
answers the other way round: "what do we know about this Prepared row?".
`modelsafety/trace/row_status.py` (a command, and the Python function
`build_row_status()`) takes the output folder of the last filter step of a
chain. It follows each filter run back to its Prepared run through the runs'
own `run_identity.json` and `summary.json`, reads every step's line maps and
removed rows, reads the findings of every scanner the steps used, and maps each
one to its Prepared row. For each Prepared data file it writes:

```text
<output>/<run>/rows/<file>.ndjson    one entry for every Prepared line, in order
<output>/<run>/rows/<file>.offsets   the byte offset of every entry
```

plus `run_identity.json`, `summary.json` and `_SUCCESS`. An entry has:

- **`prepared`:** run, file, line, byte offset and fingerprint; and `origin`,
  the short origin note (null if the row has none).
- **`steps`:** one item for every step. The row was `kept` (and where it is in
  that step's output), `removed` (which entry of `removed_rows.ndjson`, and its
  reasons, as that file has them), or `not_reached` (an earlier step removed
  it). For each scanner the step used: `scanned` with its findings, `no_text`,
  or `not_reached`. The findings carry what is needed to highlight them (NER:
  label, message number, start, end, confidence, tier, eligible; regex PII:
  category and value; toxicity: message number, role, verdict or error,
  Detoxify's score; duplicates: the Prepared location of the other row, and
  `duplicated_by` on the row that is copied) and `at`, the file and line in the
  scanner's own results.
- **`final`:** where the row is in the last filter run (`kept`), or the step
  that removed it.
- **`not_scanned`:** every scan that didn't see the row, as `no_text` (the
  scanner read the row but found no text by its own rule) or `removed_earlier`.
  **A row is clean for a scan only if it is `scanned` with no findings.** The
  UI should say "not scanned" for the rest, never "clean".

Because the status files are in row order and each has an offsets file (one
21-byte line for each entry), one row or a page of rows is read with two seeks,
without reading the file: `modelsafety/trace/row_status_read.py` does this with
the standard library only (`read_row()`, `read_rows()`, and a `show` command).
It refuses a folder with no `_SUCCESS`, and a run or file that `summary.json`
doesn't list.

Before it writes anything it checks, and refuses (exit code 2, no `_SUCCESS`)
if any of this fails: every Prepared file is the one its run wrote; at each
step every kept line has the fingerprint of the line it came from, at the byte
offset its line map gives; every line of the run before is kept or removed,
once; each step's counts per file and per reason equal the filter run's
`summary.json`; for each scanner and file, rows with text and without text
equal the scan's own counts; and findings equal the scan's own count, with
every finding on a line with the right fingerprint. Then it reads what it wrote
back. See "What Phase 7 ran" for what was checked on the pilot, and what hasn't
been measured at full size.

---

## Part 5: Where the tool team's results stand

Rewritten after Phase 8, the full run (9 to 12 Oct 2026). Option A was taken:
everything was converted again with today's code and scanned again. The
chain is duplicates, filter step 1, then regex PII, NER and toxicity side by
side, then filter step 2. Prepared runs are in `output/prepared/tools_team/`
and everything else is in `output/tool_team_chain/`.

| Step | Numbers | Coverage and checks |
|---|---|---|
| Conversion | 65 runs, 1,174 files, 1,143,300 rows, nothing skipped, every run has `_SUCCESS` | `verify_sources.py`: 65 of 65 match their independent counts. The same row count as the 22 Sep conversion, for every dataset. |
| Duplicates (Prepared runs) | 283,638 rows marked (24.81%), 41 without text | Passes |
| Filter step 1 | 283,638 removed, 859,662 kept. `sft_mix/xlam_irrelevance_v2` has no rows left. | 65 of 65 filter runs pass |
| Regex PII (step 1 output) | 688,458 matches in stage 1, 64,246 in stage 3, on 41,472 rows | Passes |
| NER (step 1 output) | 1,107 of 1,107 work units, 6,426,482 spans, 1,809,936 eligible, on 313,714 rows | Passes (after the empty-file fix, see below) |
| Toxicity (step 1 output) | 8 slices, 3,306 flagged, 3,219 judged, 87 guard errors, 0 unknown | Passes |
| Filter step 2 | 315,874 removed, 543,788 kept | 65 of 65 filter runs pass |
| Trace | 9,350,260 findings, all `unchanged` (none `changed`, `not_found`, `broken_link` or `not_supported`) | `_SUCCESS` |
| Row status | One entry for every Prepared row | `checks.passed`, no warnings, `_SUCCESS` |

Rules for step 2: the Phase 4 defaults, with nothing ignored. NER removed
313,714 rows and regex PII 41,472 (a row can have both). Toxicity removed
1,360 rows: 812 Unsafe, 461 Controversial and 87 guard errors.

The counts add up for all 65 runs: 1,143,300 Prepared rows = 283,638 removed
at step 1 + 315,874 removed at step 2 + 543,788 kept.

What this means:

- **Every row can be followed.** Any finding or removed row traces back
  through the line maps to its Prepared line and its Original row, with the
  fingerprint checked at every hop.
- **Clean data:** `output/tool_team_chain/filter_step2/`, 543,788 rows (47.6%
  of the Prepared rows). The NER rule alone removes 36.5% of the rows that
  step 1 left. It can be narrowed with `--ner-ignore-label`, and filter
  step 2 re-run without scanning again.
- **Not removed, not clean:** text in `metadata` is not scanned (decision 3).
- **The old data is still on disk** (`output/tool_team_converted`,
  `tool_team_pii_run`, `tool_team_ner_run`, `tool_team_toxicity_run`,
  `tool_team_dedup_run`, `tool_team_dedup_pilot` and
  `tool_team_regurgitation`). It was not deleted. The old scan results can't
  be traced, because they were made before origin notes and file records
  existed.

---

## Part 6: What to build

The work falls into four groups. Sizes are rough: small is a few hours,
medium is a day or two, large is more than that.

### A. Knowing what got scanned

1. **Every scanner counts the same things, per file. (Done in Phase 1.)** For each file: rows
   read, rows with text, rows with no text, findings. (Small, for each
   scanner.)
   - Regex PII stage 1: add per-file counts (today it has one total).
   - Toxicity stage 1: count rows with no text too, so its row count can
     equal the conversion's.
   - Duplicates: add per-file counts.
   - NER already has rows and messages per file; add rows with no text.
2. **Check toxicity's second stage. (Done in Phase 1.)** Stage 2 stops on an unreadable line
   instead of skipping it, records how many records it read, and fails (no
   `_SUCCESS`) if the number of verdicts plus errors isn't the number stage 1
   flagged. (Small.)
3. **Make the coverage report cover everything. (Done in Phase 3.)** It
   checks all four scanners, finds runs at any depth, compares every file's
   rows with the conversion, reads the finished markers and writes a JSON
   report. (Medium.)
   - Find conversions at any depth, the way the validator's runs list does,
     so it works on the UI runs root.
   - Include duplicates.
   - Compare regex PII and duplicates row counts, once step 1 gives them.
   - Look at the "finished" markers and say plainly when a scan, slice or
     piece of work didn't finish.
   - Compare toxicity stage 1 and stage 2 counts.
   - Compare row counts against rows with text, so rows with no text don't
     look like missed rows.
4. **Don't scan unfinished conversions. (Done in Phase 2.)** Scanners take
   their file list from the conversions' own `output.files_written`, only
   from runs with `_SUCCESS`, and skip and report everything else in
   `scan_inputs.json` (see Part 2, "How they choose files"). Each file's
   fingerprint is checked against its run. `--input-mode folder` keeps the old
   read-everything behaviour for medpsy. (Medium: the file-finding code was
   in four places.)

### B. Tracing a finding back to the Original row

5. **A trace tool. (Done in Phase 6; see Part 4, "The trace tool".)** It reads any scanner's findings and, for each one:
   - opens the Prepared line and checks it's the one the scanner read (line
     fingerprint where there is one, otherwise the file's fingerprint);
   - reads the origin note;
   - looks up the Original row and checks its fingerprint.

   It writes one file with a line per finding: the finding, the Prepared
   file and line, "Prepared line still the same?", the Original file and
   position, and `unchanged` / `changed` / `not_found` / `not supported`.
   Then it prints totals.

   The lookup code already exists in the validator (`validator/backend/runs.py`
   `lookup()` and `detect.py` `read_original_row()`). It should be shared,
   not copied, so the UI and the tool can't disagree. (Medium.)
6. **Line fingerprints in NER and duplicates findings (done in Phase 1),** so link 1 can be
   checked exactly for them too, like regex PII and toxicity. Check what
   reads those files first (`llm_validate.py` for NER, the duplicates report
   scripts). (Small.)

### C. Text the scanners can't see

7. **Measure the text left in `metadata`** at conversion time: how many
   written rows have text in `metadata`, how much, and in which fields. This
   goes into `summary.json` and the run view. (Small.) Then decide what to
   do about it (see below).

### D. Later

8. **One final answer per Prepared row for the tool team. (Done in Phase 7;
   see Part 4, "The per-row status".)** Clean, or flagged and by which
   scanners. `modelsafety/decisions/quality_filter_finalize.py` already does
   this very carefully for the older medpsy layout (regex PII and toxicity
   only, with every line checked by fingerprint). The tool team needed the
   same thing for its own folders, including NER and duplicates, and for
   chains. (Large.)
9. **"Show original" for rows read from the Hub.** Only worth doing if we
   start converting Hub dataset IDs directly. (Small to medium.)

### Suggested order

1, 2 and 6 first: they're small, and they make each scanner's own record
complete. Then 3, which puts it all in one report (done in Phase 3). Then 5,
the trace tool (done in Phase 6). 4 is done (Phase 2); 7 can go any time. 8 last
(done in Phase 7).

Whichever way you choose in Part 5, do it after 1, 2 and 6, so any new scans
are made with the complete records.

### Decisions needed from you

1. **Text in `metadata`:** only measure it (item 7), or also scan it? To
   scan it, either the scanners also read chosen `metadata` fields, or the
   conversion moves those fields into `messages`. The second changes what
   `messages` means for training, so it's not just a scanning question.
2. **The tool team's existing results:** option A (scan again) or option B
   (try to keep the old results) from Part 5.
3. **Toxicity guard errors** (102 today): count them as unsafe (the medpsy
   rule), or re-run them with a longer limit.
4. **An unfinished conversion under a scanned folder:** settled in Phase 2.
   The scan skips it and reports it (item 4). A stray `.jsonl` that no run
   lists is skipped and reported the same way.

---

## Checking things yourself

- Coverage across the conversions and all four scans (read-only, takes
  seconds). `--root` is the folder that was scanned. Give only the results
  folders you have; any you leave out are reported as "not run". `--json-out`
  also writes the report as JSON, and it must be outside the scanned folder
  and every results folder:

  ```bash
  python3 modelsafety/results/coverage.py --root output/prepared \
    --pii-run-dir <regex PII results> --ner-run-dir <NER results> \
    --toxicity-run-dir <toxicity results> --dedup-run-dir <duplicates results> \
    --json-out <somewhere/coverage.json>
  ```

  It exits with 0 only if every scanner that ran passed.

- Which files a scan would read under a folder, and what it would skip
  (read-only, nothing is written):

  ```bash
  python3 modelsafety/contract/run_finder.py files --scan-root output/prepared
  ```

  The file list goes to stdout and the skipped runs and files, with reasons,
  to stderr. Add `--input-mode folder` for the old read-everything list.

- Run a filter step. It writes new runs into `--output-root` (a new folder,
  outside the input folder and every results folder) and never changes the
  input folder. It refuses before writing anything if a check fails:

  ```bash
  python3 modelsafety/filter/filter_rows.py --input-root output/prepared \
    --output-root <new folder> \
    --pii-run-dir <regex PII results> --ner-run-dir <NER results> \
    --toxicity-run-dir <toxicity results> --dedup-run-dir <duplicates results>
  ```

  Add any of `--regex-ignore-category`, `--ner-ignore-label`,
  `--toxicity-remove-verdict`, `--toxicity-guard-errors remove|refuse` and
  `--dedup-mode` to change the rules.

- Trace findings back to their Original rows. Read-only on everything it
  reads; it writes into `--output-dir`, a new folder outside them. Give the
  results of any scanner you have, and a `--filter-root` for each filter step:

  ```bash
  python3 modelsafety/trace/trace_findings.py --output-dir <new folder> \
    --pii-run-dir <regex PII results> --ner-run-dir <NER results> \
    --toxicity-run-dir <toxicity results> --dedup-run-dir <duplicates results> \
    --filter-root <filter step output> [--filter-root <next filter step output>]
  ```

  It needs the packages in `modelsafety/readers/.venv` (pandas, pyarrow,
  datasets), so run it with `modelsafety/readers/.venv/bin/python`.

- Write the status of every Prepared row of a chain. Give it the output of
  the last filter step; it finds the earlier steps and the scanner results
  itself. It writes into `--output-dir`, a new folder outside everything it
  reads, and uses the same Python as the trace:

  ```bash
  modelsafety/readers/.venv/bin/python modelsafety/trace/row_status.py \
    --filter-root <last filter step output> --output-dir <new folder>
  ```

  It exits with 0 only if every check passed. Then read one row, or a page,
  without reading the whole file:

  ```bash
  python3 modelsafety/trace/row_status_read.py show --status-dir <that folder> \
    --run <run path> --file <Prepared file in the run> --line N [--count K]
  ```

- Toxicity state per slice, and guard errors:
  `./scripts/tool_team_toxicity_scan.sh report`
- Duplicates: `./scripts/tool_team_dedup_scan.sh report`
- One Prepared run's counts, skipped rows and "Show original": open its
  folder in the validator's Prepared run view.
