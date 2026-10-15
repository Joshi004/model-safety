import { useCallback, useEffect, useState } from 'react'
import { ApiError, errorMessage, getDataset } from '../api'
import { formatCount, formatTimestamp } from '../format'
import { DATASET_TABS, hrefFor } from '../route'
import type { DatasetTab } from '../route'
import type { Dataset } from '../types'
import Chip from './Chip'
import ConversionTab from './ConversionTab'
import DataTab from './DataTab'
import HistoryTab from './HistoryTab'
import OverviewTab from './OverviewTab'
import ResultPage from './ResultPage'
import ResultsTab from './ResultsTab'
import ScannerChips from './ScannerChips'

type Loaded = { status: 'loading' } | { status: 'ready'; dataset: Dataset } | { status: 'error'; message: string }

const TAB_LABELS: Record<DatasetTab, string> = {
  overview: 'Overview',
  data: 'Data',
  conversion: 'Conversion',
  results: 'Results',
  history: 'History',
}

// One dataset: a header with what it is, its current Prepared version and the state of each
// scanner on it, and a tab for each thing to do or look at. `scanner` is only set on the
// results tab: the result page of that scanner. Everything shown is plain text.
export default function DatasetPage({
  datasetId,
  tab,
  scanner,
}: {
  datasetId: string
  tab: DatasetTab
  scanner: string | null
}) {
  const [loaded, setLoaded] = useState<Loaded>({ status: 'loading' })
  // The Conversion tab holds edits that aren't saved yet, so once it has been opened it
  // stays mounted (hidden) while another tab is shown.
  const [conversionOpened, setConversionOpened] = useState(tab === 'conversion')
  if (tab === 'conversion' && !conversionOpened) setConversionOpened(true)

  useEffect(() => {
    let cancelled = false
    getDataset(datasetId)
      .then((dataset) => {
        if (!cancelled) setLoaded({ status: 'ready', dataset })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const notFound = error instanceof ApiError && error.status === 404
        setLoaded({
          status: 'error',
          message: notFound ? 'No dataset has this id. It may have been unregistered.' : errorMessage(error),
        })
      })
    return () => {
      cancelled = true
    }
  }, [datasetId])

  // Reads the dataset again (a conversion started or finished) without going back to "Loading".
  // A failure leaves what is shown as it is.
  const reload = useCallback(() => {
    getDataset(datasetId)
      .then((dataset) => setLoaded({ status: 'ready', dataset }))
      .catch(() => undefined)
  }, [datasetId])

  const setDataset = useCallback((dataset: Dataset) => setLoaded({ status: 'ready', dataset }), [])

  return (
    <>
      <p className="back-link">
        <a href={hrefFor({ page: 'datasets' })}>← Datasets</a>
      </p>

      {loaded.status === 'loading' && <p className="muted">Loading…</p>}

      {loaded.status === 'error' && (
        <div className="banner banner-error" role="alert">
          {loaded.message}
        </div>
      )}

      {loaded.status === 'ready' && (
        <>
          <div className="page-header">
            <h1>{loaded.dataset.name}</h1>
            <Chip label={loaded.dataset.team} />
            {loaded.dataset.available ? (
              <Chip label="Available" tone="ok" />
            ) : (
              <Chip label="Unavailable" tone="error" title={loaded.dataset.unavailable_reason ?? undefined} />
            )}
          </div>

          <div className="card">
            <dl className="facts">
              <dt>Path</dt>
              <dd>
                <code>{loaded.dataset.path}</code>
              </dd>
              {!loaded.dataset.available && loaded.dataset.unavailable_reason && (
                <>
                  <dt>Problem</dt>
                  <dd>{loaded.dataset.unavailable_reason}</dd>
                </>
              )}
              <dt>Converted data</dt>
              <dd>
                {loaded.dataset.prepared !== null ? (
                  <>
                    {formatCount(loaded.dataset.prepared.rows)} rows
                    {loaded.dataset.prepared.finished_at !== null &&
                      `, converted ${formatTimestamp(loaded.dataset.prepared.finished_at)}`}{' '}
                    <span className="muted">(version {loaded.dataset.prepared.version_id})</span>
                  </>
                ) : (
                  <span className="muted">Not converted</span>
                )}
              </dd>
              <dt>Scans</dt>
              <dd className="scans-cell">
                <ScannerChips datasetId={loaded.dataset.id} scans={loaded.dataset.scans} />
              </dd>
              <dt>Registered</dt>
              <dd>{formatTimestamp(loaded.dataset.created_at)}</dd>
            </dl>
          </div>

          <div className="tabs" role="tablist" aria-label="Dataset">
            {DATASET_TABS.map((name) => (
              <a
                key={name}
                role="tab"
                aria-selected={tab === name}
                className={tab === name ? 'tab selected' : 'tab'}
                href={hrefFor({ page: 'dataset', id: datasetId, tab: name })}
              >
                {TAB_LABELS[name]}
              </a>
            ))}
          </div>

          {tab === 'overview' && <OverviewTab dataset={loaded.dataset} />}
          {tab === 'data' && <DataTab dataset={loaded.dataset} />}
          {conversionOpened && (
            <div hidden={tab !== 'conversion'}>
              <ConversionTab dataset={loaded.dataset} onDatasetChanged={setDataset} onReload={reload} />
            </div>
          )}
          {tab === 'results' &&
            (scanner === null ? (
              <ResultsTab dataset={loaded.dataset} onChanged={reload} />
            ) : (
              <ResultPage key={scanner} dataset={loaded.dataset} scanner={scanner} onChanged={reload} />
            ))}
          {tab === 'history' && <HistoryTab dataset={loaded.dataset} onChanged={reload} />}
        </>
      )}
    </>
  )
}
