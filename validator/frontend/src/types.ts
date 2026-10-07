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
  // huggingface input only: prepare_rows.py's --hf-config.
  hf_config?: string
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
// The counts depend on the input type: `lines` and `blank_lines` are JSONL
// only, `rows_read` is JSONL, JSON and CSV, `rows_in_footer` is Parquet and
// Arrow, and the last three are there for every input that has per-file
// counts (anything read with load_dataset has none).
export interface RunSourceFile {
  path: string
  size_bytes: number
  modified: string
  fingerprint: string
  lines?: number
  blank_lines?: number
  rows_read?: number
  rows_in_footer?: number
  rows_written?: number
  rows_skipped?: number
  rows_left_out?: number
}

// One entry of summary.json's `output.files_written`, in write order. `path`
// is relative to the run folder.
export interface WrittenFile {
  path: string
  rows: number
  bytes: number
}

// The parts of a prepare_rows.py summary.json the run view shows.
export interface RunSummary {
  created_at: string
  input: {
    path: string
    type: string
    map: Record<string, string>
    require: string[]
    max_rows: number | null
  }
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
  output: {
    files: number
    rows_on_disk: number
    bytes_on_disk: number
    files_written?: WrittenFile[]
  }
  text_coverage: { rows_with_text: number; coverage: number }
  checks: { passed: boolean; failures: string[]; warnings: string[] }
}

// run_identity.json: every command-line setting of the run, by name.
export interface RunIdentity {
  settings?: Record<string, unknown>
  created_at?: string
}

export interface RunResponse {
  run_dir: string
  summary: RunSummary
  run_identity: RunIdentity | null
}

// progress.json (modelsafety/readers/progress_file.py), written by
// prepare_rows.py while it runs. The first eight phases are in the order a
// run goes through them; `done` and `failed` end it.
export type ProgressPhase =
  | 'finding_files'
  | 'fingerprinting_files'
  | 'reading'
  | 'fingerprinting_rows'
  | 'preparing'
  | 'checking_text'
  | 'writing'
  | 'finishing'
  | 'done'
  | 'failed'

export interface RunProgress {
  tool: string
  phase: ProgressPhase
  started_at: string
  phase_started_at: string
  updated_at: string
  // What `done` and `total` count; null for CSV and JSON reading, which has no
  // counts, and for the end phases.
  unit: 'bytes' | 'files' | 'rows' | null
  done: number
  total: number | null
  files_done: number | null
  files_total: number | null
  rows_written: number
  rows_skipped: number
  error: string | null
}

// POST /api/run/progress. `progress` is null when the folder has no usable
// progress.json.
export interface RunProgressResponse {
  run_dir: string
  progress: RunProgress | null
  has_summary: boolean
  has_success: boolean
  may_have_stopped: boolean
}

// GET /api/runs: every prepare_rows.py run folder under the runs root.
export type RunStatus =
  | 'checks_passed'
  | 'checks_failed'
  | 'no_summary'
  | 'in_progress'
  | 'may_have_stopped'
  | 'failed'

export interface RunListItem {
  // Relative to the runs root.
  path: string
  run_dir: string
  created_at: string | null
  status: RunStatus
  // null when the run has no summary.json yet.
  rows_read: number | null
  written: number | null
  skipped: number | null
}

