# Dataset Scan Service: Implementation Phases

This document describes a new service, built fresh, whose job is simple: **take a
dataset, scan it, show the results.** It scans for duplicates, PII and toxicity, shows
what it found, and lets the user decide what to drop.

It is written to be handed to a model one phase at a time. Give the model this whole
document and say "Do Phase N". Every phase lists what to read first, what to build,
what not to do, and how to tell it's done.

---

## 1. How to use this document

- **One phase at a time, in order.** Each phase assumes the phases before it are done.
- **Planning model (e.g. Opus):** read sections 1 to 8, then the phase. Read every file
  in the phase's "Read first" list. Write a short plan: the files to add, change or
  delete, the endpoints, the data shapes, and anything in the phase that turned out to be
  wrong or unclear. Stop and ask if a decision in section 7 doesn't fit what the code does.
- **Executing model (e.g. Sonnet):** follow the plan, keep to the rules in section 5, and
  check every item under "Done when".
- **After a phase:** tick it in the status table (section 8) and add a line under
  "Notes from earlier phases" (section 10) for anything the next phase needs to know.

---

## 2. What we are building

**This is a fresh build.** The system isn't live and nothing in it has to be kept:

- The current UI and all its tabs are replaced by a new one.
- The concept of **chains** goes away completely. There are no chains, no fixed pipeline
  and no steps that depend on each other.
- Old outputs (conversions, scans, chain folders under `output/`) don't need to be kept.
  Datasets are converted and scanned again in the new service.

The old code is still useful as **reference**: how conversions, scans, SLURM jobs and
results are read today. The phases point at it so the logic can be moved into the new
service. Only the original training data (the datasets themselves) must never be touched.

### The model in one line

**Data → scan → results.** The top level stays simple. Anyone who wants detail can drill
down into it.

1. The user sees every registered dataset, each under one **team**, and can register more.
2. The user opens a dataset to see what's in it, and how it looks after conversion.
3. The user picks one or more datasets and runs a **scan**: duplicates, PII or toxicity.
   Every scan is independent, and dedup is not required first.
4. The user sees each dataset's **results**: numbers, a coverage check, and the flagged
   rows themselves.
5. The user decides what to drop. They select rows (or all rows matching a filter), and
   the service writes a **filtered version** as a new dataset. Scans never remove
   anything by themselves.

### Today vs. target

| Area | Today (being replaced) | Target |
|---|---|---|
| Datasets | Hard-coded catalog (`sources.py`) plus a "Custom path" box that isn't saved | A saved dataset registry. Every dataset belongs to one team. |
| Inspect | "Inspect a dataset" tab, format picker, preview | A dataset page: overview, rows, conversion |
| Conversion | Prominent: Start panel, batch review | Set up once per dataset. "Convert only" is hidden under "More" |
| Scans | Only as steps of a chain, in a fixed order | Select datasets, pick a scan, run it. No chains. |
| Settings | Few | Dedup scope, PII method and NER categories, toxicity threshold and guard categories |
| Results | Per chain stage | Per dataset, per scanner. Running again replaces the old result. |
| Removing rows | Automatic filter steps driven by rules | The user selects rows and makes a filtered version |
| Jobs | Conversions and chain steps, separately | One "Activity" page for everything running |

---

## 3. Words we use

| Word | Meaning |
|---|---|
| **Team** | The owner of a dataset. Fixed list (D1): BotPsy, BitPsy, MedPsy, VisionPsy, TranslatePsy, TranscribePsy. |
| **Dataset** | One registered entry: a team, a name, the absolute path of the original data, and its saved conversion settings. |
| **Prepared version** | The converted rows of a dataset (the output of `prepare_rows.py`). Every scanner reads this. A dataset has one current Prepared version; converting again replaces it (D6). |
| **Scanner** | One kind of scan: `dedup`, `pii_ner`, `pii_regex` or `toxicity`. |
| **Stage** | A phase inside a scanner. For example toxicity has "Detoxify screen" then "Qwen3Guard judge". |
| **Scan** | One start of one scanner over one or more datasets, with its own folder and record. |
| **Result** | What a finished scan found for one dataset. Each (dataset, scanner) pair has at most one result; running again replaces it (D5). |
| **Finding** | One thing a scan flagged: a duplicate row, a PII span, or a toxic message. |
| **Coverage check** | The existing `modelsafety/results/coverage.py` report: proof that a scan read exactly the files and rows of its input. |
| **Filtered version** | A new dataset made by dropping chosen rows from a Prepared version. Its rows are copied byte for byte, and it keeps links back to the original rows. |

---

## 4. The target experience

### User flows

1. **Find or register a dataset.** Datasets page → filter by team or search, or
   "Register dataset" → choose the team → paste a path → the service detects the format
   → name it → it's saved.
2. **Look inside and set up conversion.** Open the dataset → Overview (format, files,
   size, rows, fields, sample rows) → Conversion tab (pick format and field mapping, see
   the converted preview, start the conversion). This is done once, and the settings
   are saved.
3. **Scan.** Datasets page → tick one or more datasets → choose an action:
   - **Find duplicates:** across all chosen datasets together (the default), or within
     each dataset separately.
   - **Scan for PII:** NER (the default) with a category picker, or rule-based (regex).
   - **Scan for toxicity:** the Detoxify threshold (default 0.5), and which Qwen3Guard
     categories and verdicts count as flagged.

   For each chosen dataset, the dialog shows whether it's converted (D3):
   - **Not converted:** the service converts it first, then scans. These are two
     separate jobs. The scan starts when the conversion finishes.
   - **Already converted:** "Use the existing converted data" (the default), or
     "Convert again", which deletes the converted data and its results, converts again,
     then scans.

   Then confirm (GPU scans ask again), and watch both jobs on the Activity page.
4. **Review.** Dataset → Results → a scanner → summary numbers, coverage verdict,
   breakdown by category → the list of flagged rows → open a row to see why it was
   flagged.
5. **Act.** Select rows (some, or all matching the current filter) → "Drop selected" →
   a filtered version appears as a new dataset in the same team, linked to its parent.

### Pages (these are the only pages)

| Page | What it shows |
|---|---|
| **Datasets** (home) | Team tabs or filter, then a table: name, format, rows, whether converted, and a status chip per scanner. Search, multi-select, the action bar, and "Register dataset". |
| **Dataset** | Header (team, name, path, current Prepared version). Tabs: Overview, Data, Conversion, Results, History. |
| **Scan dialog** | Chosen datasets, scanner options, an "Advanced" section (resources, exact command), the cost, Start. |
| **Result** (dataset × scanner) | Summary, coverage, breakdowns, the flagged-rows table with checkboxes, and the row detail. |
| **Activity** | Every conversion, scan and filter job, newest first: state, datasets, time, Cancel, log tail. |

Model validation (for example the regurgitation tool) is out of scope for now. The
navigation should leave room for it.

---

## 5. Rules every phase keeps

**Safety**
- Never write to, move or delete **original data** (the paths datasets point at). Many of
  them are shared training data, such as `/home/shared/...`.
- The service writes only under the **service root** (section 6), plus the scan wrappers'
  own job logs under the repo's `logs/`.
- Deletion by code is limited to the replacements this document defines (D5, D6, D2).
  It happens only inside the service root, after checking the path is inside it.
- While building, deleting old code and old `output/` folders is allowed, as the phases
  say. Show the list of what will be deleted before doing it.
- Every command is an argument list run with `shell=False`. Every setting from the UI is
  checked against a fixed list, a number range or a strict pattern before use. User
  text never reaches a shell.
