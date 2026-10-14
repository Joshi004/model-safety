import { useEffect, useState } from 'react'
import { ApiError, errorMessage, getDataset } from '../api'
import { formatTimestamp } from '../format'
import { hrefFor } from '../route'
import type { Dataset } from '../types'
import Chip from './Chip'

type Loaded = { status: 'loading' } | { status: 'ready'; dataset: Dataset } | { status: 'error'; message: string }

// A placeholder: the dataset's overview, data, conversion, results and history come later.
export default function DatasetPage({ datasetId }: { datasetId: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ status: 'loading' })

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
          message: notFound
            ? 'No dataset has this id. It may have been unregistered.'
            : errorMessage(error),
        })
      })
    return () => {
      cancelled = true
    }
  }, [datasetId])

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
              <dt>Registered</dt>
              <dd>{formatTimestamp(loaded.dataset.created_at)}</dd>
            </dl>
            <p className="muted">More about this dataset is coming.</p>
          </div>
        </>
      )}
    </>
  )
}
