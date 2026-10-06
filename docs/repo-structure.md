# Repo structure

What's in this repo and what each file does, one to two lines each. See
[`unified-screening-repo.md`](unified-screening-repo.md) for why it's shaped
this way and [`porting-checklist.md`](porting-checklist.md) for what's not
built yet. [`../PROVENANCE.md`](../PROVENANCE.md) has the file-by-file mapping
back to where each piece was ported from.

## Layout at a glance

```text
qvac-model-safety/
  README.md, PROVENANCE.md, pyproject.toml
  scripts/                          placeholder for the future screen.sh
  configs/                          placeholder for thresholds/policies
  modelsafety/
    contract/                       row shape + text extraction + manifest
    runconfig/                      placeholder for settings resolution
    readers/                        raw data -> contract rows
    data_screening/
      pii/regex/                    stages 1-4a regex PII pipeline
      pii/model/                    standalone model-based PII pipeline
      toxicity/                     two-stage toxicity screen
      dedup/                        near-dup + contamination scan
    model_screening/
      targets/                      placeholder for the shared generate() interface
      redteam/                      placeholder for deepteam adapters
      regurgitation/                PII memorisation probe
    results/                        placeholder for findings/report/resolve
    decisions/                      join findings -> keep/drop partitions
  launchers/slurm/                  every .sbatch / .sh entry point
  validator/                        personal input-structure/mapping preview UI
  docs/                             this folder
```

## Root

- `README.md` — repo overview: status, why it exists, layout, source commit.
- `PROVENANCE.md` — every copied file mapped to its medpsy source path and commit; lists what was deliberately left behind and the import fixes made.
- `pyproject.toml` — package metadata for `modelsafety`; dependencies aren't consolidated yet, each module still carries its own `requirements.txt`.
- `scripts/.gitkeep` — placeholder; the interactive `screen.sh` entry point described in the design doc isn't written yet.
- `configs/.gitkeep` — placeholder for thresholds, judge policies, and per-dataset settings once they're pulled out of code.

## `modelsafety/` package markers

- `modelsafety/__init__.py`, `contract/__init__.py`, `data_screening/__init__.py`, `data_screening/pii/__init__.py`, `data_screening/pii/regex/__init__.py`, `data_screening/toxicity/__init__.py`, `data_screening/dedup/__init__.py`, `model_screening/__init__.py`, `model_screening/targets/__init__.py`, `model_screening/redteam/__init__.py`, `model_screening/regurgitation/__init__.py`, `results/__init__.py`, `decisions/__init__.py`, `readers/__init__.py`, `runconfig/__init__.py` — all empty; each only marks its directory as an importable Python package.
- `data_screening/pii/model/__init__.py` — one-line module docstring identifying the standalone Privacy Filter pipeline; still empty otherwise.

## `modelsafety/contract/`

- `textract.py` — pulls the scannable text out of a `text`/`messages` JSONL row, and verifies a source file against its content-addressed snapshot before anything reads it.
- `manifest.py` — builds a deterministic, SHA-256-addressed manifest of a JSONL tree so a scan shard can detect a source file that changed after the manifest was built.

## `modelsafety/readers/`

- `prepare_rows.py` — maps arbitrary source columns (JSONL, JSON, CSV, Parquet, or a Hugging Face dataset) onto the `text`/`messages` contract, packing everything else into `metadata`.
- `prepare_jsonl_chunks.py` — splits a flat folder of JSONL files into numbered chunk directories for SLURM array jobs.
- `prepare_pipeline_chunks.py` — splits large JSONL files into byte-sized shards with GNU `split`, without loading rows into memory.

## `modelsafety/data_screening/pii/regex/` — stages 1-4a, regex-based

