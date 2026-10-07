import { listBatches } from '../api'
import { formatTimestamp } from '../format'
import { describeOutcomes, describeResources } from '../jobs'
import { useRefreshing } from '../useRefreshing'
import BatchPage from './BatchPage'

// The "Jobs" view: the batches started from the Start panel (newest first),
// and one batch's page once it's opened. Reading a batch never changes it;
// only Cancel on a batch's page does anything.
//
// Safety: every value shown here comes from batch records on disk or from
// SLURM and renders as plain React text, never as markup.

const POLL_MS = 5000
const LABELS_SHOWN = 3

function BatchesList({ onOpen }: { onOpen: (batchId: string) => void }) {
  const { data, error, reload } = useRefreshing(
    () => listBatches(),
    'batches',
    POLL_MS,
    (response) => response.batches.some((batch) => batch.has_active),
  )

  return (
    <div className="runs-list">
      <div className="runs-list-header">
        <h3>Batches</h3>
        <button type="button" className="secondary" onClick={reload}>
          Refresh
        </button>
      </div>

      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}

      {data && (
        <>
          <p className="muted small">
            Runs root: <span className="mono location-text">{data.root}</span>
          </p>
          {data.slurm_error && (
            <div className="banner banner-error small">
              {data.slurm_error} The state of the jobs can't be read right now.
            </div>
          )}
          {data.problems.length > 0 && (
            <div className="banner banner-warning small">
              Some batch records couldn't be read:
              <ul className="lookup-differences">
                {data.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          )}
          {data.batches.length === 0 && (
            <p className="muted">
              No batches yet. Start one from "Start conversion" under a dataset's preview in "Inspect a
              dataset", or select several datasets in its sidebar and start them from "Review selection".
            </p>
          )}
          {data.batches.length > 0 && (
            <div className="skipped-table-wrap">
              <table className="skipped-table">
                <thead>
                  <tr>
                    <th>Batch</th>
                    <th>Submitted</th>
                    <th>Datasets</th>
                    <th>Resources</th>
                    <th>Jobs</th>
                  </tr>
                </thead>
                <tbody>
                  {data.batches.map((batch) => {
                    const labels = batch.labels.map((label) => label ?? '(no name)')
                    return (
                      <tr key={batch.batch_id} onClick={() => onOpen(batch.batch_id)}>
                        <td>
                          {/* A click on the button reaches the row's own onClick. */}
                          <button type="button" className="link-button mono">
                            {batch.batch_id}
                          </button>
                        </td>
                        <td>{batch.created_at ? formatTimestamp(batch.created_at) : '—'}</td>
                        <td title={labels.join('\n')}>
                          {labels.slice(0, LABELS_SHOWN).join(', ')}
                          {labels.length > LABELS_SHOWN && ` +${labels.length - LABELS_SHOWN} more`}
                        </td>
                        <td className="small">{describeResources(batch.resources)}</td>
                        <td>
                          {batch.job_count}: {describeOutcomes(batch.by_outcome)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small">Refreshes every {POLL_MS / 1000} seconds while any job is queued or running.</p>
        </>
      )}
    </div>
  )
}

export default function JobsView({
  batchId,
  onSelectBatch,
  onOpenResults,
}: {
  batchId: string | null
  onSelectBatch: (batchId: string | null) => void
  onOpenResults: (runDir: string) => void
}) {
  if (batchId !== null) {
    return (
      <BatchPage key={batchId} batchId={batchId} onBack={() => onSelectBatch(null)} onOpenResults={onOpenResults} />
    )
  }
  return <BatchesList onOpen={onSelectBatch} />
}