export interface RunsListResponse {
  root: string
  exists: boolean
  // True when the walk stopped early, so the list is incomplete.
  truncated: boolean
  runs: RunListItem[]
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

// ---- Starting and watching conversions (backend/jobs.py) ----

// What every job asks SLURM for. All four are always sent; there is no
// "leave it to SLURM's default" (see the note in ResourcesFields.tsx).
export interface JobResources {
  partition: string
  cpus: number
  mem: string
  time: string
}

// GET /api/jobs/settings.
export interface JobsSettings {
  root: string
  partitions: string[]
  default_resources: JobResources
  max_cpus: number
  // Set when sinfo couldn't be read (then `partitions` is empty).
  slurm_error: string | null
}

// One dataset to convert, as /api/jobs/check and /api/jobs/submit take it.
export interface PlannedRun {
  label?: string | null
  team?: string | null
  group?: string | null
  input_path: string
  input_type: string
  file?: string | null
  split?: string
  hf_config?: string
  map?: Record<string, string>
  require?: string[]
  exclude_metadata?: string[]
  // Relative to the runs root; null asks for the suggested folder name.
  run_folder?: string | null
  // submit only: run it again even if its folder holds the same finished run.
  run_again?: boolean
}

// What's already in a planned run's output folder.
export type FolderStatus =
  | 'outside_root'
  | 'inside_run'
  | 'running'
  | 'empty'
  | 'done_same'
  | 'same_unfinished'
  | 'different'
  | 'no_identity'
  | 'refused'

// The short summary of a run: from a folder's summary.json.
export interface RunBrief {
  created_at: string | null
  rows_read: number | null
  written: number | null
  skipped: number | null
  checks_passed: boolean
}

export interface FolderContents {
  file_count: number
  total_bytes: number
  summary: RunBrief | null
}

export interface CheckedRun {
  label: string
  // Relative to the runs root.
  folder: string
  // null when the folder name itself was refused.
  run_dir: string | null
  status: FolderStatus
  reason: string | null
  // `different` only: what changed, previous -> requested.
  differences: string[]
  contents: FolderContents | null
  // `running` only: the batch whose job is writing there, if any.
  batch_id: string | null
  prepare_args: string[] | null
  // The local command, and the sbatch command (only when resources were sent and valid).
  command: string | null
  sbatch_command: string | null
}

export interface CheckRequest {
  runs: PlannedRun[]
  resources?: Partial<JobResources> | null
}

// POST /api/jobs/check.
export interface CheckResponse {
  root: string
  // Set when squeue couldn't be read: starting and deleting are refused then.
  slurm_error: string | null
  runs: CheckedRun[]
  repeats: { folder: string; labels: string[] }[]
  resources: JobResources | null
  resources_error: string | null
}

export interface DeleteResponse {
  deleted: { run_dir: string; file_count: number; total_bytes: number }[]
}

export interface SubmitResponse {
  batch_id: string
  submitted: number
  skipped: number
  submit_errors: string[]
}

export type JobOutcome =
  | 'queued'
  | 'running'
  | 'stalled'
  | 'succeeded'
  | 'checks_failed'
  | 'failed'
  | 'cancelled'
  | 'skipped'
  | 'not_submitted'
  | 'unknown'

export interface JobStatus {
  index: number
  label: string | null
  input_path: string | null
  input_type: string | null
  run_dir: string | null
  // Relative to the runs root.
  folder: string | null
  slurm_job_id: string | null
  submitted_at: string | null
  skipped: string | null
  submit_error: string | null
  outcome: JobOutcome
  // SLURM's reason while queued, or why the outcome is what it is.
  reason: string | null
  slurm_state: string | null
  exit_code: string | null
  elapsed: string | null
  max_rss_bytes: number | null
  // Only from files written after the job was submitted.
  progress: RunProgress | null
  may_have_stopped: boolean
  summary: RunBrief | null
  out_log: string | null
  err_log: string | null
  out_tail: string | null
  err_tail: string | null
}

// POST /api/jobs/status.
export interface BatchStatusResponse {
  batch_id: string
  created_at: string | null
  resources: JobResources | null
  slurm_error: string | null
  jobs: JobStatus[]
  by_outcome: Partial<Record<JobOutcome, number>>
  has_active: boolean
}

export interface BatchListItem {
  batch_id: string
  created_at: string | null
  resources: JobResources | null
  job_count: number
  labels: (string | null)[]
  by_outcome: Partial<Record<JobOutcome, number>>
  has_active: boolean
}

// GET /api/jobs/batches.
export interface BatchesResponse {
  root: string
  exists: boolean
  slurm_error: string | null
  // Batch records that couldn't be read.
  problems: string[]
  batches: BatchListItem[]
}

export interface CancelResponse {
  cancelled: string[]
  not_active: string[]
}
