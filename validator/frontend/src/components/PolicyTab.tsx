import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { clearDatasetPolicy, errorMessage, getDatasetPolicy, setDatasetPolicy } from '../api'
import { formatTimestamp } from '../format'
import { policyProblem } from '../policy'
import { hrefFor } from '../route'
import type { Dataset, DatasetPolicy } from '../types'
import PolicyEditor from './PolicyEditor'

// The toxicity policy of one dataset: its team's, until a policy is saved for the dataset alone.
// Saving here makes that policy of its own; "Use the team's policy" removes it again.
export default function PolicyTab({ dataset }: { dataset: Dataset }) {
  // Null until the policy has been read.
  const [policy, setPolicy] = useState<DatasetPolicy | null>(null)
  const [text, setText] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  // Bumped to read the policy again after a failure.
  const [reloads, setReloads] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    // An answer that arrives after leaving the tab, or after a newer request, is dropped.
    let stale = false
    getDatasetPolicy(dataset.id)
      .then((loaded) => {
        if (stale) return
        setPolicy(loaded)
        setText(loaded.text)
        setLoadError(null)
      })
      .catch((failure: unknown) => {
        if (!stale) setLoadError(errorMessage(failure))
      })
    return () => {
      stale = true
    }
  }, [dataset.id, reloads])

  const changed = policy !== null && text !== policy.text
  const canSave = changed && policyProblem(text) === null && !busy

  // Runs one change to the saved policy, and shows what the server says the policy is now.
  async function change(action: () => Promise<DatasetPolicy>, done: string) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const updated = await action()
      setPolicy(updated)
      setText(updated.text)
      setNotice(done)
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!canSave) return
    void change(() => setDatasetPolicy(dataset.id, text), 'Saved. Scans started from now on use this policy for this dataset.')
  }

  return (
    <div className="card">
      <h2>Toxicity policy</h2>

      {policy === null && loadError === null && <p className="muted">Loading…</p>}
      {loadError !== null && (
        <div className="banner banner-error" role="alert">
          <span>Couldn't load the policy: {loadError}</span>
          <button type="button" className="secondary" onClick={() => setReloads((count) => count + 1)}>
            Try again
          </button>
        </div>
      )}

      {policy !== null && (
        <form className="form" onSubmit={handleSubmit}>
          {policy.source === 'team' ? (
            <p>
              This dataset follows the <strong>{policy.team}</strong> team's policy. When the team's policy changes, so
              does this one. Saving a policy below gives this dataset one of its own instead.
            </p>
          ) : (
            <p>
              This dataset has a policy of its own
              {policy.updated_at !== null && ` (saved ${formatTimestamp(policy.updated_at)})`}. Changes to the{' '}
              <strong>{policy.team}</strong> team's policy don't reach it.
            </p>
          )}
          <p className="muted small">
            A message the first guard rates Unsafe or Controversial is checked again against this policy. A message the
            policy says is fine isn't counted as flagged. A scan keeps a copy of the policy when it starts, so a later
            change never alters a scan that has started or finished: its result page shows the policy it used. The
            team's policy is edited on the <a href={hrefFor({ page: 'datasets' })}>Datasets page</a>, under the{' '}
            {policy.team} tab.
          </p>

          <PolicyEditor
            label="Policy for this dataset"
            value={text}
            saved={policy.text}
            disabled={busy}
            onChange={setText}
          />

          {error !== null && (
            <div className="banner banner-error" role="alert">
              {error}
            </div>
          )}
          {notice !== null && (
            <div className="banner banner-ok" role="status">
              {notice}
            </div>
          )}

          <div className="dialog-actions">
            {policy.source === 'dataset' && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() =>
                  void change(
                    () => clearDatasetPolicy(dataset.id),
                    `This dataset follows the ${policy.team} team's policy again.`,
                  )
                }
              >
                Use the team's policy
              </button>
            )}
            {changed && (
              <button type="button" className="secondary" disabled={busy} onClick={() => setText(policy.text)}>
                Discard changes
              </button>
            )}
            <button type="submit" className="primary" disabled={!canSave}>
              {busy ? 'Saving…' : 'Save for this dataset'}
            </button>
          </div>

          {policy.override_text !== null && (
            <details>
              <summary className="small">The {policy.team} team's policy, to compare</summary>
              <pre className="policy-view">{policy.team_text}</pre>
            </details>
          )}
        </form>
      )}
    </div>
  )
}
