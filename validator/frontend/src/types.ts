// Mirrors validator/backend's actual JSON responses -- see detect.py and
// preview.py for where each shape comes from. Kept in one file since the
// backend and frontend aren't built from a shared schema (no codegen).

export interface HealthResponse {
  ok: boolean
}

// backend/sources.py's curated Tools-team / medpsy catalog -- see
// GET /api/sources. `screening` on an entry is always already resolved
// (the entry's own note, or its group's, whichever applies) -- there's no
// need to fall back to the group's `screening` on the frontend.
export interface SourceEntry {
  label: string
  path: string
  description: string
  screening: string
  available: boolean
  unavailable_reason: string | null
}

export interface SourceGroupInfo {
  name: string
  description: string
  screening: string
  note: string | null
  entries: SourceEntry[]
}

export interface TeamCatalog {
  team: string
  groups: SourceGroupInfo[]
}

export interface SourcesResponse {
  teams: TeamCatalog[]
}

export interface InspectGroup {
  format: string
  input_type: string | null
  sample_target: 'folder' | 'file' | null
  files: string[]
  file_count: number
  total_bytes: number
  truncated: boolean
  note: string
}

export interface InspectResult {
  kind: 'file' | 'directory' | 'huggingface_dataset'
  path: string
  groups: InspectGroup[]
  walk_truncated?: boolean
}

export interface MappingHint {
  field: string
  severity: string
  message: string
}

// `map` is the only suggestion the backend returns now -- a plain object
// `{target: source}`, e.g. `{"messages": "conversations"}`. There's no
// `require` suggestion anymore: whether a target is required is a plain
// "skip the row if this is empty" checkbox the user sets themselves (see
// mapping.ts), not something the backend guesses.
export interface SuggestedMapping {
  map: Record<string, string>
  hints: MappingHint[]
}

export interface SkippedRow {
  row_index: number
  missing_column: string | null
}

export interface WriteError {
  row_index: number
  error: string
  likely_column: string | null
  exclude_metadata_key: string | null
  hint: string
}

export interface PreviewRow {
  row_index: number
  prepared: Record<string, unknown>
  scanned_text: string
  scanned_text_length: number
}

export interface PreviewResult {
  sample_size: number
  written: number
  skipped: number
  skipped_rows: SkippedRow[]
  write_errors: WriteError[]
  fatal_if_run: boolean
  fatal_note: string
  rows: PreviewRow[]
  rows_with_text: number
  coverage: number
}

export interface SourceInfo {
  expected_rows: number | null
  expected_rows_from: string
  files_discovered: number | null
  sampled_from_file: string | null
  // jsonl only: the file each sampled row came from, in sample order (a
  // sample of a folder can cross files). Needed to find a Prepared row's run.
  row_files?: string[]
  declared_schema: unknown | null
}

// A sampled row exactly as `_prepare_row()` itself receives it -- the same
// real data, just made JSON-safe for the browser (backend/preview.py's
// `to_display()`: a value that would break `json.dumps` is replaced with
// a small `{__non_json_value__: {type, repr}}` marker -- see describe.ts
// for how that's recognized and labeled). Same order, same length, and
// same row_index values as `preview.rows` / `preview.skipped_rows` /
// `preview.write_errors` index into.
export type InputRow = Record<string, unknown>

export interface PreviewResponse {
  source_info: SourceInfo
  input_rows: InputRow[]
  suggested_mapping: SuggestedMapping
  preview: PreviewResult
  command: string
}

export interface PreviewRequest {
  path: string
  input_type: string
  file?: string | null
  split?: string
  max_rows?: number
  // 0-indexed. Which row to start the sample at -- lets the row
  // navigator jump into the middle of a file instead of always seeing
  // the first `max_rows` rows. Defaults to 0 on the backend if omitted.
  start_row?: number
  // jsonl only: start at the line that begins at this byte instead of at
  // `start_row` (which counts non-blank rows, not lines).
  start_byte?: number | null
  map?: Record<string, string>
  require?: string[]
  exclude_metadata?: string[]
  metadata_column?: string
  output_dir?: string
}

// ---- Prepared runs (backend/runs.py) ----

// An `_origin` note (modelsafety/contract/origin.py): where a Prepared row
// came from. A JSONL note has `line` and `byte`; every other format has
// `row`; an Arrow folder also has `split`. A note read with load_dataset
// has `config` (only if one was given).
export interface Origin {
  path: string
  line?: number
  byte?: number
  row?: number
  split?: string | null
  config?: string
  fingerprint: string
  v: number
}

// One entry of summary.json's `source.files` -- only the fields used here.
export interface RunSourceFile {
  path: string
  size_bytes: number
  modified: string
  fingerprint: string
  blank_lines?: number
}

// The parts of a prepare_rows.py summary.json the run view shows.
export interface RunSummary {
  created_at: string
  input: { path: string; type: string; map: Record<string, string>; require: string[] }
  source: { rows_read: number; files: RunSourceFile[] }
  rows: {
    written: number
    skipped: number
    skipped_file: {
      file: string
      entries: number
      by_reason: Record<string, number>
      by_file: Record<string, number>
    }
  }
  checks: { passed: boolean; failures: string[]; warnings: string[] }
}

export interface RunResponse {
  run_dir: string
  summary: RunSummary
  run_identity: Record<string, unknown> | null
}

// One line of skipped_rows.ndjson. `row` is the original row as the reader
// saw it (made JSON-safe).
export interface SkippedEntry {
  _origin: Origin
  reason: string
  column: string
  used_as: string
  row: Record<string, unknown>
}

export interface SkippedItem {
  // The entry's line in skipped_rows.ndjson, from 1.
  number: number
  entry: SkippedEntry
  preview: string
}

export interface SkippedPageRequest {
  run_dir: string
  page?: number
  page_size?: number
  reason?: string | null
  file?: string | null
}

export interface SkippedPageResponse {
  page: number
  page_size: number
  // null when both filters are set (summary.json only counts each alone).
  total: number | null
  has_next: boolean
  entries: SkippedItem[]
}

export type LookupStatus = 'unchanged' | 'changed' | 'not_found'

export interface LookupRequest {
  origin: Origin
  run_dir?: string | null
  prepared_path?: string | null
}

export interface LookupResponse {
  status: LookupStatus
  run_dir: string
  input_type: string
  path: string
  // What changed on disk since the run read the file (size, modified time).
  file_differences: string[]
  row: Record<string, unknown> | null
  // JSONL only: the line, when it isn't a JSON object.
  raw_line: string | null
  // Why there is no row, when there isn't one.
  note: string | null
}
