import { useState } from 'react'
import { countCatalog } from '../catalog'
import { loadJSON, saveJSON } from '../storage'
import type { TeamCatalog } from '../types'
import PathForm from './PathForm'
import ProgressBar from './ProgressBar'
import SourcePicker from './SourcePicker'

// The collapsible left-hand panel: overall Verified progress, the
// team/group/dataset picker (SourcePicker), and a "Custom path" box
// (PathForm) for anything not in the catalog. Always mounted (see
// App.tsx) so its own scroll position and open/closed state survive
// picking a different dataset -- only the main panel on the right
// changes.
//
// Collapsed state is persisted to localStorage (client-only, never sent
// to the backend -- see storage.ts) so it survives a reload. "Custom
// path" defaults open only while the catalog hasn't loaded (or failed to
// -- see sources.py/App.tsx's getSources() call): once real teams show
// up, it defaults closed instead and is left alone after that, so a
// user's own manual toggle always sticks.
//
// "Select datasets to convert" is the way into converting several datasets
// at once: while it's on, the picker's rows are selectable and the custom
// path box can add to the selection. The selection itself lives in App.tsx;
// turning the switch off only hides the checkboxes, it keeps the selection.
export default function Sidebar({
  teams,
  loading,
  verified,
  onToggleVerified,
  activePath,
  onPick,
  onInspect,
  selecting,
  onSelectingChange,
  selection,
  onSelect,
  onClearSelection,
  onReview,
}: {
  teams: TeamCatalog[]
  loading: boolean
  verified: Record<string, true>
  onToggleVerified: (path: string, verified: boolean) => void
  activePath: string | null
  onPick: (path: string) => void
  onInspect: (path: string) => void
  selecting: boolean
  onSelectingChange: (selecting: boolean) => void
  selection: string[]
  onSelect: (paths: string[], selected: boolean) => void
  onClearSelection: () => void
  onReview: () => void
}) {
  const [collapsed, setCollapsed] = useState(() => loadJSON('layout.sidebarCollapsed', false))

  function setCollapsedState(next: boolean) {
    setCollapsed(next)
    saveJSON('layout.sidebarCollapsed', next)
  }

  const hasCatalog = teams.length > 0
  const overall = countCatalog(teams, verified)

  if (collapsed) {
    return (
      <aside className="sidebar sidebar-collapsed">
        <button
          type="button"
          className="sidebar-toggle"
          onClick={() => setCollapsedState(false)}
          title="Expand the dataset sidebar"
        >
          »
        </button>
        {hasCatalog && (
          <div className="sidebar-collapsed-count muted small" title={`${overall.done} of ${overall.total} verified`}>
            {overall.done}/{overall.total}
          </div>
        )}
      </aside>
    )
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h3>Datasets</h3>
        <button
          type="button"
          className="sidebar-toggle"
          onClick={() => setCollapsedState(true)}
          title="Collapse the dataset sidebar"
        >
          «
        </button>
      </div>

      {hasCatalog && <ProgressBar progress={overall} label="Verified" />}

      <div className="sidebar-select">
        <label className="inline small">
          <input type="checkbox" checked={selecting} onChange={(event) => onSelectingChange(event.target.checked)} />
          Select datasets to convert
        </label>
        {selection.length > 0 && (
          <div className="sidebar-select-summary">
            <span className="small">
              {selection.length} selected
            </span>
            <button type="button" onClick={onReview}>
              Review {selection.length} dataset{selection.length === 1 ? '' : 's'}
            </button>
            <button type="button" className="secondary" onClick={onClearSelection}>
              Clear selection
            </button>
          </div>
        )}
      </div>

      {hasCatalog && (
        <SourcePicker
          teams={teams}
          loading={loading}
          verified={verified}
          onToggleVerified={onToggleVerified}
          activePath={activePath}
          onPick={onPick}
          selecting={selecting}
          selection={selection}
          onSelect={onSelect}
        />
      )}

      {/* See this file's own header comment for why `open` is left as a
          plain expression (no onToggle/persisted state): it only forces
          the DOM to match once, right when the catalog finishes loading
          (or finishes failing) -- a user's own click afterward always
          sticks, since the expression's value doesn't change again. */}
      <details className="sidebar-custom-path" open={!hasCatalog}>
        <summary>Custom path</summary>
        <PathForm
          onInspect={onInspect}
          loading={loading}
          onAddToSelection={selecting ? (path) => onSelect([path], true) : undefined}
        />
      </details>

      {hasCatalog && (
        <p className="muted small sidebar-footnote">
          Verified marks are saved in this browser only, for this exact address -- never sent to the backend.
        </p>
      )}
    </aside>
  )
}
