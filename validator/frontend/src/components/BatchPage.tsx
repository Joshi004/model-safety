import { useState } from 'react'
import { ApiError, cancelBatchJobs, getBatchStatus } from '../api'
import { formatBytes, formatTimestamp } from '../format'
import { ACTIVE_OUTCOMES, OUTCOME_CLASSES, OUTCOME_LABELS, describeOutcomes, describeResources } from '../jobs'
import { STEPS, describeTimeLeft, formatAmount, measureStep, unitSuffix } from '../progress'
import type { JobStatus } from '../types'
import { useRefreshing } from '../useRefreshing'

// One batch in the Jobs view: a row per job with its outcome, the step it's
// in, how many rows it has written and skipped, its peak memory, the end of
// its logs, and Cancel while it is queued or running. It asks the backend
// again every 5 seconds while any job is still going.
//
// Safety: every value shown here comes from SLURM, from progress.json or
// summary.json, or from a log, and renders as plain React text, never as
// markup.

const POLL_MS = 5000

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString()
}

// Written rows: from the finished run's summary, else the last progress report.
function rowsWritten(job: JobStatus): number | null {
  return job.summary?.written ?? job.progress?.rows_written ?? null
}

function rowsSkipped(job: JobStatus): number | null {
  return job.summary?.skipped ?? job.progress?.rows_skipped ?? null
}

