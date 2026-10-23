import { Fragment, useEffect, useState } from 'react'
import { errorMessage, getScan, listDatasets } from '../api'
import { describeResources } from '../conversions'
import { formatCount, formatSetting, formatTimestamp } from '../format'
import { hrefFor } from '../route'
import { STATE_TONES, actionForScanner, canRetryScan, describeMode, isHolding, settingsToRepeat, stateLabelLong } from '../scans'
import type { Dataset, ScanDetail, ScanSubmitResponse } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'
import CommandBox from './CommandBox'
import FailureDetails from './FailureDetails'
import ScanDialog from './ScanDialog'

const POLL_MS = 3000

// The datasets of a scan that are still registered, as the scan dialog takes them, in the order
// the scan read them.
function datasetsStillRegistered(scan: ScanDetail, registered: Dataset[]): Dataset[] {
  const byId = new Map(registered.map((item) => [item.id, item]))
  return scan.datasets.flatMap((item) => {
    const found = byId.get(item.id)
    return found === undefined ? [] : [found]
  })
}

// What a finished scan found, as the scanner reports it: a number, a text or a small object.
function DetailValue({ value }: { value: unknown }) {
  if (typeof value === 'number') return <>{formatCount(value)}</>
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    return <pre className="raw-json">{JSON.stringify(value, null, 2)}</pre>
  }
  return <>{formatSetting(value)}</>
}

