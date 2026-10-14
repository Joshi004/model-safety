import { useState } from 'react'
import { extractContentText, roleHeading } from '../describe'
import {
  getConversationTurns,
  getGroundTruth,
  getReferenceOptions,
  getTarget,
  parseToolDefinitions,
  splitReasoning,
  splitToolCalls,
  tryParseJson,
} from '../readable'
import type { ParsedToolCall, ToolDefinition } from '../readable'
import type { PreviewRow } from '../types'
import ValueView, { StringValue } from './ValueView'

// The "Readable" view of a sampled row: a reconstructed conversation
// (tools, tool calls, reasoning, reference answer) alongside exactly what
// the real PII/toxicity scanner reads from this same row. The two can
// disagree -- e.g. a verl row's conversation lives in `prompt`, a field
// the real scanner never looks at -- and that disagreement is exactly
// what this view exists to surface, never to hide.
//
// Safety: every piece of real data below renders as plain React text (a
// string inside `{...}`, which React escapes) -- never
// dangerouslySetInnerHTML. Markup found in the data (e.g. a literal
// "<script>" in some row's text) is displayed as text, never interpreted.

// `messages`/`prompt` (whichever holds the conversation) and `text` get
// their own full rendering elsewhere in this view (sections 3 and 5) --
// excluded here so they don't also show up a second time, as a flat
// dump, in the chips row or the generic "other fields" section.
const EXCLUDED_FROM_GENERIC = new Set(['messages', 'prompt', 'text'])

function isChipValue(value: unknown): value is string | number | boolean {
  if (typeof value === 'number' || typeof value === 'boolean') return true
  if (typeof value === 'string') return value.length > 0 && value.length <= 60 && !value.includes('\n')
  return false
}

// ---- Scanner coverage: always first, always visible --------------------

function ScannerCoverage({ written }: { written: PreviewRow | undefined }) {
  const [showText, setShowText] = useState(false)

  if (!written) {
    return (
      <p className="muted small">
        This row wasn't written with the current mapping, so there's no real scanned text to show here --
        see the row tab above, or "Fields &amp; output", for why.
      </p>
    )
  }

  return (
    <div className="scanner-coverage">
      <div className="scanner-coverage-header">
        <strong>
          Scanner reads {written.scanned_text_length.toLocaleString()} char
          {written.scanned_text_length === 1 ? '' : 's'} from this row
        </strong>
        <button type="button" className="link-button" onClick={() => setShowText((prev) => !prev)}>
          {showText ? 'hide exact scanned text' : 'show exact scanned text'}
        </button>
      </div>
      {showText && <pre className="scanned-text">{written.scanned_text || '(empty)'}</pre>}
    </div>
  )
}

// ---- Section 1: short top-level values as chips -------------------------

function TopLevelChips({ record }: { record: Record<string, unknown> }) {
  const chips = Object.entries(record).filter(
    ([key, value]) => !EXCLUDED_FROM_GENERIC.has(key) && isChipValue(value),
  )
  if (chips.length === 0) return null

  return (
    <div className="chip-row">
      {chips.map(([key, value]) => (
        <span key={key} className="record-chip">
          <span className="record-chip-key">{key}:</span> {String(value)}
        </span>
      ))}
    </div>
  )
}

// ---- Section 2: tool definitions -----------------------------------------

function ToolDefinitionsSection({ definitions }: { definitions: ToolDefinition[] }) {
  if (definitions.length === 0) return null

  return (
    <div className="record-section">
      <h4>
        Tools available to the model ({definitions.length})
      </h4>
      {definitions.map((definition, index) => (
        <details key={index} className="tool-def-card">
          <summary className="mono">{definition.name}</summary>
          {definition.description && <p className="muted small">{definition.description}</p>}
          {definition.parameters !== null ? (
            <ValueView value={definition.parameters} depth={1} />
          ) : (
            <pre className="raw-json small">{definition.raw}</pre>
          )}
        </details>
      ))}
    </div>
  )
}

// ---- Section 3: the conversation ------------------------------------------

