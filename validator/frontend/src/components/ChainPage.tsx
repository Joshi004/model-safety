import { useState } from 'react'
import { getChain } from '../api'
import { ACTIVE_STATES, STATE_LABELS, STEP_OUTCOME_LABELS, describeProgress, describeReasons, sortDatasets, summarizeDataset } from '../chains'
import type { DatasetSortKey } from '../chains'
import { formatCount, formatPercent, formatTimestamp } from '../format'
import type { ChainResponse, ChainStage, FunnelStep, RemovedFilters } from '../types'
import { useRefreshing } from '../useRefreshing'
import StartStepPanel from './StartStepPanel'
import StateChip, { VerdictChip } from './StateChip'
import StepResults from './StepResults'

// One chain on one screen, drawn the way the data flows: the funnel of row
// counts, the stages with their states (click one for its results), the trace
// and row status, and a table of the chain's datasets. Reading never changes
// anything; "Run next step" opens the form that starts a step (StartStepPanel).
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

const POLL_MS = 5000

// The filter step that removed the most rows from a dataset, where its Removed page starts.
function mostRemovedStep(dataset: ChainResponse['datasets'][number]): number {
  return dataset.steps.reduce((best, step) => ((step.removed ?? 0) > (best.removed ?? 0) ? step : best), dataset.steps[0]).step
}

const STAGE_COLUMNS: { step: number | null; title: string }[] = [
  { step: 1, title: 'Step 1: duplicates' },
  { step: 2, title: 'Step 2: PII and toxicity' },
  { step: null, title: 'After step 2' },
]

function FunnelBox({ title, count, lines }: { title: string; count: number | null; lines: string[] }) {
  return (
    <div className="funnel-box">
      <div className="funnel-title muted small">{title}</div>
      <div className="funnel-count">{formatCount(count)}</div>
      {lines.map((line) => (
        <div key={line} className="funnel-line muted small">
          {line}
        </div>
      ))}
    </div>
  )
}

function funnelStepLines(step: FunnelStep): string[] {
  if (step.state !== 'finished') return [STATE_LABELS[step.state]]
  const lines = [`removed ${formatCount(step.removed)}`]
  if (step.removed_by_reason !== null) lines.push(describeReasons(step.removed_by_reason, 4))
  return lines
}

function Funnel({ chain }: { chain: ChainResponse }) {
  const { funnel } = chain
  return (
    <div className="chain-funnel">
      <FunnelBox title="Prepared" count={funnel.prepared} lines={[`${formatCount(funnel.prepared_runs)} runs`]} />
      {funnel.steps.map((step) => (
        <div key={step.step} className="funnel-step">
          <span className="funnel-arrow" aria-hidden="true">
            →
          </span>
          <FunnelBox title={`Kept by step ${step.step}`} count={step.kept} lines={funnelStepLines(step)} />
        </div>
      ))}
      <div className="funnel-step">
        <span className="funnel-arrow" aria-hidden="true">
          →
        </span>
        <FunnelBox
          title="Clean data"
          count={funnel.clean}
          lines={funnel.clean === null ? [] : [`${formatPercent(funnel.clean, funnel.prepared)} of Prepared`]}
        />
      </div>
    </div>
  )
}

// "Run next step": only the steps whose inputs are finished (and whose coverage report passed).
function RunNextStep({ chain, onPick }: { chain: ChainResponse; onPick: (stage: string) => void }) {
  if (!chain.startable) {
    return (
      <p className="muted small">
        This chain was recorded after it ran, so steps can't be started into it. Make a new chain to run steps from here.
      </p>
    )
  }
  const ready = chain.stages.filter((stage) => stage.ready)
  return (
    <div className="run-next">
      <label htmlFor="run-next-step" className="small">
        <strong>Run next step</strong>
      </label>{' '}
      <select
        id="run-next-step"
        value=""
        disabled={ready.length === 0}
        title={ready.length === 0 ? 'Nothing can be started now: each step waits for the ones before it, or is already under way.' : undefined}
        onChange={(event) => {
          if (event.target.value) onPick(event.target.value)
        }}
      >
        <option value="">{ready.length === 0 ? 'Nothing to start now' : 'Choose a step…'}</option>
        {ready.map((stage) => (
          <option key={stage.key} value={stage.key}>
            {stage.label}
          </option>
        ))}
      </select>
    </div>
  )
}

