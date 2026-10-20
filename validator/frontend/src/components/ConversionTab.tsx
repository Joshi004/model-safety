import { useEffect, useMemo, useState } from 'react'
import { checkConversions, errorMessage, listConversions, submitConversions, updateDataset } from '../api'
import { describeRowStatus } from '../describe'
import { applySuggestion, emptyMapping, fromRequestFlags, toRequestFlags } from '../mapping'
import type { MappingState } from '../mapping'
import { choiceFromConversion, conversionOf, defaultChoice, sameConversion } from '../source'
import type { SourceChoice } from '../source'
import type { ConversionPlanItem, Dataset, InspectGroup } from '../types'
import { useConversionResources } from '../useConversionResources'
import { useInspect, usePreview } from '../usePreview'
import CommandBox from './CommandBox'
import ConversionJob from './ConversionJob'
import Dialog from './Dialog'
import FormatPicker from './FormatPicker'
import InputFields from './InputFields'
import OutputPreview from './OutputPreview'
import RecordView from './RecordView'
import PartitionPicker from './PartitionPicker'
import ResourcesFields from './ResourcesFields'
import RowNavigator from './RowNavigator'

// A couple of real rows, not a large sample: the data in one file is assumed to look alike.
const SAMPLE_ROWS = 2

type Busy = 'saving' | 'checking' | 'starting' | null

