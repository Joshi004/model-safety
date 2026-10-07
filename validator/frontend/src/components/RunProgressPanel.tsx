import { useEffect, useState } from 'react'
import { ApiError, getRunProgress } from '../api'
import { formatTimestamp } from '../format'
import { STEPS, describeTimeLeft, formatAmount, formatSpeed, measureStep, unitSuffix } from '../progress'
import type { RunProgressResponse } from '../types'

// Shown in the Prepared run view while a run is still going (or stopped
// without finishing): which step it's in, how far that step has got, and
// roughly how long it has left. It asks the backend again every 3 seconds
// until the run is `done` (then `onDone` opens the full results) or `failed`.
//
// Safety: every value shown here comes from progress.json and renders as
// plain React text, never as markup.

const POLL_MS = 3000

export default function RunProgressPanel({
  initial,
  onDone,
}: {
  initial: RunProgressResponse
  onDone: (runDir: string) => void
}) {
  const runDir = initial.run_dir
  const [data, setData] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  // Bumped after every request, so the next one is scheduled whether or not
  // the last one worked.
  const [polls, setPolls] = useState(0)

  const progress = data.progress
  const finished = progress !== null && (progress.phase === 'done' || progress.phase === 'failed')

  useEffect(() => {
    if (finished) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      getRunProgress(runDir)
        .then((response) => {
          if (cancelled) return
          setData(response)
          setError(null)
          // onDone is left out of the dependencies on purpose: its identity
          // changes on every render of the parent, and it only uses setters.
          if (response.progress?.phase === 'done') onDone(runDir)
        })
        .catch((caught: unknown) => {
          if (!cancelled) setError(caught instanceof ApiError ? caught.message : String(caught))
        })
        .finally(() => {
          if (!cancelled) setPolls((count) => count + 1)
        })
    }, POLL_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runDir, finished, polls])

  if (progress === null) {
    return (
      <div className="run-progress">
        <h3>Run progress</h3>
        <p className="muted">
          <span className="mono location-text">{runDir}</span> has no usable <code>progress.json</code> now.
        </p>
        {error && <p className="error">{error}</p>}
      </div>
    )
  }

  const { done, total, unit } = progress
  const { stepIndex, percent, rate, secondsLeft } = measureStep(progress)

  return (
    <div className="run-progress">
      <h3>Run progress</h3>
      <p>
        <span className="mono location-text">{runDir}</span>
      </p>

      {progress.phase === 'failed' && (
        <div className="banner banner-error small">
          This run failed:
          <div className="run-progress-error mono">{progress.error ?? '(no message)'}</div>
        </div>
      )}

      {progress.phase === 'done' && <p>Finished. Opening the results…</p>}

      {stepIndex >= 0 && (
        <>
          <p>
            <strong>
              Step {stepIndex + 1} of {STEPS.length}:
            </strong>{' '}
            {STEPS[stepIndex]?.words ?? progress.phase}
          </p>

          {percent === null ? (
            <p className="muted small">working…</p>
          ) : (
            <div className="progress-bar progress-bar-full">
              <div
                className="progress-bar-track"
                role="progressbar"
                aria-valuenow={done}
                aria-valuemin={0}
                aria-valuemax={total ?? undefined}
                aria-label="Progress of the current step"
              >
                <div className="progress-bar-fill" style={{ width: `${percent}%` }} />
              </div>
              <span className="progress-bar-count muted small">
                {formatAmount(done, unit)} of {formatAmount(total ?? 0, unit)}
                {unitSuffix(unit)} ({percent}%)
              </span>
            </div>
          )}

          {progress.files_total !== null && (
            <p className="small">
              Files: {(progress.files_done ?? 0).toLocaleString()} of {progress.files_total.toLocaleString()} done
            </p>
          )}

          <p className="small">
            Written so far: {progress.rows_written.toLocaleString()} · Skipped so far:{' '}
            {progress.rows_skipped.toLocaleString()}
          </p>

          {rate !== null && (
            <p className="small">
              Speed: {formatSpeed(rate, unit)}
              {secondsLeft !== null && <> · {describeTimeLeft(secondsLeft)}</>}
            </p>
          )}
        </>
      )}

      <p className="muted small">Last update: {formatTimestamp(progress.updated_at)}</p>

      {data.may_have_stopped && (
        <div className="banner banner-warning small">
          This run hasn't reported any progress for over 2 minutes, so it may have stopped.
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  )
}
