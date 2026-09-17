# One repo for data screening and behaviour screening

*Written Sep 2026. Follows `unification-plan.md`, which found that our safety work sits in three codebases that don't know about each other. That document argued for unifying; this one is about the repository that does it, and about the first slice of work — moving the medical team's scanners in and proving they still give the same answers.*

---

## The short version

We're building one repository that answers two questions about every model we ship: **is the training data clean**, and **does the model behave**. Right now both questions get answered separately by whoever happens to need them. The medical team built the data half and has never attacked a model. The vision team built the attack half and has never screened a corpus. The tool team has neither, and if nothing changes they will build a third copy of both.

The point of the new repo is that the tool team builds almost nothing: they write a reader for their data shape, point the same scanners at it, and get the same report. Everything the medical team has already solved — the hash-bound provenance, the two-pass screening, the decision join — becomes shared infrastructure rather than something living under one project's `tools/` directory.

We start the repo from scratch but write very little new logic. The data scanners are ported from the medical stack. The text red-teaming comes from **deepteam, which we use as a pinned upstream dependency and never fork**. Phase 1 is deliberately narrow: move the medical data-screening modules in, put them behind one record contract, and prove on datasets we already have that they produce the same results as before.

Three things are required of every scan in it, and they're the main change from how the existing tools behave: **you run one script and it asks you for anything it can't work out**, instead of failing — or worse, silently defaulting — on a missing environment variable; **every scan makes you choose whether to just report, to redact, or to remove**, and never edits source data in place; and **every finding resolves back to the exact original row at any time**, including after a redaction or removal has produced a new version of the dataset.

---

## Why a new repo and not another folder somewhere

Three practical reasons:

- **The medical scanners are in the wrong place to be shared.** They live inside a synthetic-data-generation project, under `synth_data_gen/tools/data_prep/`. Anyone outside that team who wants toxicity screening today has to clone a data generation repo and know which subfolder to look in. Nobody does that.
- **Ownership.** The tool team is not going to send pull requests into the medical team's data pipeline, and the medical team shouldn't have to review tool-team changes to keep their pipeline moving.
- **Release cycles.** deepteam is upstream and updates on its own schedule. The vision harness is mid-experiment across a dozen checkpoints. Neither wants to be coupled to the other.

One thing to be honest about: `unification-plan.md` argued for a *thin* layer that calls the existing tools rather than absorbing them, and this plan absorbs the medical ones. The reason for the change is that those scanners aren't importable today — they're scripts driven by environment variables, each with its own `requirements.txt` and its own venv. You can't call them as a library from outside without either shelling out to `sbatch` or restructuring them, and restructuring them in place means churning a repo that is currently wired into a live data pipeline. So they get copied here and adapted, with a parity check before the originals are retired.

**The risk that buys us:** for a while there will be two copies of the PII and toxicity code, and they can drift. The mitigation is the parity run described below and an agreement up front that the copies are temporary. If we skip that agreement, we've built the fourth stack that the unification plan warned about.

---

## What the repo covers

Two halves, sharing one spine.

**Data screening** — given a corpus, find what shouldn't be in it: personal information, toxic or harmful content, duplicates and benchmark contamination. Later: sensitive attributes, and image-side checks for the vision corpus.

**Behaviour screening** — given a served model, probe it: does it refuse harmful requests, does it resist jailbreaks, does it reproduce things it memorised from training.

What it explicitly is **not**:

- Not a data generation or training system. It reads corpora; it never produces them.
- Not a model server. It talks to an OpenAI-compatible endpoint that somebody else started, the same way both existing stacks already do.
- Not a fork of deepteam, and not a reimplementation of it.
- Not the owner of anyone's corpus. It can emit keep/drop decisions and write byte-preserved filtered partitions — the medical stack already does this and it's worth keeping — but it never edits source data in place.

---

## The principles worth writing down before any code

