import Menu from './Menu'

const NOT_YET = 'Comes in a later update'

// Shown while at least one dataset is ticked. "Find duplicates" opens the scan dialog; the
// other scans are disabled until they can be started. "More -> Convert only" converts the
// ticked datasets.
export default function ActionBar({
  count,
  onClear,
  onScan,
  onConvertOnly,
}: {
  count: number
  onClear: () => void
  // Opens the scan dialog for one scanner, by its key.
  onScan: (scannerKey: string) => void
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
          <button type="button" className="secondary" onClick={() => onScan('dedup')}>
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
    </div>
  )
}