function ToolCallCard({ call }: { call: ParsedToolCall }) {
  if (call.functionName === null) {
    // Doesn't match the usual <function=...> shape -- shown raw rather
    // than silently dropped.
    return <pre className="raw-json small">{call.raw}</pre>
  }
  return (
    <div className="tool-call-card">
      <span className="mono">{call.functionName}</span>
      {call.arguments && call.arguments.length > 0 && (
        <div className="tool-call-args">
          {call.arguments.map((argument) => (
            <div key={argument.key} className="value-entry small">
              <span className="value-key">{argument.key}:</span> <StringValue value={argument.value} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function ConversationTurnBody({ turn }: { turn: Record<string, unknown> }) {
  const role = typeof turn.role === 'string' ? turn.role.toLowerCase() : ''
  const contentText = extractContentText(turn.content)

  if (role === 'system') {
    // Collapsed by default -- this is usually the longest turn (tool
    // definitions, instructions) and rarely what someone's checking a
    // row for.
    return (
      <details className="system-prompt-collapse">
        <summary className="muted small">
          System prompt ({contentText.length.toLocaleString()} chars) -- click to show
        </summary>
        <pre className="scanned-text">{contentText || '(empty)'}</pre>
      </details>
    )
  }

  if (role === 'tool') {
    const parsedJson = tryParseJson(contentText)
    if (parsedJson !== null) {
      return <pre className="raw-json">{JSON.stringify(parsedJson, null, 2)}</pre>
    }
    return <StringValue value={contentText} />
  }

  const { reasoning, visibleText } = splitReasoning(contentText, turn)
  const { toolCalls, remainingText } = splitToolCalls(visibleText)

  return (
    <>
      {reasoning && (
        <details className="reasoning-collapse">
          <summary className="muted small">Reasoning ({reasoning.length.toLocaleString()} chars)</summary>
          <p className="reasoning-text">{reasoning}</p>
        </details>
      )}
      {toolCalls.map((call, index) => (
        <ToolCallCard key={index} call={call} />
      ))}
      {remainingText && <StringValue value={remainingText} />}
      {!reasoning && toolCalls.length === 0 && !remainingText && <span className="value-empty">(empty)</span>}
    </>
  )
}

function ConversationTurnCard({ turn, index }: { turn: Record<string, unknown>; index: number }) {
  const contentText = extractContentText(turn.content)
  // Everything on a turn besides role/content -- reasoning_content and a
  // structured tool_calls field both land here -- is real data the
  // scanner never reads (extract_record_text() only reads role/content).
  const extraKeys = Object.keys(turn).filter((key) => key !== 'role' && key !== 'content')

  return (
    <div className="turn-card">
      <div className="turn-header">
        <span className="turn-role-pill">{roleHeading(turn.role, index)}</span>
        {!contentText && <span className="muted small">not read -- content is empty on this turn</span>}
      </div>
      <div className="turn-content">
        <ConversationTurnBody turn={turn} />
      </div>
      {extraKeys.length > 0 && (
        <div className="turn-extra muted small">not read by the scanner: {extraKeys.join(', ')}</div>
      )}
    </div>
  )
}

function ConversationSection({ turns }: { turns: Record<string, unknown>[] }) {
  return (
    <div className="record-section">
      <h4>
        Conversation ({turns.length} turn{turns.length === 1 ? '' : 's'})
      </h4>
      {turns.map((turn, index) => (
        <ConversationTurnCard key={index} turn={turn} index={index} />
      ))}
    </div>
  )
}

// ---- Section 4: reference answer ------------------------------------------

function GroundTruthValue({ groundTruth }: { groundTruth: string }) {
  const parsed = tryParseJson(groundTruth)
  if (parsed !== null && (Array.isArray(parsed) || typeof parsed === 'object')) {
    return <pre className="raw-json small">{JSON.stringify(parsed, null, 2)}</pre>
  }
  return <StringValue value={groundTruth} />
}

function ReferenceAnswerSection({ record }: { record: Record<string, unknown> }) {
  const groundTruth = getGroundTruth(record)
  const target = getTarget(record)
  const options = getReferenceOptions(record, target)

  if (groundTruth === null && target === null && options === null) return null

  return (
    <div className="record-section">
      <h4>Reference answer</h4>
      {groundTruth !== null && (
        <div className="value-entry">
          <span className="value-key">reward_model.ground_truth:</span>{' '}
          <GroundTruthValue groundTruth={groundTruth} />
        </div>
      )}
      {target !== null && (
        <div className="value-entry">
          <span className="value-key">target:</span> <StringValue value={target} />
        </div>
      )}
      {options && (
        <div className="reference-options">
          {options.map((option, index) => (
            <div key={index} className={option.isCorrect ? 'reference-option correct' : 'reference-option'}>
              {option.isCorrect && <span className="reference-option-mark">correct:</span>} {option.text}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---- Section 5: plain document view (mid-training `text` rows) -----------

function DocumentSection({ text }: { text: string }) {
  return (
    <div className="record-section">
      <h4>Document text ({text.length.toLocaleString()} chars)</h4>
      <pre className="scanned-text">{text}</pre>
    </div>
  )
}

// ---- Section 6: remaining fields, through the existing ValueView ---------

function RemainingFieldsSection({ record }: { record: Record<string, unknown> }) {
  const keys = Object.keys(record).filter((key) => !EXCLUDED_FROM_GENERIC.has(key))
  if (keys.length === 0) return null

  return (
    <div className="record-section">
      <h4>Other fields</h4>
      {keys.map((key) => (
        <div key={key} className="field-card">
          <div className="field-card-header">
            <span className="field-name">{key}</span>
          </div>
          <div className="field-value">
            <ValueView value={record[key]} />
          </div>
        </div>
      ))}
    </div>
  )
}

export default function RecordView({
  record,
  written,
  showScannerCoverage = true,
}: {
  record: Record<string, unknown>
  // The real backend-computed preview.rows entry for this same row index
  // -- undefined when this row was skipped or errored instead of
  // written, in which case there's no real scanned_text to show.
  written: PreviewRow | undefined
  // The coverage note is about the preview's current mapping, so it's
  // hidden where a row is shown on its own (a skipped row's saved copy, or
  // an original looked up through its _origin note).
  showScannerCoverage?: boolean
}) {
  const turns = getConversationTurns(record)
  // <tools>...</tools> only ever shows up in the system prompt in every
  // sample this catalog was checked against, but concatenating every
  // turn's text (instead of assuming which one) still finds it without
  // having to guess which role holds it.
  const allTurnsText = turns ? turns.map((turn) => extractContentText(turn.content)).join('\n') : ''
  const toolDefinitions = parseToolDefinitions(allTurnsText)
  const documentText = typeof record.text === 'string' && record.text ? record.text : null

  return (
    <div className="record-view">
      {showScannerCoverage && <ScannerCoverage written={written} />}
      <TopLevelChips record={record} />
      <ToolDefinitionsSection definitions={toolDefinitions} />
      {turns && <ConversationSection turns={turns} />}
      <ReferenceAnswerSection record={record} />
      {documentText && <DocumentSection text={documentText} />}
      <RemainingFieldsSection record={record} />
    </div>
  )
}
