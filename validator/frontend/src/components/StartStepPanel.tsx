import { useEffect, useRef, useState } from 'react'
import { ApiError, checkStep, getStepSpec, submitStep } from '../api'
import { formatCount } from '../format'
import type { StepField, StepPlan, StepRecordView, StepSpecResponse, StepStageSpec } from '../types'
import { useRefreshing } from '../useRefreshing'
import CommandBox from './CommandBox'

// The form that starts one step of a chain: what it reads and skips, its settings and
// resources, what it costs (with its own confirmation for GPUs), the exact command, and
// what stops it. The check runs while you type, and nothing starts until you press Start.
// The backend checks everything again before it starts anything.
//
// Safety: every value shown here comes from the backend's check or from files on disk and
// renders as plain React text, never as markup.

const CHECK_DELAY_MS = 400

interface ResourceBoxes {
  partition: string
  cpus: string
  mem: string
  time: string
}

// A setting as the form holds it: text for a number or a list, a list of names for a
// multiple choice, the choice itself otherwise.
type FieldValue = string | string[]

function initialValue(field: StepField): FieldValue {
  if (field.type === 'multi') return field.default
  if (field.type === 'list') return field.default.join(', ')
  if (field.type === 'integer') return field.default === null ? '' : String(field.default)
  return field.default
}

function initialResources(spec: StepSpecResponse, stage: StepStageSpec): ResourceBoxes {
  const partition = spec.partitions.includes(spec.default_partition) ? spec.default_partition : (spec.partitions[0] ?? spec.default_partition)
  return {
    partition,
    cpus: stage.resources ? String(stage.resources.cpus) : '',
    mem: stage.resources?.mem ?? '',
    time: stage.resources?.time ?? '',
  }
}

// What goes to the backend: a number that isn't one goes out as it is, and the backend says why not.
function toSettings(fields: StepField[], values: Record<string, FieldValue>): Record<string, unknown> {
  return Object.fromEntries(
    fields.map((field) => {
      const value = values[field.name]
      if (field.type === 'integer') {
        const text = String(value).trim()
        return [field.name, text === '' ? null : /^\d+$/.test(text) ? Number(text) : text]
      }
      return [field.name, value]
    }),
  )
}

