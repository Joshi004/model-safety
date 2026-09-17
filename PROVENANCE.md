# Provenance

Everything below was copied from `qvac-research-medpsy` at commit
`6243747898238c0b229eb9a94fa5b7d61a46c030` (branch `synth_data_gen`), from
under `synth_data_gen/tools/data_prep/` unless noted otherwise. Copies are
byte-identical except for the import fixes listed at the bottom of this file.

## contract/

| This repo | Source |
|---|---|
| `modelsafety/contract/textract.py` | `pii_scan/source_contract.py` |
| `modelsafety/contract/manifest.py` | `quality_filter_manifest.py` |

## readers/

| This repo | Source |
|---|---|
| `modelsafety/readers/prepare_rows.py` | `prepare_rows.py` |
| `modelsafety/readers/prepare_jsonl_chunks.py` | `prepare_jsonl_chunks.py` |
| `modelsafety/readers/prepare_pipeline_chunks.py` | `prepare_pipeline_chunks.py` |

## data_screening/pii/regex/ (from `pii_scan/`)

| This repo | Source |
|---|---|
| `pii_extractor.py` | `pii_scan/pii_extractor.py` |
| `pii_llm_validator.py` | `pii_scan/pii_llm_validator.py` |
| `pii_output.py` | `pii_scan/pii_output.py` |
| `pii_scanner_fast.py` | `pii_scan/pii_scanner_fast.py` |
| `pii_validator.py` | `pii_scan/pii_validator.py` |
| `requirements.txt` | `pii_scan/requirements.txt` |
| `validator/*.py` (9 files) | `pii_scan/validator/*.py` |

`pii_scan/pii_regurgitation_check.py` did **not** stay here — see
`model_screening/regurgitation/` below.

## data_screening/pii/model/ (from `pii_privacy_filter/`)

| This repo | Source |
|---|---|
| `__init__.py` | `pii_privacy_filter/__init__.py` |
| `backends.py` | `pii_privacy_filter/backends.py` |
| `bioes.py` | `pii_privacy_filter/bioes.py` |
| `build_work_manifest.py` | `pii_privacy_filter/build_work_manifest.py` |
| `common.py` | `pii_privacy_filter/common.py` |
| `compare_models.py` | `pii_privacy_filter/compare_models.py` |
| `compare_regex.py` | `pii_privacy_filter/compare_regex.py` |
| `config.env.example` | `pii_privacy_filter/config.env.example` |
| `llm_validate.py` | `pii_privacy_filter/llm_validate.py` |
| `parity_benchmark.py` | `pii_privacy_filter/parity_benchmark.py` |
| `pilot_sources.example.txt` | `pii_privacy_filter/pilot_sources.example.txt` |
| `policy.json` | `pii_privacy_filter/policy.json` |
| `policy.py` | `pii_privacy_filter/policy.py` |
| `requirements.txt` | `pii_privacy_filter/requirements.txt` |
| `scanner.py` | `pii_privacy_filter/scanner.py` |
| `.gitignore` | `pii_privacy_filter/.gitignore` |

Policy data (`policy.json`) stayed next to this code rather than moving to
`configs/` — its load path isn't verified yet. See the checklist.

## data_screening/toxicity/ (from `toxicity/`)

| This repo | Source |
|---|---|
| `detoxify.py` | `toxicity/detoxify.py` |
| `guard_report.py` | `toxicity/guard_report.py` |
| `llm_check_guard.py` | `toxicity/llm_check_guard.py` |
| `medical_guard_pilot.py` | `toxicity/medical_guard_pilot.py` |
| `summarize_qwen_record_impact.py` | `toxicity/summarize_qwen_record_impact.py` |
| `requirements.txt` | `toxicity/requirements.txt` |
| `run.py` | `toxicity/run.py` |
| `.gitignore` | `toxicity/.gitignore` |
| `policies/medpsy_medical_sexual_v1.txt` | `toxicity/policies/medpsy_medical_sexual_v1.txt` |

