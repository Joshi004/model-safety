import { useState } from 'react'
import { cancelConversion, cancelFilter, cancelScan, errorMessage, listAllConversions, listFilters, listScans } from '../api'
import { OUTCOME_LABELS, OUTCOME_TONES, isActive } from '../conversions'
import { formatDuration, formatTimestamp, secondsBetween } from '../format'
import { hrefFor } from '../route'
import { STATE_LABELS, STATE_TONES, isHolding, stateLabelLong } from '../scans'
import type { ActivityConversion, AllConversionsResponse, FilterJob as FilterJobView, FiltersResponse, ScanView, ScansResponse } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'
import ConversionJob from './ConversionJob'
import FilterJob from './FilterJob'
import ScanJob from './ScanJob'

const POLL_MS = 5000
const COLUMNS = 6

interface Activity {
  scans: ScansResponse
  conversions: AllConversionsResponse
  filters: FiltersResponse
}

// One line of the page: a conversion, a scan, or a filter job (rows dropped from a dataset).
type Item =
  | { kind: 'scan'; scan: ScanView }
  | { kind: 'conversion'; conversion: ActivityConversion }
  | { kind: 'filter'; filter: FilterJobView }

async function loadActivity(): Promise<Activity> {
  const [scans, conversions, filters] = await Promise.all([listScans(), listAllConversions(), listFilters()])
  return { scans, conversions, filters }
}

// The id a scan, a conversion or a filter job has begins with the UTC time it was made, so a
// bigger id is a newer job. (A filter job's is the id of the version it makes; one whose record
// can't be read has none, and goes last.)
function itemId(item: Item): string {
  if (item.kind === 'scan') return item.scan.id
  if (item.kind === 'conversion') return item.conversion.job_id
  return item.filter.version_id ?? ''
}

function itemKey(item: Item): string {
  if (item.kind === 'scan') return `scan/${item.scan.id}`
  if (item.kind === 'conversion') return `conversion/${item.conversion.dataset_id}/${item.conversion.job_id}`
  return `filter/${item.filter.dataset_id}`
}

// Whether the job hasn't ended: it can be cancelled, and the page keeps asking about it.
function isGoing(item: Item): boolean {
  if (item.kind === 'scan') return isHolding(item.scan.state)
  if (item.kind === 'conversion') return isActive(item.conversion.outcome)
  return isHolding(item.filter.state)
}

function mergeItems(activity: Activity): Item[] {
  const items: Item[] = [
    ...activity.scans.scans.map((scan): Item => ({ kind: 'scan', scan })),
    ...activity.conversions.conversions.map((conversion): Item => ({ kind: 'conversion', conversion })),
    ...activity.filters.filters.map((filter): Item => ({ kind: 'filter', filter })),
  ]
  return items.sort((a, b) => (itemId(a) < itemId(b) ? 1 : itemId(a) > itemId(b) ? -1 : 0))
}

function kindOf(item: Item): string {
  if (item.kind === 'conversion') return item.conversion.reconvert ? 'Convert again' : 'Conversion'
  if (item.kind === 'filter') {
    const rows = item.filter.removed
    const drop = item.filter.automatic !== null ? 'Automatic drop' : 'Drop'
    return rows === null ? `${drop} of rows` : `${drop} of ${rows.toLocaleString()} ${rows === 1 ? 'row' : 'rows'}`
  }
  const { scan } = item
  const across = scan.mode === 'across' && scan.datasets.length > 1 ? ` (across ${scan.datasets.length})` : ''
  return `${scan.label} scan${across}`
}

function startedOf(item: Item): string {
  const job = item.kind === 'scan' ? item.scan : item.kind === 'conversion' ? item.conversion : item.filter
  const time = job.submitted_at ?? job.created_at
  return time === null ? '—' : formatTimestamp(time)
}

// SLURM's run time while it knows the job. A scan (or a filter job) that already became a result
// is not asked about, so its time is from the start to the finish (the wait in the queue included).
function timeOf(item: Item): string {
  if (item.kind === 'conversion') return item.conversion.elapsed ?? '—'
  if (item.kind === 'filter') {
    const { filter } = item
    if (filter.elapsed !== null) return filter.elapsed
    const seconds = filter.state === 'succeeded' ? secondsBetween(filter.submitted_at, filter.finished_at) : null
    return seconds === null ? '—' : formatDuration(seconds)
  }
  const { scan } = item
  if (scan.elapsed !== null) return scan.elapsed
  const seconds = scan.state === 'succeeded' ? secondsBetween(scan.submitted_at, scan.finished_at) : null
  return seconds === null ? '—' : formatDuration(seconds)
}

