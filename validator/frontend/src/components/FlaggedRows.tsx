import { useEffect, useState } from 'react'
import { errorMessage, getFlaggedPage } from '../api'
import { isChosen } from '../drop'
import type { ScannerSelection } from '../drop'
import { describeFound, findingName, nameFilterLabel, reasonLabel } from '../flagged'
import { formatCount } from '../format'
import { basename } from '../origin'
import type { DropRow, FlaggedOption, FlaggedPage } from '../types'
import FlaggedRowPanel from './FlaggedRowPanel'

// The flagged rows of one result, a page at a time, from the findings index (so a page never
// reads a result file). Filters narrow the list: by reason and by name (a NER label, a rule
// category, or the dataset a duplicate's kept copy is in) for most scanners, and for toxicity
// by the categories and verdicts chosen above this table. A row opens below the table.
//
// Rows can be ticked, or every row matching the filters chosen at once ("select all matching",
// not only the rows on this page). What is chosen belongs to the dataset page above, which keeps
// it while paging and while moving between result pages, and drops it from there.
//
// Safety: every value shown here comes from files on disk and renders as plain React text,
// never as markup.

const PAGE_SIZE = 50

function FilterSelect({
  id,
  label,
  options,
  value,
  optionLabel,
  onChange,
}: {
  id: string
  label: string
  options: FlaggedOption[]
  value: string
  optionLabel: (option: FlaggedOption) => string
  onChange: (value: string) => void
}) {
  return (
    <div className="start-field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Any</option>
        {options.map((option) => (
          <option key={option.name} value={option.name}>
            {optionLabel(option)} ({formatCount(option.count)})
          </option>
        ))}
      </select>
    </div>
  )
}

interface Outcome {
  key: string
  error: string | null
}

interface OpenRow {
  file: string
  line: number
}

