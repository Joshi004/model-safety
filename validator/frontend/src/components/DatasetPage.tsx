import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, errorMessage, getDataset } from '../api'
import { EMPTY_SELECTION, clearAll } from '../drop'
import type { DropSelection } from '../drop'
import { formatCount, formatTimestamp } from '../format'
import { DATASET_TABS, hrefFor } from '../route'
import type { DatasetTab } from '../route'
import type { Dataset, DropSubmitResponse } from '../types'
import Chip from './Chip'
import ConversionTab from './ConversionTab'
import DataTab from './DataTab'
import { DropBar } from './DropDialog'
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

  // The rows chosen to drop, from every result page of this dataset. They belong to one version of
  // its converted data: when that changes (it is converted again) they are forgotten, which is
  // worked out here instead of being reset by an effect.
  const version = loaded.status === 'ready' ? (loaded.dataset.prepared?.version_id ?? null) : null
  const [chosen, setChosen] = useState<{ version: string | null; selection: DropSelection }>({
    version: null,
    selection: EMPTY_SELECTION,
  })
  const selection = chosen.version === version ? chosen.selection : EMPTY_SELECTION
  const changeSelection = useCallback(
    (change: (current: DropSelection) => DropSelection) =>
      setChosen((current) => ({
        version,
        selection: change(current.version === version ? current.selection : EMPTY_SELECTION),
      })),
    [version],
  )
  // What to say after rows were dropped: where the new dataset is, or why it didn't start.
  const [notice, setNotice] = useState<{ tone: 'ok' | 'error'; content: ReactNode } | null>(null)

  function dropped(response: DropSubmitResponse, parentName: string) {
    changeSelection(clearAll)
    const rows = `${formatCount(response.rows)} ${response.rows === 1 ? 'row' : 'rows'}`
    const link = <a href={hrefFor({ page: 'dataset', id: response.dataset_id })}>{response.name}</a>
    setNotice(
      response.submit_error === null
        ? {
            tone: 'ok',
            content: (
              <>
                Dropping {rows} from {parentName}: {link} is being made. Watch it on the{' '}
                <a href={hrefFor({ page: 'activity' })}>Activity page</a>. {parentName} itself isn't changed.
              </>
            ),
          }
        : {
            tone: 'error',
            content: (
              <>
                {link} was made, but its job didn't start: {response.submit_error} Unregister it, then try again.
              </>
            ),
          },
    )
    reload()
  }

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

          {notice !== null && (
            <div className={notice.tone === 'ok' ? 'banner banner-ok' : 'banner banner-error'} role="status">
              <span>{notice.content}</span>
              <button type="button" className="link-button" onClick={() => setNotice(null)}>
                Dismiss
              </button>
            </div>
          )}

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
              <ResultPage
                key={scanner}
                dataset={loaded.dataset}
                scanner={scanner}
                selection={selection}
                onSelectionChange={changeSelection}
                onChanged={reload}
              />
            ))}
          {tab === 'history' && <HistoryTab dataset={loaded.dataset} onChanged={reload} />}
          {tab === 'results' && (
            <DropBar
              dataset={loaded.dataset}
              selection={selection}
              onClear={() => changeSelection(clearAll)}
              onDropped={(response) => dropped(response, loaded.dataset.name)}
            />
          )}
        </>
      )}
    </>
  )
}
