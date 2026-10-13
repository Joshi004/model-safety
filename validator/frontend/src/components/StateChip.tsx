import { STATE_LABELS } from '../chains'
import type { StageState } from '../types'

// A stage's state, and a coverage / trace / row status verdict, as small
// coloured labels. Both come from files on disk and render as plain text.

export default function StateChip({ state }: { state: StageState }) {
  return <span className={`state-chip state-${state}`}>{STATE_LABELS[state]}</span>
}

export function VerdictChip({ verdict }: { verdict: string | null }) {
  if (verdict === null) return null
  return <span className={verdict === 'pass' ? 'state-chip state-finished' : 'state-chip state-failed'}>{verdict}</span>
}
