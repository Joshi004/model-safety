import { useState } from 'react'
import { getRow, getRowFromClean } from '../api'
import { basename, describePosition } from '../origin'
import { describeFate, describeStepOutcome, rowHash } from '../rows'
import type { RowPlace, RowStory, RowTarget } from '../types'
import { useRefreshing } from '../useRefreshing'
import { ShowOriginal } from './OriginalLookup'
import { DuplicatePanel, Findings } from './RowFindings'
import RowText from './RowText'

// The story of one row: where it came from, what each step did to it, what each
// scanner found in it (marked in its text), and whether it was removed or where it
// is in the clean data. Read-only.
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

function TimelineBox({ title, lines, tone }: { title: string; lines: string[]; tone?: 'ok' | 'removed' | 'muted' }) {
  return (
    <div className={`timeline-box${tone ? ` timeline-${tone}` : ''}`}>
      <div className="muted small">{title}</div>
      {lines.map((line) => (
        <div key={line} className="timeline-line">
          {line}
        </div>
      ))}
    </div>
  )
}

function Timeline({ story }: { story: RowStory }) {
  const { entry, summary } = story
  const origin = entry.origin
  const originLine =
    origin === null
      ? ['unknown']
      : [basename(origin.path), origin.line !== undefined ? `line ${origin.line}` : `row ${origin.row}`]
  const parts = [
    <TimelineBox key="original" title="Original" lines={originLine} />,
    <TimelineBox key="prepared" title="Prepared" lines={[entry.prepared.file, `line ${entry.prepared.line}`]} />,
  ]
  for (const step of entry.steps) {
    const reasons = (step.reasons ?? []).map((reason) => reason.reason)
    const findings = Object.values(step.scanners).reduce(
      (sum, scanner) => sum + (scanner.state === 'scanned' ? (scanner.findings?.length ?? 0) : 0),
      0,
    )
    const lines = [describeStepOutcome(step.outcome, reasons)]
    if (step.outcome !== 'not_reached') lines.push(`${findings.toLocaleString()} finding${findings === 1 ? '' : 's'}`)
    if (step.at) lines.push(`line ${step.at.line}`)
    parts.push(
      <TimelineBox
        key={`step${step.step}`}
        title={`Step ${step.step}`}
        lines={lines}
        tone={step.outcome === 'kept' ? 'ok' : step.outcome === 'removed' ? 'removed' : 'muted'}
      />,
    )
  }
  parts.push(
    summary.status === 'kept' ? (
      <TimelineBox
        key="final"
        title="Result"
        lines={['CLEAN', summary.clean ? `${summary.clean.file}, line ${summary.clean.line}` : '']
          .filter(Boolean)}
        tone="ok"
      />
    ) : (
      <TimelineBox key="final" title="Result" lines={['REMOVED', `at step ${summary.removed_at_step}`]} tone="removed" />
    ),
  )
  return (
    <div className="row-timeline">
      {parts.map((part, index) => (
        <div key={part.key} className="timeline-item">
          {index > 0 && (
            <span className="funnel-arrow" aria-hidden="true">
              →
            </span>
          )}
          {part}
        </div>
      ))}
    </div>
  )
}

function CopyLink({ target }: { target: RowTarget }) {
  const [link, setLink] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    const url = `${window.location.origin}${window.location.pathname}${rowHash(target)}`
    setLink(url)
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
    } catch {
      // No clipboard access here: the link stays in the box below to copy by hand.
      setCopied(false)
    }
  }

  return (
    <span className="copy-link">
      <button type="button" className="secondary" onClick={handleCopy}>
        Copy link
      </button>
      {link !== null && (
        <>
          {copied && <span className="muted small"> copied </span>}
          <input type="text" readOnly value={link} aria-label="Link to this row" onFocus={(event) => event.target.select()} />
        </>
      )}
    </span>
  )
}

