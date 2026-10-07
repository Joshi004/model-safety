import type { Destination, MappingState, Target } from '../mapping'
import { collectKeysInOrder, destinationOf, setDestination, setSkipIfEmpty } from '../mapping'
import { describeValue, isChatTurnList } from '../describe'
import type { MappingHint } from '../types'
import ValueView from './ValueView'

// One card per top-level key, each with a real example value and a
// "Goes to" select -- the UI's only way to build --map/--require/
// --exclude-metadata (see ../mapping.ts for exactly how a selection here
// turns into those flags). Replaces the old StructureTree.tsx (an
// aggregate tree summarizing every sampled row) and MappingPanel.tsx (a
// free-text target=source textarea) with one view that shows a real row
// directly, next to the control for it.

const DESTINATION_LABELS: Record<Destination, string> = {
  metadata: 'Kept in metadata',
  text: 'Scanned as text',
  messages: 'Scanned as messages',
  drop: 'Dropped',
}

// A hint's `field` is either a bare top-level key ("conversations") or a
// dotted path into one ("responses_create_params.input") -- see
// backend/preview.py's suggest_mapping(). Either way, it belongs on the
// top-level key's card: that's the only key the UI can actually offer a
// "Goes to" choice for.
function hintsForKey(hints: MappingHint[], key: string): MappingHint[] {
  return hints.filter((hint) => hint.field === key || hint.field.startsWith(`${key}.`))
}

export default function InputFields({
  inputRows,
  selectedRowIndex,
  mapping,
  hints,
  onMappingChange,
}: {
  inputRows: Record<string, unknown>[]
  selectedRowIndex: number
  mapping: MappingState
  hints: MappingHint[]
  onMappingChange: (next: MappingState) => void
}) {
  const keys = collectKeysInOrder(inputRows)
  const row = inputRows[selectedRowIndex] ?? {}

  return (
    <div className="input-fields">
      <p className="muted small">
        Only a top-level key can be sent to text or messages below -- a key nested inside another
        object (shown as part of that object's value) can't be picked directly; its hint below
        explains why, if one applies.
      </p>
      {keys.length === 0 && <p className="muted">No fields found in the sample.</p>}
      {keys.map((key) => {
        const destination = destinationOf(mapping, key)
        const present = key in row
        const keyHints = hintsForKey(hints, key)

        return (
          <div key={key} className={`field-card field-card-${destination}`}>
            <div className="field-card-header">
              <span className="field-name">{key}</span>
              <span className="muted small">
                {present ? describeValue(row[key]) : 'not present in this row'}
              </span>
            </div>

            <div className="field-card-controls">
              <label className="inline">
                Goes to
                <select
                  value={destination}
                  onChange={(event) =>
                    onMappingChange(setDestination(mapping, key, event.target.value as Destination))
                  }
                >
                  <option value="metadata">{DESTINATION_LABELS.metadata}</option>
                  <option value="text">{DESTINATION_LABELS.text}</option>
                  <option value="messages">{DESTINATION_LABELS.messages}</option>
                  <option value="drop">{DESTINATION_LABELS.drop}</option>
                </select>
              </label>
              {(destination === 'text' || destination === 'messages') && (
                <label className="inline small">
                  <input
                    type="checkbox"
                    checked={mapping.skipIfEmpty[destination]}
                    onChange={(event) =>
                      onMappingChange(setSkipIfEmpty(mapping, destination as Target, event.target.checked))
                    }
                  />
                  Skip the row if this is empty
                </label>
              )}
            </div>

            {keyHints.map((hint, index) => (
              <p key={index} className="field-hint small">
                {hint.message}
              </p>
            ))}

            {present && isChatTurnList(row[key]) && (
              <p className="field-note small">
                Shown below as a conversation: each turn's <code>role</code> becomes a heading and{' '}
                <code>content</code> is what's read -- always in that order, regardless of which
                order the source data's own keys happen to be in. Any other field on a turn isn't
                read.
              </p>
            )}

            {present && (
              <div className="field-value">
                <ValueView value={row[key]} />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
