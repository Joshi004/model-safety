# Open questions: PII validator, BIOES, the toxicity policy, and dedup's place here

*Written in response to four questions raised while reading the ported code.
Each section restates the question, then answers it against what the code
actually does — I read the full files before writing any of this, not just
the docstrings. The two design questions (toxicity policy, dedup) end with a
recommendation, clearly marked as opinion rather than fact.*

---

## 1. What does `pii_validator.py` add on top of `pii_scanner_fast.py`? Why would it be slower? Why is it more effective?

### What stage 1 (`pii_scanner_fast.py`) actually does

It touches every byte of the corpus. For every JSONL file, it walks every
line, decodes it, extracts the scannable text, and runs all nine PII regexes
against it with `pattern.findall(text)`:

```python
for category, pattern in PII_REGEXES.items():
    pii_items = pattern.findall(text)
    if pii_items:
        ...
        output_files[category].write(f"{rel_path}|{current_line}|{source_sha256}|{normalized_item}\n")
```

The parallelism is real but it's parallelism *over the corpus*: the file
pre-scans everything into fixed-size line chunks (`PII_CHUNK_SIZE`, default
10,000 lines), hands chunks round-robin to a worker pool (`PII_NUM_WORKERS`,
default 100), and each worker independently re-opens its slice of the file
and regex-scans it. Total work is `O(corpus size × 9 regexes)`. This is
correctly the fast stage — it's a compiled-regex pattern match, nothing else.

### What stage 2 (`pii_validator.py`) adds

It never looks at the corpus again. It reads only the *hit files* stage 1
already wrote — one line per candidate, `path|line|sha256|value` — and for
each one calls a per-category function that either accepts or rejects it:

| Category | What the stage-2 check actually verifies |
|---|---|
| `credit_cards` | Luhn checksum, and rejects a hardcoded set of well-known test numbers (`4111111111111111`, etc.) |
| `btc_addresses` | Real Base58Check or Bech32/Bech32m checksum verification — this is cryptographic math, not a heuristic |
| `emails` | Local-part/domain syntax, and rejects reserved example domains (`example.com`, `example.org`, `localhost`) |
| `ips` / `ipv6s` | Excludes RFC 5737/documentation address ranges |
| `phones` / `phones_with_exts` / `ukphones` | Excludes toll-free area codes and obvious placeholders (`555`-style, repeated digits) |
| `po_boxes` | A tighter regex than stage 1's, requiring an actual PO-box-shaped string |
| `street_addresses` | Requires a real street-suffix word (avenue, blvd, drive, ...) be present |

So stage 2 is a pure filter: it can only shrink the candidate set stage 1
produced, never grow it. It's also parallelized (same `Pool` pattern, same
`PII_NUM_WORKERS`), just over hit-file lines instead of corpus lines.

### Is it actually slower?

Based on reading the code — not a profiled run, I want to be upfront about
that — I don't think it structurally should be. Stage 1's working set is the
whole corpus; stage 2's working set is only the candidates stage 1 flagged,
which by construction is far smaller. Stage 2 does no file I/O against the
original JSONL at all, and every validator function is a cheap, bounded
operation (a checksum, a set-membership check, a regex against a short
string) — nothing here scales with corpus size the way stage 1 does.

If stage 2 is *observed* to take a while in practice, my best guess is one of
two things, not that heuristic checking is inherently expensive:

- **It's sequential, not parallel-in-time, with stage 1.** It can't start
  until stage 1's hit files exist, so its latency gets added to stage 1's on
  a run's total clock time even though its own per-unit cost is small.
- **A specific category produced an unusually large raw hit volume.** If a
  loosely-written regex (say, phone numbers) fires on a lot of noise in a
  particular corpus, stage 2 has more rows to process for that category —
  still cheap per-row, but the row count could be large.

Worth naming separately: the pipeline's actual expensive step is stage 4a
(`pii_llm_validator.py`), which makes real LLM calls per candidate. That's
where wall-clock cost genuinely jumps, for a reason that has nothing to do
with stage 2 — network/inference latency versus a checksum. If the "stage 2
feels slow" observation is really about the pipeline feeling slow after
stage 1, stage 4a is the more likely culprit worth checking first.

