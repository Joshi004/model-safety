import { useId } from 'react'
import { formatCount } from '../format'
import { POLICY_MAX_CHARS, policyProblem } from '../policy'

// The box a toxicity policy is edited in, with its length and a note when it differs from what
// is saved. Saving is up to the parent (the team dialog and the dataset tab save different
// things); it can ask `policyProblem` whether the text can be saved at all.
export default function PolicyEditor({
  label,
  value,
  saved,
  disabled,
  onChange,
}: {
  label: string
  value: string
  // The text that is saved now, to tell whether there is anything unsaved.
  saved: string
  disabled: boolean
  onChange: (value: string) => void
}) {
  const id = useId()
  const problem = policyProblem(value)
  const changed = value !== saved

  return (
    <div className="field policy-editor">
      <label htmlFor={id} className="field-label">
        {label}
      </label>
      <textarea
        id={id}
        className="mono"
        rows={16}
        value={value}
        spellCheck={false}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
      <div className="policy-meta small">
        <span className={problem !== null ? 'error' : 'muted'}>
          {problem ?? `${formatCount(value.trim().length)} of ${formatCount(POLICY_MAX_CHARS)} characters`}
        </span>
        {changed && <strong>Unsaved changes</strong>}
      </div>
    </div>
  )
}
