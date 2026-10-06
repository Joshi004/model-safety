// Helpers for `_origin` notes (see modelsafety/contract/origin.py) and the
// skipped-rows page. Everything here only builds plain text from data -- it
// is shown as React text, never as markup.
import type { LookupStatus, Origin, SkippedEntry } from './types'

// A Prepared row's `metadata._origin`, or null if it has none (or a
// malformed one). The row can come from any file, so nothing is assumed.
export function getOrigin(row: Record<string, unknown>): Origin | null {
  const metadata = row.metadata
  if (metadata === null || typeof metadata !== 'object' || Array.isArray(metadata)) return null
  const origin = (metadata as Record<string, unknown>)._origin
  if (origin === null || typeof origin !== 'object' || Array.isArray(origin)) return null
  if (typeof (origin as Record<string, unknown>).path !== 'string') return null
  return origin as Origin
}

export function basename(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path
}

// "Line 3" for a JSONL note, "row 12" (counting from 0) for the rest.
export function describePosition(origin: Origin): string {
  if (origin.line !== undefined) return `line ${origin.line}`
  return `row ${origin.row} (counting from 0)`
}

export function describeLocation(origin: Origin): string {
  if (origin.line !== undefined && origin.byte !== undefined) {
    return `${origin.path}, line ${origin.line} (byte ${origin.byte})`
  }
  const split = origin.split ? `, split "${origin.split}"` : ''
  return `${origin.path}${split}, row ${origin.row} (counting from 0)`
}

// For example: 'question' (used as text) is empty
export function describeSkipReason(entry: SkippedEntry): string {
  const column =
    entry.used_as === entry.column
      ? `'${entry.column}'`
      : `'${entry.column}' (used as ${entry.used_as})`
  if (entry.reason === 'missing_column') return `${column} is missing`
  if (entry.reason === 'empty_column') return `${column} is empty`
  return `${column} was skipped (${entry.reason})`
}

export const STATUS_LABELS: Record<LookupStatus, string> = {
  unchanged: 'Unchanged',
  changed: 'Changed',
  not_found: 'Not found',
}

export function statusSentence(status: LookupStatus, path: string): string {
  if (status === 'unchanged') return 'This is the original row, unchanged.'
  if (status === 'changed') return 'The original file has changed since this run.'
  return `Original file not found at ${path}.`
}

// Same rule as Python's shlex.quote: the path comes from data, so it is
// quoted before it is offered as a terminal command.
function shellQuote(text: string): string {
  if (text !== '' && /^[A-Za-z0-9_@%+=:,./-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'"'"'`)}'`
}

// Prints the one line a JSONL note points at (`tail` counts bytes from 1).
export function printCommand(origin: Origin): string | null {
  const byte = origin.byte
  if (typeof byte !== 'number' || !Number.isInteger(byte) || byte < 0) return null
  return `tail -c +${byte + 1} ${shellQuote(origin.path)} | head -n 1`
}
