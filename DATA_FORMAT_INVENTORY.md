# Every input data format in `/home/shared/agentic_slm/data/`, and what each one needs

This is a research document only. **Nothing here builds an adapter — it's a
complete map of what adapters we'd eventually need, and why**, so we have one
place to look before deciding what to build. Same rule as the other two docs:
every number and every example below came from actually reading the real
files, not from guessing.

This is a companion to
[`HOW_TO_SCAN_YOUR_DATA.md`](HOW_TO_SCAN_YOUR_DATA.md) (file types, how to
run the scanners) and
[`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md) (why 3 sample
formats needed adapters). This document finishes that job: instead of a
handful of samples, it covers **all 153 `.jsonl` files** in the shared
folder, not just the ones we happened to check by hand before — so nothing
is left unaccounted for.

## The output format we're aiming for (recap)

Every adapter's job is to produce one of these two shapes — this hasn't
changed from the other docs, repeating it here so this document stands on
its own:

```json
{"text": "a plain string"}
```

```json
{"messages": [{"role": "user", "content": "a plain string"}, {"role": "assistant", "content": "a plain string"}]}
```

`role` can be anything (`user`, `assistant`, `system`, `tool`, ...); `content`
must end up as a plain string. That's it — that's the entire target. Every
format below is judged against "does it already look like this, or what
would it take to make it look like this."

## How this inventory was built (and how to rebuild it yourself)

Every `.jsonl` file's first line was parsed, and files were grouped by their
top-level key names. This is the exact command (also given at the end of
[`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md), repeated here
since it's the main tool for this document):

```bash
python3 -c "
import json
from pathlib import Path
from collections import Counter

root = Path('/home/shared/agentic_slm/data')
signatures = Counter()
examples = {}

for path in sorted(root.rglob('*.jsonl')):
    try:
        with path.open('r', encoding='utf-8') as f:
            first_line = f.readline()
        keys = tuple(sorted(json.loads(first_line).keys()))
    except Exception as exc:
        keys = (f'ERROR: {exc}',)
    signatures[keys] += 1
    examples.setdefault(keys, str(path))

for keys, count in signatures.most_common():
    print(f'{count:3d} files  {keys}')
    print(f'         e.g. {examples[keys]}')
"
```

This found **~30 distinct raw key combinations**. Most of those turned out to
be the same underlying format wearing different amounts of extra metadata —
the table below groups them into **9 real families** you'd actually need to
design for. All 153 files are accounted for across these 9 families — the
counts add up exactly, nothing was left out.

One honest limit: this only checks each file's *first* line. It's a fast,
cheap signal, not a guarantee every row in a file matches. Good enough for
this kind of inventory; worth remembering if you build on top of it.

## The 9 families, at a glance

| # | Family | Files | Already matches the target? | What it would need |
|---|---|---|---|---|
| 1 | Plain `{"text": ...}` | 36 | **Yes** | Nothing |
| 2 | `{"messages": [...]}` with plain string content throughout | 4 | **Yes** (mostly — see the tool-calls warning below) | Nothing, but verify per-file |
| 3 | `{"conversations": [{"role", "content"}], "tools": [...]}` | 8 | Same shape, different key name | Rename only — `--map messages=conversations` already works today |
| 4 | `{"responses_create_params": {"input": [...], "tools": [...], ...}, ...lots of other metadata...}` | **84** | No — nested one level down | New adapter: pull out `responses_create_params.input` |
| 5 | `{"conversations": {"from": [...], "value": [...]}, "id": ...}` | 3 | No — parallel arrays, not a list | New adapter: zip `from[i]`/`value[i]` into `{role, content}` pairs |
| 6 | `{"id", "description", "user_scenario": {...long nested text...}, "evaluation_criteria", "db_path"}` | 1 | No — not a conversation at all | Bespoke adapter: pull specific nested string fields |
| 7 | `{"question": "...", "answers": {"direct": ..., "tool_call": ...}, ...}` (eval/QA shape) | 2 | No — no `messages` concept | `--map text=question` covers half of it; `answers` needs separate handling |
| 8 | `{"messages": [prompt only], "chosen_response": {...}, "rejected_response": {...}}` | 2 | Partially — misses the actual reply | Adapter needs to append `chosen_response`/`rejected_response` onto `messages` |
| 9 | `removed_rows.jsonl` decontamination logs | 11 | Mixed — see below | Not really "data," a reporting artifact — decide if it's in scope at all |

