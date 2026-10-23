import { useEffect, useRef } from 'react'
import { listConversions, listScans } from '../api'
import { OUTCOME_LABELS, OUTCOME_TONES } from '../conversions'
import { formatCount, formatTimestamp } from '../format'
import { hrefFor } from '../route'
import { STATE_TONES, describeMode, isHolding, stateLabelLong } from '../scans'
import type { ConversionJob, Dataset } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'

const POLL_MS = 3000

function rowsOf(job: ConversionJob): number | null {
  return job.result?.rows ?? job.summary?.written ?? null
}

// Where a conversion's rows are now.
function dataState(job: ConversionJob) {
  if (job.current) return <Chip label="Current" tone="ok" />
  if (job.folder_exists) return <span className="muted">On disk, not current</span>
  return <span className="muted">Deleted</span>
}

// The scans that include this dataset, newest first. A scan across several datasets is listed
// on each of them. It asks again every 3 seconds while one of them hasn't ended.
function ScansHistory({ dataset }: { dataset: Dataset }) {
  const { data, error } = useRefreshing(
    listScans,
    dataset.id,
    POLL_MS,
    (response) => response.scans.some((scan) => isHolding(scan.state) && scan.datasets.some((item) => item.id === dataset.id)),
    'scans',
  )
  const scans = data?.scans.filter((scan) => scan.datasets.some((item) => item.id === dataset.id)) ?? []

  return (
    <div className="card">
      <h2>Scans</h2>
      {error !== null && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data?.slurm_error && (
        <div className="banner banner-warning small">
          The state of running jobs couldn't be read, so some may show as unknown. {data.slurm_error}
        </div>
      )}
      {data !== null && scans.length === 0 && <p className="muted">No scan has included this dataset yet.</p>}
      {scans.length > 0 && (
        <table className="history-table">
          <thead>
            <tr>
              <th>Started</th>
              <th>Scan</th>
              <th>Compared</th>
              <th>State</th>
              <th>Result</th>
              <th>
                <span className="visually-hidden">Details</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {scans.map((scan) => (
              <tr key={scan.id}>
                <td>{formatTimestamp(scan.submitted_at ?? scan.created_at ?? '')}</td>
                <td>{scan.label}</td>
                <td>{describeMode(scan.mode, scan.datasets.map((item) => item.name ?? item.id))}</td>
                <td>
                  <Chip label={stateLabelLong(scan.state)} tone={STATE_TONES[scan.state]} title={scan.reason ?? undefined} />
                </td>
                <td>{scan.is_result && <Chip label="Current result" tone="ok" />}</td>
                <td>
                  <a href={hrefFor({ page: 'activity' })}>Activity</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// The conversions of this dataset, newest first. It asks again every 3 seconds while one is
// going, and calls `onChanged` when the last one stops, so the page reads the dataset again.
function ConversionsHistory({ dataset, onChanged }: { dataset: Dataset; onChanged: () => void }) {
  const { data, error } = useRefreshing(
    () => listConversions(dataset.id),
    dataset.id,
    POLL_MS,
    (response) => response.has_active,
    `conversions:${dataset.id}`,
  )

  const hasActive = data?.has_active ?? null
  const lastHasActive = useRef<boolean | null>(null)
  useEffect(() => {
    if (hasActive === null) return
    if (lastHasActive.current === true && !hasActive) onChanged()
    lastHasActive.current = hasActive
  }, [hasActive, onChanged])

  return (
    <div className="card">
      <h2>Conversions</h2>
      {error !== null && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data?.slurm_error && (
        <div className="banner banner-warning small">
          The state of running jobs couldn't be read, so some outcomes may show as unknown. {data.slurm_error}
        </div>
      )}
      {data?.problems.map((problem) => (
        <div key={problem} className="banner banner-warning small">
          {problem}
        </div>
      ))}
      {data !== null && data.conversions.length === 0 && (
        <p className="muted">This dataset hasn't been converted yet. Start a conversion from its Conversion tab.</p>
      )}
      {data !== null && data.conversions.length > 0 && (
        <table className="history-table">
          <thead>
            <tr>
              <th>Started</th>
              <th>Kind</th>
              <th>Outcome</th>
              <th>Rows</th>
              <th>Time used</th>
              <th>Converted data</th>
            </tr>
          </thead>
          <tbody>
            {data.conversions.map((job) => (
              <tr key={job.job_id}>
                <td>{formatTimestamp(job.submitted_at ?? job.created_at ?? '')}</td>
                <td>{job.reconvert ? 'Convert again' : 'Convert'}</td>
                <td>
                  <Chip label={OUTCOME_LABELS[job.outcome]} tone={OUTCOME_TONES[job.outcome]} title={job.reason ?? undefined} />
                </td>
                <td>{formatCount(rowsOf(job))}</td>
                <td>{job.elapsed ?? '—'}</td>
                <td>{dataState(job)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// What happened to this dataset: its conversions, and the scans that included it.
export default function HistoryTab({ dataset, onChanged }: { dataset: Dataset; onChanged: () => void }) {
  return (
    <>
      <ConversionsHistory dataset={dataset} onChanged={onChanged} />
      <ScansHistory dataset={dataset} />
    </>
  )
}
