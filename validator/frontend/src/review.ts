// The logic behind the batch review table: what a row holds, what it still
// needs from the backend, when it needs attention, and "apply this mapping to
// all". No React here, same style as mapping.ts.
//
// A row fills in step by step: inspect the path, choose a format (done for
// it when exactly one readable format is found), preview the sample with no
// mapping to get the columns and the suggested mapping, preview again with
// that mapping, and ask the backend for the suggested output folder.
// `pendingWork` says which of those a row still needs; a row with none left
// (or an error) is no longer loading.

import type { FoundEntry } from './catalog'
import { applySuggestion, collectKeysInOrder, setSkipIfEmpty, setSource, sourceFor, toRequestFlags } from './mapping'
import type { MappingState } from './mapping'
import type { FolderStatus, InspectGroup, InspectResult, PlannedRun, PreviewRequest, PreviewResponse } from './types'

// The same number of sample rows as the Inspect view's, so the suggested
// mapping is made from the same evidence.
const REVIEW_SAMPLE_ROWS = 2

// Every request of a row runs in the validator on the login node, so only
// this many are in flight at once across the whole table.
export const MAX_CONCURRENT_REQUESTS = 2

// A planned run in one of these can't be started as it is (the backend
// refuses them in /api/jobs/submit).
export const BLOCKING_STATUSES: FolderStatus[] = [
  'refused',
  'outside_root',
  'inside_run',
  'running',
  'different',
  'no_identity',
]

// ---- Running requests two at a time ----

export function createLimiter(max: number) {
  let running = 0
  const waiting: (() => void)[] = []

  function startNext() {
    if (running >= max) return
    const start = waiting.shift()
    if (start === undefined) return
    running += 1
    start()
  }

  return function run<T>(task: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      waiting.push(() => {
        Promise.resolve()
          .then(task)
          .then(resolve, reject)
          .finally(() => {
            running -= 1
            startNext()
          })
      })
      startNext()
    })
  }
}

// ---- The row ----

export interface ReviewRow {
  id: number
  path: string
  inspect: InspectResult | null
  // Index into inspect.groups of the format to convert; null until chosen.
  groupIndex: number | null
  // csv and json inside a folder need exactly one file.
  file: string | null
  // The latest preview, and the inputs (previewKey) it was made for. Its
  // sample rows and suggestion don't depend on the mapping, so it also
  // supplies the columns while a newer preview is loading.
  preview: PreviewResponse | null
  previewKey: string | null
  mapping: MappingState
  // The output folder under the runs root; null until the backend suggests one.
  folder: string | null
  runAgain: boolean
  error: string | null
}

// "Skip the row if this is empty" starts ticked in a batch, so a row with
// nothing to scan isn't written (Decisions still open, item 2). The Inspect
// view's own default is unticked.
export function batchStartMapping(): MappingState {
  return { destinations: {}, skipIfEmpty: { text: true, messages: true } }
}

let lastRowId = 0

export function newRow(path: string): ReviewRow {
  lastRowId += 1
  return {
    id: lastRowId,
    path,
    inspect: null,
    groupIndex: null,
    file: null,
    preview: null,
    previewKey: null,
    mapping: batchStartMapping(),
    folder: null,
    runAgain: false,
    error: null,
  }
}

// ---- Format and file ----

export function readableIndexes(inspect: InspectResult): number[] {
  return inspect.groups.flatMap((group, index) => (group.input_type ? [index] : []))
}

export function chosenGroup(row: ReviewRow): InspectGroup | null {
  if (row.inspect === null || row.groupIndex === null) return null
  return row.inspect.groups[row.groupIndex] ?? null
}

export function needsFile(row: ReviewRow): boolean {
  const group = chosenGroup(row)
  return group !== null && group.sample_target === 'file' && row.inspect?.kind !== 'file'
}

// A format is chosen, and its file too when it needs one.
function isComplete(row: ReviewRow): boolean {
  const group = chosenGroup(row)
  return group !== null && group.input_type !== null && (!needsFile(row) || row.file !== null)
}

// ---- What a row still needs ----

