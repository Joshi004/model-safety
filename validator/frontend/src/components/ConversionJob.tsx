import { useEffect, useRef, useState } from 'react'
import { cancelConversion, errorMessage, getConversion } from '../api'
import { OUTCOME_LABELS, OUTCOME_TONES, describeResources, isActive } from '../conversions'
import { formatCount, formatTimestamp } from '../format'
import { STEPS, describeTimeLeft, formatAmount, formatSpeed, measureStep, unitSuffix } from '../progress'
import type { ConversionStatusResponse, RunProgress } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'
import CommandBox from './CommandBox'

const POLL_MS = 3000
// How many more times a job that SLURM calls finished is asked about while the backend
// hasn't seen its files yet (a shared disk can be up to about a minute behind SLURM): two
// minutes in all.
const MAX_FILE_WAITS = 40

// Which step a conversion is in, how far that step has got, and roughly how long it has
// left, from the job's progress.json.
function ProgressDetails({ progress }: { progress: RunProgress }) {
  const { done, total, unit } = progress
  const { stepIndex, percent, rate, secondsLeft } = measureStep(progress)

  return (
    <div className="run-progress">
      {progress.phase === 'failed' && (
        <div className="banner banner-error small">
          The run failed:
          <div className="run-progress-error mono">{progress.error ?? '(no message)'}</div>
        </div>
      )}

      {stepIndex >= 0 && (
        <>
          <p>
            <strong>
              Step {stepIndex + 1} of {STEPS.length}:
            </strong>{' '}
            {STEPS[stepIndex]?.words ?? progress.phase}
          </p>

          {percent === null ? (
            <p className="muted small">working…</p>
          ) : (
            <div className="progress-bar">
              <div
                className="progress-bar-track"
                role="progressbar"
                aria-valuenow={done}
                aria-valuemin={0}
                aria-valuemax={total ?? undefined}
                aria-label="Progress of the current step"
              >
                <div className="progress-bar-fill" style={{ width: `${percent}%` }} />
              </div>
              <span className="progress-bar-count muted small">
                {formatAmount(done, unit)} of {formatAmount(total ?? 0, unit)}
                {unitSuffix(unit)} ({percent}%)
              </span>
            </div>
          )}

          {progress.files_total !== null && (
            <p className="small">
              Files: {(progress.files_done ?? 0).toLocaleString()} of {progress.files_total.toLocaleString()} done
            </p>
          )}

          <p className="small">
            Written so far: {progress.rows_written.toLocaleString()} · Skipped so far:{' '}
            {progress.rows_skipped.toLocaleString()}
          </p>

          {rate !== null && (
            <p className="small">
              Speed: {formatSpeed(rate, unit)}
              {secondsLeft !== null && <> · {describeTimeLeft(secondsLeft)}</>}
            </p>
          )}
        </>
      )}

      <p className="muted small">Last update: {formatTimestamp(progress.updated_at)}</p>
    </div>
  )
}

