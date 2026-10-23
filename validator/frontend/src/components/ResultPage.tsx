import { Fragment, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { checkResult, errorMessage, getDatasetResult, listDatasets } from '../api'
import { clearScanner, partFor, selectAllMatching, setRows } from '../drop'
import type { DropSelection, SelectionSource } from '../drop'
import { formatCount, formatSetting, formatTimestamp } from '../format'
import { hrefFor } from '../route'
import {
  STATE_LABELS,
  STATE_TONES,
  actionForScanner,
  analysisNote,
  breakdownName,
  choiceLabel,
  describeFlagged,
  describeMode,
  isAnalysisGoing,
  isHolding,
  scannerNote,
  settingsToRepeat,
  shownSettings,
} from '../scans'
import type { AnalysisState, Dataset, DatasetResultResponse, ScanSubmitResponse, ScannerResult } from '../types'
import { useRefreshing } from '../useRefreshing'
import AutoDropCard from './AutoDropCard'
import Chip from './Chip'
import CommandBox from './CommandBox'
import CoverageBadge from './CoverageBadge'
import FlaggedRows from './FlaggedRows'
import ResultPolicy from './ResultPolicy'
import ScanDialog from './ScanDialog'
import ToxicityFilter from './ToxicityFilter'

const POLL_MS = 4000

// Why the table of flagged rows isn't there: it is made by the same check as the coverage verdict.
function flaggedRowsNote(state: AnalysisState): string {
  if (isAnalysisGoing(state)) return 'The flagged rows are listed once the check of this scan has finished.'
  if (state === 'succeeded') {
    return 'This scan was checked before flagged rows were listed. Use "Check again" above to list them.'
  }
  return "The check of this scan didn't finish, so its flagged rows can't be listed. Use \"Check again\" above."
}

// Whether the page should keep asking: the scan's automatic drop (D17) is being decided or made.
// A drop that waits for a check that failed isn't going to be decided, so it doesn't count.
function autoDropGoing(loaded: DatasetResultResponse): boolean {
  const auto = loaded.auto_drop
  if (auto === null) return false
  if (auto.outcome === 'dropping') return true
  return auto.outcome === 'waiting' && loaded.result !== null && loaded.result.analysis.state === 'succeeded'
}

// The categories and verdicts chosen on the page. Null: the ones the scan was started with.
interface Choice {
  categories: string[] | null
  verdicts: string[] | null
}

function settingText(scannerKey: string, name: string, value: unknown): string {
  if (Array.isArray(value)) {
    return value.length === 0
      ? '(none)'
      : value.map((item) => (typeof item === 'string' ? choiceLabel(scannerKey, name, item) : String(item))).join(', ')
  }
  return typeof value === 'string' ? choiceLabel(scannerKey, name, value) : formatSetting(value)
}

// One scanner's result for one dataset in full: what it found (for this dataset only), whether
// the scan read exactly this dataset's converted data, the breakdowns, and the settings used.
// For toxicity the verdicts and categories that count as flagged can be changed; that only
// changes what is shown (D8). "Run again" opens the scan dialog filled in from this scan.
//
// Safety: every value shown here comes from a file on disk or from SLURM and renders as plain
// React text, never as markup.
export default function ResultPage({
  dataset,
  scanner,
  selection,
  onSelectionChange,
  onChanged,
}: {
  dataset: Dataset
  scanner: string
  // The rows chosen to drop from this dataset, from every result page (kept by the dataset page).
  selection: DropSelection
  onSelectionChange: (change: (current: DropSelection) => DropSelection) => void
  // Called when something that the dataset's header shows may have changed.
  onChanged: () => void
}) {
  const [choice, setChoice] = useState<Choice>({ categories: null, verdicts: null })
  const { data, error, reload } = useRefreshing(
    () => getDatasetResult(dataset.id, scanner, choice),
    `${dataset.id}/${scanner}/${JSON.stringify(choice)}`,
    POLL_MS,
    (loaded) =>
      isHolding(loaded.summary.state) ||
      (loaded.result !== null && isAnalysisGoing(loaded.result.analysis.state)) ||
      autoDropGoing(loaded),
    `result:${dataset.id}/${scanner}/${JSON.stringify(choice)}`,
  )
  const [checking, setChecking] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<ReactNode>(null)
  // The datasets "Run again" opens the dialog for, in the order the scan read them.
  const [repeating, setRepeating] = useState<Dataset[] | null>(null)

  // The header shows the same count and coverage as this page, so it is read again once the
  // check of the scan has finished.
  const analysisGoing = data?.result != null && isAnalysisGoing(data.result.analysis.state)
  const wasGoing = useRef(false)
  useEffect(() => {
    if (wasGoing.current && !analysisGoing) onChanged()
    wasGoing.current = analysisGoing
  }, [analysisGoing, onChanged])

  // Rows chosen from a scan that a newer scan has replaced are no longer rows of this result.
  const chosenScan = selection[scanner]?.scanId
  const replaced = data?.result != null && chosenScan !== undefined && chosenScan !== data.result.scan_id
  useEffect(() => {
    if (replaced) onSelectionChange((current) => clearScanner(current, scanner))
  }, [replaced, scanner, onSelectionChange])

  async function checkAgain() {
    setChecking(true)
    setActionError(null)
    try {
      const response = await checkResult(dataset.id, scanner)
      if (response.submit_error !== null) setActionError(response.submit_error)
      reload()
    } catch (failure) {
      setActionError(errorMessage(failure))
    } finally {
      setChecking(false)
    }
  }

  async function runAgain(result: ScannerResult) {
    setActionError(null)
    try {
      const { datasets } = await listDatasets(true)
      const byId = new Map(datasets.map((item) => [item.id, item]))
      const chosen = result.datasets.flatMap((item) => {
        const found = byId.get(item.id)
        return found === undefined ? [] : [found]
      })
      if (chosen.length === 0) {
        setActionError('None of the datasets this scan covered is registered any more.')
        return
      }
      setNotice(null)
      setRepeating(chosen)
    } catch (failure) {
      setActionError(errorMessage(failure))
    }
  }

  function handleStarted(response: ScanSubmitResponse) {
    setRepeating(null)
    const problems = response.errors.length > 0 ? ` Problems: ${response.errors.join(' ')}` : ''
    setNotice(
      <>
        Started {response.scans.length} {response.scans.length === 1 ? 'scan' : 'scans'}. Watch it on the{' '}
        <a href={hrefFor({ page: 'activity' })}>Activity page</a>. This result stays until the new scan finishes.
        {problems}
      </>,
    )
    reload()
    onChanged()
  }

  const result = data?.result ?? null
  const note = result === null ? null : analysisNote(result.analysis)
  // The datasets the scan covered, in the order it read them: this one is marked, and one that
  // isn't registered any more is said so rather than left out.
  const names =
    result?.datasets.map((item) => {
      if (item.id === dataset.id) return `${item.name ?? dataset.name} (this dataset)`
      return item.name ?? 'a dataset that is no longer registered'
    }) ?? []
  const filter = result?.filter
  const selectedCategories = choice.categories ?? filter?.saved_categories ?? []
  const selectedVerdicts = choice.verdicts ?? filter?.saved_verdicts ?? []
  // Where the rows chosen on this page come from: this scan, under this choice (toxicity only).
  const source = (scanId: string): SelectionSource => ({
    scanner,
    scanId,
    categories: choice.categories,
    verdicts: choice.verdicts,
  })

  return (
    <>
      <p className="back-link">
        <a href={hrefFor({ page: 'dataset', id: dataset.id, tab: 'results' })}>← All results of this dataset</a>
      </p>

      <div className="page-header">
        <h2>{data?.label ?? scanner}</h2>
        {data !== null && (data.result === null || data.summary.state !== 'succeeded') && (
          <Chip
            label={STATE_LABELS[data.summary.state]}
            tone={STATE_TONES[data.summary.state]}
            title={data.summary.reason ?? undefined}
          />
        )}
        {result !== null && <CoverageBadge verdict={result.coverage?.verdict ?? null} state={result.analysis.state} />}
      </div>

      {error !== null && (
        <div className="banner banner-error" role="alert">
          <span>Couldn't load this result: {error}</span>
          <button type="button" className="secondary" onClick={reload}>
            Retry
          </button>
        </div>
      )}
      {data?.slurm_error != null && (
        <div className="banner banner-warning small">
          SLURM couldn't be reached, so the state of the check may show as unknown. {data.slurm_error}
        </div>
      )}
      {notice !== null && (
        <div className="banner banner-ok" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      {actionError !== null && (
        <div className="banner banner-error" role="alert">
          {actionError}
        </div>
      )}

      {data === null && error === null && <p className="muted">Loading…</p>}

      {data !== null && result === null && (
        <div className="card empty-state">
          <p>
            <strong>{data.label} hasn't finished on this dataset.</strong>
          </p>
          <p className="muted">
            Its state is: {STATE_LABELS[data.summary.state].toLowerCase()}
            {data.summary.reason !== null && ` (${data.summary.reason})`}. Start a scan from the{' '}
            <a href={hrefFor({ page: 'datasets' })}>Datasets page</a>.
          </p>
        </div>
      )}

      {data !== null && result !== null && (
        <>
          {isHolding(data.summary.state) && (
            <div className="banner banner-warning small">
              A newer {data.label} scan of this dataset is {STATE_LABELS[data.summary.state].toLowerCase()}. This is the
              earlier result, and it stays until the new scan finishes.
            </div>
          )}
          {note !== null && (
            <div className={note.tone === 'error' ? 'banner banner-error' : 'banner banner-warning small'}>{note.text}</div>
          )}

          <div className="card">
            <div className="page-header">
              <h3 className="scan-heading">What it found in this dataset</h3>
              <button
                type="button"
                className="secondary push-right"
                onClick={() => void runAgain(result)}
                disabled={isHolding(data.summary.state)}
                title={isHolding(data.summary.state) ? 'A scan of this dataset is already going.' : undefined}
              >
                Run again
              </button>
            </div>
            <dl className="facts">
              <dt>Flagged</dt>
              <dd>
                <strong>{describeFlagged(result.flagged)}</strong>
              </dd>
              <dt>Rows scanned</dt>
              <dd>
                {formatCount(result.rows.scanned)}
                {result.rows.scanned !== null &&
                  ` (${formatCount(result.rows.with_text)} with text, ${formatCount(result.rows.without_text)} without)`}
              </dd>
              {result.facts.map((fact) => (
                <Fragment key={fact.label}>
                  <dt>{fact.label}</dt>
                  <dd>{formatCount(fact.value)}</dd>
                </Fragment>
              ))}
              {result.mode !== null && (
                <>
                  <dt>Compared</dt>
                  <dd>{describeMode(result.mode, names)}</dd>
                </>
              )}
              {result.notes.map((item) => (
                <Fragment key={item.label}>
                  <dt>{item.label}</dt>
                  <dd>{item.text}</dd>
                </Fragment>
              ))}
              <dt>Scan</dt>
              <dd>
                <a className="mono" href={hrefFor({ page: 'activity', scan: result.scan_id })}>
                  {result.scan_id}
                </a>
                {result.finished_at !== null && `, finished ${formatTimestamp(result.finished_at)}`}
              </dd>
            </dl>
            {scannerNote(scanner) !== null && <p className="muted small">{scannerNote(scanner)}</p>}
            {scanner === 'dedup' && result.mode === 'across' && result.datasets.length > 1 && (
              <p className="muted small">
                {
                  "A row here is flagged when it repeats a row that was read earlier, in this dataset or in a dataset that comes before it in the 'Compared' list. Rows of this dataset that other datasets repeat are kept here, and are flagged on those datasets' pages instead. Datasets this scan did not cover are not compared with this one."
                }
              </p>
            )}
            {scanner === 'dedup' && (
              <p className="muted small">
                {data.auto_drop === null
                  ? 'Nothing is dropped by this scan. You choose what to drop from the results.'
                  : 'This scan was started to drop its duplicates by itself once its coverage check passed (see "Automatic drop" below). You can still choose other rows to drop from the results.'}
              </p>
            )}
          </div>

          <div className="card">
            <div className="page-header">
              <h3 className="scan-heading">Coverage</h3>
              <CoverageBadge verdict={result.coverage?.verdict ?? null} state={result.analysis.state} />
              <button
                type="button"
                className="secondary push-right"
                onClick={() => void checkAgain()}
                disabled={checking || isAnalysisGoing(result.analysis.state)}
                title={isAnalysisGoing(result.analysis.state) ? 'The check is already going.' : undefined}
              >
                {checking ? 'Starting…' : 'Check again'}
              </button>
            </div>
            {result.coverage === null && (
              <p className="muted small">
                {isAnalysisGoing(result.analysis.state)
                  ? 'The coverage verdict shows once the check of this scan has finished. It is one short CPU job.'
                  : "There is no coverage verdict because the check didn't finish. Use \"Check again\"."}
              </p>
            )}
            {result.coverage?.verdict === 'pass' && (
              <p className="small">
                The scan read exactly the converted files of this dataset, and every row in them, and finished every
                stage.
                {result.coverage.checked_at !== null && ` Checked ${formatTimestamp(result.coverage.checked_at)}.`}
              </p>
            )}
            {result.coverage?.verdict === 'fail' && (
              <>
                <p className="small">
                  The scan may not have read exactly this dataset's converted data. Don't rely on these numbers until
                  this is understood.
                  {result.coverage.checked_at !== null && ` Checked ${formatTimestamp(result.coverage.checked_at)}.`}
                </p>
                <ul className="coverage-problems">
                  {result.coverage.problems.map((problem, index) => (
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
                {result.coverage.problems_total > result.coverage.problems.length && (
                  <p className="muted small">
                    And {formatCount(result.coverage.problems_total - result.coverage.problems.length)} more.
                  </p>
                )}
              </>
            )}
          </div>

          {data.auto_drop !== null && <AutoDropCard auto={data.auto_drop} analysis={result.analysis.state} />}

          {filter !== undefined && result.flagged.count !== null && (
            <ToxicityFilter
              filter={filter}
              categories={selectedCategories}
              verdicts={selectedVerdicts}
              onChange={(next) => {
                // Rows chosen under the earlier choice might not be flagged under this one.
                onSelectionChange((current) => clearScanner(current, scanner))
                setChoice(next)
              }}
            />
          )}

          {result.policy != null && <ResultPolicy policy={result.policy} datasetId={dataset.id} />}

          {result.breakdowns.length > 0 && (
            <div className="result-breakdowns">
              {result.breakdowns.map((breakdown) => (
                <div key={breakdown.title} className="card">
                  <h3 className="scan-heading">{breakdown.title}</h3>
                  {breakdown.items.length === 0 ? (
                    <p className="muted small">Nothing was found.</p>
                  ) : (
                    <table className="history-table">
                      <thead>
                        <tr>
                          <th>Name</th>
                          <th>Count</th>
                        </tr>
                      </thead>
                      <tbody>
                        {breakdown.items.map((item) => (
                          <tr key={item.name}>
                            <td>{breakdownName(scanner, breakdown.title, item.name)}</td>
                            <td>{formatCount(item.count)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {breakdown.note !== null && <p className="muted small">{breakdown.note}</p>}
                </div>
              ))}
            </div>
          )}

          {result.has_index ? (
            // A new scan, or a new choice of toxicity categories and verdicts, starts the table again.
            <FlaggedRows
              key={`${result.scan_id}|${JSON.stringify(choice)}`}
              datasetId={dataset.id}
              scanner={scanner}
              categories={choice.categories}
              verdicts={choice.verdicts}
              chosen={partFor(selection, source(result.scan_id))}
              onRows={(rows, checked) =>
                onSelectionChange((current) => setRows(current, source(result.scan_id), rows, checked))
              }
              onSelectAll={(filters, total) =>
                onSelectionChange((current) => selectAllMatching(current, source(result.scan_id), filters, total))
              }
              onClear={() => onSelectionChange((current) => clearScanner(current, scanner))}
            />
          ) : (
            <div className="card">
              <h3 className="scan-heading">Flagged rows</h3>
              <p className="muted small">{flaggedRowsNote(result.analysis.state)}</p>
            </div>
          )}

          <div className="card">
            <h3 className="scan-heading">Settings the scan used</h3>
            <dl className="facts">
              {Object.entries(shownSettings(scanner, result.settings)).map(([name, value]) => (
                <Fragment key={name}>
                  <dt>{name.replace(/_/g, ' ')}</dt>
                  <dd>{settingText(scanner, name, value)}</dd>
                </Fragment>
              ))}
            </dl>
          </div>

          <details className="card">
            <summary className="small">The check's command and logs</summary>
            {result.analysis.command_text != null ? (
              <CommandBox
                command={result.analysis.command_text}
                title="Submitted with"
                note="Exactly what was run to check this scan."
              />
            ) : (
              <p className="muted small">No command yet: the check hasn't been submitted.</p>
            )}
            {(result.analysis.logs ?? [])
              .filter((log) => log.tail !== null)
              .map((log) => (
                <div key={log.name}>
                  <h4>{log.name} (last lines)</h4>
                  <pre className="raw-json">{log.tail || '(empty)'}</pre>
                </div>
              ))}
          </details>
        </>
      )}

      {repeating !== null && result !== null && (
        <ScanDialog
          action={actionForScanner(scanner)}
          datasets={repeating}
          initial={{
            scannerKey: scanner,
            settings: settingsToRepeat(scanner, result.settings),
            mode: result.mode,
            autoDrop: data?.auto_drop != null,
          }}
          onClose={() => setRepeating(null)}
          onStarted={handleStarted}
        />
      )}
    </>
  )
}
