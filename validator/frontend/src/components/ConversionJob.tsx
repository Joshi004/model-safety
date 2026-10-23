import { useEffect, useRef, useState } from 'react'
import { cancelConversion, errorMessage, getConversion, getDataset } from '../api'
import { OUTCOME_LABELS, OUTCOME_TONES, canRetryConversion, describeResources, isActive } from '../conversions'
import { formatCount, formatTimestamp } from '../format'
import { STEPS, describeTimeLeft, formatAmount, formatSpeed, measureStep, unitSuffix } from '../progress'
import type { ConversionStatusResponse, ConversionSubmitResponse, Dataset, RunProgress } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'
import CommandBox from './CommandBox'
import ConvertDialog from './ConvertDialog'
import FailureDetails from './FailureDetails'

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
// With `allowRetry`, a conversion that failed, was cancelled or wasn't submitted has Retry: it
// opens the convert dialog for the dataset with the resources this conversion ran with. The
// dialog checks everything again like any other start; this conversion is kept, so the reason it
// failed can still be read.
//
// Safety: every value shown here comes from a file on disk or from SLURM and renders as
// plain React text, never as markup.
export default function ConversionJob({
  datasetId,
  jobId,
  onChanged,
  title = 'Latest conversion',
  allowRetry = false,
  startWithRetry = false,
}: {
  datasetId: string
  jobId: string
  onChanged: () => void
  title?: string
  // Offer Retry. Off where the page has its own way to convert again (a dataset's Conversion tab).
  allowRetry?: boolean
  // Opens the retry dialog as soon as the conversion has loaded (the Activity page's Retry button,
  // which opens this card). Only read when the card first appears.
  startWithRetry?: boolean
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

  // Retry: whether it was pressed and the dataset is being read, the dataset the dialog is open for
  // (null while it is closed), and what to tell the user.
  const [retryAsked, setRetryAsked] = useState(startWithRetry)
  const [retrying, setRetrying] = useState<Dataset | null>(null)
  const [retryError, setRetryError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const canRetry = allowRetry && job !== null && canRetryConversion(job.outcome)

  useEffect(() => {
    if (!retryAsked || !canRetry) return
    let cancelled = false
    // The dataset as it is now: whether it has converted data decides what the dialog offers.
    getDataset(datasetId, true)
      .then((dataset) => {
        if (cancelled) return
        setRetryAsked(false)
        setRetryError(null)
        setRetrying(dataset)
      })
      .catch((failure: unknown) => {
        if (cancelled) return
        setRetryAsked(false)
        setRetryError(errorMessage(failure))
      })
    return () => {
      cancelled = true
    }
  }, [retryAsked, canRetry, datasetId])

  function handleRetryStarted(result: ConversionSubmitResponse) {
    setRetrying(null)
    const problems = result.errors.length > 0 ? ` Problems: ${result.errors.join(' ')}` : ''
    setNotice(
      result.started.length === 0
        ? `Nothing was started.${problems}`
        : `Started a new conversion. It is at the top of the Activity list. This one is kept so you can still read why it failed.${problems}`,
    )
    onChanged()
  }

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
        <h2>{title}</h2>
        {error !== null ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}
      </div>
    )
  }

  const active = isActive(job.outcome)

  return (
    <div className="card conversion-job">
      <div className="page-header">
        <h2>{title}</h2>
        <Chip label={OUTCOME_LABELS[job.outcome]} tone={OUTCOME_TONES[job.outcome]} title={job.reason ?? undefined} />
        {job.current && <Chip label="Current converted data" tone="ok" />}
        {active && (
          <button type="button" className="secondary push-right" onClick={() => void cancel()} disabled={cancelling}>
            {cancelling ? 'Cancelling…' : 'Cancel conversion'}
          </button>
        )}
        {canRetry && (
          <button
            type="button"
            className="secondary push-right"
            onClick={() => {
              setNotice(null)
              setRetryAsked(true)
            }}
            disabled={retryAsked}
            title="Opens the convert dialog for this dataset with the resources this conversion used. This conversion is kept."
          >
            Retry
          </button>
        )}
      </div>

      {notice !== null && (
        <div className="banner banner-ok small" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      {retryError !== null && (
        <div className="banner banner-error small" role="alert">
          {retryError}
        </div>
      )}

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
      {job.outcome === 'failed' && <FailureDetails reason={job.reason} failure={job.failure} />}
      {job.outcome === 'checks_failed' && (
        <>
          <div className="banner banner-error small">
            The run read every row, but its own checks failed, so nothing was kept as converted data. Look at the log
            below for the reason.
          </div>
          <FailureDetails reason={null} failure={job.failure} />
        </>
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

      <details open={job.outcome === 'failed' || job.outcome === 'checks_failed'}>
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
            {job.out_log !== null && (
              <p className="muted small">
                <code>{job.out_log}</code>
              </p>
            )}
            <pre className="raw-json">{job.out_tail || '(empty)'}</pre>
          </>
        )}
        {job.err_tail !== null && (
          <>
            <h4>Errors (last lines)</h4>
            {job.err_log !== null && (
              <p className="muted small">
                <code>{job.err_log}</code>
              </p>
            )}
            <pre className="raw-json">{job.err_tail || '(empty)'}</pre>
          </>
        )}
        {job.out_tail === null && job.err_tail === null && <p className="muted small">No log yet.</p>}
      </details>

      {retrying !== null && (
        <ConvertDialog
          datasets={[retrying]}
          initialResources={job.resources ?? undefined}
          initialAgain
          onClose={() => setRetrying(null)}
          onStarted={handleRetryStarted}
        />
      )}
    </div>
  )
}