- `pii_output.py` — shared output layout: the PII category list, run-directory resolution, and metrics-file writers every stage below uses.
- `pii_scanner_fast.py` — stage 1: parallel, line-chunked regex scan of a JSONL tree; writes one hit file per PII category.
- `pii_validator.py` — stage 2: drops regex false positives from stage-1 hits using the per-category rules in `validator/`.
- `pii_extractor.py` — stage 3: joins surviving hits back to their exact source JSONL line (hash-verified) and writes a CSV.
- `pii_llm_validator.py` — stage 4a: asks an OpenAI-compatible chat model whether each extracted candidate is real PII in context.
- `requirements.txt` — pinned deps for this pipeline (`piiregex`, `tqdm`).
- `validator/__init__.py` — empty; just marks the validator rules as a package.
- `validator/bitcoin.py` — checks Bitcoin address checksums (Base58Check and Bech32/Bech32m).
- `validator/credit_card.py` — Luhn-checks card numbers and rejects known test numbers.
- `validator/email.py` — validates local-part/domain syntax and rejects reserved example domains.
- `validator/ip.py` — validates IPv4 addresses and excludes documentation/example ranges.
- `validator/ipv6.py` — validates IPv6 addresses, reusing the IPv4 documentation-range exclusions.
- `validator/phone.py` — validates phone numbers and filters out toll-free and placeholder area codes.
- `validator/po_box.py` — matches PO box formats by regex.
- `validator/street_address.py` — validates street addresses against known street-suffix vocabulary.

## `modelsafety/data_screening/pii/model/` — standalone model-based pipeline

- `backends.py` — pluggable prediction backends (`transformers`, `vllm`, an OpenMed reference) that turn text windows into PII spans.
- `bioes.py` — BIOES tag decoding: Viterbi path decoding and span extraction from per-token label scores.
- `build_work_manifest.py` — splits a JSONL tree into deterministic byte-range work units for standalone scanning.
- `common.py` — shared work-unit dataclass, ID hashing, and an atomic JSON writer used across this pipeline.
- `policy.py` — loads the tiered confidence-threshold policy that decides which spans count as reportable PII.
- `policy.json` — the actual threshold policy: per-tier and per-label confidence cutoffs, versioned.
- `scanner.py` — standalone resumable scanner: reads work units, runs a backend, applies policy, aggregates results.
- `llm_validate.py` — optionally confirms candidates from this pipeline with a local LLM.
- `compare_models.py` — compares two Privacy Filter span outputs (e.g. two backends or versions) over the same manifest scope.
- `compare_regex.py` — compares this pipeline's spans against the regex pipeline's candidates, the only cross-check between the two methods.
- `parity_benchmark.py` — benchmarks the custom batched decoder against the OpenMed reference decoder for correctness and speed.
- `config.env.example` — example environment variables for a run (input/output roots, backend, model, batching).
- `pilot_sources.example.txt` — example pilot file list (paths relative to the input root) for a small-scope run.
- `requirements.txt` — pinned deps (`openmed`, `torch`, `transformers`, `huggingface_hub`, `numpy`).
- `.gitignore` — ignores local venvs, `__pycache__`, and `.pyc` files.

## `modelsafety/data_screening/toxicity/`

- `detoxify.py` — vendored Detoxify model wrapper (Apache-2.0, from `unitaryai/detoxify`), adapted for this repo.
- `run.py` — stage 1: multi-GPU Detoxify screen over JSONL, with source-line traceability and manifest support.
- `llm_check_guard.py` — stage 2: sends each stage-1-flagged conversation, truncated to the flagged turn, to a Qwen3Guard endpoint and records its safety verdict.
- `guard_report.py` — summarizes stage-2 guard safety labels (safe/unsafe/controversial/error/unknown) across a folder of JSONL.
- `medical_guard_pilot.py` — pilot comparing a medical-aware Granite Guardian judge against Gemma on the same flagged messages, kept independent of Qwen3Guard.
- `summarize_qwen_record_impact.py` — rolls message-level Qwen3Guard outcomes up to source-record level.
- `requirements.txt` — pinned deps (`torch`, `transformers`, `accelerate`, `openai`, etc.).
- `.gitignore` — ignores caches, logs, and local venvs.
- `policies/medpsy_medical_sexual_v1.txt` — the medical-domain judge prompt: what counts as unsuitable content versus legitimate clinical material.

## `modelsafety/data_screening/dedup/`

