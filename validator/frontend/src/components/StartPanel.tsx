import { useEffect, useState } from 'react'
import { ApiError, checkRuns, deleteRunFolders, submitRuns } from '../api'
import { describeBrief, describeContents } from '../jobs'
import { toRequestResources } from '../resources'
import type { ResourceInputs } from '../resources'
import type { CheckResponse, CheckedRun, FolderContents, JobsSettings, PlannedRun } from '../types'
import CommandBox from './CommandBox'
import ResourcesFields from './ResourcesFields'

// "Start conversion", under the preview in the Inspect view: pick the output
// folder and the SLURM resources, check what's already in the folder, then
// submit one CPU-only job that runs the command shown. Everything the user
// sees about a folder (its name, its status, the commands) comes from
// /api/jobs/check, so the naming rule and the exact arguments live only in
// the backend. A check result is only shown while the mapping, the folder
// name and the resources are the ones it was made for, so Start always
// follows a fresh check.
//
// Safety: every value shown here comes from the backend or from files on
// disk and renders as plain React text, never as markup.

function describeError(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : String(caught)
}

function FolderContentsLine({ contents }: { contents: FolderContents | null }) {
  if (contents === null) return null
  return (
    <p className="small">
      In the folder now: {describeContents(contents)}.
      {contents.summary && <> {describeBrief(contents.summary)}</>}
    </p>
  )
}

// The text of the one confirmation before a folder is deleted.
function deleteConfirmation(row: CheckedRun): string {
  const lines = ['Delete this folder and everything in it?', '', row.run_dir ?? '']
  if (row.contents) {
    lines.push(describeContents(row.contents))
    if (row.contents.summary) lines.push(describeBrief(row.contents.summary))
  }
  lines.push('', "This can't be undone. The original data isn't touched.")
  return lines.join('\n')
}

