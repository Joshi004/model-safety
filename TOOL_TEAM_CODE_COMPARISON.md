# Our safety code vs. the tool team's: what each side covers, and what we should take

Someone asked: the tool team has written their own data-safety code — how does
it compare to ours, and is there anything we should be borrowing? This is the
answer, written after reading both codebases line by line.

**Where the two codebases are:**

| | Path | What's in it |
|---|---|---|
| **Ours** | `/home/naresh/model-safety/modelsafety/` | ~9,000 lines: PII (regex + AI model), toxicity, dedup/decontamination, regurgitation, readers, 22 SLURM launchers |
| **Theirs** | `/home/shared/agentic_slm/qvac-research-tool-call/data_processing/decontamination/` | ~1,400 lines: one tool (`decontaminate.py`, 709 lines) + its tests (674 lines) + a 250-line README + a benchmark registry |

## The short version

**We barely overlap.** Their entire safety codebase does one job: remove
training rows that contain evaluation-benchmark questions. We do that too
(it's one mode of our dedup tool), plus four other things they don't touch at
all.

**In the one place we do overlap, their implementation is better** — not
because it's bigger, but because it's purpose-built for this exact job and
ours is a general-purpose near-duplicate engine pointed at the problem. They
get the details right that decide whether the answer is correct.

**But theirs would fall over on our scale.** It's single-process, no cluster
integration. Ours is built for 40M rows across a SLURM cluster.

So the honest summary: **borrow their accuracy, keep our scale.** There are
about five specific things worth lifting, and one of them closes a gap we
already documented as an open problem in `TOOL_TEAM_DATA_READINESS.md`.

---

## What each side actually has

| Safety job | Ours | Theirs |
|---|---|---|
| Find PII with regex patterns (emails, phones, cards, addresses, IPs, wallets) | **Yes** — 2-stage, ~2,200 lines, plus an LLM double-check stage | No |
| Find PII with an AI model (NER) | **Yes** — ~2,400 lines, GPU-based | No |
| Find toxic/unsafe content | **Yes** — Detoxify + Qwen3Guard, ~1,900 lines | No |
| Check whether a *trained model* regurgitates PII | **Yes** — 1,129 lines | No |
| Find duplicate rows *within* a dataset | **Yes** — MinHash/LSH near-dup | No |
| **Remove rows that leak benchmark questions** | **Yes** — "cross mode" of our dedup tool | **Yes** — the whole tool |
| Convert parquet/arrow/json → scannable JSONL | **Yes** — `prepare_rows.py` | Not needed — reads formats directly |

Only the bolded row is a genuine head-to-head. Everything else, one side has
and the other doesn't.

---

## The one real overlap: benchmark decontamination

### First, a nuance that matters

The two tools aren't quite chasing the same thing:

- **Theirs asks:** "does this training row contain a question from one of our
  11 eval benchmarks?" It answers with exact word-level matching and writes
  out a cleaned copy of the dataset.
- **Ours asks:** "is this training row roughly similar to a row in some
  reference set?" It answers with MinHash/LSH fuzzy similarity. Point the
  reference set at benchmarks, and you get decontamination — but that's a
  general tool doing a specific job, not a tool designed for it.

That difference explains most of what follows.

### Where their implementation is better, and why

**1. They read the fields the data actually keeps its content in.**

This is the big one. Their `instance_text()` pulls text from:
- `user` turns of `prompt` / `messages`
- **`conversations`** (the ShareGPT-shaped `from`/`value` column)
- **`answer`**
- **`reward_model.ground_truth`**

Ours reads only `text`, or `messages` with `role`/`content`. Those last three
fields are *exactly* the three gaps we documented as unsolved in
`TOOL_TEAM_DATA_READINESS.md` — the "golden answer" content that never appears
in `prompt`. They already solved all three, in about 20 lines.

**2. They deliberately ignore system prompts; we deliberately include them.**

Their rule, stated in their README: *"System prompts and tool definitions are
not matched (schema overlap is not instance contamination)."* They take
`role == "user"` turns only.

Ours includes everything — our `--text-scope prompt` setting only strips the
*last* message if it happens to be an assistant turn. Everything else (system,
tool, mid-conversation assistant) goes into the similarity comparison.

Why this matters concretely: I measured the MOPD and RL mixes earlier — the
system turn (the tool-definition boilerplate) is **70–93% of a row's
characters**. So when our tool compares two rows, the great majority of what
it's comparing is identical boilerplate that says nothing about whether the
rows are actually the same. Two completely unrelated questions that call the
same tool will look highly similar to us, and won't to them. They're right and
we're wrong here, and it's not a close call.

**3. They spent real effort on not over-removing.** Their code has three
guards ours has none of:
- **Degenerate n-gram filtering** — n-grams that are mostly 1–2 character
  tokens (`"A B C D E F G H I J"`), mostly digits, or entirely function words
  ("let me know if there s anything else") never trigger a match. Their README
  gives a real example this caught: a coordinate pair matching a
  slope-gradient benchmark item against a row that sums two temperatures.
- **Template-aware handling** — for IFEval-style benchmarks, whose prompts are
  assembled from a shared constraint library that our own IF training data is
  *also* built from, template text is excluded. Without this you delete real
  capability data for overlap that was inevitable by construction.
- **The "instance-specific" rule** — a matched 10-gram only counts if it
  belongs to one benchmark item, *or* every item carrying it has identical
  text. Their README explains why the second clause exists: 47.9% of BFCL
  components are duplicates of another item, and a duplicated test item is
  still a test item. Getting this wrong meant verbatim BFCL copies scored
  clean.

**4. Their reporting answers the question you actually have.** Both tools
report per-benchmark contamination. Theirs separates two things ours conflates:
- `leaked_pct_of_benchmark` — *can I still trust a score on this benchmark?*
- `training_rows_matched` + `training_rows_per_leaked_item` — *what did
  cleaning cost, and is that number inflated by one item repeated 73 times?*

Their README's example makes the case: 73 rows carrying one HellaSwag item is
0.01% of HellaSwag; 22 rows carrying 8 AIME25 items is 27% of AIME25. Ranked
by rows removed, HellaSwag looks worse. Ranked by what actually leaked, AIME25
is the one whose score is void. Our CSV reports rows-matched per benchmark
*file*, with no item-level deduplication — so we'd rank those two the wrong
way round.

**5. Nothing fails quietly.** Theirs exits with an error, naming the flag that
waives it, when: a benchmark path resolves to no data; a registry entry names
a column the data doesn't have; the input folder holds a file in an
unsupported format; a file yields no instance text at all. And "nothing was
indexed" is fatal *even with the waiver*, because an empty index makes every
row trivially clean. Every waiver used is recorded in `summary.json`.

Their README is candid about why: BFCL and ACEBench — their two main
tool-calling benchmarks — were silently skipped for a while after a directory
reorganisation, producing clean-looking reports that meant nothing.

Ours does have `_SUCCESS` markers and counts `rows_without_text`, but nothing
equivalent to "this benchmark contributed zero items, stop the run."

**6. They have tests. We have none.** 674 lines, 52 test methods, including a
regression test that injects a verbatim item from each real benchmark and
asserts it gets caught. Our `modelsafety/` tree has zero test files.

### Where our implementation is better

**1. We can actually run at corpus scale.** Ours has multiprocessing
(`--workers`, defaults to all cores), a SLURM launcher, input manifests for
excluding known-bad files, and progress logging every 100k rows. Theirs is
single-process — their own README says "roughly 100k rows per few minutes on a
login node." For the ~40M parquet rows in the data lake, that's the difference
between a job and an afternoon.

**2. We find duplicates *within* a dataset; they have no such thing.** Our
global mode clusters near-duplicate rows with union-find and picks a canonical
survivor. Theirs only ever compares training data against benchmarks. Two
copies of the same training row sail straight through their tool.

**3. Fuzzy matching is a genuinely different instrument.** Theirs is exact
10-grams plus an LCS rule — precise, but it can't say "these two rows are 80%
alike overall." Ours can, tunably. For decontamination their precision is the
right trade; for finding sloppy duplicated training data, ours is.

**4. We verify our inputs haven't changed.** `modelsafety/contract/textract.py`
has SHA-256 source-snapshot verification, so a later pipeline stage fails loudly
if the file it's reading was modified after the earlier stage read it. Theirs
has no equivalent.

**5. We handle far more input than they'd ever see** — the readers layer
(`prepare_rows.py`) converts parquet, arrow, HF `save_to_disk` folders, CSV and
JSON, with text-coverage validation. Though see the shortfall below: needing
that conversion at all is partly the problem.

---

## Their shortfalls (being fair, not diplomatic)

- **Single-process, no cluster story.** Biggest practical limit. No
  parallelism, no sbatch, no manifest/resume.
- **No within-dataset deduplication.** Not their goal, but it means their tool
  can't be the only data-cleaning step.
- **Lexical only, and they say so.** No paraphrase detection — a reworded test
  item with different numbers passes straight through. Their README points at
  a separate TF-IDF audit pipeline for that, which lives outside this tool.
- **A stated detection floor.** Short benchmark prompts ("Turn off wifi")
  can't be caught by a 10-gram rule at all. They document this honestly rather
  than hiding it, but it is a real hole — it mostly affects ToolSandbox and
  symbol-heavy maths items.
- **Their benchmark registry is hand-maintained.** Eleven entries with
  hardcoded absolute paths. Robust (they deliberately avoid the HF cache
  because its hash directories change), but someone has to keep it current.
- **They don't read `tool_calls` blocks either.** Same blind spot as us for
  structured function-call arguments. (Not an issue for the three mixes we
  analysed — those have no `tool_calls` column — but it is for raw upstream
  data.)

## Our shortfalls (the honest list)

- **Everything in "where theirs is better" above**, most importantly the text
  surface and the system-prompt dilution.
- **Zero tests across ~9,000 lines.** Theirs has ~45 test methods for 709
  lines of logic.
- **`--text-scope prompt` is misleadingly named.** It sounds like "compare the
  prompt only." It actually means "compare everything except a trailing
  assistant turn." Anyone reading the flag name would assume we already do
  what the tool team does. We don't.
- **We need a conversion step they don't.** They read parquet/jsonl/json
  natively and write cleaned output back in the original format. We require
  everything be converted to JSONL first, which is where all the mapping
  gotchas documented in `TOOL_TEAM_DATA_READINESS.md` live — and our output
  comes back as JSONL, so you'd have to convert back.
- **Our benchmark prep guesses at column names.** `prepare_benchmarks.py`
  tries `question`, `Question`, `QUESTION`, `Text`, `text`, `query`,
  `instruction`, `prompt` in order and takes the first hit. Convenient, but if
  a benchmark file uses a name not on that list, the row is silently skipped
  (counted, at least, in `skipped_no_prompt`). Their explicit registry can't
  fail that way — a wrong column name is a hard error naming the columns that
  do exist.
- **Two of our four copies of the text-extraction logic have already
  drifted.** Our own docs said these four were byte-for-byte identical. They
  aren't any more:

  | File | Guard on empty `text` |
  |---|---|
  | `contract/textract.py` | `isinstance(text, str) and text` ✅ |
  | `pii/model/common.py` | `isinstance(text, str) and text` ✅ |
  | `dedup/run.py` | `isinstance(text, str)` ❌ |
  | `toxicity/run.py` | `isinstance(text, str)` ❌ |

  Practical effect: a row shaped `{"text": "", "messages": [...real
  conversation...]}` gets fully scanned by the PII tool and silently skipped
  by the dedup and toxicity tools. Narrow, but it's the exact drift the docs
  warned would happen, and it has happened.

---

## What I'd actually take from them

In priority order, most value first:

1. **Their `instance_text()` field surface.** Teach our extractor to read
   `conversations` (`from`/`value`), `answer`, and `reward_model.ground_truth`.
   This single change closes all three gaps we flagged as open in
   `TOOL_TEAM_DATA_READINESS.md`. It's ~20 lines, and they've already proven
   the shape works on exactly our data.

2. **A user-turns-only text scope for dedup.** Given system prompts are 70–93%
   of the text in these mixes, comparing them is mostly comparing boilerplate.
   Add a real "user turns only" option (and consider making it the default for
   cross/decontamination mode). Also rename or document `--text-scope prompt`,
   which currently promises something it doesn't do.

3. **Their degenerate n-gram guards.** Cheap, self-contained, and they prevent
   a whole class of false removals we'd currently make. Roughly 15 lines
   (`gram_ok`).

4. **Fail loudly when a reference set indexes nothing.** We already do exactly
   this in `prepare_rows.py` (0% text coverage = exit 2, no `_SUCCESS`). The
   same discipline just hasn't reached the dedup tool. Their framing is worth
   copying too: every waiver gets a named flag and gets recorded in the output
   summary.

5. **Item-level leakage reporting.** Report "% of the benchmark that leaked"
   separately from "training rows removed", with a rows-per-leaked-item ratio.
   It's the number that decides whether an eval score is reportable, and today
   ours can't produce it.

Worth considering, lower priority: native parquet input for the scanners (skip
the JSONL round-trip), an explicit benchmark registry instead of column
guessing, and their template-benchmark handling if we ever decontaminate
IF-style data ourselves.

**Not worth copying:** their single-process execution model. Ours is better
there and we should keep it.

## What they could take from us

Worth mentioning if this turns into a two-way conversation: parallel execution
and SLURM launchers, within-dataset deduplication, source-snapshot SHA-256
verification, and — the big one — the entire PII and toxicity screening
surface, which they currently have no equivalent of at all.

## Verify any of this yourself

```bash
# Their tool, in full (709 lines) and its README (the reasoning + paper refs)
less /home/shared/agentic_slm/qvac-research-tool-call/data_processing/decontamination/decontaminate.py
less /home/shared/agentic_slm/qvac-research-tool-call/data_processing/decontamination/README.md

# The field surface we're missing -- their instance_text()
sed -n '445,471p' /home/shared/agentic_slm/qvac-research-tool-call/data_processing/decontamination/decontaminate.py

# Ours, for the same job
sed -n '57,78p' /home/naresh/model-safety/modelsafety/data_screening/dedup/run.py

# Their tests exist (52); ours don't (0)
grep -c "def test_" /home/shared/agentic_slm/qvac-research-tool-call/data_processing/decontamination/test_decontaminate.py
# the -not -path is essential: without it you count numpy's own tests inside our bundled venvs
find /home/naresh/model-safety/modelsafety -name "test_*.py" -not -path "*/.venv/*" | wc -l

# The four drifted copies of our text extraction
cd /home/naresh/model-safety
for f in modelsafety/contract/textract.py modelsafety/data_screening/dedup/run.py \
         modelsafety/data_screening/toxicity/run.py modelsafety/data_screening/pii/model/common.py; do
  echo "== $f"; grep -A1 'record.get("text")' "$f" | head -2
done
```

## Related reading

- [`TOOL_TEAM_DATA_READINESS.md`](TOOL_TEAM_DATA_READINESS.md) — the three
  datasets this all applies to, and the `conversations` / `answer` /
  `reward_model.ground_truth` gaps that recommendation #1 above closes.
- [`docs/data-screening/dedup.md`](docs/data-screening/dedup.md) — how our
  dedup tool is meant to be run.
- [`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md) — section 3
  is where the "four copies of the same logic" risk was first written down.
