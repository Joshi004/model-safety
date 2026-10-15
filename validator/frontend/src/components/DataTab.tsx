import { useMemo, useState } from 'react'
import { reasonLabel } from '../flagged'
import { formatCount, formatTimestamp } from '../format'
import { choiceFromConversion, defaultChoice } from '../source'
import type { Dataset, DatasetPrepared, FilterRunSummary } from '../types'
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
      {dataset.parent !== null && (
        <p className="muted small">
          These are the original rows of the data this dataset was made from, all of them: the rows that were dropped
          are still among them.
        </p>
      )}
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

// What a filtered dataset's version says about the rows its filter dropped: how many, and why.
// The dropped rows themselves are in the version folder's `removed_rows.ndjson`.
function DroppedRows({ summary, parentName }: { summary: FilterRunSummary; parentName: string | null }) {
  const reasons = Object.entries(summary.removed_by_reason)
  return (
    <>
      <p>
        <strong>{formatCount(summary.rows.removed)}</strong> {summary.rows.removed === 1 ? 'row was' : 'rows were'} dropped
        from {parentName ?? 'the dataset this one was made from'}, which had {formatCount(summary.source.rows_read)}.
      </p>
      {reasons.length > 0 && (
        <p className="small">
          {reasons.map(([reason, count]) => `${reasonLabel(reason)}: ${formatCount(count)}`).join(', ')}.
        </p>
      )}
      <p className="muted small">
        Each dropped row is kept, with where it was and why it was dropped, in <code>{summary.removed_rows.path}</code> in
        the version's folder.
      </p>
    </>
  )
}

// The converted rows of the current Prepared version, and the rows the conversion skipped. For a
// filtered dataset the version is a filter run: it has the rows that were left, and the rows its
// filter dropped instead of skipped rows.
function PreparedData({ prepared, parentName }: { prepared: DatasetPrepared; parentName: string | null }) {
  const run = useRun(prepared.folder)
  const preparedRun = run.data?.kind === 'prepared' ? run.data : null
  const filterRun = run.data?.kind === 'filter' ? run.data : null
  const files = (preparedRun ?? filterRun)?.summary.output.files_written

  return (
    <>
      <div className="card">
        <h2>{filterRun !== null ? 'Rows that were kept' : 'Converted rows'}</h2>
        <p className="muted small">
          Version {prepared.version_id}
          {prepared.finished_at !== null && (
            <>
              , {filterRun !== null ? 'made' : 'converted'} {formatTimestamp(prepared.finished_at)}
            </>
          )}
          : {formatCount(prepared.rows)} rows written
          {filterRun === null && <>, {formatCount(prepared.skipped)} skipped</>}.
        </p>
        {run.loading && <p className="muted">Opening the converted data…</p>}
        {run.error !== null && <p className="error">{run.error}</p>}
        {run.data !== null &&
          (files === undefined ? (
            <p className="muted">
              This version's <code>summary.json</code> doesn't list the files it wrote, so its rows can't be opened here.
            </p>
          ) : (
            <PreparedRows key={prepared.folder} runDir={prepared.folder} files={files} />
          ))}
      </div>

      {filterRun !== null ? (
        <div className="card">
          <h2>Dropped rows ({formatCount(filterRun.summary.rows.removed)})</h2>
          <DroppedRows summary={filterRun.summary} parentName={parentName} />
        </div>
      ) : (
        <div className="card">
          <h2>Skipped rows{preparedRun !== null && ` (${formatCount(preparedRun.summary.rows.skipped_file.entries)})`}</h2>
          {preparedRun !== null && (
            <SkippedRows key={prepared.folder} runDir={prepared.folder} summary={preparedRun.summary} />
          )}
        </div>
      )}
    </>
  )
}

// Browse the rows of a dataset: the original rows, and, once it is converted, the
// converted rows and the rows the conversion skipped, each with its reason. For a filtered
// dataset, the rows that were kept and the rows that were dropped.
export default function DataTab({ dataset }: { dataset: Dataset }) {
  const filtered = dataset.parent !== null
  return (
    <>
      <OriginalRows dataset={dataset} />
      {dataset.prepared !== null ? (
        <PreparedData prepared={dataset.prepared} parentName={dataset.filter?.parent_name ?? null} />
      ) : (
        <div className="card">
          <h2>{filtered ? 'Rows that were kept' : 'Converted rows'}</h2>
          <p className="muted">
            {filtered
              ? "This filtered version isn't ready yet. Once its filter job has finished, the rows that were kept and the rows that were dropped appear here."
              : "This dataset isn't converted yet. Once it is, its converted rows and the rows the conversion skipped appear here."}
          </p>
        </div>
      )}
    </>
  )
}