// Everything about converting one dataset: which format and file to read, how its fields
// map to what the scanners read, a preview of the converted rows, the settings saved on the
// dataset, and the button that starts the conversion. A conversion always uses the saved
// settings, so Convert saves first.
export default function ConversionTab({
  dataset,
  onDatasetChanged,
  onReload,
}: {
  dataset: Dataset
  // The dataset after its settings were saved.
  onDatasetChanged: (dataset: Dataset) => void
  // Read the dataset again (a conversion started).
  onReload: () => void
}) {
  const inspect = useInspect(dataset.available ? dataset.path : null)

  // What the user picked. Null until they do: then the saved settings, or the first
  // format the readers understand, are used.
  const [picked, setPicked] = useState<SourceChoice | null>(() =>
    dataset.conversion !== null ? choiceFromConversion(dataset.conversion) : null,
  )
  const [mapping, setMapping] = useState<MappingState>(() =>
    dataset.conversion !== null ? fromRequestFlags(dataset.conversion) : emptyMapping(),
  )
  const [startRow, setStartRow] = useState(0)
  const [selectedRowIndex, setSelectedRowIndex] = useState(0)
  const [viewMode, setViewMode] = useState<'readable' | 'fields'>('readable')

  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // The result of the last check: the commands, shown under "Advanced".
  const [plan, setPlan] = useState<ConversionPlanItem | null>(null)
  const [planError, setPlanError] = useState<string | null>(null)
  // A check that found something to delete, waiting for the user's confirmation.
  const [confirming, setConfirming] = useState<ConversionPlanItem | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)

  const resources = useConversionResources()

  // The newest conversion of the dataset, if there is one, is shown below the buttons.
  useEffect(() => {
    let cancelled = false
    listConversions(dataset.id)
      .then((response) => {
        if (!cancelled) setJobId((current) => current ?? response.conversions[0]?.job_id ?? null)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [dataset.id])

  // A folder of csv or json files needs the user to pick one file, so nothing is
  // previewed for it until they do.
  const automatic = useMemo(() => {
    const choice = inspect.data === null ? null : defaultChoice(inspect.data)
    return choice !== null && choice.file === null ? choice : null
  }, [inspect.data])
  const choice = picked ?? automatic

  const flags = toRequestFlags(mapping)
  const preview = usePreview(
    choice === null ? null : { path: dataset.path, ...choice, max_rows: SAMPLE_ROWS, start_row: startRow, ...flags },
  )

  const current = choice === null ? null : conversionOf(choice, flags)
  const dirty = current !== null && (dataset.conversion === null || !sameConversion(current, dataset.conversion))
  const converted = dataset.prepared !== null

  function handleFormatSelect(group: InspectGroup, file: string | null) {
    if (group.input_type === null) return
    // The picker announces its starting selection when it appears; that is not a change.
    if (choice !== null && choice.input_type === group.input_type && (choice.file ?? null) === file) return
    setPicked({
      input_type: group.input_type,
      file,
      split: choice?.split ?? 'train',
      hf_config: choice?.hf_config ?? '',
    })
    setMapping(emptyMapping())
    setStartRow(0)
    setSelectedRowIndex(0)
  }

  function changeHuggingface(change: Partial<Pick<SourceChoice, 'split' | 'hf_config'>>) {
    if (choice === null) return
    setPicked({ ...choice, ...change })
    setStartRow(0)
    setSelectedRowIndex(0)
  }

  function jump(newStartRow: number) {
    setStartRow(newStartRow)
    setSelectedRowIndex(0)
  }

  async function saveSettings(): Promise<boolean> {
    if (current === null) return false
    setBusy('saving')
    try {
      onDatasetChanged(await updateDataset(dataset.id, { conversion: current }))
      return true
    } catch (failure) {
      setError(errorMessage(failure))
      return false
    } finally {
      setBusy(null)
    }
  }

  async function handleSave() {
    setError(null)
    setNotice(null)
    if (await saveSettings()) setNotice('Settings saved.')
  }

  // Saves the settings, asks the backend what starting would do, and starts when nothing
  // is deleted; otherwise asks first.
  async function handleConvert() {
    setError(null)
    setNotice(null)
    setPlanError(null)
    if (current === null) return
    if (dirty && !(await saveSettings())) return

    setBusy('checking')
    try {
      const result = await checkConversions({
        items: [{ id: dataset.id, reconvert: converted }],
        resources: resources.requestResources,
      })
      const item = result.items[0]
      setPlan(item)
      if (result.resources_error !== null) {
        setPlanError(result.resources_error)
        return
      }
      if (!item.can_start) {
        setError(item.reason ?? "This dataset can't be converted now.")
        return
      }
      if (item.will_delete !== null || item.leftovers.length > 0) {
        setConfirming(item)
        return
      }
      await start(item)
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy((now) => (now === 'checking' ? null : now))
    }
  }

  async function start(item: ConversionPlanItem) {
    setBusy('starting')
    setError(null)
    try {
      const result = await submitConversions({
        items: [{ id: dataset.id, reconvert: item.reconvert }],
        resources: resources.requestResources,
      })
      const started = result.started[0]
      if (started !== undefined) {
        setJobId(started.job_id)
        setNotice('The conversion was started.')
      }
      if (result.errors.length > 0) setError(result.errors.join(' '))
      setConfirming(null)
      onReload()
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(null)
    }
  }

  // A const, so what is known about it still holds inside the callbacks below.
  const sample = preview.data
  const rows = sample?.input_rows ?? []
  const suggestion = sample?.suggested_mapping.map ?? {}
  const suggestedEntries = Object.entries(suggestion)
  const selectedRow = rows[selectedRowIndex]

  return (
    <>
      {!dataset.available && (
        <div className="banner banner-error" role="alert">
          The original data can't be read, so nothing can be previewed or converted. {dataset.unavailable_reason}
        </div>
      )}

      {dataset.available && (
        <div className="card">
          <h2>1. What to read</h2>
          {inspect.loading && <p className="muted">Looking at the data…</p>}
          {inspect.error !== null && <p className="error">{inspect.error}</p>}
          {inspect.data !== null && (
            <FormatPicker
              groups={inspect.data.groups}
              isSingleFile={inspect.data.kind === 'file'}
              initial={dataset.conversion !== null ? choiceFromConversion(dataset.conversion) : null}
              onSelect={handleFormatSelect}
            />
          )}
          {choice !== null && choice.input_type === 'huggingface' && (
            <div className="start-resources">
              <div className="start-field">
                <label htmlFor="conversion-split">Split</label>
                <input
                  id="conversion-split"
                  type="text"
                  value={choice.split}
                  onChange={(event) => changeHuggingface({ split: event.target.value })}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
              <div className="start-field">
                <label htmlFor="conversion-hf-config">Config (optional)</label>
                <input
                  id="conversion-hf-config"
                  type="text"
                  value={choice.hf_config}
                  onChange={(event) => changeHuggingface({ hf_config: event.target.value })}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {preview.error !== null && <p className="error">{preview.error}</p>}

      {sample !== null && (
        <div className="card">
          <h2>2. How the fields map</h2>
          <div className={preview.loading ? 'preview-area loading' : 'preview-area'}>
            <div className="preview-toolbar">
              <div className="toolbar-left">
                <RowNavigator
                  sourceInfo={sample.source_info}
                  startRow={startRow}
                  sampleSize={rows.length}
                  onJump={jump}
                />
                <div className="row-tabs">
                  {rows.map((_, index) => (
                    <button
                      key={index}
                      type="button"
                      className={index === selectedRowIndex ? 'row-tab selected' : 'row-tab'}
                      onClick={() => setSelectedRowIndex(index)}
                    >
                      Row {startRow + index + 1}: {describeRowStatus(sample.preview, index)}
                    </button>
                  ))}
                </div>
                <div className="row-tabs">
                  <button
                    type="button"
                    className={viewMode === 'readable' ? 'row-tab selected' : 'row-tab'}
                    onClick={() => setViewMode('readable')}
                  >
                    Readable
                  </button>
                  <button
                    type="button"
                    className={viewMode === 'fields' ? 'row-tab selected' : 'row-tab'}
                    onClick={() => setViewMode('fields')}
                  >
                    Fields &amp; output
                  </button>
                </div>
              </div>
              {suggestedEntries.length > 0 && (
                <div className="suggestion-bar small">
                  <span className="muted">Suggested:</span>{' '}
                  {suggestedEntries.map(([target, source]) => `${target} ← ${source}`).join(', ')}
                  <button type="button" className="secondary" onClick={() => setMapping(applySuggestion(mapping, suggestion))}>
                    Apply
                  </button>
                </div>
              )}
            </div>

            {selectedRow === undefined ? (
              <p className="muted">No rows in this sample.</p>
            ) : viewMode === 'readable' ? (
              <RecordView
                record={selectedRow}
                written={sample.preview.rows.find((row) => row.row_index === selectedRowIndex)}
              />
            ) : (
              <div className="columns">
                <div className="column">
                  <h3>Your input (row {selectedRowIndex + 1})</h3>
                  <InputFields
                    inputRows={rows}
                    selectedRowIndex={selectedRowIndex}
                    mapping={mapping}
                    hints={sample.suggested_mapping.hints}
                    onMappingChange={setMapping}
                  />
                </div>
                <div className="column wide sticky-column">
                  <h3>Output (row {selectedRowIndex + 1})</h3>
                  <OutputPreview result={sample} mapping={mapping} selectedRowIndex={selectedRowIndex} />
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="card">
        <h2>3. Convert</h2>
        <p className="small">
          {dataset.conversion === null ? (
            <span className="muted">No conversion settings are saved for this dataset yet.</span>
          ) : dirty ? (
            <span className="muted">You have changes that aren't saved yet.</span>
          ) : (
            <span className="muted">These settings are saved.</span>
          )}{' '}
          A conversion always uses the saved settings, so Convert saves them first.
        </p>

        {sample !== null && sample.preview.fatal_if_run && (
          <div className="banner banner-warning small">
            <strong>This mapping would stop the whole run, not just skip a row.</strong> {sample.preview.fatal_note}
          </div>
        )}
        {converted && (
          <div className="banner banner-warning small">
            This dataset is already converted. "Convert again" deletes the current converted data and its results,
            then converts it again.
          </div>
        )}

        <PartitionPicker
          partitions={resources.settings?.partitions ?? []}
          selected={resources.inputs.partition}
          onSelect={(partition) => resources.change({ ...resources.inputs, partition })}
          error={resources.error ?? resources.settings?.slurm_error ?? null}
          onReload={resources.reload}
        />

        <div className="start-actions">
          <button
            type="button"
            className="secondary"
            onClick={() => void handleSave()}
            disabled={!dirty || busy !== null}
          >
            {busy === 'saving' ? 'Saving…' : 'Save settings'}
          </button>
          <button
            type="button"
            className="primary"
            onClick={() => void handleConvert()}
            disabled={current === null || busy !== null || !dataset.available}
          >
            {busy === 'checking' || busy === 'starting' ? 'Working…' : converted ? 'Convert again…' : 'Convert'}
          </button>
        </div>

        {notice !== null && (
          <div className="banner banner-ok" role="status">
            {notice}
          </div>
        )}
        {error !== null && (
          <div className="banner banner-error" role="alert">
            {error}
          </div>
        )}

        <details>
          <summary className="small">Advanced: CPUs, memory, time and the exact commands</summary>
          <ResourcesFields
            settings={resources.settings}
            resources={resources.inputs}
            onResourcesChange={resources.change}
          />
          {planError !== null && (
            <div className="banner banner-error small" role="alert">
              {planError}
            </div>
          )}
          {plan !== null && plan.sbatch_command !== null ? (
            <CommandBox
              command={plan.sbatch_command}
              title="Submitted with"
              note='"<version-id>" is replaced with a new folder name when the job starts. The job then keeps the exact command it ran.'
            />
          ) : (
            <p className="muted small">The exact commands appear here once you press Convert.</p>
          )}
          {plan !== null && plan.command !== null && (
            <CommandBox command={plan.command} title="Conversion command" note="What the job runs, from the repository root." />
          )}
        </details>
      </div>

      {jobId !== null && <ConversionJob datasetId={dataset.id} jobId={jobId} onChanged={onReload} />}

      {confirming !== null && (
        <Dialog
          title={confirming.reconvert ? 'Convert again' : 'Convert'}
          onClose={() => setConfirming(null)}
          busy={busy === 'starting'}
        >
          <p>
            Starting this conversion of <strong>{dataset.name}</strong> deletes:
          </p>
          <ul>
            {confirming.will_delete !== null && !confirming.will_delete.kept && (
              <li>
                the current converted data (<code>{confirming.will_delete.folder}</code>
                {confirming.will_delete.rows !== null && `, ${confirming.will_delete.rows.toLocaleString()} rows`}) and
                the results of every scan that read it
              </li>
            )}
            {confirming.will_delete !== null && confirming.will_delete.kept && (
              <li>
                nothing of the current converted data: a filtered dataset was made from it, so it stays on disk. This
                dataset shows as not converted until the new conversion finishes
              </li>
            )}
            {confirming.leftovers.map((folder) => (
              <li key={folder}>
                the folder of a failed or cancelled attempt: <code>{folder}</code>
              </li>
            ))}
          </ul>
          <p>
            The original data at <code>{dataset.path}</code> is not touched.
          </p>

          {error !== null && (
            <div className="banner banner-error" role="alert">
              {error}
            </div>
          )}

          <div className="dialog-actions">
            <button type="button" className="secondary" onClick={() => setConfirming(null)} disabled={busy === 'starting'}>
              Cancel
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => void start(confirming)}
              disabled={busy === 'starting'}
            >
              {busy === 'starting' ? 'Starting…' : 'Delete and convert'}
            </button>
          </div>
        </Dialog>
      )}
    </>
  )
}
