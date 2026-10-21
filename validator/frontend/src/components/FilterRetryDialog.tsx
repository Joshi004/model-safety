import { useEffect, useState } from 'react'
import { checkFilterRetry, errorMessage, retryFilter } from '../api'
import { describeResources } from '../conversions'
import { reasonLabel } from '../flagged'
import { formatCount, formatTimestamp } from '../format'
import type { FilterRetryCheckResponse, FilterRetryResponse } from '../types'
import CommandBox from './CommandBox'
import Dialog from './Dialog'

interface Outcome {
  checked: FilterRetryCheckResponse | null
  error: string | null
}

// What the server says running a failed or cancelled drop again would do, and the question whether
// to do it. It asks the server when it opens, so what it shows is what a retry would do now. Nothing
// is started until "Retry drop" is pressed. The retry drops exactly the rows the first attempt chose
// (the drop list it already wrote), into a new folder of the same filtered dataset; the failed
// attempt's folder and logs stay.
//
// Safety: every value shown here comes from the backend or from files on disk and renders as plain
// React text, never as markup.
export default function FilterRetryDialog({
  datasetId,
  onClose,
  onStarted,
}: {
  datasetId: string
  onClose: () => void
  onStarted: (response: FilterRetryResponse) => void
}) {
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    checkFilterRetry(datasetId)
      .then((checked) => {
        if (!cancelled) setOutcome({ checked, error: null })
      })
      .catch((caught: unknown) => {
        if (!cancelled) setOutcome({ checked: null, error: errorMessage(caught) })
      })
    return () => {
      cancelled = true
    }
  }, [datasetId])

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      onStarted(await retryFilter(datasetId))
    } catch (failure) {
      setError(errorMessage(failure))
      setBusy(false)
    }
  }

  const checked = outcome?.checked ?? null

  return (
    <Dialog title="Retry drop" onClose={onClose} busy={busy} wide>
      {outcome === null && <p className="muted">Checking…</p>}
      {outcome?.error != null && (
        <div className="banner banner-error" role="alert">
          {outcome.error}
        </div>
      )}

      {checked !== null && (
        <>
          <p>
            Run the drop that makes <strong>{checked.dataset_name}</strong> again. It drops the same{' '}
            <strong>{formatCount(checked.rows)}</strong> {checked.rows === 1 ? 'row' : 'rows'} as before: the rows are
            not chosen again.
          </p>

          <table className="history-table">
            <thead>
              <tr>
                <th>Flagged by</th>
                <th>Rows</th>
                <th>Reasons</th>
              </tr>
            </thead>
            <tbody>
              {checked.counts.by_scanner.map((item) => (
                <tr key={item.scanner}>
                  <td>{item.label}</td>
                  <td>{formatCount(item.rows)}</td>
                  <td className="small">
                    {item.by_reason.map((entry) => `${reasonLabel(entry.reason)} ${formatCount(entry.count)}`).join(', ')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <dl className="facts">
            <dt>Last attempt</dt>
            <dd>
              {checked.previous.job_id === null ? 'No SLURM job' : `SLURM job ${checked.previous.job_id}`}
              {checked.previous.submitted_at !== null && `, started ${formatTimestamp(checked.previous.submitted_at)}`}
              {checked.previous.reason !== null && `: ${checked.previous.reason}`}
            </dd>
            <dt>What stays</dt>
            <dd>
              The failed attempt's folder and logs are kept, so you can still read why it failed. The new attempt (number{' '}
              {checked.attempt}) writes to a new folder of the same dataset. The dataset these rows come from isn't
              changed.
            </dd>
            {checked.resources !== null && (
              <>
                <dt>Resources</dt>
                <dd>{describeResources(checked.resources)}</dd>
              </>
            )}
          </dl>

          {checked.warnings.map((warning) => (
            <div key={warning} className="banner banner-warning small">
              {warning}
            </div>
          ))}
          {checked.blocked.map((problem) => (
            <div key={problem} className="banner banner-error" role="alert">
              {problem}
            </div>
          ))}

          {checked.command_text !== null && (
            <details>
              <summary className="small">Advanced: the exact command</summary>
              <CommandBox
                command={checked.command_text}
                title="Submitted with"
                note='Exactly what runs, with "<version-id>" standing for the new folder name made when you press Retry drop.'
              />
            </details>
          )}
        </>
      )}

      {error !== null && (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      )}

      <div className="dialog-actions">
        <button type="button" className="secondary" onClick={onClose} disabled={busy}>
          {checked === null ? 'Close' : 'Cancel'}
        </button>
        {checked !== null && (
          <button type="button" onClick={() => void confirm()} disabled={busy || !checked.can_start}>
            {busy ? 'Starting…' : 'Retry drop'}
          </button>
        )}
      </div>
    </Dialog>
  )
}
