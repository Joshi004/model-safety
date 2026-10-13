import { useState } from 'react'
import { ApiError, cancelStepJobs, getStepJobs, getStepStatus } from '../api'
import { STATE_LABELS, STEP_OUTCOME_LABELS, describeProgress, describeStepResources } from '../chains'
import { formatBytes, formatSetting, formatTimestamp } from '../format'
import type { StepJobView, StepOutcome, StepRecordView } from '../types'
import { useRefreshing } from '../useRefreshing'

// The steps of every chain that were started from here (a scan, a coverage report, a
// filter, the trace, the row status): what SLURM says about their jobs, how far each
// stage has got, the command, and the end of the logs. It asks the backend again every
// 5 seconds while any job is queued or running. Reading never changes anything; only
// Cancel does (scancel, on this record's own jobs).
//
// Safety: every value shown here comes from step records, SLURM, the stage's own
// files or a log, and renders as plain React text, never as markup.

const POLL_MS = 5000

// run-status-passed / run-status-failed are the colours the runs list uses.
const OUTCOME_CLASSES: Record<StepOutcome, string | undefined> = {
  queued: 'muted',
  running: undefined,
  succeeded: 'run-status-passed',
  failed: 'run-status-failed',
  cancelled: 'muted',
  unknown: 'muted',
}

function recordKey(record: StepRecordView): string {
  return `${record.chain_dir}|${record.id}`
}

function Progress({ record }: { record: StepRecordView }) {
  const { progress } = record
  if (record.stage_state === 'finished') {
    return <span className="small run-status-passed">stage finished{record.stage_verdict ? `: ${record.stage_verdict}` : ''}</span>
  }
  if (progress === null) {
    return <span className="muted small">{record.stage_state ? STATE_LABELS[record.stage_state] : '—'}</span>
  }
  const percent = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0
  return (
    <div className="progress-bar progress-bar-full">
      <div
        className="progress-bar-track"
        role="progressbar"
        aria-valuenow={progress.done}
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-label={`${record.label} progress`}
      >
        <div className="progress-bar-fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="progress-bar-count muted small">
        {describeProgress(progress)} ({percent}%)
      </span>
    </div>
  )
}

function JobLogs({ job }: { job: StepJobView }) {
  return (
    <div className="job-logs">
      <h4>
        Standard output <span className="muted small mono location-text">{job.out_log ?? ''}</span>
      </h4>
      <pre className="command-text job-log">{job.out_tail ?? (job.out_log ? '(no log file yet)' : '(no log file recorded)')}</pre>
      <h4>
        Errors <span className="muted small mono location-text">{job.err_log ?? ''}</span>
      </h4>
      <pre className="command-text job-log">{job.err_tail ?? (job.err_log ? '(no log file yet)' : '(no log file recorded)')}</pre>
      <p className="muted small">The last 40 lines of each, at most.</p>
    </div>
  )
}

