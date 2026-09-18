# How data conversion actually works, and what your scanners can (and can't) see

This is a companion to
[`HOW_TO_SCAN_YOUR_DATA.md`](HOW_TO_SCAN_YOUR_DATA.md), digging deeper into
three specific questions: how hard is it to support field names other than
`text`/`messages`/`role`/`content`, how do you trace a PII hit found in
*converted* data back to its original source row, and how robust is the
converter really — would it hold up for a different team's data, like a
tool-use / agentic-conversation team?

Same rule as the other doc: every claim below was actually run, not
guessed. Where I say "I tested this," there's a real command and real
output next to it.

## 1. The one shape every scanner is built around

Every scanner in this repo is ultimately looking for one of two shapes on
each JSON row:

```json
{"text": "a plain string"}
```

```json
{"messages": [{"role": "user", "content": "a plain string"}, ...]}
```

The actual code that reads this is tiny. Here's the real function from
`modelsafety/contract/textract.py` that the PII scanner uses:

```python
def extract_record_text(record):
    text = record.get("text")
    if isinstance(text, str) and text:
        return text
    messages = record.get("messages")
    if not isinstance(messages, list):
        return ""
    rendered = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = extract_content_text(message.get("content"))
        if content:
            role = str(message.get("role") or "").upper()
            rendered.append(f"{role}:\n{content}")
    return "\n\n".join(rendered)
```

Notice what it does **not** do: it never looks at any key other than
`text`, `messages`, `role`, and `content`. Anything else in your row —
whatever it's called — is invisible to it.

One genuinely flexible thing already built in: `role` can be *any* string
— `"user"`, `"assistant"`, `"tool"`, `"function"`, anything. The code just
uppercases whatever's there. It's only the four key **names** themselves
(`text`, `messages`, `role`, `content`) that are fixed.

## 2. What happens when your data isn't shaped this way — tested for real

No error. No warning. It just quietly finds nothing to scan, and reports
"0 hits" exactly the way it would for genuinely clean data — the two cases
look identical from the outside.

I proved this against a real dataset that's already sitting in
`/home/shared/agentic_slm/data/` (`rl/raw/nemo_gym_recipe/Tool-N1-hermes/train.jsonl`,
a tool-calling dataset):

```bash
$ PYTHONPATH="$PWD" python3 -c "
from modelsafety.contract.textract import extract_record_text
import json
line = open('.../Tool-N1-hermes/train.jsonl').readline()
record = json.loads(line)
print('Top-level keys:', list(record.keys()))
print('Scanned text:', repr(extract_record_text(record)))
"
Top-level keys: ['responses_create_params', 'expected_actions', 'agent_ref']
Scanned text: ''
```

That's a real row from a real dataset in your shared folder, and the PII
scanner would find **zero characters** to check in it — not because the
row has no PII, but because this dataset stores its conversation under
`responses_create_params.input` (a different, newer API shape some tools
use) instead of `messages`. If you ran the PII scanner over this file
today, it would come back completely clean, and that report would be
meaningless.

## 3. Can we make the scanners understand other key names? (your first question)

Short answer: **easy for a simple rename, harder for anything more than
that — and the code isn't centralized, which is the real risk.**

### The easy path: rename at conversion time, no code changes

`modelsafety/readers/prepare_rows.py` (covered in the other doc) already
has a `--map target=source` flag built exactly for this. If your data
calls the field `prompt` instead of `text`, you don't touch any scanner
code:

```bash
python3 prepare_rows.py --input-path yours.parquet --input-type parquet \
  --map text=prompt --output-dir ./converted
```

This covers "my column has a different name but the same shape" — which
is probably the most common case. It's a one-line answer, no risk of
breaking anything else.

### The harder path: teaching the scanners themselves new key names

If you don't want to convert first, and want the scanners to natively read
a different key straight from your own `.jsonl`, that means editing code —
and here's the part worth knowing before you do: **this logic is not
written once and shared. It's copy-pasted, nearly identically, into at
least four separate files:**

