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
| 2 | New app and Datasets home | 1 | Frontend | ☑ |
| 3 | Dataset page: overview, data, conversion | 2 | Both | ☑ |
| 4 | Scan framework, with the duplicates scan | 3 | Backend | ☑ |
| 5 | Scan action in the UI, and Activity | 4 | Frontend | ☑ |
| 6 | PII scans: NER and rule-based | 5 | Both | ☑ |
| 7 | Toxicity scan | 5 | Both | ☑ |
| 8 | Results per dataset, with coverage | 5 (and 6, 7 for those scanners) | Both | ☑ |
| 9 | Flagged rows: browse and inspect | 8 | Both | ☑ |
| 10 | Drop rows and make a filtered version | 9 | Both | ☑ |
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
- **Phase 2** (`validator/frontend/src/`, `validator/backend/app.py`, `registry_seed.py`):
  - The old UI is deleted: every file in `components/`, and the helpers `catalog`, `chains`, `describe`, `jobs`, `mapping`, `origin`, `progress`, `readable`, `resources`, `review`, `rows` and `useRefreshing`. Phases 3, 5, 8 and 9 read them from git: `git show 86dd0cf:validator/frontend/src/<file>` (`86dd0cf` is the last commit before Phase 2).
  - New in `src/`: `route.ts` (`#/datasets`, `#/datasets/<id>`, `#/activity`; any other address is replaced with `#/datasets`), and in `components/`: `Chip` (tones ok, warn, error, neutral), `Dialog` (the native `<dialog>`; while `busy`, Escape and the backdrop don't close it), `Menu`, `DatasetsPage` with `DatasetsTable`, `ActionBar`, `RegisterDatasetDialog` and `UnregisterDialog`, and two placeholders: `DatasetPage` (Phase 3) and `ActivityPage` (Phase 5). `api.ts` now holds only the registry calls and `inspectPath`, and `types.ts` only the registry and inspect types. `DatasetPrepared` in `types.ts` is a placeholder shape: Phase 3 should adjust it to what it sets.
  - Left empty for later: the table's "Scans" column and the action bar's buttons (all disabled). Phase 5 fills the column and enables "Find duplicates", Phases 6 and 7 the other two, and Phase 3 "Convert only" (under "More"). Build the scanner chips on `Chip`; the old `StateChip` went with the chain code.
  - The "Format" column shows `conversion.input_type`, so it is a dash for every dataset until Phase 3 saves conversion settings. The Register dialog detects the format to confirm the path but saves no `conversion`.
  - Selection and search are forgotten when the user leaves the Datasets page. Only the team tab is remembered (localStorage `validator.datasets.team`).
  - Backend: every route only the old views used is gone, including all six `/api/steps/*` routes. Kept: `/api/health`, `/api/teams`, `/api/datasets*`, `/api/inspect`, `/api/preview`, `/api/runs`, `/api/run*`, `/api/origin`, `/api/prepared-row` (no view uses it; Phase 12 decides) and `/api/jobs/*`. `app.py` no longer imports `chains`, `steps`, `step_start`, `rows` or `removed`; those modules stay until Phase 12.
  - `sources.py` is deleted. `CATALOG` and the `children_of` expansion moved unchanged into `registry_seed.py`. The seed output was compared before and after and is identical (99 records: 67 BotPsy, 32 MedPsy, none skipped). One small difference: a `children_of` folder that exists but can't be read now gets the reason from `registry.unavailable_reason` in `seed_skipped`.
  - `index.css` got two colours, `--warn` and `--warn-bg`. `run.sh`'s header and `app.py`'s docstring only lost what refers to removed routes; their full rewrite is still Phase 12 (the `run.sh` "Conversions" paragraph still describes the old Start panel).
  - Checked: `npm run build` and `npm run lint` are clean. The backend was checked with `curl` against a throwaway `$VALIDATOR_SERVICE_ROOT`, on a separate uvicorn instead of `run.sh` (which would overwrite `validator/.validator-state`): the seed, register, the five refusals, persistence over a real restart, unregister, the removed routes answering 404 or 405, and the original file unchanged. **The pages were not clicked through in a browser** (none was available on the login node), so do that before relying on Phase 2: tabs, search, selection, the row and "More" menus, Register and Unregister.
- **Phase 3** (`validator/backend/conversions.py`; `validator/frontend/src/components/`: `DatasetPage`, `OverviewTab`, `DataTab`, `ConversionTab`, `ConversionJob`, `HistoryTab`, `ResultsTab`, `ConvertDialog`):
  - **What Phase 4 calls:** `conversions.start_conversion(dataset_id, reconvert=False, resources=None)` returns the conversion's record (a dict). `job_id` (the version id, like a batch id) and `dataset_id` identify it for `conversions.conversion_status(dataset_id, job_id)`, which gives the live `outcome` (`queued`, `running`, `stalled`, `succeeded`, `checks_failed`, `failed`, `cancelled`, `not_submitted`, `unknown`), `current` (it is the dataset's Prepared version) and `result` (set once it became current). `submit_error` in the record says `sbatch` failed. It raises `ConversionError` (a `jobs.JobsError`) when the dataset has no saved settings ("Set up conversion first"), is already converted and `reconvert` is false, is unavailable, or has a conversion queued or running; `jobs.SlurmUnavailable` when SLURM can't be asked. It takes its own lock, so call it once per dataset. `conversions.check(items, resources)` and `submit(items, resources)` take `[{id, reconvert}]`.
  - **Where things are:** `<service root>/datasets/<id>/prepared/<version-id>/`, `_jobs/<version-id>.json`, `_logs/<slurm-id>.out|.err`. The dataset's `prepared` is `{version_id, folder, rows, rows_read, skipped, finished_at, kept}` (`folder` is absolute). `kept_versions` lists earlier versions that stay on disk. `registry.set_prepared` and `registry.clear_prepared` are the only code that change them (`PATCH` still refuses both). Nothing sets `kept` before Phase 10; when it does, set it on `prepared` and "convert again" keeps that folder, as D6 says (checked by hand).
  - **Convert again (D6) deletes only the Prepared version.** Scans don't exist yet, so Phase 4 must extend `start_conversion` (in `_launch`, after `clear_prepared`) to delete the scans that only read that version, and `conversions.unregister` for D2. Every start also deletes the folders of failed or cancelled attempts: any `prepared/<version-id>` that is neither the current version nor in `kept_versions`. The confirmation lists them.
  - **A job's state is still never stored**, but `prepared` is: a finished conversion becomes the current version when something reads it (`GET /api/datasets`, `GET /api/datasets/{id}`, the two conversion read routes, `check`, `submit`), not by a background task. Phase 4's 30-second loop can call `conversions.refresh_all()`. `refresh` skips instead of waiting when a start is in progress.
  - **Shared-disk lag (found by testing, not in the plan):** on this cluster's NFS, looking for a file or folder before it exists makes the login node remember "not there" for about a minute, even after the job wrote it. So `conversions.py` creates the empty version folder before `sbatch` (`prepare_rows.py` accepts an empty folder), looks for `summary.json` and `_SUCCESS` only once SLURM no longer lists the job, and reads nothing while the job is queued. That means `refresh` asks `squeue` once, but only when a conversion is waiting to be picked up. Phase 4 must keep this rule for the files of its own scans (look for results only after SLURM stops listing the job).
  - **The commands shown before a start** (check, Convert dialog, "Advanced") have `<version-id>` where the folder name goes, because the id is chosen when the job starts. The job's record, and the "Commands and logs" box, hold the exact commands that ran.
  - **Removed:** all six `/api/jobs/*` routes and their request models. `jobs.py` stays: `conversions.py` reuses its SLURM helpers (`validate_resources`, `active_jobs`, `_read_slurm`, `_decide_outcome`, `_submit_one`, ...). `sbatch_args` and `_submit_one` now take the logs folder instead of the runs root.
  - **Frontend:** the address is `#/datasets/<id>[/<tab>]` (tabs: overview, data, conversion, results, history; `route.ts`). Ported from git (`git show 86dd0cf:validator/frontend/src/<file>`): `mapping`, `describe`, `readable`, `progress`, `origin`, `resources`, `useRefreshing`, `FormatPicker` (now starts from the saved settings), `InputFields`, `OutputPreview`, `TargetFormat`, `RowNavigator`, `RecordView`, `ValueView`, `PreparedRows`, `SkippedEntryDetails` (the "Open this row in the preview" button is gone: "Original now" shows the original row), `OriginalLookup`, `CommandBox`, `ResourcesFields`. New hooks: `usePreview` (with `useInspect`, `useRun`, `useRequest`), `useConversionResources`. Resources are only sent after the user edits a box; otherwise the backend uses `jobs.DEFAULT_RESOURCES`. The Conversion tab stays mounted once opened, so unsaved edits survive a trip to another tab. The Overview's "rows" is the Prepared count, or `expected_rows` from the preview when the format knows it, otherwise "Known after conversion" (`/api/inspect` has no row count).
  - **Checked:** `npm run build` and `npm run lint` are clean. The backend was checked with `curl` against a throwaway `$VALIDATOR_SERVICE_ROOT` on the shared disk and real SLURM jobs on two tiny datasets: convert without settings is refused; save settings, convert, and the rows show in `GET /api/datasets`; convert again deletes the old version and a planted fake failed attempt but not a folder with another name; a conversion whose checks fail leaves the dataset "not converted" with its folder and log kept, and the next start deletes it; Cancel works and a second Cancel says "not active"; unregister is refused while a conversion runs and works after; a batch of two with one version marked `kept` leaves that folder; the Data tab's calls (`/api/run`, `/api/run/skipped`, `/api/preview` on a Prepared file, `/api/origin`) work on the new layout; the original files were byte-identical afterwards and the real `output/service` was not touched. **The pages were not clicked through in a browser** (none on the login node), so do that before relying on Phase 3: every tab, Save settings, Convert and Convert again (with its confirmation), Cancel, and "More → Convert only".
