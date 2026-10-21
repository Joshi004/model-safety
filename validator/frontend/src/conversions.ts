// Words for a conversion job's outcome and resources. No React here.
import type { ChipTone } from './components/Chip'
import type { JobOutcome, JobResources } from './types'

// Outcomes of a job that is still going.
export const ACTIVE_OUTCOMES: JobOutcome[] = ['queued', 'running', 'stalled']

export const OUTCOME_LABELS: Record<JobOutcome, string> = {
  queued: 'Queued',
  running: 'Running',
  stalled: 'Stalled?',
  succeeded: 'Succeeded',
  checks_failed: 'Checks failed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  skipped: 'Skipped',
  not_submitted: 'Not submitted',
  unknown: 'Unknown',
}

export const OUTCOME_TONES: Record<JobOutcome, ChipTone> = {
  queued: 'neutral',
  running: 'neutral',
  stalled: 'warn',
  succeeded: 'ok',
  checks_failed: 'error',
  failed: 'error',
  cancelled: 'warn',
  skipped: 'neutral',
  not_submitted: 'error',
  unknown: 'warn',
}

export function isActive(outcome: JobOutcome): boolean {
  return ACTIVE_OUTCOMES.includes(outcome)
}

// A conversion that ended in one of these gave no converted data, so it can be started again.
const RETRYABLE_OUTCOMES: readonly JobOutcome[] = ['failed', 'checks_failed', 'cancelled', 'not_submitted']

export function canRetryConversion(outcome: JobOutcome): boolean {
  return RETRYABLE_OUTCOMES.includes(outcome)
}

// "health, 2 CPUs, 32G, 04:00:00"
export function describeResources(resources: JobResources): string {
  return `${resources.partition}, ${resources.cpus} CPUs, ${resources.mem}, ${resources.time}`
}