// One record in full: the command, its settings, and each job with its logs.
function RecordDetails({ chainDir, id }: { chainDir: string; id: string }) {
  const { data, error } = useRefreshing(
    () => getStepStatus(chainDir, id),
    `${chainDir}|${id}`,
    POLL_MS,
    (record) => record.has_active,
  )
  if (error) return <p className="error">{error}</p>
  if (data === null) return <p className="muted">Loading…</p>
  return (
    <div className="step-details">
      {data.slurm_error && <div className="banner banner-error small">{data.slurm_error}</div>}
      {data.warnings.length > 0 && (
        <div className="banner banner-warning small">
          {data.warnings.map((warning) => (
            <div key={warning}>{warning}</div>
          ))}
        </div>
      )}
      {data.command_text && (
        <>
          <h4>Command</h4>
          <pre className="command-text">{data.command_text}</pre>
        </>
      )}
      <p className="small">
        {data.resources
          ? describeStepResources(data.resources, data.gpus)
          : data.jobs.length > 0
            ? 'Resources not recorded'
            : 'No SLURM resources: the backend ran this itself'}
      </p>
      {data.output && (
        <p className="muted small">
          Writes <span className="mono location-text">{data.output}</span>
        </p>
      )}
      {data.settings && Object.keys(data.settings).length > 0 && (
        <details>
          <summary className="small">Settings</summary>
          <table className="counts-table">
            <tbody>
              {Object.entries(data.settings).map(([name, value]) => (
                <tr key={name}>
                  <th scope="row">{name}</th>
                  <td className="mono location-text">{formatSetting(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      {data.wrapper_output && (
        <details>
          <summary className="small">What the start command printed</summary>
          <pre className="command-text job-log">{data.wrapper_output}</pre>
        </details>
      )}
      {data.inline && (
        <p className="small">
          The backend ran this command itself; it ended with exit code {String(data.inline.exit_code)}.
        </p>
      )}
      {data.jobs.map((job) => (
        <div key={job.job_id ?? job.role ?? 'job'} className="step-job">
          <p className="small">
            <strong>SLURM job {job.job_id ?? '(none recorded)'}</strong>
            {job.role && ` (${job.role})`}: <span className={OUTCOME_CLASSES[job.outcome]}>{STEP_OUTCOME_LABELS[job.outcome]}</span>
            {job.elapsed && ` · elapsed ${job.elapsed}`}
            {job.exit_code && ` · exit code ${job.exit_code}`}
            {job.max_rss_bytes !== null && ` · peak memory ${formatBytes(job.max_rss_bytes)}`}
            {job.depends_on.length > 0 && ` · waits for ${job.depends_on.join(', ')}`}
            {job.reason && <span className="muted job-error"> ({job.reason})</span>}
          </p>
          {(job.out_log || job.err_log) && (
            <details>
              <summary className="small">Show log</summary>
              <JobLogs job={job} />
            </details>
          )}
        </div>
      ))}
    </div>
  )
}

export default function StepJobsList({ onOpenChain }: { onOpenChain: (chainDir: string) => void }) {
  const { data, error, reload } = useRefreshing(
    () => getStepJobs(),
    'step-jobs',
    POLL_MS,
    (response) => response.records.some((record) => record.has_active),
  )
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  async function cancel(record: StepRecordView) {
    if (
      !window.confirm(
        `Cancel the queued and running SLURM jobs of "${record.label}" in ${record.chain}?\n\nWhat it has written so far stays where it is.`,
      )
    ) {
      return
    }
    setCancelling(recordKey(record))
    setActionError(null)
    try {
      await cancelStepJobs(record.chain_dir, record.id)
      reload()
    } catch (caught) {
      setActionError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setCancelling(null)
    }
  }

  const records = data?.records ?? []
  return (
    <div className="runs-list step-jobs">
      <div className="runs-list-header">
        <h3>Chain steps</h3>
        <button type="button" className="secondary" onClick={reload}>
          Refresh
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {actionError && <p className="error">{actionError}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data && (
        <>
          {data.slurm_error && (
            <div className="banner banner-error small">{data.slurm_error} The state of the jobs can't be read right now.</div>
          )}
          {data.problems.length > 0 && (
            <div className="banner banner-warning small">
              Some step records couldn't be read:
              <ul className="lookup-differences">
                {data.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          )}
          {records.length === 0 && (
            <p className="muted">
              No chain steps have been started from here yet. A step run by hand (from a terminal) leaves no record, but its
              stage still shows its state on the chain's page.
            </p>
          )}
          {records.length > 0 && (
            <div className="skipped-table-wrap">
              <table className="skipped-table job-table">
                <thead>
                  <tr>
                    <th>Step</th>
                    <th>Chain</th>
                    <th>Started</th>
                    <th>Status</th>
                    <th>Progress</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => {
                    const key = recordKey(record)
                    return (
                      <RecordRows
                        key={key}
                        record={record}
                        open={openKey === key}
                        cancelling={cancelling === key}
                        onToggle={() => setOpenKey(openKey === key ? null : key)}
                        onCancel={() => void cancel(record)}
                        onOpenChain={() => onOpenChain(record.chain_dir)}
                      />
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small">Refreshes every {POLL_MS / 1000} seconds while a job is queued or running.</p>
        </>
      )}
    </div>
  )
}

function RecordRows({
  record,
  open,
  cancelling,
  onToggle,
  onCancel,
  onOpenChain,
}: {
  record: StepRecordView
  open: boolean
  cancelling: boolean
  onToggle: () => void
  onCancel: () => void
  onOpenChain: () => void
}) {
  return (
    <>
      <tr className="no-click">
        <td>
          <div>{record.label}</div>
          <div className="muted small">{record.id}</div>
          {record.gpus > 0 && (
            <div className="small">
              {record.gpus} GPU{record.gpus === 1 ? '' : 's'}
            </div>
          )}
        </td>
        <td>{record.chain}</td>
        <td>{record.submitted_at ? formatTimestamp(record.submitted_at) : '—'}</td>
        <td>
          <div className={OUTCOME_CLASSES[record.outcome]}>{STEP_OUTCOME_LABELS[record.outcome]}</div>
          {record.reason && <div className="muted small job-error">{record.reason}</div>}
          {record.outcome === 'succeeded' && record.stage_state !== null && record.stage_state !== 'finished' && (
            <div className="muted small">its jobs finished, but the stage doesn't show as finished</div>
          )}
        </td>
        <td>
          <Progress record={record} />
        </td>
        <td className="job-actions">
          <button type="button" className="secondary" onClick={onToggle}>
            {open ? 'Hide details' : 'Details'}
          </button>
          <button type="button" className="secondary" onClick={onOpenChain}>
            Open chain
          </button>
          {record.has_active && (
            <button type="button" className="secondary" disabled={cancelling} onClick={onCancel}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="no-click">
          <td colSpan={6}>
            <RecordDetails chainDir={record.chain_dir} id={record.id} />
          </td>
        </tr>
      )}
    </>
  )
}
