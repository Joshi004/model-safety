# qvac-model-safety

Unified data screening and behaviour screening for our models: one place to
check whether training data is clean (PII, toxicity, duplicates) and whether
a served model behaves (refusals, jailbreak resistance, memorisation).

**Status: scaffold.** This repo currently holds the medical team's data
screening code, copied over and reorganised into the target structure. It is
not wired together yet and does not run end to end. See
[`docs/porting-checklist.md`](docs/porting-checklist.md) for exactly what's
left, and [`PROVENANCE.md`](PROVENANCE.md) for where every file came from.

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
launchers/slurm/        SLURM wrappers, ported as-is, paths not yet fixed
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