function JobStep({ job }: { job: JobStatus }) {
  const { progress } = job
  if (progress === null) {
    return job.outcome === 'running' || job.outcome === 'stalled' ? (
      <span className="muted small">no progress reported yet</span>
    ) : (
      <span className="muted">—</span>
    )
  }
  if (progress.phase === 'done') return <span className="small">Finished</span>
  if (progress.phase === 'failed') {
    return <span className="small run-status-failed job-error">Failed: {progress.error ?? '(no message)'}</span>
  }

  const { stepIndex, percent, secondsLeft } = measureStep(progress)
  const { done, total, unit } = progress
  return (
    <div>
      <div className="small">
        <strong>
          Step {stepIndex + 1} of {STEPS.length}:
        </strong>{' '}
        {STEPS[stepIndex]?.words ?? progress.phase}
      </div>
      {percent === null ? (
        <div className="muted small">working…</div>
      ) : (
        <div className="progress-bar progress-bar-full">
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
      {job.outcome === 'running' && secondsLeft !== null && (
        <div className="muted small">{describeTimeLeft(secondsLeft)}</div>
      )}
    </div>
  )
}

function JobLogs({ job }: { job: JobStatus }) {
  return (
    <div className="job-logs">
      <h4>
        Standard output <span className="muted small mono location-text">{job.out_log ?? ''}</span>
      </h4>
      <pre className="command-text job-log">{job.out_tail ?? '(no log file yet)'}</pre>
      <h4>
        Errors <span className="muted small mono location-text">{job.err_log ?? ''}</span>
      </h4>
      <pre className="command-text job-log">{job.err_tail ?? '(no log file yet)'}</pre>
      <p className="muted small">The last 40 lines of each, at most.</p>
    </div>
  )
}

export default function BatchPage({
  batchId,
  onBack,
  onOpenResults,
}: {
  batchId: string
  onBack: () => void
  onOpenResults: (runDir: string) => void
}) {
  const { data, error, reload } = useRefreshing(
    () => getBatchStatus(batchId),
    batchId,
    POLL_MS,
    (response) => response.has_active,
  )
  const [logsShownFor, setLogsShownFor] = useState<number | null>(null)
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  async function cancelJob(job: JobStatus) {
    const jobId = job.slurm_job_id
    if (jobId === null) return
    const name = job.label ?? job.folder ?? jobId
    if (!window.confirm(`Cancel SLURM job ${jobId} (${name})?\n\nIts run folder keeps whatever it had written so far.`)) {
      return
    }
    setCancelling(jobId)
    setActionError(null)
    try {
      await cancelBatchJobs(batchId, [jobId])
      reload()
    } catch (caught) {
      setActionError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setCancelling(null)
    }
  }

  const jobs = data?.jobs ?? []
  const written = jobs.reduce((sum, job) => sum + (rowsWritten(job) ?? 0), 0)
  const skipped = jobs.reduce((sum, job) => sum + (rowsSkipped(job) ?? 0), 0)
  const peakMemory = jobs.reduce((peak, job) => Math.max(peak, job.max_rss_bytes ?? 0), 0)

  return (
    <div className="batch-page">
      <div className="runs-list-header">
        <h3>
          Batch <span className="mono">{batchId}</span>
        </h3>
        <span>
          <button type="button" className="secondary" onClick={onBack}>
            ‹ All batches
          </button>{' '}
          <button type="button" className="secondary" onClick={reload}>
            Refresh
          </button>
        </span>
      </div>

      {error && <p className="error">{error}</p>}
      {actionError && <p className="error">{actionError}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}

      {data && (
        <>
          <p className="muted small">
            Submitted {data.created_at ? formatTimestamp(data.created_at) : '(time not recorded)'} ·{' '}
            {describeResources(data.resources)}
          </p>
          {data.slurm_error && (
            <div className="banner banner-error small">
              {data.slurm_error} The state of its jobs can't be read right now.
            </div>
          )}
          <p>
            <strong>
              {jobs.length} job{jobs.length === 1 ? '' : 's'}:
            </strong>{' '}
            {describeOutcomes(data.by_outcome)}
            <br />
            <span className="small">
              Rows written {written.toLocaleString()} · skipped {skipped.toLocaleString()}
              {data.has_active && ' (so far, for jobs still going)'}
              {peakMemory > 0 && <> · highest peak memory {formatBytes(peakMemory)}</>}
            </span>
          </p>

          <div className="skipped-table-wrap">
            <table className="skipped-table job-table">
              <thead>
                <tr>
                  <th>Dataset</th>
                  <th>Outcome</th>
                  <th>Step</th>
                  <th className="number">Written</th>
                  <th className="number">Skipped</th>
                  <th className="number">Peak memory</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => (
                  <JobRows
                    key={job.index}
                    job={job}
                    logsShown={logsShownFor === job.index}
                    cancelling={cancelling === job.slurm_job_id}
                    onToggleLogs={() => setLogsShownFor(logsShownFor === job.index ? null : job.index)}
                    onCancel={() => void cancelJob(job)}
                    onOpenResults={onOpenResults}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            Refreshes every {POLL_MS / 1000} seconds while a job is queued or running.
          </p>
        </>
      )}
    </div>
  )
}

function JobRows({
  job,
  logsShown,
  cancelling,
  onToggleLogs,
  onCancel,
  onOpenResults,
}: {
  job: JobStatus
  logsShown: boolean
  cancelling: boolean
  onToggleLogs: () => void
  onCancel: () => void
  onOpenResults: (runDir: string) => void
}) {
  const canCancel = ACTIVE_OUTCOMES.includes(job.outcome) && job.slurm_job_id !== null
  return (
    <>
      <tr>
        <td title={job.run_dir ?? undefined}>
          <div>{job.label ?? '(no name)'}</div>
          {job.input_path && <div className="muted small mono location-text">from {job.input_path}</div>}
          <div className="muted small mono location-text">into {job.folder}</div>
          {job.slurm_job_id && <div className="muted small">SLURM job {job.slurm_job_id}</div>}
        </td>
        <td>
          <div className={OUTCOME_CLASSES[job.outcome]}>{OUTCOME_LABELS[job.outcome]}</div>
          {job.reason && <div className="muted small job-error">{job.reason}</div>}
          {job.elapsed && <div className="muted small">elapsed {job.elapsed}</div>}
        </td>
        <td>
          <JobStep job={job} />
        </td>
        <td className="number">{formatCount(rowsWritten(job))}</td>
        <td className="number">{formatCount(rowsSkipped(job))}</td>
        <td className="number">{job.max_rss_bytes === null ? '—' : formatBytes(job.max_rss_bytes)}</td>
        <td className="job-actions">
          {job.slurm_job_id && (
            <button type="button" className="secondary" onClick={onToggleLogs}>
              {logsShown ? 'Hide log' : 'Show log'}
            </button>
          )}
          {job.summary !== null && job.run_dir && (
            <button type="button" className="secondary" onClick={() => onOpenResults(job.run_dir ?? '')}>
              Open results
            </button>
          )}
          {canCancel && (
            <button type="button" className="secondary" disabled={cancelling} onClick={onCancel}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
        </td>
      </tr>
      {logsShown && (
        <tr>
          <td colSpan={7}>
            <JobLogs job={job} />
          </td>
        </tr>
      )}
    </>
  )
}