export default function StartPanel({
  run,
  settings,
  settingsError,
  onReloadSettings,
  resources,
  onResourcesChange,
  previewWarning,
  onOpenResults,
  onOpenBatch,
}: {
  // The dataset and mapping to convert, without a folder name: this panel
  // adds that. The parent gives the panel a new `key` per dataset.
  run: PlannedRun
  settings: JobsSettings | null
  settingsError: string | null
  onReloadSettings: () => void
  resources: ResourceInputs
  onResourcesChange: (next: ResourceInputs) => void
  previewWarning: string | null
  onOpenResults: (runDir: string) => void
  onOpenBatch: (batchId: string) => void
}) {
  const [folder, setFolder] = useState('')
  const [suggesting, setSuggesting] = useState(true)
  const [checked, setChecked] = useState<{ key: string; response: CheckResponse } | null>(null)
  const [runAgain, setRunAgain] = useState(false)
  const [busy, setBusy] = useState<'checking' | 'deleting' | 'starting' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // The suggested folder name comes from the backend (null asks for it), once
  // per dataset. A mapping change doesn't rename the folder.
  useEffect(() => {
    let cancelled = false
    checkRuns({ runs: [{ ...run, run_folder: null }] })
      .then((response) => {
        if (!cancelled) setFolder(response.runs[0]?.folder ?? '')
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(describeError(caught))
      })
      .finally(() => {
        if (!cancelled) setSuggesting(false)
      })
    return () => {
      cancelled = true
    }
    // Deliberately once per mount: the parent remounts this panel for another dataset.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const requestKey = JSON.stringify([run, folder, resources])
  // A result belongs to the inputs it was made for. Changing any of them
  // clears it for good: putting the old value back doesn't bring it back.
  useEffect(() => {
    setChecked(null)
  }, [requestKey])
  const result = checked !== null && checked.key === requestKey ? checked.response : null
  const row = result?.runs[0] ?? null
  const status = row?.status ?? null

  async function runCheck(afterNotice: string | null = null) {
    const key = requestKey
    setBusy('checking')
    setError(null)
    setNotice(null)
    try {
      const response = await checkRuns({
        runs: [{ ...run, run_folder: folder }],
        resources: toRequestResources(resources),
      })
      setChecked({ key, response })
      setRunAgain(false)
      setNotice(afterNotice)
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(null)
    }
  }

  async function deleteAndCheckAgain() {
    if (!row?.run_dir) return
    if (!window.confirm(deleteConfirmation(row))) return
    setBusy('deleting')
    setError(null)
    setNotice(null)
    try {
      const deleted = await deleteRunFolders([row.run_dir])
      const files = deleted.deleted.reduce((sum, item) => sum + item.file_count, 0)
      await runCheck(`Deleted ${files.toLocaleString()} file${files === 1 ? '' : 's'}. The folder is empty now.`)
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
      const response = await submitRuns(
        [{ ...run, run_folder: folder, run_again: runAgain }],
        toRequestResources(resources),
      )
      onOpenBatch(response.batch_id)
    } catch (caught) {
      setError(describeError(caught))
    } finally {
      setBusy(null)
    }
  }

  const canStart =
    busy === null &&
    result !== null &&
    row !== null &&
    result.slurm_error === null &&
    result.resources_error === null &&
    row.sbatch_command !== null &&
    (status === 'empty' || status === 'same_unfinished' || (status === 'done_same' && runAgain))

  return (
    <div className="start-panel">
      <h2>Start conversion</h2>
      <p className="muted small">
        Submits one SLURM job (CPU only) that runs <code>prepare_rows.py</code> with the mapping above, writing
        into a folder under the runs root. Nothing is written into that folder until the job begins.
      </p>

      {previewWarning && (
        <div className="banner banner-warning small">
          The preview above says this mapping would fail when run: {previewWarning}
        </div>
      )}

      <div className="start-field">
        <label htmlFor="start-folder">Folder name (under the runs root)</label>
        <input
          id="start-folder"
          type="text"
          className="mono"
          value={folder}
          disabled={suggesting}
          placeholder={suggesting ? 'Working out a name…' : 'team/group/dataset'}
          onChange={(event) => setFolder(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <p className="muted small">
          Runs root: <span className="mono location-text">{settings?.root ?? result?.root ?? '…'}</span>. Letters,
          digits, <code>.</code> <code>_</code> <code>-</code>, with <code>/</code> between parts.
        </p>
      </div>

      <ResourcesFields
        settings={settings}
        settingsError={settingsError}
        onReloadSettings={onReloadSettings}
        resources={resources}
        onResourcesChange={onResourcesChange}
      />

      <div className="start-actions">
        <button
          type="button"
          className="secondary"
          disabled={suggesting || busy !== null || settings === null}
          onClick={() => void runCheck()}
        >
          {busy === 'checking' ? 'Checking…' : 'Check folder'}
        </button>
        <button type="button" disabled={!canStart} onClick={() => void start()}>
          {busy === 'starting' ? 'Submitting…' : 'Start'}
        </button>
        {result === null && busy === null && !suggesting && (
          <span className="muted small">Check the folder first: Start follows a fresh check.</span>
        )}
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

      {row && (
        <div className="start-result">
          {status === 'empty' && (
            <div className="banner banner-ok small">
              <span className="mono location-text">{row.run_dir}</span> is empty or doesn't exist yet. Ready to
              start.
            </div>
          )}

          {status === 'done_same' && (
            <>
              <div className="banner banner-ok small">
                Already done: this folder holds a finished run with exactly these settings and this input.
              </div>
              <FolderContentsLine contents={row.contents} />
              <div className="start-actions">
                {row.run_dir && (
                  <button type="button" className="secondary" onClick={() => onOpenResults(row.run_dir ?? '')}>
                    Open results
                  </button>
                )}
                <label className="inline small">
                  <input type="checkbox" checked={runAgain} onChange={(event) => setRunAgain(event.target.checked)} />
                  Run it again
                </label>
              </div>
            </>
          )}

          {status === 'same_unfinished' && (
            <>
              <div className="banner banner-warning small">
                This folder holds an unfinished (or failed) run with the same settings and input. Starting runs
                over it.
              </div>
              <FolderContentsLine contents={row.contents} />
            </>
          )}

          {(status === 'different' || status === 'no_identity') && (
            <>
              <div className="banner banner-error small">
                {status === 'different' ? (
                  <>
                    This folder holds a different run, so it can't be reused as it is. What changed (previous →
                    requested):
                    <ul className="lookup-differences">
                      {row.differences.map((line) => (
                        <li key={line} className="mono location-text">
                          {line}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : (
                  <>
                    This folder has files but no <code>run_identity.json</code>, so it holds output from before run
                    identities existed (or something else). It can't be reused as it is.
                  </>
                )}
              </div>
              <FolderContentsLine contents={row.contents} />
              <div className="start-actions">
                {row.run_dir && row.contents?.summary && (
                  <button type="button" className="secondary" onClick={() => onOpenResults(row.run_dir ?? '')}>
                    Open results
                  </button>
                )}
                <button type="button" className="secondary" disabled={busy !== null} onClick={() => void deleteAndCheckAgain()}>
                  {busy === 'deleting' ? 'Deleting…' : 'Delete and start fresh'}
                </button>
              </div>
              <p className="muted small">Or change the folder name above to use another folder.</p>
            </>
          )}

          {status === 'running' && (
            <>
              <div className="banner banner-warning small">
                A job is writing into this folder now: {row.reason}
              </div>
              {row.batch_id && (
                <button type="button" className="secondary" onClick={() => onOpenBatch(row.batch_id ?? '')}>
                  Open its batch
                </button>
              )}
            </>
          )}

          {(status === 'refused' || status === 'outside_root' || status === 'inside_run') && (
            <div className="banner banner-error small">{row.reason}</div>
          )}

          {row.sbatch_command && (
            <CommandBox
              title="sbatch command"
              command={row.sbatch_command}
              note="Start submits exactly this, from the repo root. Copy it to submit the same job by hand."
            />
          )}
          {row.command && (
            <CommandBox
              title="Local command"
              command={row.command}
              note="The same arguments, run on this machine instead of through SLURM. Run it from the repo root yourself; the UI never runs it."
            />
          )}
        </div>
      )}
    </div>
  )
}