function StageBox({
  stage,
  selected,
  onSelect,
}: {
  stage: ChainStage
  selected: boolean
  onSelect: () => void
}) {
  const progress = describeProgress(stage.progress)
  return (
    <button
      type="button"
      className={selected ? 'stage-box selected' : 'stage-box'}
      onClick={onSelect}
      title={stage.state === 'not_started' && stage.blocked_by.length > 0 ? stage.blocked_by.join('\n') : stage.path ?? undefined}
    >
      <span className="stage-box-label">{stage.label}</span>
      <span className="stage-box-chips">
        <StateChip state={stage.state} />
        {stage.state === 'finished' && <VerdictChip verdict={stage.verdict} />}
      </span>
      {progress !== null && stage.state !== 'finished' && <span className="muted small">{progress}</span>}
      {stage.job !== undefined && stage.state !== 'finished' && (
        <span className="muted small">
          step {stage.job.record_id}: {STEP_OUTCOME_LABELS[stage.job.outcome]}
        </span>
      )}
      {(stage.runs_failed_count ?? 0) > 0 && (
        <span className="error small">
          {stage.runs_failed_count} run{stage.runs_failed_count === 1 ? '' : 's'} failed their checks
        </span>
      )}
    </button>
  )
}

function TraceAndStatus({ chain }: { chain: ChainResponse }) {
  const { trace, row_status: status } = chain
  const byStatus = trace?.totals?.by_status ?? null
  const notUnchanged = byStatus === null ? [] : Object.entries(byStatus).filter(([name, count]) => name !== 'unchanged' && count > 0)
  return (
    <div className="chain-extras small">
      <p>
        <strong>Trace:</strong>{' '}
        {trace === null ? (
          <span className="muted">this chain has no trace folder</span>
        ) : !trace.finished ? (
          <span className="muted">not finished</span>
        ) : (
          <>
            {formatCount(trace.totals?.findings ?? null)} findings
            {byStatus !== null &&
              (notUnchanged.length === 0
                ? ', all unchanged'
                : `, not all unchanged: ${notUnchanged.map(([name, count]) => `${name} ${formatCount(count)}`).join(', ')}`)}{' '}
            <VerdictChip verdict={trace.verdict} />
          </>
        )}
      </p>
      <p>
        <strong>Row status:</strong>{' '}
        {status === null ? (
          <span className="muted">this chain has no row status folder</span>
        ) : !status.finished ? (
          <span className="muted">not finished</span>
        ) : (
          <>
            built for {formatCount(status.totals?.prepared_rows ?? null)} rows <VerdictChip verdict={status.verdict} />
            {!status.has_removed_index && (
              <span className="error"> (no removed-rows index: build it again to browse removed rows)</span>
            )}
          </>
        )}
      </p>
    </div>
  )
}

function SortHeader({
  label,
  column,
  sort,
  onSort,
  number,
}: {
  label: string
  column: DatasetSortKey
  sort: { key: DatasetSortKey; descending: boolean }
  onSort: (column: DatasetSortKey) => void
  number?: boolean
}) {
  const active = sort.key === column
  return (
    <th className={number ? 'number' : undefined} aria-sort={active ? (sort.descending ? 'descending' : 'ascending') : 'none'}>
      <button type="button" className="link-button" onClick={() => onSort(column)}>
        {label}
        {active ? (sort.descending ? ' ▼' : ' ▲') : ''}
      </button>
    </th>
  )
}

