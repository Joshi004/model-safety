import { formatCount } from './format'
import type { ChainDataset, DatasetStep, StageProgress, StageState, StepOutcome, StepResources } from './types'

// Plain-words helpers for the chain views. Nothing here touches the backend.

export const STATE_LABELS: Record<StageState, string> = {
  not_started: 'not started',
  unfinished: 'started, not finished',
  queued: 'queued',
  running: 'running',
  failed: 'failed',
  finished: 'finished',
}

// States of a stage that is under way: pages showing it keep refreshing.
export const ACTIVE_STATES: StageState[] = ['unfinished', 'queued', 'running']

export const STEP_OUTCOME_LABELS: Record<StepOutcome, string> = {
  queued: 'queued',
  running: 'running',
  succeeded: 'succeeded',
  failed: 'failed',
  cancelled: 'cancelled',
  unknown: 'unknown',
}

// "1,107 of 1,107 pieces", or null for a stage that has no count.
export function describeProgress(progress: StageProgress | null): string | null {
  if (progress === null) return null
  return `${formatCount(progress.done)} of ${formatCount(progress.total)} ${progress.unit}`
}

// "health · 4 CPUs · 256G · 1-00:00:00 · 8 GPUs": what a started step asked SLURM for. A
// step whose job scripts fix its resources may have recorded only some of them.
export function describeStepResources(resources: StepResources, gpus: number): string {
  const parts = [resources.partition]
  if (resources.cpus !== undefined) parts.push(`${resources.cpus} CPU${resources.cpus === 1 ? '' : 's'}`)
  if (resources.mem) parts.push(resources.mem)
  if (resources.time) parts.push(resources.time)
  if (gpus > 0) parts.push(`${gpus} GPU${gpus === 1 ? '' : 's'}`)
  return parts.join(' · ')
}

// "ner 82,455, regex_pii 14,143 +2 more": the biggest reasons first.
export function describeReasons(reasons: Record<string, number> | null, limit = 3): string {
  if (reasons === null) return '—'
  const sorted = Object.entries(reasons).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  if (sorted.length === 0) return 'none'
  const shown = sorted.slice(0, limit).map(([reason, count]) => `${reason} ${formatCount(count)}`)
  const more = sorted.length - shown.length
  return more > 0 ? `${shown.join(', ')} +${more} more` : shown.join(', ')
}

function mergeReasons(steps: DatasetStep[]): Record<string, number> | null {
  const merged: Record<string, number> = {}
  let found = false
  for (const step of steps) {
    if (step.removed_by_reason === null) continue
    found = true
    for (const [reason, count] of Object.entries(step.removed_by_reason)) {
      merged[reason] = (merged[reason] ?? 0) + count
    }
  }
  return found ? merged : null
}

// A dataset with the numbers the table shows worked out. A number is null until
// the step it comes from has finished for this dataset.
export interface DatasetSummary {
  dataset: ChainDataset
  // Rows kept by the first filter step.
  afterStep1: number | null
  // Rows kept by the last filter step: the dataset's clean rows.
  clean: number | null
  // Rows removed over all the steps (null until every step has finished).
  removed: number | null
  // The first step after which nothing was left, if any.
  emptyAfter: number | null
  reasons: Record<string, number> | null
}

export function summarizeDataset(dataset: ChainDataset): DatasetSummary {
  const { steps } = dataset
  const first = steps[0]
  const last = steps[steps.length - 1]
  const allFinished = steps.length > 0 && steps.every((step) => step.state === 'finished')
  const empty = steps.find((step) => step.state === 'finished' && step.kept === 0)
  return {
    dataset,
    afterStep1: first && first.state === 'finished' ? first.kept : null,
    clean: last && last.state === 'finished' ? last.kept : null,
    removed: allFinished ? steps.reduce((sum, step) => sum + (step.removed ?? 0), 0) : null,
    // Only a dataset that had rows to lose can be "emptied".
    emptyAfter: empty !== undefined && (dataset.prepared ?? 0) > 0 ? empty.step : null,
    reasons: mergeReasons(steps),
  }
}

export type DatasetSortKey = 'run' | 'prepared' | 'afterStep1' | 'clean' | 'removed'

// Sorts by one column. A number that isn't known yet always goes last.
export function sortDatasets(rows: DatasetSummary[], key: DatasetSortKey, descending: boolean): DatasetSummary[] {
  const value = (row: DatasetSummary): number | string | null =>
    key === 'run' ? row.dataset.run : key === 'prepared' ? row.dataset.prepared : row[key]
  return [...rows].sort((a, b) => {
    const left = value(a)
    const right = value(b)
    if (left === null && right === null) return 0
    if (left === null) return 1
    if (right === null) return -1
    const order = typeof left === 'string' ? left.localeCompare(String(right)) : left - Number(right)
    return descending ? -order : order
  })
}