**36 + 4 + 8 + 84 + 3 + 1 + 2 + 2 + 11 = 151**, plus **1 file with its own
niche one-off shape** and **1 file that's completely empty** = **153**, every
file in the shared folder accounted for. Details on both stragglers are in
their own section below.

The single biggest thing to notice: **family 4 alone is 84 of the 153
files — well over half of everything.** One adapter, built once, would cover
more files than everything else in this table combined.

---

## Family 1 — plain `text` — already works, no adapter

```json
{"text": "..."}
```

**Where:** 36 files, all under `mid/_megatron/full/jsonl/` and
`mid/_megatron/smoke/jsonl/` (pretokenization staging data — code, web text,
etc., already flattened to plain strings before this stage).

**Verify it yourself:**
```bash
head -n 1 /home/shared/agentic_slm/data/mid/_megatron/full/jsonl/code_000.jsonl | python3 -m json.tool
```

Nothing to build here. This is what a "clean" file looks like.

## Family 2 — plain `messages`, real content strings — already works (with a caveat)

**Where:** `rl/raw/areal_tau2_sft/tau2_sft_train.jsonl` (and its duplicate
copy under `sft/raw/areal_tau2/`), and
`sft/raw/ultradata_sft_2605/sampled_v1/{think,nothink}.jsonl` — 4 files.

Real example from `areal_tau2_sft` — this one is a tau2-bench customer
service simulation, and it already includes a `tool` role message with
readable content:

```json
{"role": "tool", "name": "find_user_id_by_email", "content": "ethan_wright_7882"}
```

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/rl/raw/areal_tau2_sft/tau2_sft_train.jsonl').readline())
print([m.get('role') for m in r['messages']])
"
```

**The caveat:** this is a tool-use benchmark, so some `assistant` turns in
this file are very likely to also carry a `tool_calls` block, the same way
the real example near the end of this document shows for a different
dataset. I didn't confirm one in the specific row I sampled, but I'd verify
this per-file with the same check shown in that section before trusting a
"0 hits" report on this dataset.

## Family 3 — `conversations` instead of `messages`, otherwise identical shape

```json
{"conversations": [{"role": "user", "content": "..."}, ...], "tools": [...]}
```

**Where:** 8 files — the `toolmind/open_datasets/` family (glaive, xlam,
ToolACE, BUTTONInstruct, tau-train, APIGen-MT — already covered in the other
doc) plus `toolmind/graph_syn_datasets/graphsyn.jsonl`, which turns out to
share the exact same shape:

```json
{"role": "user", "content": "What's the current time in New York?"}
```

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/sft/raw/toolmind/graph_syn_datasets/graphsyn.jsonl').readline())
print(r['conversations'][0])
"
```

**What it needs:** nothing new — `--map messages=conversations`, already
proven to work in the other doc.

## Family 4 — `responses_create_params` — the big one, 84 of 153 files

This is the OpenAI **Responses API** request shape, nested one level inside
another object:

```json
{
  "responses_create_params": {
    "input": [{"role": "user", "content": "...", "type": "message"}],
    "tools": [...],
    "tool_choice": "auto",
    "...": "..."
  },
  "agent_ref": {...},
  "...": "many other metadata fields, different per dataset"
}
```

