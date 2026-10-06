import { useEffect, useRef, useState } from 'react'
import './App.css'
import { ApiError, getHealth, getSources, inspectPath, runPreview } from './api'
import { findEntry } from './catalog'
import CommandBox from './components/CommandBox'
import DatasetHeader from './components/DatasetHeader'
import FormatPicker from './components/FormatPicker'
import InputFields from './components/InputFields'
import OutputPreview, { describeRowStatus } from './components/OutputPreview'
import RecordView from './components/RecordView'
import RowNavigator from './components/RowNavigator'
import Sidebar from './components/Sidebar'
import { applySuggestion, emptyMapping, toRequestFlags } from './mapping'
import type { MappingState } from './mapping'
import { loadJSON, saveJSON } from './storage'
import type { InspectGroup, InspectResult, PreviewResponse, TeamCatalog } from './types'

// Fixed, not user-configurable: the plan is to show 1-2 real rows clearly
// rather than a large sample, since the data in a given file is assumed
// similar row-to-row. Matches backend/app.py's own DEFAULT_SAMPLE_ROWS.
const SAMPLE_ROWS = 2

// localStorage key for the Verified mark -- unchanged from when this
// lived entirely inside SourcePicker.tsx, so anything already ticked
// before this file moved the state up to here stays ticked.
const VERIFIED_KEY = 'sourcePicker.reviewedPaths'

export default function App() {
  const [backendOk, setBackendOk] = useState<boolean | null>(null)
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

  async function handleInspect(path: string) {
    setActivePath(path)
    setInspectLoading(true)
    setInspectError(null)
    setInspectResult(null)
    setSelectedGroup(null)
    setSelectedFile(null)
    setMapping(emptyMapping())
    setSelectedRowIndex(0)
    setStartRow(0)
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
    setStartRow(0)
  }

  // From RowNavigator's jump form: move the sample window to start at
  // newStartRow, and show whatever lands at position 0 within it.
  function handleJump(newStartRow: number) {
    setStartRow(newStartRow)
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
    // selectedFile/currentPath/mapping/startRow are exactly the request's
    // inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedGroup, selectedFile, currentPath, mapping, startRow])

  function handleApplySuggestion() {
    if (!previewResult) return
    setMapping((current) => applySuggestion(current, previewResult.suggested_mapping.map))
  }

  const suggestedEntries = previewResult ? Object.entries(previewResult.suggested_mapping.map) : []
  const sampleSize = previewResult?.preview.sample_size ?? 0
  const rowIndexes = Array.from({ length: sampleSize }, (_, index) => index)
  const foundEntry = activePath ? findEntry(sourceTeams, activePath) : null

  return (
    <div className="app">
      <header>
        <h1>Input validator</h1>
        <p className="muted">
          Read-only. Shows what modelsafety/readers/prepare_rows.py would do to a path's data, without
          converting anything.
        </p>
        {backendOk === false && (
          <div className="banner banner-error">
            Couldn't reach the backend. Make sure <code>./validator/run.sh</code> is still running, and open
            the exact URL it printed.
          </div>
        )}
      </header>

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
                            elsewhere (RecordView, OutputPreview, InputFields) -- display-only. */}
                        Row {startRow + index + 1}: {describeRowStatus(previewResult.preview, index)}
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
  )
}
