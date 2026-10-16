import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { errorMessage, getFlaggedRow } from '../api'
import { describeConfidence, describeSameShare, whereInRow } from '../flagged'
import { nerLabelName, ruleCategoryName } from '../scans'
import type { FlaggedRowDetail, KeptCopy, RowHighlight, RowMessageFlag, RowUnplaced } from '../types'
import { ShowOriginal } from './OriginalLookup'
import RowText from './RowText'

// One flagged row, opened from the table: what each scanner found in it (and where in its text),
// its Prepared text with the findings marked, for a duplicate the copy it matched beside it, and
// "Show original", which opens the original row the Prepared row came from.
//
// The text is the Prepared line as it was when it was scanned (the backend checks its
// fingerprint), so it is the text the findings point into.
//
// Safety: every value shown here comes from files on disk and renders as plain React text,
// never as markup.

const SHOWN_FINDINGS = 12
const SHOWN_TEXT_CHARS = 60

function clip(text: string): string {
  return text.length > SHOWN_TEXT_CHARS ? `${text.slice(0, SHOWN_TEXT_CHARS)}…` : text
}

interface Item {
  key: string
  content: ReactNode
}

// The first findings shown, the rest folded away.
function FindingList({ items }: { items: Item[] }) {
  if (items.length === 0) return null
  const rest = items.slice(SHOWN_FINDINGS)
  return (
    <>
      <ul className="finding-list">
        {items.slice(0, SHOWN_FINDINGS).map((item) => (
          <li key={item.key}>{item.content}</li>
        ))}
      </ul>
      {rest.length > 0 && (
        <details>
          <summary className="small">and {rest.length.toLocaleString()} more</summary>
          <ul className="finding-list">
            {rest.map((item) => (
              <li key={item.key}>{item.content}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}

function nerItems(highlights: RowHighlight[], unplaced: RowUnplaced[]): Item[] {
  const placed = highlights
    .filter((mark) => mark.scanner === 'ner')
    .map((mark, index) => ({
      key: `p${index}`,
      content: (
        <>
          <strong>{nerLabelName(mark.label ?? '')}</strong> in {whereInRow(mark.message_index)},{' '}
          {describeConfidence(mark.confidence)}
          {mark.matched !== undefined && <> "{clip(mark.matched)}"</>}
        </>
      ),
    }))
  const lost = unplaced
    .filter((item) => item.scanner === 'ner')
    .map((item, index) => ({
      key: `u${index}`,
      content: (
        <>
          <strong>{nerLabelName(item.label ?? '')}</strong> in {whereInRow(item.message_index)}:{' '}
          <span className="muted">not marked in the text, {item.why}</span>
        </>
      ),
    }))
  return [...placed, ...lost]
}

function regexItems(highlights: RowHighlight[], unplaced: RowUnplaced[]): Item[] {
  // A value can be in the text more than once; it is one finding.
  const groups = new Map<string, { mark: RowHighlight; places: number }>()
  for (const mark of highlights) {
    if (mark.scanner !== 'regex_pii') continue
    const key = `${mark.category}|${mark.value}`
    const group = groups.get(key)
    if (group) group.places += 1
    else groups.set(key, { mark, places: 1 })
  }
  const placed = [...groups.entries()].map(([key, { mark, places }]) => ({
    key,
    content: (
      <>
        <strong>{ruleCategoryName(mark.category ?? '')}</strong> "{clip(mark.value ?? '')}" marked in {places} place
        {places === 1 ? '' : 's'}
      </>
    ),
  }))
  const lost = unplaced
    .filter((item) => item.scanner === 'regex_pii')
    .map((item, index) => ({
      key: `u${index}`,
      content: (
        <>
          <strong>{ruleCategoryName(item.category ?? '')}</strong> "{clip(item.value ?? '')}":{' '}
          <span className="muted">not marked in the text, {item.why}</span>
        </>
      ),
    }))
  return [...placed, ...lost]
}

function toxicityItems(flags: RowMessageFlag[], unplaced: RowUnplaced[]): Item[] {
  const placed = flags.map((flag, index) => ({
    key: `p${index}`,
    content: (
      <>
        <strong>{flag.verdict ?? 'Guard error'}</strong> in {whereInRow(flag.message_index)}
        {flag.categories.length > 0 && <>: {flag.categories.join(', ')}</>}
        {flag.detoxify_score !== null && <span className="muted"> (Detoxify {flag.detoxify_score.toFixed(2)})</span>}
        {flag.error !== null && <span className="error"> (guard error: {flag.error})</span>}
        {!flag.counts && <span className="muted"> — doesn't count under the current choice</span>}
      </>
    ),
  }))
  const lost = unplaced
    .filter((item) => item.scanner === 'toxicity')
    .map((item, index) => ({
      key: `u${index}`,
      content: (
        <>
          <strong>{item.verdict ?? 'Guard error'}</strong>: <span className="muted">{item.why}</span>
        </>
      ),
    }))
  return [...placed, ...lost]
}

function Findings({ detail }: { detail: FlaggedRowDetail }) {
  const items =
    detail.scanner === 'pii_ner'
      ? nerItems(detail.highlights, detail.unplaced)
      : detail.scanner === 'pii_regex'
        ? regexItems(detail.highlights, detail.unplaced)
        : toxicityItems(detail.message_flags, detail.unplaced)
  return (
    <>
      <h4>Findings</h4>
      {items.length === 0 ? (
        <p className="muted small">No finding could be listed for this row.</p>
      ) : (
        <p className="small">
          {items.length.toLocaleString()} finding{items.length === 1 ? '' : 's'}
        </p>
      )}
      <FindingList items={items} />
      {detail.findings_cut && (
        <p className="muted small">This row has more findings than are shown here; the first ones are listed.</p>
      )}
    </>
  )
}

// "Show original" for a Prepared line, when its `_origin` note could be read.
function Original({ origin, runDir }: { origin: FlaggedRowDetail['origin']; runDir: string | null }) {
  if (origin === null || runDir === null) {
    return <p className="muted small">The original row can't be shown: this line has no note of where it came from.</p>
  }
  return <ShowOriginal origin={origin} runDir={runDir} />
}

// The row beside the copy of it that was kept.
function DuplicateSides({ detail, copy }: { detail: FlaggedRowDetail; copy: KeptCopy }) {
  // The marks and the dimming only apply when the rows were compared; with a problem the lists are empty.
  const comparison = detail.comparison !== null && detail.comparison.problem === null ? detail.comparison : null
  const problem = detail.comparison?.problem ?? null
  // Below this share of the same words, the two rows are alike only loosely.
  const LOW_SHARE = 0.5
  return (
    <>
      <h4>Duplicate</h4>
      <p className="small">
        This row matches the row at{' '}
        <span className="mono">
          {copy.file ?? '?'}, line {copy.line ?? '?'}
        </span>{' '}
        in {copy.dataset ?? 'a reference set'}. That copy is the one that is kept; this row is flagged.
      </p>
      {copy.problem !== null && <p className="muted small">The kept copy's text isn't shown: {copy.problem}</p>}
      {problem !== null && <p className="muted small">The rows aren't compared word by word: {problem}</p>}
      {comparison !== null && comparison.same_share !== null && (
        <>
          <p className="small">
            <mark className="hl hl-dedup-same">Same words</mark> are in both rows;{' '}
            <mark className="hl hl-dedup-different">other words</mark> are only in that row.{' '}
            {describeSameShare(comparison.same_share)} of the compared words are the same.
          </p>
          {comparison.same_share < LOW_SHARE && (
            <p className="muted small">
              This row may have been linked to the kept copy through another similar row, so the two can differ more
              than expected.
            </p>
          )}
          {comparison.cut && (
            <p className="muted small">The rows differ in more places than are marked; the first ones are marked.</p>
          )}
          <p className="muted small">
            Marks come from a word-by-word comparison, a guide to how alike the rows are, not the scan's own score.
            Messages the scan did not compare are dimmed.
          </p>
        </>
      )}
      <div className="duplicate-sides">
        <div>
          <h4>This row (flagged)</h4>
          {detail.segments !== null ? (
            <RowText
              segments={detail.segments}
              highlights={comparison?.row_highlights}
              compared={comparison?.row_compared}
            />
          ) : (
            <p className="muted small">This row's text isn't shown.</p>
          )}
          <Original origin={detail.origin} runDir={detail.prepared.run_dir} />
        </div>
        <div>
          <h4>The kept copy</h4>
          {copy.segments !== null ? (
            <RowText
              segments={copy.segments}
              highlights={comparison?.kept_highlights}
              compared={comparison?.kept_compared}
            />
          ) : (
            <p className="muted small">The kept copy's text isn't shown.</p>
          )}
          <Original origin={copy.origin} runDir={copy.run_dir} />
        </div>
      </div>
    </>
  )
}

interface Outcome {
  key: string
  detail: FlaggedRowDetail | null
  error: string | null
}

export default function FlaggedRowPanel({
  datasetId,
  scanner,
  file,
  line,
  categories,
  verdicts,
  onClose,
}: {
  datasetId: string
  scanner: string
  file: string
  line: number
  // Toxicity's choice of what counts as flagged: it decides which messages are marked as counting.
  categories: string[] | null
  verdicts: string[] | null
  onClose: () => void
}) {
  // What was asked for, so an answer for an earlier row is never shown for this one.
  const requestKey = JSON.stringify([datasetId, scanner, file, line, categories, verdicts])
  const [outcome, setOutcome] = useState<Outcome | null>(null)

  useEffect(() => {
    let cancelled = false
    getFlaggedRow(datasetId, scanner, file, line, { categories, verdicts })
      .then((detail) => {
        if (!cancelled) setOutcome({ key: requestKey, detail, error: null })
      })
      .catch((caught: unknown) => {
        if (!cancelled) setOutcome({ key: requestKey, detail: null, error: errorMessage(caught) })
      })
    return () => {
      cancelled = true
    }
    // `requestKey` says when what is asked for has really changed; `categories` and
    // `verdicts` are arrays that can be new objects with the same items.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey])

  const current = outcome?.key === requestKey ? outcome : null
  const detail = current?.detail ?? null
  const copy = detail?.duplicate_of ?? null

  return (
    <div className="card flagged-detail">
      <div className="page-header">
        <h3 className="scan-heading">Flagged row</h3>
        <button type="button" className="secondary push-right" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="small">
        <span className="mono">
          {file}, line {line.toLocaleString()}
        </span>{' '}
        <span className="muted">(in this dataset's converted data)</span>
      </p>

      {current === null && <p className="muted">Loading the row…</p>}
      {current?.error != null && <div className="banner banner-error">Couldn't open this row: {current.error}</div>}

      {detail !== null && (
        <>
          {detail.prepared.problem !== null && (
            <div className={detail.prepared.fingerprint_ok === false ? 'banner banner-error small' : 'banner banner-warning small'}>
              {detail.prepared.problem}
            </div>
          )}
          {detail.scanner !== 'dedup' && <Findings detail={detail} />}
          {copy !== null ? (
            <DuplicateSides detail={detail} copy={copy} />
          ) : (
            <>
              <h4>Text</h4>
              {detail.segments !== null ? (
                <RowText segments={detail.segments} highlights={detail.highlights} flags={detail.message_flags} />
              ) : (
                <p className="muted small">The row's text isn't shown.</p>
              )}
              <Original origin={detail.origin} runDir={detail.prepared.run_dir} />
            </>
          )}
        </>
      )}
    </div>
  )
}
