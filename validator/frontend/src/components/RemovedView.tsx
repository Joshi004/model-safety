import { useState } from 'react'
import { getRemovedOptions, getRemovedPage, listChains } from '../api'
import { formatCount } from '../format'
import { basename } from '../origin'
import { reasonLabel } from '../rows'
import type {
  Counts,
  RemovedEntry,
  RemovedFilters,
  RemovedOptionsResponse,
  RemovedPageResponse,
  RowTarget,
} from '../types'
import { useRefreshing } from '../useRefreshing'

// The "Removed" view: browse the rows a filter step took out of one dataset, by
// reason, NER label, regex category or toxicity verdict, a page at a time. It
// pages the removed-rows index in the chain's row status, so a page never reads the
// filter run's own (very large) list of removed rows. Each row opens on the Rows
// page. Reading never changes anything.
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

const PAGE_SIZE = 50

// "email ×2, phone_number": the names in a count dictionary, with their counts when above 1.
function describeCounts(counts: Counts): string {
  return Object.entries(counts)
    .map(([name, count]) => (count > 1 ? `${name} ×${count}` : name))
    .join(', ')
}

function describeFound(entry: RemovedEntry): string {
  const parts = [
    describeCounts(entry.ner_labels),
    describeCounts(entry.regex_categories),
    describeCounts(entry.toxicity_verdicts),
  ].filter((part) => part !== '')
  return parts.length === 0 ? '—' : parts.join('; ')
}

function describeOriginal(entry: RemovedEntry): string {
  const original = entry.original
  if (original === null) return '—'
  return `${basename(original.path)}, ${original.line !== undefined ? `line ${original.line}` : `row ${original.row}`}`
}

// A drop-down of the names in `counts`, biggest first, each with its count.
function CountSelect({
  id,
  label,
  counts,
  value,
  onChange,
}: {
  id: string
  label: string
  counts: Counts
  value: string
  onChange: (value: string) => void
}) {
  const names = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  return (
    <div className="start-field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)} disabled={names.length === 0}>
        <option value="">{names.length === 0 ? 'none' : 'Any'}</option>
        {names.map(([name, count]) => (
          <option key={name} value={name}>
            {id === 'removed-reason' ? reasonLabel(name) : name} ({formatCount(count)})
          </option>
        ))}
      </select>
    </div>
  )
}

