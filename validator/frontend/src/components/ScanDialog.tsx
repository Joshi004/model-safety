import { useEffect, useId, useRef, useState } from 'react'
import { checkScans, errorMessage, submitScans } from '../api'
import { formatCount } from '../format'
import { hrefFor } from '../route'
import {
  choiceLabel,
  defaultSettings,
  dropNote,
  isToggle,
  modeLabel,
  modeNote,
  scannerLabel,
  visibleFields,
} from '../scans'
import type {
  Dataset,
  ScanCheckResponse,
  ScanSubmitResponse,
  ScanUnitDataset,
  ScannerField,
} from '../types'
import { useScanResources } from '../useScanResources'
import CommandBox from './CommandBox'
import DeleteList from './DeleteList'
import Dialog from './Dialog'
import ResourcesFields from './ResourcesFields'

// What the check says about one dataset: whether it is converted, and what the scan does about it.
function PlanNote({ plan }: { plan: ScanUnitDataset }) {
  if (plan.action === 'convert') {
    return <p className="small">Will be converted first (a separate job).</p>
  }
  if (plan.action === 'convert_again') {
    const old = plan.will_delete
    const size = old !== null && old.rows !== null ? ` (${formatCount(old.rows)} rows)` : ''
    return (
      <p className="small">
        Will be converted again first (a separate job).{' '}
        {old !== null && old.kept
          ? 'Its current converted data stays on disk, because a filtered dataset was made from it.'
          : `Its current converted data${size} and the results of every scan that read it will be deleted.`}
      </p>
    )
  }
  if (plan.action === 'use_existing') return null
  if (plan.status === 'no_settings') {
    return (
      <p className="small">
        Set up conversion first: this dataset has no saved conversion settings.{' '}
        <a href={hrefFor({ page: 'dataset', id: plan.id, tab: 'conversion' })}>Open its Conversion tab</a>, or leave it
        out of this scan.
      </p>
    )
  }
  return <p className="error small">{plan.reason}</p>
}

// One chosen dataset: where it stands (converted or not), and, for one that is converted, the
// choice between using that data and converting again.
function DatasetItem({
  dataset,
  plan,
  again,
  canLeaveOut,
  onAgain,
  onLeaveOut,
}: {
  dataset: Dataset
  // Null until the first check has answered.
  plan: ScanUnitDataset | null
  again: boolean
  canLeaveOut: boolean
  onAgain: (value: boolean) => void
  onLeaveOut: () => void
}) {
  // The check knows best; until it has answered, the dataset as the list shows it will do.
  const prepared = plan !== null ? plan.prepared : dataset.prepared
  const name = `scan-${dataset.id}`

  return (
    <li className="convert-item">
      <div className="convert-item-header">
        <strong>{dataset.name}</strong>
        <span className="muted small">{dataset.team}</span>
        {canLeaveOut && (
          <button type="button" className="link-button push-right" onClick={onLeaveOut}>
            Leave out
          </button>
        )}
      </div>

      {prepared !== null && (
        <div className="convert-item-choices">
          <label>
            <input type="radio" name={name} checked={!again} onChange={() => onAgain(false)} />
            Use the existing converted data ({formatCount(prepared.rows)} rows)
          </label>
          <label>
            <input type="radio" name={name} checked={again} onChange={() => onAgain(true)} />
            Convert again (deletes the converted data and its results first)
          </label>
        </div>
      )}

      {plan === null ? <p className="muted small">Checking…</p> : <PlanNote plan={plan} />}
    </li>
  )
}

// One setting of the scanner: a checkbox when it is on or off, otherwise a choice of radio buttons.
function SettingControl({
  scannerKey,
  field,
  value,
  onChange,
}: {
  scannerKey: string
  field: ScannerField
  value: string
  onChange: (value: string) => void
}) {
  const id = useId()

  if (isToggle(field)) {
    return (
      <div className="scan-setting">
        <label className="scan-choice">
          <input type="checkbox" checked={value === '1'} onChange={(event) => onChange(event.target.checked ? '1' : '0')} />
          {field.label}
        </label>
        <p className="muted small">{field.help}</p>
      </div>
    )
  }
  return (
    <fieldset className="scan-setting">
      <legend>{field.label}</legend>
      {field.choices.map((choice) => (
        <label key={choice} className="scan-choice">
          <input type="radio" name={`${id}-${field.name}`} checked={value === choice} onChange={() => onChange(choice)} />
          {choiceLabel(scannerKey, field.name, choice)}
        </label>
      ))}
      <p className="muted small">{field.help}</p>
    </fieldset>
  )
}

