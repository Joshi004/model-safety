import { useEffect, useState } from 'react'
import { ApiError, getSkippedPage, openRun } from '../api'
import { basename, describePosition, describeSkipReason } from '../origin'
import type { Origin, RunResponse, SkippedItem, SkippedPageResponse } from '../types'
import SkippedEntryDetails from './SkippedEntryDetails'

// The "Prepared run" view: open the output folder of a prepare_rows.py run,
// see its totals, and page through the rows it skipped. Read-only -- it
// only reads summary.json and skipped_rows.ndjson (through the backend).
//
// Safety: every value shown here comes from files on disk and renders as
// plain React text, never as markup.

const PAGE_SIZE = 50

interface PageOutcome {
  key: string
  data: SkippedPageResponse | null
  error: string | null
}

// The file filter lists each original file by its name; if two files share
// a name, the full path is used for every file so they can be told apart.
function fileLabels(paths: string[]): Record<string, string> {
  const names = paths.map(basename)
  const hasDuplicates = new Set(names).size !== names.length
  return Object.fromEntries(paths.map((path, index) => [path, hasDuplicates ? path : names[index]]))
}

function RunSummaryBlock({ run }: { run: RunResponse }) {
  const { summary } = run
  const blankLines = summary.source.files.reduce((sum, file) => sum + (file.blank_lines ?? 0), 0)
  const hasLineCounts = summary.source.files.some((file) => file.blank_lines !== undefined)
  const mapping = Object.entries(summary.input.map)

  return (
    <div className="run-summary">
      <p>
        <strong>Run:</strong> <span className="mono location-text">{run.run_dir}</span>
      </p>
      <p>
        Read {summary.source.rows_read.toLocaleString()} · Written {summary.rows.written.toLocaleString()} ·
        Skipped {summary.rows.skipped.toLocaleString()}
        {hasLineCounts && <> · Blank lines {blankLines.toLocaleString()}</>}
      </p>
      <p className="muted small">
        Input: <span className="mono">{summary.input.path}</span> ({summary.input.type})
        {mapping.length > 0 && <> · mapping: {mapping.map(([target, source]) => `${target} ← ${source}`).join(', ')}</>}
        {summary.input.require.length > 0 && <> · required: {summary.input.require.join(', ')}</>}
      </p>
      {summary.checks.passed ? (
        <p className="small">All of this run's own checks passed.</p>
      ) : (
        <div className="banner banner-error small">
          This run's own checks failed:
          <ul className="lookup-differences">
            {summary.checks.failures.map((failure) => (
              <li key={failure}>{failure}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export default function RunView({ onOpenInPreview }: { onOpenInPreview: (origin: Origin) => void }) {
  const [pathInput, setPathInput] = useState('')
  const [run, setRun] = useState<RunResponse | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [runLoading, setRunLoading] = useState(false)

  const [reason, setReason] = useState<string | null>(null)
  const [file, setFile] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageOutcome, setPageOutcome] = useState<PageOutcome | null>(null)
  const [selected, setSelected] = useState<SkippedItem | null>(null)

  // What the current page request is for, so an answer for an older request
  // is never shown for it -- and "loading" is just "no answer for this yet".
  const pageKey = run ? JSON.stringify([run.run_dir, page, reason, file]) : null
  const pageLoading = pageKey !== null && pageOutcome?.key !== pageKey
  const pageData = pageOutcome?.data ?? null
  const pageError = pageOutcome?.key === pageKey ? pageOutcome.error : null

  async function handleOpen(path: string) {
    setRunLoading(true)
    setRunError(null)
    try {
      const opened = await openRun(path)
      setRun(opened)
      setReason(null)
      setFile(null)
      setPage(1)
      setPageOutcome(null)
      setSelected(null)
    } catch (caught) {
      setRun(null)
      setPageOutcome(null)
      setSelected(null)
      setRunError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setRunLoading(false)
    }
  }

  useEffect(() => {
    if (!run || pageKey === null) return
    let cancelled = false
    getSkippedPage({ run_dir: run.run_dir, page, page_size: PAGE_SIZE, reason, file })
      .then((response) => {
        if (!cancelled) setPageOutcome({ key: pageKey, data: response, error: null })
      })
      .catch((caught: unknown) => {
        const message = caught instanceof ApiError ? caught.message : String(caught)
        if (!cancelled) setPageOutcome({ key: pageKey, data: null, error: message })
      })
    return () => {
      cancelled = true
    }
  }, [run, page, reason, file, pageKey])

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

  const skippedFile = run?.summary.rows.skipped_file
  const labels = skippedFile ? fileLabels(Object.keys(skippedFile.by_file)) : {}
  // The count for the current filters; null when both are set, because
  // summary.json only counts each filter on its own.
  const headingCount = pageData === null ? (skippedFile?.entries ?? null) : pageData.total
  const totalPages = pageData && pageData.total !== null ? Math.max(1, Math.ceil(pageData.total / PAGE_SIZE)) : null

  return (
    <div className="run-view">
      <form
        className="path-form"
        onSubmit={(event) => {
          event.preventDefault()
          if (pathInput.trim()) void handleOpen(pathInput.trim())
        }}
      >
        <label htmlFor="run-path-input">Prepared output folder (the one with summary.json)</label>
        <div className="path-form-row">
          <input
            id="run-path-input"
            type="text"
            value={pathInput}
            onChange={(event) => setPathInput(event.target.value)}
            placeholder="/path/to/converted/my_dataset"
            autoComplete="off"
            spellCheck={false}
          />
          <button type="submit" disabled={runLoading || !pathInput.trim()}>
            {runLoading ? 'Opening…' : 'Open run'}
          </button>
        </div>
        <p className="muted small">
          The output folder of a <code>prepare_rows.py</code> run. Read-only: this only reads its{' '}
          <code>summary.json</code> and <code>skipped_rows.ndjson</code>.
        </p>
      </form>

      {runError && <p className="error">{runError}</p>}

      {run && skippedFile && (
        <>
          <RunSummaryBlock run={run} />

          <h3>Skipped rows{headingCount !== null && ` (${headingCount.toLocaleString()})`}</h3>
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
              <button
                type="button"
                className="secondary"
                disabled={pageLoading || page <= 1}
                onClick={() => changePage(page - 1)}
              >
                ‹ Prev
              </button>
              <span className="small">{totalPages !== null ? `Page ${page} of ${totalPages}` : `Page ${page}`}</span>
              <button
                type="button"
                className="secondary"
                disabled={pageLoading || !pageData?.has_next}
                onClick={() => changePage(page + 1)}
              >
                Next ›
              </button>
            </span>
          </div>

          {pageError && <p className="error">{pageError}</p>}
          {pageData && pageData.entries.length === 0 && <p className="muted">No skipped rows match.</p>}
          {pageData && pageData.entries.length > 0 && (
            <div className={pageLoading ? 'skipped-table-wrap loading' : 'skipped-table-wrap'}>
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
                  {pageData.entries.map((item) => (
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

          {selected && (
            <SkippedEntryDetails
              key={`${run.run_dir}:${selected.number}`}
              entry={selected.entry}
              runDir={run.run_dir}
              onOpenInPreview={onOpenInPreview}
            />
          )}
        </>
      )}
    </div>
  )
}