function StateCell({ item }: { item: Item }) {
  if (item.kind === 'conversion') {
    const { conversion } = item
    return (
      <>
        <Chip label={OUTCOME_LABELS[conversion.outcome]} tone={OUTCOME_TONES[conversion.outcome]} />
        {conversion.reason !== null && <div className="muted small">{conversion.reason}</div>}
      </>
    )
  }
  if (item.kind === 'filter') {
    const { filter } = item
    return (
      <>
        <Chip label={STATE_LABELS[filter.state]} tone={STATE_TONES[filter.state]} />
        {filter.reason !== null && <div className="muted small">{filter.reason}</div>}
      </>
    )
  }
  const { scan } = item
  return (
    <>
      <Chip label={stateLabelLong(scan.state)} tone={STATE_TONES[scan.state]} />
      {scan.waiting_for.map((wait) => (
        <div key={wait.job_id} className="muted small">
          Waiting for the conversion of {wait.name} ({wait.job_id}, {wait.outcome})
        </div>
      ))}
      {scan.reason !== null && <div className="muted small">{scan.reason}</div>}
    </>
  )
}

function DatasetsCell({ item }: { item: Item }) {
  if (item.kind === 'conversion') {
    return <a href={hrefFor({ page: 'dataset', id: item.conversion.dataset_id })}>{item.conversion.dataset_name}</a>
  }
  if (item.kind === 'filter') {
    const { filter } = item
    return (
      <>
        <a href={hrefFor({ page: 'dataset', id: filter.dataset_id })}>{filter.dataset_name}</a>
        {filter.parent_name !== null && (
          <span className="muted small filter-lineage">
            from <a href={hrefFor({ page: 'dataset', id: filter.parent_id })}>{filter.parent_name}</a>
          </span>
        )}
        {filter.automatic !== null && (
          <span className="muted small filter-lineage">
            automatic, after scan{' '}
            <a className="mono" href={hrefFor({ page: 'activity', scan: filter.automatic.scan_id })}>
              {filter.automatic.scan_id}
            </a>
          </span>
        )}
      </>
    )
  }
  return (
    <>
      {item.scan.datasets.map((dataset, index) => (
        <span key={dataset.id}>
          {index > 0 && ', '}
          <a href={hrefFor({ page: 'dataset', id: dataset.id })}>{dataset.name ?? dataset.id}</a>
        </span>
      ))}
    </>
  )
}

