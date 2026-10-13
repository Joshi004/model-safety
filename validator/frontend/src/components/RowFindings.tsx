import type { ReactNode } from 'react'
import { SCANNER_LABELS, describeFate, describePlace, describeStepOutcome } from '../rows'
import type {
  DuplicateCopy,
  RowHighlight,
  RowMessageFlag,
  RowPlace,
  RowScannerEntry,
  RowStep,
  RowStory,
  RowUnplaced,
} from '../types'
import RowText from './RowText'

// What each scanner found in a row, step by step, with the findings that caused
// the removal first; and, for a duplicate, the kept copy beside it. Every state
// is spelled out: "found nothing" is not "not scanned" (the row was removed
// earlier) and not "no text".
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

const SHOWN_FINDINGS = 12
const SHOWN_TEXT_CHARS = 60

function clip(text: string): string {
  return text.length > SHOWN_TEXT_CHARS ? `${text.slice(0, SHOWN_TEXT_CHARS)}…` : text
}

function where(index: number | null | undefined): string {
  return index === null || index === undefined ? 'text' : `message ${index}`
}

function percent(confidence: number | null | undefined): string {
  return typeof confidence === 'number' ? `${Math.round(confidence * 100)}%` : '—'
}

// The first findings shown, the rest folded away.
function FindingList({ items }: { items: { key: string; caused: boolean; content: ReactNode }[] }) {
  if (items.length === 0) return null
  // Findings that caused the removal come first.
  const ordered = [...items].sort((a, b) => Number(b.caused) - Number(a.caused))
  const render = (list: typeof ordered) =>
    list.map((item) => (
      <li key={item.key} className={item.caused ? 'finding finding-caused' : 'finding'}>
        {item.content}
        {item.caused && <strong> removed the row</strong>}
      </li>
    ))
  const rest = ordered.slice(SHOWN_FINDINGS)
  return (
    <>
      <ul className="finding-list">{render(ordered.slice(0, SHOWN_FINDINGS))}</ul>
      {rest.length > 0 && (
        <details>
          <summary className="small">and {rest.length.toLocaleString()} more</summary>
          <ul className="finding-list">{render(rest)}</ul>
        </details>
      )}
    </>
  )
}

function nerItems(step: number, highlights: RowHighlight[], unplaced: RowUnplaced[]) {
  const placed = highlights
    .filter((mark) => mark.step === step && mark.scanner === 'ner')
    .map((mark, index) => ({
      key: `p${index}`,
      caused: mark.caused_removal,
      content: (
        <>
          <strong>{mark.label}</strong> in {where(mark.message_index)}, {percent(mark.confidence)}
          {mark.matched !== undefined && <> "{clip(mark.matched)}"</>}
          {mark.eligible === false && <span className="muted"> (excluded by the NER policy)</span>}
        </>
      ),
    }))
  const lost = unplaced
    .filter((item) => item.step === step && item.scanner === 'ner')
    .map((item, index) => ({
      key: `u${index}`,
      caused: item.caused_removal,
      content: (
        <>
          <strong>{item.label}</strong> in {where(item.message_index)}: <span className="muted">not marked in the text, {item.why}</span>
        </>
      ),
    }))
  return [...placed, ...lost]
}

function regexItems(step: number, highlights: RowHighlight[], unplaced: RowUnplaced[]) {
  // A value can be in the text more than once; it is one finding.
  const groups = new Map<string, { mark: RowHighlight; matches: number }>()
  for (const mark of highlights) {
    if (mark.step !== step || mark.scanner !== 'regex_pii') continue
    const key = `${mark.category}|${mark.value}`
    const group = groups.get(key)
    if (group) group.matches += 1
    else groups.set(key, { mark, matches: 1 })
  }
  const placed = [...groups.entries()].map(([key, { mark, matches }]) => ({
    key,
    caused: mark.caused_removal,
    content: (
      <>
        <strong>{mark.category}</strong> "{clip(mark.value ?? '')}" marked in {matches} place{matches === 1 ? '' : 's'}
      </>
    ),
  }))
  const lost = unplaced
    .filter((item) => item.step === step && item.scanner === 'regex_pii')
    .map((item, index) => ({
      key: `u${index}`,
      caused: item.caused_removal,
      content: (
        <>
          <strong>{item.category}</strong> "{clip(item.value ?? '')}": <span className="muted">not marked in the text, {item.why}</span>
        </>
      ),
    }))
  return [...placed, ...lost]
}

function toxicityItems(step: number, flags: RowMessageFlag[], unplaced: RowUnplaced[]) {
  const placed = flags
    .filter((flag) => flag.step === step)
    .map((flag, index) => ({
      key: `p${index}`,
      caused: flag.caused_removal,
      content: (
        <>
          <strong>{flag.verdict ?? flag.outcome ?? 'flagged'}</strong> in {where(flag.message_index)}
          {flag.categories && flag.categories.length > 0 && <>: {flag.categories.join(', ')}</>}
          {flag.error && <span className="error"> (guard error: {flag.error})</span>}
        </>
      ),
    }))
  const lost = unplaced
    .filter((item) => item.step === step && item.scanner === 'toxicity')
    .map((item, index) => ({
      key: `u${index}`,
      caused: item.caused_removal,
      content: (
        <>
          <strong>{item.verdict}</strong>: <span className="muted">{item.why}</span>
        </>
      ),
    }))
  return [...placed, ...lost]
}