- GPU scans need their own confirmation.
- Paths and IDs from requests are checked: paths must be absolute and resolved, IDs
  must match a strict pattern, and any read or write must be inside the folder it
  belongs to.
- Every value read from files renders as plain React text, never as markup.

**Behaviour**
- List pages read only small files (`summary.json`, `_SUCCESS`, records, indexes), never
  whole data files.
- Job state is read live from SLURM (`squeue`, `sacct`) and from the files a job writes,
  never stored, as `validator/backend/steps.py` does today.
- The command shown in the UI is exactly the command that runs.
- The scan tools in `modelsafety/`, `scripts/` and `launchers/` are what the service runs.
  Extend them with new flags if needed, and never change what an existing flag does.

**Code**
- Match the existing style: small modules, plain names, comments only for constraints
  the code can't show.
- Backend: FastAPI and pydantic in `validator/backend/`, started by `validator/run.sh`.
- Frontend: React and TypeScript with plain CSS in `validator/frontend/`. Add no new
  dependencies unless the phase says so.
- Don't write tests unless asked. Check the frontend with `npm run build` and
  `npm run lint`. Check the backend by running `./validator/run.sh` and trying the
  endpoints and pages by hand.

---

## 6. Where things live on disk

```text
<repo>/output/service/                 the service root ($VALIDATOR_SERVICE_ROOT)
  registry/
    datasets.json                      every registered dataset
  datasets/<dataset-id>/
    prepared/<version-id>/             the current Prepared version (prepare_rows.py output,
                                       or filter_rows.py output for a filtered dataset)
    drop_list.jsonl                    filtered datasets only: the rows dropped from the parent
    _jobs/  _logs/                     conversion and filter job records and logs
  scans/<scan-id>/
    scan.json                          what was started: scanner, datasets, Prepared versions read, settings, jobs
    inputs/                            files the scan is given (file manifest, NER policy.json, ...)
    results/                           the scanner's own output folder
    coverage.json                      the coverage report (Phase 8)
    index/<dataset-id>.jsonl           the flagged rows of each dataset (Phase 9)
    _logs/
```

Every Prepared version lives under `datasets/`, so a scan over several datasets always has
one common input root (`<service root>/datasets`).

---

## 7. Decisions taken (confirm or change before the phase that uses them)

