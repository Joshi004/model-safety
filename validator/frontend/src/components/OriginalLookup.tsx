import { useEffect, useState } from 'react'
import { ApiError, lookupOrigin } from '../api'
import { STATUS_LABELS, describeLocation, printCommand, statusSentence } from '../origin'
import type { LookupResponse, LookupStatus, Origin } from '../types'
import CommandBox from './CommandBox'
import RecordView from './RecordView'

// The original row an `_origin` note points at, looked up through
// /api/origin, as it is now. Pass `runDir` when the run is known (a skipped
// entry), or `preparedPath` for a Prepared row shown outside a run -- the
// backend then finds its run by walking up from that file.
//
// Safety: the path, the row and every message below are real data and
// render as plain React text, never as markup.

interface Outcome {
  key: string
  result: LookupResponse | null
  error: string | null
}

const STATUS_BANNER: Record<LookupStatus, string> = {
  unchanged: 'banner banner-ok',
  changed: 'banner banner-warning',
  not_found: 'banner banner-error',
}

export function OriginalLookup({
  origin,
  runDir,
  preparedPath,
}: {
  origin: Origin
  runDir?: string
  preparedPath?: string
}) {
  // What was asked for, so an answer for an older request is never shown
  // for the current one -- and "loading" is just "no answer for this yet".
  const requestKey = JSON.stringify([origin, runDir ?? null, preparedPath ?? null])
  const [outcome, setOutcome] = useState<Outcome | null>(null)

  useEffect(() => {
    let cancelled = false
    lookupOrigin({ origin, run_dir: runDir ?? null, prepared_path: preparedPath ?? null })
      .then((response) => {
        if (!cancelled) setOutcome({ key: requestKey, result: response, error: null })
      })
      .catch((caught: unknown) => {
        const message = caught instanceof ApiError ? caught.message : String(caught)
        if (!cancelled) setOutcome({ key: requestKey, result: null, error: message })
      })
    return () => {
      cancelled = true
    }
  }, [requestKey, origin, runDir, preparedPath])

  const current = outcome?.key === requestKey ? outcome : null
  const loading = current === null
  const result = current?.result ?? null
  const error = current?.error ?? null

  const command = printCommand(origin)

  return (
    <div className="original-lookup">
      <p className="small">
        <strong>Where it is:</strong> <span className="mono location-text">{describeLocation(origin)}</span>
      </p>
      {command && (
        <CommandBox
          command={command}
          title="Print this line"
          note="Prints that one line of the original file. It only reads; nothing is changed."
        />
      )}
      {loading && <p className="muted small">Looking up the original…</p>}
      {error && <p className="error">{error}</p>}
      {result && (
        <>
          <div className={STATUS_BANNER[result.status]}>
            <strong>{STATUS_LABELS[result.status]}.</strong> {statusSentence(result.status, result.path)}
            {result.note && <div className="small">{result.note}</div>}
          </div>
          {result.file_differences.length > 0 && (
            <div className="banner banner-warning small">
              The file is different from when this run read it:
              <ul className="lookup-differences">
                {result.file_differences.map((difference) => (
                  <li key={difference}>{difference}</li>
                ))}
              </ul>
            </div>
          )}
          {result.row && <RecordView record={result.row} written={undefined} showScannerCoverage={false} />}
          {result.raw_line !== null && (
            <>
              <p className="muted small">This line isn't a JSON object, so it's shown as it is:</p>
              <pre className="raw-json">{result.raw_line || '(empty line)'}</pre>
            </>
          )}
        </>
      )}
    </div>
  )
}

export function ShowOriginal({
  origin,
  runDir,
  preparedPath,
}: {
  origin: Origin
  runDir?: string
  preparedPath?: string
}) {
  const [open, setOpen] = useState(false)

  return (
    <div className="show-original">
      <button type="button" className="secondary" onClick={() => setOpen((previous) => !previous)}>
        {open ? 'Hide original' : 'Show original'}
      </button>
      {open && <OriginalLookup origin={origin} runDir={runDir} preparedPath={preparedPath} />}
    </div>
  )
}
