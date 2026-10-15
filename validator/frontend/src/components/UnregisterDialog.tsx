import { useState } from 'react'
import { errorMessage, unregisterDataset } from '../api'
import type { Dataset, UnregisterResponse } from '../types'
import Dialog from './Dialog'

// Says what unregistering removes (D2) and asks first. Everything shown is plain text.
export default function UnregisterDialog({
  dataset,
  serviceRoot,
  onClose,
  onUnregistered,
}: {
  dataset: Dataset
  serviceRoot: string
  onClose: () => void
  onUnregistered: (result: UnregisterResponse) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      onUnregistered(await unregisterDataset(dataset.id))
    } catch (failure) {
      setError(errorMessage(failure))
      setBusy(false)
    }
  }

  return (
    <Dialog title="Unregister dataset" onClose={onClose} busy={busy}>
      <p>
        Unregister <strong>{dataset.name}</strong> ({dataset.team})? This removes:
      </p>
      <ul>
        <li>its entry in the dataset list</li>
        <li>
          its own folder, <code>{`${serviceRoot}/datasets/${dataset.id}`}</code>, with any converted data and
          scan results inside it
        </li>
      </ul>
      <p>
        The original data at <code>{dataset.path}</code> is not touched.
        {dataset.parent !== null &&
          ` This is a filtered version, so the dataset it was made from keeps its converted data and its results.`}
      </p>

      {error !== null && (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      )}

      <div className="dialog-actions">
        <button type="button" className="secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="danger" onClick={() => void confirm()} disabled={busy}>
          {busy ? 'Unregistering…' : 'Unregister'}
        </button>
      </div>
    </Dialog>
  )
}