function Datasets({
  chain,
  onOpenRun,
  onOpenRemoved,
}: {
  chain: ChainResponse
  onOpenRun: (runDir: string) => void
  onOpenRemoved: (filters: RemovedFilters) => void
}) {
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<{ key: DatasetSortKey; descending: boolean }>({ key: 'run', descending: false })

  function handleSort(column: DatasetSortKey) {
    setSort((current) =>
      current.key === column ? { key: column, descending: !current.descending } : { key: column, descending: column !== 'run' },
    )
  }

  const needle = search.trim().toLowerCase()
  const summaries = chain.datasets.map(summarizeDataset).filter((row) => row.dataset.run.toLowerCase().includes(needle))
  const rows = sortDatasets(summaries, sort.key, sort.descending)
  const outputRoot = (step: number) => chain.steps.find((item) => item.number === step)?.output_root
  // The Removed page needs the row status and its index of removed rows.
  const canBrowseRemoved = chain.row_status?.finished === true && chain.row_status.has_removed_index

  return (
    <div className="chain-datasets">
      <div className="runs-list-header">
        <h3>Datasets ({formatCount(chain.datasets.length)})</h3>
        <input
          type="search"
          placeholder="Search datasets"
          aria-label="Search datasets"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>
      {chain.datasets.length === 0 && <p className="muted">The Prepared folder holds no runs yet.</p>}
      {chain.datasets.length > 0 && (
        <div className="skipped-table-wrap">
          <table className="skipped-table datasets-table">
            <thead>
              <tr>
                <SortHeader label="Dataset" column="run" sort={sort} onSort={handleSort} />
                <SortHeader label="Prepared" column="prepared" sort={sort} onSort={handleSort} number />
                <SortHeader label="Kept by step 1" column="afterStep1" sort={sort} onSort={handleSort} number />
                <SortHeader label="Clean" column="clean" sort={sort} onSort={handleSort} number />
                <SortHeader label="Removed" column="removed" sort={sort} onSort={handleSort} number />
                <th>Removed by</th>
                <th>Open run</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ dataset, afterStep1, clean, removed, emptyAfter, reasons }) => (
                <tr key={dataset.run} className="no-click">
                  <td className="mono location-text">{dataset.run}</td>
                  <td className="number">{formatCount(dataset.prepared)}</td>
                  <td className="number">{formatCount(afterStep1)}</td>
                  <td className="number">
                    {formatCount(clean)}
                    {emptyAfter !== null && <div className="muted small">no rows left after step {emptyAfter}</div>}
                  </td>
                  <td className="number">{formatCount(removed)}</td>
                  <td className="small">{describeReasons(reasons)}</td>
                  <td className="job-actions-inline">
                    <button type="button" className="link-button" onClick={() => onOpenRun(dataset.run_dir)}>
                      Prepared
                    </button>
                    {canBrowseRemoved && (removed ?? 0) > 0 && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => onOpenRemoved({ chainDir: chain.chain_dir, run: dataset.run, step: mostRemovedStep(dataset) })}
                      >
                        Removed rows
                      </button>
                    )}
                    {dataset.steps.map((step) => {
                      const root = outputRoot(step.step)
                      if (root === undefined || step.state === 'not_started') return null
                      return (
                        <button
                          key={step.step}
                          type="button"
                          className="link-button"
                          onClick={() => onOpenRun(`${root}/${dataset.run}`)}
                        >
                          {step.step === chain.steps.length ? 'Clean' : `Step ${step.step}`}
                        </button>
                      )
                    })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {chain.datasets.length > 0 && rows.length === 0 && <p className="muted">No dataset matches "{search}".</p>}
    </div>
  )
}

export default function ChainPage({
  chainDir,
  onBack,
  onOpenRun,
  onOpenRemoved,
  onOpenJobs,
}: {
  chainDir: string
  onBack: () => void
  onOpenRun: (runDir: string) => void
  onOpenRemoved: (filters: RemovedFilters) => void
  onOpenJobs: () => void
}) {
  const { data, error, reload } = useRefreshing(
    () => getChain(chainDir),
    chainDir,
    POLL_MS,
    (chain) => chain.stages.some((stage) => ACTIVE_STATES.includes(stage.state)),
  )
  const [selectedStage, setSelectedStage] = useState<string | null>(null)
  // The step the start form is open for.
  const [startStage, setStartStage] = useState<string | null>(null)

  return (
    <div className="chain-page">
      <div className="runs-list-header">
        <button type="button" className="secondary" onClick={onBack}>
          ← All chains
        </button>
        <button type="button" className="secondary" onClick={reload}>
          Refresh
        </button>
      </div>

      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}

      {data && (
        <>
          <h2>{data.name}</h2>
          <p className="muted small">
            <span className="mono location-text">{data.chain_dir}</span>
            {data.created_at && <> · created {formatTimestamp(data.created_at)}</>}
            {' · '}Prepared runs in <span className="mono location-text">{data.prepared_root}</span>
          </p>
          {data.problems.length > 0 && (
            <div className="banner banner-error small">
              This chain doesn't fit together:
              <ul className="lookup-differences">
                {data.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          )}

          <Funnel chain={data} />

          <RunNextStep chain={data} onPick={setStartStage} />

          <div className="stage-columns">
            {STAGE_COLUMNS.map((column) => (
              <div key={column.title} className="stage-column">
                <h4>{column.title}</h4>
                {data.stages
                  .filter((stage) => stage.step === column.step)
                  .map((stage) => (
                    <StageBox
                      key={stage.key}
                      stage={stage}
                      selected={selectedStage === stage.key}
                      onSelect={() => setSelectedStage((current) => (current === stage.key ? null : stage.key))}
                    />
                  ))}
              </div>
            ))}
          </div>

          {startStage !== null && (
            <StartStepPanel
              chainDir={data.chain_dir}
              stageKey={startStage}
              onStarted={reload}
              onClose={() => setStartStage(null)}
              onOpenJobs={onOpenJobs}
            />
          )}

          {selectedStage !== null && (
            // Reloads when the stage's state changes, so a finishing job shows its results.
            <StepResults
              key={`${selectedStage}|${data.stages.find((stage) => stage.key === selectedStage)?.state}`}
              chainDir={data.chain_dir}
              stage={selectedStage}
            />
          )}

          <TraceAndStatus chain={data} />
          <Datasets chain={data} onOpenRun={onOpenRun} onOpenRemoved={onOpenRemoved} />
        </>
      )}
    </div>
  )
}
