import { useEffect, useState } from 'react'
import { errorMessage, getDatasetPolicy } from '../api'
import { sourceLabel } from '../policy'
import { hrefFor } from '../route'
import type { Dataset, DatasetPolicy } from '../types'

type Loaded = { status: 'loading' } | { status: 'ready'; policy: DatasetPolicy } | { status: 'error'; message: string }

// The policy one dataset would be scanned with: where it comes from, and its text to read.
function PolicyItem({ dataset }: { dataset: Dataset }) {
  const [loaded, setLoaded] = useState<Loaded>({ status: 'loading' })

  useEffect(() => {
    // An answer for a dataset that was left out of the scan, or after the dialog closed, is dropped.
    let stale = false
    getDatasetPolicy(dataset.id)
      .then((policy) => {
        if (!stale) setLoaded({ status: 'ready', policy })
      })
      .catch((failure: unknown) => {
        if (!stale) setLoaded({ status: 'error', message: errorMessage(failure) })
      })
    return () => {
      stale = true
    }
  }, [dataset.id])

  return (
    <li>
      <strong>{dataset.name}</strong>:{' '}
      {loaded.status === 'loading' && <span className="muted">Loading…</span>}
      {loaded.status === 'error' && <span className="error">couldn't read its policy: {loaded.message}</span>}
      {loaded.status === 'ready' && sourceLabel(loaded.policy.source, loaded.policy.team)}{' '}
      <a href={hrefFor({ page: 'dataset', id: dataset.id, tab: 'policy' })}>View or change</a>
      {loaded.status === 'ready' && (
        <details>
          <summary className="small">Show the policy</summary>
          <pre className="policy-view">{loaded.policy.text}</pre>
        </details>
      )}
    </li>
  )
}

// What the toxicity scan dialog says about the policy: one line for each chosen dataset, read-only.
// Changing a policy is done on the dataset's own Toxicity policy tab, not here.
export default function ScanPolicies({ datasets }: { datasets: Dataset[] }) {
  return (
    <div className="scan-setting">
      <h3 className="scan-heading">Toxicity policy</h3>
      <p className="muted small">
        The policy each dataset is checked against. The scan keeps a copy of it as it is when the scan starts. To change
        one, open the dataset's Toxicity policy tab.
      </p>
      <ul className="policy-list">
        {datasets.map((dataset) => (
          <PolicyItem key={dataset.id} dataset={dataset} />
        ))}
      </ul>
    </div>
  )
}
