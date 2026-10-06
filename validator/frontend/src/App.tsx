import { useEffect, useRef, useState } from 'react'
import './App.css'
import { ApiError, getHealth, getSources, inspectPath, runPreview } from './api'
import { findEntry } from './catalog'
import CommandBox from './components/CommandBox'
import DatasetHeader from './components/DatasetHeader'
import FormatPicker from './components/FormatPicker'
import InputFields from './components/InputFields'
import { ShowOriginal } from './components/OriginalLookup'
import OutputPreview, { describeRowStatus } from './components/OutputPreview'
import RecordView from './components/RecordView'
import RowNavigator from './components/RowNavigator'
import RunView from './components/RunView'
import Sidebar from './components/Sidebar'
import { applySuggestion, emptyMapping, toRequestFlags } from './mapping'
import type { MappingState } from './mapping'
import { getOrigin } from './origin'
import { loadJSON, saveJSON } from './storage'
import type { InspectGroup, InspectResult, Origin, PreviewResponse, TeamCatalog } from './types'

// Fixed, not user-configurable: the plan is to show 1-2 real rows clearly
// rather than a large sample, since the data in a given file is assumed
// similar row-to-row. Matches backend/app.py's own DEFAULT_SAMPLE_ROWS.
const SAMPLE_ROWS = 2

// localStorage key for the Verified mark -- unchanged from when this
// lived entirely inside SourcePicker.tsx, so anything already ticked
// before this file moved the state up to here stays ticked.
const VERIFIED_KEY = 'sourcePicker.reviewedPaths'

// Where a preview opened from a Prepared run starts, instead of at row 0.
// A JSONL note starts at its exact `byte` (`line` is only for the label);
// every other format starts at its `row`; an Arrow folder also has a `split`.
interface InspectStart {
  row: number
  byte: number | null
  line: number | null
  split: string | null
}