// Everything a preview's request is made from.
export function previewKey(row: ReviewRow): string | null {
  if (!isComplete(row)) return null
  const { map, require } = toRequestFlags(row.mapping)
  return JSON.stringify([row.groupIndex, row.file, map.text ?? null, map.messages ?? null, require])
}

export type Work = 'inspect' | 'preview' | 'folder'

// The next request this row needs, or null when it has none left. A row with
// an error waits until something about it is changed.
export function pendingWork(row: ReviewRow): Work | null {
  if (row.error !== null) return null
  if (row.inspect === null) return 'inspect'
  const key = previewKey(row)
  if (key !== null && row.previewKey !== key) return 'preview'
  if (row.folder === null && isComplete(row)) return 'folder'
  return null
}

// Names the pending request, so a response is only used while the row still
// wants exactly that.
export function workKey(row: ReviewRow): string | null {
  const work = pendingWork(row)
  return work === 'preview' ? `preview:${previewKey(row)}` : work
}

export function isLoading(row: ReviewRow): boolean {
  return pendingWork(row) !== null
}

export function previewRequest(row: ReviewRow): PreviewRequest | null {
  const group = chosenGroup(row)
  if (group === null || group.input_type === null) return null
  const { map, require, exclude_metadata } = toRequestFlags(row.mapping)
  return {
    path: row.path,
    input_type: group.input_type,
    file: row.file,
    max_rows: REVIEW_SAMPLE_ROWS,
    start_row: 0,
    split: 'train',
    map,
    require,
    exclude_metadata,
  }
}

// ---- Changing a row ----

export function receiveInspect(row: ReviewRow, inspect: InspectResult): ReviewRow {
  const readable = readableIndexes(inspect)
  return { ...row, inspect, groupIndex: readable.length === 1 ? readable[0] : null }
}

// The first preview of a format also starts the mapping from its suggestion.
export function receivePreview(row: ReviewRow, preview: PreviewResponse): ReviewRow {
  const mapping =
    row.preview === null ? applySuggestion(row.mapping, preview.suggested_mapping.map) : row.mapping
  return { ...row, preview, previewKey: previewKey(row), mapping }
}

export function receiveFolder(row: ReviewRow, folder: string): ReviewRow {
  return { ...row, folder }
}

export function receiveError(row: ReviewRow, message: string): ReviewRow {
  return { ...row, error: message }
}

// A different format or file means different columns, so the old mapping
// (made against the old ones) starts again.
function restartSample(row: ReviewRow, groupIndex: number | null, file: string | null): ReviewRow {
  return {
    ...row,
    groupIndex,
    file,
    preview: null,
    previewKey: null,
    mapping: batchStartMapping(),
    runAgain: false,
    error: null,
  }
}

export function chooseFormat(row: ReviewRow, groupIndex: number | null): ReviewRow {
  return groupIndex === row.groupIndex ? row : restartSample(row, groupIndex, null)
}

export function chooseFile(row: ReviewRow, file: string | null): ReviewRow {
  return file === row.file ? row : restartSample(row, row.groupIndex, file)
}

export function editMapping(row: ReviewRow, mapping: MappingState): ReviewRow {
  return { ...row, mapping, runAgain: false, error: null }
}

export function editFolder(row: ReviewRow, folder: string): ReviewRow {
  return { ...row, folder, runAgain: false }
}

// Clears an error so the row asks again.
export function retry(row: ReviewRow): ReviewRow {
  return { ...row, error: null }
}

// ---- Turning a row into a run ----

export function columnsOf(row: ReviewRow): string[] {
  return row.preview === null ? [] : collectKeysInOrder(row.preview.input_rows)
}

export function displayName(row: ReviewRow, found: FoundEntry | null): string {
  if (found !== null) return found.entry.label
  return row.path.replace(/\/+$/, '').split('/').pop() || row.path
}

