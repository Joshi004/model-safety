import { hrefFor } from '../route'
import { chipLabel, chipTitle, chipTone, scannerLabel } from '../scans'
import type { ScanSummary } from '../types'
import Chip from './Chip'

// One chip for each scanner on a dataset: "Duplicates: Done (12)". A finished scan's chip opens
// its result page. The Datasets table and a dataset's header both show these, from the same
// data (`dataset.scans`).
export default function ScannerChips({
  datasetId,
  scans,
}: {
  datasetId: string
  scans: Record<string, ScanSummary> | undefined
}) {
  return (
    <>
      {Object.entries(scans ?? {}).map(([scannerKey, summary]) => {
        const chip = (
          <Chip
            label={`${scannerLabel(scannerKey)}: ${chipLabel(summary)}`}
            tone={chipTone(summary)}
            title={chipTitle(scannerKey, summary)}
          />
        )
        return summary.state === 'succeeded' ? (
          <a
            key={scannerKey}
            className="chip-link"
            href={hrefFor({ page: 'dataset', id: datasetId, tab: 'results', scanner: scannerKey })}
          >
            {chip}
          </a>
        ) : (
          <span key={scannerKey}>{chip}</span>
        )
      })}
    </>
  )
}
