import { useEffect, useState } from 'react'
import { ApiError, listRuns } from '../api'
import { formatTimestamp } from '../format'
import type { RunKind, RunStatus, RunsListResponse } from '../types'

// The runs under every folder the validator reads (GET /api/runs): the runs
// root, and the folders of each chain. Click one to open it.
//
// Safety: every value shown here comes from files on disk and renders as
// plain React text, never as markup.

const STATUS_LABELS: Record<RunStatus, string> = {
  checks_passed: 'checks passed',
  checks_failed: 'checks failed',
  no_summary: 'no summary yet',
  in_progress: 'in progress',
  may_have_stopped: 'may have stopped',
  failed: 'failed',
}

const STATUS_CLASSES: Record<RunStatus, string | undefined> = {
  checks_passed: 'run-status-passed',
  checks_failed: 'run-status-failed',
  no_summary: 'muted',
  in_progress: undefined,
  may_have_stopped: 'run-status-failed',
  failed: 'run-status-failed',
}

const KIND_LABELS: Record<RunKind, string> = {
  prepared: 'Prepared',
  filter: 'Filter',
}

function formatCount(value: number | null): string {
  return value === null ? '—' : value.toLocaleString()
}

export default function RunsList({ onOpen }: { onOpen: (runDir: string) => void }) {
  const [data, setData] = useState<RunsListResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  // Bumped by the Refresh button to run the effect below again.
  const [reloads, setReloads] = useState(0)

  useEffect(() => {
    let cancelled = false
    listRuns()
      .then((response) => {
        if (!cancelled) setData(response)
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setData(null)
        setError(caught instanceof ApiError ? caught.message : String(caught))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [reloads])

  function refresh() {
    setLoading(true)
    setError(null)
    setReloads((count) => count + 1)
  }

  return (
    <div className="runs-list">
      <div className="runs-list-header">
        <h3>Runs</h3>
        <button type="button" className="secondary" disabled={loading} onClick={refresh}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {data && data.roots.length === 0 && <p className="muted">No folders to read runs from.</p>}

      {data?.roots.map((root) => (
        <div key={root.root} className="runs-root">
          <p className="muted small">
            <strong>{root.label}:</strong> <span className="mono location-text">{root.root}</span>
          </p>
          {root.runs.length === 0 && (
            <p className="muted">No runs yet.{!root.exists && " This folder doesn't exist yet."}</p>
          )}
          {root.truncated && (
            <p className="banner banner-warning small">
              The list was cut short: only the first folders under this folder were looked at.
            </p>
          )}
          {root.runs.length > 0 && (
            <div className="skipped-table-wrap">
              <table className="skipped-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Kind</th>
                    <th>Created</th>
                    <th>Status</th>
                    <th className="number">Read</th>
                    <th className="number">Written</th>
                    <th className="number">Skipped</th>
                    <th className="number">Removed</th>
                  </tr>
                </thead>
                <tbody>
                  {root.runs.map((run) => (
                    <tr key={run.run_dir} onClick={() => onOpen(run.run_dir)}>
                      <td title={run.run_dir}>
                        {/* A click on the button reaches the row's own onClick. */}
                        <button type="button" className="link-button">
                          {run.path}
                        </button>
                      </td>
                      <td>{KIND_LABELS[run.kind]}</td>
                      <td title={run.created_at ?? undefined}>
                        {run.created_at ? formatTimestamp(run.created_at) : '—'}
                      </td>
                      <td className={STATUS_CLASSES[run.status]}>{STATUS_LABELS[run.status]}</td>
                      <td className="number">{formatCount(run.rows_read)}</td>
                      <td className="number">{formatCount(run.written)}</td>
                      <td className="number">{formatCount(run.skipped)}</td>
                      <td className="number">{formatCount(run.removed)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
