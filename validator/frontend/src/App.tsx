import { useEffect, useState } from 'react'
import './App.css'
import { getHealth } from './api'
import ActivityPage from './components/ActivityPage'
import DatasetPage from './components/DatasetPage'
import DatasetsPage from './components/DatasetsPage'
import { hrefFor, useRoute } from './route'

// A placeholder until the team picks a name (D13). Keep it equal to the <title> in index.html.
const PRODUCT_NAME = 'Data Safety Scanner'

export default function App() {
  const route = useRoute()
  const [backendOk, setBackendOk] = useState<boolean | null>(null)

  useEffect(() => {
    getHealth()
      .then(() => setBackendOk(true))
      .catch(() => setBackendOk(false))
  }, [])

  // A dataset's own page belongs to Datasets.
  const inDatasets = route.page === 'datasets' || route.page === 'dataset'

  return (
    <>
      <header className="top-bar">
        <a className="product-name" href={hrefFor({ page: 'datasets' })}>
          {PRODUCT_NAME}
        </a>
        <nav className="top-nav" aria-label="Main">
          <a
            href={hrefFor({ page: 'datasets' })}
            className={inDatasets ? 'nav-link current' : 'nav-link'}
            aria-current={inDatasets ? 'page' : undefined}
          >
            Datasets
          </a>
          <a
            href={hrefFor({ page: 'activity' })}
            className={route.page === 'activity' ? 'nav-link current' : 'nav-link'}
            aria-current={route.page === 'activity' ? 'page' : undefined}
          >
            Activity
          </a>
        </nav>
      </header>

      <main className="page">
        {backendOk === false && (
          <div className="banner banner-error" role="alert">
            Couldn't reach the backend. Make sure <code>./validator/run.sh</code> is still running, and open the
            exact URL it printed.
          </div>
        )}
        {route.page === 'datasets' && <DatasetsPage />}
        {route.page === 'dataset' && <DatasetPage key={route.id} datasetId={route.id} />}
        {route.page === 'activity' && <ActivityPage />}
      </main>
    </>
  )
}
