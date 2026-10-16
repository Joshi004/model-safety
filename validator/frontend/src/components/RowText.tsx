import { describeHighlight, splitPieces } from '../flagged'
import type { RowHighlight, RowMessageFlag, RowSegment } from '../types'
import Chip from './Chip'

// The text a scanner read from a row (its `text`, or each message), with the findings marked in
// it: NER spans and rule-based PII matches inside the text, and a chip on each message that
// toxicity judged. A toxicity chip is red when the message counts as flagged under the choice
// made on the page, and grey when it doesn't.
//
// Safety: the text is real data and renders as plain React text, never as markup.

// A message with no marks and more text than this starts folded.
const FOLD_CHARS = 4000

// A duplicate's mark is coloured by whether the words are in both rows; any other by its scanner.
function highlightClass(mark: RowHighlight): string {
  return mark.scanner === 'dedup' ? `hl-dedup-${mark.kind ?? 'different'}` : `hl-${mark.scanner}`
}

function MarkedText({ text, marks }: { text: string; marks: RowHighlight[] }) {
  const pieces = splitPieces(text, marks)
  return (
    <pre className="segment-text">
      {pieces.map((piece, index) =>
        piece.marks.length === 0 ? (
          <span key={index}>{piece.text}</span>
        ) : (
          <mark
            key={index}
            className={`hl ${piece.marks.map(highlightClass).join(' ')}`}
            title={piece.marks.map(describeHighlight).join('\n')}
          >
            {piece.text}
          </mark>
        ),
      )}
    </pre>
  )
}

function flagTitle(flag: RowMessageFlag): string {
  const parts = [flag.categories.length > 0 ? flag.categories.join(', ') : 'no category']
  if (flag.detoxify_score !== null) parts.push(`Detoxify score ${flag.detoxify_score.toFixed(2)}`)
  if (flag.error !== null) parts.push(`guard error: ${flag.error}`)
  parts.push(flag.counts ? 'counts as flagged' : "doesn't count under the current choice")
  return parts.join('; ')
}

export default function RowText({
  segments,
  highlights = [],
  flags = [],
  compared,
}: {
  segments: RowSegment[]
  highlights?: RowHighlight[]
  flags?: RowMessageFlag[]
  // For a duplicate: the messages (null for a row's `text`) the scan compared. A message not in
  // the list is dimmed, labelled and folded. Left out, every message is shown as usual.
  compared?: (number | null)[]
}) {
  if (segments.length === 0) return <p className="muted">This row has no text or messages.</p>
  return (
    <div className="row-text">
      {segments.map((segment) => {
        const marks = highlights.filter((mark) => mark.message_index === segment.message_index)
        const segmentFlags = flags.filter((flag) => flag.message_index === segment.message_index)
        const notCompared = compared !== undefined && !compared.includes(segment.message_index)
        const folded =
          notCompared || (marks.length === 0 && segmentFlags.length === 0 && segment.text.length > FOLD_CHARS)
        const label = segment.message_index === null ? 'text' : `${segment.role} #${segment.message_index}`
        return (
          <div key={segment.message_index ?? 'text'} className={notCompared ? 'turn-card turn-card-dimmed' : 'turn-card'}>
            <div className="turn-header">
              <span className="turn-role-pill">{label}</span>
              <span className="muted small">{segment.length.toLocaleString()} characters</span>
              {marks.length > 0 && <span className="muted small">{marks.length.toLocaleString()} marked</span>}
              {notCompared && (
                <Chip
                  label="not compared"
                  title="The duplicates scan left this message out, so it is not part of the comparison."
                />
              )}
              {segmentFlags.map((flag, index) => (
                <Chip
                  key={index}
                  label={`toxicity: ${flag.verdict ?? 'guard error'}`}
                  tone={flag.counts ? 'error' : 'neutral'}
                  title={flagTitle(flag)}
                />
              ))}
            </div>
            {segment.text === '' ? (
              <p className="muted small">No text here, so no scanner read anything from it.</p>
            ) : folded ? (
              <details>
                <summary className="small">
                  {notCompared ? 'Show the text (the scan did not compare it)' : 'Show the text (nothing in it was marked)'}
                </summary>
                <MarkedText text={segment.text} marks={marks} />
              </details>
            ) : (
              <MarkedText text={segment.text} marks={marks} />
            )}
            {segment.truncated && (
              <p className="muted small">Only the first {segment.text.length.toLocaleString()} characters are shown.</p>
            )}
          </div>
        )
      })}
    </div>
  )
}