**Where:** the entire `rl/raw/nemo_gym_recipe/` family — `Tool-N1-hermes`,
every `Nemotron-RL-Agentic-*` dataset, `Nemotron-RL-instruction_following*`,
`Nemotron-RL-knowledge-mcqa`, `Nemotron-RL-agent-workplace_assistant`, and
the entire `_mix/` folder of blended training sets (~30 files there alone).

The raw key-scan found **19 different top-level signatures** in this family
— they range from the simple `Tool-N1-hermes` shape
(`agent_ref, expected_actions, responses_create_params`) up to a 29-field
"distractor" variant used for structured-output training
(`schema_str, tool_name_style, distractor_style, num_distractors, ...`). I
checked one of the most complex ones directly to confirm the core shape
holds regardless of how much extra metadata surrounds it:

```bash
$ python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/rl/raw/nemo_gym_recipe/_mix/phase1b_late_0617b/train.jsonl').readline())
print(list(r['responses_create_params'].keys()))
"
['background', 'include', 'input', 'instructions', 'max_output_tokens', 'max_tool_calls',
 'metadata', 'model', 'parallel_tool_calls', 'previous_response_id', 'prompt', 'reasoning',
 'service_tier', 'store', 'temperature', 'text', 'tool_choice', 'tools', 'top_logprobs',
 'top_p', 'truncation', 'user', 'stream']
```

Same `input` list in every variant checked — this really is one format with
19 different amounts of extra metadata bolted on, not 19 different formats.

**Verify it yourself (any file in this family):**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/rl/raw/nemo_gym_recipe/Tool-N1-hermes/train.jsonl').readline())
print(r['responses_create_params']['input'])
"
```

**What it needs:** one adapter that pulls `row['responses_create_params']['input']`
out and renames it to `messages` — covered in
[`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md) section 5 as
"Family 2." Given this is 84 of 153 files, this is the single highest-value
adapter to build.

## Family 5 — `conversations` as parallel arrays (ShareGPT-style)

```json
{"conversations": {"from": [...], "value": [...]}, "id": "..."}
```

**Where:** `sft/raw/toolbench/{train,validation,toolbench_all}.jsonl` — 3
files. This is the format released by the
[ToolLLM paper](https://arxiv.org/abs/2307.16789) (see the paper
recommendations from earlier in this chat) — worth knowing that public
mirrors of the *same* dataset sometimes already convert this into a clean
list of `{role, content}` dicts; the copy in our shared folder is still in
this older parallel-array shape.

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/sft/raw/toolbench/train.jsonl').readline())
print(r['conversations']['from'])
print(r['conversations']['value'][0][:200])
"
```

**What it needs:** real reshaping code (zip `from[i]` with `value[i]`), not
just a rename — covered as "Family 3" in the other doc.

## Family 6 — a scenario definition, not a conversation at all

```json
{
  "id": "airline_1",
  "description": {"purpose": "..."},
  "user_scenario": {"instructions": {"task_instructions": "...long text..."}},
  "evaluation_criteria": {...},
  "db_path": "..."
}
```

**Where:** `areal_tau2/tau2_rl_train.jsonl` — 1 file today, but this is a
tau2-bench RL *environment scenario* definition (the setup a simulated user
follows), not a chat transcript. Worth watching for if more tau2-style RL
data gets added later.

This one matters because the nested text is exactly the kind of thing PII
scanning exists for — it's synthetic, but shaped like real customer data:

```json
"task_instructions": "YOUR GOAL: You want to change the flight date on
reservation HKEG34 to the best available nonstop option...\nMia Li, a Gold
member from Austin, has a one-way business reservation HKEG34 from Denver
to Las Vegas on May 27...\n"
```

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/areal_tau2/tau2_rl_train.jsonl').readline())
print(r['user_scenario']['instructions']['task_instructions'][:400])
"
```

**What it needs:** a bespoke adapter — there's no `messages`/`conversations`
concept to lean on here, it's just "go find this one specific nested string
field and treat it as `text`."

## Family 7 — question/answers eval format, no `messages`