function ScannerFindings({
  name,
  scanner,
  step,
  story,
  onOpenRow,
}: {
  name: string
  scanner: RowScannerEntry
  step: RowStep
  story: RowStory
  onOpenRow: (place: RowPlace) => void
}) {
  const label = SCANNER_LABELS[name] ?? name
  let body: ReactNode
  if (scanner.state === 'not_reached') {
    body = <span className="muted">not scanned: an earlier step removed the row</span>
  } else if (scanner.state === 'no_text') {
    body = <span className="muted">no text to scan (this is not the same as clean)</span>
  } else if (name === 'dedup') {
    const copies = story.duplicate_of.filter((copy) => copy.place.step === step.step)
    const duplicates = story.duplicated_by.items.filter((place) => place.step === step.step)
    const count = scanner.duplicated_by?.length ?? 0
    body = (
      <>
        {copies.length === 0 && count === 0 && 'no duplicate found'}
        {copies.map((copy) => (
          <span key={describePlace(copy.place)}>
            duplicate of{' '}
            <button type="button" className="link-button" onClick={() => onOpenRow(copy.place)}>
              {describePlace(copy.place)}
            </button>
          </span>
        ))}
        {count > 0 && (
          <>
            {copies.length > 0 && '; '}
            {count.toLocaleString()} row{count === 1 ? ' was' : 's were'} removed as {count === 1 ? 'a duplicate' : 'duplicates'} of this one
            {duplicates.length > 0 && (
              <details>
                <summary className="small">Show {duplicates.length === count ? 'them' : `the first ${duplicates.length}`}</summary>
                <ul className="finding-list">
                  {duplicates.map((place) => (
                    <li key={describePlace(place)}>
                      <button type="button" className="link-button" onClick={() => onOpenRow(place)}>
                        {describePlace(place)}
                      </button>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </>
    )
  } else {
    const items =
      name === 'ner'
        ? nerItems(step.step, story.highlights, story.unplaced)
        : name === 'regex_pii'
          ? regexItems(step.step, story.highlights, story.unplaced)
          : toxicityItems(step.step, story.message_flags, story.unplaced)
    body =
      items.length === 0 ? (
        <span>found nothing</span>
      ) : (
        <>
          {items.length.toLocaleString()} finding{items.length === 1 ? '' : 's'}
          <FindingList items={items} />
        </>
      )
  }
  return (
    <div className="scanner-findings">
      <strong>{label}:</strong> {body}
    </div>
  )
}

export function Findings({ story, onOpenRow }: { story: RowStory; onOpenRow: (place: RowPlace) => void }) {
  return (
    <div className="row-findings">
      <h3>Findings</h3>
      {story.entry.steps.map((step) => (
        <div key={step.step} className="row-step">
          <h4>
            Step {step.step}:{' '}
            {describeStepOutcome(
              step.outcome,
              (step.reasons ?? []).map((reason) => reason.reason),
            )}
          </h4>
          {Object.entries(step.scanners).map(([name, scanner]) => (
            <ScannerFindings key={name} name={name} scanner={scanner} step={step} story={story} onOpenRow={onOpenRow} />
          ))}
        </div>
      ))}
      {story.findings_cut && (
        <p className="muted small">This row has more findings than are shown here; the first ones are listed.</p>
      )}
    </div>
  )
}

function CopyFate({ copy }: { copy: DuplicateCopy }) {
  if (copy.story === null) {
    return <div className="banner banner-warning small">The kept copy's status can't be read: {copy.problem}</div>
  }
  if (copy.story.status === 'kept') {
    return (
      <div className="banner banner-ok small">
        The kept copy is in the clean data
        {copy.story.clean && <> ({copy.story.clean.file}, line {copy.story.clean.line})</>}.
      </div>
    )
  }
  return (
    <div className="banner banner-warning small">
      <strong>The kept copy was itself removed later:</strong> {describeFate(copy.story)}. So this text is not in the
      clean data through that copy.
    </div>
  )
}

// For a row removed as a duplicate: the copy that was kept instead, what became of
// it (in 121,976 of the full run's duplicates it was removed later), and both texts.
export function DuplicatePanel({
  story,
  onOpenRow,
}: {
  story: RowStory
  onOpenRow: (place: RowPlace) => void
}) {
  if (story.duplicate_of.length === 0) return null
  return (
    <div className="duplicate-panel">
      <h3>Duplicate</h3>
      {story.duplicate_of.map((copy) => (
        <div key={describePlace(copy.place)} className="duplicate-copy">
          <p>
            Removed at step {copy.place.step} as a duplicate of{' '}
            <button type="button" className="link-button" onClick={() => onOpenRow(copy.place)}>
              {describePlace(copy.place)}
            </button>
            {copy.place.run !== story.location.run && <span className="muted small"> (in another dataset)</span>}
          </p>
          <CopyFate copy={copy} />
          {copy.problem !== null && copy.story !== null && (
            <p className="muted small">The kept copy's text isn't shown: {copy.problem}</p>
          )}
          {copy.segments !== null && story.segments !== null && (
            <details>
              <summary className="small">Show this row and the kept copy side by side</summary>
              <div className="duplicate-sides">
                <div>
                  <h4>This row (removed)</h4>
                  <RowText segments={story.segments} />
                </div>
                <div>
                  <h4>The kept copy</h4>
                  <RowText segments={copy.segments} />
                </div>
              </div>
            </details>
          )}
        </div>
      ))}
    </div>
  )
}
