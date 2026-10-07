// A UTC ISO time from a file as the browser's local time. Anything the
// browser can't read is shown as it is.
export function formatTimestamp(value: string): string {
  const time = new Date(value)
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString()
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
