import { useState } from 'react'
import { getFilter } from '../api'
import { reasonLabel } from '../flagged'
import { formatCount, formatTimestamp } from '../format'
import { hrefFor } from '../route'
import { STATE_LABELS, STATE_TONES, canRetryFilter, isHolding } from '../scans'
import type { FilterRetryResponse } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'
import CommandBox from './CommandBox'
import FailureDetails from './FailureDetails'
import FilterRetryDialog from './FilterRetryDialog'

const POLL_MS = 3000

// One filter job in full: the job that makes a filtered dataset by copying the rows that were
// left into it. Its state and why, the dataset it was made from, how many rows it drops (by
// scanner and reason), the command that ran and the tails of its logs. It asks the backend again
// every 3 seconds while the job hasn't ended.
//
// A drop that failed or was cancelled has Retry: it opens a dialog that says what a retry would do
// and runs the same drop again from the rows it already chose, into a new folder of the same
// dataset. The failed attempt's folder and logs are kept.
//
// Safety: every value shown here comes from a file on disk or from SLURM and renders as plain
// React text, never as markup.
export default function FilterJob({
  datasetId,
  onChanged,
  startWithRetry = false,
}: {
  datasetId: string
  // Called after a retry has started, so the page that lists the jobs reads them again.
  onChanged?: () => void
  // Opens the retry dialog as soon as the job has loaded (the Activity page's Retry button, which
  // opens this card). Only read when the card first appears.
  startWithRetry?: boolean
}) {
  const { data: job, error, reload } = useRefreshing(
    () => getFilter(datasetId),
    datasetId,
    POLL_MS,
    (loaded) => isHolding(loaded.state),
  )
  const [retryOpen, setRetryOpen] = useState(startWithRetry)
  const [notice, setNotice] = useState<string | null>(null)

  function handleRetryStarted(response: FilterRetryResponse) {
    setRetryOpen(false)
    setNotice(
      response.submit_error === null
        ? `Started attempt ${response.attempt}. The failed attempt's folder and logs are kept.`
        : `The retry couldn't be submitted: ${response.submit_error}`,
    )
    reload()
    onChanged?.()
  }

  if (job === null) {
    return (
      <div className="card">
        {error !== null ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}
      </div>
    )
  }

  const logs = job.logs.filter((log) => log.tail !== null)
  const canRetry = canRetryFilter(job.state)
  const parentLink =
    job.parent_name === null ? (
      <span className="mono">{job.parent_id}</span>
    ) : (
      <a href={hrefFor({ page: 'dataset', id: job.parent_id })}>{job.parent_name}</a>
    )

  return (
    <div className="card filter-job">
      <div className="page-header">
        <h2>Drop rows: {job.dataset_name}</h2>
        <Chip label={STATE_LABELS[job.state]} tone={STATE_TONES[job.state]} title={job.reason ?? undefined} />
        {job.automatic !== null && (
          <Chip label="Automatic" title="Made by itself when the scan finished and its coverage check passed." />
        )}
        {canRetry && (
          <button
            type="button"
            className="secondary push-right"
            onClick={() => {
              setNotice(null)
              setRetryOpen(true)
            }}
            title="Runs this drop again with the rows it already chose. The failed attempt is kept."
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
      {error !== null && <p className="error">{error}</p>}
      {job.slurm_error !== null && (
        <div className="banner banner-warning small">
          The state of this job couldn't be read, so it may show as unknown. {job.slurm_error}
        </div>
      )}
      {job.state === 'failed' && (
        <FailureDetails
          reason={job.reason}
          failure={job.failure}
          after="Retry runs the same drop again. Or unregister this dataset and choose the rows again."
        />
      )}
      {job.reason !== null && job.state === 'cancelled' && (
        <div className="banner banner-warning small" role="alert">
          {job.reason}
        </div>
      )}

      <dl className="facts">
        <dt>New dataset</dt>
        <dd>
          <a href={hrefFor({ page: 'dataset', id: job.dataset_id })}>{job.dataset_name}</a>
        </dd>
        <dt>Made from</dt>
        <dd>{parentLink}, which isn't changed</dd>
        {job.automatic !== null && (
          <>
            <dt>Dropped</dt>
            <dd>
              Automatically, after scan{' '}
              <a className="mono" href={hrefFor({ page: 'activity', scan: job.automatic.scan_id })}>
                {job.automatic.scan_id}
              </a>{' '}
              passed its coverage check
            </dd>
          </>
        )}
        {job.drop !== null && (
          <>
            <dt>Rows dropped</dt>
            <dd>
              {formatCount(job.drop.rows)}
              {job.removed !== null && job.state === 'succeeded' && job.removed !== job.drop.rows && (
                <span className="muted"> (the filter reported {formatCount(job.removed)})</span>
              )}
            </dd>
          </>
        )}
        <dt>Started</dt>
        <dd>{job.submitted_at !== null ? formatTimestamp(job.submitted_at) : 'Not started'}</dd>
        {job.finished_at !== null && (
          <>
            <dt>Finished</dt>
            <dd>{formatTimestamp(job.finished_at)}</dd>
          </>
        )}
        {job.elapsed !== null && (
          <>
            <dt>Time used</dt>
            <dd>{job.elapsed}</dd>
          </>
        )}
        {job.job_id !== null && (
          <>
            <dt>SLURM job</dt>
            <dd>{job.job_id}</dd>
          </>
        )}
        {job.folder !== null && (
          <>
            <dt>Output folder</dt>
            <dd>
              <code>{job.folder}</code>
            </dd>
          </>
        )}
      </dl>

      {job.drop !== null && job.drop.by_scanner.length > 0 && (
        <>
          <h3>What was chosen to drop</h3>
          <table className="history-table">
            <thead>
              <tr>
                <th>Flagged by</th>
                <th>Rows</th>
                <th>Reasons</th>
                <th>Scan</th>
              </tr>
            </thead>
            <tbody>
              {job.drop.by_scanner.map((item) => (
                <tr key={item.scanner}>
                  <td>{item.label}</td>
                  <td>{formatCount(item.rows)}</td>
                  <td className="small">
                    {item.by_reason.map((entry) => `${reasonLabel(entry.reason)} ${formatCount(entry.count)}`).join(', ')}
                  </td>
                  <td>
                    <a className="mono" href={hrefFor({ page: 'activity', scan: item.scan_id })}>
                      {item.scan_id}
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <details open={job.state === 'failed'}>
        <summary className="small">Command and logs</summary>
        {job.command_text !== null ? (
          <CommandBox command={job.command_text} title="Submitted with" note="Exactly what was run to start this job." />
        ) : (
          <p className="muted small">No command was recorded.</p>
        )}
        {job.wrapper_output !== null && job.wrapper_output !== '' && (
          <>
            <h4>What the command printed</h4>
            <pre className="raw-json">{job.wrapper_output}</pre>
          </>
        )}
        {logs.map((log) => (
          <div key={log.name}>
            <h4>{log.name} (last lines)</h4>
            <p className="muted small">
              <code>{log.path}</code>
            </p>
            <pre className="raw-json">{log.tail || '(empty)'}</pre>
          </div>
        ))}
        {logs.length === 0 && <p className="muted small">No log yet.</p>}
      </details>

      {retryOpen && canRetry && (
        <FilterRetryDialog
          datasetId={job.dataset_id}
          onClose={() => setRetryOpen(false)}
          onStarted={handleRetryStarted}
        />
      )}
    </div>
  )
}