function MetadataChips({ metadata }: { metadata: Record<string, unknown> | null }) {
  if (metadata === null) return null
  const chips = Object.entries(metadata).filter(
    ([key, value]) =>
      key !== '_origin' &&
      (typeof value === 'number' ||
        typeof value === 'boolean' ||
        (typeof value === 'string' && value.length > 0 && value.length <= 60 && !value.includes('\n'))),
  )
  if (chips.length === 0) return null
  return (
    <div className="chip-row">
      {chips.map(([key, value]) => (
        <span key={key} className="chip">
          <span className="chip-key">{key}</span> {String(value)}
        </span>
      ))}
    </div>
  )
}

export default function RowPage({
  target,
  onOpenRow,
}: {
  target: RowTarget
  onOpenRow: (target: RowTarget) => void
}) {
  const { data, error } = useRefreshing<RowStory>(
    () =>
      (target.from === 'clean' ? getRowFromClean : getRow)({
        chain_dir: target.chainDir,
        run: target.run,
        file: target.file,
        line: target.line,
      }),
    JSON.stringify(target),
    0,
    () => false,
  )

  // A link to another Prepared row of the same chain.
  function openPlace(place: RowPlace) {
    onOpenRow({ chainDir: target.chainDir, run: place.run, file: place.file, line: place.line, from: 'prepared' })
  }

  if (error) return <p className="error">{error}</p>
  if (data === null) return <p className="muted">Loading the row…</p>

  const { location, summary, origin } = data
  const preparedTarget: RowTarget = {
    chainDir: target.chainDir,
    run: location.run,
    file: location.file,
    line: location.line,
    from: 'prepared',
  }

  return (
    <div className="row-page">
      <div className="runs-list-header">
        <h2>
          Row: {location.run}, Prepared line {location.line}
        </h2>
        <CopyLink target={preparedTarget} />
      </div>
      <p className="muted small">
        <span className="mono location-text">{location.file}</span> · {describeFate(summary)}
      </p>
      {data.from_clean && (
        <div className="banner banner-ok small">
          Followed back from line {data.from_clean.clean.line} of the clean file {data.from_clean.clean.file} through the line maps:{' '}
          {data.from_clean.hops.map((hop) => `step ${hop.step}, line ${hop.line} → ${hop.parent.line}`).join('; ')}.{' '}
          {data.from_clean.fingerprint_matches
            ? 'The line map and the row status agree on the row.'
            : 'The line map and the row status do NOT agree on the row.'}
        </div>
      )}

      <Timeline story={data} />

      {data.prepared.problem !== null && <div className="banner banner-error small">{data.prepared.problem}</div>}
      {data.prepared.fingerprint_ok && (
        <p className="muted small">The Prepared line has the same fingerprint as when it was scanned.</p>
      )}
      <MetadataChips metadata={data.metadata} />

      <div className="row-columns">
        <div className="row-column">
          <h3>The row</h3>
          {data.segments === null ? (
            <p className="muted">The Prepared line isn't shown (see above).</p>
          ) : (
            <RowText segments={data.segments} highlights={data.highlights} flags={data.message_flags} />
          )}
        </div>
        <div className="row-column">
          <Findings story={data} onOpenRow={openPlace} />
        </div>
      </div>

      <DuplicatePanel story={data} onOpenRow={openPlace} />

      <div className="row-original">
        <h3>Original</h3>
        {data.entry.origin ? (
          <p className="small">
            <span className="mono location-text">{data.entry.origin.path}</span>,{' '}
            {origin ? describePosition(origin) : data.entry.origin.line !== undefined ? `line ${data.entry.origin.line}` : `row ${data.entry.origin.row}`}
          </p>
        ) : (
          <p className="muted">This row has no origin note.</p>
        )}
        {origin && data.prepared.run_dir !== null && (
          <ShowOriginal key={JSON.stringify(location)} origin={origin} runDir={data.prepared.run_dir} />
        )}
        {!origin && <p className="muted small">The Prepared line isn't shown, so its Original can't be looked up here.</p>}
      </div>
    </div>
  )
}
