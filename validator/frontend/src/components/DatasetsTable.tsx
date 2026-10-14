import { useEffect, useRef } from 'react'
import { formatCount } from '../format'
import { hrefFor } from '../route'
import { STATE_TONES, chipLabel, chipTitle, scannerLabel } from '../scans'
import type { Dataset } from '../types'
import Chip from './Chip'
import Menu from './Menu'

// Every value here comes from the registry and is rendered as plain text.
export default function DatasetsTable({
  datasets,
  selected,
  onToggle,
  onToggleAll,
  onUnregister,
}: {
  datasets: Dataset[]
  selected: Set<string>
  onToggle: (id: string, checked: boolean) => void
  // Ticks or clears every dataset shown (not the ones hidden by the team tab or the search).
  onToggleAll: (checked: boolean) => void
  onUnregister: (dataset: Dataset) => void
}) {
  const selectedHere = datasets.filter((dataset) => selected.has(dataset.id)).length
  const allSelected = datasets.length > 0 && selectedHere === datasets.length
  const someSelected = selectedHere > 0 && !allSelected

  const headerCheckbox = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (headerCheckbox.current !== null) headerCheckbox.current.indeterminate = someSelected
  }, [someSelected])

  return (
    <table className="datasets-table">
      <thead>
        <tr>
          <th className="check-cell">
            <input
              ref={headerCheckbox}
              type="checkbox"
              checked={allSelected}
              onChange={(event) => onToggleAll(event.target.checked)}
              aria-label="Select all datasets shown"
            />
          </th>
          <th>Name</th>
          <th>Team</th>
          <th>Format</th>
          <th>Rows</th>
          <th>Availability</th>
          <th>Scans</th>
          <th className="menu-cell">
            <span className="visually-hidden">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {datasets.map((dataset) => (
          <tr key={dataset.id} className={selected.has(dataset.id) ? 'selected' : undefined}>
            <td className="check-cell">
              <input
                type="checkbox"
                checked={selected.has(dataset.id)}
                onChange={(event) => onToggle(dataset.id, event.target.checked)}
                aria-label={`Select ${dataset.name}`}
              />
            </td>
            <td className="name-cell">
              <a href={hrefFor({ page: 'dataset', id: dataset.id })}>{dataset.name}</a>
              <div className="path-line muted small" title={dataset.path}>
                {dataset.path}
              </div>
            </td>
            <td>{dataset.team}</td>
            <td>{dataset.conversion !== null ? dataset.conversion.input_type : '—'}</td>
            <td>
              {dataset.prepared !== null ? (
                formatCount(dataset.prepared.rows)
              ) : (
                <span className="muted">Not converted</span>
              )}
            </td>
            <td>
              {dataset.available ? (
                <Chip label="Available" tone="ok" />
              ) : (
                <Chip label="Unavailable" tone="error" title={dataset.unavailable_reason ?? undefined} />
              )}
            </td>
            <td className="scans-cell">
              {Object.entries(dataset.scans ?? {}).map(([scannerKey, summary]) => (
                <Chip
                  key={scannerKey}
                  label={`${scannerLabel(scannerKey)}: ${chipLabel(summary)}`}
                  tone={STATE_TONES[summary.state]}
                  title={chipTitle(scannerKey, summary)}
                />
              ))}
            </td>
            <td className="menu-cell">
              <Menu label="⋯" ariaLabel={`Actions for ${dataset.name}`} align="right">
                <a role="menuitem" className="menu-item" href={hrefFor({ page: 'dataset', id: dataset.id })}>
                  Open
                </a>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item menu-item-danger"
                  onClick={() => onUnregister(dataset)}
                >
                  Unregister…
                </button>
              </Menu>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
