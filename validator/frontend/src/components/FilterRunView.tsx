import { formatBytes, formatSetting } from '../format'
import type { FilterRunResponse } from '../types'
import PreparedRows from './PreparedRows'

// The "Filter run" view: what one filter step (filter_rows.py) did to the run
// before it. It shows the counts, why rows were removed, the rules and the
// scanner results the filter used, its own checks, and the rows it kept, each
// with "Show original" (the lookup follows the parent runs back to the
// Prepared run). Read-only: everything here comes from the run's own
// summary.json and run_identity.json, and its written files (through the
// backend).
//
// Safety: every value shown here comes from files on disk and renders as
// plain React text, never as markup.

export default function FilterRunView({
  run,
  onOpenRun,
}: {
  run: FilterRunResponse
  // Opens another run in the run view, the way a click in the runs list does.
  onOpenRun: (runDir: string) => void
}) {
  const { summary } = run
  const removedByReason = Object.entries(summary.removed_by_reason ?? {})
  const rules = Object.entries(summary.rules ?? {})
  const scanners = Object.entries(summary.scanner_results ?? {})
  const keptFiles = summary.output.files_written
  const checks = summary.checks

  return (
    <div className="run-summary">
      <p>
        <strong>Filter run:</strong> <span className="mono location-text">{run.run_dir}</span>
      </p>
      <p>
        Read {summary.source.rows_read.toLocaleString()} · Kept {summary.rows.written.toLocaleString()} · Removed{' '}
        {summary.rows.removed.toLocaleString()}
      </p>
      <p className="small">
        Output: {summary.output.files.toLocaleString()} file{summary.output.files === 1 ? '' : 's'},{' '}
        {summary.output.rows_on_disk.toLocaleString()} rows, {formatBytes(summary.output.bytes_on_disk)} on disk
      </p>

      <p className="small">
        Filtered the run <span className="mono">{summary.parent.run}</span>{' '}
        <button type="button" className="link-button" onClick={() => onOpenRun(summary.parent.run_dir)}>
          Open it
        </button>
        <br />
        <span className="muted">
          ({summary.parent.files.toLocaleString()} file{summary.parent.files === 1 ? '' : 's'} in{' '}
          <span className="mono location-text">{summary.parent.run_dir}</span>)
        </span>
      </p>
      {run.prepared_run_dir !== null && run.prepared_run_dir !== summary.parent.run_dir && (
        <p className="small">
          The Prepared run it came from:{' '}
          <button type="button" className="link-button" onClick={() => onOpenRun(run.prepared_run_dir as string)}>
            {run.prepared_run_dir}
          </button>
        </p>
      )}
      {run.prepared_run_problem !== null && (
        <div className="banner banner-warning small">{run.prepared_run_problem}</div>
      )}

      {checks.passed ? (
        <p className="small">All of this run's own checks passed.</p>
      ) : (
        <div className="banner banner-error small">
          This run's own checks failed:
          <ul className="lookup-differences">
            {checks.failures.map((failure) => (
              <li key={failure}>{failure}</li>
            ))}
          </ul>
        </div>
      )}
      {checks.warnings.length > 0 && (
        <div className="banner banner-warning small">
          Warnings:
          <ul className="lookup-differences">
            {checks.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <h3>Why rows were removed</h3>
      {removedByReason.length === 0 ? (
        <p className="muted">This step removed no rows.</p>
      ) : (
        <table className="counts-table">
          <thead>
            <tr>
              <th>Reason</th>
              <th className="number">Rows</th>
            </tr>
          </thead>
          <tbody>
            {removedByReason.map(([reason, count]) => (
              <tr key={reason}>
                <th scope="row">{reason}</th>
                <td className="number">{count.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted small">A row can be removed for more than one reason, so these can add up to more than the rows removed.</p>

      <h3>Rules</h3>
      <table className="counts-table">
        <tbody>
          {rules.map(([name, value]) => (
            <tr key={name}>
              <th scope="row">{name}</th>
              <td className="mono location-text">{formatSetting(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Scanner results used</h3>
      <table className="counts-table">
        <tbody>
          {scanners.map(([name, info]) => (
            <tr key={name}>
              <th scope="row">{name}</th>
              <td className="mono location-text">
                {info.results_dir} ({info.tool}
                {info.mode ? `, ${info.mode}` : ''})
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Kept rows ({summary.rows.written.toLocaleString()})</h3>
      {keptFiles === undefined ? (
        <p className="muted">
          This run's <code>summary.json</code> doesn't list the files it wrote, so its rows can't be opened here.
        </p>
      ) : (
        <PreparedRows key={run.run_dir} runDir={run.run_dir} files={keptFiles} />
      )}
    </div>
  )
}
