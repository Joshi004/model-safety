import { useState } from 'react'
import type { MappingState } from '../mapping'
import { sourceFor } from '../mapping'
import type { PreviewResponse } from '../types'
import { describeValue } from '../describe'
import TargetFormat from './TargetFormat'
import ValueView from './ValueView'

// What each target needs, in the same plain language describeValue() uses
// for what a real value actually got mapped there -- so "needed" and
// "got" read as a matched pair. Mirrors TargetFormat.tsx's two shapes.
const TARGET_NEEDS: Record<'text' | 'messages', string> = {
  text: 'non-empty text',
  messages: 'a list of {role, content} turns',
}

export default function OutputPreview({
  result,
  mapping,
  selectedRowIndex,
}: {
  result: PreviewResponse
  mapping: MappingState
  selectedRowIndex: number
}) {
  const [showRaw, setShowRaw] = useState(false)
  const { preview } = result

  const written = preview.rows.find((row) => row.row_index === selectedRowIndex)
  const skipped = preview.skipped_rows.find((row) => row.row_index === selectedRowIndex)
  const errored = preview.write_errors.find((error) => error.row_index === selectedRowIndex)

  const metadata =
    written && typeof written.prepared.metadata === 'object' && written.prepared.metadata !== null
      ? (written.prepared.metadata as Record<string, unknown>)
      : null

  return (
    <div className="output-preview">
      <TargetFormat />

      <div className="mapping-summary small">
        <span className="muted">Mapping:</span>
        {(['text', 'messages'] as const).map((target) => {
          const source = sourceFor(mapping, target)
          return source ? (
            <span key={target} className="mapping-summary-item">
              <code>{target}</code> ← <code>{source}</code>
            </span>
          ) : null
        })}
        {!sourceFor(mapping, 'text') && !sourceFor(mapping, 'messages') && (
          <span className="muted">nothing mapped yet -- everything goes to metadata</span>
        )}
        {metadata && (
          <span className="mapping-summary-item muted">
            metadata: {Object.keys(metadata).length > 0 ? Object.keys(metadata).join(', ') : '(empty)'}
          </span>
        )}
      </div>

      {errored && (
        <div className="write-errors">
          {preview.fatal_if_run && (
            <div className="banner banner-error">
              <strong>This would stop the whole run, not just skip this row.</strong> {preview.fatal_note}
            </div>
          )}
          <div className="write-error">
            <div>
              <code>{errored.likely_column ?? '(unknown column)'}</code>
            </div>
            <div className="muted small">{errored.error}</div>
            <div className="hint-text small">{errored.hint}</div>
          </div>
        </div>
      )}

      {skipped && (
        <div className="banner banner-warning">
          Skipped: <code>{skipped.missing_column}</code> is missing or empty on this row, and "skip
          the row if this is empty" is checked for it.
        </div>
      )}

      {written && (
        <>
          {written.scanned_text_length === 0 && (
            <div className="banner banner-warning">
              <strong>Written, but the scanner reads nothing from it.</strong>
              {(['text', 'messages'] as const).map((target) => {
                const source = sourceFor(mapping, target)
                if (!source) return null
                return (
                  <div key={target} className="small">
                    <code>{target}</code> needed: {TARGET_NEEDS[target]} — got:{' '}
                    {describeValue(written.prepared[target])}
                  </div>
                )
              })}
            </div>
          )}

          <div className="output-row">
            <div className="output-col">
              <div className="output-col-header">
                <span className="muted small">Converted row</span>
                <button type="button" className="link-button" onClick={() => setShowRaw((prev) => !prev)}>
                  {showRaw ? 'show readable view' : 'show raw JSON'}
                </button>
              </div>
              {showRaw ? (
                <pre className="raw-json">{JSON.stringify(written.prepared, null, 2)}</pre>
              ) : (
                <ValueView value={written.prepared} depth={1} />
              )}
            </div>
            <div className="output-col">
              <div className="muted small">
                What the scanner reads ({written.scanned_text_length.toLocaleString()} chars)
              </div>
              <pre className="scanned-text">{written.scanned_text || '(empty)'}</pre>
            </div>
          </div>
        </>
      )}

      {!written && !skipped && !errored && (
        <p className="muted">This row isn't in the current sample.</p>
      )}
    </div>
  )
}
