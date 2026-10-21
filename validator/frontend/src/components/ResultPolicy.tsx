import { sourceLabel } from '../policy'
import { hrefFor } from '../route'
import type { ScanPolicy } from '../types'

// The toxicity policy a scan used, as the scan copied it when it started. Shown read-only: the
// saved policy may have been edited since, and this result is still the one made with this text.
export default function ResultPolicy({ policy, datasetId }: { policy: ScanPolicy; datasetId: string }) {
  return (
    <div className="card">
      <h3 className="scan-heading">Toxicity policy the scan used</h3>
      <p className="small">
        From {sourceLabel(policy.source, policy.team)}, as it was when the scan started (SHA-256{' '}
        <code>{policy.sha256.slice(0, 12)}</code>).
      </p>
      <p className="muted small">
        Changing the policy doesn't change this result: it takes a new scan. Change it on the dataset's{' '}
        <a href={hrefFor({ page: 'dataset', id: datasetId, tab: 'policy' })}>Toxicity policy tab</a>, then run the scan
        again.
      </p>
      <details>
        <summary className="small">Show the policy</summary>
        <pre className="policy-view">{policy.text}</pre>
      </details>
    </div>
  )
}