export default function FlaggedRows({
  datasetId,
  scanner,
  categories,
  verdicts,
  chosen,
  onRows,
  onSelectAll,
  onClear,
}: {
  datasetId: string
  scanner: string
  // Toxicity's choice of what counts as flagged (null: the scan's own). Other scanners have none.
  categories: string[] | null
  verdicts: string[] | null
  // What is chosen from this result so far, or null.
  chosen: ScannerSelection | null
  onRows: (rows: DropRow[], checked: boolean) => void
  onSelectAll: (filters: { reason: string | null; name: string | null }, total: number) => void
  onClear: () => void
}) {
  const [reason, setReason] = useState('')
  const [name, setName] = useState('')
  const [page, setPage] = useState(1)
  const [open, setOpen] = useState<OpenRow | null>(null)

  // What was asked for, so an answer for an earlier request is never taken for this one. The
  // page on screen stays (dimmed) until the new one arrives.
  const requestKey = JSON.stringify([datasetId, scanner, page, reason, name, categories, verdicts])
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [shown, setShown] = useState<FlaggedPage | null>(null)

  useEffect(() => {
    let cancelled = false
    getFlaggedPage(datasetId, scanner, page, PAGE_SIZE, {
      reason: reason === '' ? null : reason,
      name: name === '' ? null : name,
      categories,
      verdicts,
    })
      .then((response) => {
        if (cancelled) return
        setShown(response)
        setOutcome({ key: requestKey, error: null })
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setShown(null)
        setOutcome({ key: requestKey, error: errorMessage(caught) })
      })
    return () => {
      cancelled = true
    }
    // `requestKey` says when what is asked for has really changed; `categories` and
    // `verdicts` are arrays that can be new objects with the same items.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey])

  const settled = outcome?.key === requestKey
  const loading = !settled
  const error = settled ? outcome.error : null
  const pages = shown === null ? 1 : Math.max(1, Math.ceil(shown.total / shown.page_size))

  // A filter changes the rows that match, so the list starts again from its first page, and
  // what was chosen from this result and the open row are forgotten.
  function changed(setter: (value: string) => void) {
    return (value: string) => {
      setter(value)
      setPage(1)
      onClear()
      setOpen(null)
    }
  }

  const entries = shown?.entries ?? []
  // With every matching row chosen, rows can't be ticked or unticked one by one.
  const everythingChosen = chosen?.all != null
  const allOnPage = entries.length > 0 && entries.every((entry) => isChosen(chosen, entry.file, entry.line))

  function togglePage(checked: boolean) {
    onRows(
      entries.map((entry) => ({ file: entry.file, line: entry.line })),
      checked,
    )
  }

  const reasonOptions = shown?.options.reasons ?? []
  const nameOptions = shown?.options.names ?? []

  return (
    <div className="card">
      <h3 className="scan-heading">Flagged rows</h3>

      {(reasonOptions.length > 1 || nameOptions.length > 1) && (
        <div className="start-resources flagged-filters">
          {reasonOptions.length > 1 && (
            <FilterSelect
              id="flagged-reason"
              label="Reason"
              options={reasonOptions}
              value={reason}
              optionLabel={(option) => reasonLabel(option.name)}
              onChange={changed(setReason)}
            />
          )}
          {nameOptions.length > 1 && (
            <FilterSelect
              id="flagged-name"
              label={nameFilterLabel(scanner)}
              options={nameOptions}
              value={name}
              optionLabel={(option) => (scanner === 'dedup' ? (option.label ?? option.name) : findingName(scanner, option.name))}
              onChange={changed(setName)}
            />
          )}
        </div>
      )}

      {error !== null && (
        <div className="banner banner-error" role="alert">
          Couldn't list the flagged rows: {error}
        </div>
      )}
      {shown === null && error === null && <p className="muted">Loading…</p>}

      {shown !== null && (
        <>
          <div className="flagged-bar">
            <p>
              <strong>{formatCount(shown.total)}</strong> row{shown.total === 1 ? '' : 's'} · page {formatCount(page)} of{' '}
              {formatCount(pages)}
            </p>
            <div className="run-pager">
              <button type="button" className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                Previous
              </button>
              <button type="button" className="secondary" disabled={!shown.has_next} onClick={() => setPage(page + 1)}>
                Next
              </button>
            </div>
          </div>

          {chosen !== null && (
            <p className="small">
              {chosen.all !== null
                ? `All ${formatCount(chosen.all.total)} matching rows are chosen.`
                : `${formatCount(chosen.rows.size)} chosen.`}{' '}
              <button type="button" className="link-button" onClick={onClear}>
                Clear
              </button>{' '}
              <span className="muted">
                They are kept while you page and while you look at the other results of this dataset.
              </span>
            </p>
          )}
          {shown.total > 0 && !everythingChosen && (
            <p className="small">
              <button
                type="button"
                className="link-button"
                onClick={() => onSelectAll({ reason: reason === '' ? null : reason, name: name === '' ? null : name }, shown.total)}
              >
                Choose all {formatCount(shown.total)} matching {shown.total === 1 ? 'row' : 'rows'}
              </button>{' '}
              <span className="muted">
                {shown.total > entries.length ? 'Not only the ones on this page. ' : ''}
                The server counts them again when you review the drop.
              </span>
            </p>
          )}

          {shown.total === 0 && <p className="muted">No rows match these filters.</p>}
          {entries.length > 0 && (
            <div className={loading ? 'skipped-table-wrap loading' : 'skipped-table-wrap'}>
              <table className="skipped-table">
                <thead>
                  <tr>
                    <th>
                      <input
                        type="checkbox"
                        checked={allOnPage}
                        disabled={everythingChosen}
                        onChange={(event) => togglePage(event.target.checked)}
                        aria-label="Select every row on this page"
                      />
                    </th>
                    <th>Prepared line</th>
                    <th>Text</th>
                    <th>What was found</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => {
                    const key = `${entry.line}|${entry.file}`
                    const openThis = () => setOpen({ file: entry.file, line: entry.line })
                    return (
                      <tr
                        key={key}
                        className={open?.file === entry.file && open.line === entry.line ? 'selected' : undefined}
                        onClick={openThis}
                      >
                        {/* A click on the checkbox mustn't also open the row. */}
                        <td onClick={(event) => event.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={isChosen(chosen, entry.file, entry.line)}
                            disabled={everythingChosen}
                            title={everythingChosen ? 'Every matching row is chosen. Clear the choice to pick rows one by one.' : undefined}
                            onChange={(event) =>
                              onRows([{ file: entry.file, line: entry.line }], event.target.checked)
                            }
                            aria-label={`Select ${entry.file} line ${entry.line}`}
                          />
                        </td>
                        <td title={entry.file} className="mono">
                          {basename(entry.file)}:{entry.line}
                        </td>
                        <td className="skipped-preview">{entry.preview}</td>
                        <td className="small">{describeFound(scanner, entry)}</td>
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

      {open !== null && (
        <FlaggedRowPanel
          datasetId={datasetId}
          scanner={scanner}
          file={open.file}
          line={open.line}
          categories={categories}
          verdicts={verdicts}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  )
}
