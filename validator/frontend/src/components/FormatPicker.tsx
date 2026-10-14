import { useEffect, useState } from 'react'
import { formatBytes } from '../format'
import type { SourceChoice } from '../source'
import type { InspectGroup } from '../types'

// The group and file to start with: the saved ones when the group is still there,
// otherwise the first group a reader understands.
function startingSelection(
  groups: InspectGroup[],
  initial: SourceChoice | null,
): { index: number | null; file: string } {
  const saved = initial === null ? -1 : groups.findIndex((group) => group.input_type === initial.input_type)
  if (saved >= 0) return { index: saved, file: initial?.file ?? '' }
  const first = groups.findIndex((group) => group.input_type)
  return { index: first >= 0 ? first : null, file: '' }
}

export default function FormatPicker({
  groups,
  isSingleFile,
  initial = null,
  onSelect,
}: {
  groups: InspectGroup[]
  isSingleFile: boolean
  // What the dataset has saved, if anything: the picker starts on it.
  initial?: SourceChoice | null
  // Called the moment a selection is actually ready to preview: right
  // away for a group that reads a whole file/folder, or once a file is
  // chosen for a group that needs exactly one (csv/json).
  onSelect: (group: InspectGroup, file: string | null) => void
}) {
  const [selectedIndex, setSelectedIndex] = useState<number | null>(() => startingSelection(groups, initial).index)
  const [selectedFile, setSelectedFile] = useState<string>(() => startingSelection(groups, initial).file)

  useEffect(() => {
    const start = startingSelection(groups, initial)
    setSelectedIndex(start.index)
    setSelectedFile(start.file)
    if (start.index !== null) {
      const group = groups[start.index]
      const needsFile = group.sample_target === 'file' && !isSingleFile
      if (!needsFile) onSelect(group, null)
      else if (start.file) onSelect(group, start.file)
    }
    // groups is a new array on every inspect, which is exactly when this
    // selection should reset and auto-load -- intentionally not depending
    // on initial/isSingleFile/onSelect themselves.
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
            {/* The saved file may be past the 50 the inspect lists. */}
            {(selectedFile === '' || selected.files.includes(selectedFile)
              ? selected.files
              : [selectedFile, ...selected.files]
            ).map((file) => (
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
