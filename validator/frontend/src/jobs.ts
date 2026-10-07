// Words for a conversion job's outcome and settings, shared by the batches
// list and the batch page. No React here.

import { formatBytes, formatTimestamp } from './format'
import type { FolderContents, JobOutcome, JobResources, RunBrief } from './types'

export const OUTCOME_LABELS: Record<JobOutcome, string> = {
  queued: 'queued',
  running: 'running',
  stalled: 'may have stopped',
  succeeded: 'succeeded',
  checks_failed: 'checks failed',
  failed: 'failed',
  cancelled: 'cancelled',
  skipped: 'skipped (already done)',
  not_submitted: 'not submitted',
  unknown: 'unknown',
}

// run-status-passed / run-status-failed are the colours the runs list uses.
export const OUTCOME_CLASSES: Record<JobOutcome, string | undefined> = {
  queued: 'muted',
  running: undefined,
  stalled: 'run-status-failed',
  succeeded: 'run-status-passed',
  checks_failed: 'run-status-failed',
  failed: 'run-status-failed',
  cancelled: 'muted',
  skipped: 'muted',
  not_submitted: 'run-status-failed',
  unknown: 'muted',
}

// Outcomes of a job that is still going: the page keeps refreshing for these.
export const ACTIVE_OUTCOMES: JobOutcome[] = ['queued', 'running', 'stalled']

// "2 succeeded, 1 running", in the order the outcomes are listed above.
export function describeOutcomes(byOutcome: Partial<Record<JobOutcome, number>>): string {
  const parts = (Object.keys(OUTCOME_LABELS) as JobOutcome[])
    .filter((outcome) => (byOutcome[outcome] ?? 0) > 0)
    .map((outcome) => `${byOutcome[outcome]} ${OUTCOME_LABELS[outcome]}`)
  return parts.length > 0 ? parts.join(', ') : 'no jobs'
}

export function describeResources(resources: JobResources | null): string {
  if (resources === null) return 'resources not recorded'
  return `${resources.partition} · ${resources.cpus} CPU${resources.cpus === 1 ? '' : 's'} · ${resources.mem} · ${resources.time}`
}

export function formatCount(value: number | null): string {
  return value === null ? '?' : value.toLocaleString()
}

export function describeBrief(brief: RunBrief): string {
  const finished = brief.created_at ? `Finished ${formatTimestamp(brief.created_at)}: ` : ''
  const counts = `read ${formatCount(brief.rows_read)}, written ${formatCount(brief.written)}, skipped ${formatCount(brief.skipped)}`
  return `${finished}${counts}; its own checks ${brief.checks_passed ? 'passed' : 'failed'}.`
}

export function describeContents(contents: FolderContents): string {
  return `${contents.file_count.toLocaleString()} file${contents.file_count === 1 ? '' : 's'}, ${formatBytes(contents.total_bytes)}`
}