// Every conversion, scan and filter job (rows dropped from a dataset), newest first, with its
// state, the datasets it is about, how long it has run, Cancel while it is going, and the job in
// full under "Details". It asks again every 5 seconds while anything is going. A scan that waits
// for a conversion says which.
//
// `scan` is a scan to show with its details open and in view (a link from a result page).
//
// Everything shown comes from SLURM or from files on disk and renders as plain text.
export default function ActivityPage({ scan }: { scan: string | null }) {
  const { data, error, reload } = useRefreshing(loadActivity, 'activity', POLL_MS, (loaded) =>
    mergeItems(loaded).some(isGoing),
  )
  // The jobs whose details are open, by `itemKey`.
  const [open, setOpen] = useState<Set<string>>(() => new Set(scan === null ? [] : [`scan/${scan}`]))
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const items = data === null ? [] : mergeItems(data)
  const slurmErrors =
    data === null
      ? []
      : [
          ...new Set(
            [data.scans.slurm_error, data.conversions.slurm_error, data.filters.slurm_error].filter(
              (text): text is string => text !== null,
            ),
          ),
        ]
  const problems = data === null ? [] : [...new Set([...data.scans.problems, ...data.conversions.problems])]
  // A scan that was asked for but isn't listed: a newer scan replaced it and it was deleted (D5).
  const scanMissing = scan !== null && data !== null && !items.some((item) => item.kind === 'scan' && item.scan.id === scan)

  function toggle(key: string) {
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  async function cancel(item: Item) {
    setBusyKey(itemKey(item))
    setNotice(null)
    setActionError(null)
    try {
      if (item.kind === 'scan') {
        const result = await cancelScan(item.scan.id)
        setNotice(
          result.stopped_waiting
            ? "The scan was stopped before it started. The conversions it was waiting for keep running: cancel them here too if you don't want them."
            : result.cancelled.length > 0
              ? 'The scan was cancelled.'
              : 'The scan was no longer running, so nothing was cancelled.',
        )
      } else if (item.kind === 'filter') {
        const result = await cancelFilter(item.filter.dataset_id)
        setNotice(
          result.cancelled.length > 0
            ? 'The filter job was cancelled. Its dataset has no data: unregister it, and drop the rows again if you still want them dropped.'
            : 'The filter job was no longer running, so nothing was cancelled.',
        )
      } else {
        const result = await cancelConversion(item.conversion.dataset_id, item.conversion.job_id)
        setNotice(
          result.cancelled.length > 0
            ? 'The conversion was cancelled.'
            : 'The conversion was no longer running, so nothing was cancelled.',
        )
      }
      reload()
    } catch (failure) {
      setActionError(errorMessage(failure))
    } finally {
      setBusyKey(null)
    }
  }

  return (
    <>
      <div className="page-header">
        <h1>Activity</h1>
        <button type="button" className="secondary push-right" onClick={reload}>
          Refresh
        </button>
      </div>

      {notice !== null && (
        <div className="banner banner-ok" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      {actionError !== null && (
        <div className="banner banner-error" role="alert">
          {actionError}
        </div>
      )}
      {error !== null && (
        <div className="banner banner-error" role="alert">
          <span>Couldn't load the activity: {error}</span>
          <button type="button" className="secondary" onClick={reload}>
            Retry
          </button>
        </div>
      )}
      {scanMissing && (
        <div className="banner banner-warning small">
          Scan {scan} isn't in the list. A newer scan of the same kind may have replaced it, and then the old one is deleted.
        </div>
      )}
      {slurmErrors.map((text) => (
        <div key={text} className="banner banner-warning small">
          The state of running jobs couldn't be read, so some may show as unknown. {text}
        </div>
      ))}
      {problems.map((problem) => (
        <div key={problem} className="banner banner-warning small">
          {problem}
        </div>
      ))}

      {data === null && error === null && (
        <div className="card empty-state">
          <p>Loading…</p>
        </div>
      )}

      {data !== null && items.length === 0 && (
        <div className="card empty-state">
          <p>
            <strong>Nothing has been started yet.</strong>
          </p>
          <p className="muted">
            Conversions and scans are listed here, newest first, once you start one from the{' '}
            <a href={hrefFor({ page: 'datasets' })}>Datasets page</a>.
          </p>
        </div>
      )}

      {items.length > 0 && (
        <div className="card">
          <table className="history-table activity-table">
            <thead>
              <tr>
                <th>Job</th>
                <th>State</th>
                <th>Datasets</th>
                <th>Started</th>
                <th>Time used</th>
                <th>
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const key = itemKey(item)
                const going = isGoing(item)
                return (
                  <ActivityRows
                    key={key}
                    item={item}
                    going={going}
                    open={open.has(key)}
                    focus={item.kind === 'scan' && item.scan.id === scan}
                    busy={busyKey === key}
                    onToggle={() => toggle(key)}
                    onCancel={() => void cancel(item)}
                    onChanged={reload}
                  />
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

// Scrolls the line a link pointed at into view when it first appears. A function that never
// changes, so React calls it once, when the line is added.
function scrollIntoView(element: HTMLElement | null) {
  element?.scrollIntoView({ block: 'center' })
}

// A job's line, and under it, when open, the job in full.
function ActivityRows({
  item,
  going,
  open,
  focus,
  busy,
  onToggle,
  onCancel,
  onChanged,
}: {
  item: Item
  going: boolean
  open: boolean
  focus: boolean
  busy: boolean
  onToggle: () => void
  onCancel: () => void
  onChanged: () => void
}) {
  return (
    <>
      <tr ref={focus ? scrollIntoView : undefined} className={focus ? 'activity-focus' : undefined}>
        <td>{kindOf(item)}</td>
        <td>
          <StateCell item={item} />
        </td>
        <td>
          <DatasetsCell item={item} />
        </td>
        <td>{startedOf(item)}</td>
        <td>{timeOf(item)}</td>
        <td className="activity-actions">
          {going && (
            <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
              {busy ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
          <button type="button" className="secondary" onClick={onToggle} aria-expanded={open}>
            {open ? 'Hide' : 'Details'}
          </button>
        </td>
      </tr>
      {open && (
        <tr className="activity-detail">
          <td colSpan={COLUMNS}>
            {item.kind === 'scan' ? (
              <ScanJob scanId={item.scan.id} />
            ) : item.kind === 'filter' ? (
              <FilterJob datasetId={item.filter.dataset_id} />
            ) : (
              <ConversionJob
                datasetId={item.conversion.dataset_id}
                jobId={item.conversion.job_id}
                onChanged={onChanged}
                title={`Conversion of ${item.conversion.dataset_name}`}
              />
            )}
          </td>
        </tr>
      )}
    </>
  )
}
