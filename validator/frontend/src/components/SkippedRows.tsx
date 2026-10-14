import { useState } from 'react'
import { getSkippedPage } from '../api'
import { basename, describePosition, describeSkipReason, fileLabels } from '../origin'
import type { RunSummary, SkippedItem } from '../types'
import { useRequest } from '../usePreview'
import SkippedEntryDetails from './SkippedEntryDetails'

const PAGE_SIZE = 20

// The rows a conversion skipped, with the reason for each: one page of
// skipped_rows.ndjson at a time, filtered by reason or by original file. Opening a row
// shows the saved copy next to the original as it is now.
//
// Safety: every value shown here comes from a file on disk and renders as plain React
// text, never as markup.
export default function SkippedRows({ runDir, summary }: { runDir: string; summary: RunSummary }) {
  const skippedFile = summary.rows.skipped_file
  const [reason, setReason] = useState<string | null>(null)
  const [file, setFile] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<SkippedItem | null>(null)

  const request = { run_dir: runDir, page, page_size: PAGE_SIZE, reason, file }
  // Nothing is asked for when no row was skipped.
  const { data, error, loading } = useRequest(skippedFile.entries === 0 ? null : JSON.stringify(request), () =>
    getSkippedPage(request),
  )

  function changeReason(value: string) {
    setReason(value || null)
    setPage(1)
    setSelected(null)
  }

  function changeFile(value: string) {
    setFile(value || null)
    setPage(1)
    setSelected(null)
  }

  function changePage(next: number) {
    setPage(next)
    setSelected(null)
  }

  if (skippedFile.entries === 0) return <p className="muted">No rows were skipped.</p>

  const labels = fileLabels(Object.keys(skippedFile.by_file))
  // Null when both filters are set, because summary.json only counts each one alone.
  const totalPages = data !== null && data.total !== null ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : null

  return (
    <div>
      <div className="run-filters">
        <label className="inline">
          Reason
          <select value={reason ?? ''} onChange={(event) => changeReason(event.target.value)}>
            <option value="">All ({skippedFile.entries.toLocaleString()})</option>
            {Object.entries(skippedFile.by_reason).map(([name, count]) => (
              <option key={name} value={name}>
                {name} ({count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          File
          <select value={file ?? ''} onChange={(event) => changeFile(event.target.value)}>
            <option value="">All files</option>
            {Object.entries(skippedFile.by_file).map(([path, count]) => (
              <option key={path} value={path}>
                {labels[path]} ({count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>
        <span className="run-pager">
          <button type="button" className="secondary" disabled={loading || page <= 1} onClick={() => changePage(page - 1)}>
            ‹ Prev
          </button>
          <span className="small">{totalPages !== null ? `Page ${page} of ${totalPages}` : `Page ${page}`}</span>
          <button
            type="button"
            className="secondary"
            disabled={loading || !data?.has_next}
            onClick={() => changePage(page + 1)}
          >
            Next ›
          </button>
        </span>
      </div>

      {error !== null && <p className="error">{error}</p>}
      {data !== null && data.entries.length === 0 && <p className="muted">No skipped rows match.</p>}
      {data !== null && data.entries.length > 0 && (
        <div className={loading ? 'skipped-table-wrap loading' : 'skipped-table-wrap'}>
          <table className="skipped-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Original file</th>
                <th>Where</th>
                <th>Reason</th>
                <th>Preview</th>
              </tr>
            </thead>
            <tbody>
              {data.entries.map((item) => (
                <tr
                  key={item.number}
                  className={selected?.number === item.number ? 'selected' : undefined}
                  onClick={() => setSelected(item)}
                >
                  <td>
                    <button type="button" className="link-button" onClick={() => setSelected(item)}>
                      {item.number}
                    </button>
                  </td>
                  <td title={item.entry._origin.path}>{basename(item.entry._origin.path)}</td>
                  <td>{describePosition(item.entry._origin)}</td>
                  <td>{describeSkipReason(item.entry)}</td>
                  <td className="skipped-preview" title={item.preview}>
                    {item.preview}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected !== null && (
        <SkippedEntryDetails key={`${runDir}:${selected.number}`} entry={selected.entry} runDir={runDir} />
      )}
    </div>
  )
}
