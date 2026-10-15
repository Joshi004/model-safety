// Mapping state for the UI's "Goes to" selects -- the browser's only model
// of --map/--require/--exclude-metadata. Kept in one file so App.tsx and
// InputFields.tsx share one definition of what a destination means and how
// it turns into the real request flags prepare_rows.py reads.
//
// Replaces the old mappingText.ts (free-text "target=source" textareas) --
// this version is a plain key -> destination map instead of parsed text,
// since each top-level key now gets its own "Goes to" select rather than a
// shared text box.

export type Target = 'text' | 'messages'
export type Destination = Target | 'metadata' | 'drop'

export interface MappingState {
  // A key absent from this map is implicitly 'metadata' -- that's also
  // _prepare_row()'s own real default: every column that isn't mapped,
  // required, or excluded lands in metadata untouched.
  destinations: Record<string, Destination>
  skipIfEmpty: Record<Target, boolean>
}

export function emptyMapping(): MappingState {
  return { destinations: {}, skipIfEmpty: { text: false, messages: false } }
}

export function destinationOf(state: MappingState, key: string): Destination {
  return state.destinations[key] ?? 'metadata'
}

// Which key currently feeds a target, if any. At most one key ever can --
// same as --map, which only keeps the last source given for the same
// target.
export function sourceFor(state: MappingState, target: Target): string | null {
  for (const [key, destination] of Object.entries(state.destinations)) {
    if (destination === target) return key
  }
  return null
}

// Sets `key`'s destination. Picking a key as the new source for `text` or
// `messages` first clears whichever other key held that target before
// (back to the implicit 'metadata') -- only one key can feed a target at
// a time, so the UI can't silently build a --map with two sources for the
// same target.
export function setDestination(state: MappingState, key: string, destination: Destination): MappingState {
  const destinations = { ...state.destinations }
  if (destination === 'text' || destination === 'messages') {
    for (const [existingKey, existingDestination] of Object.entries(destinations)) {
      if (existingDestination === destination && existingKey !== key) {
        delete destinations[existingKey]
      }
    }
  }
  if (destination === 'metadata') {
    delete destinations[key]
  } else {
    destinations[key] = destination
  }
  return { ...state, destinations }
}

export function setSkipIfEmpty(state: MappingState, target: Target, value: boolean): MappingState {
  return { ...state, skipIfEmpty: { ...state.skipIfEmpty, [target]: value } }
}

// Every top-level key seen across the rows, in the order first seen.
export function collectKeysInOrder(rows: Record<string, unknown>[]): string[] {
  const seen = new Set<string>()
  const ordered: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.has(key)) {
        seen.add(key)
        ordered.push(key)
      }
    }
  }
  return ordered
}

// Merges a backend suggestion into the current state -- applies every
// target the suggestion mentions, same as clicking "Goes to" for each one
// by hand. Clicking "Apply" again (e.g. after the mapping changed) always
// matches whatever the backend currently suggests.
export function applySuggestion(state: MappingState, suggestedMap: Record<string, string>): MappingState {
  let next = state
  for (const [target, source] of Object.entries(suggestedMap)) {
    if (target === 'text' || target === 'messages') {
      next = setDestination(next, source, target)
    }
  }
  return next
}

export interface RequestFlags {
  map: Record<string, string>
  require: string[]
  exclude_metadata: string[]
}

// The UI's mapping state for a set of saved flags -- the reverse of toRequestFlags. A map
// target other than text or messages is left out: this UI can't set one.
export function fromRequestFlags(flags: RequestFlags): MappingState {
  const destinations: Record<string, Destination> = {}
  for (const [target, source] of Object.entries(flags.map)) {
    if (target === 'text' || target === 'messages') destinations[source] = target
  }
  for (const key of flags.exclude_metadata) destinations[key] = 'drop'
  return {
    destinations,
    skipIfEmpty: { text: flags.require.includes('text'), messages: flags.require.includes('messages') },
  }
}

// Turns the UI's mapping state into exactly the three request fields
// /api/preview reads -- the same three flags (--map, --require,
// --exclude-metadata) a real prepare_rows.py run would take.
export function toRequestFlags(state: MappingState): RequestFlags {
  const map: Record<string, string> = {}
  const exclude_metadata: string[] = []
  for (const [key, destination] of Object.entries(state.destinations)) {
    if (destination === 'text' || destination === 'messages') map[destination] = key
    if (destination === 'drop') exclude_metadata.push(key)
  }
  const require = (['text', 'messages'] as const).filter((target) => map[target] && state.skipIfEmpty[target])
  return { map, require, exclude_metadata }
}
