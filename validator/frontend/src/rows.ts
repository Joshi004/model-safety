import type { RowHighlight, RowOutcome, RowPlace, RowSummary, RowTarget } from './types'

// Plain-words helpers for the Rows page, and the link (URL hash) that names a row.
// Nothing here touches the backend, and all of it builds plain text.

export const SCANNER_LABELS: Record<string, string> = {
  dedup: 'Duplicates',
  regex_pii: 'Regex PII',
  ner: 'NER',
  toxicity: 'Toxicity',
}

export const REASON_LABELS: Record<string, string> = {
  duplicate: 'duplicate',
  ner: 'NER',
  regex_pii: 'regex PII',
  toxicity_unsafe: 'toxicity (Unsafe)',
  toxicity_controversial: 'toxicity (Controversial)',
  toxicity_guard_error: 'toxicity (guard error)',
}

export function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason
}

export function describeReasonList(reasons: string[]): string {
  return reasons.length === 0 ? 'no reason recorded' : reasons.map(reasonLabel).join(', ')
}

// "kept", "removed by NER, regex PII", "not reached": what one step did to a row.
export function describeStepOutcome(outcome: RowOutcome, reasons: string[]): string {
  if (outcome === 'kept') return 'kept'
  if (outcome === 'removed') return `removed by ${describeReasonList(reasons)}`
  return 'not reached (removed earlier)'
}

// What became of a row, in a sentence.
export function describeFate(summary: RowSummary): string {
  if (summary.status === 'kept') return 'kept: it is in the clean data'
  if (summary.status === 'removed') {
    return `removed at step ${summary.removed_at_step} by ${describeReasonList(summary.reasons)}`
  }
  return 'unknown'
}

export function describePlace(place: RowPlace): string {
  return `${place.run}, ${place.file}, line ${place.line}`
}

// One mark in the text: what found it, and whether it is why the row was removed.
export function describeHighlight(mark: RowHighlight): string {
  const caused = mark.caused_removal ? ' (caused the removal)' : ''
  if (mark.scanner === 'ner') {
    const confidence = typeof mark.confidence === 'number' ? `, ${Math.round(mark.confidence * 100)}%` : ''
    return `Step ${mark.step}, NER: ${mark.label}${confidence}${caused}`
  }
  return `Step ${mark.step}, regex PII: ${mark.category}${caused}`
}

// A piece of a message's text, and the marks that cover it.
export interface TextPiece {
  text: string
  marks: RowHighlight[]
}

// Cuts `text` at every start and end of `marks` (marks may overlap), so each piece
// has the list of marks that cover all of it. A mark outside the text is ignored.
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

// ---- The link to a row: #row?chain=...&run=...&file=...&line=...&from=clean ----

const ROW_HASH = '#row?'

export function rowHash(target: RowTarget): string {
  const params = new URLSearchParams({
    chain: target.chainDir,
    run: target.run,
    file: target.file,
    line: String(target.line),
    from: target.from,
  })
  return `${ROW_HASH}${params.toString()}`
}

// The row a link names, or null when the hash isn't a complete row link.
export function parseRowHash(hash: string): RowTarget | null {
  if (!hash.startsWith(ROW_HASH)) return null
  const params = new URLSearchParams(hash.slice(ROW_HASH.length))
  const chainDir = params.get('chain')
  const run = params.get('run')
  const file = params.get('file')
  const line = Number(params.get('line'))
  const from = params.get('from') === 'clean' ? 'clean' : 'prepared'
  if (!chainDir || !run || !file || !Number.isInteger(line) || line < 1) return null
  return { chainDir, run, file, line, from }
}

export function sameTarget(a: RowTarget | null, b: RowTarget | null): boolean {
  if (a === null || b === null) return a === b
  return a.chainDir === b.chainDir && a.run === b.run && a.file === b.file && a.line === b.line && a.from === b.from
}