// One conversion of a dataset: its outcome, progress, summary, commands and logs, and
// Cancel while it is queued or running. It asks the backend again every 3 seconds while
// the job is going, and calls `onChanged` whenever the outcome changes, so the page reads
// the dataset again (its converted data is set once the job has finished).
//
// Safety: every value shown here comes from a file on disk or from SLURM and renders as
// plain React text, never as markup.
export default function ConversionJob({
  datasetId,
  jobId,
  onChanged,
}: {
  datasetId: string
  jobId: string
  onChanged: () => void
}) {
  const fileWaits = useRef(0)
  useEffect(() => {
    fileWaits.current = 0
  }, [jobId])

  // Keeps asking while the job is going, and for a little while after SLURM calls it
  // finished until the backend has made it the dataset's converted data (`result` is set).
  function keepPolling(job: ConversionStatusResponse): boolean {
    if (isActive(job.outcome)) return true
    if (job.outcome !== 'succeeded' || job.result !== null) return false
    fileWaits.current += 1
    return fileWaits.current <= MAX_FILE_WAITS
  }

  const { data, error, reload } = useRefreshing(
    () => getConversion(datasetId, jobId),
    `${datasetId}/${jobId}`,
    POLL_MS,
    keepPolling,
  )
  // The last answer stays in `data` while another job's first answer is on its way.
  const job = data !== null && data.job_id === jobId ? data : null

  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)

  // The page reads the dataset again whenever the outcome, or whether this is the
  // dataset's current converted data, changes.
  const state = job === null ? null : `${job.outcome}/${job.current}`
  const lastState = useRef<string | null>(null)
  useEffect(() => {
    if (state === null || lastState.current === state) return
    lastState.current = state
    onChanged()
  }, [state, onChanged])

  async function cancel() {
    setCancelling(true)
    setCancelError(null)
    try {
      await cancelConversion(datasetId, jobId)
      reload()
    } catch (failure) {
      setCancelError(errorMessage(failure))
    } finally {
      setCancelling(false)
    }
  }

  if (job === null) {
    return (
      <div className="card">
        <h2>Latest conversion</h2>
        {error !== null ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}
      </div>
    )
  }

  const active = isActive(job.outcome)

  return (
    <div className="card conversion-job">
      <div className="page-header">
        <h2>Latest conversion</h2>
        <Chip label={OUTCOME_LABELS[job.outcome]} tone={OUTCOME_TONES[job.outcome]} title={job.reason ?? undefined} />
        {job.current && <Chip label="Current converted data" tone="ok" />}
        {active && (
          <button type="button" className="secondary push-right" onClick={() => void cancel()} disabled={cancelling}>
            {cancelling ? 'Cancelling…' : 'Cancel conversion'}
          </button>
        )}
      </div>

      <dl className="facts">
        <dt>Started</dt>
        <dd>{job.submitted_at !== null ? formatTimestamp(job.submitted_at) : '—'}</dd>
        {job.elapsed !== null && (
          <>
            <dt>Time used</dt>
            <dd>{job.elapsed}</dd>
          </>
        )}
        {job.slurm_job_id !== null && (
          <>
            <dt>SLURM job</dt>
            <dd>
              {job.slurm_job_id}
              {job.slurm_state !== null && <span className="muted"> ({job.slurm_state})</span>}
            </dd>
          </>
        )}
        {job.resources !== null && (
          <>
            <dt>Resources</dt>
            <dd>{describeResources(job.resources)}</dd>
          </>
        )}
        {job.reason !== null && (
          <>
            <dt>Status</dt>
            <dd>{job.reason}</dd>
          </>
        )}
      </dl>

      {job.submit_error !== null && (
        <div className="banner banner-error" role="alert">
          The job was not submitted: {job.submit_error}
        </div>
      )}
      {cancelError !== null && (
        <div className="banner banner-error" role="alert">
          {cancelError}
        </div>
      )}
      {error !== null && <p className="error">{error}</p>}
      {job.may_have_stopped && (
        <div className="banner banner-warning small">
          This run hasn't reported any progress for over 2 minutes, so it may have stopped.
        </div>
      )}
      {job.outcome === 'succeeded' && job.result === null && (
        <div className="banner banner-warning small">
          The job has finished. Waiting for its files to show up here: the shared disk can be about a minute behind.
          This page keeps checking.
        </div>
      )}
      {job.outcome === 'checks_failed' && (
        <div className="banner banner-error small">
          The run read every row, but its own checks failed, so nothing was kept as converted data. Look at the log
          below for the reason.
        </div>
      )}

      {job.progress !== null && (active || job.outcome === 'failed') && <ProgressDetails progress={job.progress} />}

      {job.summary !== null && (
        <p className="small">
          Rows read: {formatCount(job.summary.rows_read)} · written: {formatCount(job.summary.written)} · skipped:{' '}
          {formatCount(job.summary.skipped)} · checks {job.summary.checks_passed ? 'passed' : 'failed'}
        </p>
      )}

      {job.deleted.length > 0 && (
        <p className="muted small">
          Deleted when it started: <span className="mono">{job.deleted.join(', ')}</span>
        </p>
      )}

      <details>
        <summary className="small">Commands and logs</summary>
        {job.sbatch_command !== null && (
          <CommandBox
            command={job.sbatch_command}
            title="Submitted with"
            note="Exactly what was run to start this job."
          />
        )}
        {job.command !== null && (
          <CommandBox command={job.command} title="Conversion command" note="What the job runs, from the repository root." />
        )}
        {job.out_tail !== null && (
          <>
            <h4>Output (last lines)</h4>
            <pre className="raw-json">{job.out_tail || '(empty)'}</pre>
          </>
        )}
        {job.err_tail !== null && (
          <>
            <h4>Errors (last lines)</h4>
            <pre className="raw-json">{job.err_tail || '(empty)'}</pre>
          </>
        )}
        {job.out_tail === null && job.err_tail === null && <p className="muted small">No log yet.</p>}
      </details>
    </div>
  )
}
