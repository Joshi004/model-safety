# How do we know a conversion actually worked? Every option, and which ones are worth doing

This is a decision document. **Nothing here is built yet** — it's a map of the
validation options we have, what each one really buys us, what each one costs,
and a recommendation at the end so we can stop re-arguing it.

Same rule as the other docs in this folder: every number below was measured on
the real data on this machine, not estimated. Where something is a rough
estimate or an opinion, it says so.

This sits alongside
[`HOW_TO_SCAN_YOUR_DATA.md`](HOW_TO_SCAN_YOUR_DATA.md) (how to run the
scanners), [`HOW_DATA_CONVERSION_WORKS.md`](HOW_DATA_CONVERSION_WORKS.md) (how
the converter works and where it falls short),
[`DATA_FORMAT_INVENTORY.md`](DATA_FORMAT_INVENTORY.md) and
[`FULL_DATASET_INVENTORY.md`](FULL_DATASET_INVENTORY.md) (what we have).

**Scope note:** this document is about *validating* a conversion — "did the
data make it across intact and usable." It deliberately does **not** cover
tracing a finding back to its original source row; that's a separate question
parked in `HOW_DATA_CONVERSION_WORKS.md` section 4. The one place the two
touch is mentioned once, in the dead-letter option, and left there.

## The problem, on one screen

Here are two real conversions run on real files from
`/home/shared/agentic_slm/data/`. The converter's own output is identical in
spirit — both say the job went fine:

```bash
# Conversion A -- an arrow folder of agentic traces
$ prepare_rows.py --input-path .../distill/envgen_solo_v1 \
    --input-type huggingface --require messages --output-dir ./out
Prepared 999 rows; skipped 0 rows.

# Conversion B -- a real tool-calling jsonl file
$ prepare_rows.py --input-path .../nemo_gym_recipe/Tool-N1-hermes/train.jsonl \
    --input-type jsonl --map messages=responses_create_params --output-dir ./out
Prepared 200 rows; skipped 0 rows.
```

Now here is what the scanner would actually *see* in each of those outputs —
measured by running the scanner's own text-extraction function over the
converted files:

```
Conversion A:  999 rows,  999 with real text (100.0%),  38,117,454 characters
Conversion B:  200 rows,    0 with real text (  0.0%),            0 characters
```

Conversion B produced 200 perfectly well-formed JSON rows containing **zero
scannable characters**. If we ran a PII scan on it, we'd get a clean report,
and the clean report would be meaningless. Nothing in the pipeline today says
a word about this.

That gap — between "the converter didn't complain" and "the data is actually
in there" — is the whole subject of this document. It's a three-line check to
close it, and the rest of this doc is about which other checks are worth
having next to it.

## What can actually go wrong

Everything below is a real, distinct way a conversion can betray us. The
"seen for real?" column is honest about which ones we've actually hit versus
which ones are theoretical-but-possible.

| What goes wrong | Seen for real? | Would we notice today? |
|---|---|---|
| Crash part-way, leaving partial output that looks finished | **Yes** — the `ndarray` crash left a 0-byte `.jsonl` behind | Yes, loudly — and the crash itself is now fixed |
| Rows written, but they hold no scannable text | **Yes** — `Tool-N1-hermes`, 200 rows, 0.0% text (above) | **No. Completely silent** |
| Rows silently dropped by `--require` | **Yes** — by design, this is the tool working as intended | Partly — we get a count, but no reason and no way to look at them |
| A file or nested sub-folder never got read at all | Not observed | **No** — the converter never says what it found |
| Right shape, but the wrong column got mapped | Not observed | **No** — and counts and coverage checks both say "fine" |
| Text truncated, or later turns of a conversation dropped | Not observed | **No** |

The second row is the dangerous one, and it's the one we've actually hit
twice (Families 2 and 3 in `HOW_DATA_CONVERSION_WORKS.md`). A crash is a good
day by comparison: it tells you something's wrong.

## The one measurement that shapes every decision here

Before comparing options, it's worth knowing what validation actually costs on
this data, because the answer is lopsided and it settles most of the argument.

