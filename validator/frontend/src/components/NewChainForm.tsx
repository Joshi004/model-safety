import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, createChain, getPreparedRoots } from '../api'
import { formatCount } from '../format'
import { useRefreshing } from '../useRefreshing'

// Makes a new chain: a folder of its own under the chains root, named by you, that reads
// its starting data from a folder of finished Prepared runs. Nothing runs yet: the steps
// are started one at a time from the chain's page. It never changes an existing folder.
//
// Safety: every value shown here comes from files on disk and renders as plain React
// text, never as markup.

const OTHER = '__other__'

export default function NewChainForm({
  onCreated,
  onCancel,
}: {
  onCreated: (chainDir: string) => void
  onCancel: () => void
}) {
  const { data, error } = useRefreshing(() => getPreparedRoots(), 'prepared-roots', 0, () => false)
  const [name, setName] = useState('')
  const [choice, setChoice] = useState('')
  const [otherPath, setOtherPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const candidates = data?.candidates ?? []
  const selected = choice || candidates[0]?.path || OTHER
  const preparedRoot = selected === OTHER ? otherPath.trim() : selected

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setMessage(null)
    setBusy(true)
    try {
      const made = await createChain(name.trim(), preparedRoot)
      onCreated(made.chain_dir)
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="row-finder new-chain" onSubmit={handleSubmit}>
      <h3>New chain</h3>
      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data && (
        <>
          <div className="start-resources">
            <div className="start-field">
              <label htmlFor="new-chain-name">Name</label>
              <input
                id="new-chain-name"
                type="text"
                value={name}
                placeholder="my_chain"
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setName(event.target.value)}
              />
              <div className="muted small">
                Becomes the folder <span className="mono">{data.chains_root}/{name || '<name>'}</span>. Letters, digits, dots, dashes
                and underscores.
              </div>
            </div>
            <div className="start-field">
              <label htmlFor="new-chain-source">Starting data (finished Prepared runs)</label>
              <select id="new-chain-source" value={selected} onChange={(event) => setChoice(event.target.value)}>
                {candidates.map((candidate) => (
                  <option key={candidate.path} value={candidate.path} disabled={candidate.unfinished > 0}>
                    {candidate.label}: {formatCount(candidate.runs)} runs, {formatCount(candidate.rows)} rows
                    {candidate.unfinished > 0 ? ` (${candidate.unfinished} not finished)` : ''}
                  </option>
                ))}
                <option value={OTHER}>Another folder…</option>
              </select>
            </div>
          </div>
          {selected === OTHER && (
            <div className="start-field">
              <label htmlFor="new-chain-path">Folder of finished Prepared runs (absolute path)</label>
              <input id="new-chain-path" type="text" value={otherPath} autoComplete="off" spellCheck={false} onChange={(event) => setOtherPath(event.target.value)} />
            </div>
          )}
          <p className="muted small">
            A chain starts from finished runs only. The folder is read, never changed; the chain's own folder holds everything its
            steps write.
          </p>
        </>
      )}
      <div className="start-actions">
        <button type="submit" disabled={busy || name.trim() === '' || preparedRoot === ''}>
          {busy ? 'Making…' : 'Make chain'}
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {message !== null && <p className="error job-error">{message}</p>}
    </form>
  )
}
