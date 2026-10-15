import { formatCount, formatTimestamp } from '../format'
import { hrefFor } from '../route'
import { isAnalysisGoing } from '../scans'
import type { AnalysisState, AutoDropOutcome, AutoDropView } from '../types'
import type { ChipTone } from './Chip'
import Chip from './Chip'

const OUTCOME_LABELS: Record<AutoDropOutcome, string> = {
  waiting: 'Waiting',
  dropping: 'Dropping',
  dropped: 'Dropped',
  nothing_to_drop: 'Nothing to drop',
  not_dropped: 'Not dropped',
  failed: 'Failed',
  interrupted: 'Interrupted',
}

const OUTCOME_TONES: Record<AutoDropOutcome, ChipTone> = {
  waiting: 'neutral',
  dropping: 'neutral',
  dropped: 'ok',
  nothing_to_drop: 'ok',
  not_dropped: 'error',
  failed: 'error',
  interrupted: 'warn',
}

// What is said before the drop has been decided. It is decided once the check of the scan has
// finished (within a minute), so a check that didn't finish means it stays undecided.
function waitingText(analysis: AnalysisState): string {
  if (isAnalysisGoing(analysis)) return 'It is decided once the check of this scan has finished.'
  if (analysis === 'succeeded') return 'The check has finished. It is being decided now, which takes under a minute.'
  return 'Nothing is dropped until the check of this scan has finished and passed. It didn\'t finish: use "Check again" above.'
}

// What came of a scan's "drop duplicates automatically" (D17): nothing dropped because the
// coverage check failed, or the filtered datasets that were made. Every value is plain text.
export default function AutoDropCard({ auto, analysis }: { auto: AutoDropView; analysis: AnalysisState }) {
  return (
    <div className="card">
      <div className="page-header">
        <h3 className="scan-heading">Automatic drop</h3>
        <Chip label={OUTCOME_LABELS[auto.outcome]} tone={OUTCOME_TONES[auto.outcome]} />
      </div>

      {auto.outcome === 'waiting' && <p className="small">{waitingText(analysis)}</p>}
      {auto.outcome === 'dropping' && <p className="small">The filtered datasets are being made.</p>}
      {auto.outcome === 'nothing_to_drop' && <p className="small">The coverage check passed, and the scan flagged no rows, so there was nothing to drop.</p>}
      {auto.outcome === 'dropped' && (
        <p className="small">
          The coverage check passed, so the duplicates were dropped by themselves into new datasets. Their jobs are on the{' '}
          <a href={hrefFor({ page: 'activity' })}>Activity page</a>.
          {auto.decided_at !== null && ` Decided ${formatTimestamp(auto.decided_at)}.`}
        </p>
      )}

      {auto.outcome === 'not_dropped' && (
        <>
          <p className="small">
            <strong>Not dropped: the coverage check failed.</strong> Nothing was dropped, for any dataset. The scan may not
            have read exactly the converted data, so these duplicates can't be trusted until this is understood. You can
            still choose rows to drop from the results.
          </p>
          <ul className="coverage-problems">
            {auto.problems.map((problem, index) => (
              <li key={index}>
                {problem.message}
                {problem.path !== null && (
                  <>
                    {' '}
                    <code>{problem.path}</code>
                  </>
                )}
              </li>
            ))}
          </ul>
          {auto.problems_total > auto.problems.length && (
            <p className="muted small">And {formatCount(auto.problems_total - auto.problems.length)} more.</p>
          )}
        </>
      )}

      {(auto.outcome === 'failed' || auto.outcome === 'interrupted') && auto.reason !== null && (
        <p className="small">{auto.reason}</p>
      )}

      {auto.datasets.length > 0 && (
        <ul className="small">
          {auto.datasets.map((entry) => (
            <li key={entry.dataset_id} className={entry.error !== null ? 'error' : undefined}>
              <strong>{entry.dataset_name ?? entry.dataset_id}</strong>:{' '}
              {entry.filtered_id !== null ? (
                <>
                  {formatCount(entry.rows)} {entry.rows === 1 ? 'row' : 'rows'} dropped into{' '}
                  <a href={hrefFor({ page: 'dataset', id: entry.filtered_id })}>{entry.filtered_name ?? entry.filtered_id}</a>
                  {entry.error !== null && `. ${entry.error}`}
                </>
              ) : (
                (entry.error ?? entry.note ?? 'Nothing to drop.')
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
