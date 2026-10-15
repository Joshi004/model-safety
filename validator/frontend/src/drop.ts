// The rows a user has chosen to drop from one dataset, across its result pages, and how that
// becomes a request. No React here, and nothing touches the backend.
//
// The choice is kept per scanner. For each scanner it is either some rows (ticked one by one) or
// every row that matches the filters at the time ("select all matching", which the server
// resolves when it counts and again when it drops). It remembers the scan the rows were chosen
// from, and for toxicity the categories and verdicts they were chosen under: rows ticked under one
// choice might not be flagged under another, so a part is only ever added to under the same ones.
import { formatCount } from './format'
import { scannerLabel } from './scans'
import type { DropRequest, DropRow, DropSelectionRequest } from './types'

// The most rows that can be ticked one by one in all (the same limit the backend has). "Select all
// matching" has no such limit.
export const MAX_EXPLICIT_ROWS = 10_000

export interface AllMatching {
  reason: string | null
  name: string | null
  // How many rows matched when it was chosen. The server counts them again.
  total: number
}

export interface ScannerSelection {
  // The scan the rows were chosen from. A newer scan replaces it, and the choice with it.
  scanId: string
  // Ticked rows by `rowKey`.
  rows: Map<string, DropRow>
  // Set when every row matching the filters is chosen; then `rows` is empty.
  all: AllMatching | null
  // Toxicity's choice of what counts as flagged when the rows were chosen (null: the scan's own).
  categories: string[] | null
  verdicts: string[] | null
}

// By scanner key. A scanner with nothing chosen has no entry.
export type DropSelection = Record<string, ScannerSelection>

export const EMPTY_SELECTION: DropSelection = {}

// What a part of the selection is made from: the scanner, its scan, and toxicity's choice.
export interface SelectionSource {
  scanner: string
  scanId: string
  categories: string[] | null
  verdicts: string[] | null
}

// A row is told apart by its place in the dataset's Prepared version.
export function rowKey(file: string, line: number): string {
  return `${line}|${file}`
}

function sameChoice(a: string[] | null, b: string[] | null): boolean {
  if (a === null || b === null) return a === b
  return a.length === b.length && a.every((item, index) => item === b[index])
}

function matches(part: ScannerSelection, source: SelectionSource): boolean {
  return (
    part.scanId === source.scanId &&
    sameChoice(part.categories, source.categories) &&
    sameChoice(part.verdicts, source.verdicts)
  )
}

// The rows chosen from this source so far. A part made from another scan, or under another
// choice, doesn't count: it is replaced the first time something is chosen again.
export function partFor(selection: DropSelection, source: SelectionSource): ScannerSelection | null {
  const part = selection[source.scanner]
  return part !== undefined && matches(part, source) ? part : null
}

export function partCount(part: ScannerSelection): number {
  return part.all !== null ? part.all.total : part.rows.size
}

function withPart(selection: DropSelection, scanner: string, part: ScannerSelection): DropSelection {
  const next = { ...selection }
  if (part.all === null && part.rows.size === 0) delete next[scanner]
  else next[scanner] = part
  return next
}

function freshPart(source: SelectionSource): ScannerSelection {
  return { scanId: source.scanId, rows: new Map(), all: null, categories: source.categories, verdicts: source.verdicts }
}

// Ticks or unticks rows. With every matching row chosen already, rows can't be changed one by one.
export function setRows(
  selection: DropSelection,
  source: SelectionSource,
  rows: DropRow[],
  checked: boolean,
): DropSelection {
  const current = partFor(selection, source) ?? freshPart(source)
  if (current.all !== null) return selection
  const next = new Map(current.rows)
  for (const row of rows) {
    if (checked) next.set(rowKey(row.file, row.line), row)
    else next.delete(rowKey(row.file, row.line))
  }
  return withPart(selection, source.scanner, { ...current, rows: next })
}

// Chooses every row that matches the filters, in place of any rows ticked one by one.
export function selectAllMatching(
  selection: DropSelection,
  source: SelectionSource,
  filters: { reason: string | null; name: string | null },
  total: number,
): DropSelection {
  if (total <= 0) return selection
  return withPart(selection, source.scanner, {
    ...freshPart(source),
    all: { reason: filters.reason, name: filters.name, total },
  })
}

export function clearAll(): DropSelection {
  return EMPTY_SELECTION
}

export function clearScanner(selection: DropSelection, scanner: string): DropSelection {
  if (!(scanner in selection)) return selection
  const next = { ...selection }
  delete next[scanner]
  return next
}

// Whether a row is chosen: every row is, when all matching rows are.
export function isChosen(part: ScannerSelection | null, file: string, line: number): boolean {
  if (part === null) return false
  return part.all !== null || part.rows.has(rowKey(file, line))
}

export function scannerKeys(selection: DropSelection): string[] {
  return Object.keys(selection)
}

export function isEmpty(selection: DropSelection): boolean {
  return scannerKeys(selection).length === 0
}

// Rows ticked one by one, across every scanner.
export function explicitCount(selection: DropSelection): number {
  return Object.values(selection).reduce((sum, part) => sum + part.rows.size, 0)
}

// "4 from Toxicity, all 6 matching from PII (rules)": what is chosen, before the server has
// counted it. A row that more than one scanner flagged is counted once for each here.
export function describeSelection(selection: DropSelection): string {
  return scannerKeys(selection)
    .map((scanner) => {
      const part = selection[scanner]
      const label = scannerLabel(scanner)
      return part.all !== null
        ? `all ${formatCount(part.all.total)} matching from ${label}`
        : `${formatCount(part.rows.size)} from ${label}`
    })
    .join(', ')
}

// The request the backend takes, for the dataset's converted data `versionId`.
export function buildDropRequest(versionId: string, selection: DropSelection): DropRequest {
  const selections: DropSelectionRequest[] = scannerKeys(selection).map((scanner) => {
    const part = selection[scanner]
    return {
      scanner,
      scan_id: part.scanId,
      rows: part.all === null ? [...part.rows.values()] : null,
      all_matching: part.all === null ? null : { reason: part.all.reason, name: part.all.name },
      categories: part.categories,
      verdicts: part.verdicts,
    }
  })
  return { version_id: versionId, selections }
}
