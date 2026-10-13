import { getChainStep } from '../api'
import { describeProgress } from '../chains'
import { formatCount, formatPercent, formatSetting, formatTimestamp } from '../format'
import type {
  ChainStepResponse,
  CoverageSection,
  CoverageStageDetails,
  Counts,
  DedupDetails,
  FilterStageDetails,
  NerDetails,
  RegexPiiDetails,
  RowStatusStageDetails,
  ShortList,
  ToxicityDetails,
  TraceStageDetails,
} from '../types'
import { useRefreshing } from '../useRefreshing'
import StateChip, { VerdictChip } from './StateChip'

// What one stage of a chain found, opened from the chain page: the scanner's
// counts, the coverage verdict in plain words, what a filter removed and the
// rules it used. Read-only: it shows what the summary files say.
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

// A few counts that sit side by side: label, value.
function KeyFigures({ rows }: { rows: [string, string][] }) {
  return (
    <table className="counts-table key-figures">
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <th scope="row">{label}</th>
            <td className="number">{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// One count per name, the biggest first (the backend sends them sorted).
function CountsTable({ title, counts, note }: { title: string; counts: Counts | null | undefined; note?: string }) {
  if (counts === null || counts === undefined) return null
  const entries = Object.entries(counts)
  return (
    <>
      <h4>{title}</h4>
      {entries.length === 0 ? (
        <p className="muted small">None.</p>
      ) : (
        <table className="counts-table">
          <tbody>
            {entries.map(([name, count]) => (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td className="number">{formatCount(count)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {note && <p className="muted small">{note}</p>}
    </>
  )
}

function SettingsTable({ title, values }: { title: string; values: Record<string, unknown> | null | undefined }) {
  if (values === null || values === undefined) return null
  return (
    <>
      <h4>{title}</h4>
      <table className="counts-table">
        <tbody>
          {Object.entries(values).map(([name, value]) => (
            <tr key={name}>
              <th scope="row">{name}</th>
              <td className="mono location-text">{formatSetting(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function Listed({ title, list }: { title: string; list: ShortList<string> }) {
  if (list.items.length === 0) return null
  return (
    <div className="small">
      <strong>{title}</strong>
      <ul className="lookup-differences">
        {list.items.map((item) => (
          <li key={item}>{item}</li>
        ))}
        {list.more > 0 && <li className="muted">and {formatCount(list.more)} more</li>}
      </ul>
    </div>
  )
}

function Problems({ problems }: { problems: CoverageSection['problems'] | undefined }) {
  if (problems === undefined || problems.items.length === 0) return null
  return (
    <div className="banner banner-error small">
      Problems found:
      <ul className="lookup-differences">
        {problems.items.map((problem, index) => (
          <li key={`${index}|${problem.message}`}>
            {problem.run ? `${problem.run}: ` : ''}
            {problem.message}
          </li>
        ))}
        {problems.more > 0 && <li>and {formatCount(problems.more)} more</li>}
      </ul>
    </div>
  )
}

// What the step's coverage report says about this scanner, in plain words.
function CoverageBlock({ coverage }: { coverage: CoverageSection | null }) {
  if (coverage === null) {
    return <p className="muted small">No coverage report for this step yet, so nothing says whether the scan read every file.</p>
  }
  const { files, rows } = coverage
  return (
    <div className="step-coverage">
      <h4>
        Coverage <VerdictChip verdict={coverage.status === 'pass' ? 'pass' : (coverage.status ?? 'unknown')} />
      </h4>
      {coverage.reason && <p className="muted small">{coverage.reason}</p>}
      <p className="small">
        {files.read === null ? 'The scan recorded no list of the files it read' : `Read ${formatCount(files.read)} of ${formatCount(files.expected)} files`}
        {rows !== null && rows.read !== null && (
          <>
            ; {formatCount(rows.read)} rows ({formatCount(rows.with_text)} with text, {formatCount(rows.without_text)} without),{' '}
            {formatCount(rows.findings)} findings
          </>
        )}
        .
      </p>
      <Listed title="Files the scan never read" list={files.missing} />
      <Listed title="Files it read that no finished run wrote" list={files.extra} />
      <Listed title="Files that changed since the scan read them" list={files.different} />
      <Listed title="Files in work that didn't finish" list={files.not_finished} />
      <Problems problems={coverage.problems} />
      {coverage.parts.length > 0 && (
        <details>
          <summary className="small">Parts of the scan ({coverage.parts.length})</summary>
          <table className="counts-table">
            <tbody>
              {coverage.parts.map((part) => (
                <tr key={part.name}>
                  <th scope="row">{part.name}</th>
                  <td>{part.status}</td>
                  <td className="small">{part.reason ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      <p className="muted small">
        Report: <span className="mono location-text">{coverage.file}</span>
      </p>
    </div>
  )
}

function DedupResults({ details }: { details: DedupDetails }) {
  return (
    <>
      <KeyFigures
        rows={[
          ['Mode', details.mode ?? '—'],
          ['Input mode', details.input_mode ?? '—'],
          ['Files read', formatCount(details.files)],
          ['Rows read', formatCount(details.rows_total)],
          ['Rows with text (hashed)', formatCount(details.rows_hashed)],
          ['Rows without text', formatCount(details.rows_without_text)],
          ['Candidate pairs', formatCount(details.candidate_pairs)],
          ['Duplicate rows found', formatCount(details.duplicate_rows)],
        ]}
      />
      <p className="muted small">
        Rows without text can't be compared, so they are never duplicates: they show as "no text", not as clean.
      </p>
      <SettingsTable title="Settings" values={details.configuration} />
    </>
  )
}

function RegexPiiResults({ details }: { details: RegexPiiDetails }) {
  return (
    <>
      <KeyFigures rows={[['Stages finished', `${details.stages_finished} of ${details.stages}`]]} />
      <CountsTable
        title="Rows removed, by category"
        counts={details.removed_by_category}
        note="Regex PII keeps no summary of its own (its findings are one very large file). These are the rows the filter removed for each category, from the row status. A row with several categories counts once for each."
      />
      {details.removed_by_category === null && (
        <p className="muted small">The categories show once the filter and the row status are finished.</p>
      )}
    </>
  )
}

function NerResults({ details }: { details: NerDetails }) {
  return (
    <>
      <KeyFigures
        rows={[
          ['Pieces of work finished', `${formatCount(details.completed_units)} of ${formatCount(details.expected_units)}`],
          ['Rows read', formatCount(details.rows_read)],
          ['Rows with text', formatCount(details.rows_with_text)],
          ['Rows without text', formatCount(details.rows_without_text)],
          ['Messages scanned', formatCount(details.messages)],
          ['Spans found', formatCount(details.spans)],
          ['Spans the policy keeps', formatCount(details.policy_eligible_spans)],
          ['Candidates for the LLM check', formatCount(details.llm_candidates)],
        ]}
      />
      {details.missing_units !== null && details.missing_units !== undefined && details.missing_units > 0 && (
        <div className="banner banner-error small">{formatCount(details.missing_units)} pieces of work are missing from the results.</div>
      )}
      <CountsTable title="Spans by tier" counts={details.tiers} />
      <CountsTable title="Spans the policy left out, and why" counts={details.policy_exclusions} />
      {details.labels && (
        <details>
          <summary className="small">Spans by label ({Object.keys(details.labels).length})</summary>
          <CountsTable title="Spans by label" counts={details.labels} />
        </details>
      )}
    </>
  )
}

function ToxicityResults({ details }: { details: ToxicityDetails }) {
  if (details.shards === null) {
    return <p className="muted small">The scan's manifest isn't there yet, so there are no shards to read.</p>
  }
  return (
    <>
      <KeyFigures
        rows={[
          ['Shards finished', `${formatCount(details.shards_finished)} of ${formatCount(details.shards)}`],
          ['Shards with guard verdicts', formatCount(details.shards_judged)],
          ['Rows read', formatCount(details.rows_read)],
          ['Messages scored', formatCount(details.messages_scored)],
          ['Messages flagged by the first pass', formatCount(details.messages_flagged)],
          ['Messages judged by the guard model', formatCount(details.guard_records)],
          ['Guard errors', formatCount(details.guard_errors)],
          ['Unknown verdicts', formatCount(details.guard_unknown)],
        ]}
      />
      <CountsTable title="Guard verdicts" counts={details.verdicts} />
      <CountsTable title="Categories" counts={details.categories} />
    </>
  )
}

function CoverageStageResults({ details }: { details: CoverageStageDetails }) {
  if (details.report === null) return <p className="muted small">There is no coverage report for this step yet.</p>
  return (
    <>
      <p>
        Verdict: <VerdictChip verdict={details.verdict ?? null} />
        {details.created_at && <span className="muted small"> · made {formatTimestamp(details.created_at)}</span>}
      </p>
      <p className="muted small">
        Checked the runs under <span className="mono location-text">{details.root}</span>. Report:{' '}
        <span className="mono location-text">{details.report}</span>
      </p>
      {details.runs && (
        <KeyFigures
          rows={[
            ['Runs found', formatCount(details.runs.found)],
            ['Runs checked', formatCount(details.runs.checked)],
            ['Files', formatCount(details.runs.files)],
            ['Rows', formatCount(details.runs.rows)],
          ]}
        />
      )}
      <h4>Scanners</h4>
      <table className="counts-table">
        <tbody>
          {Object.entries(details.scanner_status ?? {}).map(([name, status]) => (
            <tr key={name}>
              <th scope="row">{name}</th>
              <td className={status === 'pass' ? 'run-status-passed' : status === 'not_run' ? 'muted' : 'run-status-failed'}>
                {status === 'not_run' ? 'not part of this step' : status}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Problems problems={details.problems} />
    </>
  )
}

function FilterResults({ details }: { details: FilterStageDetails }) {
  const counts = details.row_status_counts
  return (
    <>
      <p className="muted small">
        Filtered <span className="mono location-text">{details.input_root}</span> into{' '}
        <span className="mono location-text">{details.output_root}</span>.
      </p>
      <KeyFigures
        rows={[
          ['Runs', formatCount(details.runs)],
          ['Rows read', formatCount(details.rows_read)],
          ['Rows kept', `${formatCount(details.kept)} (${formatPercent(details.kept, details.rows_read)})`],
          ['Rows removed', formatCount(details.removed)],
        ]}
      />
      <CountsTable
        title="Why rows were removed"
        counts={details.removed_by_reason}
        note="A row can be removed for more than one reason, so these can add up to more than the rows removed."
      />
      {details.failed_runs.items.length > 0 && (
        <div className="banner banner-error small">
          Runs whose own checks failed:
          <ul className="lookup-differences">
            {details.failed_runs.items.map((item) => (
              <li key={item.run}>
                {item.run}: {item.failures.join('; ')}
              </li>
            ))}
            {details.failed_runs.more > 0 && <li>and {formatCount(details.failed_runs.more)} more</li>}
          </ul>
        </div>
      )}
      {details.rules_differ && (
        <div className="banner banner-warning small">Not every run of this step used the same rules. The first run's are shown.</div>
      )}
      <SettingsTable title="Rules" values={details.rules} />
      {details.scanner_results && (
        <>
          <h4>Scanner results used</h4>
          <table className="counts-table">
            <tbody>
              {Object.entries(details.scanner_results).map(([name, info]) => (
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
        </>
      )}
      {counts !== null && (
        <details>
          <summary className="small">Removed rows by what was found (from the row status)</summary>
          <CountsTable title="NER label" counts={counts.by_ner_label} note="A row with several labels counts once for each." />
          <CountsTable title="Regex PII category" counts={counts.by_regex_category} />
          <CountsTable title="Toxicity verdict" counts={counts.by_toxicity_verdict} />
        </details>
      )}
    </>
  )
}

function ChecksNote({ checks }: { checks: { passed: boolean; failures: string[] } | null | undefined }) {
  if (!checks) return null
  if (checks.passed) return <p className="small">All of its own checks passed.</p>
  return (
    <div className="banner banner-error small">
      Its own checks failed:
      <ul className="lookup-differences">
        {checks.failures.map((failure) => (
          <li key={failure}>{failure}</li>
        ))}
      </ul>
    </div>
  )
}

function TraceResults({ details }: { details: TraceStageDetails }) {
  if (details.summary === null || details.totals === undefined || details.totals === null) {
    return <p className="muted small">The trace hasn't finished, so it has no summary yet.</p>
  }
  const { totals } = details
  return (
    <>
      <p>
        Verdict: <VerdictChip verdict={details.verdict ?? null} />
        {details.created_at && <span className="muted small"> · made {formatTimestamp(details.created_at)}</span>}
      </p>
      <KeyFigures
        rows={[
          ['Findings followed back to their row', formatCount(totals.findings)],
          ['Distinct lines walked', formatCount(totals.distinct_lines_walked)],
          ['Prepared rows looked up', formatCount(totals.prepared_rows_looked_up)],
        ]}
      />
      <CountsTable title="Findings by status" counts={totals.by_status} note="unchanged means the finding still points at the same row it was found in." />
      {details.sources && (
        <>
          <h4>By source</h4>
          <table className="counts-table">
            <thead>
              <tr>
                <th>Source</th>
                <th className="number">Findings</th>
                <th>Not unchanged</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(details.sources).map(([name, source]) => {
                const others = Object.entries(source.by_status ?? {}).filter(([status, count]) => status !== 'unchanged' && count > 0)
                return (
                  <tr key={name}>
                    <th scope="row">{name}</th>
                    <td className="number">{formatCount(source.findings)}</td>
                    <td className="small">{others.length === 0 ? 'none' : others.map(([status, count]) => `${status} ${formatCount(count)}`).join(', ')}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </>
      )}
      <ChecksNote checks={details.checks} />
    </>
  )
}

function RowStatusResults({ details }: { details: RowStatusStageDetails }) {
  if (details.summary === null || details.totals === undefined || details.totals === null) {
    return <p className="muted small">The row status hasn't finished, so it has no summary yet.</p>
  }
  const { totals } = details
  const byStep = Object.entries(totals.removed_by_step_and_reason ?? {})
  return (
    <>
      <p>
        Verdict: <VerdictChip verdict={details.verdict ?? null} />
        {details.created_at && <span className="muted small"> · made {formatTimestamp(details.created_at)}</span>}
        {details.report_version !== null && details.report_version !== undefined && (
          <span className="muted small"> · report version {details.report_version}</span>
        )}
      </p>
      <KeyFigures
        rows={[
          ['Prepared rows with a status', formatCount(totals.prepared_rows)],
          ['Kept (clean)', formatCount(totals.kept)],
          ['Removed', formatCount(totals.removed)],
          ['Rows with no origin note', formatCount(totals.rows_without_origin)],
        ]}
      />
      {byStep.map(([step, reasons]) => (
        <CountsTable key={step} title={`Removed at step ${step}, by reason`} counts={reasons} />
      ))}
      <ChecksNote checks={details.checks} />
    </>
  )
}

function Results({ response }: { response: ChainStepResponse }) {
  const { stage } = response
  const details = response.details
  if (details === null || details === undefined) return null
  switch (stage.key) {
    case 'dedup':
      return <DedupResults details={details as DedupDetails} />
    case 'regex_pii':
      return <RegexPiiResults details={details as RegexPiiDetails} />
    case 'ner':
      return <NerResults details={details as NerDetails} />
    case 'toxicity':
      return <ToxicityResults details={details as ToxicityDetails} />
    case 'trace':
      return <TraceResults details={details as TraceStageDetails} />
    case 'row_status':
      return <RowStatusResults details={details as RowStatusStageDetails} />
    default:
      return stage.kind === 'coverage' ? (
        <CoverageStageResults details={details as CoverageStageDetails} />
      ) : (
        <FilterResults details={details as FilterStageDetails} />
      )
  }
}

export default function StepResults({ chainDir, stage }: { chainDir: string; stage: string }) {
  const { data, error } = useRefreshing(() => getChainStep(chainDir, stage), `${chainDir}|${stage}`, 0, () => false)

  return (
    <div className="step-results">
      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data && (
        <>
          <h3>
            {data.stage.label} <StateChip state={data.stage.state} />
          </h3>
          {data.stage.path && (
            <p className="muted small">
              <span className="mono location-text">{data.stage.path}</span>
              {describeProgress(data.stage.progress) !== null && <> · {describeProgress(data.stage.progress)}</>}
            </p>
          )}
          {data.stage.state === 'not_started' && <p className="muted">This stage hasn't started, so it has no results yet.</p>}
          {data.stage.state === 'unfinished' && (
            <p className="muted small">Not finished yet: what is shown is what its files hold so far.</p>
          )}
          {data.stage.state !== 'not_started' && (
            <>
              <Results response={data} />
              {data.stage.kind === 'scan' && <CoverageBlock coverage={data.coverage} />}
            </>
          )}
        </>
      )}
    </div>
  )
}
