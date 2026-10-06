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
  map?: Record<string, string>
  require?: string[]
  exclude_metadata?: string[]
  metadata_column?: string
  output_dir?: string
}
