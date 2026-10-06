import { useState } from 'react'

export default function CommandBox({ command }: { command: string }) {
  const [copied, setCopied] = useState(false)

  return (
    <div className="command-box">
      <h3>Matching command</h3>
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
      <p className="muted small">
        This never runs automatically -- copy it and run it yourself when you're ready to actually convert.
      </p>
    </div>
  )
}
