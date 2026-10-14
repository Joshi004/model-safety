import Menu from './Menu'

const NOT_YET = 'Not available yet'

// Shown while at least one dataset is ticked. The scans can't be started yet, so they are
// disabled with a short note. "More -> Convert only" converts the ticked datasets.
export default function ActionBar({
  count,
  onClear,
  onConvertOnly,
}: {
  count: number
  onClear: () => void
  onConvertOnly: () => void
}) {
  return (
    <div className="action-bar" role="region" aria-label="Actions for the selected datasets">
      <div className="action-bar-main">
        <strong>{count} selected</strong>
        <button type="button" className="link-button" onClick={onClear}>
          Clear
        </button>
        <div className="action-buttons">
          <button type="button" className="secondary" disabled title={NOT_YET}>
            Find duplicates
          </button>
          <button type="button" className="secondary" disabled title={NOT_YET}>
            Scan for PII
          </button>
          <button type="button" className="secondary" disabled title={NOT_YET}>
            Scan for toxicity
          </button>
          <Menu label="More ▾" ariaLabel="More actions" align="right">
            <button type="button" role="menuitem" className="menu-item" onClick={onConvertOnly}>
              Convert only…
            </button>
          </Menu>
        </div>
      </div>
      <p className="muted small">Scans can't be started from here yet.</p>
    </div>
  )
}
