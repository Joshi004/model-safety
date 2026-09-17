# Standalone Nemotron Privacy Filter

This folder is independent from `pii_scan/`, the regex pipeline, and
`submit_medpsy_quality_filter.sh`. It never writes to the existing
`pii_toxicity_cleaning/scans/pii/` tree or changes final filtered data.

The scanner uses
[`OpenMed/privacy-filter-nemotron-v2`](https://huggingface.co/OpenMed/privacy-filter-nemotron-v2),
a 1.4B-parameter sparse-MoE bidirectional token classifier with 55 fine-grained
PII labels and 221 BIOES token classes.

The same backend also supports
[`openai/privacy-filter`](https://huggingface.co/openai/privacy-filter), the
original eight-category model with 33 BIOES classes. Select it with
`PRIVACY_FILTER_MODEL=openai/privacy-filter`. Use a separate run root for every
model and policy combination.

## Backends

`transformers` is currently the production backend. A direct capability probe
inside `vllm-openai-v0.24.0-cu129.sqsh` reported that
`OpenAIPrivacyFilterForTokenClassification` is not registered. The `vllm`
backend is retained for a future image containing native support, but it fails
loudly rather than applying an unsafe automatic model conversion.

The SLURM wrappers build an isolated environment and install Torch from the
official CUDA 12.8 wheel index, matching the cluster driver.

The `openmed` backend is a slow correctness reference for parity tests. It uses
the model family's intended grouped-span decoder.

## Pipeline

1. `build_work_manifest.py` scans source JSONL files once and writes small
   byte-range work units with exact 1-based source line coordinates.
2. `scanner.py worker` loads one model replica per GPU and dynamically claims
   units. Large monolithic JSONL files can therefore use all GPUs.
3. Each unit is written atomically with a success marker. Interrupted runs
   process only unfinished units.
4. `scanner.py aggregate` creates global span, candidate, and summary files.
5. `compare_regex.py` can optionally compare results with an existing regex
   `pii_extract.csv` without modifying that pipeline.
6. `llm_validate.py` is an optional standalone contextual false-positive gate.

## Label policy

All 55 labels are stored in `all_spans.jsonl`.

- `direct_identifier`: credentials, IDs, contact details, exact addresses,
  financial, healthcare, and device identifiers.
- `person_linked`: first and last names.
- `contextual`: generic dates, places, demographics, occupations, languages,
  and company names.

Only direct and person-linked spans are exported to `llm_candidates.csv` by
default, after the confidence and markup policy in `policy.json`. This policy
affects the optional LLM stage, not the complete span artifact.

## Candidate policy

Every model span remains in `all_spans.jsonl`. Each record additionally stores
`candidate_eligible`, `candidate_threshold`, `candidate_excluded_reason`, and
`policy_version`.

The default policy:

- disables contextual labels for candidate export;
- uses 0.90 for first and last names;
- uses 0.95 for usernames;
- uses 0.99 for OpenAI `private_person`;
- disables OpenAI `private_date`, because its broad date category mostly found
  clinical relative dates in the pilot;
- uses 0.50 for structured labels whose pilot confidence was lower, including
  email, phone, postcode, medical IDs, national IDs, and payment cards;
- uses 0.70 for other direct identifiers;
- suppresses detections overlapping control tags such as `<think>`,
  `<analysis>`, and tool/role markup.
- suppresses configured answer markers such as `CORRECT` and `INCORRECT`.

Thresholds are intentionally configurable and are not ground-truth precision
estimates. Set `PRIVACY_FILTER_POLICY` to an alternative JSON policy for a new
run. Never reuse completed work units after changing policy.

## Submit a pilot

All work runs through the `health` partition:

```bash
export PRIVACY_FILTER_MODE=pilot
export PRIVACY_FILTER_INPUT_ROOT=/path/to/input
export PRIVACY_FILTER_RUN_ROOT=/path/to/output/privacy_filter_pilot
bash launchers/slurm/submit_privacy_filter.sh
```

The pilot defaults to one 32 MiB work unit on one H100. Supply
`PRIVACY_FILTER_SOURCE_MANIFEST` for a deterministic curated source selection;
copy `pilot_sources.example.txt` and replace its repository-relative examples.

## Submit the full scan

```bash
export PRIVACY_FILTER_MODE=full
export PRIVACY_FILTER_INPUT_ROOT=/path/to/input
export PRIVACY_FILTER_RUN_ROOT=/path/to/output/privacy_filter
bash launchers/slurm/submit_privacy_filter.sh
```

The full job uses one node with eight H100s and many 256 MiB dynamic work units.
Useful controls:

- `PRIVACY_FILTER_BACKEND=transformers`
- `PRIVACY_FILTER_UNIT_MIB=256`
- `PRIVACY_FILTER_INPUT_BATCH_SIZE=64`
- `PRIVACY_FILTER_MODEL_BATCH_SIZE=16`
- `PRIVACY_FILTER_MAX_TOKENS=4096`
- `PRIVACY_FILTER_OVERLAP_TOKENS=256`
- `PRIVACY_FILTER_POLICY=/path/to/policy.json`

## Outputs

Under `PRIVACY_FILTER_RUN_ROOT`:

```text
manifest/
  manifest.json
  work_units.jsonl
spans/units/
  <unit-id>.jsonl
candidates/units/
  <unit-id>.csv
units/<unit-id>/
  summary.json
  _SUCCESS
worker_logs/
all_spans.jsonl
llm_candidates.csv
summary.json
```

`all_spans.jsonl` retains source file, physical source line, message index,
role, character offsets, label, confidence, model, and backend.

## Optional regex comparison

```bash
export PRIVACY_FILTER_REGEX_INPUT=/path/to/regex/scans/pii/shards
sbatch --export=ALL launchers/slurm/run_privacy_filter_compare.sbatch
```

The report is overlap only; it does not claim precision or recall without
ground-truth labels. The SLURM wrapper compares only spans marked
`candidate_eligible`; run `compare_regex.py` without `--eligible-only` to audit
all raw model spans.

`compare_models.py` compares two `all_spans.jsonl` artifacts after canonicalizing
the coarse OpenAI and fine-grained Nemotron taxonomies. Pass `--policy
policy.json` to apply the current policy dynamically to existing raw artifacts
without rerunning inference.

## Optional LLM confirmation

Submit `launchers/slurm/run_privacy_filter_llm.sbatch` with `PRIVACY_FILTER_RUN_ROOT` set. It
reads `llm_candidates.csv` and writes only inside
`$PRIVACY_FILTER_RUN_ROOT/llm_validation/`. API/model failures are reported and
must be retried before decisions are used.

## Safety and validation

This checkpoint is experimental and trained primarily from synthetic PII
sources. It should be evaluated on the target medical dataset. Tests cover
BIOES constraints, Unicode offsets, byte-range coordinates, work claims,
resumption, tier policy, and comparison semantics.

