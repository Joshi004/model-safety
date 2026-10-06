import { useState } from 'react'
import type { SourceInfo } from '../types'

// Shows "Showing rows X–Y of Z" (or "No rows at position N" once a jump
// lands past the end), plus a small form to jump to an arbitrary row.
// `startRow`/`onJump` are 0-indexed (same as the backend's `start_row`);
// the input itself is 1-indexed to match the existing "Row 1" / "Row 2"
// tab labels, and is only converted right before calling `onJump`.
//
// The input is local state, committed only on submit (Enter or "Go") --
// not on every keystroke, same as every other control in this app ("every
// change here is a deliberate click or select, not a keystroke").
export default function RowNavigator({
  sourceInfo,
  startRow,
  sampleSize,
  onJump,
}: {
  sourceInfo: SourceInfo
  startRow: number
  sampleSize: number
  onJump: (startRow: number) => void
}) {
  const [input, setInput] = useState('')
  const totalRows = sourceInfo.expected_rows

  const rangeText =
    sampleSize === 0
      ? totalRows !== null
        ? `No rows at position ${startRow + 1} (dataset has ${totalRows.toLocaleString()} rows)`
        : `No rows at position ${startRow + 1}`
      : totalRows !== null
        ? `Showing rows ${startRow + 1}–${startRow + sampleSize} of ${totalRows.toLocaleString()}`
        : `Showing rows ${startRow + 1}–${startRow + sampleSize} of unknown`

  return (
    <div className="row-navigator">
      <div className="row-navigator-main">
        <span className="small">{rangeText}</span>
        <form
          className="row-jump-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (!input.trim()) return
            const parsed = Math.trunc(Number(input))
            if (!Number.isFinite(parsed)) return
            // Clamp to [1, totalRows] -- or just [1, ...) when totalRows
            // is unknown, matching "no artificial cap, the file's own
            // length is the real limit" (see this component's own
            // "unavailable" note below for that case).
            let oneIndexedRow = Math.max(1, parsed)
            if (totalRows !== null) oneIndexedRow = Math.min(oneIndexedRow, totalRows)
            onJump(oneIndexedRow - 1)
            setInput('')
          }}
        >
          <label className="inline small">
            Jump to row #
            <input
              type="number"
              min={1}
              step={1}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="e.g. 500"
            />
          </label>
          <button type="submit" className="secondary">
            Go
          </button>
        </form>
      </div>
      {totalRows === null && (
        <p className="muted small">
          {sourceInfo.expected_rows_from}. Jumping far into the file scans sequentially from the start.
        </p>
      )}
    </div>
  )
}
