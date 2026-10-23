// A small in-memory cache of what the backend last answered, so a page can show it at
// once while the up-to-date answer is fetched in the background. It lives in this tab's
// memory only: a reload starts empty, and nothing is ever written to disk.
//
// Anything that changes something on the backend calls `clearCache` (see api.ts), so a
// page never starts from what was there before the change.
const entries = new Map<string, unknown>()

// Bumped by `clearCache`. A request remembers the number it started under, so an answer
// that arrives after a change is not stored: it may describe the state before the change.
let generation = 0

export function cacheGeneration(): number {
  return generation
}

export function getCached<T>(key: string): T | undefined {
  return entries.get(key) as T | undefined
}

// `startedUnder` is `cacheGeneration()` as it was when the request was sent.
export function setCached(key: string, value: unknown, startedUnder: number): void {
  if (startedUnder !== generation) return
  entries.set(key, value)
}

export function clearCache(): void {
  generation += 1
  entries.clear()
}
