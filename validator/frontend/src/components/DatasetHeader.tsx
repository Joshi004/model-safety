import type { FoundEntry } from '../catalog'

// Sits at the top of the main panel, above "What's here" -- the one
// place a picked dataset's name, path, description, and medpsy/Tools-team
// screening note show now that they're no longer on the sidebar row (see
// SourcePicker.tsx). `found` is null for a hand-typed "Custom path" that
// doesn't exactly match a catalog entry -- that gets a plain header with
// no breadcrumb and no Verified checkbox, since there's nothing in the
// catalog to mark.
//
// Only the top bar (breadcrumb + Verified) is sticky -- see App.css's
// `.dataset-header-bar` -- so the dataset's name and its checkbox stay
// visible while a long row's content scrolls underneath. The path,
// description, and screening note scroll normally, same as everything
// else in the main panel.
export default function DatasetHeader({
  path,
  found,
  verified,
  onToggleVerified,
}: {
  path: string
  found: FoundEntry | null
  verified: boolean
  onToggleVerified: (path: string, verified: boolean) => void
}) {
  return (
    <div className="dataset-header">
      <div className="dataset-header-bar">
        <div className="dataset-header-breadcrumb">
          {found ? (
            <>
              <span className="muted small">{found.team.team}</span>
              <span className="muted small"> / </span>
              <span className="muted small">{found.group.name}</span>
              <span className="muted small"> / </span>
              <strong>{found.entry.label}</strong>
            </>
          ) : (
            <strong>Custom path</strong>
          )}
        </div>
        {found && (
          <label className="inline dataset-header-verified">
            <input
              type="checkbox"
              checked={verified}
              disabled={!found.entry.available}
              onChange={(event) => onToggleVerified(found.entry.path, event.target.checked)}
            />
            Verified
          </label>
        )}
      </div>

      <p className="mono small dataset-header-path">{path}</p>

      {found && found.entry.description && <p className="muted small">{found.entry.description}</p>}

      {found && !found.entry.available && (
        <p className="error small">Unavailable: {found.entry.unavailable_reason}</p>
      )}
      {found && found.entry.available && found.entry.screening && (
        <p className="dataset-header-screening small">{found.entry.screening}</p>
      )}

      {found && found.group.description && (
        <details className="dataset-header-group-note">
          <summary className="muted small">About the "{found.group.name}" group</summary>
          <p className="muted small">{found.group.description}</p>
        </details>
      )}
    </div>
  )
}