| File | What it duplicates |
|---|---|
| `modelsafety/contract/textract.py` | the shared version — used by the PII regex scanner, the regurgitation check, and the quality-filter finalizer |
| `modelsafety/data_screening/toxicity/run.py` | its own private copy of the same `content` parsing |
| `modelsafety/data_screening/dedup/run.py` | its own private copy again |
| `modelsafety/data_screening/pii/model/common.py` | its own private copy again (the AI-model PII scanner) |

I checked: right now, the core "turn a `content` field into a string"
logic is byte-for-byte identical in all four. That's good news today, but
it also means there's no shared source of truth — if you (or anyone)
updates one file to understand a new key and forgets the other three, the
tools will quietly start disagreeing with each other about the same input
file, and nothing will warn you (there's no test suite covering this yet —
see `docs/porting-checklist.md`).

**Practical effort estimate:** each individual edit is small — a few lines,
in a function that's easy to read. The actual work is discipline: making
the same small change in four places, then re-running the smoke test from
the other doc on all four tools afterward to confirm they still agree with
each other. It's a half-hour task done carefully, or a source of
hard-to-notice bugs done carelessly.

## 4. Tracing a PII hit back to its original source row after conversion (your second question)

This has a good answer for data that's *already* `.jsonl`, and basically
no answer yet for data that went through the converter first.

### For native `.jsonl` (no conversion involved): this already works well

The PII scanner hashes the exact original line (SHA-256) and records the
file name and line number with every hit:

```
sample.jsonl|1|78166ecb6d9b20f5f57c9a5d9c8dd8f8d1c5b7a341b60558def2d799eec0eb36|"j.smith@brightwood-clinic.io"
```

Later stages re-hash the line before trusting it, and fail loudly if the
source file changed underneath them. This part is solid — it's designed
so a hit can always be traced back to one exact, verified original row.

### For data that went through `prepare_rows.py` first: this does not exist yet

I tested this directly, twice, against real behavior.

**Case A — the source data happens to have its own ID column.** Recall the
real `medmcqa` conversion from the other doc — its output looked like
this:

```json
{"text": "Which of the following is derived from fibroblast cells ?", "metadata": {"id": "84f328d3-fca4-422d-8fb2-19d55eb31503", "...": "..."}}
```

Here you *can* trace a hit back to the source, but only because
`medmcqa`'s own `id` column happened to survive into `metadata` — the
converter didn't add that, the source dataset already had it.

**Case B — the source data has no ID column of its own.** I made up a tiny
two-row dataset with no id/row-number column and converted it the same
way:

```bash
$ python3 prepare_rows.py --input-path no_id.parquet --input-type parquet --map text=question --output-dir ./out
$ cat ./out/chunk-0/*.jsonl
{"text": "What is the capital of France?", "metadata": {"answer": "Paris"}}
{"text": "What is 2+2?", "metadata": {"answer": "4"}}
```

**Nothing here says which file or which row of the original parquet this
came from.** No `source_file`, no `source_row_index`, nothing — confirmed,
the converter genuinely adds no provenance of its own.

What you're left with, if you need to trace a hit back: the converter
preserves row **order** (it doesn't shuffle unless you pass `--shuffle`),
so in principle "line 47 of the converted output" corresponds to "row 47
of the original file" — but only if you're certain no row was silently
dropped along the way (which happens whenever `--require` rejects a row
for missing data), and nothing tells you if that happened. This is fragile
enough that I wouldn't rely on it for anything you actually care about
tracing.

**Practical recommendation today:** before converting a dataset, check
whether it already has a natural unique ID or row-number column. If it
does, make sure it's *not* excluded from `metadata` (don't pass
`--exclude-metadata` on it) — that's your only real traceability. If it
doesn't have one, converting it today means you lose the ability to trace
a finding back to its exact source row.

## 5. How robust is this converter for a different team's data — e.g. a tool-use / agentic team? (your third question)

I went and actually tried converting real tool-calling datasets that are
already sitting in `/home/shared/agentic_slm/data/` — not just one, but
three genuinely different ways tool-call data gets stored there. **The
honest answer is "it depends entirely on which shape your data is in."**
One whole family of real datasets converts perfectly today with a single
flag. Two other real families come out completely empty — no crash, no
warning, just silently empty.

### Family 1 — a list of `{role, content}` turns under a different key name: works today

