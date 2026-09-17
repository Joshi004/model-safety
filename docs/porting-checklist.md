# Porting checklist

Copied from medpsy at commit `6243747898238c0b229eb9a94fa5b7d61a46c030`,
unchanged except 5 import lines (moving `source_contract.py` to
`contract/textract.py` broke every `from source_contract import ...` /
`from pii_scan.source_contract import ...` line; there were 5 of these,
not the 4 originally estimated — see `PROVENANCE.md`).

Nothing below is done yet.

## Make it run
- [ ] `pip install -e .` works and every module imports
- [ ] Re-path the SLURM scripts in `launchers/slurm/` (all hardcode `synth_data_gen/`)
- [ ] Consolidate 4 `requirements.txt` files into pyproject extras
- [ ] Rename the two `run.py` files to `toxicity_screen.py` / `dedup_scan.py`

## The three behaviours
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