**Getting an exact row count out of the source is nearly free for our columnar
formats.** Parquet stores its row count in the file footer, so you never touch
the data:

```
25 parquet files, 603,683 rows counted in 0.24 s  ->  ~9 ms per file
```

At 9 ms per file, exact row counts for **all 966 parquet files** (40.2M rows,
121.8 GB) take about **9 seconds**. Arrow is the same story — it memory-maps,
so the count comes from metadata:

```
envgen_solo_v1   (0.04 GB):    999 rows in 0.05 s
toucan_sft_v1    (0.31 GB): 28,028 rows in 0.26 s
```

**Reading data, on the other hand, is the expensive thing.** The measurement
already in `FULL_DATASET_INVENTORY.md`: line-counting all 143.6 GB of `.jsonl`
took **23 minutes** with 16 parallel processes — roughly 107 MB/s aggregate on
this filesystem. That's the ceiling for anything that needs a fresh pass over
the corpus.

**And the check logic itself is cheap compared to just parsing the JSON.**
Measured on a real converted file (999 agentic traces, 40.9 MB):

```
json.loads on every row                      : 0.25 s  ->  4,073 rows/s
extract_record_text + character count        : 0.02 s  -> 46,114 rows/s
```

The check is about **11x cheaper than the JSON parsing it would need** if run
as a separate pass. And inside the converter it's cheaper still — the rows are
already sitting in memory as Python objects, so the check costs *only* that
0.02 s of CPU and **no I/O at all**.

**The conclusion that falls out of this:** the cost of validation is dominated
by how many times you read the data, not by how clever the check is. A check
that rides along inside a pass we're already making is close to free. A check
that demands its own pass over the whole text-bearing corpus — roughly 395 GB
and on the order of 125 million rows, adding up the group totals in
`FULL_DATASET_INVENTORY.md` and excluding the no-text groups — costs tens of
minutes to hours. (That total is part-measured, part-sampled: the `.jsonl` and
`.parquet` counts are exact, the `.arrow` ones are sampled estimates.)
Whenever two options catch a similar class of problem, prefer the one that
rides along.

## How mature systems handle this

This is a solved problem in the data-engineering world, and the patterns are
worth copying rather than reinventing. Here's what's out there and what each
one translates to for us.

