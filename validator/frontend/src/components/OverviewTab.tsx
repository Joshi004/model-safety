import { useMemo } from 'react'
import { formatBytes, formatCount, formatTimestamp } from '../format'
import { collectKeysInOrder } from '../mapping'
import { choiceFromConversion, defaultChoice } from '../source'
import type { Dataset, InspectResult, PreviewResponse } from '../types'
import { useInspect, usePreview } from '../usePreview'
import RecordView from './RecordView'

const SAMPLE_ROWS = 2

// The field names of a sample: the keys of its rows, or else the names a Parquet or
// HuggingFace schema declares.
function fieldNames(preview: PreviewResponse): string[] {
  const fromRows = collectKeysInOrder(preview.input_rows)
  if (fromRows.length > 0) return fromRows
  const schema = preview.source_info.declared_schema
  if (schema !== null && typeof schema === 'object' && 'fields' in schema) {
    const fields = (schema as { fields: unknown }).fields
    if (fields !== null && typeof fields === 'object') return Object.keys(fields)
  }
  return []
}

function describeFormats(inspect: InspectResult): string {
  if (inspect.groups.length === 0) return 'Nothing was found.'
  return inspect.groups
    .map((group) => {
      const files = `${group.file_count.toLocaleString()} ${group.file_count === 1 ? 'file' : 'files'}`
      return `${group.format}${group.input_type === null ? ' (can\'t be read)' : ''}: ${files}`
    })
    .join('; ')
}

// What a dataset is, read without starting anything: its format, files, size, rows,
// fields, two sample rows, and the notes saved with it. Everything shown is plain text.
export default function OverviewTab({ dataset }: { dataset: Dataset }) {
  const inspect = useInspect(dataset.available ? dataset.path : null)
  const choice = useMemo(() => {
    if (dataset.conversion !== null) return choiceFromConversion(dataset.conversion)
    return inspect.data === null ? null : defaultChoice(inspect.data)
  }, [dataset.conversion, inspect.data])
  const preview = usePreview(
    choice === null ? null : { path: dataset.path, ...choice, max_rows: SAMPLE_ROWS },
  )

  const totalFiles = inspect.data?.groups.reduce((sum, group) => sum + group.file_count, 0) ?? 0
  const totalBytes = inspect.data?.groups.reduce((sum, group) => sum + group.total_bytes, 0) ?? 0
  const expectedRows = preview.data?.source_info.expected_rows ?? null
  const fields = preview.data === null ? [] : fieldNames(preview.data)

  return (
    <>
      {!dataset.available && (
        <div className="banner banner-error" role="alert">
          The original data can't be read, so its format and rows can't be shown.{' '}
          {dataset.unavailable_reason}
        </div>
      )}

      <div className="card">
        <h2>Overview</h2>
        {inspect.loading && dataset.available && <p className="muted">Looking at the data…</p>}
        {inspect.error !== null && <p className="error">{inspect.error}</p>}

        <dl className="facts">
          <dt>Format</dt>
          <dd>
            {dataset.conversion !== null && (
              <>
                {dataset.conversion.input_type} (saved conversion settings)
                <br />
              </>
            )}
            {inspect.data !== null ? describeFormats(inspect.data) : <span className="muted">—</span>}
          </dd>
          <dt>Files</dt>
          <dd>
            {inspect.data !== null ? formatCount(totalFiles) : '—'}
            {inspect.data?.walk_truncated && <span className="muted"> (only part of the folder was looked at)</span>}
          </dd>
          <dt>Size on disk</dt>
          <dd>{inspect.data !== null ? formatBytes(totalBytes) : '—'}</dd>
          <dt>Rows</dt>
          <dd>
            {dataset.prepared !== null ? (
              <>
                {formatCount(dataset.prepared.rows)} <span className="muted">(in the converted data)</span>
              </>
            ) : expectedRows !== null ? (
              <>
                {formatCount(expectedRows)} <span className="muted">({preview.data?.source_info.expected_rows_from})</span>
              </>
            ) : (
              <span className="muted">Known after conversion</span>
            )}
          </dd>
          <dt>Fields</dt>
          <dd>
            {fields.length > 0 ? (
              <span className="mono">{fields.join(', ')}</span>
            ) : (
              <span className="muted">{preview.loading ? 'Reading a sample…' : '—'}</span>
            )}
          </dd>
          {dataset.prepared !== null && (
            <>
              <dt>Converted</dt>
              <dd>
                {dataset.prepared.finished_at !== null ? formatTimestamp(dataset.prepared.finished_at) : '—'}
                {dataset.prepared.skipped !== null && dataset.prepared.skipped > 0 && (
                  <span className="muted"> · {formatCount(dataset.prepared.skipped)} rows skipped</span>
                )}
              </dd>
            </>
          )}
        </dl>

        {inspect.data !== null && choice === null && (
          <div className="banner banner-warning">
            Nothing in here can be converted yet: no reader understands these files.
          </div>
        )}
      </div>

      {(dataset.description !== '' || dataset.notes !== '') && (
        <div className="card">
          <h2>Notes</h2>
          {dataset.description !== '' && <p className="notes-text">{dataset.description}</p>}
          {dataset.notes !== '' && <p className="notes-text">{dataset.notes}</p>}
        </div>
      )}

      <div className="card">
        <h2>Sample rows</h2>
        {preview.error !== null && <p className="error">{preview.error}</p>}
        {preview.data === null && preview.error === null && choice !== null && <p className="muted">Reading a sample…</p>}
        {preview.data !== null && preview.data.input_rows.length === 0 && <p className="muted">No rows in the sample.</p>}
        {preview.data !== null &&
          preview.data.input_rows.map((row, index) => (
            <div key={index} className="sample-row">
              <h3>Row {index + 1}</h3>
              <RecordView record={row} written={undefined} showScannerCoverage={false} />
            </div>
          ))}
        {choice !== null && choice.file !== null && dataset.conversion === null && (
          <p className="muted small">
            This folder holds several {choice.input_type} files and a conversion reads exactly one. The sample comes
            from <code>{choice.file}</code>; pick the file on the Conversion tab.
          </p>
        )}
      </div>
    </>
  )
}
