// Tiny localStorage helpers for client-only UI preferences (selected
// team, which groups are expanded, which entries are marked reviewed).
// Never sent to the backend. Wrapped in try/catch since localStorage
// can throw (private browsing, quota) -- losing a UI preference isn't
// worth surfacing an error for, so callers always get a safe fallback.
const PREFIX = 'validator.'

export function loadString(key: string, fallback: string): string {
  try {
    return localStorage.getItem(PREFIX + key) ?? fallback
  } catch {
    return fallback
  }
}

export function saveString(key: string, value: string): void {
  try {
    localStorage.setItem(PREFIX + key, value)
  } catch {
    // ignore -- see module comment
  }
}

export function loadJSON<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

export function saveJSON<T>(key: string, value: T): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value))
  } catch {
    // ignore -- see module comment
  }
}