function toResources(stage: StepStageSpec, boxes: ResourceBoxes): Record<string, unknown> {
  if (stage.mode === 'inline') return {}
  if (stage.resources === null) return { partition: boxes.partition }
  const cpus = boxes.cpus.trim()
  return {
    partition: boxes.partition,
    cpus: /^\d+$/.test(cpus) ? Number(cpus) : cpus,
    mem: boxes.mem.trim(),
    time: boxes.time.trim(),
  }
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: StepField
  value: FieldValue
  onChange: (value: FieldValue) => void
}) {
  const id = `start-${field.name}`
  if (field.type === 'choice') {
    return (
      <select id={id} value={String(value)} onChange={(event) => onChange(event.target.value)}>
        {field.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    )
  }
  if (field.type === 'multi') {
    const chosen = Array.isArray(value) ? value : []
    return (
      <div className="start-checks" id={id}>
        {field.choices.map((choice) => (
          <label key={choice} className="inline small">
            <input
              type="checkbox"
              checked={chosen.includes(choice)}
              onChange={(event) =>
                onChange(event.target.checked ? [...chosen, choice] : chosen.filter((item) => item !== choice))
              }
            />
            {choice}
          </label>
        ))}
      </div>
    )
  }
  return (
    <input
      id={id}
      type="text"
      value={String(value)}
      inputMode={field.type === 'integer' ? 'numeric' : undefined}
      autoComplete="off"
      spellCheck={false}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

function PlanDetails({ plan }: { plan: StepPlan }) {
  return (
    <div className="start-plan">
      {plan.input_summaries.map((summary) => (
        <p key={summary.root} className="small">
          <strong>Reads</strong> {formatCount(summary.files)} file{summary.files === 1 ? '' : 's'} ({formatCount(summary.rows)} rows) from{' '}
          {formatCount(summary.runs_read)} finished run{summary.runs_read === 1 ? '' : 's'} under{' '}
          <span className="mono location-text">{summary.root}</span>.
          {summary.runs_skipped > 0 && <> It skips {formatCount(summary.runs_skipped)} run(s) that aren't finished.</>}
          {summary.skipped.length > 0 && (
            <details>
              <summary>What it skips</summary>
              <ul className="lookup-differences">
                {summary.skipped.map((item) => (
                  <li key={`${item.kind}|${item.path}`}>
                    {item.kind} {item.path}: {item.reason}
                  </li>
                ))}
                {summary.skipped_more > 0 && <li>and {formatCount(summary.skipped_more)} more</li>}
              </ul>
            </details>
          )}
        </p>
      ))}
      <p className="small">
        <strong>Writes</strong> <span className="mono location-text">{plan.output}</span>{' '}
        <span className="muted">
          ({plan.output_state === 'new' ? 'not there yet' : plan.output_state === 'empty' ? 'an empty folder' : 'something is already there'})
        </span>
      </p>
      {plan.dependency.length > 0 && (
        <p className="small">
          <strong>Waits for</strong> SLURM job {plan.dependency.join(', ')} to finish well.
        </p>
      )}
    </div>
  )
}

function StartForm({
  chainDir,
  stageKey,
  spec,
  onStarted,
  onClose,
  onOpenJobs,
}: {
  chainDir: string
  stageKey: string
  spec: StepSpecResponse
  onStarted: () => void
  onClose: () => void
  onOpenJobs: () => void
}) {
  const stage = spec.stages[stageKey]
  const [values, setValues] = useState<Record<string, FieldValue>>(() =>
    Object.fromEntries(stage.fields.map((field) => [field.name, initialValue(field)])),
  )
  const [boxes, setBoxes] = useState<ResourceBoxes>(() => initialResources(spec, stage))
  const [plan, setPlan] = useState<StepPlan | null>(null)
  const [checkError, setCheckError] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)
  const [confirmedCost, setConfirmedCost] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [started, setStarted] = useState<StepRecordView | null>(null)
  const requestId = useRef(0)

  // The check, a moment after the last change, so typing doesn't ask the backend for every key.
  const formKey = JSON.stringify([values, boxes])
  useEffect(() => {
    const thisRequest = ++requestId.current
    setChecking(true)
    const timer = window.setTimeout(() => {
      checkStep({
        chain_dir: chainDir,
        stage: stageKey,
        settings: toSettings(stage.fields, values),
        resources: toResources(stage, boxes),
      })
        .then((response) => {
          if (requestId.current !== thisRequest) return
          setPlan(response)
          setCheckError(null)
        })
        .catch((caught: unknown) => {
          if (requestId.current !== thisRequest) return
          setPlan(null)
          setCheckError(caught instanceof ApiError ? caught.message : String(caught))
        })
        .finally(() => {
          if (requestId.current === thisRequest) setChecking(false)
        })
    }, CHECK_DELAY_MS)
    return () => window.clearTimeout(timer)
    // The request is exactly what formKey names.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chainDir, stageKey, formKey])

  // The GPU confirmation is for the cost shown when it was ticked: a change of settings that changes it asks again.
  const confirmed = plan?.cost !== null && plan?.cost !== undefined && plan.cost.summary === confirmedCost
  const needsGpu = plan?.needs_gpu_confirmation === true
  const canStart = plan?.can_start === true && !checking && !submitting && (!needsGpu || confirmed)
  const inline = stage.mode === 'inline'

  async function handleStart() {
    if (plan === null) return
    setSubmitting(true)
    setSubmitError(null)
    try {
      const record = await submitStep({
        chain_dir: chainDir,
        stage: stageKey,
        settings: toSettings(stage.fields, values),
        resources: toResources(stage, boxes),
        confirm_gpu: confirmed,
      })
      setStarted(record)
      onStarted()
    } catch (caught) {
      setSubmitError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }

  if (started !== null) {
    return (
      <div className="start-panel start-step">
        <h3>{stage.label}</h3>
        <div className={started.outcome === 'failed' ? 'banner banner-error' : 'banner banner-ok'}>
          {inline ? (
            <>
              Ran: <strong>{started.outcome}</strong>
              {started.stage_verdict && <> (verdict: {started.stage_verdict})</>}.
            </>
          ) : (
            <>
              Started. SLURM job{started.jobs.length === 1 ? '' : 's'}: {started.jobs.map((job) => job.job_id ?? '?').join(', ') || '(none read)'}.
            </>
          )}
        </div>
        {started.warnings.map((warning) => (
          <div key={warning} className="banner banner-warning small">
            {warning}
          </div>
        ))}
        <div className="start-actions">
          <button type="button" className="secondary" onClick={onOpenJobs}>
            Follow it in Jobs
          </button>
          <button type="button" className="secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="start-panel start-step">
      <div className="runs-list-header">
        <h3>Start: {stage.label}</h3>
        <button type="button" className="secondary" onClick={onClose}>
          Close
        </button>
      </div>

      {stage.fields.length > 0 && (
        <div className="start-resources">
          {stage.fields.map((field) => (
            <div key={field.name} className="start-field">
              <label htmlFor={`start-${field.name}`}>{field.label}</label>
              <FieldInput
                field={field}
                value={values[field.name]}
                onChange={(value) => setValues((current) => ({ ...current, [field.name]: value }))}
              />
              {field.help && <div className="muted small">{field.help}</div>}
            </div>
          ))}
        </div>
      )}

      {stage.mode !== 'inline' && (
        <div className="start-resources">
          <div className="start-field">
            <label htmlFor="start-partition">Partition</label>
            <select id="start-partition" value={boxes.partition} onChange={(event) => setBoxes({ ...boxes, partition: event.target.value })}>
              {(spec.partitions.includes(boxes.partition) ? spec.partitions : [boxes.partition, ...spec.partitions]).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          {stage.resources !== null && (
            <>
              <div className="start-field">
                <label htmlFor="start-cpus">CPUs (1–{spec.max_cpus})</label>
                <input id="start-cpus" type="text" inputMode="numeric" value={boxes.cpus} autoComplete="off" onChange={(event) => setBoxes({ ...boxes, cpus: event.target.value })} />
              </div>
              <div className="start-field">
                <label htmlFor="start-mem">Memory</label>
                <input id="start-mem" type="text" value={boxes.mem} autoComplete="off" spellCheck={false} onChange={(event) => setBoxes({ ...boxes, mem: event.target.value })} />
              </div>
              <div className="start-field">
                <label htmlFor="start-time">Time limit</label>
                <input id="start-time" type="text" value={boxes.time} autoComplete="off" spellCheck={false} onChange={(event) => setBoxes({ ...boxes, time: event.target.value })} />
              </div>
            </>
          )}
        </div>
      )}
      {stage.mode !== 'inline' && stage.resources === null && (
        <p className="muted small">The CPUs, memory and time of this step are fixed by its job scripts; the cost below says what they are.</p>
      )}
      {spec.slurm_error && <div className="banner banner-error small">{spec.slurm_error}</div>}

      {checkError && <p className="error">{checkError}</p>}
      {checking && <p className="muted small">Checking…</p>}

      {plan && (
        <div className={checking ? 'preview-area loading' : undefined}>
          <PlanDetails plan={plan} />

          {plan.cost && (
            <div className={plan.cost.gpus > 0 ? 'banner banner-warning' : 'start-cost small'}>
              <strong>Cost:</strong> {plan.cost.summary}
              {plan.needs_gpu_confirmation && (
                <label className="inline start-gpu-confirm">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) => setConfirmedCost(event.target.checked ? (plan.cost?.summary ?? null) : null)}
                  />
                  I know this uses GPUs, and I want to start it.
                </label>
              )}
            </div>
          )}

          {plan.blocked.length > 0 && (
            <div className="banner banner-error small">
              It can't start yet:
              <ul className="lookup-differences">
                {plan.blocked.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          )}
          {plan.warnings.map((warning) => (
            <div key={warning} className="banner banner-warning small">
              {warning}
            </div>
          ))}

          {plan.command_text && (
            <CommandBox
              command={plan.command_text}
              title="The command"
              note={
                inline
                  ? 'Start runs exactly this inside the validator and waits for it (up to 5 minutes).'
                  : 'Start runs exactly this, from the repo folder: it only submits jobs to SLURM and returns.'
              }
            />
          )}
        </div>
      )}

      {submitError && <p className="error job-error">{submitError}</p>}
      <div className="start-actions">
        <button type="button" disabled={!canStart} onClick={() => void handleStart()}>
          {submitting ? (inline ? 'Running… this can take a few minutes' : 'Starting…') : inline ? 'Run now' : 'Start'}
        </button>
      </div>
    </div>
  )
}

export default function StartStepPanel({
  chainDir,
  stageKey,
  onStarted,
  onClose,
  onOpenJobs,
}: {
  chainDir: string
  stageKey: string
  onStarted: () => void
  onClose: () => void
  onOpenJobs: () => void
}) {
  const { data, error } = useRefreshing(() => getStepSpec(), 'step-spec', 0, () => false)
  if (error) return <p className="error">{error}</p>
  if (data === null) return <p className="muted">Loading…</p>
  if (!(stageKey in data.stages)) return <p className="error">There is no step called {stageKey}.</p>
  // A new step starts the form again, from that step's defaults.
  return <StartForm key={`${chainDir}|${stageKey}`} chainDir={chainDir} stageKey={stageKey} spec={data} onStarted={onStarted} onClose={onClose} onOpenJobs={onOpenJobs} />
}
