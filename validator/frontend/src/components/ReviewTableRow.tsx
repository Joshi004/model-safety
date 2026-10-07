import type { FoundEntry } from '../catalog'
import { describeBrief, describeContents } from '../jobs'
import { setSkipIfEmpty, setSource, sourceFor } from '../mapping'
import type { MappingState } from '../mapping'
import {
  attentionReasons,
  chosenGroup,
  columnsOf,
  displayName,
  isLoading,
  needsFile,
  readableIndexes,
} from '../review'
import type { ReviewRow } from '../review'
import type { CheckedRun } from '../types'
import CommandBox from './CommandBox'
import { describeRowStatus } from './OutputPreview'
import RecordView from './RecordView'

// One dataset of the batch review table: what it is, its format, its mapping,
// its output folder, what's already in that folder, and whether it can be
// started. Expanding it shows one sample row before and after the mapping,
// and, after a check, the exact commands.
//
// Safety: every value shown here comes from the backend or from files on disk
// and renders as plain React text, never as markup.

const REVIEW_COLUMNS = 6

function FormatCell({
  row,
  onChooseFormat,
  onChooseFile,
}: {
  row: ReviewRow
  onChooseFormat: (groupIndex: number | null) => void
  onChooseFile: (file: string | null) => void
}) {
  const { inspect } = row
  if (inspect === null) {
    return row.error === null ? <span className="muted small">inspecting…</span> : <span className="muted">—</span>
  }
  const readable = readableIndexes(inspect)
  if (readable.length === 0) return <span className="muted small">no readable format</span>

  const group = chosenGroup(row)
  return (
    <div className="review-format">
      {readable.length === 1 && group !== null ? (
        <div className="small">
          {group.format}
          <span className="muted"> · {group.file_count.toLocaleString()} file{group.file_count === 1 ? '' : 's'}</span>
        </div>
      ) : (
        <select
          value={row.groupIndex ?? ''}
          aria-label="Format to convert"
          onChange={(event) => onChooseFormat(event.target.value === '' ? null : Number(event.target.value))}
        >
          <option value="">Choose a format…</option>
          {readable.map((index) => (
            <option key={index} value={index}>
              {inspect.groups[index].format} ({inspect.groups[index].file_count.toLocaleString()} file
              {inspect.groups[index].file_count === 1 ? '' : 's'})
            </option>
          ))}
        </select>
      )}
      {group !== null && needsFile(row) && (
        <>
          <select
            value={row.file ?? ''}
            aria-label={`${group.format} file to use`}
            onChange={(event) => onChooseFile(event.target.value === '' ? null : event.target.value)}
          >
            <option value="">Choose a file…</option>
            {group.files.map((file) => (
              <option key={file} value={file}>
                {file}
              </option>
            ))}
          </select>
          {group.truncated && <div className="muted small">Only the first {group.files.length} files are listed.</div>}
        </>
      )}
    </div>
  )
}

function MappingCell({
  row,
  canApplyToAll,
  onMapping,
  onApplyToAll,
}: {
  row: ReviewRow
  canApplyToAll: boolean
  onMapping: (mapping: MappingState) => void
  onApplyToAll: () => void
}) {
  if (row.preview === null) {
    return <span className="muted small">{isLoading(row) ? 'loading the sample…' : '—'}</span>
  }
  const columns = columnsOf(row)
  const hints = row.preview.suggested_mapping.hints
  const mapsSomething = sourceFor(row.mapping, 'text') !== null || sourceFor(row.mapping, 'messages') !== null

  return (
    <div className="review-mapping">
      {(['text', 'messages'] as const).map((target) => {
        const source = sourceFor(row.mapping, target)
        return (
          <div key={target} className="review-mapping-line">
            <label className="inline small">
              <code>{target}</code> ←
              <select
                value={source ?? ''}
                onChange={(event) => onMapping(setSource(row.mapping, target, event.target.value === '' ? null : event.target.value))}
              >
                <option value="">(none)</option>
                {columns.map((column) => (
                  <option key={column} value={column}>
                    {column}
                  </option>
                ))}
              </select>
            </label>
            {source !== null && (
              <label className="inline small">
                <input
                  type="checkbox"
                  checked={row.mapping.skipIfEmpty[target]}
                  onChange={(event) => onMapping(setSkipIfEmpty(row.mapping, target, event.target.checked))}
                />
                skip if empty
              </label>
            )}
          </div>
        )
      })}
      <button type="button" className="secondary" disabled={!canApplyToAll || !mapsSomething} onClick={onApplyToAll}>
        Apply this mapping to all
      </button>
      {hints.length > 0 && (
        <details className="review-hints">
          <summary className="small">
            {hints.length} mapping hint{hints.length === 1 ? '' : 's'}
          </summary>
          {hints.map((hint, index) => (
            <p key={index} className="field-hint small">
              {hint.message}
            </p>
          ))}
        </details>
      )}
    </div>
  )
}

