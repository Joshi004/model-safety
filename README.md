# qvac-model-safety

Unified data screening and behaviour screening for our models: one place to
check whether training data is clean (PII, toxicity, duplicates) and whether
a served model behaves (refusals, jailbreak resistance, memorisation).

**Status: the SLURM launchers run against this repo's layout.** This repo
holds the medical team's data screening code, copied over, reorganised into
the target structure, and re-pathed so the original medpsy
`export VAR=...` then `sbatch` workflow works here unchanged in spirit. None
of the three behaviours from the design doc (interactive settings, redact,
resolve-to-source) are implemented yet. See
[`docs/porting-checklist.md`](docs/porting-checklist.md) for exactly what's
left, and [`PROVENANCE.md`](PROVENANCE.md) for where every file came from.

## Running something

Every launcher is env-var driven, exactly like medpsy. Submit from the repo
root on a login node; heavy work always runs through `sbatch` on the `health`
partition, never on the login node itself.

```bash
cd /path/to/qvac-model-safety

# Regex PII scan (stages 1+2), pointed at a JSONL tree
sbatch --export=ALL,PII_BASE_DIR=/path/to/jsonl/root,PII_RUN_DIR=/path/to/pii_run \
  launchers/slurm/run_pii_scan.sbatch

# The full MedPsy PII+toxicity quality-filter job graph
export QUALITY_INPUT_ROOT=/path/to/input
export QUALITY_RUN_ROOT=/path/to/output
export QUALITY_SHARD_COUNT=16
bash launchers/slurm/submit_medpsy_quality_filter.sh
```

Per-module docs and their full environment-variable tables are under
[`docs/data-screening/`](docs/data-screening/); the end-to-end job graph is in
[`docs/operations/quality-filtering.md`](docs/operations/quality-filtering.md).
Each module bootstraps its own venv from its own `requirements.txt` on first
run, same as before -- there is no shared root venv or `pip install -e .`
step. `pii/model` (the Privacy Filter) uses a separate isolated venv on
purpose; see the note in `docs/data-screening/pii-model.md`.

One accepted gap: `modelsafety/readers/prepare_rows.py` is missing a sibling
file (`chunked_writer.py`) and cannot be imported. No launcher calls it, so
it doesn't block anything here -- see `docs/porting-checklist.md`.

## Why this repo exists

Read [`docs/unified-screening-repo.md`](docs/unified-screening-repo.md) first.
Short version: the medical team solved data screening and never attacks a
model; the vision team attacks models and never screens data; the tool team
has neither. This repo is meant to be the shared home so nobody builds a
third copy of either.

## Layout

```text
modelsafety/
  contract/          row + provenance contract everything reads
  runconfig/         settings resolution (not implemented yet)
  readers/           per-dataset adapters: raw data -> contract rows
  data_screening/
    pii/regex/        regex PII scan (ported from medpsy pii_scan/)
    pii/model/         model-based PII backend (ported from medpsy pii_privacy_filter/)
    toxicity/          fast screen + guard verification
    dedup/             near-duplicates and benchmark contamination
  model_screening/
    targets/           generate(prompt, image=None) -> text (not implemented yet)
    redteam/           deepteam adapters (not implemented yet)
    regurgitation/      PII reconstruction probe
  results/             findings + report schema (not implemented yet)
  decisions/           report/redact/remove join, byte-preserved partitions
configs/               thresholds, judge policies, per-dataset settings
launchers/slurm/        SLURM wrappers, re-pathed to this repo's layout
docs/
  unified-screening-repo.md   the design doc this repo implements
  porting-checklist.md        what's left to do
  data-screening/              per-module docs, ported from medpsy READMEs
  operations/                  SLURM / run-hygiene docs
```

## Source

Everything under `modelsafety/data_screening/`, `modelsafety/decisions/`,
`modelsafety/model_screening/regurgitation/`, and `launchers/slurm/` was
copied from `qvac-research-medpsy` (`synth_data_gen/tools/data_prep/`) at
commit `6243747898238c0b229eb9a94fa5b7d61a46c030`. See
[`PROVENANCE.md`](PROVENANCE.md) for the exact file-by-file mapping.
