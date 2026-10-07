import { useEffect, useState } from 'react'
import { ApiError, runPreview } from '../api'
import { getOrigin } from '../origin'
import type { PreviewResponse, WrittenFile } from '../types'
import { ShowOriginal } from './OriginalLookup'
import RecordView from './RecordView'
import RowNavigator from './RowNavigator'

// The Prepared rows a run wrote: pick one of its output files and step
// through its rows, each shown with "Show original". It reads them through
// /api/preview with the run folder as the path and the written file as the
// file, so no row is changed or written.
//
// Safety: every row shown here comes from a file on disk and renders as
// plain React text, never as markup.

// Same size as the Inspect view's sample: a couple of real rows, not a page.
const SAMPLE_ROWS = 2

interface Outcome {
  key: string
  result: PreviewResponse | null
  error: string | null
}

export default function PreparedRows({ runDir, files }: { runDir: string; files: WrittenFile[] }) {
  const [filePath, setFilePath] = useState(files[0]?.path ?? '')
  const [startRow, setStartRow] = useState(0)
  const [selectedRowIndex, setSelectedRowIndex] = useState(0)
  const [outcome, setOutcome] = useState<Outcome | null>(null)

  // What the current request is for, so an answer for an older request is
  // never shown for it -- and "loading" is just "no answer for this yet".
  const requestKey = JSON.stringify([runDir, filePath, startRow])

  useEffect(() => {
    if (!filePath) return
    let cancelled = false
    runPreview({
      path: runDir,
      input_type: 'jsonl',
      file: filePath,
      max_rows: SAMPLE_ROWS,
      start_row: startRow,
    })
      .then((response) => {
        if (!cancelled) setOutcome({ key: requestKey, result: response, error: null })
      })
      .catch((caught: unknown) => {
        const message = caught instanceof ApiError ? caught.message : String(caught)
        if (!cancelled) setOutcome({ key: requestKey, result: null, error: message })
      })
    return () => {
      cancelled = true
    }
  }, [runDir, filePath, startRow, requestKey])

  if (files.length === 0) {
    return <p className="muted">This run wrote no Prepared rows.</p>
  }

  const selectedFile = files.find((file) => file.path === filePath) ?? files[0]
  const current = outcome?.key === requestKey ? outcome : null
  const result = current?.result ?? null
  const rows = result?.input_rows ?? []
  const row = rows[selectedRowIndex]
  const origin = row ? getOrigin(row) : null

  function changeFile(path: string) {
    setFilePath(path)
    setStartRow(0)
    setSelectedRowIndex(0)
  }

  function jump(newStartRow: number) {
    setStartRow(newStartRow)
    setSelectedRowIndex(0)
  }

  return (
    <div className="prepared-rows">
      <div className="run-filters">
        <label className="inline">
          Prepared file
          <select value={selectedFile.path} onChange={(event) => changeFile(event.target.value)}>
            {files.map((file) => (
              <option key={file.path} value={file.path}>
                {file.path} ({file.rows.toLocaleString()} rows)
              </option>
            ))}
          </select>
        </label>
      </div>

      {current === null && <p className="muted small">Loading rows…</p>}
      {current?.error && <p className="error">{current.error}</p>}

      {result && (
        <>
          <RowNavigator
            sourceInfo={{
              ...result.source_info,
              expected_rows: selectedFile.rows,
              expected_rows_from: "the run's summary.json",
            }}
            startRow={startRow}
            sampleSize={rows.length}
            onJump={jump}
          />
          <div className="row-tabs">
            {rows.map((_, index) => (
              <button
                key={index}
                type="button"
                className={index === selectedRowIndex ? 'row-tab selected' : 'row-tab'}
                onClick={() => setSelectedRowIndex(index)}
              >
                Row {startRow + index + 1}
              </button>
            ))}
          </div>
          {origin && (
            <ShowOriginal
              key={`${filePath}|${startRow}|${selectedRowIndex}`}
              origin={origin}
              runDir={runDir}
            />
          )}
          {row ? (
            <RecordView record={row} written={undefined} showScannerCoverage={false} />
          ) : (
            <p className="muted">No rows at this position.</p>
          )}
        </>
      )}
    </div>
  )
}
