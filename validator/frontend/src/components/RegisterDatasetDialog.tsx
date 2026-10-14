import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { errorMessage, inspectPath, registerDataset } from '../api'
import { formatBytes } from '../format'
import type { Dataset, InspectResult, Team } from '../types'
import Dialog from './Dialog'

const NAME_MAX_CHARS = 100

type Detection =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  // `path` is the text that was inspected.
  | { status: 'done'; path: string; result: InspectResult }

const KIND_LABELS: Record<InspectResult['kind'], string> = {
  file: 'A single file',
  directory: 'A folder',
  huggingface_dataset: 'A HuggingFace dataset folder',
}

// The last part of the path as a name the registry accepts: runs of other characters become
// one space, and the name starts with a letter or digit (the same rule as backend/registry.py).
function suggestName(path: string): string {
  const last = path.split('/').filter(Boolean).pop() ?? ''
  return last
    .replace(/[^A-Za-z0-9._-]+/g, ' ')
    .trim()
    .replace(/^[ ._-]+/, '')
    .slice(0, NAME_MAX_CHARS)
}

function teamLabel(team: Team): string {
  return team.description ? `${team.name} (${team.description})` : team.name
}

export default function RegisterDatasetDialog({
  teams,
  defaultTeam,
  onClose,
  onRegistered,
}: {
  teams: Team[]
  // The team tab that was open, or '' for "All".
  defaultTeam: string
  onClose: () => void
  onRegistered: (dataset: Dataset) => void
}) {
  const [team, setTeam] = useState(defaultTeam)
  const [path, setPath] = useState('')
  const [name, setName] = useState('')
  // Once the user types a name, detecting another path doesn't replace it.
  const [nameEdited, setNameEdited] = useState(false)
  const [description, setDescription] = useState('')
  const [detection, setDetection] = useState<Detection>({ status: 'idle' })
  const [registering, setRegistering] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Lets an answer for an older path be ignored after the path was edited.
  const detectionId = useRef(0)

  const trimmedPath = path.trim()
  const trimmedName = name.trim()
  const detectedThisPath = detection.status === 'done' && detection.path === trimmedPath
  const canRegister = team !== '' && trimmedName !== '' && detectedThisPath && !registering

  function changePath(value: string) {
    setPath(value)
    detectionId.current += 1
    setDetection({ status: 'idle' })
  }

  async function detect() {
    if (trimmedPath === '') return
    const id = ++detectionId.current
    setDetection({ status: 'loading' })
    try {
      const result = await inspectPath(trimmedPath)
      if (id !== detectionId.current) return
      setDetection({ status: 'done', path: trimmedPath, result })
      if (!nameEdited) setName(suggestName(result.path))
    } catch (failure) {
      if (id !== detectionId.current) return
      setDetection({ status: 'error', message: errorMessage(failure) })
    }
  }

  async function register() {
    if (!canRegister) return
    setRegistering(true)
    setError(null)
    try {
      onRegistered(await registerDataset({ team, name: trimmedName, path: trimmedPath, description: description.trim() }))
    } catch (failure) {
      setError(errorMessage(failure))
      setRegistering(false)
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    void register()
  }

  const nothingConvertible =
    detection.status === 'done' && detection.result.groups.every((group) => group.input_type === null)

  return (
    <Dialog title="Register dataset" onClose={onClose} busy={registering}>
      <form className="form" onSubmit={handleSubmit}>
        <label>
          <span className="field-label">Team</span>
          <select value={team} onChange={(event) => setTeam(event.target.value)} required>
            <option value="">Choose a team…</option>
            {teams.map((option) => (
              <option key={option.name} value={option.name}>
                {teamLabel(option)}
              </option>
            ))}
          </select>
        </label>

        <div className="field">
          <label htmlFor="register-path" className="field-label">
            Path to the original data
          </label>
          <div className="input-row">
            <input
              id="register-path"
              type="text"
              className="mono"
              placeholder="/absolute/path/to/the/data"
              value={path}
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => changePath(event.target.value)}
              onKeyDown={(event) => {
                // Enter here looks at the path; it doesn't register.
                if (event.key === 'Enter') {
                  event.preventDefault()
                  void detect()
                }
              }}
            />
            <button
              type="button"
              className="secondary"
              onClick={() => void detect()}
              disabled={trimmedPath === '' || detection.status === 'loading'}
            >
              {detection.status === 'loading' ? 'Detecting…' : 'Detect'}
            </button>
          </div>
        </div>

        {detection.status === 'error' && (
          <div className="banner banner-error" role="alert">
            {detection.message}
          </div>
        )}

        {detection.status === 'done' && detectedThisPath && (
          <div className="detection" role="status">
            <p>
              <strong>{KIND_LABELS[detection.result.kind]}</strong> at <code>{detection.result.path}</code>
            </p>
            {detection.result.groups.length > 0 && (
              <ul>
                {detection.result.groups.map((group, index) => (
                  <li key={`${group.format}-${index}`}>
                    <strong>{group.format}</strong>: {group.file_count} {group.file_count === 1 ? 'file' : 'files'}
                    {group.total_bytes > 0 ? `, ${formatBytes(group.total_bytes)}` : ''}
                    {group.truncated ? ' (more not listed)' : ''}
                    {group.note !== '' && <span className="muted small"> {group.note}</span>}
                  </li>
                ))}
              </ul>
            )}
            {detection.result.walk_truncated && (
              <p className="muted small">Only part of the folder was looked at.</p>
            )}
            {nothingConvertible && (
              <div className="banner banner-warning">
                Nothing in here can be converted. You can still register it, but it can't be scanned until it has a
                readable format.
              </div>
            )}
          </div>
        )}

        <label>
          <span className="field-label">Name</span>
          <input
            type="text"
            value={name}
            maxLength={NAME_MAX_CHARS}
            onChange={(event) => {
              setName(event.target.value)
              setNameEdited(true)
            }}
          />
          <span className="muted small">
            Letters, digits, spaces, "." "_" and "-". It must start with a letter or digit, and be new in the team.
          </span>
        </label>

        <label>
          <span className="field-label">Description (optional)</span>
          <textarea rows={2} value={description} onChange={(event) => setDescription(event.target.value)} />
        </label>

        {error !== null && (
          <div className="banner banner-error" role="alert">
            {error}
          </div>
        )}

        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={onClose} disabled={registering}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!canRegister}>
            {registering ? 'Registering…' : 'Register'}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
