// What a run's progress.json (see types.ts's RunProgress) means in words and
// numbers. Shared by the progress panel in the Prepared run view and the
// batch page in the Jobs view, so both show the same figures. No React here.

import { formatBytes } from './format'
import type { ProgressPhase, RunProgress } from './types'

// The steps a run goes through, in order (progress_file.py's PHASES).
export const STEPS: { phase: ProgressPhase; words: string }[] = [
  { phase: 'finding_files', words: 'Finding the original files' },
  { phase: 'fingerprinting_files', words: 'Fingerprinting the original files' },
  { phase: 'reading', words: 'Reading the original rows' },
  { phase: 'fingerprinting_rows', words: 'Fingerprinting each row' },
  { phase: 'preparing', words: 'Preparing the rows (mapping, required columns)' },
  { phase: 'checking_text', words: 'Checking the rows have scannable text' },
  { phase: 'writing', words: 'Writing the Prepared files' },
  { phase: 'finishing', words: 'Running the final checks and writing summary.json' },
]

export function formatAmount(value: number, unit: RunProgress['unit']): string {
  if (unit === 'bytes') return formatBytes(value)
  return value.toLocaleString()
}

export function unitSuffix(unit: RunProgress['unit']): string {
  return unit === 'rows' || unit === 'files' ? ` ${unit}` : ''
}

export function formatSpeed(rate: number, unit: RunProgress['unit']): string {
  if (unit === 'bytes') return `${formatBytes(rate)}/s`
  if (unit === 'files') return `${rate.toFixed(1)} files/s`
  return `${Math.round(rate).toLocaleString()} rows/s`
}

// "about 3 min left in this step". Never a time for the whole run: only the
// current step has a total.
export function describeTimeLeft(secondsLeft: number): string {
  return `about ${Math.max(1, Math.round(secondsLeft / 60))} min left in this step`
}

// Seconds between the start of the current step and the last update, which
// is the stretch of time `done` was counted over.
function stepSeconds(progress: RunProgress): number {
  const start = new Date(progress.phase_started_at).getTime()
  const end = new Date(progress.updated_at).getTime()
  return Number.isNaN(start) || Number.isNaN(end) ? 0 : (end - start) / 1000
}

export interface StepMeasure {
  // Index into STEPS, or -1 for `done` and `failed`.
  stepIndex: number
  // null when the step has no total (CSV and JSON reading).
  percent: number | null
  // Units per second over the current step; null until there is one.
  rate: number | null
  // null when there's no total or no rate yet.
  secondsLeft: number | null
}

export function measureStep(progress: RunProgress): StepMeasure {
  const { done, total } = progress
  const percent = total === null ? null : total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 100
  const seconds = stepSeconds(progress)
  const rate = seconds > 0 && done > 0 ? done / seconds : null
  const secondsLeft = rate !== null && total !== null && total > done ? (total - done) / rate : null
  return {
    stepIndex: STEPS.findIndex((step) => step.phase === progress.phase),
    percent,
    rate,
    secondsLeft,
  }
}
