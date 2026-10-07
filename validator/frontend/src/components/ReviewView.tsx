import { useEffect, useRef, useState } from 'react'
import { ApiError, checkRuns, deleteRunFolders, inspectPath, runPreview, submitRuns } from '../api'
import { findEntry } from '../catalog'
import { describeBrief, describeContents } from '../jobs'
import { toRequestResources } from '../resources'
import type { ResourceInputs } from '../resources'
import {
  BLOCKING_STATUSES,
  MAX_CONCURRENT_REQUESTS,
  applyMappingToAll,
  attentionReasons,
  chooseFile,
  chooseFormat,
  createLimiter,
  displayName,
  editFolder,
  editMapping,
  isLoading,
  newRow,
  pendingWork,
  plannedRun,
  plannedRunWithoutFolder,
  previewRequest,
  receiveError,
  receiveFolder,
  receiveInspect,
  receivePreview,
  retry,
  workKey,
} from '../review'
import type { ReviewRow } from '../review'
import type { CheckResponse, CheckedRun, JobsSettings, PlannedRun, TeamCatalog } from '../types'
import ResourcesFields from './ResourcesFields'
import ReviewTableRow from './ReviewTableRow'

// The batch review: one row per dataset selected in the sidebar, each with
// its format, mapping and output folder to review and edit, then one folder
// check for all of them, one delete for the folders the user chooses to
// clear, and one Start that submits a SLURM job per dataset as one batch.
//
// Rows fill in two requests at a time (see review.ts's pendingWork), because
// every request runs in the validator on the login node. This view stays
// mounted once opened (App.tsx hides it instead), so its rows keep loading
// and keep their edits while another view is shown.
//
// Safety: every value shown here comes from the backend or from files on
// disk and renders as plain React text, never as markup.

function describeError(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : String(caught)
}

// The text of the one confirmation before folders are deleted.
function deleteConfirmation(folders: CheckedRun[]): string {
  const lines = [`Delete ${folders.length} folder${folders.length === 1 ? '' : 's'} and everything in them?`, '']
  for (const folder of folders) {
    lines.push(folder.run_dir ?? '')
    if (folder.contents) {
      lines.push(`  ${describeContents(folder.contents)}`)
      if (folder.contents.summary) lines.push(`  ${describeBrief(folder.contents.summary)}`)
    }
  }
  lines.push('', "This can't be undone. The original data isn't touched.")
  return lines.join('\n')
}

interface AppliedToAll {
  from: string
  applied: string[]
  leftAlone: { name: string; reason: string }[]
}

