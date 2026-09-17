# Porting checklist

Copied from medpsy at commit `6243747898238c0b229eb9a94fa5b7d61a46c030`,
unchanged except 5 import lines (moving `source_contract.py` to
`contract/textract.py` broke every `from source_contract import ...` /
`from pii_scan.source_contract import ...` line; there were 5 of these,
not the 4 originally estimated — see `PROVENANCE.md`).

`readers/prepare_rows.py` has an unconditional top-level
`from chunked_writer import write_chunked_output`. `chunked_writer.py` was
missed in the original scaffold pass, then restored — see `PROVENANCE.md`.
`readers/` also has its own `requirements.txt` now (`pandas`, `datasets`,
`pyarrow`) — not a medpsy file, since medpsy ran this script inside its one
shared project-wide venv rather than an isolated one; see `PROVENANCE.md`
for why only these three packages, not medpsy's full root `requirements.txt`.

## Make it run
- [x] Re-path the SLURM scripts in `launchers/slurm/` to this repo's layout
      (`REPO` no longer appends `/synth_data_gen`; `PII_DIR`/`PIPELINE_DIR`/
      `TOX_DIR`/`DEDUP_DIR` point at `modelsafety/data_screening/...`;
      the three directly-invoked scripts point at their new `modelsafety/`
      locations; `#SBATCH --output`/`--error` point at `logs/slurm/`;
      the two submit scripts invoke `sbatch` against `${SCRIPT_DIR}/...`
      instead of the old code-adjacent paths). Verified statically: every
      `bash -n` passes, zero leftover `synth_data_gen`/`tools/data_prep`
      references, every referenced `.py` and `.sbatch` path exists on disk.
      Not yet verified by an actual `sbatch` submission — there is no
      cluster or data available from this environment.
- [x] `PYTHONPATH="${REPO}"` exported by every launcher that runs a script
      importing `modelsafety.contract.textract`
      (`pii_scanner_fast.py`, `pii_extractor.py`, `pii_llm_validator.py`,
      `quality_filter_finalize.py`) — confirmed the import resolves under
      that `PYTHONPATH` without running a scan. No `pip install -e .` step
      needed for the launcher workflow.
- [ ] `pip install -e .` works and every module imports (only relevant if
      something other than the launchers is used to run this code —
      `pyproject.toml` still declares `dependencies = []`, unchanged)
- [ ] Consolidate 5 `requirements.txt` files into pyproject extras (reverted
      — each module keeps bootstrapping its own venv from its own
      `requirements.txt`, matching medpsy's pattern for the four scanner
      modules; `readers/requirements.txt` itself is new, see `PROVENANCE.md`)
- [ ] Rename the two `run.py` files to `toxicity_screen.py` / `dedup_scan.py`

## The three behaviours

A `scripts/screen.sh` bash guided runner (env-or-prompt resolution, a
two-level flow menu, dependency preflight, `run_config.json` replay) was
built and verified for all 30 direct-execution flows, then deliberately
reverted in favour of running the original medpsy `sbatch` workflow as-is.
It was never committed, so there's nothing to recover from git history —
picking this back up means rebuilding it, not restoring it.

- [ ] `runconfig/` resolver: flag > config > env > ask > default
- [ ] `scripts/screen.sh` asks, validates, freezes `run_config.json`, submits
- [ ] `--non-interactive` lists what is missing instead of prompting
- [ ] Add `redact` and category routing to `decisions/`
- [ ] Span map in `contract/textract.py` (blocks redaction only)
- [ ] `results/resolve.py`: finding or report -> original row
- [ ] `results/lineage.py`: row mapping across dataset versions

## Contract
- [ ] Stable row ID on every record
- [ ] Optional `image_path` on the record
- [ ] Scanners take a resolved config instead of reading the environment
- [ ] Toxicity stage 1 passes source columns through (known gap)

## Decide later
- [ ] Keep or drop `medical_guard_pilot.py`, `summarize_qwen_record_impact.py`
- [ ] Move policy files to `configs/`
- [ ] Git remote and repo home

## Then
- [ ] Parity run against a completed medpsy run