- `run.py` — MinHash/LSH near-duplicate and cross-dataset contamination detection over a JSONL tree (adapted from the Apache-2.0 Cerebras Model Zoo pipeline).
- `prepare_benchmarks.py` — normalizes mixed benchmark test sets into prompt-only JSONL for contamination checking.
- `postprocess_matches.py` — builds a filtered dataset tree from selected rows in an existing match report.
- `summarize_cross_matches.py` — aggregates cross-dataset match reports by training and benchmark dataset name.
- `requirements.txt` — pinned deps (`datasketch`).
- `.gitignore` — ignores local venvs and caches.

## `modelsafety/model_screening/`

- `regurgitation/pii_regurgitation_check.py` — memorisation probe: blanks a found PII value out of its context, asks the model to reconstruct it several times, and checks the responses against the withheld value.
- `targets/`, `redteam/` — empty; the shared `generate(prompt, image=None)` target interface and the deepteam adapters aren't written yet.

## `modelsafety/results/` and `modelsafety/decisions/`

- `results/` — empty; the findings/report schema and the `resolve`/lineage commands aren't written yet.
- `decisions/quality_filter_finalize.py` — joins PII and toxicity decisions per shard and writes byte-preserved accept/reject dataset partitions.
- `decisions/finalize_medpsy_cleaning.py` — combines the quality-filter rejects with separate decontamination rejects into one final dataset.

## `launchers/slurm/`

All ported as-is; every one still hardcodes `synth_data_gen/`-relative paths (see the checklist).

- `run_pii_scan.sbatch` — regex PII stages 1+2 as a single job (the recommended default).
- `run_pii_scan_array.sbatch` — the same stages 1+2, as a non-exclusive CPU array job that shares the node with other work.
- `run_pii_llm.sbatch` — stage 4a LLM confirmation, as a SLURM array (one shard per task).
- `run_pii_llm_full_node.sbatch` — stage 4a on a full node, load-balanced across several local vLLM replicas.
- `launch_vllm_replicas.sh` — starts N local vLLM server replicas on sequential ports; used by `run_pii_llm_full_node.sbatch`.
- `count_unique_conversations.sh` — prints deduplicated hit counts per PII category from a hits directory.
- `prepare_privacy_filter.sbatch` — builds the byte-range work manifest for the model-based PII pipeline.
- `run_privacy_filter_node.sbatch` — runs the model-based PII scan across all GPUs on one node.
- `run_privacy_filter_pilot.sbatch` — runs the same scan in pilot (small-scope) mode.
- `run_privacy_filter_llm.sbatch` — starts a local vLLM Gemma server and runs LLM confirmation over its candidates.
- `run_privacy_filter_compare.sbatch` — runs the regex-vs-model comparison over the same manifest scope.
- `submit_privacy_filter.sh` — submits the manifest-build and GPU-scan jobs for the model-based PII pipeline as a dependency chain.
- `run_detoxify_multigpu.sbatch` — multi-GPU `accelerate` launch of the Detoxify stage-1 screen.
- `run_toxicity_scan.sbatch` — toxicity stages 1+2 end to end, starting and stopping a local Qwen3Guard vLLM server.
- `run_toxicity_scan_array.sbatch` — toxicity stages 1+2 as a SLURM array, one shard per task.
- `run_medical_guard_pilot.sbatch` — runs the Granite-Guardian-vs-Gemma medical guard pilot comparison.
- `run_dedup_scan.sbatch` — runs the MinHash/LSH dedup and contamination scan as a single job.
- `prepare_quality_filter.sbatch` — builds shard manifests before a full PII+toxicity quality-filter run.
- `run_quality_filter_finalize.sbatch` — SLURM array: runs the decision join (`quality_filter_finalize.py shard`) per shard.
- `run_quality_filter_report.sbatch` — runs the aggregation step (`quality_filter_finalize.py aggregate`) over every shard's results.
- `run_finalize_medpsy_cleaning.sbatch` — runs `finalize_medpsy_cleaning.py` to merge in decontamination rejects.
- `submit_medpsy_quality_filter.sh` — submits the complete PII+toxicity quality-filter dependency graph (prepare, PII, toxicity, finalize, report).

