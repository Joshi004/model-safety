// A UTC ISO time from a file as the browser's local time. Anything the
// browser can't read is shown as it is.
export function formatTimestamp(value: string): string {
  const time = new Date(value)
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString()
}

// A count with thousands separators, or a dash for a number that isn't known yet.
export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString()
}

// "1:02:03": a length of time in hours, minutes and seconds (hours can go past 24).
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const rest = String(total % 60).padStart(2, '0')
  return `${Math.floor(total / 3600)}:${minutes}:${rest}`
}

// The seconds from `start` to `end` (UTC ISO times), or null when either is missing or
// can't be read.
export function secondsBetween(start: string | null, end: string | null): number | null {
  if (start === null || end === null) return null
  const from = new Date(start).getTime()
  const to = new Date(end).getTime()
  return Number.isNaN(from) || Number.isNaN(to) ? null : (to - from) / 1000
}

// "47.6%": `part` as a share of `whole`, or a dash when there is no whole.
export function formatPercent(part: number | null | undefined, whole: number | null | undefined): string {
  if (part === null || part === undefined || !whole) return '—'
  return `${((part / whole) * 100).toFixed(1)}%`
}

// "1.5 MB", "12 KB", "0 B": a byte count in the biggest unit that keeps the
// number small.
export function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(unitIndex === 0 || value >= 10 ? 0 : 1)} ${units[unitIndex]}`
}

// A setting or rule value from a run_identity.json or summary.json, as text.
export function formatSetting(value: unknown): string {
  if (value === null || value === undefined) return '(not set)'
  if (Array.isArray(value)) {
    if (value.length === 0) return '(none)'
    return value.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))).join(', ')
  }
  if (typeof value === 'string') return value === '' ? '(empty)' : value
  return JSON.stringify(value)
}
