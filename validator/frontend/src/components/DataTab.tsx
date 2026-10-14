import { useMemo, useState } from 'react'
import { formatCount, formatTimestamp } from '../format'
import { choiceFromConversion, defaultChoice } from '../source'
import type { Dataset, DatasetPrepared } from '../types'
import { useInspect, usePreview, useRun } from '../usePreview'
import PreparedRows from './PreparedRows'
import RecordView from './RecordView'
import RowNavigator from './RowNavigator'
import SkippedRows from './SkippedRows'

// A couple of real rows, not a page: the same size as the Overview's sample.
const SAMPLE_ROWS = 2

// The original rows of a dataset, a couple at a time, with a jump-to-row box. Read-only:
// it reads a bounded sample from the original data through /api/preview.
function OriginalRows({ dataset }: { dataset: Dataset }) {
  const [startRow, setStartRow] = useState(0)
  const [selectedRowIndex, setSelectedRowIndex] = useState(0)

  const inspect = useInspect(dataset.available ? dataset.path : null)
  const choice = useMemo(() => {
    if (dataset.conversion !== null) return choiceFromConversion(dataset.conversion)
    return inspect.data === null ? null : defaultChoice(inspect.data)
  }, [dataset.conversion, inspect.data])
  const preview = usePreview(
    choice === null ? null : { path: dataset.path, ...choice, max_rows: SAMPLE_ROWS, start_row: startRow },
  )

  function jump(newStartRow: number) {
    setStartRow(newStartRow)
    setSelectedRowIndex(0)
  }

  const rows = preview.data?.input_rows ?? []
  const row = rows[selectedRowIndex]

  return (
    <div className="card">
      <h2>Original rows</h2>
      {!dataset.available && (
        <div className="banner banner-error" role="alert">
          The original data can't be read. {dataset.unavailable_reason}
        </div>
      )}
      {inspect.error !== null && <p className="error">{inspect.error}</p>}
      {inspect.data !== null && choice === null && (
        <p className="muted">Nothing in here can be converted yet, so no rows can be shown.</p>
      )}
      {preview.error !== null && <p className="error">{preview.error}</p>}
      {preview.data === null && preview.error === null && choice !== null && <p className="muted">Reading rows…</p>}

      {preview.data !== null && (
        <div className={preview.loading ? 'preview-area loading' : 'preview-area'}>
          <RowNavigator
            sourceInfo={preview.data.source_info}
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
          {row !== undefined ? (
            <RecordView record={row} written={undefined} showScannerCoverage={false} />
          ) : (
            <p className="muted">No rows at this position.</p>
          )}
        </div>
      )}
    </div>
  )
}

// The converted rows of the current Prepared version, and the rows the conversion skipped.
function PreparedData({ prepared }: { prepared: DatasetPrepared }) {
  const run = useRun(prepared.folder)
  const summary = run.data?.summary ?? null

  return (
    <>
      <div className="card">
        <h2>Converted rows</h2>
        <p className="muted small">
          Version {prepared.version_id}
          {prepared.finished_at !== null && <>, converted {formatTimestamp(prepared.finished_at)}</>}:{' '}
          {formatCount(prepared.rows)} rows written, {formatCount(prepared.skipped)} skipped.
        </p>
        {run.loading && <p className="muted">Opening the converted data…</p>}
        {run.error !== null && <p className="error">{run.error}</p>}
        {summary !== null &&
          (summary.output.files_written === undefined ? (
            <p className="muted">
              This version's <code>summary.json</code> doesn't list the files it wrote, so its rows can't be opened here.
            </p>
          ) : (
            <PreparedRows key={prepared.folder} runDir={prepared.folder} files={summary.output.files_written} />
          ))}
      </div>

      <div className="card">
        <h2>Skipped rows{summary !== null && ` (${formatCount(summary.rows.skipped_file.entries)})`}</h2>
        {summary !== null && <SkippedRows key={prepared.folder} runDir={prepared.folder} summary={summary} />}
      </div>
    </>
  )
}

// Browse the rows of a dataset: the original rows, and, once it is converted, the
// converted rows and the rows the conversion skipped, each with its reason.
export default function DataTab({ dataset }: { dataset: Dataset }) {
  return (
    <>
      <OriginalRows dataset={dataset} />
      {dataset.prepared !== null ? (
        <PreparedData prepared={dataset.prepared} />
      ) : (
        <div className="card">
          <h2>Converted rows</h2>
          <p className="muted">
            This dataset isn't converted yet. Once it is, its converted rows and the rows the conversion skipped
            appear here.
          </p>
        </div>
      )}
    </>
  )
}
