import { useState } from 'react'
import { countGroup, countTeam } from '../catalog'
import type { SourceEntry, SourceGroupInfo, TeamCatalog } from '../types'
import { loadJSON, loadString, saveJSON, saveString } from '../storage'
import ProgressBar from './ProgressBar'

// Lives inside Sidebar.tsx, above the "Custom path" box. One tab per
// team; each group is a native <details> (defaults open, so every entry
// is visible right away -- the user can still collapse a long one like
// the 61-component SFT mix). Each row is a single line (a Verified
// checkbox plus the dataset's name) -- the description and screening note
// for whichever one is actually picked show in DatasetHeader instead, on
// the right, so this list stays scannable even at ~100 entries.
//
// Picking a row calls `onPick` directly -- unlike the old "Try an
// example" row this came from (PathForm.tsx still has its own, separate
// one), it no longer fills a path text box first; there's nothing left
// in the sidebar for it to fill.
//
// The selected team and each group's open/closed state are persisted to
// localStorage (see storage.ts) -- client-only, never sent to the
// backend -- keyed by team/group name, so they survive a reload and
// degrade gracefully (fall back to today's defaults) if the catalog
// changes. "Verified" (renamed from the old "Reviewed") is lifted up to
// App.tsx now, since the same mark is also shown and toggled from
// DatasetHeader -- see this component's `verified`/`onToggleVerified`
// props.
export default function SourcePicker({
  teams,
  loading,
  verified,
  onToggleVerified,
  activePath,
  onPick,
}: {
  teams: TeamCatalog[]
  loading: boolean
  verified: Record<string, true>
  onToggleVerified: (path: string, verified: boolean) => void
  activePath: string | null
  onPick: (path: string) => void
}) {
  const [selectedTeamName, setSelectedTeamName] = useState(() =>
    loadString('sourcePicker.selectedTeam', ''),
  )
  const [groupOpen, setGroupOpen] = useState(() =>
    loadJSON<Record<string, boolean>>('sourcePicker.groupOpen', {}),
  )

  if (teams.length === 0) return null

  const teamIndex = teams.findIndex((candidate) => candidate.team === selectedTeamName)
  const team = teams[teamIndex === -1 ? 0 : teamIndex]

  function pickTeam(name: string) {
    setSelectedTeamName(name)
    saveString('sourcePicker.selectedTeam', name)
  }

  function setGroupOpenState(name: string, open: boolean) {
    const next = { ...groupOpen, [name]: open }
    setGroupOpen(next)
    saveJSON('sourcePicker.groupOpen', next)
  }

  return (
    <div className="source-picker">
      <p className="muted small">
        What each team actually trains on -- not every file in their data folders.
      </p>
      <div className="row-tabs">
        {teams.map((candidate) => (
          <button
            key={candidate.team}
            type="button"
            className={candidate.team === team.team ? 'row-tab selected' : 'row-tab'}
            onClick={() => pickTeam(candidate.team)}
          >
            <span className="row-tab-content">
              {candidate.team}
              <ProgressBar progress={countTeam(candidate, verified)} variant="compact" />
            </span>
          </button>
        ))}
      </div>

      {team.groups.map((group) => (
        <SourceGroupSection
          key={group.name}
          group={group}
          loading={loading}
          onPick={onPick}
          open={groupOpen[group.name] ?? true}
          onToggleOpen={(open) => setGroupOpenState(group.name, open)}
          verified={verified}
          onToggleVerified={onToggleVerified}
          activePath={activePath}
        />
      ))}
    </div>
  )
}

function SourceGroupSection({
  group,
  loading,
  onPick,
  open,
  onToggleOpen,
  verified,
  onToggleVerified,
  activePath,
}: {
  group: SourceGroupInfo
  loading: boolean
  onPick: (path: string) => void
  open: boolean
  onToggleOpen: (open: boolean) => void
  verified: Record<string, true>
  onToggleVerified: (path: string, verified: boolean) => void
  activePath: string | null
}) {
  return (
    <details className="source-group" open={open} onToggle={(event) => onToggleOpen(event.currentTarget.open)}>
      <summary>
        <span className="source-group-summary-row">
          <span>{group.name}</span>
          <ProgressBar progress={countGroup(group, verified)} variant="compact" />
        </span>
      </summary>
      {group.note && <div className="banner banner-warning small">{group.note}</div>}
      <div className="source-entries">
        {group.entries.map((entry) => (
          <SourceEntryRow
            key={entry.path}
            entry={entry}
            loading={loading}
            active={entry.path === activePath}
            verified={!!verified[entry.path]}
            onPick={onPick}
            onToggleVerified={onToggleVerified}
          />
        ))}
      </div>
    </details>
  )
}

function SourceEntryRow({
  entry,
  loading,
  active,
  verified,
  onPick,
  onToggleVerified,
}: {
  entry: SourceEntry
  loading: boolean
  active: boolean
  verified: boolean
  onPick: (path: string) => void
  onToggleVerified: (path: string, verified: boolean) => void
}) {
  const classNames = ['source-entry-row']
  if (active) classNames.push('active')
  if (verified) classNames.push('verified')
  if (!entry.available) classNames.push('unavailable')

  return (
    <div className={classNames.join(' ')}>
      <input
        type="checkbox"
        className="source-entry-checkbox"
        checked={verified}
        disabled={!entry.available}
        title="Verified"
        onChange={(event) => onToggleVerified(entry.path, event.target.checked)}
      />
      <button
        type="button"
        className="source-entry-pick"
        disabled={loading || !entry.available}
        title={entry.available ? entry.path : `Unavailable: ${entry.unavailable_reason}`}
        onClick={() => onPick(entry.path)}
      >
        {entry.label}
      </button>
    </div>
  )
}