export default function ReviewView({
  selection,
  teams,
  settings,
  settingsError,
  onReloadSettings,
  resources,
  onResourcesChange,
  onRemove,
  onBack,
  onOpenResults,
  onOpenBatch,
}: {
  selection: string[]
  teams: TeamCatalog[]
  settings: JobsSettings | null
  settingsError: string | null
  onReloadSettings: () => void
  resources: ResourceInputs
  onResourcesChange: (next: ResourceInputs) => void
  onRemove: (path: string) => void
  onBack: () => void
  onOpenResults: (runDir: string) => void
  onOpenBatch: (batchId: string) => void
}) {
  const [rows, setRows] = useState<ReviewRow[]>(() => selection.map(newRow))
  const rowsRef = useRef(rows)
  const [limit] = useState(() => createLimiter(MAX_CONCURRENT_REQUESTS))
  const inFlight = useRef(new Set<string>())
  // Bumped whenever a request ends, so a row whose request was skipped is looked at again.
  const [finishedRequests, setFinishedRequests] = useState(0)

  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [checked, setChecked] = useState<{ key: string; response: CheckResponse } | null>(null)
  const [busy, setBusy] = useState<'checking' | 'deleting' | 'starting' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [appliedToAll, setAppliedToAll] = useState<AppliedToAll | null>(null)

  // Rows follow the selection: a removed path drops its row, an added one
  // gets a new row that starts loading.
  useEffect(() => {
    setRows((current) => {
      const next = selection.map((path) => current.find((row) => row.path === path) ?? newRow(path))
      return next.length === current.length && next.every((row, index) => row === current[index]) ? current : next
    })
  }, [selection])

  useEffect(() => {
    rowsRef.current = rows
  }, [rows])

  // Keeps every row's next request going, two at a time across the table.
  useEffect(() => {
    for (const row of rows) startWork(row)
    // startWork only reads state through refs and the current props.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, finishedRequests])

  // Used only while the row still wants exactly this request: a row that was
  // removed or edited in the meantime ignores the response.
  function applyIfStillWanted(id: number, key: string, change: (row: ReviewRow) => ReviewRow) {
    setRows((current) => current.map((row) => (row.id === id && workKey(row) === key ? change(row) : row)))
  }

  function startWork(row: ReviewRow) {
    const work = pendingWork(row)
    const key = workKey(row)
    if (work === null || key === null) return
    const flightKey = `${row.id}|${key}`
    if (inFlight.current.has(flightKey)) return
    inFlight.current.add(flightKey)
    const id = row.id

    void limit(async () => {
      const latest = rowsRef.current.find((candidate) => candidate.id === id)
      if (latest === undefined || workKey(latest) !== key) return
      try {
        if (work === 'inspect') {
          const inspect = await inspectPath(latest.path)
          applyIfStillWanted(id, key, (current) => receiveInspect(current, inspect))
        } else if (work === 'preview') {
          const request = previewRequest(latest)
          if (request === null) return
          const preview = await runPreview(request)
          applyIfStillWanted(id, key, (current) => receivePreview(current, preview))
        } else {
          const planned = plannedRunWithoutFolder(latest, findEntry(teams, latest.path))
          if (planned === null) return
          const response = await checkRuns({ runs: [{ ...planned, run_folder: null }] })
          const folder = response.runs[0]?.folder
          if (typeof folder !== 'string') throw new Error('The backend gave no folder name.')
          applyIfStillWanted(id, key, (current) => receiveFolder(current, folder))
        }
      } catch (caught) {
        applyIfStillWanted(id, key, (current) => receiveError(current, describeError(caught)))
      }
    }).finally(() => {
      inFlight.current.delete(flightKey)
      setFinishedRequests((count) => count + 1)
    })
  }

  function updateRow(id: number, change: (row: ReviewRow) => ReviewRow) {
    setRows((current) => current.map((row) => (row.id === id ? change(row) : row)))
  }

  function applyToAll(fromRow: ReviewRow) {
    const result = applyMappingToAll(rows, fromRow.id)
    const nameOf = (path: string) => {
      const row = rows.find((candidate) => candidate.path === path)
      return row ? displayName(row, findEntry(teams, path)) : path
    }
    setRows(result.rows)
    setAppliedToAll({
      from: nameOf(fromRow.path),
      applied: result.appliedPaths.map(nameOf),
      leftAlone: result.leftAlone.map((item) => ({ name: nameOf(item.path), reason: item.reason })),
    })
  }

  const founds = rows.map((row) => findEntry(teams, row.path))
  const plannedOrNull = rows.map((row, index) => plannedRun(row, founds[index]))
  const plannedRuns = plannedOrNull.filter((run): run is PlannedRun => run !== null)
  const allPlanned = rows.length > 0 && plannedRuns.length === rows.length

  const loadingCount = rows.filter(isLoading).length
  const attentionCount = rows.filter((row) => attentionReasons(row).length > 0).length

  // A check result belongs to the rows, folders and resources it was made
  // for ("run again" doesn't matter to it). Changing any of them clears it
  // for good, so Start always follows a fresh check.
  const checkKey = JSON.stringify([plannedRuns.map((run) => ({ ...run, run_again: false })), resources])
  useEffect(() => {
    setChecked(null)
  }, [checkKey])
  const result = allPlanned && checked !== null && checked.key === checkKey ? checked.response : null

  const checkedRows = rows.map((_row, index) => result?.runs[index] ?? null)
  const deletable = checkedRows.filter(
    (row): row is CheckedRun =>
      row !== null && row.run_dir !== null && (row.status === 'different' || row.status === 'no_identity'),
  )

  async function runCheck(afterNotice: string | null = null) {
    const key = checkKey
    setBusy('checking')
    setError(null)
    setNotice(null)
    try {
      const response = await checkRuns({ runs: plannedRuns, resources: toRequestResources(resources) })
      setChecked({ key, response })
      setNotice(afterNotice)
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(null)
    }
  }

  async function deleteAndCheckAgain() {
    if (deletable.length === 0) return
    if (!window.confirm(deleteConfirmation(deletable))) return
    setBusy('deleting')
    setError(null)
    setNotice(null)
    try {
      const deleted = await deleteRunFolders(deletable.map((row) => row.run_dir ?? ''))
      const files = deleted.deleted.reduce((sum, item) => sum + item.file_count, 0)
      await runCheck(
        `Deleted ${deleted.deleted.length} folder${deleted.deleted.length === 1 ? '' : 's'} ` +
          `(${files.toLocaleString()} file${files === 1 ? '' : 's'}).`,
      )
    } catch (caught) {
      setError(describeError(caught))
      setBusy(null)
    }
  }

  async function start() {
    setBusy('starting')
    setError(null)
    setNotice(null)
    try {
      const response = await submitRuns(plannedRuns, toRequestResources(resources))
      // The folders are about to be written to, so the old check is out of date.
      setChecked(null)
      onOpenBatch(response.batch_id)
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(null)
    }
  }

  // Why Start can't be pressed yet, or null when it can.
  function startBlocker(): string | null {
    if (rows.length === 0) return 'No datasets are selected.'
    if (loadingCount > 0) return 'Wait for every dataset to finish loading.'
    if (attentionCount > 0) {
      return `${attentionCount} dataset${attentionCount === 1 ? ' needs' : 's need'} attention: fix ${attentionCount === 1 ? 'it' : 'each one'} or remove ${attentionCount === 1 ? 'it' : 'them'} from the batch.`
    }
    if (result === null) return 'Check the folders first: Start follows a fresh check.'
    if (result.slurm_error) return 'SLURM can\'t be reached.'
    if (result.resources_error) return result.resources_error
    const blocked = checkedRows.filter((row) => row !== null && BLOCKING_STATUSES.includes(row.status)).length
    if (blocked > 0) {
      return `${blocked} folder${blocked === 1 ? ' can\'t' : 's can\'t'} be used as ${blocked === 1 ? 'it is' : 'they are'}: delete ${blocked === 1 ? 'it' : 'them'}, choose another folder, or remove the dataset.`
    }
    if (checkedRows.some((row) => row === null || row.sbatch_command === null)) {
      return 'Some commands couldn\'t be built: check the resources above.'
    }
    const willRun = checkedRows.some((row, index) => row?.status !== 'done_same' || rows[index].runAgain)
    if (!willRun) return 'Every dataset is already done. Tick "Run it again" on one to start it again.'
    return null
  }

  const blocker = busy === null ? startBlocker() : 'Working…'
  const canCheck = busy === null && allPlanned && loadingCount === 0

  return (
    <div className="review-view">
      <div className="runs-list-header">
        <h3>
          Review {rows.length} dataset{rows.length === 1 ? '' : 's'}
        </h3>
        <button type="button" className="secondary" onClick={onBack}>
          ‹ Back to datasets
        </button>
      </div>

      {rows.length === 0 && (
        <p className="muted">
          No datasets are selected. Tick "Select datasets to convert" in the sidebar of "Inspect a dataset", pick some
          datasets, then choose "Review".
        </p>
      )}

      {rows.length > 0 && (
        <>
          <p className="muted small">
            Each dataset is converted by its own SLURM job (CPU only), all started together as one batch. Runs root:{' '}
            <span className="mono location-text">{settings?.root ?? result?.root ?? '…'}</span>
          </p>
          <p className="small">
            <strong>
              {rows.length - loadingCount} of {rows.length} loaded
            </strong>
            {attentionCount > 0 && <> · {attentionCount} need attention</>}
            {loadingCount > 0 && <span className="muted"> (two requests at a time)</span>}
          </p>

          <h4>SLURM resources for every job in this batch</h4>
          <ResourcesFields
            settings={settings}
            settingsError={settingsError}
            onReloadSettings={onReloadSettings}
            resources={resources}
            onResourcesChange={onResourcesChange}
          />

          {appliedToAll && (
            <div className="banner banner-warning small">
              Applied the mapping of {appliedToAll.from} to {appliedToAll.applied.length} other dataset
              {appliedToAll.applied.length === 1 ? '' : 's'}.
              {appliedToAll.leftAlone.length > 0 && (
                <>
                  {' '}
                  Left alone:
                  <ul className="lookup-differences">
                    {appliedToAll.leftAlone.map((item) => (
                      <li key={item.name}>
                        {item.name}: {item.reason}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <button type="button" className="secondary" onClick={() => setAppliedToAll(null)}>
                Dismiss
              </button>
            </div>
          )}

          <div className="start-actions">
            <button type="button" className="secondary" disabled={!canCheck} onClick={() => void runCheck()}>
              {busy === 'checking' ? 'Checking…' : 'Check folders'}
            </button>
            {deletable.length > 0 && (
              <button type="button" className="secondary" disabled={busy !== null} onClick={() => void deleteAndCheckAgain()}>
                {busy === 'deleting'
                  ? 'Deleting…'
                  : `Delete ${deletable.length} folder${deletable.length === 1 ? '' : 's'} and start fresh`}
              </button>
            )}
            <button type="button" disabled={blocker !== null} onClick={() => void start()}>
              {busy === 'starting' ? 'Submitting…' : 'Start'}
            </button>
            {blocker !== null && busy === null && <span className="muted small">{blocker}</span>}
          </div>

          {error && <p className="error">{error}</p>}
          {notice && <p className="small">{notice}</p>}
          {result && result.slurm_error && (
            <div className="banner banner-error small">
              {result.slurm_error} Starting and deleting stay disabled until SLURM can be reached.
            </div>
          )}
          {result && result.resources_error && (
            <div className="banner banner-error small">{result.resources_error}</div>
          )}

          <div className="skipped-table-wrap">
            <table className="skipped-table review-table">
              <thead>
                <tr>
                  <th>Dataset</th>
                  <th>Format</th>
                  <th>Mapping</th>
                  <th>Output folder</th>
                  <th>Folder status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <ReviewTableRow
                    key={row.id}
                    row={row}
                    found={founds[index]}
                    checked={checkedRows[index]}
                    expanded={expandedId === row.id}
                    canApplyToAll={rows.length > 1}
                    onToggleExpanded={() => setExpandedId(expandedId === row.id ? null : row.id)}
                    onChooseFormat={(groupIndex) => updateRow(row.id, (current) => chooseFormat(current, groupIndex))}
                    onChooseFile={(file) => updateRow(row.id, (current) => chooseFile(current, file))}
                    onMapping={(mapping) => updateRow(row.id, (current) => editMapping(current, mapping))}
                    onFolder={(folder) => updateRow(row.id, (current) => editFolder(current, folder))}
                    onRunAgain={(runAgain) => updateRow(row.id, (current) => ({ ...current, runAgain }))}
                    onApplyToAll={() => applyToAll(row)}
                    onRetry={() => updateRow(row.id, retry)}
                    onRemove={() => onRemove(row.path)}
                    onOpenResults={onOpenResults}
                    onOpenBatch={onOpenBatch}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