## `validator/`

A personal, read-only tool: given a cluster path, shows what `prepare_rows.py` would see and produce for it — detected format, each top-level field in a sampled row with a plain-language description and a "Goes to" choice (scanned as text/messages, kept in metadata, or dropped), the real converted row and the exact text the scanner reads for it, and the matching command — without converting or writing anything. A collapsible left sidebar (`Sidebar`/`SourcePicker`) lists a curated catalog of what the Tools team and medpsy actually train on (`backend/sources.py`), grouped by team and group, each entry with medpsy's original PII/toxicity screening status and a one-click Verified checkbox; picking one loads it immediately (there's no path box to fill anymore -- that moved into the sidebar's own collapsed-by-default "Custom path" box). A done/total progress count shows per group, per team, and for the whole catalog (`catalog.ts`), and `DatasetHeader` shows the picked entry's name, path, description, and screening note at the top of the main panel. Every Verified mark and the sidebar's own open/collapsed state live in this browser only (`localStorage`, see `storage.ts`), never sent to the backend. A "Readable" view (`RecordView`, the default) then renders a sampled row as a conversation — tool definitions, tool calls, reasoning, the reference answer — right next to the same real scanned text the scanner sees, so the two can be compared directly instead of trusted to match. Not part of the screening pipeline; started by hand with `./validator/run.sh`. No login or token: it's meant for a trusted network (see `run.sh`'s own header comment).

- `run.sh` — creates the backend venv and builds the frontend if either is missing, then starts the backend (which also serves the built frontend) on `127.0.0.1`.
- `.gitignore` — ignores `backend/.venv/`, `frontend/node_modules/`, `frontend/dist/`.
- `backend/app.py` — FastAPI app: `/api/health`, `/api/sources`, `/api/inspect`, `/api/preview`; serves the built frontend at `/`. Samples 2 rows by default (`DEFAULT_SAMPLE_ROWS`) on the assumption that one dataset's rows are shaped alike, so the UI can show them clearly rather than summarize many.
- `backend/sources.py` — the source picker's curated catalog: a plain Python list of `{team, groups: [{name, entries: [{label, path, description, screening}]}]}`, covering only data actually used for training (not eval outputs, benchmarks, or raw/cached sources). A group can set `children_of` to expand every child folder of a directory instead of listing each one by hand (covers the Tools team's 61-component SFT mix and medpsy's `nothink25/` folders). Each entry's `screening` note comes from cross-referencing medpsy's own `lineage.json` files against `final_cleaned_data/filtered`. `list_sources()` (called by `/api/sources`) marks each entry `available`/`unavailable_reason` via the same `detect.resolve_input_path()` used everywhere else plus an existence check, so a path this account can't reach just greys an entry out instead of breaking the picker.
- `backend/detect.py` — resolves any absolute path (no folder allowlist; reads with the running user's own permissions), walks a folder and groups its files by format, and samples the first rows the same way `prepare_rows.py`'s own readers do.
- `backend/preview.py` — infers a field structure internally (used only to suggest a mapping, not sent to the browser), suggests a `--map` for a column that's already correctly named and shaped (an exact match always wins) or named like a common choice, runs the real `_prepare_row`/`json.dumps`/`extract_record_text` steps, converts a sampled or converted row to a browser-safe form via `to_display()` (a value that would break `json.dumps` — a pandas `Timestamp`, raw bytes, a numpy scalar — becomes a small marker naming its real type, instead of being hidden or faked), and builds the matching CLI command.
- `backend/reader_imports.py` — puts `modelsafety/readers` on `sys.path` and re-exports the private `prepare_rows.py` functions `detect.py`/`preview.py` call directly, so a preview can't drift from the real converter.
- `backend/requirements.txt` — `fastapi`, `uvicorn`, plus `prepare_rows.py`'s own `requirements.txt`, so reader versions match.
- `frontend/` — Vite `react-ts` app, no UI library. `src/App.tsx` holds the page state (selected data, mapping, selected row, Readable vs. Fields & output view, the Verified mark per dataset path, and `activePath` -- the path an inspect was last asked for, set the instant that happens so the sidebar row and `DatasetHeader` can highlight/show it before the request returns) and re-runs the preview on any change, with a request-id guard so a slower, older request can't overwrite a newer one. `src/mapping.ts` is the mapping state — one destination per top-level key (text, messages, metadata, or dropped) — and turns it into the real `--map`/`--require`/`--exclude-metadata` request flags. `src/catalog.ts` is pure progress-counting over the `/api/sources` catalog: a dataset only counts toward its group's/team's/the whole catalog's done/total if it's actually `available` (so a path nobody on this account can reach doesn't permanently cap that group below 100%), plus `findEntry()`, which looks up the catalog entry (if any) behind whatever path is currently active. `src/describe.ts` turns a real value into a plain-language label (e.g. "list · 2 objects (role, content)") and recognizes `to_display()`'s non-JSON marker. `src/readable.ts` is pure parsing functions (no React) for the Readable view: a conversation (`messages`, or else verl's `prompt`), a turn's reasoning (a `<think>` block, a lone `</think>`, or a `reasoning_content` field), Qwen-XML `<tool_call>`/`<function=…>`/`<parameter=…>` calls, the `<tools>…</tools>` definitions in a system prompt, and the reference answer (`reward_model.ground_truth`, `target`, `metadata.options`). `src/storage.ts` is the thin `localStorage` wrapper every client-only preference below is saved through (selected team, open/closed groups, the sidebar's own collapsed state, Verified marks) — never sent to the backend, and scoped to this exact browser and address. `src/components/` has one file per panel: `Sidebar` (the collapsible left panel: overall Verified progress, `SourcePicker`, and a collapsed-by-default "Custom path" box), `SourcePicker` (the Tools-team/medpsy catalog from `/api/sources`, grouped by team, one Verified checkbox per dataset and a done/total count on every team tab and group header), `PathForm` (the plain path input and "Try an example" row, now only rendered inside Sidebar's "Custom path" box), `DatasetHeader` (the picked dataset's name, path, description, and screening note at the top of the main panel, with its own Verified checkbox; a sticky top bar keeps the name and checkbox visible while a long row's content scrolls underneath — see `.dataset-header-bar` in `App.css`), `ProgressBar` (the done/total bar reused by Sidebar's overall count and by every team tab/group header), `FormatPicker` (format/file choice, auto-loads the first readable one), `InputFields` (one card per top-level key: its real value and "Goes to" select), `TargetFormat` (the two shapes the scanner reads, shown with type placeholders, never sample data), `OutputPreview` (the real converted row and scanned text, row status, mapping summary), `RecordView` (the Readable view: tool definitions, the conversation turn by turn, the reference answer, a plain document view for `text` rows, and the real scanned text next to a plain-language char count), `ValueView` (shared, collapsible renderer for a real value, used by input, output, and `RecordView`), and `CommandBox`.

## `docs/`

- `unified-screening-repo.md` — the design doc this repo implements: why a unified repo, what it borrows from where, and the phased plan.
- `porting-checklist.md` — tracked checklist of what's left before this scaffold actually runs.
- `repo-structure.md` — this document.
- `data-screening/pii-regex.md` — ported README for the regex PII pipeline (stages, input/output format, environment variables).
- `data-screening/pii-regex-pipeline.png`, `pii-regex-pipeline.svg` — the same pipeline diagram, raster and vector.
- `data-screening/pii-model.md` — ported README for the model-based PII pipeline.
- `data-screening/toxicity.md` — ported README for the toxicity pipeline.
- `data-screening/toxicity-pipeline.png`, `toxicity-pipeline.svg` — the same pipeline diagram, raster and vector.
- `data-screening/dedup.md` — ported README for the dedup/contamination pipeline.
- `operations/quality-filtering.md` — ported runbook for the end-to-end MedPsy PII+toxicity quality-filter job graph.
