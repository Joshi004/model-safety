import { useEffect, useRef } from 'react'
import { getDatasetResults } from '../api'
import { formatCount, formatTimestamp } from '../format'
import { hrefFor } from '../route'
import { SCAN_ACTIONS, STATE_LABELS, STATE_TONES, analysisNote, describeFlagged, isAnalysisGoing, isHolding } from '../scans'
import type { Dataset, DatasetResultsResponse } from '../types'
import { useRefreshing } from '../useRefreshing'
import Chip from './Chip'
import CoverageBadge from './CoverageBadge'

const POLL_MS = 5000

// Whether anything on the page is still changing: a scan that is going, or the check of a
// finished one.
function anythingGoing(response: DatasetResultsResponse): boolean {
  return Object.values(response.scanners).some(
    (view) => isHolding(view.summary.state) || (view.result !== null && isAnalysisGoing(view.result.analysis.state)),
  )
}

// The action of the action bar that starts a scanner's scan, in words.
function startHint(scannerKey: string): string {
  const title = Object.values(SCAN_ACTIONS).find((action) => action.scannerKeys.includes(scannerKey))?.title
  return title === undefined ? 'a scan' : `"${title}"`
}

// What each scanner found in this dataset, one card each: the flagged count, whether the scan
// read exactly this dataset's data (coverage), and a link to the full result. Everything
// shown comes from files on disk and renders as plain text.
export default function ResultsTab({ dataset, onChanged }: { dataset: Dataset; onChanged: () => void }) {
  const { data, error, reload } = useRefreshing(
    () => getDatasetResults(dataset.id),
    dataset.id,
    POLL_MS,
    anythingGoing,
    `results:${dataset.id}`,
  )

  // The header shows the same counts and coverage, so it is read again once the checks have finished.
  const going = data !== null && anythingGoing(data)
  const wasGoing = useRef(false)
  useEffect(() => {
    if (wasGoing.current && !going) onChanged()
    wasGoing.current = going
  }, [going, onChanged])

  return (
    <>
      <p className="muted">
        What each scan found in this dataset. The numbers are for this dataset only, even when a scan covered several.
      </p>

      {error !== null && (
        <div className="banner banner-error" role="alert">
          <span>Couldn't load the results: {error}</span>
          <button type="button" className="secondary" onClick={reload}>
            Retry
          </button>
        </div>
      )}
      {data?.slurm_error != null && (
        <div className="banner banner-warning small">
          SLURM couldn't be reached, so the state of a check may show as unknown. {data.slurm_error}
        </div>
      )}
      {data === null && error === null && <p className="muted">Loading…</p>}

      {data !== null &&
        Object.entries(data.scanners).map(([key, view]) => {
          const result = view.result
          const note = result === null ? null : analysisNote(result.analysis)
          return (
            <div key={key} className="card result-card">
              <div className="page-header">
                <h2>{view.label}</h2>
                {(result === null || view.summary.state !== 'succeeded') && (
                  <Chip
                    label={STATE_LABELS[view.summary.state]}
                    tone={STATE_TONES[view.summary.state]}
                    title={view.summary.reason ?? undefined}
                  />
                )}
                {result !== null && (
                  <>
                    <CoverageBadge verdict={result.coverage?.verdict ?? null} state={result.analysis.state} />
                    <a
                      className="push-right"
                      href={hrefFor({ page: 'dataset', id: dataset.id, tab: 'results', scanner: key })}
                    >
                      Open the result
                    </a>
                  </>
                )}
              </div>

              {result === null ? (
                <p className="muted small">
                  {view.summary.state === 'not_scanned'
                    ? `Not scanned. Tick this dataset on the Datasets page and choose ${startHint(key)}.`
                    : (view.summary.reason ?? '')}
                </p>
              ) : (
                <>
                  <p className="result-headline">
                    {describeFlagged(result.flagged)}
                    {result.rows.scanned !== null && (
                      <span className="muted"> of {formatCount(result.rows.scanned)} rows scanned</span>
                    )}
                  </p>
                  {isHolding(view.summary.state) && (
                    <p className="muted small">A newer scan is {STATE_LABELS[view.summary.state].toLowerCase()}. This is the earlier result.</p>
                  )}
                  {note !== null && <p className="muted small">{note.text}</p>}
                  <p className="muted small">
                    {result.finished_at !== null && `Finished ${formatTimestamp(result.finished_at)}. `}
                    <a className="mono" href={hrefFor({ page: 'activity', scan: result.scan_id })}>
                      {result.scan_id}
                    </a>
                  </p>
                </>
              )}
            </div>
          )
        })}
    </>
  )
}