### Why it's more effective

"Effective" here means precision, not recall. Stage 1 is deliberately a
high-recall, low-precision pass — regexes can only match *shape*, so a
credit-card regex matches any 16-digit run, an email regex matches any
`x@y.z`, an IP regex matches any dotted quad. All three will happily match
test fixtures, placeholder data, and syntactically-valid-but-meaningless
strings. Stage 2 is where the pipeline actually proves or disproves each
candidate using domain-specific rules — and for two categories (Bitcoin,
credit card) that proof is genuine algorithmic verification (a real checksum
), not a guess. This is the same "cheap high-recall screen, then a stricter
pass that removes false positives" shape the toxicity scan and the model-
based PII pipeline both use elsewhere in this repo — it's a repeated pattern
here, not a one-off.

---

## 2. What is `bioes.py` doing, and why does it matter?

This file only matters to the *model-based* PII pipeline
(`data_screening/pii/model/`) — the regex pipeline never touches it. It's
the piece that turns a token classifier's raw output into usable findings,
so it's worth walking the whole path from `backends.py` through to a `Span`.

### The problem it solves

`TransformersBackend`/`VllmBackend` run a token-classification model
(`OpenMed/privacy-filter-nemotron-v2`) over the text. A token classifier
doesn't say "there's a person's name at characters 40–52" directly — it
outputs, for *every individual token*, a score for every label in its
vocabulary (things like `O`, `B-PERSON`, `I-PERSON`, `E-PERSON`, `S-PERSON`,
`B-EMAIL`, and so on — one `B`/`I`/`E`/`S` set per category, plus one shared
`O` meaning "not part of any entity"). That's the **BIOES** scheme: **B**egin,
**I**nside, **E**nd, **S**ingle-token entity, **O**utside.

Turning "a score for every label, on every token" into "here are the entity
spans" is not just "take the highest-scoring label per token." If you did
that naively you'd get illegal sequences all the time — an `I-PERSON` with
no `B-PERSON` before it, or a `B-PERSON` immediately followed by `E-EMAIL`.
Nothing stops a token classifier's raw per-token argmax from producing
nonsense like that, because each token's prediction is scored independently.

### What each function actually does

- **`viterbi_decode`** finds the single highest-total-score *sequence* of
  tags across the whole window that also obeys BIOES's legality rules (a
  span must open with `B` or be a lone `S`, continue with `I` of the same
  category, and close with `E` of that same category — categories can't mix
  mid-span). It's a real Viterbi decode, with the docstring's claimed
  `O(tokens × labels)` cost coming from a vectorised trick: from any "closed"
  state (`O`, or an `E-`/`S-` of some category) you can only transition to a
  `B`/`S` of some category or back to `O`, so the code computes that one
  `max` as a numpy op across all closed states at once, instead of a full
  transition matrix multiply.