function Browser({
  chainDir,
  options,
  initial,
  onOpenRow,
  onOpenRun,
}: {
  chainDir: string
  options: RemovedOptionsResponse
  initial: RemovedFilters | null
  onOpenRow: (target: RowTarget) => void
  onOpenRun: (runDir: string) => void
}) {
  const lastStep = options.steps[options.steps.length - 1] ?? 1
  const [chosenStep, setChosenStep] = useState<number | null>(initial?.step ?? null)
  const step = chosenStep ?? lastStep
  const [chosenRun, setChosenRun] = useState(initial?.run ?? '')
  // Without a choice, the first dataset that had rows removed at this step.
  const run =
    chosenRun !== ''
      ? chosenRun
      : (options.runs.find((item) => (item.steps[String(step)]?.entries ?? 0) > 0)?.run ?? options.runs[0]?.run ?? '')
  const [reason, setReason] = useState('')
  const [nerLabel, setNerLabel] = useState('')
  const [regexCategory, setRegexCategory] = useState('')
  const [toxicityVerdict, setToxicityVerdict] = useState('')
  const [page, setPage] = useState(1)

  const counts = options.runs.find((item) => item.run === run)?.steps[String(step)]
  const requestKey = JSON.stringify([chainDir, run, step, reason, nerLabel, regexCategory, toxicityVerdict, page])
  const { data, error } = useRefreshing<RemovedPageResponse | null>(
    () =>
      run === ''
        ? Promise.resolve(null)
        : getRemovedPage({
            chain_dir: chainDir,
            run,
            step,
            reason: reason || null,
            ner_label: nerLabel || null,
            regex_category: regexCategory || null,
            toxicity_verdict: toxicityVerdict || null,
            page,
            page_size: PAGE_SIZE,
          }),
    requestKey,
    0,
    () => false,
  )

  function clearFilters() {
    setReason('')
    setNerLabel('')
    setRegexCategory('')
    setToxicityVerdict('')
    setPage(1)
  }

  // A filter changes the rows that match, so the list starts again from its first page.
  function changed(setter: (value: string) => void) {
    return (value: string) => {
      setter(value)
      setPage(1)
    }
  }

  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1
  // The page on screen is for an older request until the new one has arrived.
  const stale = data !== null && (data.run !== run || data.step !== step || data.page !== page)

  return (
    <>
      <div className="row-finder">
        <div className="start-resources">
          <div className="start-field">
            <label htmlFor="removed-run">Dataset</label>
            <select
              id="removed-run"
              value={run}
              onChange={(event) => {
                setChosenRun(event.target.value)
                clearFilters()
              }}
            >
              {options.runs.map((item) => (
                <option key={item.run} value={item.run}>
                  {item.run} ({formatCount(item.steps[String(step)]?.entries ?? 0)} removed)
                </option>
              ))}
            </select>
          </div>
          <div className="start-field">
            <label htmlFor="removed-step">Step</label>
            <select
              id="removed-step"
              value={step}
              onChange={(event) => {
                setChosenStep(Number(event.target.value))
                clearFilters()
              }}
            >
              {options.steps.map((number) => (
                <option key={number} value={number}>
                  Step {number}
                </option>
              ))}
            </select>
          </div>
          <CountSelect id="removed-reason" label="Reason" counts={counts?.by_reason ?? {}} value={reason} onChange={changed(setReason)} />
          <CountSelect id="removed-ner" label="NER label" counts={counts?.by_ner_label ?? {}} value={nerLabel} onChange={changed(setNerLabel)} />
          <CountSelect
            id="removed-regex"
            label="Regex PII category"
            counts={counts?.by_regex_category ?? {}}
            value={regexCategory}
            onChange={changed(setRegexCategory)}
          />
          <CountSelect
            id="removed-toxicity"
            label="Toxicity verdict"
            counts={counts?.by_toxicity_verdict ?? {}}
            value={toxicityVerdict}
            onChange={changed(setToxicityVerdict)}
          />
        </div>
        <p className="muted small">
          A row counts once for each name it has, so the counts can add up to more than the rows removed. Choosing several
          filters keeps the rows that match all of them.
          {options.clean_root !== null && run !== '' && (
            <>
              {' '}
              <button type="button" className="link-button" onClick={() => onOpenRun(`${options.clean_root}/${run}`)}>
                Browse this dataset's clean rows
              </button>
            </>
          )}
        </p>
      </div>

      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data && (
        <>
          <div className="runs-list-header">
            <p>
              <strong>{formatCount(data.total)}</strong> row{data.total === 1 ? '' : 's'} · page {formatCount(page)} of {formatCount(pages)}
            </p>
            <div className="run-pager">
              <button type="button" className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                Previous
              </button>
              <button type="button" className="secondary" disabled={!data.has_next} onClick={() => setPage(page + 1)}>
                Next
              </button>
            </div>
          </div>
          {data.total === 0 && <p className="muted">No rows match. This step removed none of this dataset's rows that fit these filters.</p>}
          {data.entries.length > 0 && (
            <div className={stale ? 'skipped-table-wrap loading' : 'skipped-table-wrap'}>
              <table className="skipped-table">
                <thead>
                  <tr>
                    <th>Prepared line</th>
                    <th>Reason</th>
                    <th>What was found</th>
                    <th>Original</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.entries.map((entry) => {
                    const open = () =>
                      onOpenRow({ chainDir, run: entry.run, file: entry.file, line: entry.line, from: 'prepared' })
                    return (
                      <tr key={`${entry.file}|${entry.line}`} onClick={open}>
                        <td title={entry.file}>
                          {/* A click on the button reaches the row's own onClick. */}
                          <button type="button" className="link-button mono">
                            {basename(entry.file)}:{entry.line}
                          </button>
                        </td>
                        <td>{entry.reasons.map(reasonLabel).join(', ')}</td>
                        <td className="small">{describeFound(entry)}</td>
                        <td className="small" title={entry.original?.path}>
                          {describeOriginal(entry)}
                        </td>
                        <td>
                          <button type="button" className="link-button">
                            Open
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  )
}

export default function RemovedView({
  initial,
  onOpenRow,
  onOpenRun,
}: {
  initial: RemovedFilters | null
  onOpenRow: (target: RowTarget) => void
  onOpenRun: (runDir: string) => void
}) {
  const chainsState = useRefreshing(() => listChains(), 'chains-for-removed', 0, () => false)
  // Only a chain whose row status is finished has the removed-rows index.
  const chains = (chainsState.data?.chains ?? []).filter((chain) => chain.row_status?.finished === true)
  const [chosenChain, setChosenChain] = useState(initial?.chainDir ?? '')
  const chainDir = chosenChain || chains[0]?.chain_dir || ''

  const optionsState = useRefreshing<RemovedOptionsResponse | null>(
    () => (chainDir ? getRemovedOptions(chainDir) : Promise.resolve(null)),
    chainDir,
    0,
    () => false,
  )
  // The options of the chain before this one stay in `data` until the new ones arrive.
  const options = optionsState.data?.chain_dir === chainDir ? optionsState.data : null

  return (
    <div className="removed-view">
      <h3>Removed rows</h3>
      {chainsState.error && <p className="error">{chainsState.error}</p>}
      {chainsState.data === null && chainsState.error === null && <p className="muted">Loading…</p>}
      {chainsState.data && chains.length === 0 && (
        <p className="muted">No chain has a finished row status yet, so there are no removed rows to browse.</p>
      )}
      {chains.length > 0 && (
        <div className="start-field">
          <label htmlFor="removed-chain">Chain</label>
          <select id="removed-chain" value={chainDir} onChange={(event) => setChosenChain(event.target.value)}>
            {chains.map((chain) => (
              <option key={chain.chain_dir} value={chain.chain_dir}>
                {chain.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {optionsState.error && <p className="error">{optionsState.error}</p>}
      {options && !options.has_removed_index && (
        <div className="banner banner-warning small">
          This chain's row status has no removed-rows index (it was built before the index existed). Run{' '}
          <code>row_status.py</code> again, into the same folder, to add it.
        </div>
      )}
      {options && options.has_removed_index && (
        // A different chain, or a link to a dataset, starts the filters again.
        <Browser
          key={`${chainDir}|${initial?.run ?? ''}|${initial?.step ?? ''}`}
          chainDir={chainDir}
          options={options}
          initial={initial !== null && initial.chainDir === chainDir ? initial : null}
          onOpenRow={onOpenRow}
          onOpenRun={onOpenRun}
        />
      )}
    </div>
  )
}