**1. Row counts, plus "control totals."** The classic warehouse/ETL practice:
reconcile counts between source and target, but never trust counts *alone*.
The [Varigence write-up](https://www.varigence.com/blog/row-counts-lie-column-control-totals)
puts it bluntly — row counts answer "how many," and you also need a sum over a
meaningful column to answer "how much," because a transformation can corrupt
every value while preserving the count perfectly.
[Data-warehouse testing guides](https://www.appsierra.com/blog/data-warehouse-testing)
say the same thing: "Counts match while every value is wrong. Always reconcile
aggregates too." *For us:* our data is text, so our control total is total
character count, not a sum of dollars.

**2. Per-stage kept/processed counts.** NVIDIA's
[NeMo Curator](https://docs.nvidia.com/nemo/curator/main/curate-text/process-data/quality-assessment/heuristic)
— their LLM data-curation framework, and notably the same family of tooling
some of our own data came from — tracks `num_items_processed` on every pipeline
stage so you can compare "reader input" against "writer output" and compute a
retention rate. Their guidance also includes explicitly computing the
empty-text count and percentage, and failing the run when retention crosses a
threshold. *For us:* this is exactly the coverage check, blessed by the people
who do this at corpus scale.

**3. Score first, filter second.** Also from NeMo Curator: use `Score` to
compute and record a metric without dropping anything, look at the
distribution, *then* set thresholds. *For us:* measure text length across a
converted output before deciding what "too short" means, instead of guessing a
threshold up front.

**4. Split-size verification — already in our venv.** The `datasets` library
we already depend on has this built in. Its
[`verify_splits()`](https://github.com/huggingface/datasets/blob/main/src/datasets/utils/info_utils.py)
compares the expected `num_examples` against what was actually recorded and
raises `NonMatchingSplitsSizesError` when they disagree, with `ALL_CHECKS` /
`BASIC_CHECKS` / `NO_CHECKS` modes (checksums included at the strictest
level). *For us, with a real catch:* that check runs on the `load_dataset`
path and only when the metadata carries expected counts. I checked all 755
`dataset_info.json` files under the data root — **only 43 of them carry
`num_examples`**. The other ~94% are `save_to_disk` folders whose metadata has
the schema but no counts, e.g. `envgen_solo_v1`, whose `state.json` holds only
a file list and a fingerprint. So we get this for free on a small minority of
our arrow data and have to do it ourselves for the rest.

**5. A marker file that means "this output is complete."** Hadoop and Spark
write a zero-byte `_SUCCESS` file into an output directory, and only after the
job has genuinely committed — documented in the
[Hadoop manifest committer protocol](https://apache.github.io/hadoop/hadoop-mapreduce-client/hadoop-mapreduce-client-core/manifest_committer_protocol.html),
which also stashes job statistics inside that file. Downstream jobs check for
the marker instead of trusting that a directory full of files means a finished
job. *For us:* this is the direct answer to "a crash left a 0-byte `.jsonl`
behind and nothing downstream could tell."

**6. Dead-letter queues.** Rather than dropping unprocessable records or
killing the whole run, pipelines route bad records to a side location together
with the error and enough context to diagnose and reprocess them — see this
[error-handling and DLQ walkthrough](https://kindatechnical.com/apache-spark/error-handling-and-dead-letter-queues-in-pipelines.html).
*For us:* `--require` currently drops rows and tells us only a count. A
rejects file with the reason attached turns that into something we can look at.

**7. Schema and statistics validation frameworks.** A whole tool category:
[Great Expectations](https://pyrastra.com/posts/python-data-validation-pandera-great-expectations/)
(declarative "expectations" plus browsable HTML Data Docs),
[Pandera](https://pandera.readthedocs.io/en/stable/lazy_validation.html)
(lightweight dataframe schemas, `lazy=True` to collect all failures at once),
Soda Core (YAML checks, warehouse-oriented), AWS Deequ (Spark-native, built
for very large tables), and
[TensorFlow Data Validation](https://www.tensorflow.org/tfx/guide/tfdv) (infers
a schema, computes statistics, and detects drift/skew using L-infinity
distance and Jensen-Shannon divergence). Compared against our case in its own
section below.

**8. Acceptance sampling.** Straight out of manufacturing QA, and the right
tool when checking everything is impractical. For a zero-defect plan the
sample size is `n = ln(1 − C) / ln(1 − p)`, so for 95% confidence that fewer
than 1% of items are bad you inspect **299 randomly chosen items and allow
zero defects** ([calculator and formula](https://www.simplicityhub.co.uk/pages/attrsamplesize.html)).
The related "rule of three" says that zero defects in `n` samples puts the
upper bound at roughly `3/n`. The honest flip side, well put in
[this discussion](https://stats.stackexchange.com/questions/642165/how-many-samples-should-i-test-to-be-95-sure-that-no-error-exists):
you can never be 95% sure of *zero* defects without inspecting 100%. *For us:*
300 spot-checked rows buys a defensible statement about a corpus of ~125
million, and it costs seconds.

## Our options, compared

Ten concrete options. Factors are defined right after the table.

| # | Option | What it catches | Reliability | Build effort | Run cost on our data |
|---|---|---|---|---|---|
| 1 | **Count the rows** — source rows vs. written + skipped | Missed files, truncated reads, crash mid-write | Exact — it's arithmetic, can't be fooled | Small | ~0 (9 s for all parquet; free elsewhere) |
| 2 | **Count the files** — report what was discovered before starting | Missed nested folders, wrong glob, empty input | Exact, but needs a human to know the expected number | Very small | ~0 |
| 3 | **Marker file + run report** — write `_SUCCESS` and the metrics only at the very end | Partial output mistaken for complete | Exact — it's there or it isn't | Very small | 0 |
| 4 | **Text-coverage check** — % of rows where the scanner's own extractor returns text | The silent-empty class (Families 2 and 3) | Exact for that class — runs the consumer's own code | Small | Free inline; +10% as a separate pass |
| 5 | **Shape assertion** — `messages` really is a list of `{role, content}` | Type-level wrongness, with row-level detail | Exact | Small | Free inline |
| 6 | **Character control total** — compare text volume in vs. out | Truncation, dropped turns, partial content loss | Heuristic — needs a tolerance band, can false-alarm | Medium | Free inline |
| 7 | **Sample 300 rows side by side** — source row next to converted row | Wrong-but-plausible column; wrong nesting level | Probabilistic but quantifiable (<1% bad at 95% confidence) | Small to build, needs human reading time | Seconds of machine time |
| 8 | **Keep the rejects** — write skipped rows plus the reason | Makes every other failure diagnosable | Not a check; it's what makes checks actionable | Medium | ~0 |
| 9 | **Hash everything** — compare normalized text of every row, both sides | Essentially all content loss, exactly | Exact, but the normalization rules become their own bug source | Large | A full extra pass: ~23 min per 143 GB, hours for the 439 GB arrow tree |
| 10 | **Drift between runs** — keep metrics history, flag changes | Regressions over time, a new format behaving oddly | Heuristic, threshold-tuned | Large | Small per run, but needs stored history |

**How to read the factor columns:**

- **Reliability** — *Exact* means it either passes or fails on arithmetic or a
  direct code path, with no judgment call and no way to be fooled within its
  scope. *Heuristic* means it needs a threshold, and thresholds produce false
  alarms. *Probabilistic* means it gives you a confidence statement, not a
  guarantee.
- **Build effort** — *Very small* is a handful of lines. *Small* is tens of
  lines inside an existing function. *Medium* means new output files or
  per-format knowledge to maintain. *Large* means a genuinely new piece of
  machinery.
- **Run cost** — measured, from the section above. "Free inline" means it
  happens inside a pass the converter is already making, so it adds CPU but
  no disk reads.

### The ones worth understanding in a bit more detail

**Option 4, text coverage, has an unusually strong property** and it's why I'd
put it first. It doesn't approximate what the scanner sees — it calls
`extract_record_text()`, the *same function the PII scanner itself uses*. So
it cannot disagree with reality. If the check says "0% of rows have text," the
scanner will find nothing, guaranteed, because it's the same code. Most
validation is a model of the consumer; this one *is* the consumer.

**Option 6, the character control total, needs an honest caveat.** Output text
is legitimately *not* the same size as the source content — the extractor adds
`ROLE:\n` prefixes and joins turns with blank lines, so a correct conversion
inflates the character count a little. So this can't be an equality check;
it has to be a ratio with a tolerance band, which means it can cry wolf. It
should warn, never fail a run. It's still worth having, because it's the only
cheap thing on the list that catches partial loss — a conversion that keeps
just the first turn of every conversation would sail past options 1 through 5
with a perfect score.

**Option 7 is the only one that catches a wrong-but-plausible mapping.** If we
map `text=summary` when we meant `text=body`, every count matches, coverage is
100%, the shape is right, and the volume looks reasonable. Nothing but looking
at the actual content catches it. The sampling math is what makes it a real
check rather than a vibe: 300 random rows with zero problems supports "fewer
than 1% of rows are wrong, at 95% confidence." Want a stronger claim? Fewer
than 0.1% at the same confidence needs about 2,995 rows — still cheap in
machine terms, but that's a lot of human reading.

**Option 9 is where I'd push back.** It's the most thorough option and the
worst value here. It needs a full extra pass over the source (tens of minutes
to hours), and the hard part isn't the hashing — it's writing normalization
rules that decide whether reshaped-but-correct text counts as "the same."
Those rules are themselves code that can be wrong, and when they are, they
produce false failures on good data. A cheaper 90% of the benefit: run it on
*one representative file per format family* rather than the whole corpus.

**Option 10 is a good idea at the wrong time.** Drift detection pays off when
the same pipeline runs repeatedly and you want to know when today differs from
yesterday. Our situation is mostly a large one-off conversion with new datasets
arriving occasionally. If conversions become a recurring job, revisit it —
and at that point the metrics from option 3 are already the history it needs,
which is a nice argument for doing option 3 early.

## Should we just use an off-the-shelf framework?

Worth asking seriously, because "write our own checks" is how people end up
maintaining a bad version of a solved thing. Here's the honest comparison for
*our* situation.

| Framework | Built for | Fits us? |
|---|---|---|
| **Pandera** | Lightweight dataframe schema validation in Python | Closest fit of the bunch. ~12 direct dependencies, pandas-native, and our parquet/csv path is already pandas. Worth a look if we outgrow hand-written checks |
| **Great Expectations** | Teams that need readable, shareable data-quality reports | Powerful, and its HTML "Data Docs" are genuinely nice — but 100+ dependencies and a config-heavy setup to validate two possible row shapes. Too much machinery for the problem |
| **Soda Core** | YAML-declared checks against SQL warehouses | Wrong shape — it's built around querying a warehouse. We have files, not tables |
| **AWS Deequ** | Constraint checks on very large Spark tables | Needs Spark. We have SLURM and single-process Python. Overkill by a wide margin |
| **TFDV** | ML training data: infer a schema, compute statistics, detect drift | The most conceptually relevant (it's for ML data, and drift is real) but it's TFRecord/protobuf-shaped and pulls in the TFX world. Heavy for what we need |

**My read:** none of these earn their keep *yet*, for one specific reason —
our validation target is unusually small. We are not validating 40 columns of
typed business data; we are checking two possible shapes (`text`, or
`messages` as a list of `{role, content}`) and a handful of counts. The
highest-value check we have (option 4) is three lines calling a function we
already own, and no framework can beat that because no framework knows what
our scanner considers readable text.

There's also a real cost to frameworks that doesn't show up in feature
tables: Pandera's own issue tracker has a case where
[lazy validation took ~17 minutes](https://github.com/unionai-oss/pandera/issues/652)
on a 594k-row frame because collecting a large number of failure cases scaled
badly. Our rows are single documents holding up to 113,000 characters of text
each; heavy per-value machinery is not obviously going to be fast on that
shape.

So: **hand-roll the cheap checks now, and reconsider Pandera specifically if
the checks start multiplying.** That's a real fork we can revisit, not a
permanent no.

## What I'd actually pick

Three tiers. The reasoning is in the previous sections; this is just the
shortlist.

**Do these first — they're nearly free and they cover what we've actually been
bitten by:**

- **Option 4, text coverage.** Highest value on the list. It's the only thing
  standing between us and a meaningless "0 hits" report, and it's the cheapest
  real check we have.
- **Option 1, row-count reconciliation.** Exact, arithmetic, no judgment.
  It also makes `--require` skips visible as a number that has to add up.
- **Option 3, marker file plus a small report.** Turns all of the above from
  terminal output nobody re-reads into a file sitting next to the data, and
  it's the thing that makes the numbers auditable next week. It also doubles
  as the metrics history that option 10 would need later.

**Do these next, once the first three are in and we trust them:**

- **Option 8, keep the rejects.** Small effort, and it's what turns "4,312
  rows were skipped" into something we can actually diagnose.
- **Option 7, sample 300 rows.** Cheap, and the only defense against a
  plausible-but-wrong mapping. Best used once per new dataset family rather
  than on every run.
- **Option 6, character control total, as a warning only.** Worth it for
  partial-loss detection, but it must not be able to fail a run.

**Deliberately not now:**

- **Option 9, hash everything** — bad cost/benefit at corpus scale; do it on
  one file per family instead if we ever want the extra assurance.
- **Option 10, drift detection** — right idea, wrong time. Revisit if
  conversions become recurring.
- **Any of the frameworks** — nothing to gain yet at our tiny schema surface.
- **Provenance and tracing** — out of scope by decision, still parked in
  `HOW_DATA_CONVERSION_WORKS.md` section 4. Worth knowing that option 8 is
  much more useful with some row identifier attached, so if we ever do build
  provenance, the rejects file gets better for free.

Two things none of this fixes, worth stating plainly so they aren't a surprise
later.

`prepare_rows.py` currently loads an entire input into memory before writing
anything — that's why the 1.9 GB verl file had to be sliced to test it.
Validation doesn't make that worse, but no amount of checking turns it into a
streaming converter.

And `--max-rows` is not a cheap way to sample. Reading the code, it slices
*after* the full read (`rows = list(...)` and then `rows[: args.max_rows]`),
so it limits what gets written, not what gets read. The 200-row demo at the
top of this doc took 38 seconds because it read all 111 MB of
`Tool-N1-hermes/train.jsonl` first. Worth knowing before building option 7 on
top of it — for a genuinely cheap sample of a large file, slice it with
`pyarrow` or `head` first, the way the verl test did.

## Reproduce every number in this document

```bash
cd /home/naresh/model-safety

# The headline demo -- a "successful" conversion with zero scannable text.
# Step 1: convert the tool-calling file the way --map allows today
PYTHONPATH="$PWD/modelsafety/readers" modelsafety/readers/.venv/bin/python \
  modelsafety/readers/prepare_rows.py \
  --input-path /home/shared/agentic_slm/data/rl/raw/nemo_gym_recipe/Tool-N1-hermes/train.jsonl \
  --input-type jsonl --map messages=responses_create_params \
  --output-dir output/validation_demo/family2 --max-rows 200

# Step 2: ask what the scanner would actually see in that output
PYTHONPATH="$PWD" modelsafety/readers/.venv/bin/python -c "
import json, glob
from modelsafety.contract.textract import extract_record_text
rows = [json.loads(l) for f in sorted(glob.glob('output/validation_demo/family2/chunk-*/*.jsonl'))
        for l in open(f, encoding='utf-8')]
texts = [extract_record_text(r) for r in rows]
nonempty = sum(1 for t in texts if t)
print(f'rows written:   {len(rows):,}')
print(f'with real text: {nonempty:,} ({100*nonempty/len(rows):.1f}%)')
print(f'total chars:    {sum(len(t) for t in texts):,}')
"
# -> rows written: 200 / with real text: 0 (0.0%) / total chars: 0

# The same check on a good conversion, for contrast -> 999 rows, 100.0%
# (convert .../sft/curriculum/distill/envgen_solo_v1 with
#  --input-type huggingface --require messages, then rerun the check above)

# Row counts are nearly free from parquet footers -- ~9 ms/file
modelsafety/readers/.venv/bin/python -c "
import time, glob
import pyarrow.parquet as pq
files = glob.glob('/home/shared/agentic_slm/data/rl/processed/qwen35-xml/sft-stage1_0714/shards/*/train.parquet')[:25]
t = time.perf_counter()
total = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
dt = time.perf_counter() - t
print(f'{len(files)} files, {total:,} rows, {dt:.2f}s -> {dt/len(files)*1000:.0f} ms/file')
"

# ...and free from arrow too, because it memory-maps
modelsafety/readers/.venv/bin/python -c "
import time
from datasets import load_from_disk
t = time.perf_counter()
ds = load_from_disk('/home/shared/agentic_slm/data/sft/curriculum/distill/envgen_solo_v1')
print(f'{ds.num_rows:,} rows in {time.perf_counter()-t:.2f}s')
"

# How many arrow folders give us a free expected row count? (43 of 755)
find /home/shared/agentic_slm/data -name dataset_info.json > /tmp/di.txt
echo "total:          $(wc -l < /tmp/di.txt)"
echo "with counts:    $(xargs -a /tmp/di.txt grep -l num_examples 2>/dev/null | wc -l)"

# The sampling math: 95% confidence that under 1% of rows are bad
python3 -c "
import math
for p in (0.01, 0.001):
    print(f'under {p*100:g}% bad at 95% confidence -> inspect {math.ceil(math.log(0.05)/math.log(1-p))} rows, allow zero defects')
"
```