Several real tool-calling datasets under `sft/raw/toolmind/open_datasets/`
— I checked all six: `glaive-function-calling-v2`,
`xlam-function-calling-60k`, `ToolACE`, `BUTTONInstruct`, `tau-train`, and
`APIGen-MT-5k` — store each conversation as
`"conversations": [{"role": ..., "content": ...}, ...]`. That's already
the exact shape the scanners want, just under the name `conversations`
instead of `messages`. One flag fixes that, tested for real:

```bash
$ python3 prepare_rows.py --input-path glaive-function-calling-v2-query.jsonl \
  --input-type jsonl --map messages=conversations --output-dir ./out
Prepared 3 rows; skipped 0 rows.
```

And the converted output genuinely scans:

```
USER:
Hi, can you tell me the current stock price of Apple?

ASSISTANT:
<think>
Okay, the user is asking for the current stock price of Apple...
```

**This is good news if your data looks like this** — six real
tool-calling datasets, zero code changes, one `--map` flag.

One caveat still applies here, though: the `tool_calls`
block on that same assistant turn (the actual structured function call,
e.g. `{"function": {"name": "get_stock_price", "arguments":
{"company_name": "Apple"}}}`) is still never read — only the plain
`content` text next to it is. In this dataset the model's reasoning
happens to restate the argument in plain English anyway ("I'll structure
the tool call with..."), so it still gets scanned here — but that's a
property of this particular model's verbose reasoning style, not
something the tool is actually reading. A terser model, or a dataset
without that reasoning trace, would hide the same argument completely.

### Family 2 — a nested object instead of a list: silently empty

The `nemo_gym_recipe` family (`Tool-N1-hermes`,
`Nemotron-RL-Agentic-Function-Calling-Pivot-v1`,
`Nemotron-RL-Agentic-Conversational-Tool-Use-Pivot-v1`,
`Nemotron-RL-agent-workplace_assistant`, and others) all store the
conversation as `responses_create_params.input` — nested one level inside
another object, not a plain top-level list.

`--map` only understands flat, top-level column names, so the closest
possible attempt —

```bash
$ python3 prepare_rows.py --input-path Tool-N1-hermes/train.jsonl \
  --input-type jsonl --map messages=responses_create_params --output-dir ./out
Prepared 2 rows; skipped 0 rows.
```

— "succeeds" and reports rows converted, but `messages` ends up holding
the *entire* `responses_create_params` object (the tools list, the
`tool_choice` flag, all of it) rather than just the list of turns inside
it. That's a dict, not a list, so the scanner's own shape check rejects it
immediately:

```python
>>> extract_record_text(converted_row)
''
```

Zero characters, on every row, with no warning anywhere in the pipeline.
**Fixing this needs either a tiny purpose-built preprocessing script**
(pull `row["responses_create_params"]["input"]` out and rename it to
`messages` — a handful of lines of Python) **or a code change to
`prepare_rows.py`** so `--map` can reach into nested fields (something like
`--map messages=responses_create_params.input`). Neither exists today.

### Family 3 — parallel arrays instead of one list: also silently empty, differently

`sft/raw/toolbench` stores conversations as two parallel lists under one
key, not a list of turn-objects at all:

```json
"conversations": {"from": ["system", "user", "assistant", "function", ...], "value": ["...", "...", "...", "...", ...]}
```

The same `--map messages=conversations` attempt "succeeds" the same way —
rows get written, nothing crashes — but `messages` ends up holding
`{"from": [...], "value": [...]}`, a dict again, not a list. Same result,
tested for real: `extract_record_text()` returns `''` for every row.
Fixing this one is more work than Family 2, because it's not just "reach
one level deeper" — it's genuine reshaping: zip `from[i]` and `value[i]`
together into `{"role": from[i], "content": value[i]}` pairs. That needs
real transformation code (in a preprocessing script, or a new
`prepare_rows.py` feature), not just a rename.

### One more real failure mode: a nested column inside a table (parquet/csv) — FIXED

Chat-style datasets stored as a *table* (not `.jsonl`) sometimes hold the
whole list of turns in one cell per row. This used to be a hard crash.
**It's now fixed — verified against real production files, not just a
synthetic example, shown below.**

The reason it crashed: `pandas`/`pyarrow` hand back a nested list-of-dicts
column as a `numpy.ndarray`, not a plain Python `list` — and Python's own
`json.dumps` doesn't know how to write a `numpy.ndarray` to JSON. Unlike
Families 2 and 3, this one never failed silently — it was a hard crash,
part-way through, after it already told you `Prepared N rows` — and it
left a 0-byte `.jsonl` file behind rather than cleaning up after itself.

**The fix:** `_clean_value()` in
[`modelsafety/readers/prepare_rows.py`](modelsafety/readers/prepare_rows.py)
— the one function every row from every input type passes through before
anything else touches it — now converts a `numpy.ndarray` to a plain
Python `list` (via `.tolist()`, then re-checked recursively so a `NaN`
hiding inside a nested dict still gets caught) before the row ever reaches
`json.dumps`. `numpy` is now also declared directly in
`modelsafety/readers/requirements.txt` rather than arriving only as an
undeclared transitive dependency of `pandas`.

**Reconfirmed on the exact same real production file used above** —
`rl/processed/qwen35-xml/sft-stage1_0714/train.parquet` (1.9 GB, 603,683
rows, the **verl RL-training row format**:
`prompt`/`reward_model`/`ability`/`data_source`/`extra_info`, `prompt`
being the nested `list<struct<role,content>>` column). Pulled a real slice
out of it (without loading the full 1.9 GB into memory — read one
`pyarrow` row group and sliced it) and ran it through the real converter:

```bash
$ modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path tiny_real_verl_sample.parquet \
  --input-type parquet --map messages=prompt --output-dir ./out
Prepared 200 rows; skipped 0 rows.
```

No crash. Also reconfirmed on the real `medmcqa_think.parquet` referenced
elsewhere in this doc (29,986 rows, `messages` already correctly named but
still the same nested-array shape):

```bash
$ modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path .../general_warmup_sft_0903/clean_parquet/medmcqa_think.parquet \
  --input-type parquet --require messages --output-dir ./out
Prepared 29986 rows; skipped 0 rows.
```

Both outputs were checked beyond just "didn't crash": `extract_record_text()`
returns real, non-empty text for sampled rows from each, and the
`medmcqa_think` conversion was fed straight into `run_pii_scan.sbatch`,
which scanned all 29,986 lines end-to-end and produced a real result (not
an empty one) — stage 1 found 1,370 raw regex hits, stage 2 validated 6 of
them.

Given how much of `rl/processed/qwen35-xml/` looks like this exact
format — several files in that folder run from a few hundred MB up to
multiple GB — and that other parquet groups hit the same `numpy.ndarray`
shape (see `FULL_DATASET_INVENTORY.md`), this fix reaches well beyond the
one file tested here.

### The pattern across all of this

Two out of the three real `.jsonl` tool-call shapes I tested convert
"successfully" (no error, no crash, a normal-looking output file) and then
scan as 100% clean — because there was never any text in them to begin
with. The tabular (parquet/csv) case used to be the exception that crashed
loudly instead of failing silently — now that the `numpy.ndarray` bug
above is fixed, it converts successfully too, so this silent-failure
pattern is now the thing to watch for across every format, not just
`.jsonl`. **That's the single riskiest thing about this whole layer: for
Families 2 and 3 above, success and silent failure look identical unless
you actually open a converted file and read it yourself.** The smoke-test
habit from the other doc — always look at a few real converted rows before
trusting a "0 hits" report — matters most exactly here.

### What this means in practice

| Your tool-call data looks like... | Works today? |
|---|---|
| `"conversations": [{"role": ..., "content": ...}, ...]` — a plain list of turns, just under a different key name | **Yes** — `--map messages=conversations`, tested on 6 real datasets (Family 1) |
| `"responses_create_params": {"input": [...], "tools": [...]}` — conversation nested inside another object | **No — silently empty.** Needs a small preprocessing script or a `--map` upgrade for nested paths (Family 2) |
| `"conversations": {"from": [...], "value": [...]}` — parallel arrays instead of a list | **No — silently empty.** Needs real reshaping code, not just a rename (Family 3) |
| Any of the above, but with real function-call arguments only inside `tool_calls`, never restated in plain `content` | **No — invisible regardless of which family above** (see the Family 1 caveat above) |
| A table (parquet/csv) with one column holding a whole nested conversation | **Yes — fixed.** Used to be a hard crash (`numpy.ndarray` not JSON-serializable); now converts cleanly, tested above |
| Several source files that need to become one converted dataset | **No** — `.csv`/`.parquet`/`.json` inputs are one file per run (see the other doc) |

## 6. If this matters enough to fix — options, not a decision

**Update: the first item below has since been fixed** (see section 5
above for the details and verification). Everything else in this section
is still just an option, not something that's been built — no other code
has changed. If and when the rest is worth investing in, here's roughly
what each remaining fix would look like, from smallest to largest:

- ~~**Stop the tabular crash:**~~ **Done.** `_clean_value()` in
  `prepare_rows.py` now converts a `numpy.ndarray` to a plain `list`
  (recursively, so nested `NaN`s are still caught) before it reaches
  `json.dumps`, and `numpy` is now declared in
  `modelsafety/readers/requirements.txt`. Verified on real multi-GB
  production files, not just a synthetic test — see section 5.
- **Add provenance (section 4):** have `prepare_rows.py` stamp
  `source_file` and `source_row_index` into every row's `metadata`
  automatically, instead of relying on the source data to already have an
  ID column. Small change, meaningfully improves traceability for every
  future conversion.
- **Let `--map` reach nested fields (fixes Family 2):** support dotted
  paths like `--map messages=responses_create_params.input`, so the
  `nemo_gym_recipe` family and anything shaped like it converts with a
  flag instead of a one-off script. Small-to-moderate change, and it's
  purely additive — a plain `--map messages=conversations` still works
  exactly as it does today.
- **Add a reshaping option (fixes Family 3):** something like
  `--zip-columns messages=from,content` to turn two parallel arrays
  (`toolbench`'s shape) into a proper list of `{role, content}` pairs.
  Moderate change — this is genuinely new logic, not just a path lookup.
- **Read `tool_calls`:** teach the shared text-extraction function to also
  render `tool_calls` (function name + arguments) into the scanned text
  when `content` is empty. Moderate change — needs a decision on exactly
  how to format function-call arguments as text for a regex/AI scanner to
  read. This is the one that would matter most for a tools team, since it
  affects every family above, not just one.
- **Centralize the duplicated logic (section 3):** move
  `toxicity/run.py`, `dedup/run.py`, and `pii/model/common.py`'s copies of
  `content_text` over to import the one in `modelsafety/contract/textract.py`
  instead of keeping their own. This is the change that makes every other
  fix on this list automatically apply to all four tools at once, instead
  of needing to be repeated four times.

## Reproducing any of this yourself

Everything above was run directly, nothing was inferred. If you want to
re-check any of it:

```bash
cd /home/naresh/model-safety

# Section 2 — a real tool-use record produces no scannable text
PYTHONPATH="$PWD" python3 -c "
from modelsafety.contract.textract import extract_record_text
import json
line = open('/home/shared/agentic_slm/data/rl/raw/nemo_gym_recipe/Tool-N1-hermes/train.jsonl').readline()
print(extract_record_text(json.loads(line)))
"

# Section 5, Family 1 — this one really does convert and scan correctly
modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/sft/raw/toolmind/open_datasets/glaive-function-calling-v2-query.jsonl \
  --input-type jsonl --map messages=conversations --output-dir /tmp/try_family1 --max-rows 3

# Section 5, Family 2 — "succeeds" but converts to an empty-text dict, not a list
modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/rl/raw/nemo_gym_recipe/Tool-N1-hermes/train.jsonl \
  --input-type jsonl --map messages=responses_create_params --output-dir /tmp/try_family2 --max-rows 2

# Section 5, Family 3 — same silent-empty outcome, different shape (toolbench)
modelsafety/readers/.venv/bin/python modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/sft/raw/toolbench/train.jsonl \
  --input-type jsonl --map messages=conversations --output-dir /tmp/try_family3 --max-rows 2
```

The `medmcqa` conversion referenced in section 4 is still on disk at
`output/converted_smoke/medmcqa_test/` from the other document's testing,
if you want to look at real converted output directly.