// "Find duplicates" and the other scans: the dialog every scan is started from. It is built
// from what GET /api/scanners says about the scanner, and checks (POST /api/scans/check)
// whenever a choice changes, so what it shows (what each dataset needs first, the cost, the
// exact commands, what stops the start) is what the start will do. Start is only possible
// when the check of the choices as they are now has passed, and anything that will be deleted
// is asked about first.
//
// Everything shown is plain text.
export default function ScanDialog({
  scannerKey,
  datasets,
  onClose,
  onStarted,
}: {
  scannerKey: string
  datasets: Dataset[]
  onClose: () => void
  onStarted: (result: ScanSubmitResponse) => void
}) {
  const resources = useScanResources(scannerKey)
  const scanner = resources.scanner

  // The datasets still in the scan, by id. A dataset that can't be scanned can be left out.
  const [chosen, setChosen] = useState<string[]>(() => datasets.map((dataset) => dataset.id))
  // The datasets the user chose "Convert again" for, by id.
  const [again, setAgain] = useState<Set<string>>(new Set())
  // Null until the user picks: the scanner's own default applies.
  const [modeChoice, setModeChoice] = useState<string | null>(null)
  // Only the settings the user changed; the others are the scanner's defaults.
  const [overrides, setOverrides] = useState<Record<string, string>>({})
  const [confirmGpu, setConfirmGpu] = useState(false)

  // The newest answer of the check, and the choices (`key`) it was for.
  const [checked, setChecked] = useState<{ key: string; response: ScanCheckResponse } | null>(null)
  const [checkError, setCheckError] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const rows = chosen.flatMap((id) => {
    const dataset = datasets.find((item) => item.id === id)
    return dataset === undefined ? [] : [dataset]
  })

  const mode = scanner !== null && scanner.modes !== null ? (modeChoice ?? scanner.default_mode) : null
  const settings: Record<string, string> = scanner === null ? {} : { ...defaultSettings(scanner), ...overrides }
  const items = rows.map((dataset) => ({ id: dataset.id, reconvert: again.has(dataset.id) }))
  // A scan of one dataset is always that dataset on its own, whatever the mode says.
  const effectiveMode = mode !== null && rows.length <= 1 ? 'within' : mode
  const modeText = modeNote(scannerKey, effectiveMode, rows.length)
  const dropText = dropNote(scannerKey)
  const requestKey = JSON.stringify(
    scanner === null || rows.length === 0 ? null : [items, mode, settings, resources.requestResources],
  )

  // Checks again whenever a choice or a resource changes. The first check is immediate;
  // later ones wait a moment, so typing in a resource box doesn't ask on every key.
  const firstCheck = useRef(true)
  useEffect(() => {
    if (scanner === null || rows.length === 0) return
    let cancelled = false
    const delay = firstCheck.current ? 0 : 400
    firstCheck.current = false
    const timer = window.setTimeout(() => {
      setChecking(true)
      checkScans({
        scanner: scannerKey,
        datasets: items,
        mode,
        settings,
        resources: resources.requestResources,
        confirm_gpu: false,
      })
        .then((response) => {
          if (cancelled) return
          setChecked({ key: requestKey, response })
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
    // `items`, `settings` and the resources are new objects on every render; `requestKey` says when they changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey])

  const check = checked?.response ?? null
  // Whether `check` is the answer for the choices as they are now.
  const upToDate = checked !== null && checked.key === requestKey && !checking
  const units = check?.units ?? []
  const planRows = units.flatMap((unit) => unit.datasets)
  const converting = planRows.filter((row) => row.action === 'convert' || row.action === 'convert_again')
  const deletes = converting.filter((row) => row.will_delete !== null || row.leftovers.length > 0)
  const gpuUnit = units.find((unit) => unit.needs_gpu_confirmation)

  // A dataset's own reason is shown under it, so the same words aren't repeated as a banner.
  const shownUnderDatasets = new Set(planRows.flatMap((row) => (row.reason === null ? [] : [`'${row.name}': ${row.reason}`])))
  const blocked =
    check === null
      ? []
      : [
          ...new Set([
            ...check.blocked,
            ...units.flatMap((unit) => unit.blocked).filter((line) => !shownUnderDatasets.has(line)),
          ]),
        ]
  const warnings = [...new Set(units.flatMap((unit) => unit.warnings))]
  const costs = [...new Set(units.flatMap((unit) => (unit.cost === null ? [] : [unit.cost.summary])))]

  const canStart =
    scanner !== null &&
    rows.length > 0 &&
    check !== null &&
    check.can_start &&
    upToDate &&
    (gpuUnit === undefined || confirmGpu) &&
    !starting

  function chooseAgain(datasetId: string, value: boolean) {
    setAgain((current) => {
      const next = new Set(current)
      if (value) next.add(datasetId)
      else next.delete(datasetId)
      return next
    })
  }

  function leaveOut(datasetId: string) {
    setChosen((current) => current.filter((id) => id !== datasetId))
    chooseAgain(datasetId, false)
  }

  async function start() {
    setStarting(true)
    setError(null)
    try {
      onStarted(
        await submitScans({
          scanner: scannerKey,
          datasets: items,
          mode,
          settings,
          resources: resources.requestResources,
          confirm_gpu: confirmGpu,
        }),
      )
    } catch (failure) {
      setError(errorMessage(failure))
      setStarting(false)
      setConfirming(false)
    }
  }

  const title = `Scan for ${(scanner?.label ?? scannerLabel(scannerKey)).toLowerCase()}`

  function startLabel(): string {
    if (starting) return 'Starting…'
    if (rows.length === 0) return 'Nothing to scan'
    // A check that failed isn't going to finish: the error is shown above.
    if (!upToDate) return checkError !== null && !checking ? 'Start' : 'Checking…'
    return `Start ${units.length} ${units.length === 1 ? 'scan' : 'scans'}`
  }

  // Each view has its own key, so the browser's dialog is made again when the view changes:
  // Escape closes the dialog itself, and a dialog that was closed that way isn't opened again.
  if (confirming) {
    return (
      <Dialog key="confirm" title="Delete, convert and scan" onClose={() => setConfirming(false)} busy={starting}>
        <p>Starting this scan deletes:</p>
        <DeleteList
          items={deletes.map((row) => ({
            id: row.id,
            name: row.name,
            will_delete: row.will_delete,
            leftovers: row.leftovers,
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
            {starting ? 'Starting…' : 'Delete, convert and scan'}
          </button>
        </div>
      </Dialog>
    )
  }

  return (
    <Dialog key="main" title={title} onClose={onClose} busy={starting} wide>
      {resources.response === null && resources.error === null && <p className="muted">Loading…</p>}
      {resources.response === null && resources.error !== null && (
        <div className="banner banner-error" role="alert">
          <span>Couldn't load the scanners: {resources.error}</span>
          <button type="button" className="secondary" onClick={resources.reload}>
            Try again
          </button>
        </div>
      )}
      {resources.response !== null && scanner === null && (
        <div className="banner banner-error" role="alert">
          The backend doesn't know a scanner called "{scannerKey}".
        </div>
      )}

      {scanner !== null && (
        <>
          <p className="muted small">{scanner.description}</p>
          {scanner.stages.length > 1 && <p className="muted small">Stages: {scanner.stages.join(' → ')}.</p>}

          <h3 className="scan-heading">Datasets</h3>
          {rows.length === 0 ? (
            <p className="muted">Every dataset was left out, so there is nothing to scan. Close this and tick some.</p>
          ) : (
            <ul className="convert-list">
              {rows.map((dataset) => (
                <DatasetItem
                  key={dataset.id}
                  dataset={dataset}
                  plan={planRows.find((row) => row.id === dataset.id) ?? null}
                  again={again.has(dataset.id)}
                  canLeaveOut={rows.length > 1}
                  onAgain={(value) => chooseAgain(dataset.id, value)}
                  onLeaveOut={() => leaveOut(dataset.id)}
                />
              ))}
            </ul>
          )}

          {scanner.modes !== null && rows.length > 1 && (
            <fieldset className="scan-setting">
              <legend>Compare</legend>
              {Object.entries(scanner.modes).map(([key, description]) => (
                <label key={key} className="scan-choice">
                  <input type="radio" name="scan-mode" checked={mode === key} onChange={() => setModeChoice(key)} />
                  {modeLabel(scannerKey, key, description)}
                </label>
              ))}
            </fieldset>
          )}

          {visibleFields(scanner).map((field) => (
            <SettingControl
              key={field.name}
              scannerKey={scannerKey}
              field={field}
              value={settings[field.name] ?? field.default}
              onChange={(value) => setOverrides((current) => ({ ...current, [field.name]: value }))}
            />
          ))}

          {modeText !== null && <p className="small">{modeText}</p>}
          {dropText !== null && <p className="small">{dropText}</p>}

          <details>
            <summary className="small">Advanced: resources and the exact commands</summary>
            <ResourcesFields
              settings={resources.response}
              settingsError={resources.error}
              onReloadSettings={resources.reload}
              resources={resources.inputs}
              onResourcesChange={resources.change}
              jobWord="Scanning"
              usesGpu={scanner.gpu}
            />
            <p className="muted small">
              These are the resources of the scan. A conversion that has to run first uses the default conversion
              resources.
            </p>
            {converting.length === 0 && units.every((unit) => unit.command_text === null) && (
              <p className="muted small">The commands appear here once the check has passed.</p>
            )}
            {converting.map(
              (row) =>
                row.conversion_sbatch_command !== null && (
                  <CommandBox
                    key={row.id}
                    command={row.conversion_sbatch_command}
                    title={`Conversion: ${row.name}`}
                    note='"<version-id>" is replaced with a new folder name when the job starts.'
                  />
                ),
            )}
            {units.map(
              (unit, index) =>
                unit.command_text !== null && (
                  <CommandBox
                    key={unit.datasets.map((row) => row.id).join('/')}
                    command={unit.command_text}
                    title={units.length === 1 ? 'Scan' : `Scan ${index + 1} of ${units.length}: ${unit.datasets.map((row) => row.name).join(', ')}`}
                    note={'"<scan-id>" and "<version-id>" are replaced when the jobs start.'}
                  />
                ),
            )}
          </details>

          {check !== null && units.length > 0 && (
            <div className="scan-summary">
              <p className="small">
                Starts {converting.length} {converting.length === 1 ? 'conversion' : 'conversions'} and {units.length}{' '}
                {units.length === 1 ? 'scan' : 'scans'}. Watch them on the Activity page.
              </p>
              {costs.map((cost) => (
                <p key={cost} className="small">
                  {units.length > 1 ? `Each scan: ${cost}` : cost}
                </p>
              ))}
            </div>
          )}

          {warnings.map((warning) => (
            <div key={warning} className="banner banner-warning small">
              {warning}
            </div>
          ))}
          {blocked.map((line) => (
            <div key={line} className="banner banner-error small" role="alert">
              {line}
            </div>
          ))}
          {gpuUnit !== undefined && (
            <label className="scan-choice scan-gpu">
              <input type="checkbox" checked={confirmGpu} onChange={(event) => setConfirmGpu(event.target.checked)} />
              This scan uses GPUs ({gpuUnit.cost?.summary}). I want to start it.
            </label>
          )}
        </>
      )}

      {checkError !== null && (
        <div className="banner banner-error" role="alert">
          {checkError}
        </div>
      )}
      {error !== null && (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      )}

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
          {startLabel()}
        </button>
      </div>
    </Dialog>
  )
}
