import { describeSkipReason } from '../origin'
import type { SkippedEntry } from '../types'
import { OriginalLookup } from './OriginalLookup'
import RecordView from './RecordView'

// One skipped row, opened: why it was skipped, and the copy saved when it was skipped
// next to the original as it is now.
export default function SkippedEntryDetails({ entry, runDir }: { entry: SkippedEntry; runDir: string }) {
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
          <OriginalLookup origin={entry._origin} runDir={runDir} />
        </div>
      </div>
    </div>
  )
}