1. **One contract, many readers.** Adapters normalise each team's data into one boring row shape. Scanners only ever read that shape. A new dataset means a new fifty-line reader, never a new scanner.
2. **Index, never copy.** A finding refers to a source file, line number, and content hash. It does not carry a second copy of the corpus around.
3. **Any report resolves back to the exact original row, at any time.** Not "we could reconstruct it if we still have the paths" — a command that takes a finding and returns the source record, or tells you loudly that the source has changed.
4. **Ask rather than assume.** A missing setting is a question to the operator, not a silent default. Running a scan should mean running one script and answering what it asks.
5. **Finding something and acting on it are separate decisions.** Every scan reports by default. Redacting or removing anything is an explicit choice the operator makes per run, and the source data is never edited in place.
6. **Borrow, don't rewrite.** Every module that lands here records where it came from. If we find ourselves rewriting rather than porting, that's a signal to stop and ask why.
7. **deepteam stays upstream.** Pinned version, plus our adapters. If we need a behaviour it doesn't have, the first option is a contribution upstream, not a fork.
8. **Cheap screen, then expensive verify.** Both mature stacks independently converged on this. It's the pattern, not an implementation detail.
9. **One target interface.** `generate(prompt, image=None) -> text`. The vision harness already proved this works across model types; making the image optional makes every model we have probeable by one harness.
10. **Fail loud on bad input.** Malformed rows, changed source files, and missing columns fail the job. The medical stack is already strict about this and it's the reason its results can be trusted.
11. **Measure first, gate later.** Phase 1 produces numbers. Nothing blocks a training run until the numbers are trusted and thresholds are agreed.

Principles 3, 4 and 5 are new requirements rather than descriptions of what the ported code already does, so the next section spells out what they mean concretely and what has to be built to honour them.

---

## Three behaviours every scan has to have

These apply to all of it — PII, toxicity, dedup, red teaming, regurgitation. They're not per-scanner features, they're properties of the run harness, which is why they belong in the shared layer and not in any one scanner.

### 1. One script that asks for what it needs

**Today:** every scanner is driven by environment variables. Forget one and you get one of three outcomes: a clear failure (`PII_BASE_DIR is required`), a failure twenty minutes into a queued job, or — worst — a silent default. The toxicity job defaults `TOX_INPUT` to `tmp/qa_scan_test/input/data_000000_messages_pipeline.jsonl`, so a forgotten variable means a clean report about a test fixture instead of your corpus. Nothing about that is obvious in the output.

**Instead:** one entry point, `scripts/screen.sh`, that resolves every setting in a fixed order and asks for whatever is left:

```text
explicit CLI flag  >  run config file  >  environment variable  >  ask the operator  >  documented default
```

A default only exists where being wrong about it is harmless — worker counts, batch sizes, chunk sizes. Anything that decides *what gets scanned*, *where results land*, or *what happens to the data* has no default and is always either supplied or asked for.

The constraint that shapes this: **you cannot prompt from inside a batch job.** Under `sbatch` there is no terminal, so a prompt would hang a queued job holding a GPU. So the interaction happens in the submit wrapper, on the login node:

1. Resolve what it can from flags, config, and environment.
2. Ask for the rest, one question at a time, with the resolved value echoed back.
3. Validate while the human is still there — input path exists and contains JSONL, output directory is empty or `--overwrite` was given, the guard endpoint answers `/health`, the model is reachable. These are the failures that currently cost a queue wait each time.
4. Show a summary and ask for confirmation, including the scan mode from behaviour 2 below.
5. Write the answers to `run_config.json` in the run directory, then submit the batch job, which reads that frozen config and is strictly non-interactive.

Two properties fall out of this that matter more than the convenience:

- **Re-running takes no questions.** `screen.sh --config <path>` or `--from-run <dir>` replays a previous run exactly. The frozen config is also what makes a run reproducible months later, which is the same problem the manifest solves for data.
- **Automation still works.** `--non-interactive` (also assumed when stdin isn't a terminal) skips all prompting and exits with the complete list of what's missing, rather than hanging or guessing. CI and scheduled runs use this path.

Secrets are prompted without echo, never written to `run_config.json`, and passed to the job through the environment only.

### 2. Report, redact, or remove — chosen explicitly, every run

Every scan runs in one of three modes, and the mode is one of the questions the wrapper asks. There is no inherited or configured-once default, because "we removed 40,000 rows because a config file said so" is not a position anyone wants to defend.

| Mode | What it does | Source data |
|---|---|---|
| `report` | Findings and a report only. The default answer. | Untouched |
| `redact` | Writes a new dataset version with the flagged spans masked | Untouched; new version written alongside |
| `remove` | Writes a new dataset version with flagged rows dropped, plus a `dropped/` partition holding them | Untouched; new version written alongside |

The invariants that make this safe:

- **The source corpus is never edited in place, in any mode.** Redact and remove both write a new dataset version directory. The medical stack's `quality_filter_finalize.py` already works this way — it copies original bytes rather than re-serialising, so untouched rows are byte-identical — and that behaviour carries over.
- **Redaction and removal are a separate step from scanning,** running in `decisions/` over findings that already exist. A policy mistake means re-running the join, not re-running a GPU scan.
- **Every action is logged per row:** row ID, source file, line, source hash, category, which stage and rule decided it, and for redaction the exact span replaced and what it was replaced with. Nothing is dropped or masked without a record of why.
- **`remove` is irreversible for whoever gets the new version, so the dropped rows stay retrievable** through the `dropped/` partition and the resolve path in behaviour 3.
- **A fourth option exists in practice and shouldn't be lost:** the medical judge policy routes crisis material into a dedicated refusal-training set rather than deleting it. That's the same mechanism with a different destination, so `decisions/` should support routing a category to a named output rather than only keep/drop.

One honest caveat on the three modes: `report` and `remove` mean the same thing for every scan, but `redact` only really makes sense where a finding is a *span* — a phone number, a name, an API key in a tool argument. Toxicity and dedup judge whole messages or whole rows, so for those the meaningful choices are report, remove, or route. The wrapper should offer each scan only the modes that apply to it rather than pretending all three are available everywhere.

**What this needs that we don't have:** redaction requires knowing *where* in the original record a finding sits, and today neither PII backend can quite say. The regex stack records only the matched substring, with no offsets, so a value appearing twice in a row is ambiguous. The model-based backend does produce character spans — but both work against the *rendered* text that `extract_record_text` builds by concatenating `ROLE:\ncontent` blocks, not against the source JSON. Mapping a span in that rendered string back to a character range inside `messages[2].content` means the extractor has to emit a span map alongside the text. That's genuinely new code, it's small, and it belongs in `contract/textract.py` because every scanner benefits. `report` and `remove` work without it; only `redact` is blocked on it.

### 3. Every report resolves back to the original data

Most of this already exists in the medical stack and is the best thing about it — it just needs to be a first-class command instead of an internal implementation detail.

What each finding carries: row ID, source file (relative), line number, the SHA-256 of the exact source line, the category, and the matched text. What each run carries: the content-addressed manifest — every source file with its size, mtime, and SHA-256, plus the input root — verified before reading, so a source file that changed after the manifest was built fails the job instead of joining stale line numbers to unrelated records.

On top of that, three additions:

- **A `resolve` command.** `modelsafety resolve --run <dir> --finding <id>` reads the manifest, verifies the source file still matches, seeks the line, checks the line hash, and prints the original record. If the hash doesn't match it says exactly that, rather than printing a plausible wrong row. This is what makes "at any time" a real claim instead of an aspiration. It also takes a whole report, so "show me the fifty rows behind this toxicity number" is one command.
- **A lineage map across dataset versions.** This is the part that normally breaks: once `redact` or `remove` has produced v4 from v3, a finding on v4 has to trace back through v3 to the original. So every derived version writes a row-level mapping from new row to its parent row ID, and `resolve` follows the chain.
- **Run identity embedded in every report.** Run ID, manifest digest, scanner versions, thresholds, and the mode from behaviour 2. A report should never be ambiguous about which snapshot of which corpus it describes, or whether anything was changed as a result.

Findings hold references and matched spans, not row text. That keeps reports safe to copy into a shared bucket or a dashboard while the corpus stays where it lives.

---

## Proposed repo structure

Working name `qvac-model-safety`, Python package `modelsafety`. The name is a decision, not a conclusion — see the open questions.

```text
qvac-model-safety/
  README.md
  pyproject.toml              # one package, optional extras per scanner
  scripts/screen.sh           # the one entry point: asks, validates, submits
  modelsafety/
    contract/                 # the row + provenance contract everything shares
      record.py               #   canonical row: id, source_file, source_line, text, image_path
      textract.py             #   text / messages / tool calls -> one scannable string + span map
      manifest.py             #   content-addressed snapshot of a corpus
    runconfig/                # settings resolution and the operator prompts
      resolve.py              #   flag > config > env > ask > default
      prompt.py               #   questions, validation, confirmation, non-interactive mode
    readers/                  # per-dataset adapters: raw -> records
      jsonl_text.py
      jsonl_messages.py
      agent_traces.py
      image_text.py           # phase 3
    data_screening/
      pii/                    # regex stack + model-based backend + LLM confirm
      toxicity/               # fast screen + guard verification
      dedup/                  # near-duplicates and benchmark contamination
    model_screening/
      targets/                # generate(prompt, image=None) -> text
      redteam/                # deepteam adapters: callbacks, config, result mapping
      regurgitation/          # PII reconstruction probe now, canaries later
    results/                  # findings + summary schema, markdown report, run diff
      resolve.py              #   finding or report -> original source record
      lineage.py              #   row-level mapping across dataset versions
    decisions/                # report / redact / remove, byte-preserved partitions
  configs/                    # thresholds, judge policies, per-dataset settings
  launchers/slurm/
  examples/data/              # one-row fixtures per input shape
  docs/
    architecture.md
    data-screening/
    model-screening/
    operations/
```

A few notes on why it's shaped this way:

**`contract/` goes first** because everything depends on it and it depends on nothing. It's also mostly already written: `pii_scan/source_contract.py` handles text extraction from both row shapes, and `quality_filter_manifest.py` already builds the content-addressed snapshot. What it needs is a row ID, an optional image path, and a home that isn't inside one scanner's folder.

**`readers/` is the pressure valve** for the fact that every team stores data differently. That's a reading problem, not a scanning problem, and isolating it here is what keeps per-team onboarding cheap.

**`data_screening/` and `model_screening/` are deliberate siblings** rather than separate projects, because they share the contract, the results schema, the report, and the SLURM plumbing. Keeping them together is most of the duplication saving.

**`decisions/` is separate from `data_screening/`** because "what did we find" and "what do we do about it" deserve different review standards. Findings are reproducible facts; drop decisions are policy. It's also what makes the three modes cheap: switching from `report` to `remove` re-runs a join, not a scan.

**`runconfig/` and `scripts/screen.sh` are the whole interactive story.** Scanners never read the environment themselves and never prompt — they take a resolved config object. That keeps the prompting in one reviewable place and keeps every scanner usable from a batch job, a notebook, or another script.

**One package, one venv, optional extras.** Today there are three `requirements.txt` files and three venvs. Here, `pip install -e .` gets you the contract, the readers, and the regex scanners, and the heavy machinery (Detoxify, transformers, endpoint clients) comes in as extras so a PII-only run doesn't pull a deep learning stack.

**Documentation mirrors the medpsy convention** — `architecture.md` for the shared contracts, one folder per capability, `operations/` for SLURM and run hygiene. That convention works; no reason to invent another one.

---

## What we borrow, and from where

| Component | Comes from | Lands in | What has to change |
|---|---|---|---|
| Row contract + text extraction | medpsy `pii_scan/source_contract.py` | `contract/` | Add a stable row ID and `image_path`. Keep extraction semantics byte-identical so old results stay comparable |
| Corpus snapshot / manifest | medpsy `quality_filter_manifest.py` | `contract/manifest.py` | Generalise past the quality-filter run shape |
| PII regex scan, validators, extractor | medpsy `pii_scan/` stages 1–3 | `data_screening/pii/` | Take a resolved config instead of reading the environment. Keep the `path\|line\|sha256\|match` hit format and add span offsets |
| PII LLM confirmation | medpsy `pii_llm_validator.py` | `data_screening/pii/` | Share one endpoint client with toxicity stage 2 |
| Model-based PII backend | medpsy `pii_privacy_filter/` | `data_screening/pii/` | Keep the regex-vs-model comparison tooling — it's the only cross-check we have on either method |
| Toxicity fast screen | medpsy `toxicity/run.py` | `data_screening/toxicity/` | Fix the known column-retention gap: stage 1 must pass source columns through instead of rewriting rows into its slim shape |
| Guard verification + report | medpsy `llm_check_guard.py`, `guard_report.py` | `data_screening/toxicity/` | Judge policies become config, not code. `policies/medpsy_medical_sexual_v1.txt` is a domain policy and belongs under `configs/` |
| Dedup + contamination check | medpsy `dedup/` | `data_screening/dedup/` | Port as-is; carry over the warning that the index is global and in memory, so a dataset must not be split across array jobs |
| Decision join + partition writer | medpsy `quality_filter_finalize.py` | `decisions/` | Drop medpsy-specific path assumptions; add the `redact` mode and category routing next to the existing keep/drop |
| PII regurgitation probe | medpsy `pii_regurgitation_check.py` | `model_screening/regurgitation/` | Call the shared target interface instead of its own API client |
| Text red teaming | **deepteam, upstream** | nothing vendored | Write model callbacks, choose vulnerabilities and attacks, map its results into our schema |
| Target interface | vision harness `generate(image, prompt)` | `model_screening/targets/` | Make the image argument optional. That one change is what unlocks the rest |
| Vision attacks, judge, over-refusal | `modelsafety-vision/` | phase 3 | Nothing now |

Almost everything above is a port. Four things are not, and they're all from the three behaviours: the settings resolver and prompts (`runconfig/`), the span map that redaction needs (`contract/textract.py`), the `resolve` command, and the version lineage map. None is large, but they should be counted as new work rather than assumed to come free with the move.

deepteam is worth being concrete about, because "use it as-is" is easy to say and easy to quietly violate. It already ships roughly forty vulnerability categories, single-turn and multi-turn attacks, agent-specific checks, a trace scanner, and framework mappings (OWASP, NIST, EU AI Act). Its entry point takes a `model_callback`. So our entire integration surface is: a callback per model, a config saying which vulnerabilities and attacks to run, and a translator from its results into our summary schema. If we find ourselves editing files under `deepteam/`, something has gone wrong.

---

## Phase 1: the medical pieces, proven on data we already have

In scope:

- The contract and manifest, plus the span map.
- Readers for `text` and `messages` JSONL.
- `scripts/screen.sh` and the settings resolver, including the non-interactive path.
- PII: regex scan, heuristic validation, extraction, optional LLM confirmation.
- Toxicity: fast screen, guard verification, report.
- Dedup: both near-duplicate and cross-dataset contamination modes.
- The decision join with all three modes — `report`, `redact`, `remove` — and the byte-preserved partition writer.
- One summary JSON and one markdown report covering all three scanners, and the `resolve` command that maps either back to source.

Out of scope for phase 1, to keep it finishable: deepteam wiring, anything vision, the gate, sensitive attributes, CSAM, canary strings. The version lineage map can wait until a second derived version actually exists, but the row ID it depends on cannot — that ships with the contract.

### How we test it

Four levels, cheapest first.

**1. Smoke, locally.** The eight one-row files in `synth_data_gen/examples/data/` are the documented input contract for the medpsy pipelines and cover both the `text` and `messages` shapes. They exercise the readers, the extraction path, and the span map without touching a cluster.

**2. Parity against a medpsy run that already happened.** Take a dataset the quality filter has already scanned on the cluster, run the new repo over the same input with the same thresholds in `report` mode, and compare: hit counts per PII category, the hit lines themselves, the guard label distribution split by role, and the dedup components. This is the acceptance test that actually matters. Without it we're assuming the port was faithful, and ending up with two systems that disagree and no way to tell which is right would be worse than not having moved at all.

**3. The three modes, on a copy of a real dataset.** `report` changes nothing. `redact` produces a version that differs from its input *only* in spans that appear in the findings — worth checking by diff, not by eye, because a redactor with an off-by-one in its span mapping will look fine in a spot check. `remove` produces kept and dropped partitions whose line counts add up to the input, with every dropped row carrying a logged reason. Then take a sample of findings from each and confirm `resolve` returns the right original row, including through the redacted version.

**4. A corpus the scanners have never seen.** Once parity holds, point it at another team's data. Agentic traces are the obvious first target, with the caution from the unification plan: tool arguments and tool results must be inside the text being scanned. That's where credentials and stray personal data actually hide, and a scan that skips those fields returns a clean result that means nothing.

Phase 1 is done when levels 2 and 3 pass and level 4 has produced a report that somebody read. Worth running the whole thing once via `screen.sh` with no environment variables set at all, answering every prompt, because that's how anyone outside the team will meet it.

### Roughly what comes after

- **Phase 2 — behaviour screening for text models.** Target interface, deepteam callbacks, first real red-team run against the agentic model, regurgitation probe moved over. This is the biggest coverage gap we have and the tool for it is already sitting in the workspace unused.
- **Phase 3 — vision.** Data screening for the image corpus, and the vision attack library behind the shared target interface so its image-borne attacks become available to other models and deepteam's multi-turn attacks become available to the vision model.
- **Phase 4 — the gate.** Thresholds per check, evaluated before a training run or a release, failing loudly. Only worth building once the numbers underneath it are trusted.

---

## What we don't know yet

These need answers from outside this document, and a few of them block work.

- **Repo name and where it's hosted.** `qvac-model-safety` is a placeholder. `modelsafety-core` reads more naturally next to the existing `modelsafety-vision`, if we expect that repo to fold in eventually.
- **Who owns the shared layer.** The unification plan flagged this and it's still the biggest non-technical risk here. Two teams, one shared surface, nobody named: that's how this becomes a fourth half-maintained stack instead of a consolidation.
- **Whether the originals get retired.** My recommendation is yes: after parity passes, the medpsy `tools/data_prep/{pii_scan,toxicity,dedup}` trees become a dependency on this repo rather than a second copy. Until someone agrees to that, we are knowingly carrying duplicate code.
- **Where run artifacts live.** Every run currently writes to a path under whichever repo launched it. "Unified S3 bucket structure" is already on the task list, and it's cheaper to settle alongside the results schema than to retrofit. Straw man worth arguing with: `s3://<bucket>/<team>/<dataset>/<version>/<check>/<run-id>/`, with the summary JSON and `run_config.json` at predictable keys so a dashboard can find every run without crawling. Findings stay as references — no corpus text in the bucket. The `resolve` command then needs a rule for how it reaches source data from a report pulled out of the bucket, since the manifest paths are cluster paths.
- **Who is allowed to approve a `remove` run, and how long dropped rows are kept.** The mechanism makes the action explicit and logged, which is the engineering half. The other half is policy: whether dropping tens of thousands of rows needs a second pair of eyes, and whether the `dropped/` partition is retained indefinitely, expires, or gets treated as sensitive in its own right — it is, after all, a concentrated collection of everything we flagged.
- **What the tool team actually has.** "Get code from the tool team" has been on the list and hasn't happened. Their data shape determines the second reader we write, and if they've already built screening of their own, that changes what phase 1 should port.
- **Ground truth.** We still have no labelled rows, so we still cannot state precision or recall for any of these scanners, before or after the move. That's independent of this repo existing and remains the cheapest genuinely valuable thing on the list.

---

## In a paragraph

We're creating one repository so that data screening and model probing stop being things each team reinvents. It starts as a home for the medical team's data scanners — the most production-ready safety code we have — restructured behind a single record contract with per-dataset readers, so that pointing them at the tool team's traces or the vision team's captions becomes configuration instead of engineering. Three behaviours are required of everything in it: you run one script and it asks for whatever it can't find rather than falling back on a default that silently scans the wrong thing; every scan makes you choose between reporting, redacting, and removing, and never edits source data in place; and every number in every report resolves back to the exact row that produced it, including after a redaction or removal has created a new dataset version. deepteam comes in as a pinned upstream dependency for text red teaming and is never forked; the vision harness follows later, once its `generate` call has been generalised to make images optional. The first milestone is not a new capability at all: it's the same medical scanners in the new repo, producing byte-identical results on a dataset they have already scanned, which is what earns us the right to delete the old copies. The parts most likely to go wrong are not technical — nobody owns the shared layer yet, and two copies of the scanners will exist until someone agrees they shouldn't.