- **`spans_from_path`** walks the resulting legal tag path and converts each
  `S-` token or each `B…E` run into a `Span(label, start, end, text,
  confidence)`, mapping token positions back to *character* offsets in the
  original text (via the tokenizer's offset mapping) and trimming
  surrounding whitespace. Confidence is the token's own probability for a
  single-token span, or the mean probability across the run for a multi-
  token span.
- **`merge_window_spans`** exists because long documents get split into
  overlapping windows before being tokenized (`_make_windows` in
  `backends.py`, 4096 tokens per window with a 256-token overlap by default —
  the overlap exists specifically so an entity sitting near a window
  boundary isn't invisibly cut in half). That overlap means the same
  real-world entity can get predicted twice, once in each of two adjacent
  windows. This function sorts spans and, for same-label spans that overlap
  by at least 50% of the shorter one's length, keeps only the higher-
  confidence prediction.

### Why this matters beyond "it's plumbing"

Two things:

1. **It's the only reason the model's output is usable at all.** `policy.py`'s
   tiered confidence thresholds operate on `Span` objects with a label, a
   location, and a confidence number — there's no clean way to get from "a
   grid of per-token label scores" to that without correct constrained
   decoding. Concretely, in the current `policy.json`: direct identifiers
   like `email` (0.5) or `ssn` (0.8) need a confidence over their threshold
   to count as reportable, while the `contextual` tier — age, city,
   occupation, and similar (`CONTEXTUAL_LABELS` in `common.py`) — has a
   `null` tier threshold and no per-label override for any of them, so
   `policy.threshold_for` falls through to `null` and `evaluate()` rejects
   them outright (`"contextual_tier"`) regardless of confidence. That's a
   property of this policy file's current configuration, not a hard rule in
   the code — `label_thresholds` is checked before the tier default, so a
   future policy version could assign a contextual label its own numeric
   threshold if that were ever wanted.
2. **It's why the model-based pipeline exists at all, alongside the regex
   one.** Regex can only find PII with a fixed, describable *shape* — a
   name has no such shape. "John reached out about his diagnosis" contains
   a person's name that no regex will ever catch, because there's nothing
   syntactically distinctive about it; you need a model that's learned
   *context* to find it. `bioes.py` is the piece of engineering that makes
   that context-aware detection produce the same kind of structured finding
   (a location, a category, a confidence) that the regex side gets for free
   from a pattern match. It's the model pipeline's equivalent of what
   `pii_output.py`/`pii_extractor.py` are for the regex pipeline — the part
   that turns raw signal into a trustworthy, reportable decision.

---

## 3. The toxicity judge prompt: one generic prompt, or one per domain? And why "non-clinical erotica" rather than just "erotica"?

### Generic vs. per-domain

I don't think this is actually an either/or, and I think the repo's own
layout already answers most of it: the file lives at
`toxicity/policies/medpsy_medical_sexual_v1.txt` — under a `policies/`
directory, versioned (`_v1`), and named for its domain
(`medpsy_medical_sexual`) rather than hardcoded into `llm_check_guard.py`.
That's already the "policy is config, not code" shape. The open question
isn't whether to have per-domain policies — that's the design already in
place — it's whether each new domain gets a hand-written prompt from
scratch, or an instance of a shared template.

I'd push for the template. Two failure modes, one on each side:

- **One universal prompt** either ends up too permissive for a domain that
  needs sharper judgment, or stays coupled to medical framing ("removed from
  the general medical set," "isolated into a dedicated safety-training set")
  in a way that reads oddly applied to, say, agentic tool-call traces.
- **Fully independent, hand-written prompts per domain** drift. The parts of
  this policy that aren't about medicine at all — judge the flagged message
  using the conversation only for context; separate intent from topic;
  route high-risk material to a dedicated safety/refusal set rather than
  deleting it; don't treat Qwen3Guard or Detoxify's own labels as ground
  truth — are decision-framework choices that every domain's policy should
  probably make the same way. If each domain's prompt is written
  independently, there's nothing stopping one of them from quietly losing
  the "route, don't delete" instruction on a rewrite.

So concretely: factor the shared decision framework (the parts above) into
one template, and let each domain supply two things — the list of what
*disqualifies* content, and the list of what should explicitly **not** be
treated as disqualifying on its own (here, that's the clinical-vocabulary
exemption in lines 19–26). That's a small change from what exists today —
mostly just naming the template explicitly instead of leaving it implicit in
one file — and it means the next domain's policy is a short diff, not a
blank page.

### Why "non-clinical erotica" and not just "erotica"

I read this as a real, deliberate qualifier, not a euphemism. The whole
document's second half (lines 19–26) exists to say: don't flag content
merely for containing clinical vocabulary about sexual function, anatomy,
reproduction, STIs, or abuse history. A medical corpus is *supposed* to be
full of exactly that content. So the actual thing this policy is trying to
separate is **intent** (is this meant to arouse/entertain, or to
inform/treat/educate) from **topic** (does it mention sex at all). If line 7
just said "erotica," a judge applying it without the later exemption clause
in mind could plausibly start flagging legitimate clinical sexual-health
discussion for "being about sex" — precisely the failure the rest of the
document is written to prevent. "Non-clinical" is the word doing that
separation.

I also think there's a reason it's attached to "erotica" specifically and
not, say, "pornography": erotica is a genre label, and it has a legitimate
adjacent clinical use — a sex therapist's transcript can reference erotica
as a discussion topic, or even a bibliotherapy recommendation, without the
transcript itself being disqualifying content. "Pornography," "fetish
narrative," and "sexual solicitation" don't really have an equivalent
legitimate-clinical-production case inside a text QA/SFT dataset, so there's
less ambiguity to resolve for those terms.

One honest caveat: as written, the sentence is grammatically ambiguous about
whether "non-clinical" scopes just "erotica" or the whole comma-separated
list. I'm fairly confident the *intent* is the latter — the entire second
half of the document is about intent-vs-topic in general, not specifically
about erotica — but if this file gets revised, I'd tighten the wording
rather than change the underlying idea, something like restructuring it as
"the following, when the primary intent is non-clinical: erotica,
pornography, fetish narrative, ...". So: I don't think the fix here is
"just say erotic data" — that would reintroduce the exact over-flagging risk
lines 19–26 exist to prevent — but the sentence could say what it means more
clearly.

---

## 4. Should deduplication be part of a repo scoped to model safety and data safety?

### What it actually does here

Confirmed against `dedup/run.py` and its README: MinHash/LSH near-duplicate
detection over a JSONL tree, plus a "cross mode" that treats one dataset as
a fixed reference and filters a second dataset against it. That cross mode
is explicitly documented as the test-set decontamination workflow — point it
at benchmark/eval data as the reference and training data as the candidate,
and it reports (and can remove) training rows that match eval rows.

### The case for keeping it here

1. **Contamination checking protects the validity of everything else this
   repo measures.** If benchmark rows have leaked into training data, a
   refusal rate or a red-team pass rate this repo reports later could just
   be the model having memorized the eval, not actually generalizing safety
   behavior. That's not a generic "data hygiene, nice to have" argument —
   it's closer to "the thing that makes the rest of this repo's numbers
   trustworthy."
2. **Near-duplicate rows are exactly the rows most likely to be
   memorized verbatim** — repetition is the strongest known driver of
   training-data memorization. This repo already owns a memorization *test*
   (`model_screening/regurgitation/pii_regurgitation_check.py`). Dedup is
   the preventive half of that same concern: one reduces the raw material
   that causes memorization, the other checks whether it happened anyway.
   They're naturally one capability area split across "before training" and
   "after training," not two unrelated tools that happen to sit near each
   other.
3. **It costs nothing architecturally to keep here.** It already reuses the
   exact same shared shape as PII and toxicity — the `text`/`messages`
   contract, hash-bound source-line provenance, a content-addressed
   manifest verified before reading, the same SLURM execution pattern, a
   byte-preserving partition writer. Splitting it into a separate repo would
   mean either duplicating that infrastructure or coupling two repos
   together — exactly the fragmentation `unification-plan.md` was written to
   avoid.

### The honest counter-argument

PII and toxicity are harm-prevention checks: something is either exposed or
it isn't, either toxic or it isn't. Dedup and contamination checking are a
data-quality/statistics concern — nothing about a duplicate row is itself
harmful. If "model safety" is read narrowly, dedup is adjacent to this
repo's charter, not a member of it. There's also a real scope-creep risk
worth naming: `unified-screening-repo.md` explicitly warns against building
a platform that absorbs everything. If dedup counts because it's
data-quality tooling that happens to share infrastructure, does a generic
fluency filter or a language-ID filter count too, on the same reasoning?

### Where I land

Keep it, but I'd make the justification explicit rather than the current
framing in `unified-screening-repo.md`, which lists it almost as an
afterthought ("a related but separate capability worth remembering even
though it's not one of the seven checks"). I'd state the test for inclusion
as the two concrete safety linkages above — does it protect eval validity,
or does it reduce a training-data precondition for a harm this repo already
tests for — rather than "it lived next to PII and toxicity in medpsy so it
came along." That test is specific enough to correctly keep dedup in and
correctly keep a generic fluency/quality filter out, which "shares
infrastructure with the other scanners" alone would not do.
