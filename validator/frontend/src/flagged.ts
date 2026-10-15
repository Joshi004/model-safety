import { choiceLabel, nerLabelName, ruleCategoryName } from './scans'
import type { FlaggedEntry, RowHighlight } from './types'

// Plain-words helpers for the flagged rows of a result. Nothing here touches the backend, and
// all of it builds plain text.

const REASON_LABELS: Record<string, string> = {
  duplicate: 'Duplicate',
  duplicate_of_reference: 'Duplicate of a reference set',
  ner: 'NER',
  regex_pii: 'Rules',
  toxicity_unsafe: 'Unsafe',
  toxicity_controversial: 'Controversial',
  toxicity_guard_error: 'Guard error',
  // A row someone chose to drop (a filtered dataset's removed rows).
  manual: 'Chosen to drop',
  // A row dropped by itself after a scan, with nobody choosing it (D17).
  automatic: 'Dropped automatically',
}

export function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason
}

// What the second filter of each scanner's table is called. Toxicity has none: its table
// follows the categories and verdicts chosen above it.
const NAME_FILTER_LABELS: Record<string, string> = {
  pii_ner: 'Label',
  pii_regex: 'Category',
  dedup: 'Kept copy in',
}

export function nameFilterLabel(scannerKey: string): string {
  return NAME_FILTER_LABELS[scannerKey] ?? 'Name'
}

// A NER label or a rule-based category as it is named elsewhere on the page.
export function findingName(scannerKey: string, name: string): string {
  if (scannerKey === 'pii_ner') return nerLabelName(name)
  if (scannerKey === 'pii_regex') return ruleCategoryName(name)
  return name
}

// "Unsafe: Violent": one part of a toxicity row that counts as flagged. The tally's own atoms
// are "error" and "<verdict>|<category>", with an empty category for a verdict that has none.
export function describeAtom(atom: string): string {
  if (atom === 'error') return 'Guard error'
  const bar = atom.indexOf('|')
  const verdict = bar === -1 ? atom : atom.slice(0, bar)
  const category = bar === -1 ? '' : atom.slice(bar + 1)
  const verdictName = choiceLabel('toxicity', 'verdicts', verdict)
  return category === '' ? `${verdictName} (no category)` : `${verdictName}: ${category}`
}

// "Email addresses ×2, Phone numbers": the names in a row, with their counts when above 1.
function describeNames(scannerKey: string, names: Record<string, number>): string {
  return Object.entries(names)
    .map(([name, count]) => (count > 1 ? `${findingName(scannerKey, name)} ×${count}` : findingName(scannerKey, name)))
    .join(', ')
}

// What the table says was found in a row.
export function describeFound(scannerKey: string, entry: FlaggedEntry): string {
  if (scannerKey === 'toxicity') return entry.atoms.map(describeAtom).join('; ') || '—'
  if (scannerKey === 'dedup') return entry.kept_in === null ? '—' : `Matches a row in ${entry.kept_in}`
  return describeNames(scannerKey, entry.names) || '—'
}

// "98%", or a dash when the scan didn't give a confidence.
export function describeConfidence(confidence: number | null | undefined): string {
  return typeof confidence === 'number' ? `${Math.round(confidence * 100)}%` : '—'
}

// One mark in the text: what found it.
export function describeHighlight(mark: RowHighlight): string {
  if (mark.scanner === 'ner') {
    return `NER: ${nerLabelName(mark.label ?? '')}, ${describeConfidence(mark.confidence)}`
  }
  return `Rules: ${ruleCategoryName(mark.category ?? '')}`
}

// "message 2", or "text" for a row that has a `text` and no messages.
export function whereInRow(messageIndex: number | null | undefined): string {
  return messageIndex === null || messageIndex === undefined ? 'text' : `message ${messageIndex}`
}

// A piece of a message's text, and the marks that cover it.
export interface TextPiece {
  text: string
  marks: RowHighlight[]
}

// Cuts `text` at every start and end of `marks` (marks may overlap), so each piece has the list
// of marks that cover all of it. A mark outside the text is ignored.
export function splitPieces(text: string, marks: RowHighlight[]): TextPiece[] {
  const inside = marks.filter((mark) => mark.start < mark.end && mark.start < text.length)
  if (inside.length === 0) return [{ text, marks: [] }]
  const cuts = new Set<number>([0, text.length])
  for (const mark of inside) {
    cuts.add(mark.start)
    cuts.add(Math.min(mark.end, text.length))
  }
  const ordered = [...cuts].sort((a, b) => a - b)
  const pieces: TextPiece[] = []
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const start = ordered[index]
    const end = ordered[index + 1]
    pieces.push({
      text: text.slice(start, end),
      marks: inside.filter((mark) => mark.start <= start && Math.min(mark.end, text.length) >= end),
    })
  }
  return pieces
}
