import { isAnalysisGoing } from '../scans'
import type { AnalysisState, CoverageVerdict } from '../types'
import Chip from './Chip'

// Whether a finished scan read exactly its dataset's converted files and every row. `verdict`
// is null until the check has been made, and `state` (the state of that check) then says why.
export default function CoverageBadge({
  verdict,
  state,
}: {
  verdict: CoverageVerdict | null
  state: AnalysisState
}) {
  if (verdict === 'pass') {
    return (
      <Chip
        label="Coverage OK"
        tone="ok"
        title="The scan read exactly this dataset's converted files, and every row of them."
      />
    )
  }
  if (verdict === 'fail') {
    return (
      <Chip
        label="Coverage failed"
        tone="error"
        title="The scan may not have read exactly this dataset's converted data. The problems are listed on the result page."
      />
    )
  }
  if (isAnalysisGoing(state)) return <Chip label="Checking coverage…" title="The coverage check is waiting or running." />
  if (state === 'succeeded') return <Chip label="Not checked" tone="warn" title="The check finished without a verdict." />
  return <Chip label="Check failed" tone={state === 'failed' ? 'error' : 'warn'} title="The coverage check didn't finish." />
}
