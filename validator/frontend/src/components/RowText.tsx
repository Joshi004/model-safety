import { describeHighlight, splitPieces } from '../rows'
import type { RowHighlight, RowMessageFlag, RowSegment } from '../types'

// The text a scanner read from a row (its `text`, or each message), with the
// findings marked in it: NER spans and regex PII matches inside the text, and a
// flag on a message toxicity judged. A mark in red is a finding the filter named
// as a reason to remove the row.
//
// Safety: the text is real data and renders as plain React text, never as markup.

// A message with no marks and more text than this starts folded.
const FOLD_CHARS = 4000

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
            className={`hl ${piece.marks.map((mark) => `hl-${mark.scanner}`).join(' ')}${
              piece.marks.some((mark) => mark.caused_removal) ? ' hl-caused' : ''
            }`}
            title={piece.marks.map(describeHighlight).join('\n')}
          >
            {piece.text}
          </mark>
        ),
      )}
    </pre>
  )
}

export default function RowText({
  segments,
  highlights = [],
  flags = [],
}: {
  segments: RowSegment[]
  highlights?: RowHighlight[]
  flags?: RowMessageFlag[]
}) {
  if (segments.length === 0) return <p className="muted">This row has no text or messages.</p>
  return (
    <div className="row-text">
      {segments.map((segment) => {
        const marks = highlights.filter((mark) => mark.message_index === segment.message_index)
        const segmentFlags = flags.filter((flag) => flag.message_index === segment.message_index)
        const folded = marks.length === 0 && segment.text.length > FOLD_CHARS
        const label = segment.message_index === null ? 'text' : `${segment.role} #${segment.message_index}`
        return (
          <div key={segment.message_index ?? 'text'} className="turn-card">
            <div className="turn-header">
              <span className="turn-role-pill">{label}</span>
              <span className="muted small">{segment.length.toLocaleString()} characters</span>
              {marks.length > 0 && <span className="muted small">{marks.length.toLocaleString()} marked</span>}
              {segmentFlags.map((flag) => (
                <span
                  key={`${flag.step}|${flag.verdict}`}
                  className={flag.caused_removal ? 'state-chip state-failed' : 'state-chip state-unfinished'}
                  title={`Step ${flag.step}, toxicity${flag.categories?.length ? `: ${flag.categories.join(', ')}` : ''}${
                    flag.caused_removal ? ' (caused the removal)' : ''
                  }`}
                >
                  toxicity: {flag.verdict ?? flag.outcome ?? 'flagged'}
                </span>
              ))}
            </div>
            {segment.text === '' ? (
              <p className="muted small">No text here, so no scanner read anything from it.</p>
            ) : folded ? (
              <details>
                <summary className="small">Show the text (nothing in it was marked)</summary>
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