| # | Decision | Default | Used in |
|---|---|---|---|
| D1 | Teams | A fixed list in one backend constant, served to the UI: **BotPsy** (Tool team), **BitPsy** (LowBit), **MedPsy**, **VisionPsy**, **TranslatePsy**, **TranscribePsy**. Every dataset has exactly one team. Adding a team means editing the list. | 1, 2 |
| D2 | Unregistering a dataset | It removes the entry, the dataset's folder under `datasets/` (its Prepared version), and its results. The original data is never touched. The UI asks for confirmation. A scan shared with other datasets (dedup) stays while any other dataset still uses it. | 1, 2, 4 |
| D3 | Converting as part of a scan | Starting a scan checks each chosen dataset. **Not converted:** a conversion job is started with the saved conversion settings, and the scan is started as a second job once the conversion finishes. **Already converted:** the user picks "Use the existing converted data" (default) or "Convert again" (delete the current Prepared version and its results, convert, then scan; asks for confirmation). A dataset with no saved conversion settings can't be converted automatically: the dialog marks it "set up conversion first" and links to its Conversion tab. If the conversion fails, its scan is not started and is shown as "not started: conversion failed". | 3, 4, 5 |
| D4 | Many datasets chosen | PII and toxicity: one scan **per dataset** (rows are independent, so results are the same). Dedup: the user picks **across the chosen datasets** (default: one scan over all of them, so rows are compared between datasets too) or **within each dataset** (one scan per dataset, so a dataset is only compared with itself). | 4, 5, 6, 7 |
| D5 | Running a scan again | The new scan writes a new folder. When it finishes, it becomes the dataset's result for that scanner, and the old scan folder is deleted once no dataset uses it. If the new scan fails, the old result stays. | 4, 8 |
| D6 | Converting a dataset again | Whether from the Conversion tab or "Convert again" in the scan dialog, the current Prepared version is deleted first, along with scans that only read it, and then the new conversion starts. The dataset shows "not converted" and "not scanned" until the new jobs finish. A Prepared version that a filtered dataset was made from is not deleted (it's set aside and kept), because the filtered dataset needs it to trace rows back. The confirmation says so. | 3, 4, 5 |
| D7 | NER categories | Chosen before the scan and written into the scan's own `policy.json`. Pre-selected: the labels the current `policy.json` enables (direct identifiers and person-linked). Contextual labels are off by default. A chosen contextual label gets threshold **0.7** unless decided otherwise. | 6 |
| D8 | Qwen3Guard categories and verdicts | Qwen3Guard always judges every category. The user's choice of categories and verdicts (default verdicts: Unsafe and Controversial) decides what counts as **flagged** when results are read, so it can be changed later without re-running. The Detoxify threshold decides what Qwen3Guard sees, so changing it needs a new scan. | 7, 8, 9 |
| D9 | A guard error | Counts as flagged. | 7, 9 |
| D10 | What dedup does with duplicates | By default it only **flags** them, and nothing is dropped by the scan. Each duplicate is listed with the copy it matched. The copy that counts as the original is the earliest row in path order (D15 adds other choices), and the UI says so. The user chooses what to drop afterwards (Phases 9 and 10), unless they turned on the automatic drop (D17). | 5, 9, 10, 11 |
| D11 | A filtered version | It is registered as a new dataset in the parent's team, with `parent` set, named "<parent name>, filtered (N removed)". Its Prepared version is the filter output, so it can be scanned again. | 10 |
| D12 | Starting datasets | The registry starts with the entries of today's `validator/backend/sources.py`. Its "Tools team" becomes **BotPsy** and "medpsy" becomes **MedPsy**. After that the registry is the only source, and `sources.py` is deleted. | 1 |
| D13 | Product name | A placeholder ("Data Safety Scanner") until the team picks one. | 2 |
| D14 | Users and access | Unchanged: one user, localhost, no login (see `validator/run.sh`). | all |
| D15 | Which copy counts as the original (dedup across datasets) | Three choices: **first by path** (default, as today); **priority order** (the user orders the chosen datasets, and a row in a higher one wins); **never flag rows in these datasets** (protected datasets go first in the order, and their own flags are not shown or dropped). At least one chosen dataset must stay unprotected. | 11 |
| D16 | How strict a match must be (dedup) | Three presets over the MinHash settings, all with 256 permutations and 60-character n-grams: **Strict** (16 bands × 16 rows, flagged from about 82% similarity), **Standard** (default, 32 × 8, about 62%), **Loose** (64 × 4, about 32%). The percentage is the similarity at which a pair has a 50% chance of being flagged (`collision_probability_midpoint` in `run.py`). These are starting points: check them on real data before relying on them. | 11 |
| D17 | Dropping duplicates automatically | A dedup scan option, **off** by default. When on, as soon as the scan finishes **and its coverage check passes**, a filtered version is made for every dataset that has duplicates, dropping all of its flagged duplicates (D15 decides which ones). If coverage fails, nothing is dropped and the result says why. The filtered versions are normal ones (D11), so the parents are untouched. | 11 |

---

## 8. Phases at a glance

| Phase | Name | Needs | Side | Status |
|---|---|---|---|---|
| 1 | Dataset registry and teams | none | Backend | ☑ |
| 2 | New app and Datasets home | 1 | Frontend | ☐ |
| 3 | Dataset page: overview, data, conversion | 2 | Both | ☐ |
| 4 | Scan framework, with the duplicates scan | 3 | Backend | ☐ |
| 5 | Scan action in the UI, and Activity | 4 | Frontend | ☐ |
| 6 | PII scans: NER and rule-based | 5 | Both | ☑ |
| 7 | Toxicity scan | 5 | Both | ☐ |
| 8 | Results per dataset, with coverage | 5 (and 6, 7 for those scanners) | Both | ☐ |
| 9 | Flagged rows: browse and inspect | 8 | Both | ☐ |
| 10 | Drop rows and make a filtered version | 9 | Both | ☐ |
| 11 | More dedup choices: original copy, match strictness, automatic drop | 10 | Both | ☐ |
| 12 | Remove leftover old code, and write the docs | 1 to 11 | Both | ☐ |

Phases 6 and 7 don't depend on each other and can be done in either order.

---

## 9. The phases

### Phase 1: Dataset registry and teams

**Goal.** A saved list of datasets, each under one team, with the service root and its
folders in place.

**Read first**
- `validator/backend/sources.py`: today's catalog (the starting datasets, D12), availability checks, `children_of` expansion.
- `validator/backend/detect.py`: `resolve_input_path`, `InvalidPath`, format detection.
- `validator/backend/app.py`: how routes and request models are written.
- `validator/backend/runs.py`: how env-overridable roots are written (`runs_root()`).
- `modelsafety/contract/chain.py`: `_write_json` (atomic write: temp file, then `os.replace`). Read it only for the pattern; chains themselves are not used.

**Build**
1. `service_root()`: `$VALIDATOR_SERVICE_ROOT`, or `<repo>/output/service`. Create `registry/`, `datasets/` and `scans/` on first write.
2. The team list (D1) as one constant, served by `GET /api/teams`.
3. A dataset record:
   - `id`, `team` (required, from the list), `name`, `path` (absolute original), `description`, `notes`, `created_at`
   - `conversion`: saved settings (`input_type`, `file`, `split`, `hf_config`, `map`, `require`, `exclude_metadata`), null until set
   - `prepared`: the current Prepared version (folder, rows, finished at), null until converted. Phase 3 sets it.
   - `parent`: null, set in Phase 10
4. A registry module (for example `registry.py`) that reads and writes `datasets.json` atomically behind a lock, and validates:
   - the team is in the list
   - the path is absolute, resolves, exists and is readable
   - the same resolved path (with the same file and split) isn't registered twice
   - the name is 1 to 100 characters (letters, digits, space, `.`, `_`, `-`) and unique within the team
   - text fields have length limits
5. Seed the registry from `sources.py` once, when `datasets.json` doesn't exist yet (D12). Expand `children_of` groups into separate datasets. Keep each entry's description and screening note as its `description` and `notes`. The old group name can be added to the description.
6. Routes:
   - `GET /api/datasets` (optional `team` filter): each dataset with availability (does the path still exist)
   - `GET /api/datasets/{id}`
   - `POST /api/datasets`: register
   - `PATCH /api/datasets/{id}`: team, name, description, notes, conversion
   - `POST /api/datasets/{id}/unregister`: as D2. In this phase only the entry and its `datasets/<id>/` folder exist; Phase 4 extends it to scans.

**Not in this phase:** any UI, conversions or scans.

**Done when**
- The first start creates `datasets.json` with today's catalog, under BotPsy and MedPsy.
- Registering a readable path under any team works and survives a backend restart.
- A relative, missing or unreadable path, an unknown team, or a duplicate is refused with a clear message.
- Unregistering removes the entry. Nothing outside the service root is touched.

---

### Phase 2: New app and Datasets home

**Goal.** A new frontend that opens on the Datasets page and looks like a product. The
old UI is gone.

**Read first**
- `validator/frontend/src/App.tsx` and `components/`: only to see what exists. The new app doesn't keep these pages.
- `api.ts` (fetch and `ApiError` pattern), `format.ts`, `storage.ts`, `components/StateChip.tsx`: small helpers that can be kept if useful.
- `validator/frontend/src/App.css`: colours and variables to reuse or replace.
- `validator/frontend/package.json`, `vite.config.ts`: the build setup stays.

**Build**
1. Delete the old views and the components and helpers only they use. Keep or move any small helper that the new app uses. List the deletions in the plan first.
2. Layout: a top bar with the product name (D13) and a simple navigation: **Datasets** and **Activity**. Activity is a placeholder until Phase 5.
3. Simple hash routing (`#/datasets`, `#/datasets/<id>`, ...) in one small module.
4. Datasets page:
   - team tabs ("All", then the six teams), and search by name or path
   - table: checkbox, name, team, format (if known), rows (from the Prepared version, or "not converted"), availability
   - an empty "scans" column, filled in Phase 5
   - clicking a name opens `#/datasets/<id>`, a placeholder until Phase 3
5. Action bar, shown when at least one dataset is ticked: "Find duplicates", "Scan for PII", "Scan for toxicity". These are disabled with a short note until Phases 5 to 7. Also "More ▾" → "Convert only" (wired in Phase 3).
6. "Register dataset" dialog: team (required) → path → detect the format with the existing `/api/inspect` → name, description → Register → the table refreshes.
7. "Unregister" on a dataset's row menu, with a confirmation that says what will be removed (D2).
8. Style: consistent spacing, cards, a readable table, clear empty and loading states. Plain CSS only.
9. Remove the backend routes only the old views used, such as `/api/sources`, `/api/chains*`, `/api/chain*`, `/api/row*` and `/api/removed*`. Keep the modules themselves (`chains.py`, `steps.py`, `step_start.py`, `rows.py`, `removed.py`, `jobs.py`, `runs.py`, `preview.py`, `detect.py`) until Phase 12: later phases move what they need out of them. Delete `sources.py` once the seed in Phase 1 no longer needs it (move the seed data into the registry module or a seed file).

**Not in this phase:** the dataset page content, and starting scans.

**Done when**
- The app opens on Datasets with team tabs, and search works.
- Registering and unregistering work and persist.
- No old page is reachable.
- `npm run build` and `npm run lint` pass.

---

### Phase 3: Dataset page (overview, data, conversion)

**Goal.** Everything about one dataset in one place: what it is, what its rows look
like, and how it converts.

**Read first** (these are old code, for reference and reuse)
- The old `App.tsx` from git history, if it was deleted in Phase 2: how inspect, format pick, mapping, preview and the Start panel fit together.
- Old components: `FormatPicker`, `RecordView`, `InputFields`, `OutputPreview`, `RowNavigator`, `StartPanel`, `ReviewView`, `PreparedRows`, plus `mapping.ts` and `readable.ts`. Rebuild or adapt them in the new style.
- Backend: `detect.py`, `preview.py`, `jobs.py` (conversion submit, status, cancel; it writes inside the runs root), `runs.py` (`open_run`, skipped rows, origin lookup), and routes `/api/inspect`, `/api/preview`, `/api/jobs/*`, `/api/run*`, `/api/origin`.
- `modelsafety/readers/prepare_rows.py`: what a Prepared version holds (`summary.json`, `_SUCCESS`, skipped rows, `_origin` notes).

**Build**
1. Conversions write to `<service root>/datasets/<id>/prepared/<version-id>/`. Adapt `jobs.py` (its runs-root checks) or wrap it, so conversions go there and their records and logs go to `datasets/<id>/_jobs` and `_logs`.
2. When a conversion finishes and passes its checks, set the dataset's `prepared`.
3. A backend function `start_conversion(dataset, reconvert)` that Phase 4 calls too. It returns the conversion job, so a scan can wait for it.
   - It needs saved `conversion` settings. Without them it refuses with "set up conversion first" (D3).
   - With `reconvert`, it first deletes the current Prepared version (D6), then starts the new conversion. If a filtered dataset was made from that version, the version is set aside and kept instead of being deleted.
   - It refuses while a conversion of the same dataset is already queued or running.
4. `#/datasets/<id>`: a header with team, name, path, availability and current Prepared version (rows, date). Tabs: Overview, Data, Conversion, Results (a placeholder until Phase 8), History.
5. **Overview:** format, number of files, size on disk, row count (from the Prepared summary, or from inspect when not converted), fields or columns, two readable sample rows, and the dataset's notes.
6. **Data:** browse original rows with a row navigator. If converted, browse the Prepared rows too, and the skipped rows with their reasons.
7. **Conversion:** format picker, field mapping, converted preview, pre-filled from the saved `conversion` settings, and a "Save settings" button.
   - Not converted: "Convert".
   - Already converted: "Convert again", with a confirmation that says the current converted data and its results will be deleted (D6).
   - Starting either saves the settings first. Show the job's progress.
8. "More → Convert only" on the Datasets page: converts the ticked datasets with their saved settings, using the same choice for already converted ones. Any dataset without saved settings is sent to its Conversion tab first.
9. **History:** the conversions of this dataset (when, rows, result). Scans are added in Phase 5.

**Not in this phase:** scans and results.

**Done when**
- Opening a dataset shows its facts and sample rows without starting anything.
- A dataset can be converted from its Conversion tab. When the job finishes, the Datasets table shows its rows.
- "Convert again" asks first, deletes the old Prepared version, and converts again.
- Converting without saved settings is refused with "set up conversion first".
- Reopening the Conversion tab shows the saved settings.
- `npm run build` and `npm run lint` pass.

---

### Phase 4: Scan framework, with the duplicates scan

**Goal.** One generic way to start, track, cancel, replace and read scans, which also
converts a dataset first when needed. Dedup is the first scanner. It compares the chosen
datasets together, or each one on its own.

**Read first** (old chain code: take the logic, not the chain concept)
- `validator/backend/step_start.py`: the check/submit split, `SPECS` (setting fields), `_script`, `_Command`, `WRAPPER_ENV_PREFIXES`, the job-ID patterns, the lock.
- `validator/backend/steps.py`: reading SLURM state for a list of job IDs, outcomes, cancel.
- `validator/backend/jobs.py`: `_slurm`, `active_jobs`, `BATCH_ID`, `_read_json`, `_tail_file`.
- `validator/backend/chains.py`: `_SCAN_STATE` (state and progress per scanner from its files), and `_dedup_details` and the other `_*_details` (result numbers).
- `scripts/tool_team_dedup_scan.sh`, `launchers/slurm/run_dedup_scan.sbatch`.
- `modelsafety/data_screening/dedup/run.py`: `resolve_inputs`. With `--input-root` and `--input-manifest` in `runs` mode, it reads only the listed files of finished runs.
- `docs/data-screening/dedup.md`.

**Build**
1. A scanner registry (for example `scanners.py`). Each scanner has:
   - key, label, one-line description
   - its **stages** (names shown in the UI)
   - whether it uses GPUs
   - its settings fields (the same shape as `SPECS`)
   - how to build its command
   - how to read its state and progress (move the `_SCAN_STATE` logic here)
   - how to read its numbers (move the `_*_details` logic here)

   Design it so a later "review" stage can be added to any scanner without changing the framework. An example is an LLM confirming findings, as `pii_llm_validator.py` and `llm_validate.py` do today.
2. A scan record, `<service root>/scans/<scan-id>/scan.json`:
   - id (same pattern as `BATCH_ID`), scanner, dataset ids, settings, resources
   - for each dataset: "use existing" or the conversion job it waits for (D3)
   - once started: the Prepared version read for each dataset, the exact command text, the command's output, job IDs, `submitted_at`

   Write it atomically. It is written when the user starts the scan, and once more when the scan job itself is started.
3. **Convert, then scan (D3).** Submitting a scan:
   - For each dataset that isn't converted, or that the user chose to convert again, call `start_conversion()` (Phase 3). These are separate conversion jobs.
   - If every dataset is ready, start the scan job straight away.
   - Otherwise the scan is **waiting for conversion**. The backend starts it once all its conversions have finished. Check every 30 seconds in a background task, and also whenever scans are listed. A waiting scan left over when the backend was stopped is picked up when it starts again.
   - If a conversion fails or is cancelled, the scan is not started and is shown as "not started: conversion failed".
   - Use the backend instead of a SLURM dependency (`afterok`), because the wrappers check their input files when they are submitted, and the dedup file list only exists once the conversion has finished.
4. Routes:
   - `GET /api/scanners`: definitions for the UI forms
   - `POST /api/scans/check`: read-only. For each dataset: converted or not, saved settings or not. What will be read, the commands, the cost, the blockers
   - `POST /api/scans/submit`: check again under the lock, then start the conversions and the scan (or leave the scan waiting)
   - `GET /api/scans`: all scans, with live state, including "waiting for conversion"
   - `GET /api/scans/{id}`: one scan in full, with log tails
   - `POST /api/scans/{id}/cancel`: `scancel` only the job IDs the record lists. A waiting scan is cancelled without starting, and its conversion jobs keep running unless the user cancels them too.
5. Blockers:
   - a dataset needs converting but has no saved conversion settings ("set up conversion first", D3)
   - a dataset is unavailable
   - a GPU scan has no confirmation
   - the same scanner is already queued, waiting or running on one of the datasets
6. **Dedup modes** (D4):
   - **Across the chosen datasets** (default): one scan.
     - The input root is `<service root>/datasets`.
     - The scan writes `inputs/files.txt`, listing exactly the chosen datasets' Prepared files, relative to that root.
     - The wrapper only passes a manifest for `pilot`. Either add an `--input-manifest` flag to `tool_team_dedup_scan.sh` for `full`, or call `run_dedup_scan.sbatch` directly with `DEDUP_INPUT_MANIFEST`. Pick one, and keep the shown command equal to the run command.
   - **Within each dataset:** one scan per dataset, with input root that dataset's Prepared version folder. Duplicates are only found inside the dataset.
   - The record stores the mode, so results can say which one was used.
7. Each dataset's result per scanner (D5):
   - The newest **finished** scan that read the dataset's current Prepared version.
   - When a scan finishes, it becomes the result for each of its datasets. Any older scan that is now no dataset's result is deleted (inside `scans/` only). This holds whichever dedup mode either scan used.
   - A failed or cancelled scan leaves the old result in place. Its folder is kept until the next scan of that scanner on those datasets, so the failure can be read.
8. Extend unregister (D2) and re-conversion (D6) to handle scans.
9. Add a per-scanner summary to each dataset in `GET /api/datasets`: state (including "waiting for conversion"), scan id, finished at, and a headline count (for dedup, duplicate rows in this dataset).
10. Delete old `output/` folders the new service doesn't use (old prepared runs, chain folders, old scan runs) after listing them for confirmation.

**Not in this phase:** UI, the PII and toxicity scanners, coverage, and flagged-row lists.

**Done when** (try by hand with `curl` on two small datasets)
- A dedup scan across one converted and one unconverted dataset: a conversion job starts, the scan waits, then starts by itself when the conversion finishes, and finishes. Its record lists both Prepared versions.
- The same with "convert again" on the converted one: the old Prepared version and its results are gone, and both are converted again before the scan.
- A dedup scan "within each dataset" starts one scan per dataset, and each one only finds duplicates inside its own dataset.
- `GET /api/datasets` shows dedup "finished" with a duplicate count for both.
- A second dedup scan replaces the first: the old folder is gone once the new one finishes.
- A failing conversion leaves its scan "not started: conversion failed". A dataset with no saved conversion settings is refused with "set up conversion first". Cancel stops a queued or waiting scan.

---

### Phase 5: Scan action in the UI, and Activity

**Goal.** Tick datasets → "Find duplicates" → options → Start → watch it run.

**Read first**
- Phases 2 and 4 output.
- Old components, for reference: `StartStepPanel.tsx` (check while typing, GPU confirmation, command box), `ResourcesFields.tsx`, `StepJobsList.tsx`, `JobsView.tsx`, `useRefreshing.ts`, `CommandBox.tsx`.

**Build**
1. A generic **scan dialog**, built from `GET /api/scanners`:
   - The chosen datasets, each with its conversion state (D3):
     - **Not converted:** "Will be converted first (a separate job)".
     - **Already converted:** a choice, "Use the existing converted data" (selected) or "Convert again". Choosing "Convert again" shows what will be deleted.
     - **No saved conversion settings:** "Set up conversion first", with a link to its Conversion tab. Start stays disabled until it's resolved or the dataset is removed from the selection.
   - the scanner's settings
   - a collapsed "Advanced" section: partition, CPUs, memory, time, the exact commands (conversion and scan)
   - a cost line, and a GPU confirmation when needed
   - Start, with a confirmation when anything will be deleted
2. Dedup options:
   - When more than one dataset is chosen: "Compare **across the chosen datasets**" (selected) or "Find duplicates **within each dataset**".
   - "Text compared": what was asked (`prompt`) or the whole conversation (`all`)
   - "Leave out system messages", on by default
   - A plain note that follows the mode:
     - across: "Rows are compared across all N datasets you chose. When rows match, the one in the dataset that comes first by path counts as the original, and the others are flagged as duplicates." (D10)
     - within: "Each dataset is compared only with itself."
   - "Nothing is dropped by this scan. You choose what to drop from the results." (D10)
3. Wire "Find duplicates" in the action bar. "Scan for PII" and "Scan for toxicity" stay disabled until Phases 6 and 7.
4. A status chip per scanner in the Datasets table: Not scanned, Converting, Queued, Running, Done (count), Failed.
5. **Activity** page: conversions, scans, and later filter jobs, newest first, with state, datasets, duration, Cancel and log tail. A scan waiting for a conversion shows which conversion it waits for.
6. The dataset History tab lists the scans that included the dataset.

**Not in this phase:** PII and toxicity options, results pages, and the extra dedup choices (original copy, strictness, automatic drop), which come in Phase 11.

**Done when**
- Dedup can be run from the UI from start to finish, in both modes, and the chips update.
- Ticking an unconverted dataset and starting a scan shows two jobs in Activity: the conversion, then the scan.
- "Convert again" asks first, and the old converted data is gone afterwards.
- Cancel from Activity works.
- `npm run build` and `npm run lint` pass.

---

### Phase 6: PII scans (NER and rule-based)

**Goal.** "Scan for PII" offers NER (the default, with a category picker) or rule-based
detection.

**Read first**
- `docs/data-screening/pii-model.md`, `docs/data-screening/pii-regex.md`.
- `modelsafety/data_screening/pii/model/policy.json`, `common.py` (tiers: `CONTEXTUAL_LABELS`, `PERSON_LINKED_LABELS`, `label_tier`), `policy.py`.
- `scripts/tool_team_ner_scan.sh`, `launchers/slurm/submit_privacy_filter.sh` (reads `PRIVACY_FILTER_POLICY`).
- `scripts/tool_team_data.sh` (`scan`, `extract`, `--output-root`, `--pii-run-dir`), `launchers/slurm/run_pii_scan_and_extract.sbatch`.
- `validator/backend/step_start.py`: how `regex_pii` and `ner` are started today. `WRAPPER_ENV_PREFIXES` strips `PRIVACY_FILTER_*` from the inherited environment, so pass the policy explicitly.
- `validator/backend/chains.py`: `_ner_details`, `_regex_pii_details`. Also `pii_output.PII_CATEGORIES`.

**Build**
1. Two scanners: `pii_ner` (stages: build work units → model scan → combine) and `pii_regex` (stages: pattern scan → validation → extract). The UI shows them as one action, "Scan for PII", with a method choice. NER is selected by default.
2. **NER categories (D7):**
   - Show all labels, grouped as direct identifiers, person-linked and contextual, with readable names.
   - The repo doesn't hold the full list of 55 labels in one place. Read it once from the model's config (`id2label` of `OpenMed/privacy-filter-nemotron-v2`, for example from the HF cache) and save it as a constant file in the repo.
   - On submit, write `<scan>/inputs/policy.json` from the default policy:
     - an unchosen label gets threshold `null`, which disables it
     - a chosen contextual label gets a threshold (D7)
   - Pass `PRIVACY_FILTER_POLICY` in the shown command's environment.
3. NER size: "full" (8 GPUs, one node) or "one GPU", as today. GPU confirmation.
4. Rule-based settings: workers (optional). A read-only list of the 10 categories it detects, and a note on what it can't find (names, IDs).
5. One scan per dataset (D4). Starting PII on 3 datasets starts 3 scans.
6. Wire "Scan for PII" in the action bar.

**Not in this phase:** results pages (Phase 8), and the optional LLM confirmation stage.

**Done when**
- A rule-based scan and a one-GPU NER scan both run from the UI on a small dataset.
- The NER scan's `inputs/policy.json` matches the categories chosen, and the shown command sets `PRIVACY_FILTER_POLICY`.
- The chips show both methods' state.

---

### Phase 7: Toxicity scan

**Goal.** "Scan for toxicity" shows its two stages and lets the user set the Detoxify
threshold and what counts as flagged.

**Read first**
- `docs/data-screening/toxicity.md`.
- `modelsafety/data_screening/toxicity/run.py` (threshold on the highest class score), `detoxify.py` (models), `llm_check_guard.py` (`SAFETY_RE`, `CATEGORY_RE`: the guard's categories).
- `scripts/tool_team_toxicity_scan.sh` (`--threshold`, `--shards`, `--concurrency`), `launchers/slurm/run_toxicity_scan_array.sbatch`.
- `modelsafety/filter/rules.py`: `TOXICITY_REMOVABLE_VERDICTS`, guard error handling.
- `validator/backend/chains.py`: `_toxicity_state`, `_toxicity_details`.

**Build**
1. A `toxicity` scanner with stages "Quick screen: Detoxify (Unitary, multilingual model)" → "Judge: Qwen3Guard-Gen-8B".
2. Settings:
   - Detoxify threshold, 0.05 to 0.95 in steps of 0.05, default 0.5, passed as `--threshold`. Explain in a short line that a lower value sends more messages to the judge, so the scan is slower and finds more.
   - Guard categories to flag: Violent, Non-violent Illegal Acts, Sexual Content or Sexual Acts, PII, Suicide & Self-Harm, Unethical Acts, Politically Sensitive Topics, Copyright Violation, Jailbreak. All are selected by default.
   - Verdicts to flag: Unsafe and Controversial by default.
   - Guard errors count as flagged (D9).
   - Advanced: shards and concurrency.
   - GPU confirmation.
3. Categories and verdicts are saved in the scan's settings and applied when results are read (D8). The results page (Phases 8 and 9) can change them without re-running. The threshold can't, and the UI says so.
4. One scan per dataset (D4). Wire "Scan for toxicity".

**Not in this phase:** results pages.

**Done when**
- A toxicity scan runs from the UI on a small dataset, its command shows the chosen threshold, and progress shows shards done.
- The saved settings hold the chosen categories and verdicts.

---

### Phase 8: Results per dataset, with coverage

**Goal.** A dataset's Results tab shows each scanner's result, and a result page gives
the full picture for one scanner.

**Read first**
- Phase 4 (results per dataset, scanner definitions).
- `modelsafety/results/coverage.py`: how a report is made, `load_dedup` and the other loaders, `scan_inputs.json`.
- `validator/backend/chains.py`: `_coverage_section`, `_coverage_details`, `_*_details`.
- Old `components/StepResults.tsx`: the existing result tables (`DedupResults`, `KeyFigures`, `SettingsTable`), for reference.

**Build**
1. `GET /api/datasets/{id}/results`. For each scanner:
   - state, scan id, finished at, settings used
   - numbers **for this dataset only**: rows scanned, rows flagged, and the top labels, categories or verdicts
   - for dedup, the mode used: "within this dataset", or "across: A, B, C" with how many duplicates matched a row in another dataset
2. Coverage:
   - Run `coverage.py` for a scan when it is first seen finished, and save `<scan>/coverage.json`. Offer a "Check again" button.
   - For a dedup scan narrowed by a file manifest, check that coverage compares against exactly the chosen datasets. If it doesn't, extend `coverage.py` to accept the scan's own selection, without changing its existing behaviour.
   - Show a verdict badge (pass or fail, with the problems).
3. Toxicity counts apply the scan's saved categories and verdicts (D8). Let the user change them on the result page; that only changes what is shown.
4. A result page `#/datasets/<id>/results/<scanner>`:
   - summary, coverage, breakdown tables, settings used
   - a link to the scan in Activity
   - "Run again" (opens the scan dialog pre-filled)
5. The Datasets chips and the dataset header use the same data.

**Not in this phase:** listing flagged rows (Phase 9).

**Done when**
- After the scans from Phases 5 to 7, each dataset shows correct counts per scanner, and the coverage verdict.
- Changing toxicity categories on the result page changes the flagged count without starting a job.

---

### Phase 9: Flagged rows (browse and inspect)

**Goal.** From a result page, page through every flagged row of the dataset, filter
them, and open one to see exactly why it was flagged.

**Read first**
- `modelsafety/filter/rules.py`: `READERS` and `HitIndex`. They already turn every scanner's output into per-row findings keyed by file, line and fingerprint, so reuse them and the filter and the UI will agree.
- `validator/backend/removed.py`: paging an index file without reading data files (old code, for the pattern).
- `validator/backend/rows.py`, `runs.py` (`open_run`, `lookup`, `prepared_run_of`): reading one Prepared line with a fingerprint check, and finding the original row.
- Old frontend, for reference: `RemovedView.tsx` (paging, `CountSelect`), `RowText.tsx`, `RowFindings.tsx` (highlights, `DuplicatePanel`), `OriginalLookup.tsx`.

**Build**
1. A **findings index** per scan and dataset: `<scan>/index/<dataset-id>.jsonl`, plus a counts file.
   - One line per flagged row: file, line, fingerprint, reasons, labels, categories and verdicts, and a short text preview.
   - Build it when the scan is first seen finished. Use a CPU SLURM job when the results are large (regex PII results can be over a gigabyte), or run it inline when they are small.
2. The user's choices (NER labels, regex categories, toxicity categories and verdicts) are applied when the index is read, not when it is built.
3. Routes:
   - one page of flagged rows, with filters by reason, label, category and verdict
   - one row: its Prepared text (fingerprint checked), and its findings:
     - NER and regex spans with character offsets
     - toxicity: message index, verdict, categories, Detoxify score
     - dedup: the kept copy, which dataset it is in, and both texts side by side
4. UI on the result page:
   - a flagged-rows table with checkboxes, filters and paging
   - a row detail panel with highlights
   - "Show original", which opens the original row the Prepared row came from

**Not in this phase:** dropping rows.

**Done when**
- From each scanner's result page, every flagged row can be paged and filtered by category.
- Opening a row shows the highlighted reason, and "Show original" opens the original row.
- A page loads quickly on large data, because it reads only the index.

---

### Phase 10: Drop rows and make a filtered version

**Goal.** The user selects flagged rows and drops them. The service writes a new
filtered version and registers it as a dataset.

**Read first**
- `modelsafety/filter/filter_rows.py`: byte-for-byte copy, `line_map`, `removed_rows.ndjson`, a `summary.json` that reads like a Prepared run, and its checks (finished input, fingerprints).
- `modelsafety/filter/rules.py`, `launchers/slurm/filter_rows.sbatch`.
- `validator/backend/runs.py`: `prepared_run_of`, which follows filter runs back to the Prepared run.

**Build**
1. The selection can be explicit rows, or "all rows matching the current filters" (resolved on the server into a row list when submitted). It can combine results from several scanners, as long as they are for the same Prepared version.
2. Register the new dataset first (D11), then write `datasets/<new-id>/drop_list.jsonl`. One line per row: file, line, fingerprint, and the scanner and reason it came from.
3. Extend `filter_rows.py` with a drop-list input, for example `--drop-list FILE`. It removes exactly those rows with reason `manual` (and the scanner name), and keeps every existing check. Existing options behave exactly as before.
4. Start it as a CPU SLURM job, with output in `datasets/<new-id>/prepared/<version-id>/`. When it finishes, set the new dataset's `prepared` to it. Mark the parent's Prepared version as kept (D6).
5. UI:
   - a confirmation showing how many rows will be dropped, by scanner and reason
   - progress on Activity
   - a link to the new dataset
   - the parent's page lists its filtered versions
6. Nothing in the parent dataset, its Prepared rows or its results changes.

**Not in this phase:** undo. A filtered version can be unregistered (D2).

**Done when**
- Dropping 10 toxicity-flagged rows gives a new dataset with exactly 10 fewer rows, and its removed-rows file lists those 10.
- A new scan can run on the filtered version.
- "Show original" from a row of the filtered version opens the right original row.

---

### Phase 11: More dedup choices (original copy, match strictness, automatic drop)

**Goal.** Three more dedup settings in the scan dialog: which copy counts as the
original, how strict a match must be, and whether duplicates are dropped automatically.

**Read first**
- `modelsafety/data_screening/dedup/run.py`:
  - `iter_candidates` numbers rows in the order the files are given, and `resolve_inputs` keeps the manifest's line order.
  - `detect_duplicates` keeps the row with the lowest number in each group (`canonical = min(...)`).
  - `--ngram-size`, `--num-perm`, `--bands`, `--rows-per-band`, `validate_lsh_parameters`, `collision_probability_midpoint`.
- `launchers/slurm/run_dedup_scan.sbatch` (the `DEDUP_NGRAM_SIZE`, `DEDUP_NUM_PERM`, `DEDUP_BANDS`, `DEDUP_ROWS_PER_BAND` variables), `scripts/tool_team_dedup_scan.sh`.
- `docs/data-screening/dedup.md`.
- Phase 4 (the dedup scanner and the file manifest), Phase 9 (the findings index), Phase 10 (making a filtered version).

**Build**
1. **Which copy counts as the original (D15).** Only for "across the chosen datasets", with two or more datasets.
   - **First by path** (default): write `inputs/files.txt` sorted by path, as in Phase 4.
   - **Priority order:** the user drags the chosen datasets into order. Write `inputs/files.txt` dataset by dataset in that order, with each dataset's files sorted by path. Because rows are numbered in file order, a row in a higher dataset always wins.
   - **Never flag rows in these datasets:** the user ticks protected datasets. They go first in the file order, in the order shown. Their own flags are left out of the findings index, the counts and the drop list. Refuse if every dataset is protected.
   - Before relying on file order, confirm by reading the code (or a small run) that nothing between the manifest and `iter_candidates` sorts the files again. If something does, add a flag to keep the given order, without changing what happens when it isn't set.
   - Save the choice and the order in the scan record. Show it on the result page: "Original copy: first by path", or "priority: A, then B, then C", or "protected: A".
2. **Match strictness (D16).** A choice of Strict, Standard (default) and Loose.
   - Pass the bands and rows of the preset to the scan, explicitly in the shown command. The wrapper strips inherited `DEDUP_*` variables, so set them in the command's own environment, or add flags to the wrapper.
   - Show each preset's similarity next to its name, computed with the same formula as `collision_probability_midpoint`, so the UI and the scan can't disagree.
   - Show the preset on the result page, together with the midpoint the scan recorded in its `summary.json`.
3. **Drop automatically (D17).** A checkbox, off by default: "Drop duplicates automatically when the scan finishes".
   - When the scan is first seen finished: run the coverage check (Phase 8), then build the findings index (Phase 9).
   - If coverage passed, make one filtered version per dataset that has duplicates, using Phase 10's code. Its drop list is all of that dataset's duplicate flags, minus protected datasets (D15). The drop list's reason says it was automatic.
   - If coverage failed, make nothing, and show "Not dropped: the coverage check failed" with the problems.
   - Show the new filtered datasets on the result page and in Activity.
   - Make this a scan-level setting in the framework (Phase 4), so other scanners can offer it later, but only show it for dedup now.
4. Scan dialog layout: Mode (Phase 5) → Original copy → Strictness → Text compared → Leave out system messages → Drop automatically. Each choice has a one-line explanation, and the summary note at the bottom follows the choices.

**Not in this phase:** a custom strictness (any bands and rows), automatic drop for PII or toxicity, and dedup against a reference set outside the service.

**Done when**
- With "priority order" B then A, a row that appears in both is flagged in A and not in B. With the default, the dataset first by path keeps it.
- With A protected, no row of A is flagged, and A's duplicates of B rows show only in B.
- Strict finds fewer duplicates than Standard on the same data, and Loose finds more. Each result page shows the preset and its recorded midpoint.
- With "drop automatically" on, a finished scan whose coverage passed creates one filtered version per dataset that has duplicates, each with exactly that dataset's flagged rows removed. With coverage failing, none is created and the reason is shown.

---

### Phase 12: Remove leftover old code, and write the docs

**Goal.** Only the new service is left in the code, and the docs describe it.

**Read first**
- `validator/backend/` and `validator/frontend/src/`: every module, and which ones nothing imports any more.
- The chain tools outside the validator: `modelsafety/contract/chain.py`, `modelsafety/trace/` (trace, row status), `launchers/slurm/trace_findings.sbatch`, `launchers/slurm/row_status.sbatch`, and anything else only chains use.
- `validator/run.sh` (header comment), `validator/backend/app.py` (module docstring), `validator/frontend/README.md`.
- The planning docs at the repo root: `BACKEND_IMPLEMENTATION_PHASES.md`, `UI_DESIGN_ANALYSIS.md`, `TRACKING_SCANNED_ROWS.md`.

**Build**
1. List every old module, route, script and doc that is now unused, including the chain tools. Show the list to the user, then delete what they agree to.
2. Check that nothing the new service runs was removed: the scanners, `prepare_rows.py`, `filter_rows.py`, `coverage.py`, `run_finder.py`, fingerprints, origin lookup.
3. Update `validator/run.sh`'s header, `app.py`'s docstring and the frontend README to describe the new service: what it reads, what it writes and where, what it deletes (D2, D5, D6), and what asks for confirmation.

**Done when**
- Only the new service's code is left, and the user agreed to every deletion.
- `npm run build` and `npm run lint` pass, and there are no unused imports or routes.
- The docs match what the service does.

---

## 10. Notes from earlier phases

Add one line per finished phase: anything that differed from this document, and what
the next phase must know.

- **Phase 1** (`validator/backend/registry.py`, `registry_seed.py`; routes in `app.py`):
  - Dataset IDs are opaque, `ds-<12 hex>` (`registry.DATASET_ID`), so renaming never moves a folder or changes a URL. A dataset's folder is `<service root>/datasets/<id>`.
  - First request on a new service root seeds `datasets.json` from `sources.CATALOG`: 67 BotPsy and 32 MedPsy, nothing skipped (`seed_skipped` holds anything that couldn't be seeded). Seed names are cleaned to fit the name rule (`mopd_mixv4_0907 (train)` becomes `mopd_mixv4_0907 train`), and a clash gets the parent folder's name (`oe_health nothink25`). Seeded paths are kept even if unreadable, and show as unavailable. `registry_seed.py` still imports `sources.py`; Phase 2 moves `CATALOG` into the seed and deletes `sources.py`.
  - "Same dataset twice" means the same input the converter would read: the path, plus the file for csv and json, plus the split for huggingface. A dataset with no `conversion` counts by path alone.
  - `PATCH` takes only team, name, description, notes and conversion. Any other field (including `path`, `prepared`, `parent`) is refused by pydantic with a 422, not the 400 the other checks give. So Phase 3 needs its own internal function in `registry.py` to set `prepared` (use `_LOCK`, `_read_registry`, `_save`), and Phase 10 the same for `parent`.
  - `unregister` deletes only `<service root>/datasets/<id>` and refuses a symlink. It does not yet check for a running conversion (add in Phase 3) or for scans (Phase 4), and does not yet handle a parent of a filtered dataset (Phase 10).
  - Checked by hand against a throwaway `$VALIDATOR_SERVICE_ROOT`. Every "Done when" item held, and the original data was byte-identical afterwards.
- **Phase 6** (`validator/backend/scanners.py`, `scans.py`, `reader_imports.py`; `validator/frontend/src/`: `scans.ts`, `types.ts`, `useScanResources.ts`, `components/ScanDialog`, `ResourcesFields`, `ScanJob`, `ActionBar`, `DatasetsPage`; scan tools: `launchers/slurm/submit_privacy_filter.sh`, `modelsafety/data_screening/pii/regex/pii_validator.py` and `pii_output.py`, new `modelsafety/data_screening/pii/model/nemotron_labels.json`):
  - **Two scanners, one action.** `pii_ner` ("PII (NER)") and `pii_regex` ("PII (rules)") are separate entries in `SCANNERS`, so every dataset has a chip for each (`dataset.scans.pii_ner`, `.pii_regex`). The action bar's "Scan for PII" opens one dialog with a "Method" choice: `SCAN_ACTIONS` in `scans.ts` maps an action key to a title and its scanner keys (`dedup` to `['dedup']`, `pii` to `['pii_ner', 'pii_regex']`); `ScanDialog` takes `action` instead of `scannerKey`, and an unknown action is taken for a scanner key. Phase 7 adds `toxicity: {title, scannerKeys: ['toxicity']}` and enables its button (`ActionBar` still has it disabled). Each method keeps its own settings and resource boxes while the dialog is open (not reset on switching, as the plan said); the check and the GPU confirmation are reset.
  - **Framework hooks Phase 7 reuses (toxicity also prepares, then scans).** `Scanner.read_jobs(output)` returns `(jobs the scan's own job waits for, its own job ID)` and is also called on the output of a command that failed (a job started before the failure is kept: the record's `prior_jobs`, `[{role, job_id}]`, roles `plan` for NER and `scan` for a scan's own job; `jobs` in every scan view lists all of them, `job_id` stays the own job). `Scanner.log_files(scan folder, settings, jobs)` replaced `(logs folder, job id)`. `Scanner.success_markers` (paths under `results/`, all must exist; the regex scan has three) replaced the single `_SUCCESS`; `finished_at` is the newest marker's time. `Scanner.fixed_resources(settings)` is for a scanner whose job scripts fix the CPUs, memory and time: only the partition is taken (anything else is refused with a message), `scan.resources` holds the partition and what the script's `#SBATCH` lines ask for (shown, not sent), and `scanners.script_resources`/`sbatch_header` read them (`sbatch_header` is a copy of `step_start.py`'s, which stays until Phase 12). `Scanner.detects` is a read-only list (the regex categories). New setting types: `integer` (min, max, empty means the default, which may be null; the dialog sends the text as typed) and `multi` (a list of the choices, at least one, kept in the order of the choices, optional `groups` for display). `ENV_PREFIXES` is now `DEDUP_`, `PII_` and `PRIVACY_FILTER_`; `_check_export_values` refuses a comma or line break in a value, for every scanner. `scanners.cost` is called inside the same `try` as the command, so a `ScannerError` from it blocks the scan instead of failing the request.
  - **Scan state with several jobs.** A prior job that failed or was cancelled makes the scan failed or cancelled ("The plan job (N) failed: ..."), whatever became of the job waiting for it. The scan counts as running while its own job is queued behind a prior job that runs. Cancel runs `scancel` on every job the record lists that SLURM still has. A scan whose command didn't name its jobs after the scan (NER's job scripts name their own) says "jobs submitted at <time>" instead of a job name when it was being started as the service stopped. A start command that failed after its first job was submitted says so in `submit_error` and names that job ("may still be running: stop it with scancel"): nothing cancels it for you.
  - **NER.** `env REPO=... PRIVACY_FILTER_*=... bash launchers/slurm/submit_privacy_filter.sh` (not `tool_team_ner_scan.sh`, which only adds defaults and printing). Size `full` (default, 8 GPUs on one node, mode `full`) or `one_gpu` (mode `pilot`, with `PRIVACY_FILTER_MAX_FILES=100000` and `PRIVACY_FILTER_MAX_UNITS=0`, as the old chain code did). `PRIVACY_FILTER_KILL_ON_INVALID_DEP=1` is always in the command: the script (new, opt-in) then adds `--kill-on-invalid-dep=yes` to the GPU job, because this cluster has no `kill_invalid_depend` and a failed planning job would leave the GPU job pending for ever. The categories are `inputs/policy.json`: the default policy with every label of `nemotron_labels.json` set to `null` unless chosen, a chosen contextual label at **0.7** (D7), everything else (control markup, ignored values, the other model's labels) kept, and read back with `CandidatePolicy` before use; its path is `PRIVACY_FILTER_POLICY` in the shown command. The 55 labels were read from `OpenMed/privacy-filter-nemotron-v2` (revision `a04eea90...`, both cached snapshots agree): 34 direct identifiers, 2 person-linked, 19 contextual; the default policy enables 36. Cost is read from the job scripts' headers (GPU job plus the planning job), so the GPU confirmation follows. Logs: the job scripts fix `logs/slurm/privacy_filter_*` in the repo (allowed by section 5), plus `results/worker_logs/worker_0.log` for `full`. **The policy says a switched-off label was excluded for "contextual_tier"** (`policy.py` gives every `null` threshold that reason, not only contextual labels): not changed here, but Phase 9 must not read it as "this label is contextual".
  - **Rule-based.** `sbatch` on `run_pii_scan_and_extract.sbatch` with `PII_STAGES=all`, `PII_NUM_WORKERS` (the setting, or one per CPU) and `PII_LOG_ROOT` inside the scan folder. It has no `results/_SUCCESS`: the three stage markers must exist. `pii_validator.py` now also writes `stage2_validated/summary.json` (per category `raw` and `kept`, `raw_total`, `kept_total`, `unique_locations`) before it marks stage 2 finished; `metrics.txt` is unchanged and was compared with it.
  - **Counts are findings, not rows.** The chip count is NER's `policy_eligible_spans` or the regex scan's `kept_total` ("PII findings"). Rows flagged come with Phases 8 and 9 (`unique_locations` is already the rows with a kept regex hit).
  - **Frontend.** `ScannerField` is a union (`choice`, `integer`, `multi`), settings are `ScanSettings` (`SettingValue` is a string, a number, a list or null), and a scan request's `resources` can hold just the partition (`ResourceRequest`). `ResourcesFields` takes `partitionOnly`. `useScanResources(scannerKey)` keeps what was typed for each scanner. The NER categories are shown in three groups (direct identifiers, names, contextual), each with "All" and "None"; readable names are in `scans.ts` (`nerLabelName`, `ruleCategoryName`). The pages were **not clicked through in a browser** (none here): do that before relying on Phase 6: switching method, the category picker, the partition-only resources box, the GPU confirmation, the Activity page with a two-job scan.
  - **Checked.** `npm run build` and `npm run lint` are clean. The backend was checked with HTTP calls against a throwaway `$VALIDATOR_SERVICE_ROOT` on a separate uvicorn, with **a stand-in for SLURM** (no SLURM or GPU on the machine this ran on): it runs the real job scripts (`prepare_rows.sbatch`, `run_pii_scan_and_extract.sbatch`, `run_dedup_scan.sbatch`, `submit_privacy_filter.sh` and the manifest job) locally, with `--dependency` and `--kill-on-invalid-dep` honoured. **The NER model and the GPU job never ran**: the GPU job was replaced by a stub that writes a made-up result. What held: every refusal (no labels, unknown label, a list that isn't one, a bad size, CPUs, memory or time given for NER, an unknown partition, a bad `workers`, no GPU confirmation); the rule-based scan converted its dataset, waited, ran the real three stages and became the result (counts equal `metrics.txt`); the NER scan wrote a policy with exactly the chosen labels (contextual at 0.7), the policy path reached the job's environment, both jobs were found from the script's output, the GPU job was submitted with `--dependency=afterok` and `--kill-on-invalid-dep=yes`, and the scan finished and became the result; a failing planning job gave "The plan job (N) failed", SLURM removed the GPU job, and the old result stayed; a failing GPU job left the old result; Cancel stopped both jobs and a second Cancel said "not active"; an NER scan on an unconverted dataset waited, then started both jobs by itself; dedup across two datasets (one unconverted) still worked. The original files were byte-identical afterwards. **Still to do on the cluster:** one real one-GPU NER run on a small dataset (asks for the GPU confirmation), and the first real rule-based run (the job creates its virtualenv and installs `piiregex` on first use). A regex scan that fails before its job logs anything shows only the SLURM files.
  - **Trap while testing:** the regex scan's packages (`piiregex`, `tqdm`) are installed by the job script into `pii/regex/.venv` the first time; without them the job fails at `import piiregex`, and the scan shows "failed" with that in "Scan output".

---

## 11. Later ideas (not planned)

- An LLM "review" stage for any scanner, to confirm or reject findings before the user
  sees them. Phase 4's stage design leaves room for it.
- Dedup against a reference set outside the service (cross mode, test-set
  decontamination against benchmarks).
- A custom strictness setting (any bands, rows and n-gram size), beyond the three presets.
- Automatic drop for PII and toxicity scans, using the same mechanism as D17.
- Running one PII or toxicity job over several datasets at once, to save GPU start-up time.
- Model validation pages (for example regurgitation).
- Moving the code out of the `validator/` folder name, if the product gets its own name.
