import { useEffect, useState } from 'react'
import type { InspectGroup } from '../types'

function formatBytes(bytes: number): string {
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

export default function FormatPicker({
  groups,
  isSingleFile,
  onSelect,
}: {
  groups: InspectGroup[]
  isSingleFile: boolean
  // Called the moment a selection is actually ready to preview: right
  // away for a group that reads a whole file/folder, or once a file is
  // chosen for a group that needs exactly one (csv/json).
  onSelect: (group: InspectGroup, file: string | null) => void
}) {
  const readableIndices = groups
    .map((group, index) => (group.input_type ? index : -1))
    .filter((index) => index >= 0)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(readableIndices[0] ?? null)
  const [selectedFile, setSelectedFile] = useState<string>('')

  useEffect(() => {
    const firstIndex = readableIndices[0] ?? null
    setSelectedIndex(firstIndex)
    setSelectedFile('')
    if (firstIndex !== null) {
      const group = groups[firstIndex]
      const needsFile = group.sample_target === 'file' && !isSingleFile
      if (!needsFile) onSelect(group, null)
    }
    // groups is a new array on every inspect, which is exactly when this
    // selection should reset and auto-load -- intentionally not depending
    // on readableIndices/isSingleFile/onSelect themselves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups])

  const selected = selectedIndex !== null ? groups[selectedIndex] : null
  const needsFile = selected?.sample_target === 'file' && !isSingleFile

  function selectGroup(index: number, group: InspectGroup) {
    setSelectedIndex(index)
    setSelectedFile('')
    const stillNeedsFile = group.sample_target === 'file' && !isSingleFile
    if (!stillNeedsFile) onSelect(group, null)
  }

  function selectFile(file: string) {
    setSelectedFile(file)
    if (selected && file) onSelect(selected, file)
  }

  return (
    <div className="format-picker">
      <h3>What's here</h3>
      {groups.length === 0 && <p className="muted">Nothing found.</p>}
      <ul className="group-list">
        {groups.map((group, index) => {
          const isSelected = index === selectedIndex
          const isReadable = group.input_type !== null
          return (
            <li
              key={`${group.format}-${index}`}
              className={`group-item ${isReadable ? 'readable' : 'unreadable'} ${isSelected ? 'selected' : ''}`}
              onClick={() => {
                if (isReadable) selectGroup(index, group)
              }}
            >
              <div className="group-item-header">
                <strong>{group.format}</strong>
                <span className="muted small">
                  {group.file_count} file(s)
                  {group.total_bytes > 0 ? `, ${formatBytes(group.total_bytes)}` : ''}
                  {group.truncated ? ' (more not shown)' : ''}
                </span>
              </div>
              {group.note && <p className="group-note small">{group.note}</p>}
              {!isSingleFile && group.files.length > 0 && (
                <p className="muted small">
                  e.g. {group.files.slice(0, 3).join(', ')}
                  {group.files.length > 3 ? ', …' : ''}
                </p>
              )}
            </li>
          )
        })}
      </ul>

      {selected && needsFile && (
        <label>
          File to use ({selected.format} needs exactly one)
          <select value={selectedFile} onChange={(event) => selectFile(event.target.value)}>
            <option value="">Choose a file…</option>
            {selected.files.map((file) => (
              <option key={file} value={file}>
                {file}
              </option>
            ))}
          </select>
        </label>
      )}
      {selected && !needsFile && !isSingleFile && selected.sample_target === 'folder' && (
        <p className="muted small">
          Samples from the first file found; a real run processes every matching file in this folder.
        </p>
      )}
    </div>
  )
}
