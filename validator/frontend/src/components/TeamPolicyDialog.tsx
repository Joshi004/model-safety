import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { errorMessage, getTeamPolicy, setTeamPolicy } from '../api'
import { formatTimestamp } from '../format'
import { policyProblem } from '../policy'
import type { TeamPolicy } from '../types'
import Dialog from './Dialog'
import PolicyEditor from './PolicyEditor'

// Shows and edits one team's toxicity policy: the one every dataset of the team follows until a
// policy is saved for that dataset alone.
export default function TeamPolicyDialog({
  team,
  onClose,
  onSaved,
}: {
  team: string
  onClose: () => void
  onSaved: (team: string) => void
}) {
  // Null until the policy has been read.
  const [policy, setPolicy] = useState<TeamPolicy | null>(null)
  const [text, setText] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  // Bumped to read the policy again after a failure.
  const [reloads, setReloads] = useState(0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // An answer that arrives after the dialog was closed, or after a newer request, is dropped.
    let stale = false
    getTeamPolicy(team)
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
  }, [team, reloads])

  const canSave = policy !== null && text !== policy.text && policyProblem(text) === null && !saving

  async function save() {
    if (!canSave) return
    setSaving(true)
    setError(null)
    try {
      const saved = await setTeamPolicy(team, text)
      onSaved(saved.team)
    } catch (failure) {
      setError(errorMessage(failure))
      setSaving(false)
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    void save()
  }

  return (
    <Dialog title={`Toxicity policy: ${team}`} onClose={onClose} busy={saving} wide>
      <form className="form" onSubmit={handleSubmit}>
        <p>
          Every {team} dataset is judged by this policy, unless a policy was saved for that dataset alone (on its
          Toxicity policy tab).
        </p>
        <p className="muted small">
          A message the first guard rates Unsafe or Controversial is checked again against this policy. A message the
          policy says is fine isn't counted as flagged. A change applies to scans started after you save it: a scan
          that has started keeps the policy it began with, and finished results don't change.
        </p>

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
          <>
            <p className="muted small">
              {policy.updated_at === null
                ? 'Not saved yet: this is the starting policy.'
                : `Last saved ${formatTimestamp(policy.updated_at)}.`}
            </p>
            <PolicyEditor
              label={`${team} toxicity policy`}
              value={text}
              saved={policy.text}
              disabled={saving}
              onChange={setText}
            />
          </>
        )}

        {error !== null && (
          <div className="banner banner-error" role="alert">
            {error}
          </div>
        )}

        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={onClose} disabled={saving}>
            {policy !== null && text !== policy.text ? 'Cancel' : 'Close'}
          </button>
          <button type="submit" className="primary" disabled={!canSave}>
            {saving ? 'Saving…' : 'Save team policy'}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
