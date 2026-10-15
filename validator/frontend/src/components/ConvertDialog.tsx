import { useEffect, useRef, useState } from 'react'
import { checkConversions, errorMessage, submitConversions } from '../api'
import { formatCount } from '../format'
import { hrefFor } from '../route'
import type { ConversionCheckResponse, ConversionPlanItem, ConversionSubmitResponse, Dataset } from '../types'
import { useConversionResources } from '../useConversionResources'
import CommandBox from './CommandBox'
import DeleteList from './DeleteList'
import Dialog from './Dialog'
import ResourcesFields from './ResourcesFields'

// "More -> Convert only": converts the ticked datasets with their saved settings, and
// nothing else. A dataset that isn't converted is converted; one that is converted is left
// alone unless the user chooses "Convert again", which deletes its converted data first. A
// dataset with no saved settings is sent to its Conversion tab. Anything that would be
// deleted is listed and asked about before the start.
//
// Everything shown is plain text.
export default function ConvertDialog({
  datasets,
  onClose,
  onStarted,
}: {
  datasets: Dataset[]
  onClose: () => void
  onStarted: (result: ConversionSubmitResponse) => void
}) {
  // The datasets the user chose "Convert again" for, by id.
  const [again, setAgain] = useState<Set<string>>(new Set())
  const [check, setCheck] = useState<ConversionCheckResponse | null>(null)
  const [checkError, setCheckError] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)
  const [confirming, setConfirming] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const resources = useConversionResources()

  const items = datasets.map((dataset) => ({ id: dataset.id, reconvert: again.has(dataset.id) }))
  const requestKey = JSON.stringify([items, resources.requestResources])

  // Checks again whenever a choice or a resource changes. The first check is immediate;
  // later ones wait a moment, so typing in a resource box doesn't ask on every key.
  const firstCheck = useRef(true)
  useEffect(() => {
    let cancelled = false
    const delay = firstCheck.current ? 0 : 400
    firstCheck.current = false
    const timer = window.setTimeout(() => {
      setChecking(true)
      checkConversions({ items, resources: resources.requestResources })
        .then((response) => {
          if (cancelled) return
          setCheck(response)
          setCheckError(null)
        })
        .catch((failure: unknown) => {
          if (!cancelled) setCheckError(errorMessage(failure))
        })
        .finally(() => {
          if (!cancelled) setChecking(false)
        })
    }, delay)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
    // `items` and the resources are new objects on every render; `requestKey` says when they changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey])

  function planOf(dataset: Dataset): ConversionPlanItem | null {
    return check?.items.find((item) => item.dataset_id === dataset.id) ?? null
  }

  function chooseAgain(datasetId: string, value: boolean) {
    setAgain((current) => {
      const next = new Set(current)
      if (value) next.add(datasetId)
      else next.delete(datasetId)
      return next
    })
  }

  const startable = check?.items.filter((item) => item.can_start) ?? []
  const deletes = startable.filter((item) => item.will_delete !== null || item.leftovers.length > 0)
  const canStart =
    startable.length > 0 &&
    !checking &&
    !starting &&
    check !== null &&
    check.resources_error === null &&
    check.slurm_error === null

  async function start() {
    setStarting(true)
    setError(null)
    try {
      onStarted(
        await submitConversions({
          items: startable.map((item) => ({ id: item.dataset_id, reconvert: item.reconvert })),
          resources: resources.requestResources,
        }),
      )
    } catch (failure) {
      setError(errorMessage(failure))
      setStarting(false)
      setConfirming(false)
    }
  }

  if (confirming) {
    return (
      <Dialog title="Delete and convert" onClose={() => setConfirming(false)} busy={starting}>
        <p>
          Starting {startable.length} {startable.length === 1 ? 'conversion' : 'conversions'} deletes:
        </p>
        <DeleteList
          items={deletes.map((item) => ({
            id: item.dataset_id,
            name: item.name,
            will_delete: item.will_delete,
            leftovers: item.leftovers,
          }))}
        />
        <p>The original data is not touched.</p>

        {error !== null && (
          <div className="banner banner-error" role="alert">
            {error}
          </div>
        )}

        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={() => setConfirming(false)} disabled={starting}>
            Back
          </button>
          <button type="button" className="danger" onClick={() => void start()} disabled={starting}>
            {starting ? 'Starting…' : 'Delete and convert'}
          </button>
        </div>
      </Dialog>
    )
  }

  return (
    <Dialog title="Convert only" onClose={onClose} busy={starting}>
      <p className="muted small">
        Each dataset is converted with its saved settings, as its own SLURM job. Nothing is scanned. A dataset that is
        already converted is left as it is, unless you choose "Convert again".
      </p>

      <ul className="convert-list">
        {datasets.map((dataset) => {
          const item = planOf(dataset)
          return (
            <li key={dataset.id} className="convert-item">
              <div className="convert-item-header">
                <strong>{dataset.name}</strong>
                <span className="muted small">{dataset.team}</span>
              </div>

              {dataset.prepared !== null && (
                <div className="convert-item-choices">
                  <label>
                    <input
                      type="radio"
                      name={`convert-${dataset.id}`}
                      checked={!again.has(dataset.id)}
                      onChange={() => chooseAgain(dataset.id, false)}
                    />
                    Use the existing converted data ({formatCount(dataset.prepared.rows)} rows)
                  </label>
                  <label>
                    <input
                      type="radio"
                      name={`convert-${dataset.id}`}
                      checked={again.has(dataset.id)}
                      onChange={() => chooseAgain(dataset.id, true)}
                    />
                    Convert again (deletes the converted data and its results first)
                  </label>
                </div>
              )}

              {item === null && <p className="muted small">Checking…</p>}
              {item !== null && item.status === 'ready' && (
                <p className="small">
                  {item.reconvert ? 'Will be converted again.' : 'Will be converted (a separate job).'}
                </p>
              )}
              {item !== null && item.status === 'converted' && <p className="muted small">Left as it is.</p>}
              {item !== null && item.status === 'no_settings' && (
                <p className="small">
                  Set up conversion first, then convert it.{' '}
                  <a href={hrefFor({ page: 'dataset', id: dataset.id, tab: 'conversion' })}>Open its Conversion tab</a>.
                  It is left out of this start.
                </p>
              )}
              {item !== null && !['ready', 'converted', 'no_settings'].includes(item.status) && (
                <p className="error small">{item.reason}. It is left out of this start.</p>
              )}
            </li>
          )
        })}
      </ul>

      {checkError !== null && (
        <div className="banner banner-error" role="alert">
          {checkError}
        </div>
      )}
      {check !== null && check.slurm_error !== null && (
        <div className="banner banner-error" role="alert">
          {check.slurm_error}
        </div>
      )}
      {check !== null && check.resources_error !== null && (
        <div className="banner banner-error" role="alert">
          {check.resources_error}
        </div>
      )}
      {error !== null && (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      )}

      <details>
        <summary className="small">Advanced: resources and the exact commands</summary>
        <ResourcesFields
          settings={resources.settings}
          settingsError={resources.error}
          onReloadSettings={resources.reload}
          resources={resources.inputs}
          onResourcesChange={resources.change}
        />
        {startable.length === 0 && <p className="muted small">The commands appear here for the datasets that will start.</p>}
        {startable.map(
          (item) =>
            item.sbatch_command !== null && (
              <CommandBox
                key={item.dataset_id}
                command={item.sbatch_command}
                title={item.name}
                note='"<version-id>" is replaced with a new folder name when the job starts.'
              />
            ),
        )}
      </details>

      <div className="dialog-actions">
        <button type="button" className="secondary" onClick={onClose} disabled={starting}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          disabled={!canStart}
          onClick={() => (deletes.length > 0 ? setConfirming(true) : void start())}
        >
          {starting
            ? 'Starting…'
            : checking
              ? 'Checking…'
              : `Convert ${startable.length} ${startable.length === 1 ? 'dataset' : 'datasets'}`}
        </button>
      </div>
    </Dialog>
  )
}