```json
{
  "question": "...",
  "correct_answer": "cannot_answer",
  "answers": {"direct": "...", "tool_call": "..."},
  "tools": [...], "orig_tools": [...]
}
```

**Where:** `sft/raw/when2call/test/{when2call_test_llm_judge,when2call_test_mcq}.jsonl`
— 2 files. This is an evaluation-benchmark shape (BFCL-derived, per the
`source` field inside), not training conversation data.

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/sft/raw/when2call/test/when2call_test_mcq.jsonl').readline())
print(r['question'])
print(r['answers'])
"
```

**What it needs:** `--map text=question` covers the question text alone
(that already works with the existing converter, no new code). The
`answers` dict is a different shape again — it's not a list, it's two
named fields (`direct`, `tool_call`) — if you want those scanned too,
that's a second small piece of adapter logic, since `--map` can only grab
one field, not merge two.

## Family 8 — `messages` that's missing the actual reply

```json
{
  "messages": [{"role": "user", "content": "..."}],
  "tools": [...],
  "chosen_response": {"role": "assistant", "content": "..."},
  "rejected_response": {"role": "assistant", "content": "..."}
}
```

**Where:** `sft/raw/when2call/train/when2call_train_pref.jsonl` — 1 file (a
preference-pair / DPO-style training file). A sibling file,
`when2call_train_sft.jsonl`, has the plain `(messages, tools)` shape — worth
checking whether its `messages` array is the full conversation or also just
the prompt, since this file's convention is clearly "the reply isn't always
inside `messages`."

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/sft/raw/when2call/train/when2call_train_pref.jsonl').readline())
print('messages:', r['messages'])
print('chosen_response:', r['chosen_response'])
"
```

Real output:
```
messages: [{'role': 'user', 'content': 'Show me completed ICOs in the e-commerce and finance sectors...'}]
chosen_response: {'role': 'assistant', 'content': '<TOOLCALL>[{"name": "get_ico_calendar", "arguments": {...}}]'}
```

Interesting detail: this dataset represents a tool call as literal text
inside `content` (`<TOOLCALL>[...]`) rather than a separate `tool_calls`
field — which actually means it **would** get scanned correctly as plain
text, unlike the OpenAI-style `tool_calls` field described below. It's a
third convention, different from both other tool-calling styles found in
this data.

