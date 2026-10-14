// Plain-language, one-line labels for a real sampled or converted value --
// e.g. "text · 523 chars", "list · 2 objects (role, content)", "object ·
// keys: from, value". Used by InputFields.tsx (the input side) and
// ValueView.tsx (both sides) so a value's shape reads in plain words
// instead of as a raw type name. Every label is computed from the actual
// value passed in -- nothing here invents example data.

import type { PreviewResult } from './types'

// Matches backend/preview.py's NON_JSON_KEY exactly: the marker to_display()
// substitutes for a real value that would break json.dumps() (a pandas
// Timestamp, raw bytes, a numpy scalar, inf/-inf) -- a value a real
// conversion run would crash on, not something fabricated for the UI.
export const NON_JSON_KEY = '__non_json_value__'

export interface NonJsonMarker {
  [NON_JSON_KEY]: { type: string; repr: string }
}

export function isNonJsonValue(value: unknown): value is NonJsonMarker {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const marker = (value as Record<string, unknown>)[NON_JSON_KEY]
  if (marker === null || typeof marker !== 'object') return false
  return typeof (marker as Record<string, unknown>).type === 'string'
}

// A list that matches the one shape extract_record_text() reads as a
// conversation: every item is a plain object with BOTH a 'role' and a
// 'content' key (mirrors backend/preview.py's _is_messages_field() --
// same bar, so the UI's "this looks like a conversation" guess can't
// disagree with the backend's own). Detected so the UI can render it as
// a readable transcript instead of a generic list of objects -- see
// ValueView.tsx's ChatTurnList.
export function isChatTurnList(value: unknown): value is Record<string, unknown>[] {
  if (!Array.isArray(value) || value.length === 0) return false
  return value.every((item) => {
    if (item === null || typeof item !== 'object' || Array.isArray(item) || isNonJsonValue(item)) {
      return false
    }
    const keys = item as Record<string, unknown>
    return 'role' in keys && 'content' in keys
  })
}

// Mirrors modelsafety/contract/textract.py's extract_record_text() own
// per-turn heading rule exactly: str(message.get("role") or
// f"message_{index}").upper() -- any falsy role (missing, None, "", 0,
// False) falls back to a positional heading, 1-indexed. `zeroBasedIndex`
// is this turn's position in the array (0, 1, 2, ...).
export function roleHeading(role: unknown, zeroBasedIndex: number): string {
  const isFalsy = role === null || role === undefined || role === '' || role === 0 || role === false
  const value = isFalsy ? `message_${zeroBasedIndex + 1}` : role
  return String(value).toUpperCase()
}

// Mirrors modelsafety/contract/textract.py's extract_content_text()
// exactly. Used only to tell the user, on the input side, whether a
// turn's content would actually produce any scanned text once mapped --
// the real scanned text shown on the output side always comes from the
// backend's own call to the real function, never from this copy.
export function extractContentText(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts: string[] = []
  for (const block of content) {
    if (typeof block === 'string') {
      parts.push(block)
    } else if (
      block !== null &&
      typeof block === 'object' &&
      !Array.isArray(block) &&
      typeof (block as Record<string, unknown>).text === 'string'
    ) {
      parts.push((block as Record<string, unknown>).text as string)
    }
  }
  return parts.join('\n')
}

function describeListItems(items: unknown[]): string {
  if (items.length === 0) return 'items'
  const plural = items.length !== 1
  const first = items[0]
  if (isNonJsonValue(first)) {
    return plural ? 'non-JSON values' : 'non-JSON value'
  }
  if (Array.isArray(first)) {
    return plural ? 'lists' : 'list'
  }
  if (first !== null && typeof first === 'object') {
    const keys = Object.keys(first as Record<string, unknown>)
    const noun = plural ? 'objects' : 'object'
    return keys.length > 0 ? `${noun} (${keys.join(', ')})` : noun
  }
  if (first === null) return plural ? 'empty values' : 'empty value'
  const kind = typeof first === 'string' ? 'string' : typeof first === 'boolean' ? 'boolean' : 'number'
  return plural ? `${kind}s` : kind
}

/** A short, plain-language description of what a real value actually is. */
export function describeValue(value: unknown): string {
  if (isNonJsonValue(value)) {
    return `${value[NON_JSON_KEY].type} (not plain JSON)`
  }
  if (value === null || value === undefined) {
    return 'empty (null)'
  }
  if (typeof value === 'string') {
    if (value.trim().length === 0) return 'blank text'
    return `text · ${value.length} char${value.length === 1 ? '' : 's'}`
  }
  if (typeof value === 'boolean') {
    return 'true/false'
  }
  if (typeof value === 'number') {
    return 'number'
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return 'list · empty'
    if (isChatTurnList(value)) {
      return `conversation · ${value.length} turn${value.length === 1 ? '' : 's'}`
    }
    return `list · ${value.length} ${describeListItems(value)}`
  }
  if (typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>)
    return keys.length > 0 ? `object · keys: ${keys.join(', ')}` : 'object · empty'
  }
  return typeof value
}

/** A plain-language one-line status for a sampled row, e.g. for a row tab. */
export function describeRowStatus(result: PreviewResult, rowIndex: number): string {
  const written = result.rows.find((row) => row.row_index === rowIndex)
  if (written) {
    return written.scanned_text_length > 0
      ? `written, ${written.scanned_text_length.toLocaleString()} chars scanned`
      : 'written, scanner reads nothing'
  }
  const skipped = result.skipped_rows.find((row) => row.row_index === rowIndex)
  if (skipped) return `skipped, missing ${skipped.missing_column ?? 'a required column'}`
  const errored = result.write_errors.find((error) => error.row_index === rowIndex)
  if (errored) return 'would stop the run'
  return 'not in this sample'
}

export const MAX_INLINE_TEXT_CHARS = 400

/** Splits a long string for display: the part to show, and whether more was cut. */
export function truncateText(
  value: string,
  max: number = MAX_INLINE_TEXT_CHARS,
): { shown: string; truncated: boolean; totalLength: number } {
  if (value.length <= max) return { shown: value, truncated: false, totalLength: value.length }
  return { shown: value.slice(0, max), truncated: true, totalLength: value.length }
}