- **Phase 4** (`validator/backend/scanners.py`, `scans.py`; routes in `app.py`; small additions to `registry.py` and `jobs.py`; `conversions.py` is unchanged):
  - **Files:** `scanners.py` is the scanner registry (`SCANNERS`, one `Scanner` entry per kind: label, settings fields, default resources, `modes`, and the functions that write its inputs, build its command, name its logs and read its numbers). `scans.py` is the framework and never names a scanner. Phase 6 and 7 add entries to `SCANNERS` (a field of type other than `choice` needs a branch in `scanners._check_field`; `ENV_PREFIXES` needs the new scripts' variable prefixes so `sbatch` starts without them). `registry.py`: every record has `results` (`{scanner: {scan_id, finished_at, mode, count}}`), written only by `set_result` and emptied by `clear_prepared`; `result_pointers()` and `dataset_names()` are small read helpers (plan said `dataset_ids_with_result`; one bulk read replaced it). `jobs._run` takes an optional `env`. The dedup scan is started with `sbatch` directly on `launchers/slurm/run_dedup_scan.sbatch`, **not** through `scripts/tool_team_dedup_scan.sh` (it has no manifest flag for `full`, and its guards are redundant): the settings are in `--export`, and `DEDUP_*` variables are removed from the environment `sbatch` runs in, so the shown command is the command that runs. `DEDUP_LOG_ROOT` is set to `<scan>/_logs/dedup`, so the job's own logs stay inside the scan folder; the SLURM `%j.out|.err` files only hold what happens before that.
  - **Routes** (all in `app.py`): `GET /api/scanners`; `POST /api/scans/check` and `POST /api/scans/submit` take `{scanner, datasets: [{id, reconvert}], mode, settings, resources, confirm_gpu}` (`mode` is `across` (default) or `within` for dedup, null for scanners without modes; a single dataset is always `within`). `check` returns `{units: [{mode, datasets: [{id, name, action: use_existing|convert|convert_again|null, status, reason, prepared, will_delete, conversion_command, ...}], blocked, warnings, cost, needs_gpu_confirmation, command_text}], blocked, can_start, settings, resources}`; the command has `<version-id>` and `<scan-id>` where those aren't known yet. `submit` refuses the whole request if anything is blocked, and returns `{scans, conversions, errors}`. `GET /api/scans` and `GET /api/scans/{id}` (command, versions read, `details` once succeeded, four log tails), `POST /api/scans/{id}/cancel` (returns `{cancelled, not_active, stopped_waiting}`). `GET /api/datasets` and `GET /api/datasets/{id}` now carry `scans: {dedup: {state, scan_id, finished_at, count, mode, reason}}`, where `state` is `not_scanned`, `waiting_for_conversion`, `starting`, `queued`, `running`, `succeeded`, `failed`, `cancelled`, `conversion_failed` or `unknown`; `count` is the duplicate rows in that dataset, worked out once when the scan finished and saved with the result (so the list reads no scan file).
  - **How a scan runs:** a scan across datasets is one scan (datasets in the order of their registered paths, so the first by path holds the copy that counts as the original, D10), `inputs/files.txt` lists each dataset's Prepared files relative to `<service root>/datasets`, and the job reads root `<service root>/datasets` with that manifest. A scan within a dataset reads that dataset's Prepared version folder directly. The record has no command until every dataset has a Prepared version; `scans.refresh()` starts it (every 30 seconds in a daemon thread started by the FastAPI lifespan, and whenever scans or datasets are read; checked: a scan left waiting when the backend was stopped started by itself after a restart). The lock is per process, so run one uvicorn worker (as `run.sh` does).
  - **Not in the plan, added:** `POST /api/conversions/submit` refuses "convert again" while a scan that waits for or reads the dataset is going (`scans.refuse_if_scanning`), and a scan's check refuses "convert again" of a dataset another scanner's scan holds, because convert again deletes the data that scan reads. `unregister` goes through `scans.unregister`, which refuses while a scan of the dataset is going.
  - **Cleanup rules (D5, D6, D2):** a finished scan becomes the result of each of its datasets (`result_applied_at` is set once). A scan folder is deleted only when it was a result and nothing points at it any more: replaced by a newer scan, its dataset converted again (`clear_prepared` empties `results` at once, so the dataset shows "not scanned" immediately) or unregistered. A scan shared with a dataset that is still registered stays. **A failed, cancelled or "conversion failed" scan is never deleted automatically** (a simplification of D5's "kept until the next scan"): they pile up in `scans/` and in `GET /api/scans`, and are only removed when the scan's one dataset is unregistered. Phase 5's Activity page should let them be seen, and some later phase should decide when to clear them.
  - **Known limits:** a dedup job over `<service root>/datasets` makes `select_scan_files` walk every dataset folder (including kept versions) to check the manifest; this is small JSON reads on a SLURM node, but it grows with the number of datasets. Timing of the SLURM file checks follows Phase 3's rule: `_SUCCESS` is only looked for once SLURM no longer lists the job.
  - **Checked** with `curl` against a throwaway `$VALIDATOR_SERVICE_ROOT` on the shared disk and real SLURM jobs on three tiny datasets: the check shows commands, blockers and the convert-again warning; a dataset without saved settings is refused with "set up conversion first"; a conversion that fails its checks leaves its scan `conversion_failed` and it never starts; across (one converted, one not): the conversion runs, the scan waits and then starts and finishes by itself, and its record lists both Prepared versions and the duplicate counts (1 and 2) match the data; within: two scans, each finding only its own duplicates (1 and 1), and the first scan's folder was deleted once both datasets had a newer result; convert again on a converted dataset: its old Prepared version and result were gone at once and it was converted before the scan; a second scan on a dataset that already has one going, a convert again and an unregister were each refused while a scan was waiting; cancel stopped a waiting scan (it never got a job, and its conversion kept running) and scancelled a queued one, a second cancel said "not active"; a job that ran out of memory showed `failed` with SLURM's reason and left the old result in place; unregistering one dataset of a shared scan kept the scan, unregistering the last one deleted it (with that dataset's failed and cancelled attempts); bad scanner, mode, settings, resources, duplicate and malformed ids were refused with clear messages; the original files were byte-identical afterwards and the real `output/service` was not touched. **Not checked in a browser** (Phase 4 has no UI). The throwaway data is in `/home/naresh/phase4_test`, outside the repo.
  - **Old `output/` cleanup (build item 10):** after the list was shown and confirmed, deleted `output/prepared`, `output/tool_team_converted`, `output/tool_team_chain`, `output/pilot_chain`, `output/my_chain_test`, `output/tool_team_ner_run`, `output/tool_team_dedup_run`, `output/tool_team_pii_run`, `output/tool_team_dedup_pilot` (and its `.files.txt`), `output/tool_team_toxicity_run` and `output/verify_exclude_system_smoke`. Kept: `output/service`, `output/gpu_diag`, `output/serving`, `output/tool_team_regurgitation`. The old routes that still read `output/prepared` (`/api/runs` and friends) just find nothing there until Phase 12 decides about them.
- **Phase 5** (`validator/frontend/src/`: `scans.ts`, `useScanResources.ts`, `components/ScanDialog`, `ScanJob`, `DeleteList`, and a new `ActivityPage`; backend: `conversions.list_all`, `scans.py`, `app.py`):
  - **Not frontend-only, as the table says: two small backend additions.** `GET /api/conversions` lists every conversion of every dataset, newest first, each with `dataset_name`, from one squeue and one sacct call (`conversions.list_all`; the Activity page needs it, because the only other list is per dataset). Every scan view now has `elapsed`, SLURM's run time of its job (squeue while listed, sacct after). It is `null` for a scan without a job, and for one that already became a result (SLURM isn't asked about those), so the Activity page shows `finished_at - submitted_at` for those, which includes the wait in the queue.
  - **How Phases 6 and 7 plug in.** `ScanDialog` takes a `scannerKey` and builds itself from `GET /api/scanners`: each field is drawn by `SettingControl` (a choice whose only values are `"0"` and `"1"` is a checkbox, any other choice a radio group), the mode choice appears when the scanner has `modes` and more than one dataset is ticked, and the check, cost, commands, GPU confirmation and delete confirmation need nothing from the scanner. A new scanner needs: its entry in `SCANNERS`; its words in `scans.ts` (`SCANNER_LABELS`, `SCANNER_COUNT_LABELS`, and `CHOICE_LABELS` / `MODE_LABELS` where the raw values read badly); and its button in `ActionBar` (`onScan('<key>')`; PII and toxicity are still disabled there). `modeNote` and `dropNote` are dedup-only and return null for other scanners. A setting that isn't a choice (the NER category picker, the Detoxify threshold) needs a new field type in `scanners._check_field`, `ScannerField` in `types.ts` and a branch in `SettingControl`. `HIDDEN_SETTINGS` in `scans.ts` names settings the dialog never shows or sends (dedup's `write_filtered`), so the backend default applies. **Open for Phase 6:** the dialog is for one scanner key, while "Scan for PII" is one action over two (`pii_ner`, `pii_regex`); decide there whether the method choice sits above `ScanDialog` or inside it.
  - **Resources.** The dialog's resources are the scan job's only: `scans.submit` starts any conversion with `jobs.DEFAULT_RESOURCES`, and the dialog says so. `ResourcesFields` now takes `ResourceOptions` (`partitions`, `max_cpus`, `slurm_error`) plus `jobWord` ("Converting" by default, "Scanning" in the scan dialog) and `usesGpu` (the sentence "No GPU is ever requested" is shown unless it is set); `initialResources` takes `{partitions, default_resources}`. The boxes still ask for no GPU, so Phases 6 and 7 must decide how a GPU scan's resources are set before one can be started from here.
  - **The dialog's rules.** It checks again on every change (first check at once, later ones 400 ms after the last change), and Start is only possible when the check answers the choices as they are now (`upToDate`), not just any earlier check. "Leave out" is offered on every dataset while more than one is left, because the blockers include "this scanner is already running on it". The delete confirmation and the main view have their own `key`, so Escape on the confirmation brings the main dialog back. **`ConvertDialog` still has that Escape problem and is unchanged**: its confirmation reuses one `<dialog>`, which Escape closes natively while the page still counts it as open, so "More → Convert only…" would then do nothing until the page is reloaded. This is read from the code, not seen in a browser.
  - **Datasets page.** The chips read `Dataset.scans` (optional in `types.ts`: register and PATCH answers don't carry it) and read like "Duplicates: Done (12)". The page reads the list again every 10 s while any dataset has a scan waiting, starting, queued, running or unknown, and keeps the selection and the search.
  - **Activity.** One loader runs `GET /api/scans` and `GET /api/conversions` together and merges them by id (ids begin with the UTC time they were made, so a bigger id is newer), and asks again every 5 s while anything is going; "Refresh" asks at once. Cancel uses the existing cancel routes; cancelling a waiting scan says its conversions keep running, and they stay listed to cancel too. "Details" opens `ScanJob` (new) or `ConversionJob` (now with an optional `title`). Failed and cancelled scans are listed and nothing clears them yet (see the Phase 4 note). Phase 10's filter jobs become a third `Item` kind. The dataset History tab has a Scans card, from the same `GET /api/scans`. Small refactors on the way: `DeleteList` was taken out of `ConvertDialog`, and `Dialog` got a `wide` option.
  - **Checked:** `npm run build` and `npm run lint` are clean. The backend was checked with `curl`-style calls against a throwaway `$VALIDATOR_SERVICE_ROOT` (`/home/naresh/phase5_test`, outside the repo) on a separate uvicorn (port 8766) and real SLURM jobs on two tiny datasets: `GET /api/conversions` with no conversions (99 seeded datasets, 0.06 s warm), with one, and with several (a failed attempt, a converted one, a "convert again" and a cancelled one); the answers of `/api/scans/check`, `/api/scans/submit`, `/api/scans` and `/api/scans/{id}` were compared key by key with the types in `types.ts`; a scan across one converted and one unconverted dataset showed both jobs, waited, then started and finished by itself; `elapsed` read `0:00` while queued, counted up while running and was `null` once the scan was a result; two scans within each dataset replaced the across scan (its folder was deleted); "convert again" on one dataset deleted its data and its scan; cancelling a waiting scan answered `stopped_waiting` and its conversion could then be cancelled on its own; the original files were byte-identical afterwards and the real `output/service` was not touched. **The pages were not clicked through in a browser** (none on the login node), so do that before relying on Phase 5: "Find duplicates" in both modes, the two jobs on Activity, "Convert again" with its confirmation and the data gone afterwards, "Leave out", Cancel from Activity, the chips following a running scan, and the History tab.
  - **A trap while testing:** a dataset registered with an empty `map` for `{"messages": [...]}` rows converts with "0% text coverage" and fails its own checks (`checks_failed`). Save the mapping the preview suggests (`messages → messages`) in its conversion settings first.
- **Phase 6** (`validator/backend/scanners.py`, `scans.py`, `reader_imports.py`; `validator/frontend/src/`: `scans.ts`, `types.ts`, `useScanResources.ts`, `components/ScanDialog`, `ResourcesFields`, `ScanJob`, `ActionBar`, `DatasetsPage`; scan tools: `launchers/slurm/submit_privacy_filter.sh`, `modelsafety/data_screening/pii/regex/pii_validator.py` and `pii_output.py`, new `modelsafety/data_screening/pii/model/nemotron_labels.json`):
  - **Two scanners, one action.** `pii_ner` ("PII (NER)") and `pii_regex` ("PII (rules)") are separate entries in `SCANNERS`, so every dataset has a chip for each (`dataset.scans.pii_ner`, `.pii_regex`). The action bar's "Scan for PII" opens one dialog with a "Method" choice: `SCAN_ACTIONS` in `scans.ts` maps an action key to a title and its scanner keys (`dedup` to `['dedup']`, `pii` to `['pii_ner', 'pii_regex']`); `ScanDialog` takes `action` instead of `scannerKey`, and an unknown action is taken for a scanner key. Phase 7 adds `toxicity: {title, scannerKeys: ['toxicity']}` and enables its button (`ActionBar` still has it disabled). Each method keeps its own settings and resource boxes while the dialog is open (not reset on switching, as the plan said); the check and the GPU confirmation are reset.
  - **Framework hooks Phase 7 reuses (toxicity also prepares, then scans).** `Scanner.read_jobs(output)` returns `(jobs the scan's own job waits for, its own job ID)` and is also called on the output of a command that failed (a job started before the failure is kept: the record's `prior_jobs`, `[{role, job_id}]`, roles `plan` for NER and `scan` for a scan's own job; `jobs` in every scan view lists all of them, `job_id` stays the own job). `Scanner.log_files(scan folder, settings, jobs)` replaced `(logs folder, job id)`. `Scanner.success_markers` (paths under `results/`, all must exist; the regex scan has three) replaced the single `_SUCCESS`; `finished_at` is the newest marker's time. `Scanner.fixed_resources(settings)` is for a scanner whose job scripts fix the CPUs, memory and time: only the partition is taken (anything else is refused with a message), `scan.resources` holds the partition and what the script's `#SBATCH` lines ask for (shown, not sent), and `scanners.script_resources`/`sbatch_header` read them (`sbatch_header` is a copy of `step_start.py`'s, which stays until Phase 12). `Scanner.detects` is a read-only list (the regex categories). New setting types: `integer` (min, max, empty means the default, which may be null; the dialog sends the text as typed) and `multi` (a list of the choices, at least one, kept in the order of the choices, optional `groups` for display). `ENV_PREFIXES` is now `DEDUP_`, `PII_` and `PRIVACY_FILTER_`; `_check_export_values` refuses a comma or line break in a value, for every scanner. `scanners.cost` is called inside the same `try` as the command, so a `ScannerError` from it blocks the scan instead of failing the request.
  - **Scan state with several jobs.** A prior job that failed or was cancelled makes the scan failed or cancelled ("The plan job (N) failed: ..."), whatever became of the job waiting for it. The scan counts as running while its own job is queued behind a prior job that runs. Cancel runs `scancel` on every job the record lists that SLURM still has. A scan whose command didn't name its jobs after the scan (NER's job scripts name their own) says "jobs submitted at <time>" instead of a job name when it was being started as the service stopped. A start command that failed after its first job was submitted says so in `submit_error` and names that job ("may still be running: stop it with scancel"): nothing cancels it for you.
  - **NER.** `env REPO=... PRIVACY_FILTER_*=... bash launchers/slurm/submit_privacy_filter.sh` (not `tool_team_ner_scan.sh`, which only adds defaults and printing). Size `full` (default, 8 GPUs on one node, mode `full`) or `one_gpu` (mode `pilot`, with `PRIVACY_FILTER_MAX_FILES=100000` and `PRIVACY_FILTER_MAX_UNITS=0`, as the old chain code did). `PRIVACY_FILTER_KILL_ON_INVALID_DEP=1` is always in the command: the script (new, opt-in) then adds `--kill-on-invalid-dep=yes` to the GPU job, because this cluster has no `kill_invalid_depend` and a failed planning job would leave the GPU job pending for ever. The categories are `inputs/policy.json`: the default policy with every label of `nemotron_labels.json` set to `null` unless chosen, a chosen contextual label at **0.7** (D7), everything else (control markup, ignored values, the other model's labels) kept, and read back with `CandidatePolicy` before use; its path is `PRIVACY_FILTER_POLICY` in the shown command. The 55 labels were read from `OpenMed/privacy-filter-nemotron-v2` (revision `a04eea90...`, both cached snapshots agree): 34 direct identifiers, 2 person-linked, 19 contextual; the default policy enables 36. Cost is read from the job scripts' headers (GPU job plus the planning job), so the GPU confirmation follows. Logs: the job scripts fix `logs/slurm/privacy_filter_*` in the repo (allowed by section 5), plus `results/worker_logs/worker_0.log` for `full`. **The policy says a switched-off label was excluded for "contextual_tier"** (`policy.py` gives every `null` threshold that reason, not only contextual labels): not changed here, but Phase 9 must not read it as "this label is contextual".
  - **Rule-based.** `sbatch` on `run_pii_scan_and_extract.sbatch` with `PII_STAGES=all`, `PII_NUM_WORKERS` (the setting, or one per CPU) and `PII_LOG_ROOT` inside the scan folder. It has no `results/_SUCCESS`: the three stage markers must exist. `pii_validator.py` now also writes `stage2_validated/summary.json` (per category `raw` and `kept`, `raw_total`, `kept_total`, `unique_locations`) before it marks stage 2 finished; `metrics.txt` is unchanged and was compared with it.
  - **Counts are findings, not rows.** The chip count is NER's `policy_eligible_spans` or the regex scan's `kept_total` ("PII findings"). Rows flagged come with Phases 8 and 9 (`unique_locations` is already the rows with a kept regex hit).
  - **Checked.** `npm run build` and `npm run lint` are clean. The backend was checked with HTTP calls against a throwaway `$VALIDATOR_SERVICE_ROOT` on a separate uvicorn, with **a stand-in for SLURM** (no SLURM or GPU on the machine this ran on): it runs the real job scripts (`prepare_rows.sbatch`, `run_pii_scan_and_extract.sbatch`, `run_dedup_scan.sbatch`, `submit_privacy_filter.sh` and the manifest job) locally, with `--dependency` and `--kill-on-invalid-dep` honoured. **The NER model and the GPU job never ran**: the GPU job was replaced by a stub that writes a made-up result. What held: every refusal (no labels, unknown label, a list that isn't one, a bad size, CPUs, memory or time given for NER, an unknown partition, a bad `workers`, no GPU confirmation); the rule-based scan converted its dataset, waited, ran the real three stages and became the result (counts equal `metrics.txt`); the NER scan wrote a policy with exactly the chosen labels (contextual at 0.7), the policy path reached the job's environment, both jobs were found from the script's output, the GPU job was submitted with `--dependency=afterok` and `--kill-on-invalid-dep=yes`, and the scan finished and became the result; a failing planning job gave "The plan job (N) failed", SLURM removed the GPU job, and the old result stayed; a failing GPU job left the old result; Cancel stopped both jobs and a second Cancel said "not active"; an NER scan on an unconverted dataset waited, then started both jobs by itself; dedup across two datasets (one unconverted) still worked. The original files were byte-identical afterwards. **Still to do on the cluster:** one real one-GPU NER run on a small dataset (asks for the GPU confirmation), and the first real rule-based run (the job creates its virtualenv and installs `piiregex` on first use). A regex scan that fails before its job logs anything shows only the SLURM files.
  - **Trap while testing:** the regex scan's packages (`piiregex`, `tqdm`) are installed by the job script into `pii/regex/.venv` the first time; without them the job fails at `import piiregex`, and the scan shows "failed" with that in "Scan output".
- **Phase 7** (`validator/backend/scanners.py`, `scans.py`; `validator/frontend/src/`: `scans.ts`, `types.ts`, `components/ScanDialog`, `ScanJob`, `ActionBar`, `App.css`; scan tool: `scripts/tool_team_toxicity_scan.sh`):
  - **One scanner, `toxicity`** (stages "Quick screen: Detoxify (Unitary, multilingual model)" and "Judge: Qwen3Guard-Gen-8B"), one scan per dataset (D4). The action `toxicity` in `SCAN_ACTIONS` opens the dialog, and "Scan for toxicity" in the action bar is enabled. The command is `env TOX_KILL_ON_INVALID_DEP=1 bash scripts/tool_team_toxicity_scan.sh all --input-root <Prepared version> --run-dir <scan>/results --input-mode runs --pilot-files 0 --shards N --concurrency N --threshold T --cpus .. --mem .. --time .. --partition ..`: the wrapper, not a command of its own, because it already clamps the shards to the number of files, checks the run identity and submits both jobs. The wrapper's new opt-in `TOX_KILL_ON_INVALID_DEP=1` adds `--kill-on-invalid-dep=yes` to the array job, so a failed planning job takes the array down (checked: the array went to CANCELLED at once). Its jobs are the planning job (role `plan`, `Submitted prepare job: N`) and the array job (role `scan`, `Submitted all job: N`).
  - **Settings.** `threshold` is a new `decimal` setting type (`min`, `max`, `step`; 0.05 to 0.95 in steps of 0.05, default 0.5; checked in whole hundredths, so 0.07 and 0.1+0.2 are refused, and NaN and Infinity too). `categories` (the nine Qwen3Guard categories, all on) and `verdicts` (Unsafe and Controversial) are saved in the scan's settings and are **not** in the command (D8): the scan always judges every category, and they only decide what counts as flagged when results are read. The guard category list is a constant in `scanners.py` (`GUARD_CATEGORIES`), copied from `CATEGORY_RE` in `llm_check_guard.py` (which needs `openai`, so it isn't imported): keep them in step. `shards` and `concurrency` (default 8 and 8, `concurrency` can't be more than `shards`) are shown under "Advanced" in the dialog (`ADVANCED_SETTINGS` in `scans.ts`). Unlike NER, the resources are chosen (default 16 CPUs, 96G, 48:00:00 for each shard). Cost is the array script's GPUs times `concurrency`, and the scanner refuses to show a cost when the script has no GPU request, so a scan can't start without the GPU confirmation. Guard errors count as flagged (D9): a note in the dialog, not a setting.
  - **Framework changes Phase 8 and later can rely on.** (1) **Array jobs.** `scans.py` has its own array-aware `_read_slurm`, `_sacct_rows`, `_slurm_outcome` and `_slurm_elapsed` (copied from `steps.py`, which Phase 12 deletes): a job is going while any of its tasks is listed, running when any task runs, queued when all are pending (`DependencyNeverSatisfied` on all of them means failed), and failed when any task failed (the reason names it). `accounted` is now `{job id: [one row per task]}`, not `jobs.accounting`'s shape. `cancel` treats a job as going while any task is listed (before this it would have done nothing for an array). (2) **Markers that aren't a fixed list.** `Scanner.markers_for(results)` adds markers to `success_markers`; use `scanners.markers_of(scanner, results)`. Toxicity's are the manifest and every shard's `_SUCCESS`; the state message names the first three that are missing. (3) **Progress.** `Scanner.read_progress(results)` gives `[{stage, done, total, unit}]`, shown as `progress` in every scan view and as bars in `ScanJob`. It is only asked while the scan's own job is running (`own_job_running` in the state), and lists the shards folder instead of asking for each shard by name. NER and the others don't report progress yet. (4) `ENV_PREFIXES` also strips `TOX_`, `GUARD_`, `VLLM_` and `QUALITY_` (the wrapper reads them); `HF_TOKEN` and `PATH` are kept.
  - **Results.** `read_details` adds up the shards' `detoxify_summary.json` and `guard_results/summary.json` (shards, rows read, messages scored and flagged, guard records, errors, unknown, verdict and category counts). The chip count (`headline_count`, "messages") is the messages the guard did **not** call Safe, plus the ones with no usable verdict, from the summaries' `total_records` minus `safety.Safe`. It ignores the saved categories and verdicts, because the summaries don't have verdict-by-category counts and reading each shard's `all.jsonl` would break "list pages read only small files". **Phase 8 must replace it with the count under the scan's saved (or changed) categories and verdicts (D8).** The summaries' `categories` counts are per category over all verdicts. A verdict can come with the category `None`: Phases 8 and 9 must decide how that counts under a category filter.
  - **Shards are made from files**, one Prepared file is 1000 rows by default, so a small dataset gets fewer shards than asked for (the wrapper says so, and the cost line says "never more shards than files").
  - **Checked.** `npm run build` and `npm run lint` are clean. Backend, against a throwaway `$VALIDATOR_SERVICE_ROOT` on this cluster's **real SLURM** (the regression check Phase 6 couldn't do): the array reading against real CPU arrays (all tasks fine, one task failing, a failing job with an array behind it that has no kill-on-invalid-dep, cancel, plain jobs succeeding, failing and cancelled as before); every refusal (threshold off-step, too high or text, no category, unknown category, Safe as a verdict, no verdict, `concurrency > shards`, `shards` 0, unknown setting, settings not an object, unknown partition, no GPU confirmation); a **stand-in** for the wrapper (CPU jobs of the same shape: a planning job and an array behind it with `--kill-on-invalid-dep`) driven through the real service: conversion, waiting, both jobs found, running behind the planning job, progress per stage, finished, became the result with the chip count, details and logs read; a failing planning job gave "The plan job (N) failed" and its array was cancelled; a failing task gave "task 1: SLURM state FAILED, exit code 3:0." and the old result stayed; Cancel stopped every task and a second one said "not active"; a real dedup scan (CPU, small) still went queued, running, succeeded and became the result. The **real wrapper** was run once with a fake `sbatch` that only recorded its arguments (nothing submitted): it took the command as built, clamped 4 shards to 3 files, and made the planning submission and the array submission with `--dependency=afterok:<plan> --kill-on-invalid-dep=yes --array=0,1,2%2`, and the job IDs it printed were found by `read_jobs`. **Still to do on the cluster:** one real toxicity run on a small dataset (asks for the GPU confirmation): Detoxify, the vLLM server and Qwen3Guard have never run from the service. Check that `HF_TOKEN` (from the environment or the repo's `.env`) and the vLLM image (`/home/shared/containers/vllm-0.22.0-cu129.sqsh` here) are what the wrapper expects.
  - **Trap while testing:** the compute nodes can't see `/tmp` of the machine the service runs on, so a throwaway service root and its test data must be on the shared disk (for example under the repo's `output/`). A dataset converted without a field mapping (`map`) fails its text coverage check ("0% text coverage"): a JSONL with a `messages` column needs `map: {messages: messages}`.
- **Phase 8** (`validator/backend/results.py`, `scanners.py`, `registry.py`, `scans.py`, `app.py`; scan tools: `modelsafety/results/coverage.py`, new `modelsafety/results/analyse.py`, new `launchers/slurm/run_result_analysis.sbatch`; `validator/frontend/src/`: `route.ts`, `api.ts`, `types.ts`, `scans.ts`, `App.tsx`, `ScanDialog`, `ActivityPage`, `DatasetPage`, `DatasetsTable`, `ResultsTab`, and new `ResultPage`, `ToxicityFilter`, `CoverageBadge`, `ScannerChips`):
  - **The numbers come from one CPU SLURM job per finished scan, the "analysis" (your choice over doing it inline).** `results.refresh()` (its own 30-second daemon, started in the lifespan) submits it for every scan that is a result (`result_applied_at` set and still pointed at) and has no `<scan>/analysis.json`, at most 10 a pass. The job runs `analyse.py`, which writes `<scan>/coverage.json` and then `<scan>/tally.json` (the tally only for NER, toxicity and a dedup scan across datasets; none for regex PII or dedup within). `sbatch` is an argument list with `ANALYSE_REPO` in `--export`, the logs are `<scan>/_logs/analysis-<job>.out|.err`, and the record `analysis.json` holds the exact command, the job and `applied_at`. Fixed resources: default partition, 4 CPUs, 32G, 04:00:00 (`ANALYSIS_RESOURCES`). Its state is read live (`scans._slurm_outcome`), and succeeded means SLURM completed it **and** the expected files exist. Nothing asks for `coverage.json` or `tally.json` before SLURM stops listing the job (the shared-disk lag again), so "Check again" decides coverage-only or full from the earlier record (`has_tally`), never from the files. From a scan finishing to its numbers on the page takes about a minute and a half (three 30-second passes and a short job).
  - **Coverage.** Per-dataset scans are checked with `--root <Prepared version folder>`; a scan across datasets with `--root <service root>/datasets` and the new `coverage.py --files-from inputs/files.txt` (`build_report(..., only_files)`, `narrow_selection`): the expected files, the runs found and what was skipped are narrowed to the scan's own list, and a listed file no finished run wrote is a coverage problem, not a crash. Existing behaviour is unchanged without the flag. Checked: with a third converted dataset in the folder, coverage fails without `--files-from` and passes with it. The report is sliced per dataset by its run (`.` for a scan of one version folder, `<id>/prepared/<version>` across); a problem with no run fails every dataset of the scan, as the report says. The verdict and the exact count are copied into the dataset's result by `registry.update_result` (only `count` and `coverage`, only while the result is still that scan), once, when the job has succeeded. **A dedup scan across still makes `find_runs` walk every dataset folder**, so coverage of one scan grows with the number of datasets (the Phase 4 limit).
  - **What the tally holds** (`tally.json`, `TALLY_VERSION` 1). Toxicity: from each shard's `guard_results/all.jsonl`, messages by (verdict, categories), the guard errors (`rules.guard_outcome`, so an unusable verdict counts as an error, D9), and rows by the set of atoms their messages carry (`"<verdict>|<category>"`, `"<verdict>|"` for no category, `"error"`), which gives exact counts under **any** verdicts and categories without reading the files again (`scanners.toxicity_flagged`). **A flagged verdict with no category always counts** (your choice), a guard error always counts, and a category this service doesn't know can't be unticked. NER: from `all_spans.jsonl`, rows with an eligible span, and eligible spans by label and tier (`rows_flagged`, `eligible_by_label`, `eligible_by_tier`). Dedup across: from `dedup_results.jsonl`, each dataset's duplicates by the dataset that holds the kept copy (`--group <dataset id>=<id>/prepared/<version>/`). The files are streamed and the row sets held in memory (a few GB for millions of rows, hence the 32G).
  - **Scanner side.** `Scanner.read_result(ResultContext)` says what one dataset's result holds: `flagged {count, unit, rows}`, `breakdowns`, `facts` (and `filter` for toxicity); `Scanner.needs_tally(mode)` says whether the analysis has to tally. `toxicity_headline_count` now returns `None`: the chip count is set by the analysis, under the scan's saved categories and verdicts (D8), so it never shows a number that later changes. For the real toxicity scan it is 159 (112 Unsafe, 38 Controversial and 9 guard errors), which is what Phase 7's count was, because every category was ticked. A chip count is the headline unit of its scanner: duplicate rows, PII findings (spans for NER, matches kept for regex) or messages.
  - **Routes.** `GET /api/datasets/{id}/results` (all four scanners), `GET /api/datasets/{id}/results/{scanner}?categories=&verdicts=` (comma-separated; empty means none; only toxicity takes them; checked against `GUARD_CATEGORIES` and `TOXICITY_REMOVABLE_VERDICTS`; they only change what is shown and are never saved), and `POST /api/datasets/{id}/results/{scanner}/check` ("Check again": coverage only when the counts exist, full otherwise; refused while one is going). `ScanSummary` (`dataset.scans[scanner]`) has a new `coverage` (`pass`, `fail` or null). **While a check is going or after it failed, the page shows no numbers from it** (they only come from a succeeded analysis), only the chip count from the registry, so "Check again" makes the numbers vanish until it ends.
  - **Frontend.** The address `#/datasets/<id>/results/<scanner>` (the scanner has to be in `SCANNER_KEYS` in `scans.ts`; keep it in step with `backend/scanners.py`) opens `ResultPage`, and `#/activity/<scan id>` opens Activity with that scan's details open and in view (and says so when the scan was deleted because a newer one replaced it). The Results tab has a card for each scanner, the dataset header and the Datasets table share `ScannerChips` (a finished scan's chip opens its result page; a failed coverage check gives a warning mark and the warn tone), and "Run again" opens `ScanDialog` through its new `initial` prop with the scan's settings (minus the hidden ones), its mode and **all** the datasets the scan covered that are still registered (a dedup scan across one dataset would otherwise silently become a scan within).
  - **Phase 9 should know.** The tally's rows flagged are the numbers the findings index has to reproduce: toxicity 118 rows under the saved choices of the real scan, 15,770 for the real regex scan (`unique_locations`), duplicate rows for dedup. The index could be one more output of this same job (`analyse.py` and `results.py` already submit, track and apply a job per scan), instead of a second mechanism; the analysis's file readers use the same field names as `modelsafety/filter/rules.py`, which Phase 9 reads too.
  - **Checked.** `npm run build` and `npm run lint` are clean. Backend, against throwaway service roots in `/home/naresh/phase8_test` (outside the repo) on this cluster's real SLURM: (1) a fresh root with two tiny datasets and a real dedup scan across them: conversions, scan, result, then the analysis was submitted by itself, `coverage.json` and `tally.json` were written, the chips got `coverage: pass`, p8b's two duplicates were split one kept in p8a and one in p8b; "Check again" ran a coverage-only job, a second one was refused while it went, and a third dataset in the folder didn't change the verdict; a second scan replaced the first (its folder was deleted, the new result started with no verdict and got its own). (2) A second root whose scans read the **real** results of this dataset through symlinks (nothing in `output/service` was written; the real files were byte-identical afterwards): dedup (118,432 duplicate rows), regex PII (25,668 findings in 15,770 rows) and toxicity (159 messages in 118 rows) all passed coverage on 310,537 rows with the same counts as Phases 5 to 7; the toxicity filter gave 121 messages (86 rows) for Unsafe only, 15 for only Violent and 9 with no categories, and a category with an `&` in its name round-tripped. A shard's `_SUCCESS` removed from a throwaway copy gave `fail` with the named problem and a warning mark on the chip; a corrupted line in a copy gave a failed job (`exit code 2:0`) with the message in its log, a cancelled job was followed by a full run, and every bad request (unknown scanner, category or dataset, categories on a non-toxicity result, a scanner with no result) was refused with a clear message. The pages were looked at in headless Chromium screenshots (Results tab, toxicity, regex and dedup-across result pages, the Datasets page, the Activity link). **Not checked:** clicking through anything (no scripted browser), so the Run again dialog was only checked by sending its prefilled values to `/api/scans/check` (all four scanner types were accepted); and **NER on real output**: the real NER scan (job 514592) was still running, so its tally was tested on an `all_spans.jsonl` joined from its partial unit files (821,826 spans, 257,854 eligible, 40,260 rows in 7 seconds, equal to a direct count), and its coverage on that folder rightly failed. Once it has finished and the service is restarted, check its analysis (the peak memory of the job in `sacct`).
  - **Also.** `SCAN_SERVICE_PHASES.md` had been committed with an unfinished merge: the notes for Phases 2 to 5, the section 11 heading and the ticks for Phases 2 to 5 were missing. They were restored from `stash@{0}`, which is still there (drop it when you are happy). The log tails of a job that has just ended can be empty for a few seconds (shared-disk lag): the page asks again. A scan deleted while its analysis runs makes the job fail; that is rare and harmless. The running service has to be restarted to pick all this up, and then it submits the analysis of every result it already has (three short CPU jobs on the real data now, and a fourth once the NER scan has finished).
- **Phase 9** (`modelsafety/results/analyse.py`, `launchers/slurm/run_result_analysis.sbatch`, `validator/backend/results.py`, new `validator/backend/flagged.py`, `app.py`; `validator/frontend/src/`: `types.ts`, `api.ts`, new `flagged.ts`, `App.css`, and in `components/` `ResultPage` plus new `FlaggedRows`, `FlaggedRowPanel`, `RowText`):
  - **The index is one more output of the Phase 8 analysis job** (your choice over a second mechanism), so every scanner's index is made by `analyse.py` on a CPU node, never inline. New flags `--index-dir DIR` with `--index-name NAME` (the root is one dataset's Prepared version) or one `--group NAME=PREFIX` per dataset (a scan across); `--coverage-only` refuses them. It writes `<scan>/index/<dataset id>.jsonl` (one line for each flagged row, in file and line order) and `<dataset id>.counts.json` (`rows`, `findings`, `by_reason`, `by_name`), after the tally; any failure (an unreadable result file, **a Prepared line whose fingerprint isn't the one the scan recorded**, conflicting fingerprints) is exit code 2 with the file and line in the job's log and no partial index. The findings come from `modelsafety/filter/rules.py` (`READERS`, `HitIndex`) under `FilterRules()` with nothing ignored, so every label, category, verdict and guard error is in the index and the user's choices are applied when it is read. `rows_by_file` is built from each chosen version's own `summary.json` (`select_scan_files`), so a scan across datasets doesn't walk the datasets folder here.
  - **An index line:** `file` (**relative to the dataset's Prepared version, in both modes**), `line`, `sha256`, `byte_offset` (where the line starts), `preview`, `reasons`, `labels` (NER, label to spans), `categories` (regex, category to matches), `atoms` (toxicity, the same `"error"` and `"<verdict>|<category>"` the tally counts rows by, with the same category cleaning, `analyse._clean_categories`), `findings` (at most 2000 a row, `findings_total` says how many), `duplicate_of` (`{dataset, file, line, sha256, byte_offset}` of the kept copy, `dataset` being the id of the dataset it is in, which for a scan across can be another one). **Phase 10's drop list can be made from `file`, `line` and `sha256` as they are.** The Prepared files with flagged lines (and the kept copies) are read once, in the same job: that is where the offset, the fingerprint check and the preview come from.
  - **Where the plan was off.** (1) Regex PII has **no character offsets** (`pii_extract.csv` holds the value and the whole line), so its highlights are found by searching for the value in the row's text, once for each distinct (category, value), as the old code did; every one of 200 real rows had all its matches placed. (2) Dedup records carry no text, so every preview comes from the Prepared line. (3) **The preview is the text of the message a finding is in** (toxicity and NER name one; the lowest number if several), and only otherwise the first message that isn't a system message, as the plan said: on the real toxicity result the plan's version showed a harmless user question next to "Unsafe: Non-violent Illegal Acts" because the flagged message was the system prompt.
  - **Analysis records and "Check again".** The record has `writes_index` and `has_index`. **A record that was applied before this phase (no `has_index`) gets one new full analysis, by itself** (`results._maintain`, within the 10 a pass; tested with the real toxicity and dedup records), and the numbers vanish until it has finished, as with any check. A record whose job failed, or that says it has no index, is left for "Check again", so a scan whose index can't be made isn't resubmitted every 30 seconds. "Check again" is coverage-only only when the earlier succeeded record has the tally (if the scanner needs one) **and** the index, and then keeps both (`_submit(..., earlier)`); regex PII, which has no tally, is now checked coverage-only too. `ScannerResult.has_index` is true only for a succeeded analysis that wrote the index; while one is going the result page says so instead of showing the table, and the routes refuse with "still going".
  - **Routes** (`flagged.py`, read-only). `GET /api/datasets/{id}/results/{scanner}/flagged?page=&page_size=&reason=&name=&categories=&verdicts=` (50 a page, at most 200). `reason` and `name` must be keys of the counts file (name: a NER label, a regex category, or the id of the dataset a kept copy is in); toxicity takes `categories` and `verdicts` instead, defaulting to the scan's saved ones, with the same refusals as the result route, and refuses `reason` and `name`. Without a filter the lines before the page are skipped unparsed and the total is the counts file's `rows`; with one the whole index is parsed. The answer also carries `options` (the names to filter by with their row counts; none for toxicity, and the page shows a drop-down only when it has two or more). `GET .../flagged/row?file=&line=&categories=&verdicts=` finds the entry by looking for the file's name in each index line before parsing it, opens the Prepared line **only if the version's `summary.json` lists the file**, at `byte_offset`, and compares its fingerprint; it returns the segments, NER `highlights` (each checked to lie inside its message), regex `highlights`, toxicity `message_flags` (each with `counts`, under the current choice), `unplaced`, `origin` (the line's `_origin` note) and `prepared.run_dir` (what "Show original" sends to `/api/origin`), and for a duplicate `duplicate_of` with the kept copy's dataset name, text and origin, read from the copy's own dataset's version (`scan.versions`). The service key is `pii_ner`/`pii_regex`, but `analyse.py` and `rules.py` say `ner`/`regex_pii`; `flagged.py` uses the service's and `analyse.py` the other.
  - **Frontend.** A "Flagged rows" card on the result page after the breakdowns (`FlaggedRows`, remounted by a new scan or a new toxicity choice, which starts it again at page 1): drop-downs, a table (checkbox, `file:line`, preview, what was found, Open), Previous and Next, and the row below (`FlaggedRowPanel`: findings, `RowText` with the marks, or for a duplicate the row and its kept copy side by side, and "Show original" for each). **Ticking rows does nothing yet**: the ticks (a set of `"<line>|<file>"`) live in `FlaggedRows`, are kept while paging and cleared by a filter; Phase 10 needs them lifted or, for "all rows matching the filters", resolved on the server, where `flagged._matches` and `_counted_atoms` are what a page uses. The reason filter exists but is never shown today (each of the four scanners has one reason, toxicity doesn't use it); it is there for the other dedup mode. `rows.py` and `removed.py` are still there: their helpers were copied into `flagged.py` (Phase 12 deletes them).
  - **Checked.** `npm run build` and `npm run lint` are clean. On this cluster's real SLURM, against two throwaway service roots in `/home/naresh/phase9_test` (outside the repo): (1) one whose scans read the **real** results of the 310,537-row dataset through symlinks (nothing in `output/service` was written, and the original data and the real Prepared `summary.json` are unchanged): the index rows equal the numbers of Phases 5 to 8: toxicity 118 rows and 159 findings, regex PII 15,770 rows and 25,668 findings (`unique_locations`, `kept_total`), dedup 118,432, and **NER 103,929 rows and 659,662 findings (the tally's `rows_flagged` and the chip count): the first real NER analysis, 1 min 5 s and 3.5 GB peak in `sacct`**, the other three 5 to 21 s and at most 1.7 GB. Index sizes: 89 KB, 8 MB, 69 MB, 126 MB. The toxicity page under the choices gave 86 rows for Unsafe only, 9 with no categories ticked (the guard errors) and the same totals as Phase 8, and a category with `&` round-tripped; filter totals equal the option counts; unfiltered pages took 10 to 60 ms, the last page of the NER index 0.5 s, a filtered NER page 1.5 s (it parses all 126 MB). Of 60 real NER rows and 200 real regex rows opened, every highlight's text was the text it names (469 and 647), none was unplaced, and every fingerprint matched; 200 random dedup rows and their kept copies were read back at their `byte_offset` and matched. "Show original" opened the right original row from a toxicity, a regex and (through the kept copy's own origin) a dedup row. "Check again" ran coverage only and kept the tally and the index; the route said "still going" while it ran. A dedup scan across two tiny datasets (real conversions and scan): p9b's rows were one kept in p9a and one in p9b, the page, its filter and the row view agreed with the result page's breakdown, and the copy's text came from p9a's version. A copy of the Prepared data with one changed line made the job exit 2 with "ds-…/chunk-0/data_000000.jsonl line 1 isn't the line that was scanned" and no index; every bad argument combination and bad request (unknown reason or name, categories on a non-toxicity result, `../` as a file, a line that isn't flagged, a page of 0) was refused with a clear message. "Check again" on the regex scan (no tally) was coverage-only too and kept its index. The pages were **clicked through in headless Chromium driven over the DevTools protocol** (Node's built-in `WebSocket`, no new dependency): the table, a filter, opening a row, the highlights, "Show original" and the dedup side by side.
  - **To know.** The running service has to be restarted, and then it submits one full analysis for every result it already has (four short CPU jobs on the real data, about a minute for the NER one). Until then the new frontend (rebuilt into `validator/frontend/dist`) shows "checked before flagged rows were listed" on a result page. The highlighted text sits inside a message box that scrolls (the old UI's 480 px), so a mark lower in a long message isn't visible until the box is scrolled. The throwaway data and the DevTools script are in `/home/naresh/phase9_test`, outside the repo.
- **Phase 10** (`modelsafety/filter/rules.py`, `filter_rows.py`; new `validator/backend/filters.py`; `registry.py`, `conversions.py`, `reader_imports.py`, `app.py`; `validator/frontend/src/`: new `drop.ts`, `components/DropDialog` (it holds `DropBar`) and `FilterJob`, and `types.ts`, `api.ts`, `flagged.ts`, `App.css` and in `components/` `FlaggedRows`, `ResultPage`, `DatasetPage`, `DatasetsPage`, `DatasetsTable`, `DataTab`, `ActivityPage`, `ScanDialog`, `UnregisterDialog`):
  - **Decisions taken** (the question put to you was interrupted, so the plan's defaults were used; say if one is wrong): (1) a dataset that has filtered versions **can't be unregistered**: `registry.unregister` refuses and names them (the other choice was to keep the parent's folder as a leftover); (2) one drop can combine scanners, and the choice lives on the dataset page across its result pages; (3) the name rule now allows `,` `(` and `)`, because D11's "<parent name>, filtered (N removed)" needs them (a clash gets " 2", " 3", and a long parent name is cut to fit 100 characters); (4) a result whose coverage check failed can still be dropped from, with a warning in the dialog; (5) a filtered dataset copies its parent's `path` and `conversion`, so its Overview and original rows work, but it is never converted.
  - **The tool.** `filter_rows.py --drop-list FILE` takes a JSONL file, one line for each row and scanner: `file`, `line`, `sha256`, `scanner`, `scan_id`, `reasons` (`rules.read_drop_list`). It removes exactly those rows with the reason `manual` (`REASON_SCANNERS["manual"]` is `drop_list`); each removed-row entry's `findings` hold the scanner, scan and reasons that put it there (a row listed for two scanners has both). It can stand alone, with no scanner results folder, and every existing check stays: the input runs finished, nothing skipped, and each listed line's fingerprint is the line's. `run_identity.json` and `summary.json` only gain a `drop_list` block **when the flag is given**; the identity of a run without it was compared with the committed code's and is identical. The plan said the identity holds `{path, sha256, entries}`; it holds `{path, sha256}` (the hash pins the content) and `entries` is only in the summary. A drop list inside the output folder, with a line listed twice for one scanner, empty, or with a bad scanner, scan or reason name, is refused.
  - **How a drop works** (`filters.py`). The request is `{version_id, selections: [{scanner, scan_id, rows: [{file, line}] | null, all_matching: {reason, name} | null, categories, verdicts}]}`: exactly one of `rows` and `all_matching` for each scanner. `categories` and `verdicts` are toxicity's choice and sit on the selection, not inside `all_matching` as the plan had them, because they also decide what counts for rows ticked one by one. `check` and `submit` find the rows in each scan's findings index with `flagged._open_index`, `_toxicity_choice`, `_filter`, `_matches` and `_counted_atoms`, which are what a page uses, so what is dropped is what was listed. They refuse when the scan is no longer the dataset's current result for `version_id`, or a chosen row isn't flagged (any more) under the choice. A toxicity row's reasons come from the atoms that count (`error` is `toxicity_guard_error`, `<verdict>|...` is `toxicity_<verdict>`). Limits: 10,000 rows ticked one by one, and 5,000,000 drop-list lines (held in memory). `submit` runs under its own lock, in this order: resources checked against SLURM, rows resolved, **the parent's version marked `kept` (at submit, not when the job finishes, so "convert again" can't delete the folder under the job)**, the new dataset registered, `drop_list.jsonl` written, the empty version folder made (the shared-disk rule), `_jobs/filter.json` written and stamped, then `sbatch launchers/slurm/filter_rows.sbatch` (CPU, the default partition, 2 CPUs, 32G, 4 hours, `FILTER_REPO` in `--export`; the command shown is the command that runs). A failure before `sbatch` removes the new dataset again; a failed `sbatch` leaves it registered, showing why, and it can be unregistered. `filters.refresh` makes the output the new dataset's `prepared` (with `removed`) once SLURM no longer lists the job, `_SUCCESS` is there, a `filter_rows.py` summary newer than the submit passed its checks, and its `rows.removed` equals the rows chosen. It runs when datasets or filter jobs are read and in a 30-second daemon started by the lifespan (**not in the plan**, like the scans' and results' loops).
  - **Routes.** `POST /api/datasets/{id}/drop/check` and `.../drop/submit`; `GET /api/filters` (Activity); `GET /api/datasets/{id}/filter` (state, drop counts, command, log tails); `POST .../filter/cancel`. A dataset in `GET /api/datasets` and `GET /api/datasets/{id}` has `filter` (`state`, `reason`, `removed`, `parent_id`, `parent_name`, `version_id`; null for a dataset that isn't filtered), and the one-dataset route also `filtered_versions`. The states are the scans' (`queued`, `running`, `succeeded`, `failed`, `cancelled`, `unknown`). The unregister route goes through `filters.unregister`, which refuses while the dataset's filter job is going (and then calls `scans.unregister`). Cancelling a filter job leaves its dataset registered with no data, showing "cancelled"; unregister it to remove it. `POST /api/run` on a filtered dataset's version answers `kind: 'filter'` (it did already); `useRun` and `openRun` now return `AnyRunResponse`.
  - **Registry and conversions.** `registry.register_filtered`, `mark_kept`, `filtered_versions`, `filtered_name` (the name preview) and `filtered_without_version`. The same-input check skips filtered datasets, and a PATCH of `conversion` on one is refused. `conversions._plan_item` gives a dataset with a `parent` the status `filtered`, so the conversion check, submit, `start_conversion` and the scan dialog all refuse to convert it (a finished one with no "convert again" still reads as converted and can be scanned). `kept` is never un-marked: unregistering the last filtered version leaves the parent's version marked, and a later "convert again" then sets the old folder aside instead of deleting it (harmless, one leftover folder).
  - **Frontend.** The ticks moved up: `DatasetPage` holds the choice (`drop.ts`: per scanner, either ticked rows or "all matching the filters", with the scan and, for toxicity, the categories and verdicts it was made under) and forgets it when the dataset's converted data changes; `FlaggedRows` is controlled, with "Choose all N matching rows" (not only the page; a filter change or a new toxicity choice clears that scanner's part, and `ResultPage` drops a part whose scan was replaced). `DropBar` (sticky, bottom of the window, only on the Results tab and the result pages) opens `DropDialog`, which asks the server to count when it opens: distinct rows, rows by scanner and reason, how many rows several scans flagged, the new name, warnings (failed coverage, every row dropped), "Advanced" with the command, and "Drop N rows". After it, a notice links to the new dataset and Activity. Activity has a third kind of item (`FilterJob`: state, Cancel, drop counts, command, logs). A filtered dataset's header says what it was made from and how many rows were dropped, its Rows column says "Filtering…" or "Filter failed" while it has no data, its Data tab shows the kept rows and a "Dropped rows" summary (it has no skipped rows), its Conversion tab explains it is never converted, and the parent's header lists its filtered versions. The dataset and Datasets pages read again every few seconds while a filter job is going.
  - **For Phase 11.** The automatic drop can call `filters.submit(dataset_id, request)` with `all_matching: {}` for the scan, once the coverage check passed and the index exists. Three things it will need that are not here: `submit` only **warns** about a failed coverage check (the caller has to look at the result's `coverage` itself, as D17 says); a drop list line has no field for "this was automatic", so add one (the plan says the reason must say so; `reasons` today are the index's); and protected datasets (D15) must not be in the index, because `submit` drops every flag the index lists. A scan across datasets has one index for each dataset, so one drop per dataset is one `submit` per dataset.
  - **Checked.** `npm run build` and `npm run lint` are clean. **No SLURM was available on the machine this ran on**, so a stand-in for `sbatch`, `squeue`, `sacct`, `sinfo` and `scancel` ran the **real job scripts** (`prepare_rows.sbatch`, `run_dedup_scan.sbatch`, `run_result_analysis.sbatch`, `filter_rows.sbatch`) locally and detached; the stand-in and all test data were outside the repo (`/tmp/p10`, not kept). What held: `filter_rows.py` with a drop list (a combined list for two scanners with a row listed twice, a bad fingerprint, a repeated line, an empty list, nothing given, a list inside the output) and the identity of a run without one identical to the committed code's; the registry (names, clashes, a long name, the same-input check, the PATCH refusal, `mark_kept`, unregistering a parent refused and then allowed once its children are gone); a converted 2,300-row dataset with a toxicity, a regex and a dedup result (indexes in `analyse.py`'s format, with fingerprints and offsets from the real lines): the combined check (8 distinct rows from 4 ticked and 6 matching, overlap 2), toxicity under Unsafe only (8), under the PII category only (5), 17 refusals (stale scan, row not flagged under the choice, reason filter on toxicity, bad ids, both or neither of `rows` and `all_matching`, the same scanner twice, over 10,000 rows, `../` as a file), a real filter job: **the rows that are left are the parent's minus the dropped ones, byte for byte**, and dropping 10 toxicity-flagged rows gave exactly 2,290 rows with those 10 in `removed_rows.ndjson`, reason `manual`; "convert again" on the parent set its kept version aside and left the filtered dataset as it was; a failing `sbatch` left a dataset showing why, which unregistered; a running job showed running, refused unregister, cancelled, and a second cancel said "not active". **A real end-to-end run** over HTTP on a second dataset with 100 duplicate pairs: real dedup scan and analysis (100 flagged, coverage pass), 50 of them ticked and dropped (750 rows, the parent unchanged and `kept`), **a new real dedup scan on the filtered version** (50 left, coverage passes on the filter run's files), "Show original" from 8 of its flagged rows opened the right original row (the last one 50 lines later in the original than in the filtered file), then a drop from the filtered dataset's own result (700 rows) whose rows still found their originals **two filters away**, and the original file byte-identical at the end. The pages were **clicked through in headless Chrome driven by Playwright** (its Python package in a venv outside the repo): ticking rows, moving to another result with the choice kept, "Choose all 6 matching rows" (rows then can't be unticked one by one), the dialog (counts, overlap, warning, command), Drop, the notice, the new dataset's header, Data and Conversion tabs, Activity with the job's details, the Datasets table, the parent's header listing its version, and the refused unregister. Screenshots were looked at.
  - **Not checked.** Real SLURM (the stand-in's job states are only running, completed and cancelled, so a job queued behind others, or failed by SLURM, as a real cluster has them, was only read from the code), a real toxicity or NER result (their indexes were made by hand in the format of Phase 9's note; the real ones have not been dropped from), and the size of the filter job's memory for millions of rows (32G is what `filter_rows.sbatch` asks, as the chains did). A drop reads the whole index twice (the check, then the submit): about 1.5 s for the 126 MB NER index in Phase 9's measurements, and the check holds no lock. The toxicity categories page (`ToxicityFilter`) wasn't clicked in the browser: the fixture had no toxicity tally, so the filter box wasn't drawn. A drop that fails after `sbatch` (the job failing) leaves a registered dataset with no data, "Filter failed" in the Rows column, until it is unregistered; nothing clears it.
  - **To know.** `filter_rows.sbatch` needs `modelsafety/readers/.venv`, like the other readers jobs (it is never created by the job). The running service has to be restarted to pick this up, and the frontend rebuilt into `validator/frontend/dist`.

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