// The run without a folder: what the folder suggestion is asked for. A
// catalog entry also sends its team, group and label, which decide the
// suggested name. Null until a format (and file) is chosen.
export function plannedRunWithoutFolder(row: ReviewRow, found: FoundEntry | null): PlannedRun | null {
  const group = chosenGroup(row)
  if (group === null || group.input_type === null || !isComplete(row)) return null
  return {
    label: found?.entry.label ?? null,
    team: found?.team.team ?? null,
    group: found?.group.name ?? null,
    input_path: row.path,
    input_type: group.input_type,
    file: row.file,
    split: 'train',
    hf_config: '',
    ...toRequestFlags(row.mapping),
  }
}

// What /api/jobs/check and /api/jobs/submit take for this row; null until
// the row has a format and a folder.
export function plannedRun(row: ReviewRow, found: FoundEntry | null): PlannedRun | null {
  const run = plannedRunWithoutFolder(row, found)
  if (run === null || row.folder === null) return null
  return { ...run, run_folder: row.folder, run_again: row.runAgain }
}

// ---- Needs attention ----

// Why this row can't be started as it is. Empty while it is still loading,
// and when it's fine. Mapping hints are shown elsewhere and never count here.
export function attentionReasons(row: ReviewRow): string[] {
  if (row.error !== null) return [row.error]
  if (isLoading(row) || row.inspect === null) return []
  if (readableIndexes(row.inspect).length === 0) {
    return ['Nothing here can be converted: no format this tool reads was found.']
  }
  const group = chosenGroup(row)
  if (group === null) return ['Choose the format to convert.']
  if (needsFile(row) && row.file === null) return [`Choose the ${group.format} file to use.`]
  if (row.preview === null || row.previewKey !== previewKey(row)) return []

  const reasons: string[] = []
  if (sourceFor(row.mapping, 'text') === null && sourceFor(row.mapping, 'messages') === null) {
    reasons.push('Map a column to text or to messages.')
  }
  if (row.preview.preview.fatal_if_run) {
    const hint = row.preview.preview.write_errors[0]?.hint
    reasons.push(
      `The preview says this mapping would stop the run${hint ? `: ${hint}` : '.'} ` +
        'Left-out columns ("Drop") aren\'t offered here: start this dataset from the single-dataset view.',
    )
  }
  return reasons
}

// ---- Apply this mapping to all ----

export interface ApplyToAllResult {
  rows: ReviewRow[]
  appliedPaths: string[]
  leftAlone: { path: string; reason: string }[]
}

// Copies one row's `text` and `messages` sources and its "skip if empty"
// boxes onto every other row whose sample has the columns it names. A target
// the source row doesn't map is cleared on the others, so they end up with
// the same mapping as the source. Every other row is left as it is.
export function applyMappingToAll(rows: ReviewRow[], fromId: number): ApplyToAllResult {
  const source = rows.find((row) => row.id === fromId)
  if (source === undefined) return { rows, appliedPaths: [], leftAlone: [] }

  const textSource = sourceFor(source.mapping, 'text')
  const messagesSource = sourceFor(source.mapping, 'messages')
  const needed = [textSource, messagesSource].filter((column): column is string => column !== null)
  const appliedPaths: string[] = []
  const leftAlone: { path: string; reason: string }[] = []

  const updated = rows.map((row) => {
    if (row.id === fromId) return row
    if (row.preview === null) {
      leftAlone.push({ path: row.path, reason: "its sample isn't loaded" })
      return row
    }
    const columns = columnsOf(row)
    const missing = needed.filter((column) => !columns.includes(column))
    if (missing.length > 0) {
      leftAlone.push({ path: row.path, reason: `no ${missing.map((column) => `'${column}'`).join(' or ')} column` })
      return row
    }
    let mapping = setSource(row.mapping, 'text', textSource)
    mapping = setSource(mapping, 'messages', messagesSource)
    mapping = setSkipIfEmpty(mapping, 'text', source.mapping.skipIfEmpty.text)
    mapping = setSkipIfEmpty(mapping, 'messages', source.mapping.skipIfEmpty.messages)
    appliedPaths.push(row.path)
    return editMapping(row, mapping)
  })

  return { rows: updated, appliedPaths, leftAlone }
}