`medical_guard_pilot.py` and `summarize_qwen_record_impact.py` are
medpsy-specific one-off scripts, kept for now — whether they belong here
long-term is on the checklist. `policies/` is a medpsy domain policy, kept
next to the code that loads it rather than moved to `configs/`.

## data_screening/dedup/ (from `dedup/`)

| This repo | Source |
|---|---|
| `postprocess_matches.py` | `dedup/postprocess_matches.py` |
| `prepare_benchmarks.py` | `dedup/prepare_benchmarks.py` |
| `summarize_cross_matches.py` | `dedup/summarize_cross_matches.py` |
| `requirements.txt` | `dedup/requirements.txt` |
| `run.py` | `dedup/run.py` |
| `.gitignore` | `dedup/.gitignore` |

## decisions/

| This repo | Source |
|---|---|
| `quality_filter_finalize.py` | `quality_filter_finalize.py` |
| `finalize_medpsy_cleaning.py` | `finalize_medpsy_cleaning.py` |

## model_screening/regurgitation/

| This repo | Source |
|---|---|
| `pii_regurgitation_check.py` | `pii_scan/pii_regurgitation_check.py` |

## launchers/slurm/

| This repo | Source |
|---|---|
| `count_unique_conversations.sh` | `pii_scan/count_unique_conversations.sh` |
| `launch_vllm_replicas.sh` | `pii_scan/launch_vllm_replicas.sh` |
| `run_pii_llm.sbatch` | `pii_scan/run_pii_llm.sbatch` |
| `run_pii_llm_full_node.sbatch` | `pii_scan/run_pii_llm_full_node.sbatch` |
| `run_pii_scan.sbatch` | `pii_scan/run_pii_scan.sbatch` |
| `run_pii_scan_array.sbatch` | `pii_scan/run_pii_scan_array.sbatch` |
| `prepare_privacy_filter.sbatch` | `pii_privacy_filter/prepare_privacy_filter.sbatch` |
| `run_privacy_filter_compare.sbatch` | `pii_privacy_filter/run_privacy_filter_compare.sbatch` |
| `run_privacy_filter_llm.sbatch` | `pii_privacy_filter/run_privacy_filter_llm.sbatch` |
| `run_privacy_filter_node.sbatch` | `pii_privacy_filter/run_privacy_filter_node.sbatch` |
| `run_privacy_filter_pilot.sbatch` | `pii_privacy_filter/run_privacy_filter_pilot.sbatch` |
| `submit_privacy_filter.sh` | `pii_privacy_filter/submit_privacy_filter.sh` |
| `run_detoxify_multigpu.sbatch` | `toxicity/run_detoxify_multigpu.sbatch` |
| `run_medical_guard_pilot.sbatch` | `toxicity/run_medical_guard_pilot.sbatch` |
| `run_toxicity_scan.sbatch` | `toxicity/run_toxicity_scan.sbatch` |
| `run_toxicity_scan_array.sbatch` | `toxicity/run_toxicity_scan_array.sbatch` |
| `run_dedup_scan.sbatch` | `dedup/run_dedup_scan.sbatch` |
| `prepare_quality_filter.sbatch` | `prepare_quality_filter.sbatch` |
| `run_quality_filter_finalize.sbatch` | `run_quality_filter_finalize.sbatch` |
| `run_quality_filter_report.sbatch` | `run_quality_filter_report.sbatch` |
| `run_finalize_medpsy_cleaning.sbatch` | `run_finalize_medpsy_cleaning.sbatch` |
| `submit_medpsy_quality_filter.sh` | `submit_medpsy_quality_filter.sh` |

None of these have had their hardcoded `synth_data_gen/`-relative paths
fixed yet — see the checklist.

## docs/