**What it needs:** `extract_record_text()` only ever looks at `messages` —
it has no idea `chosen_response`/`rejected_response` exist as sibling
fields, so today it would only see the user's opening line and miss the
entire actual response. An adapter here means appending
`chosen_response` (and/or `rejected_response`, depending on which one
you're screening) onto the end of `messages` before scanning.

## Family 9 — `removed_rows.jsonl`: decontamination logs, not training data

**Where:** 11 files, all named `removed_rows.jsonl`, all inside
`.../decontaminated/...` or `.../train_shards_keep/...` folders — these are
reports written by the dedup/decontamination step, listing which rows got
removed and why, not data meant for training or scanning in the normal
sense.

Two sub-shapes, and the difference matters:

**7 files carry real text** (`bench_text`, `train_text` — the actual
matched excerpt from each side of the comparison):
```json
{"bench": "mmlu_pro", "rule": "R1", "detail": "2 shared 10-grams",
 "train_text": "I want to check the last few lines of my raw_corpus.txt...",
 "bench_text": "..."}
```

**4 files carry no text at all** — just bookkeeping (which row, which
benchmark, which rule matched):
```json
{"source": "general_if_50k", "row": 764, "bench": "mmlu_pro", "item": "6668",
 "rule": "R1", "detail": "1 shared 10-grams"}
```

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/envfactory-xml/removed_rows.jsonl').readline())
print(list(r.keys()))
"
```

**What it needs:** a decision, not really an adapter — is a decontamination
audit log in scope for PII/toxicity screening at all? If yes (the 7 files
with real text could technically carry real PII, since they're excerpts of
real training rows), `--map text=train_text` would work for those
specifically; the other 4 have nothing to scan regardless.

## The two stragglers (bringing the count to 153)

**One niche one-off** — `rl/processed/qwen35-xml/decontaminated/meddecoy_tooldecoy_only_s42/sample.jsonl`
has a much larger shape (`tools, conversations, answer, raw_system,
data_source, prompt, ability, reward_model, extra_info, task_id, split`)
that looks like an RL-verifier training format (the `ability`/`reward_model`/
`extra_info` fields are a recognizable pattern from RL training frameworks).
In the row I checked, `conversations` was actually an **empty list** — the
real content lives in `prompt` instead, itself a list of dicts. Only one
file has this exact shape today, so I'm flagging it rather than building a
whole family entry around one file — if more data like this shows up, it
needs its own look.

**One genuinely empty file** —
`rl/processed/qwen35-xml/decontaminated/ef_interactive_es_mix_1to1/removed_rows.jsonl`
is exactly **0 bytes**. This is the file that showed up as `ERROR` in the
signature scan (`json.loads('')` fails immediately, since there's no JSON to
parse). Worth confirming this is the harmless case, not the dangerous one
from the other docs: an actually-empty file produces zero chunks and the
scanner quietly skips it — no crash. A file that has some content but
*invalid* JSON (a truncated line, stray bytes) is the case that crashes the
whole scan; a totally empty file just does nothing, safely.

## The one thing that shows up across several families: `tool_calls` is real, not hypothetical

The other doc built a synthetic example of PII hiding inside a `tool_calls`
block. Here's the same thing, confirmed in **real production data** in this
exact shared folder — `sft/raw/nemotron_agentic_v2/data/tool_calling.jsonl`,
which already matches the `messages` shape (Family 2) with no conversion
needed at all:

```
turn 2: role='assistant' content='' has_tool_calls=True
   tool_calls: [{"function": {"name": "get_artwork_details", "arguments": "{\"artwork_id\": \"ART-9955\"}"}}]
turn 5: role='user' content='Sure, here's my email: prop.master@filmstudio.com...'
turn 6: role='assistant' content='' has_tool_calls=True
   tool_calls: [{"function": {"name": "send_verification_code", "arguments": "{\"contact_info\": \"prop.master@filmstudio.com\", ...}"}}]
```

The email address is scanned successfully where the **user** typed it
directly (that content string isn't empty). But every time the
**assistant** turn is empty `content` + a `tool_calls` block instead — which
happens 4 times in this one conversation — nothing in that turn gets
scanned at all, regardless of what the arguments contain. This file already
passes the shape check (it has real `messages` with real string content on
most turns), so it would never show up as a conversion failure — it would
just quietly under-scan on every tool-call turn, forever, unless something
is built to read `tool_calls` specifically.

**Verify it yourself:**
```bash
python3 -c "
import json
r = json.loads(open('/home/shared/agentic_slm/data/sft/raw/nemotron_agentic_v2/data/tool_calling.jsonl').readline())
for m in r['messages']:
    print(m.get('role'), repr(m.get('content'))[:60], 'tool_calls' in m)
"
```

## Summary: what to actually prioritize, based on real file counts

| Adapter | Files it unlocks | Effort (from the other doc) |
|---|---|---|
| `responses_create_params.input` extraction | **84** | Small–moderate: nested-path support |
| `tool_calls` rendering into scannable text | Affects turns *inside* many of the families above, not a fixed file count | Moderate: needs a text-formatting decision |
| ShareGPT `from`/`value` reshaping | 3 | Moderate: real reshaping logic |
| Preference-pair reply stitching (`chosen_response`) | 1 (today) | Small: append one field to `messages` |
| Bespoke scenario-field extraction | 1 (today) | Small, but bespoke per shape |
| Eval `answers` dict handling | 2 | Small, bespoke |

Nothing here has been built — this table is purely "if you built things in
order of how many real files they unlock today, this is that order,"
matching the 84-file `responses_create_params` family being worth building
before anything else.