export default function App() {
  const [backendOk, setBackendOk] = useState<boolean | null>(null)
  const [view, setView] = useState<'inspect' | 'run'>('inspect')
  const [sourceTeams, setSourceTeams] = useState<TeamCatalog[]>([])

  // Client-only, never sent to the backend (see storage.ts) -- one
  // checkbox per dataset path, lifted up here from SourcePicker so the
  // same mark can be read and toggled from both the sidebar row and
  // DatasetHeader on the right.
  const [verified, setVerifiedState] = useState(() => loadJSON<Record<string, true>>(VERIFIED_KEY, {}))

  const [inspectResult, setInspectResult] = useState<InspectResult | null>(null)
  const [inspectError, setInspectError] = useState<string | null>(null)
  const [inspectLoading, setInspectLoading] = useState(false)
  const [currentPath, setCurrentPath] = useState('')
  // The path an inspect was last asked for, set the instant that happens
  // -- not only once it succeeds (unlike currentPath below). Lets the
  // sidebar highlight the clicked row, and DatasetHeader show its name,
  // right away instead of waiting on the network.
  const [activePath, setActivePath] = useState<string | null>(null)

  const [selectedGroup, setSelectedGroup] = useState<InspectGroup | null>(null)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)

  const [mapping, setMapping] = useState<MappingState>(emptyMapping())
  const [selectedRowIndex, setSelectedRowIndex] = useState(0)
  // 0-indexed. Which row the current sample starts at -- 0 unless the
  // row navigator's jump form has moved it. Reset alongside
  // selectedRowIndex/mapping whenever the dataset or format changes.
  const [startRow, setStartRow] = useState(0)
  // When set (jsonl only), the sample starts at the line beginning at this
  // byte, and startRow is ignored. Set only by "Open this row in the
  // preview"; the row navigator's jump form clears it.
  const [startByte, setStartByte] = useState<number | null>(null)
  const [split, setSplit] = useState('train')
  const [inspectStart, setInspectStart] = useState<InspectStart | null>(null)
  // "Readable" by default (see the plan this feature came from); not
  // reset on a new inspect -- it's a viewing preference, not something
  // tied to a particular dataset's shape.
  const [viewMode, setViewMode] = useState<'readable' | 'fields'>('readable')

  const [previewResult, setPreviewResult] = useState<PreviewResponse | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)

  // Guards against an older, slower request's response overwriting a
  // newer one -- e.g. two mapping changes in quick succession, where the
  // first request happens to come back after the second.
  const requestIdRef = useRef(0)

  useEffect(() => {
    getHealth()
      .then(() => setBackendOk(true))
      .catch(() => setBackendOk(false))

    // Independent of the health check above: if the catalog fails to
    // load for some reason, Sidebar/SourcePicker just render the
    // "Custom path" box open with nothing above it (teams.length === 0)
    // and the rest of the page still works.
    getSources()
      .then((response) => setSourceTeams(response.teams))
      .catch(() => setSourceTeams([]))
  }, [])

  // Re-reads storage first so a second browser tab open on the same
  // address can't silently undo this one's last toggle -- see
  // storage.ts's own note on why every call here is wrapped already.
  function setVerified(path: string, isVerified: boolean) {
    const latest = loadJSON<Record<string, true>>(VERIFIED_KEY, {})
    if (isVerified) latest[path] = true
    else delete latest[path]
    saveJSON(VERIFIED_KEY, latest)
    setVerifiedState(latest)
  }

  async function handleInspect(path: string, start: InspectStart | null = null) {
    setActivePath(path)
    setInspectStart(start)
    setInspectLoading(true)
    setInspectError(null)
    setInspectResult(null)
    setSelectedGroup(null)
    setSelectedFile(null)
    setMapping(emptyMapping())
    setSelectedRowIndex(0)
    setStartRow(0)
    setStartByte(null)
    setPreviewResult(null)
    setPreviewError(null)
    try {
      const result = await inspectPath(path)
      setInspectResult(result)
      setCurrentPath(path)
    } catch (error) {
      setInspectError(error instanceof ApiError ? error.message : String(error))
    } finally {
      setInspectLoading(false)
    }
  }

  function handleFormatSelect(group: InspectGroup, file: string | null) {
    setSelectedGroup(group)
    setSelectedFile(file)
    // A different dataset means the old mapping choices (made against the
    // old columns) no longer necessarily make sense -- start clean.
    setMapping(emptyMapping())
    setSelectedRowIndex(0)
    // Where the sample starts: row 0, unless this inspect was opened from a
    // Prepared run's _origin note (see handleOpenInPreview). Setting these
    // from inspectStart every time is safe to repeat.
    setStartRow(inspectStart?.row ?? 0)
    setStartByte(inspectStart?.byte ?? null)
    setSplit(inspectStart?.split ?? 'train')
  }

  // "Open this row in the preview", from the Prepared run view: inspect the
  // original file the note names, starting at the exact row.
  function handleOpenInPreview(origin: Origin) {
    const isJsonl = typeof origin.byte === 'number'
    setView('inspect')
    void handleInspect(origin.path, {
      row: isJsonl ? 0 : (origin.row ?? 0),
      byte: isJsonl ? (origin.byte ?? null) : null,
      line: origin.line ?? null,
      split: origin.split ?? null,
    })
  }

  // From RowNavigator's jump form: move the sample window to start at
  // newStartRow, and show whatever lands at position 0 within it.
  function handleJump(newStartRow: number) {
    setStartRow(newStartRow)
    setStartByte(null)
    setSelectedRowIndex(0)
  }

  // Re-runs the real preview whenever the selected data or the mapping
  // changes. No debounce: every change here is a deliberate click or
  // select, not a keystroke, so there's no "mid-typing" state to wait out.
  useEffect(() => {
    if (!selectedGroup || !selectedGroup.input_type) return
    const thisRequest = ++requestIdRef.current
    setPreviewLoading(true)
    setPreviewError(null)
    const { map, require, exclude_metadata } = toRequestFlags(mapping)
    runPreview({
      path: currentPath,
      input_type: selectedGroup.input_type,
      file: selectedFile,
      max_rows: SAMPLE_ROWS,
      start_row: startRow,
      start_byte: startByte,
      split,
      map,
      require,
      exclude_metadata,
    })
      .then((result) => {
        if (requestIdRef.current !== thisRequest) return
        setPreviewResult(result)
      })
      .catch((error: unknown) => {
        if (requestIdRef.current !== thisRequest) return
        // Nothing stale stays on screen behind an error.
        setPreviewResult(null)
        setPreviewError(error instanceof ApiError ? error.message : String(error))
      })
      .finally(() => {
        if (requestIdRef.current !== thisRequest) return
        setPreviewLoading(false)
      })
    // Deliberately not depending on anything else -- selectedGroup/
    // selectedFile/currentPath/mapping/startRow/startByte/split are
    // exactly the request's inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedGroup, selectedFile, currentPath, mapping, startRow, startByte, split])

  function handleApplySuggestion() {
    if (!previewResult) return
    setMapping((current) => applySuggestion(current, previewResult.suggested_mapping.map))
  }

  const suggestedEntries = previewResult ? Object.entries(previewResult.suggested_mapping.map) : []
  const sampleSize = previewResult?.preview.sample_size ?? 0
  const rowIndexes = Array.from({ length: sampleSize }, (_, index) => index)
  const foundEntry = activePath ? findEntry(sourceTeams, activePath) : null

  // "Show original": for a sampled row that carries an _origin note. The
  // backend finds the row's run from the Prepared file it was read from.
  const selectedRow = previewResult?.input_rows[selectedRowIndex]
  const selectedOrigin = selectedRow ? getOrigin(selectedRow) : null
  const selectedPreparedPath =
    previewResult?.source_info.row_files?.[selectedRowIndex] ?? previewResult?.source_info.sampled_from_file ?? null
  const startLabel =
    startByte === null
      ? undefined
      : inspectStart?.line
        ? `line ${inspectStart.line} (byte ${startByte})`
        : `byte ${startByte}`

  return (
    <div className="app">
      <header>
        <h1>Input validator</h1>
        <p className="muted">
          Read-only. Shows what modelsafety/readers/prepare_rows.py would do to a path's data, without
          converting anything.
        </p>
        <div className="row-tabs view-switch">
          <button
            type="button"
            className={view === 'inspect' ? 'row-tab selected' : 'row-tab'}
            onClick={() => setView('inspect')}
          >
            Inspect a dataset
          </button>
          <button
            type="button"
            className={view === 'run' ? 'row-tab selected' : 'row-tab'}
            onClick={() => setView('run')}
          >
            Prepared run
          </button>
        </div>
        {backendOk === false && (
          <div className="banner banner-error">
            Couldn't reach the backend. Make sure <code>./validator/run.sh</code> is still running, and open
            the exact URL it printed.
          </div>
        )}
      </header>

      <div hidden={view !== 'inspect'}>
        <div className="app-shell">
          <Sidebar
            teams={sourceTeams}
            loading={inspectLoading}
            verified={verified}
            onToggleVerified={setVerified}
            activePath={activePath}
            onPick={handleInspect}
            onInspect={handleInspect}
          />

          <main className="app-main">
            {activePath && (
              <DatasetHeader
                path={activePath}
                found={foundEntry}
                verified={!!verified[activePath]}
                onToggleVerified={setVerified}
              />
            )}

            {!activePath && (
              <p className="muted">
                Pick a dataset from the sidebar, or enter a path under "Custom path", to get started.
              </p>
            )}

            {inspectLoading && <p className="muted small">Inspecting…</p>}
            {inspectError && <p className="error">{inspectError}</p>}

            {inspectResult && (
              <FormatPicker
                groups={inspectResult.groups}
                isSingleFile={inspectResult.kind === 'file'}
                onSelect={handleFormatSelect}
              />
            )}

            {previewError && <p className="error">{previewError}</p>}

            {previewResult && (
              <div className={previewLoading ? 'preview-area loading' : 'preview-area'}>
                <div className="preview-toolbar">
                  <div className="toolbar-left">
                    <RowNavigator
                      sourceInfo={previewResult.source_info}
                      startRow={startRow}
                      sampleSize={sampleSize}
                      startLabel={startLabel}
                      onJump={handleJump}
                    />
                    <div className="row-tabs">
                      {rowIndexes.map((index) => (
                        <button
                          key={index}
                          type="button"
                          className={index === selectedRowIndex ? 'row-tab selected' : 'row-tab'}
                          onClick={() => setSelectedRowIndex(index)}
                        >
                          {/* startRow + index + 1 is the absolute row number shown to the user;
                              index itself stays the local, 0-based value row_index lookups use
                              elsewhere (RecordView, OutputPreview, InputFields) -- display-only.
                              When the sample starts at a byte, there's no row number to show. */}
                          {startByte !== null ? `Sample row ${index + 1}` : `Row ${startRow + index + 1}`}:{' '}
                          {describeRowStatus(previewResult.preview, index)}
                        </button>
                      ))}
                    </div>
                    <div className="row-tabs">
                      <button
                        type="button"
                        className={viewMode === 'readable' ? 'row-tab selected' : 'row-tab'}
                        onClick={() => setViewMode('readable')}
                      >
                        Readable
                      </button>
                      <button
                        type="button"
                        className={viewMode === 'fields' ? 'row-tab selected' : 'row-tab'}
                        onClick={() => setViewMode('fields')}
                      >
                        Fields &amp; output
                      </button>
                    </div>
                  </div>
                  {suggestedEntries.length > 0 && (
                    <div className="suggestion-bar small">
                      <span className="muted">Suggested:</span>{' '}
                      {suggestedEntries.map(([target, source]) => `${target} ← ${source}`).join(', ')}
                      <button type="button" className="secondary" onClick={handleApplySuggestion}>
                        Apply
                      </button>
                    </div>
                  )}
                </div>

                {selectedOrigin && selectedPreparedPath && (
                  <ShowOriginal
                    key={`${selectedPreparedPath}|${startRow}|${startByte}|${selectedRowIndex}|${selectedOrigin.byte ?? selectedOrigin.row}`}
                    origin={selectedOrigin}
                    preparedPath={selectedPreparedPath}
                  />
                )}

                {sampleSize === 0 ? (
                  <p className="muted">No rows in this sample.</p>
                ) : viewMode === 'readable' ? (
                  <>
                    <RecordView
                      record={previewResult.input_rows[selectedRowIndex] ?? {}}
                      written={previewResult.preview.rows.find((row) => row.row_index === selectedRowIndex)}
                    />
                    <CommandBox command={previewResult.command} />
                  </>
                ) : (
                  <div className="columns">
                    <div className="column">
                      <h2>Your input (row {selectedRowIndex + 1})</h2>
                      <InputFields
                        inputRows={previewResult.input_rows}
                        selectedRowIndex={selectedRowIndex}
                        mapping={mapping}
                        hints={previewResult.suggested_mapping.hints}
                        onMappingChange={setMapping}
                      />
                    </div>
                    <div className="column wide sticky-column">
                      <h2>Output (row {selectedRowIndex + 1})</h2>
                      <OutputPreview result={previewResult} mapping={mapping} selectedRowIndex={selectedRowIndex} />
                      <CommandBox command={previewResult.command} />
                    </div>
                  </div>
                )}
              </div>
            )}
          </main>
        </div>
      </div>

      <div hidden={view !== 'run'}>
        <RunView onOpenInPreview={handleOpenInPreview} />
      </div>
    </div>
  )
}