function FolderContentsLine({ checked }: { checked: CheckedRun }) {
  if (checked.contents === null) return null
  return (
    <div className="small">
      In the folder now: {describeContents(checked.contents)}.
      {checked.contents.summary && <> {describeBrief(checked.contents.summary)}</>}
    </div>
  )
}

function FolderStatusCell({
  checked,
  runAgain,
  onRunAgain,
  onOpenResults,
  onOpenBatch,
}: {
  checked: CheckedRun | null
  runAgain: boolean
  onRunAgain: (runAgain: boolean) => void
  onOpenResults: (runDir: string) => void
  onOpenBatch: (batchId: string) => void
}) {
  if (checked === null) return <span className="muted small">not checked yet</span>

  const openResults =
    checked.run_dir !== null && checked.contents?.summary ? (
      <button type="button" className="secondary" onClick={() => onOpenResults(checked.run_dir ?? '')}>
        Open results
      </button>
    ) : null

  switch (checked.status) {
    case 'empty':
      return <span className="run-status-passed small">Empty or new: ready.</span>
    case 'done_same':
      return (
        <div>
          <div className="small">Already done, will be skipped.</div>
          <FolderContentsLine checked={checked} />
          <label className="inline small">
            <input type="checkbox" checked={runAgain} onChange={(event) => onRunAgain(event.target.checked)} />
            Run it again
          </label>
          {openResults}
        </div>
      )
    case 'same_unfinished':
      return (
        <div>
          <div className="small">An unfinished (or failed) run with the same settings is here; starting runs over it.</div>
          <FolderContentsLine checked={checked} />
        </div>
      )
    case 'different':
    case 'no_identity':
      return (
        <div>
          <div className="run-status-failed small">
            {checked.status === 'different'
              ? 'Holds a different run: delete it or choose another folder.'
              : 'Holds files but no run_identity.json: delete it or choose another folder.'}
          </div>
          <FolderContentsLine checked={checked} />
          {checked.differences.length > 0 && (
            <details>
              <summary className="small">What changed (previous → requested)</summary>
              <ul className="lookup-differences">
                {checked.differences.map((line) => (
                  <li key={line} className="mono location-text small">
                    {line}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {openResults}
        </div>
      )
    case 'running':
      return (
        <div>
          <div className="run-status-failed small">A job is writing here now: {checked.reason}</div>
          {checked.batch_id && (
            <button type="button" className="secondary" onClick={() => onOpenBatch(checked.batch_id ?? '')}>
              Open its batch
            </button>
          )}
        </div>
      )
    default:
      return <div className="run-status-failed small">{checked.reason}</div>
  }
}

function ExpandedRow({ row, checked }: { row: ReviewRow; checked: CheckedRun | null }) {
  const { preview } = row
  if (preview === null) return <p className="muted">The sample hasn't loaded yet.</p>

  const original = preview.input_rows[0]
  const written = preview.preview.rows.find((previewRow) => previewRow.row_index === 0)
  return (
    <div>
      {original === undefined ? (
        <p className="muted">No rows in this sample.</p>
      ) : (
        <div className="review-expanded-columns">
          <div>
            <h4>Original row 1</h4>
            <RecordView record={original} written={undefined} showScannerCoverage={false} />
          </div>
          <div>
            <h4>
              After this mapping: {isLoading(row) ? 'updating…' : describeRowStatus(preview.preview, 0)}
            </h4>
            {written ? (
              <RecordView record={written.prepared} written={written} />
            ) : (
              <p className="muted small">
                {isLoading(row) ? 'The preview is being updated.' : 'This row is not written with this mapping.'}
              </p>
            )}
          </div>
        </div>
      )}
      {checked?.sbatch_command && (
        <CommandBox
          title="sbatch command"
          command={checked.sbatch_command}
          note="Start submits exactly this for this dataset, from the repo root."
        />
      )}
      {checked?.command && (
        <CommandBox
          title="Local command"
          command={checked.command}
          note="The same arguments, run on this machine instead of through SLURM. The UI never runs it."
        />
      )}
    </div>
  )
}

export default function ReviewTableRow({
  row,
  found,
  checked,
  expanded,
  canApplyToAll,
  onToggleExpanded,
  onChooseFormat,
  onChooseFile,
  onMapping,
  onFolder,
  onRunAgain,
  onApplyToAll,
  onRetry,
  onRemove,
  onOpenResults,
  onOpenBatch,
}: {
  row: ReviewRow
  found: FoundEntry | null
  // This row's entry of the latest folder check, when that check is current.
  checked: CheckedRun | null
  expanded: boolean
  canApplyToAll: boolean
  onToggleExpanded: () => void
  onChooseFormat: (groupIndex: number | null) => void
  onChooseFile: (file: string | null) => void
  onMapping: (mapping: MappingState) => void
  onFolder: (folder: string) => void
  onRunAgain: (runAgain: boolean) => void
  onApplyToAll: () => void
  onRetry: () => void
  onRemove: () => void
  onOpenResults: (runDir: string) => void
  onOpenBatch: (batchId: string) => void
}) {
  const reasons = attentionReasons(row)
  const loading = isLoading(row)

  return (
    <>
      <tr>
        <td>
          <button type="button" className="link-button" aria-expanded={expanded} onClick={onToggleExpanded}>
            {expanded ? '▾' : '▸'} {displayName(row, found)}
          </button>
          {found && (
            <div className="muted small">
              {found.team.team} / {found.group.name}
            </div>
          )}
          <div className="muted small mono location-text">{row.path}</div>
        </td>
        <td>
          <FormatCell row={row} onChooseFormat={onChooseFormat} onChooseFile={onChooseFile} />
        </td>
        <td>
          <MappingCell row={row} canApplyToAll={canApplyToAll} onMapping={onMapping} onApplyToAll={onApplyToAll} />
        </td>
        <td>
          <input
            type="text"
            className="mono review-folder"
            value={row.folder ?? ''}
            disabled={row.folder === null}
            placeholder={loading ? 'working out a name…' : '—'}
            aria-label="Output folder under the runs root"
            onChange={(event) => onFolder(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </td>
        <td>
          <FolderStatusCell
            checked={checked}
            runAgain={row.runAgain}
            onRunAgain={onRunAgain}
            onOpenResults={onOpenResults}
            onOpenBatch={onOpenBatch}
          />
        </td>
        <td className="job-actions">
          {loading && <div className="muted small">loading…</div>}
          {reasons.map((reason) => (
            <div key={reason} className="run-status-failed small job-error">
              Needs attention: {reason}
            </div>
          ))}
          {!loading && reasons.length === 0 && <div className="run-status-passed small">Ready</div>}
          {row.error !== null && (
            <button type="button" className="secondary" onClick={onRetry}>
              Try again
            </button>
          )}
          <button type="button" className="secondary" onClick={onRemove}>
            Remove from batch
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="review-expanded-row">
          <td colSpan={REVIEW_COLUMNS}>
            <ExpandedRow row={row} checked={checked} />
          </td>
        </tr>
      )}
    </>
  )
}
