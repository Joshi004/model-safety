import { useEffect, useState } from 'react'
import { checkDrop, errorMessage, submitDrop } from '../api'
import { MAX_EXPLICIT_ROWS, buildDropRequest, describeSelection, explicitCount, isEmpty } from '../drop'
import type { DropSelection } from '../drop'
import { reasonLabel } from '../flagged'
import { formatCount } from '../format'
import type { Dataset, DropCheckResponse, DropSubmitResponse } from '../types'
import CommandBox from './CommandBox'
import Dialog from './Dialog'

// What the server says dropping the chosen rows would do, and the question whether to do it.
// It asks the server to count when it opens, so the numbers are what a drop would do now, not
// what the page last showed. Nothing is dropped until "Drop" is pressed: then a new dataset is
// made from this one's converted data without those rows, and this dataset isn't changed.
//
// Safety: every value shown here comes from the backend or from files on disk and renders as
// plain React text, never as markup.

interface Outcome {
  checked: DropCheckResponse | null
  error: string | null
}

function DropDialog({
  dataset,
  versionId,
  selection,
  onClose,
  onDropped,
}: {
  dataset: Dataset
  versionId: string
  selection: DropSelection
  onClose: () => void
  onDropped: (response: DropSubmitResponse) => void
}) {
  // What is asked about stays the same while the dialog is open: it is modal.
  const [request] = useState(() => buildDropRequest(versionId, selection))
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    checkDrop(dataset.id, request)
      .then((checked) => {
        if (!cancelled) setOutcome({ checked, error: null })
      })
      .catch((caught: unknown) => {
        if (!cancelled) setOutcome({ checked: null, error: errorMessage(caught) })
      })
    return () => {
      cancelled = true
    }
  }, [dataset.id, request])

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      onDropped(await submitDrop(dataset.id, request))
    } catch (failure) {
      setError(errorMessage(failure))
      setBusy(false)
    }
  }

  const checked = outcome?.checked ?? null
  const rows = checked?.counts.rows ?? 0
  const rowsIn = checked?.rows_in_dataset ?? null

  return (
    <Dialog title="Drop rows" onClose={onClose} busy={busy} wide>
      {outcome === null && <p className="muted">Counting the rows…</p>}
      {outcome?.error != null && (
        <div className="banner banner-error" role="alert">
          {outcome.error}
        </div>
      )}

      {checked !== null && (
        <>
          <p>
            Drop <strong>{formatCount(rows)}</strong> {rows === 1 ? 'row' : 'rows'} from{' '}
            <strong>{checked.dataset_name}</strong>
            {rowsIn !== null && (
              <>
                {' '}
                ({formatCount(rowsIn)} rows), leaving <strong>{formatCount(Math.max(0, rowsIn - rows))}</strong>
              </>
            )}
            ?
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
          {checked.counts.overlap > 0 && (
            <p className="muted small">
              {formatCount(checked.counts.overlap)} {checked.counts.overlap === 1 ? 'row was' : 'rows were'} flagged by
              more than one scan, and {checked.counts.overlap === 1 ? 'is' : 'are'} dropped once. A row can also have
              more than one reason, so the reasons can add up to more than the rows.
            </p>
          )}

          <dl className="facts">
            <dt>New dataset</dt>
            <dd>
              <strong>{checked.new_name}</strong>, in {dataset.team}
            </dd>
            <dt>What stays</dt>
            <dd>
              {dataset.name}, its converted rows and its results don't change. The new dataset is a copy of the converted
              rows that are left, byte for byte, and keeps a link back to the original rows. {dataset.name}'s converted
              data is kept for it (converting {dataset.name} again sets that data aside instead of deleting it).
            </dd>
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

          <details>
            <summary className="small">Advanced</summary>
            {checked.resources !== null && (
              <p className="small">
                One CPU job on the <code>{checked.resources.partition}</code> partition: {checked.resources.cpus} CPUs,{' '}
                {checked.resources.mem} memory, {checked.resources.time} time limit. Progress is on the Activity page.
              </p>
            )}
            {checked.command_text !== null && (
              <CommandBox
                command={checked.command_text}
                title="Submitted with"
                note="Exactly what runs, with <new-id> and <version-id> standing for the ids made when you press Drop."
              />
            )}
          </details>
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
          <button type="button" onClick={() => void confirm()} disabled={busy || !checked.can_start || rows === 0}>
            {busy ? 'Dropping…' : `Drop ${formatCount(rows)} ${rows === 1 ? 'row' : 'rows'}`}
          </button>
        )}
      </div>
    </Dialog>
  )
}

// The bar under the dataset's pages that says which rows are chosen, across its result pages,
// and opens the dialog above. It shows nothing while nothing is chosen.
export function DropBar({
  dataset,
  selection,
  onClear,
  onDropped,
}: {
  dataset: Dataset
  selection: DropSelection
  onClear: () => void
  onDropped: (response: DropSubmitResponse) => void
}) {
  const [reviewing, setReviewing] = useState(false)
  if (isEmpty(selection) || dataset.prepared === null) return null

  const tooMany = explicitCount(selection) > MAX_EXPLICIT_ROWS
  return (
    <>
      <div className="drop-bar" role="region" aria-label="Rows chosen to drop">
        <p>
          <strong>Chosen to drop:</strong> {describeSelection(selection)}.
        </p>
        {tooMany && (
          <p className="error small">
            More than {formatCount(MAX_EXPLICIT_ROWS)} rows are ticked one by one. Choose "all matching rows" instead, or
            untick some.
          </p>
        )}
        <div className="drop-bar-actions">
          <button type="button" className="secondary" onClick={onClear}>
            Clear all
          </button>
          <button type="button" onClick={() => setReviewing(true)} disabled={tooMany}>
            Review and drop…
          </button>
        </div>
      </div>
      {reviewing && (
        <DropDialog
          dataset={dataset}
          versionId={dataset.prepared.version_id}
          selection={selection}
          onClose={() => setReviewing(false)}
          onDropped={(response) => {
            setReviewing(false)
            onDropped(response)
          }}
        />
      )}
    </>
  )
}
