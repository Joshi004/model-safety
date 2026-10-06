import { describeSkipReason } from '../origin'
import type { Origin, SkippedEntry } from '../types'
import { OriginalLookup } from './OriginalLookup'
import RecordView from './RecordView'

// One skipped row, opened: why it was skipped, the copy saved when it was
// skipped next to the original as it is now, and a way into the preview.
export default function SkippedEntryDetails({
  entry,
  runDir,
  onOpenInPreview,
}: {
  entry: SkippedEntry
  runDir: string
  onOpenInPreview: (origin: Origin) => void
}) {
  const origin = entry._origin
  // A Hub dataset ID isn't a path the preview can open.
  const canOpen = origin.path.startsWith('/')

  return (
    <div className="skipped-details">
      <h3>Why it was skipped</h3>
      <p>{describeSkipReason(entry)}</p>
      <div className="columns">
        <div className="column">
          <h4>Saved copy</h4>
          <p className="muted small">The row as it was read, saved when it was skipped.</p>
          <RecordView record={entry.row} written={undefined} showScannerCoverage={false} />
        </div>
        <div className="column">
          <h4>Original now</h4>
          <OriginalLookup origin={origin} runDir={runDir} />
        </div>
      </div>
      <button
        type="button"
        className="secondary"
        disabled={!canOpen}
        title={canOpen ? undefined : "The preview can't open a Hub dataset."}
        onClick={() => onOpenInPreview(origin)}
      >
        Open this row in the preview
      </button>
    </div>
  )
}
