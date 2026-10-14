import { useState } from 'react'

// A static, informational card: the exact two shapes
// modelsafety/contract/textract.py's extract_record_text() reads, and its
// exact rules for each. Nothing here is sample data -- every value is a
// type placeholder like "<string>", describing the contract itself, not
// pretending to be a real row. Keep this in sync with textract.py if that
// function's rules ever change; see HOW_DATA_CONVERSION_WORKS.md section 1
// for the same rules explained in more depth.

export default function TargetFormat() {
  const [open, setOpen] = useState(false)

  return (
    <div className="target-format">
      <button type="button" className="target-format-toggle" onClick={() => setOpen((prev) => !prev)}>
        {open ? '\u25be' : '\u25b8'} Target format: the shape every scanner reads
      </button>
      {open && (
        <div className="target-format-body">
          <p className="muted small">
            This is the only shape the scanner's text extractor looks for. A row can have other
            fields too (they just sit in <code>metadata</code>, see below) -- these two are the only
            ones that get scanned.
          </p>

          <div className="target-shape">
            <div className="target-shape-name">Shape 1 — plain text</div>
            <pre className="target-shape-example">{'{\n  "text": "<string>"\n}'}</pre>
            <p className="small">
              Read only if it's a non-empty string. If both <code>text</code> and{' '}
              <code>messages</code> are set on the same row, <code>text</code> wins and{' '}
              <code>messages</code> is ignored.
            </p>
          </div>

          <div className="target-shape">
            <div className="target-shape-name">Shape 2 — a conversation</div>
            <pre className="target-shape-example">
              {'{\n  "messages": [\n    { "role": "<string>", "content": "<string>" },\n    ...\n  ]\n}'}
            </pre>
            <p className="small">
              Only <code>content</code> is read from each turn. <code>role</code> becomes the
              heading shown to the scanner (e.g. "USER:"), or "MESSAGE_N" if a turn has no role. Any
              other field on a turn (e.g. <code>tool_calls</code>) is not read.
            </p>
          </div>

          <p className="small">
            Everything else you keep is written into <code>metadata</code> on the converted row --
            it's saved to disk, but never scanned.
          </p>
        </div>
      )}
    </div>
  )
}
