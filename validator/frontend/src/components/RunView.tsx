import { useEffect, useState } from 'react'
import { ApiError, getRunProgress, getSkippedPage, openRun } from '../api'
import { formatBytes } from '../format'
import { basename, describePosition, describeSkipReason, fileLabels } from '../origin'
import type { Origin, RunProgressResponse, RunResponse, SkippedItem, SkippedPageResponse } from '../types'
import PreparedRows from './PreparedRows'
import RunFilesTable from './RunFilesTable'
import RunProgressPanel from './RunProgressPanel'
import RunsList from './RunsList'
import SkippedEntryDetails from './SkippedEntryDetails'

// The "Prepared run" view: pick a run from the runs root (or type the path
// of its output folder). A run that hasn't finished shows its progress; a
// finished one shows its totals, its original files, warnings, settings and
// output, then lets you page through the rows it skipped or the Prepared
// rows it wrote. Read-only -- it only reads the run's own progress.json,
// summary.json, run_identity.json, skipped_rows.ndjson and written files
// (through the backend).
//
// Safety: every value shown here comes from files on disk and renders as
// plain React text, never as markup.

const PAGE_SIZE = 50

// The run's settings that matter most when reading its results, in the
// order they're shown. Every other setting follows, as stored.
const MAIN_SETTINGS = ['map', 'require', 'exclude_metadata', 'split', 'max_rows']

interface PageOutcome {
  key: string
  data: SkippedPageResponse | null
  error: string | null
}

function formatSetting(value: unknown): string {
  if (value === null || value === undefined) return '(not set)'
  if (Array.isArray(value)) {
    if (value.length === 0) return '(none)'
    return value.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))).join(', ')
  }
  if (typeof value === 'string') return value === '' ? '(empty)' : value
  return JSON.stringify(value)
}