| This repo | Source |
|---|---|
| `docs/data-screening/pii-regex.md` | `pii_scan/README.md` |
| `docs/data-screening/pii-regex-pipeline.png` | `pii_scan/pipeline.png` |
| `docs/data-screening/pii-regex-pipeline.svg` | `pii_scan/pipeline.svg` |
| `docs/data-screening/pii-model.md` | `pii_privacy_filter/README.md` |
| `docs/data-screening/toxicity.md` | `toxicity/README.md` |
| `docs/data-screening/toxicity-pipeline.png` | `toxicity/pipeline.png` |
| `docs/data-screening/toxicity-pipeline.svg` | `toxicity/pipeline.svg` |
| `docs/data-screening/dedup.md` | `dedup/README.md` |
| `docs/operations/quality-filtering.md` | `QUALITY_FILTERING.md` |
| `docs/unified-screening-repo.md` | `AISafety/docs/unified-screening-repo.md` (our own design doc, not medpsy) |

Two image links were updated to match the renamed files: `pii-regex.md` and
`toxicity.md` originally linked `pipeline.png`; they now link
`pii-regex-pipeline.png` and `toxicity-pipeline.png` respectively.

## What was deliberately not copied

- The medpsy generation pipelines: `distilabel_scripts/`, `configs/`,
  `launchers/lib/`, `containers/`, `examples/`. This repo screens data and
  models; it doesn't generate training data.
- Unrelated `data_prep` scripts specific to MedPsy dataset preparation:
  `prepare_medqa_medmcqa_recovery.py` + its `.sbatch`,
  `merge_medqa_medmcqa_recovery.py`, `prepare_medhallu_counterparts.py`,
  `finalize_medec_qa_pairs.py`, `convert_qwen_general_domain_sft.py`,
  `trace_ii_medical_sources.py`, `backfill_chunked_seed_text.py`,
  `post_process_scaling_qa.py`, `prepare_benchmark_messages.py`,
  `build_medpsy_rl_auxiliary.py`, `quality_filter_manifest.py`'s sibling
  `submit_medpsy_quality_filter.sh` companions for non-screening steps.
- All medpsy tests (`synth_data_gen/tests/`). Per the user rules for this
  project, tests aren't written speculatively; a parity test suite comes
  later, against real run output, per `docs/porting-checklist.md`.
- The top-level `data_prep/__init__.py` — a fresh, empty `modelsafety/__init__.py`
  was created instead.

## Import fixes made during the copy

Moving `source_contract.py` to `contract/textract.py` broke every import of
it. This turned out to be **5** call sites, not the 4 estimated when this
move was planned — `pii_extractor.py` was missed in that estimate and is
included here for the same reason as the other four.

| File | Before | After |
|---|---|---|
| `data_screening/pii/regex/pii_scanner_fast.py` | `from source_contract import (...)` | `from modelsafety.contract.textract import (...)` |
| `data_screening/pii/regex/pii_extractor.py` | `from source_contract import (...)` | `from modelsafety.contract.textract import (...)` |
| `data_screening/pii/regex/pii_llm_validator.py` | `from source_contract import parse_record_text` | `from modelsafety.contract.textract import parse_record_text` |
| `model_screening/regurgitation/pii_regurgitation_check.py` | `from source_contract import extract_content_text, parse_record_text` | `from modelsafety.contract.textract import extract_content_text, parse_record_text` |
| `decisions/quality_filter_finalize.py` | `from pii_scan.source_contract import load_source_snapshot, verify_source_snapshot` | `from modelsafety.contract.textract import load_source_snapshot, verify_source_snapshot` |

No other code was changed. In particular, `pii_validator.py`'s
`from validator.<name> import ...` lines were left as-is: `validator/`
moved with it into `data_screening/pii/regex/`, so that import still
resolves the same way it did in medpsy.

This is a scaffold, not a working install — see
`docs/porting-checklist.md` for what's left before any of this runs.