// One scan in full: its state and why, the datasets it covers, the command that ran, the
// Prepared version it read for each dataset, what it found (once finished) and the tails of
// its logs. It asks the backend again every 3 seconds while the scan hasn't ended.
//
// A scan that failed, was cancelled or never started has Retry: it opens the scan dialog filled in
// with the scan's datasets, settings and resources. The dialog checks them again like any other
// start and makes a new scan; this one is kept, so the reason it failed can still be read.
//
// Safety: every value shown here comes from a file on disk or from SLURM and renders as
// plain React text, never as markup.
export default function ScanJob({
  scanId,
  onChanged,
  startWithRetry = false,
}: {
  scanId: string
  // Called after a retry has started a new scan, so the page that lists the scans reads them again.
  onChanged?: () => void
  // Opens the retry dialog as soon as the scan has loaded (the Activity page's Retry button, which
  // opens this card). Only read when the card first appears.
  startWithRetry?: boolean
}) {
  const { data: scan, error } = useRefreshing(
    () => getScan(scanId),
    scanId,
    POLL_MS,
    (loaded) => isHolding(loaded.state),
  )
  // Whether Retry was pressed and the datasets it needs are being looked up.
  const [retryAsked, setRetryAsked] = useState(startWithRetry)
  // The datasets the retry dialog is open for; null while it is closed.
  const [retrying, setRetrying] = useState<Dataset[] | null>(null)
  const [retryError, setRetryError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!retryAsked || scan === null || !canRetryScan(scan.state)) return
    let cancelled = false
    listDatasets(true)
      .then(({ datasets }) => {
        if (cancelled) return
        // The datasets as they are registered now: one that was unregistered since can't be scanned.
        const chosen = datasetsStillRegistered(scan, datasets)
        setRetryAsked(false)
        if (chosen.length === 0) {
          setRetryError('None of the datasets this scan covered is registered any more.')
        } else {
          setRetryError(null)
          setRetrying(chosen)
        }
      })
      .catch((failure: unknown) => {
        if (cancelled) return
        setRetryAsked(false)
        setRetryError(errorMessage(failure))
      })
    return () => {
      cancelled = true
    }
  }, [retryAsked, scan])

  function handleStarted(response: ScanSubmitResponse) {
    setRetrying(null)
    const problems = response.errors.length > 0 ? ` Problems: ${response.errors.join(' ')}` : ''
    setNotice(
      `Started ${response.scans.length} new ${response.scans.length === 1 ? 'scan' : 'scans'}. ` +
        `It is at the top of the Activity list. This scan is kept so you can still read why it failed.${problems}`,
    )
    onChanged?.()
  }

  if (scan === null) {
    return (
      <div className="card">
        {error !== null ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}
      </div>
    )
  }

  const names = scan.datasets.map((dataset) => dataset.name ?? dataset.id)
  const logs = scan.logs.filter((log) => log.tail !== null)

  return (
    <div className="card scan-job">
      <div className="page-header">
        <h2>{scan.label} scan</h2>
        <Chip label={stateLabelLong(scan.state)} tone={STATE_TONES[scan.state]} title={scan.reason ?? undefined} />
        {scan.is_result && <Chip label="Current result" tone="ok" />}
        {canRetryScan(scan.state) && (
          <button
            type="button"
            className="secondary push-right"
            onClick={() => {
              setNotice(null)
              setRetryAsked(true)
            }}
            disabled={retryAsked}
            title="Opens the scan dialog filled in with this scan's datasets, settings and resources. This scan is kept."
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
      {error !== null && <p className="error">{error}</p>}
      {scan.slurm_error !== null && (
        <div className="banner banner-warning small">
          The state of this scan's job couldn't be read, so it may show as unknown. {scan.slurm_error}
        </div>
      )}
      {(scan.state === 'failed' || scan.state === 'conversion_failed') && (
        <FailureDetails reason={scan.reason} failure={scan.failure} />
      )}
      {scan.waiting_for.length > 0 && (
        <div className="banner banner-warning small">
          Waiting for {scan.waiting_for.length === 1 ? 'a conversion' : 'conversions'} to finish before it starts:
          <ul>
            {scan.waiting_for.map((wait) => (
              <li key={wait.job_id}>
                <a href={hrefFor({ page: 'dataset', id: wait.dataset_id, tab: 'conversion' })}>{wait.name}</a> (
                {wait.outcome}, conversion {wait.job_id})
              </li>
            ))}
          </ul>
        </div>
      )}

      {scan.progress !== null && scan.progress.length > 0 && (
        <div className="scan-progress">
          {scan.progress.map((item) => {
            const percent = item.total > 0 ? Math.round((item.done / item.total) * 100) : 0
            return (
              <div key={item.stage}>
                <p className="small">{item.stage}</p>
                <div className="progress-bar">
                  <div
                    className="progress-bar-track"
                    role="progressbar"
                    aria-valuenow={item.done}
                    aria-valuemin={0}
                    aria-valuemax={item.total}
                    aria-label={item.stage}
                  >
                    <div className="progress-bar-fill" style={{ width: `${percent}%` }} />
                  </div>
                  <span className="progress-bar-count muted small">
                    {formatCount(item.done)} of {formatCount(item.total)} {item.unit}
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <dl className="facts">
        <dt>Scan</dt>
        <dd className="mono">{scan.id}</dd>
        <dt>Datasets</dt>
        <dd>
          {scan.datasets.map((dataset, index) => (
            <span key={dataset.id}>
              {index > 0 && ', '}
              <a href={hrefFor({ page: 'dataset', id: dataset.id })}>{dataset.name ?? dataset.id}</a>
            </span>
          ))}
        </dd>
        {scan.mode !== null && (
          <>
            <dt>Compared</dt>
            <dd>{describeMode(scan.mode, names)}</dd>
          </>
        )}
        <dt>Started</dt>
        <dd>{scan.submitted_at !== null ? formatTimestamp(scan.submitted_at) : 'Not started yet'}</dd>
        {scan.finished_at !== null && (
          <>
            <dt>Finished</dt>
            <dd>{formatTimestamp(scan.finished_at)}</dd>
          </>
        )}
        {scan.elapsed !== null && (
          <>
            <dt>Time used</dt>
            <dd>{scan.elapsed}</dd>
          </>
        )}
        {scan.jobs.length > 0 && (
          <>
            <dt>{scan.jobs.length === 1 ? 'SLURM job' : 'SLURM jobs'}</dt>
            <dd>
              {scan.jobs.length === 1
                ? scan.jobs[0].job_id
                : scan.jobs.map((job) => `${job.role} ${job.job_id}`).join(', ')}
            </dd>
          </>
        )}
        <dt>Resources</dt>
        <dd>{describeResources(scan.resources)}</dd>
        <dt>Settings</dt>
        <dd>
          {Object.entries(scan.settings)
            .map(([name, value]) => `${name}: ${formatSetting(value)}`)
            .join(', ') || '(none)'}
        </dd>
        <dt>Folder</dt>
        <dd>
          <code>{scan.folder}</code>
        </dd>
      </dl>

      {scan.versions !== null && Object.keys(scan.versions).length > 0 && (
        <>
          <h3>Converted data it read</h3>
          <table className="history-table">
            <thead>
              <tr>
                <th>Dataset</th>
                <th>Version</th>
                <th>Rows</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(scan.versions).map(([datasetId, version]) => (
                <tr key={datasetId}>
                  <td>{scan.datasets.find((dataset) => dataset.id === datasetId)?.name ?? datasetId}</td>
                  <td className="mono">{version.version_id}</td>
                  <td>{formatCount(version.rows)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {scan.details !== null && Object.keys(scan.details).length > 0 && (
        <>
          <h3>What it found</h3>
          <dl className="facts">
            {Object.entries(scan.details).map(([key, value]) => (
              <Fragment key={key}>
                <dt>{key.replace(/_/g, ' ')}</dt>
                <dd>
                  <DetailValue value={value} />
                </dd>
              </Fragment>
            ))}
          </dl>
        </>
      )}

      <details open={scan.state === 'failed'}>
        <summary className="small">Command and logs</summary>
        {scan.command_text !== null ? (
          <CommandBox command={scan.command_text} title="Submitted with" note="Exactly what was run to start this scan." />
        ) : (
          <p className="muted small">No command yet: the scan hasn't been started.</p>
        )}
        {scan.wrapper_output !== null && scan.wrapper_output !== '' && (
          <>
            <h4>What the command printed</h4>
            <pre className="raw-json">{scan.wrapper_output}</pre>
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

      {retrying !== null && (
        <ScanDialog
          action={actionForScanner(scan.scanner)}
          datasets={retrying}
          initial={{
            scannerKey: scan.scanner,
            settings: settingsToRepeat(scan.scanner, scan.settings),
            mode: scan.mode,
            autoDrop: scan.auto_drop,
            resources: scan.resources,
          }}
          onClose={() => setRetrying(null)}
          onStarted={handleStarted}
        />
      )}
    </div>
  )
}
