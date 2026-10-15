// Tiny localStorage helpers for client-only UI preferences (the selected
// team on the Datasets page). Never sent to the backend. Wrapped in try/catch since localStorage
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

