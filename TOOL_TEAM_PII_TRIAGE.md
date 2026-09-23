# Tool team PII triage: genuine vs synthetic, and what to build next

This is the write-up of the "how do we know a PII finding is genuine" discussion,
turned into something we can act on later. Every number below came from
actually running the checks against the real 102,772 regex findings already
sitting in `output/tool_team_pii_run/stage3_extract/pii_extract.csv` — nothing
here is hypothetical.

## The honest answer, up front

**You can prove a finding is fake. You essentially can't prove one is real.**
A synthetic phone number that satisfies NANP formatting rules and a real
person's number are byte-identical in structure — no offline analysis
separates them. Proving "real" needs out-of-band verification (SMTP-probe
the email, HLR-lookup the phone, validate the address against USPS), which
is slow, expensive, and — ironically — means transmitting the exact PII
you're investigating to a third party.

So the question this repo can actually answer isn't *"is this real?"* — it's
**"can we cheaply prove this is fake, and if not, what does it cost to just
scrub it anyway?"** Everything below is organized around that reframing.

## What we measured, in five tiers

| Tier | What it means | How much of our data | Certainty |
|---|---|---|---|
| 1 | Definitionally fake — a reserved range no real value can occupy | Tiny (see below) | **Proven** |
| 2 | Not PII at all — miscategorized | Meaningful (thousands) | **Proven** |
| 3 | Repeated so often it can only be a template | Large | **Very high confidence** |
| 4 | Passes its own checksum but fails a stricter domain rule we don't check yet | Small, but 100% wrong today | **Provable, not yet checked** |
| 5 | Well-formed, appears once, no tell either way | ~Half of distinct values in every category | **Genuinely unknown** |

### Tier 1 — reserved by standard, so provably fake

Standards bodies set aside ranges specifically so test data can't collide
with real people: RFC 2606 (`example.com`, `.test`, `.invalid`), RFC 5737
(documentation IPs), the NANP `555-0100`–`555-0199` fiction block.

```
emails:  140 / 55,778  (0.3%) use an RFC 2606 reserved domain
ips:       0 / 15,383  in the RFC 5737 documentation range
phones:  713 /  5,557  fall in the 555-0100/0199 fiction block
```

Small coverage — most fake data doesn't bother using the *officially*
reserved ranges.

### Tier 2 — not PII at all, just a bad category match

```
ips:     3,467 / 15,383  (23%) are RFC 1918 private/loopback (10.x, 192.168.x, 127.x) -- internal addresses, not personal data
emails:    392 / 55,778  are Go package paths (e.g. "//pkg.go.dev/.../gods/v2@v2.0.0-alpha") -- the regex matched on "@" surrounded by dots
```

This is a real, fixable gap in the regex categories themselves, not a
genuine-vs-fake judgment call.

### Tier 3 — repetition proves template

Nothing real repeats a thousand times in one corpus. Top repeated values,
real numbers:

| category | value | count |
|---|---|---|
| street_addresses | `123 Maple Street,` | 1,113x |
| emails | `example@gmail.com` | 686x |
| emails | `info@peekdomain.com` | 642x |
| emails | `test@gmail.com` | 524x |
| credit_cards | `9637769959178254` | 579x |
| credit_cards | `9637772874219534` | 579x |
| phones | `(250-500-1000` | 343x |
| street_addresses | `1600 Amphitheatre Parkway,` | 201x |

Note `example@gmail.com` / `test@gmail.com` — obviously placeholder, but on
a *real* domain (`gmail.com`), so the Tier 1 reserved-domain check misses
them completely. Repetition is what catches these, not domain matching.

### Tier 4 — fails a check we don't run yet

The two most-repeated credit card numbers (`9637769959178254`,
`9637772874219534` — 1,158 rows combined) **passed Luhn**, which is why they
survived stage 2 validation. But no card network issues a BIN starting with
`9` (Visa = `4`, Mastercard = `51`–`55`/`2221`–`2720`, Amex = `34`/`37`,
Discover = `6011`/`65`). **Luhn proves well-formed, not issued.** Our
[`validator/credit_card.py`](modelsafety/data_screening/pii/regex/validator/credit_card.py)
checks the checksum but never the BIN — this is the one tier where we
already have the data to be certain and simply aren't checking it.

### Tier 5 — the irreducible remainder

```
emails:            48.9% of distinct values are singletons (appear exactly once)
street_addresses:  48.6%
phones:            59.6%
credit_cards:      52.5%
```

A well-formed value with no reserved-range hit, no repetition, and a valid
BIN/checksum is where every technique above runs out. This is the genuine
hard core — and it's a few thousand rows, not 102,772.

## How the industry actually handles this (nobody solves it either)

Four patterns, all sidestepping "is it real" rather than answering it:

1. **Reframe to linkability, not authenticity.** HIPAA Safe Harbor removes
   all 18 identifier *categories* regardless of whether any instance is
   genuine. k-anonymity / differential privacy asks whether a record can be
   re-identified, not whether a field is authentic. This is the only framing
   that's actually decidable.
2. **Risk-tier by consequence.** NIST SP 800-122's approach: auto-dismiss
   Tiers 1–3, human-review only high-severity singletons (SSN, card, MRN),
   reduce the rest to statistics nobody reads individually.
3. **Cost-asymmetric scrubbing.** For *training data* specifically: if
   redacting a value costs nothing in model quality, don't bother resolving
   whether it was real. A missed real SSN in a shipped model is a real
   incident; a redacted fake one costs nothing. Scrub when in doubt.
4. **LLM-as-judge for the ambiguous middle.** The one technique that reads
   *context* instead of format — it can tell a ticket ID inside a tool-call
   demo is fabricated, while the same shape in a real support transcript
   might not be. This repo already has both halves built and neither has
   been run yet:
   - [`modelsafety/data_screening/pii/regex/pii_llm_validator.py`](modelsafety/data_screening/pii/regex/pii_llm_validator.py) — regex pipeline
   - [`modelsafety/data_screening/pii/model/llm_validate.py`](modelsafety/data_screening/pii/model/llm_validate.py) + [`launchers/slurm/run_privacy_filter_llm.sbatch`](launchers/slurm/run_privacy_filter_llm.sbatch) — NER pipeline

## What to build, in priority order

Nothing below is built yet — this is the action list, cheapest and
highest-signal first.

1. **Frequency filter.** Drop any value appearing more than ~20 times
   corpus-wide before it ever reaches a human or an LLM validator. Free,
   and removes the bulk of Tier 3 immediately (our top values repeat into
   the hundreds/thousands).
2. **Category sanity fixes.** Two small, concrete rules: exclude RFC
   1918/loopback ranges from the `ips` category (23% of current IP
   findings), and tighten the email pattern so it doesn't match package
   paths. Removes thousands of non-PII rows for the cost of a few lines.
3. **Add BIN validation to `validator/credit_card.py`.** A short lookup
   table next to the existing Luhn check. Would have caught our top two
   "findings" (1,158 rows) outright, and this is the one tier where we
   already know the fix, not just the problem.
4. **Wire up the LLM validator on whatever survives 1–3.** Steps 1–3 turn
   102,772 rows into a much smaller set where per-item LLM reasoning is
   actually worth the compute — run `pii_llm_validator.py` (regex side) or
   `run_privacy_filter_llm.sbatch` (NER side) against that reduced set
   instead of the raw output.
5. **Consider persisting a confidence tier per finding**, not just a binary
   keep/reject — e.g. add a `tier` column (`reserved_fake` /
   `miscategorized` / `template_repeat` / `failed_bin_check` /
   `unresolved`) to `pii_extract.csv` itself, so this triage doesn't have to
   be re-derived by hand next time. Lowest priority — useful, but only
   valuable once 1–3 exist to populate it.

## One thing worth stating plainly for this specific corpus

Because this is synthetic tool-calling training data, the reasonable
*prior* is that almost everything here is fabricated — that's not
certainty, but it's a legitimate input, and it's a very different starting
point than scanning real support transcripts. The actual risk being
screened for isn't "this corpus is full of real PII" — it's "a handful of
real values leaked into synthetic generation," which is exactly the Tier 5
singleton bucket above, not the corpus as a whole.

## Reproduce every number in this document

```bash
cd /home/naresh/model-safety

python3 -c "
import csv
from collections import Counter, defaultdict

csv.field_size_limit(10**9)
path = 'output/tool_team_pii_run/stage3_extract/pii_extract.csv'
by_cat = defaultdict(Counter)
total = 0
with open(path, newline='', encoding='utf-8') as fh:
    for row in csv.DictReader(fh):
        total += 1
        by_cat[row['category']][row['PII']] += 1

print(f'total findings: {total:,}')
for cat in sorted(by_cat, key=lambda c: -sum(by_cat[c].values())):
    counts = by_cat[cat]
    findings = sum(counts.values())
    distinct = len(counts)
    singles = sum(1 for v in counts.values() if v == 1)
    print(f'{cat:20s} {findings:9,d} findings  {distinct:9,d} distinct  {100*singles/distinct:5.1f}% singleton')
"
```

See the chat history for the exact commands used for the Tier 1/2/4
breakdowns (RFC-reserved ranges, private-IP counts, BIN-mismatch check,
top-repeated-value listing per category) — they're straightforward
extensions of the script above, filtering `by_cat` by pattern instead of
just counting repetition.