function RunSettings({ run }: { run: RunResponse }) {
  const settings = run.run_identity?.settings
  if (settings === null || typeof settings !== 'object' || Array.isArray(settings)) {
    return (
      <p className="muted small">
        This folder has no usable <code>run_identity.json</code>, so the run's settings aren't listed.
      </p>
    )
  }
  const names = Object.keys(settings)
  const ordered = [
    ...MAIN_SETTINGS.filter((name) => names.includes(name)),
    ...names.filter((name) => !MAIN_SETTINGS.includes(name)),
  ]

  return (
    <div className="run-settings">
      <h3>Settings</h3>
      <table className="counts-table">
        <tbody>
          {ordered.map((name) => (
            <tr key={name}>
              <th scope="row">{name}</th>
              <td className="mono location-text">{formatSetting(settings[name])}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RunSummaryBlock({ run }: { run: RunResponse }) {
  const { summary } = run
  const blankLines = summary.source.files.reduce((sum, file) => sum + (file.blank_lines ?? 0), 0)
  const hasLineCounts = summary.source.files.some((file) => file.blank_lines !== undefined)
  const mapping = Object.entries(summary.input.map)
  const { text_coverage: coverage, output } = summary

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
      <p className="small">
        Output: {output.files.toLocaleString()} file{output.files === 1 ? '' : 's'},{' '}
        {output.rows_on_disk.toLocaleString()} rows, {formatBytes(output.bytes_on_disk)} on disk
        <br />
        Text coverage: {coverage.rows_with_text.toLocaleString()} of {summary.rows.written.toLocaleString()}{' '}
        written rows have scannable text ({(coverage.coverage * 100).toFixed(1)}%)
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
      {summary.checks.warnings.length > 0 && (
        <div className="banner banner-warning small">
          Warnings:
          <ul className="lookup-differences">
            {summary.checks.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}
      <RunFilesTable files={summary.source.files} />
      <RunSettings run={run} />
    </div>
  )
}

export default function RunView({
  onOpenInPreview,
  requestedRun,
}: {
  onOpenInPreview: (origin: Origin) => void
  // A folder another view asked to have opened ("Open results"); a new
  // object each time, so asking again opens it again.
  requestedRun: { runDir: string } | null
}) {
  const [pathInput, setPathInput] = useState('')
  const [run, setRun] = useState<RunResponse | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [runLoading, setRunLoading] = useState(false)
  // Set instead of `run` while the opened folder holds a run that hasn't
  // finished (or has failed).
  const [watching, setWatching] = useState<RunProgressResponse | null>(null)

  const [reason, setReason] = useState<string | null>(null)
  const [file, setFile] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageOutcome, setPageOutcome] = useState<PageOutcome | null>(null)
  const [selected, setSelected] = useState<SkippedItem | null>(null)
  const [tab, setTab] = useState<'skipped' | 'prepared'>('skipped')

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
      // A progress.json that isn't `done` wins over any summary.json beside
      // it, which is from an earlier run in the same folder.
      const progress = await getRunProgress(path)
      if (progress.progress !== null && progress.progress.phase !== 'done') {
        setRun(null)
        setPageOutcome(null)
        setSelected(null)
        setWatching(progress)
        return
      }
      const opened = await openRun(path)
      setWatching(null)
      setRun(opened)
      setReason(null)
      setFile(null)
      setPage(1)
      setPageOutcome(null)
      setSelected(null)
      setTab('skipped')
    } catch (caught) {
      setWatching(null)
      setRun(null)
      setPageOutcome(null)
      setSelected(null)
      setRunError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setRunLoading(false)
    }
  }

  // Opens a run clicked in the runs list the way the path box does.
  function handleOpenFromList(runDir: string) {
    if (runLoading) return
    setPathInput(runDir)
    void handleOpen(runDir)
  }

  useEffect(() => {
    if (requestedRun === null) return
    setPathInput(requestedRun.runDir)
    void handleOpen(requestedRun.runDir)
    // Only a new request opens a folder; handleOpen is a new function on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedRun])

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
  // Older summaries don't list the files a run wrote.
  const preparedFiles = run?.summary.output.files_written

  return (
    <div className="run-view">
      <RunsList onOpen={handleOpenFromList} />

      <form
        className="path-form"
        onSubmit={(event) => {
          event.preventDefault()
          if (pathInput.trim()) void handleOpen(pathInput.trim())
        }}
      >
        <label htmlFor="run-path-input">
          Prepared output folder (the one with summary.json, or progress.json while it runs)
        </label>
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
          <code>progress.json</code>, <code>summary.json</code>, <code>run_identity.json</code>,{' '}
          <code>skipped_rows.ndjson</code> and the Prepared files it wrote.
        </p>
      </form>

      {runError && <p className="error">{runError}</p>}

      {watching && (
        <RunProgressPanel
          key={`${watching.run_dir}|${watching.progress?.updated_at}`}
          initial={watching}
          onDone={(runDir) => void handleOpen(runDir)}
        />
      )}

      {run && skippedFile && (
        <>
          <RunSummaryBlock run={run} />

          <div className="row-tabs run-tabs">
            <button
              type="button"
              className={tab === 'skipped' ? 'row-tab selected' : 'row-tab'}
              onClick={() => setTab('skipped')}
            >
              Skipped rows ({skippedFile.entries.toLocaleString()})
            </button>
            <button
              type="button"
              className={tab === 'prepared' ? 'row-tab selected' : 'row-tab'}
              onClick={() => setTab('prepared')}
            >
              Prepared rows ({run.summary.output.rows_on_disk.toLocaleString()})
            </button>
          </div>

          <div hidden={tab !== 'skipped'}>
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
                <span className="small">
                  {totalPages !== null ? `Page ${page} of ${totalPages}` : `Page ${page}`}
                </span>
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
          </div>

          <div hidden={tab !== 'prepared'}>
            <h3>Prepared rows ({run.summary.output.rows_on_disk.toLocaleString()})</h3>
            {preparedFiles === undefined ? (
              <p className="muted">
                This run's <code>summary.json</code> doesn't list the files it wrote, so its Prepared rows can't
                be opened here.
              </p>
            ) : (
              <PreparedRows key={run.run_dir} runDir={run.run_dir} files={preparedFiles} />
            )}
          </div>
        </>
      )}
    </div>
  )
}
