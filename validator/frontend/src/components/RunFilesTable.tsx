import { fileLabels } from '../origin'
import type { RunSourceFile } from '../types'

// Every original file of a run with where each of its lines or rows went:
// written, skipped or left out. Whether a file adds up is judged the same
// way modelsafety/readers/prepare_rows.py's _run_checks() judges it.
//
// Safety: the paths and counts come from summary.json and render as plain
// React text, never as markup.

type CountKey =
  | 'lines'
  | 'blank_lines'
  | 'rows_read'
  | 'rows_in_footer'
  | 'rows_written'
  | 'rows_skipped'
  | 'rows_left_out'

const COLUMNS: { key: CountKey; label: string }[] = [
  { key: 'lines', label: 'Lines' },
  { key: 'blank_lines', label: 'Blank lines' },
  { key: 'rows_read', label: 'Rows read' },
  { key: 'rows_in_footer', label: "File's own row count" },
  { key: 'rows_written', label: 'Written' },
  { key: 'rows_skipped', label: 'Skipped' },
  { key: 'rows_left_out', label: 'Left out' },
]

// What's wrong with a file's counts, or an empty list when they add up.
// Null when the file has no per-file counts to check.
function accountingProblems(file: RunSourceFile): string[] | null {
  if (file.rows_written === undefined) return null
  const problems: string[] = []
  const blank = file.blank_lines ?? 0

  if (file.lines !== undefined && file.rows_read !== undefined && file.lines !== blank + file.rows_read) {
    problems.push(`${file.lines} lines is not ${blank} blank + ${file.rows_read} rows read`)
  }

  let accounted = file.rows_written + (file.rows_skipped ?? 0) + (file.rows_left_out ?? 0)
  let inFile: number | undefined
  let inFileName: string
  if (file.lines !== undefined) {
    inFile = file.lines
    inFileName = 'lines'
    accounted += blank
  } else if (file.rows_in_footer !== undefined) {
    inFile = file.rows_in_footer
    inFileName = "rows in the file's own count"
  } else {
    inFile = file.rows_read
    inFileName = 'rows read'
  }
  if (inFile !== undefined && inFile !== accounted) {
    problems.push(`${inFile} ${inFileName} is not ${accounted} (blank + written + skipped + left out)`)
  }
  return problems
}

function formatCount(value: number | undefined): string {
  return value === undefined ? '—' : value.toLocaleString()
}

export default function RunFilesTable({ files }: { files: RunSourceFile[] }) {
  if (!files.some((file) => file.rows_written !== undefined)) {
    return (
      <p className="muted small">
        Per-file counts aren't available for this run's input: when rows are read with{' '}
        <code>load_dataset</code>, there's no way to tell which file each row came from.
      </p>
    )
  }

  const columns = COLUMNS.filter((column) => files.some((file) => typeof file[column.key] === 'number'))
  const labels = fileLabels(files.map((file) => file.path))

  return (
    <div className="run-files">
      <h3>Original files</h3>
      <div className="skipped-table-wrap">
        <table className="counts-table">
          <thead>
            <tr>
              <th>File</th>
              {columns.map((column) => (
                <th key={column.key} className="number">
                  {column.label}
                </th>
              ))}
              <th>Adds up?</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => {
              const problems = accountingProblems(file)
              return (
                <tr key={file.path}>
                  <td title={file.path}>{labels[file.path]}</td>
                  {columns.map((column) => (
                    <td key={column.key} className="number">
                      {formatCount(file[column.key])}
                    </td>
                  ))}
                  <td title={problems?.join('; ')}>
                    {problems === null ? (
                      '—'
                    ) : problems.length === 0 ? (
                      <span className="adds-up">Yes</span>
                    ) : (
                      <span className="not-adding-up">No: {problems.join('; ')}</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            <tr>
              <td>
                <strong>Total ({files.length.toLocaleString()} files)</strong>
              </td>
              {columns.map((column) => (
                <td key={column.key} className="number">
                  <strong>{files.reduce((sum, file) => sum + (file[column.key] ?? 0), 0).toLocaleString()}</strong>
                </td>
              ))}
              <td />
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  )
}
