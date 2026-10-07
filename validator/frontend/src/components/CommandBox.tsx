import { useState } from 'react'

export default function CommandBox({
  command,
  title = 'Matching command',
  note = 'This box never runs anything: copy the command to run it yourself, or use "Start conversion" below to submit the same conversion as a SLURM job.',
}: {
  command: string
  title?: string
  note?: string
}) {
  const [copied, setCopied] = useState(false)

  return (
    <div className="command-box">
      <h3>{title}</h3>
      <pre className="command-text">{command}</pre>
      <button
        type="button"
        className="secondary"
        onClick={() => {
          navigator.clipboard
            .writeText(command)
            .then(() => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1500)
            })
            .catch(() => {
              /* clipboard access denied -- the text is still selectable above */
            })
        }}
      >
        {copied ? 'Copied!' : 'Copy'}
      </button>
      <p className="muted small">{note}</p>
    </div>
  )
}
