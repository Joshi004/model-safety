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
