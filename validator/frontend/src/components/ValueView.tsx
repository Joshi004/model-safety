import { useState } from 'react'
import {
  NON_JSON_KEY,
  describeValue,
  extractContentText,
  isChatTurnList,
  isNonJsonValue,
  roleHeading,
  truncateText,
} from '../describe'

// Readable, collapsible renderer for a real value -- a sampled input row,
// or a real converted output row. Every label comes from describeValue()
// (plain language, computed from the actual value), long text/lists are
// clipped with a "show more" control rather than dumped in full, and a
// backend-flagged non-JSON value (see describe.ts) gets its own marker
// instead of silently looking like a normal string.

const MAX_LIST_ITEMS_SHOWN = 5

// Exported so RecordView.tsx can show a turn's text with the exact same
// "show all N chars" clipping as every other string in the app, instead
// of a second, separately-maintained copy of the same few lines.
export function StringValue({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false)
  if (value.trim().length === 0) {
    return <span className="value-empty">(blank text)</span>
  }
  const { shown, truncated, totalLength } = truncateText(value)
  return (
    <span className="value-string">
      "{expanded ? value : shown}
      {!expanded && truncated ? '…' : ''}"{' '}
      {truncated && (
        <button type="button" className="link-button" onClick={() => setExpanded((prev) => !prev)}>
          {expanded ? 'show less' : `show all ${totalLength.toLocaleString()} chars`}
        </button>
      )}
    </span>
  )
}

function ListView({ items, depth }: { items: unknown[]; depth: number }) {
  const [open, setOpen] = useState(depth < 2)
  const [showAll, setShowAll] = useState(false)
  if (items.length === 0) return <span className="value-empty">(empty list)</span>

  const visibleCount = showAll ? items.length : Math.min(items.length, MAX_LIST_ITEMS_SHOWN)
  const hiddenCount = items.length - visibleCount

  return (
    <div className="value-node">
      <button type="button" className="value-toggle" onClick={() => setOpen((prev) => !prev)}>
        {open ? '\u25be' : '\u25b8'} {describeValue(items)}
      </button>
      {open && (
        <div className="value-children">
          {items.slice(0, visibleCount).map((item, index) => (
            <div key={index} className="value-entry">
              <span className="value-index muted small">{index + 1}.</span>{' '}
              <ValueView value={item} depth={depth + 1} />
            </div>
          ))}
          {hiddenCount > 0 && (
            <button type="button" className="link-button" onClick={() => setShowAll(true)}>
              show {hiddenCount} more
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// A list of {role, content} turns -- rendered as an actual conversation
// (role heading, then content, always in that fixed order) instead of a
// generic "object · keys: ..." dump. The order shown here is deliberately
// fixed, regardless of which order the source data's own keys happen to
// be in (a parquet/arrow struct's field order is a storage detail, not
// meaningful content -- extract_record_text() reads both keys by name,
// never by position, so this reordering changes nothing about what's
// actually read). Any field on a turn besides role/content is still
// shown, but labeled as not read -- matching TargetFormat.tsx's rules
// exactly.
//
// "View as raw list" bypasses all of that and hands these same items
// straight to ListView -- the exact, unmodified renderer every other
// list/object in the app gets -- so there's always a one-click way to see
// every key (including one this view only names, like a tool_calls field)
// with its real value, exactly as stored, to check this friendlier view
// never summarized away something that matters.
function ChatTurnList({ items, depth }: { items: Record<string, unknown>[]; depth: number }) {
  const [open, setOpen] = useState(depth < 2)
  const [showAll, setShowAll] = useState(false)
  const [showRaw, setShowRaw] = useState(false)

  const visibleCount = showAll ? items.length : Math.min(items.length, MAX_LIST_ITEMS_SHOWN)
  const hiddenCount = items.length - visibleCount

  if (showRaw) {
    return (
      <div className="value-node">
        <button type="button" className="link-button" onClick={() => setShowRaw(false)}>
          back to conversation view
        </button>
        <ListView items={items} depth={depth} />
      </div>
    )
  }

  return (
    <div className="value-node">
      <button type="button" className="value-toggle" onClick={() => setOpen((prev) => !prev)}>
        {open ? '\u25be' : '\u25b8'} {describeValue(items)}
      </button>
      {open && (
        <div className="value-children">
          <button type="button" className="link-button" onClick={() => setShowRaw(true)}>
            view as raw list instead (same rendering every other field uses)
          </button>
          {items.slice(0, visibleCount).map((turn, index) => {
            const scannedFromTurn = extractContentText(turn.content)
            const extraKeys = Object.keys(turn).filter((key) => key !== 'role' && key !== 'content')
            return (
              <div key={index} className="turn-card">
                <div className="turn-header">
                  <span className="turn-role-pill">{roleHeading(turn.role, index)}</span>
                  {!scannedFromTurn && (
                    <span className="muted small">not read -- content is empty on this turn</span>
                  )}
                </div>
                <div className="turn-content">
                  <ValueView value={turn.content} depth={depth + 1} />
                </div>
                {extraKeys.length > 0 && (
                  <div className="turn-extra muted small">
                    not read by the scanner: {extraKeys.join(', ')}
                  </div>
                )}
              </div>
            )
          })}
          {hiddenCount > 0 && (
            <button type="button" className="link-button" onClick={() => setShowAll(true)}>
              show {hiddenCount} more
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function ObjectView({ value, depth }: { value: Record<string, unknown>; depth: number }) {
  const entries = Object.entries(value)
  const [open, setOpen] = useState(depth < 2)
  if (entries.length === 0) return <span className="value-empty">(empty object)</span>

  return (
    <div className="value-node">
      <button type="button" className="value-toggle" onClick={() => setOpen((prev) => !prev)}>
        {open ? '\u25be' : '\u25b8'} {describeValue(value)}
      </button>
      {open && (
        <div className="value-children">
          {entries.map(([key, child]) => (
            <div key={key} className="value-entry">
              <span className="value-key">{key}:</span>{' '}
              <ValueView value={child} depth={depth + 1} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function ValueView({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (isNonJsonValue(value)) {
    const marker = value[NON_JSON_KEY]
    return (
      <span className="value-nonjson" title={marker.repr}>
        {marker.type} (not plain JSON) — {marker.repr}
      </span>
    )
  }
  if (value === null || value === undefined) {
    return <span className="value-empty">(empty)</span>
  }
  if (typeof value === 'string') {
    return <StringValue value={value} />
  }
  if (typeof value === 'boolean') {
    return <span className="value-bool">{String(value)}</span>
  }
  if (typeof value === 'number') {
    return <span className="value-number">{value}</span>
  }
  if (Array.isArray(value)) {
    if (isChatTurnList(value)) {
      return <ChatTurnList items={value} depth={depth} />
    }
    return <ListView items={value} depth={depth} />
  }
  return <ObjectView value={value as Record<string, unknown>} depth={depth} />
}
