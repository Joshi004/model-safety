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

// POST /api/run for a Prepared run.
export interface RunResponse {
  kind: 'prepared'
  run_dir: string
  summary: RunSummary
  run_identity: RunIdentity | null
}

// ---- Filter runs (modelsafety/filter/filter_rows.py) ----

// The parts of a filter_rows.py summary.json the filter run view shows.
export interface FilterRunSummary {
  created_at: string
  // The run this one filtered: a Prepared run, or the filter run before it.
  parent: { run: string; run_dir: string; tool: string; scan_root: string; files: number }
  // The scanner results the filter read, by scanner (regex_pii, ner, toxicity, dedup).
  scanner_results: Record<string, { results_dir: string; tool: string; mode: string | null }>
  // What counts as a reason to remove a row.
  rules: Record<string, unknown>
  source: { rows_read: number }
  rows: { written: number; removed: number }
  // Rows removed, by reason (duplicate, ner, regex_pii, toxicity_unsafe, ...).
  removed_by_reason: Record<string, number>
  output: {
    files: number
    rows_on_disk: number
    bytes_on_disk: number
    files_written?: WrittenFile[]
  }
  checks: { passed: boolean; failures: string[]; warnings: string[] }
}

// POST /api/run for a filter run. `prepared_run_dir` is the Prepared run at the
// end of its parent runs, or null (with `prepared_run_problem`) when that can't
// be found.
export interface FilterRunResponse {
  kind: 'filter'
  run_dir: string
  summary: FilterRunSummary
  run_identity: RunIdentity | null
  prepared_run_dir: string | null
  prepared_run_problem: string | null
}

export type OpenedRun = RunResponse | FilterRunResponse

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

// GET /api/runs: every prepare_rows.py and filter_rows.py run folder under each
// folder the validator reads.
export type RunKind = 'prepared' | 'filter'

export type RunStatus =
  | 'checks_passed'
  | 'checks_failed'
  | 'no_summary'
  | 'in_progress'
  | 'may_have_stopped'
  | 'failed'

export interface RunListItem {
  // Relative to the root it was found under.
  path: string
  run_dir: string
  kind: RunKind
  created_at: string | null
  status: RunStatus
  // null when the run has no summary.json yet.
  rows_read: number | null
  written: number | null
  // A Prepared run skips rows and a filter run removes them: each is null for the other kind.
  skipped: number | null
  removed: number | null
}

// One folder the validator reads, and the runs found under it.
export interface RunRoot {
  // What the UI calls it: "Runs root", or a chain's name and step.
  label: string
  root: string
  exists: boolean
  // True when the walk stopped early, so the list is incomplete.
  truncated: boolean
  runs: RunListItem[]
}

export interface RunsListResponse {
  roots: RunRoot[]
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

// ---- Chains (GET /api/chains, POST /api/chain, POST /api/chain/step) ----

// Read from the files a stage leaves behind: nothing there, started but not
// marked finished (queued, running or stopped; the files can't say which), or
// finished. Where a step record says which, `unfinished` and `not_started` become
// queued, running or failed (SLURM's word for the stage's jobs).
export type StageState = 'not_started' | 'unfinished' | 'queued' | 'running' | 'failed' | 'finished'

export type StageKind = 'scan' | 'coverage' | 'filter' | 'trace' | 'row_status'

export interface StageProgress {
  done: number
  total: number
  // What done and total count: stages, pieces, shards or runs.
  unit: string
}

export interface ChainStage {
  key: string
  label: string
  kind: StageKind
  // The filter step the stage belongs to; null for the trace and the row status.
  step: number | null
  needs: string[]
  path: string | null
  state: StageState
  progress: StageProgress | null
  // A coverage report's, trace's or row status's verdict (pass or fail).
  verdict: string | null
  created_at?: string | null
  // Filter stages only: runs whose own checks failed.
  runs_failed?: string[]
  runs_failed_count?: number
  // Why the stage can't be started yet (empty when everything it needs is finished).
  blocked_by: string[]
  // Not started, everything it needs is finished, and the chain was made by the validator.
  ready: boolean
  // The newest step record for the stage, when it has one.
  job?: { record_id: string; outcome: StepOutcome; job_ids: string[] }
}

export interface ChainStepInfo {
  number: number
  output_root: string
  scan_root: string
  // null for a step that hasn't produced a run yet.
  rules: Record<string, unknown> | null
  scanners: Record<string, { results_dir: string; tool: string; mode: string | null }>
  coverage: { file: string; verdict: string | null; created_at: string | null } | null
}

export interface FunnelStep {
  step: number
  state: StageState
  kept: number | null
  removed: number | null
  removed_by_reason: Record<string, number> | null
}

export interface ChainFunnel {
  prepared: number
  prepared_runs: number
  steps: FunnelStep[]
  clean: number | null
}

export interface DatasetStep {
  step: number
  state: StageState
  rows_in: number | null
  kept: number | null
  removed: number | null
  // From the row status, so null until it is built.
  removed_by_reason: Record<string, number> | null
}

export interface ChainDataset {
  run: string
  run_dir: string
  prepared: number | null
  prepared_state: StageState
  steps: DatasetStep[]
}

export interface ChainTrace {
  dir: string
  finished: boolean
  verdict: string | null
  totals: { findings: number | null; by_status: Record<string, number> | null } | null
}

export interface ChainRowStatusTotals {
  prepared_rows: number | null
  kept: number | null
  removed: number | null
  removed_by_step: Record<string, number> | null
  removed_by_step_and_reason: Record<string, Record<string, number>> | null
  rows_without_origin: number | null
}

export interface ChainRowStatus {
  dir: string
  finished: boolean
  verdict: string | null
  report_version: number | null
  // Whether it has the index of removed rows the Removed page pages through.
  has_removed_index: boolean
  totals: ChainRowStatusTotals | null
}

export interface ChainResponse {
  chain_dir: string
  name: string
  version: number
  created_at: string | null
  // True for a chain the validator made, which can start its own steps.
  startable: boolean
  prepared_root: string
  last_filter_root: string
  problems: string[]
  steps: ChainStepInfo[]
  stages: ChainStage[]
  funnel: ChainFunnel
  datasets: ChainDataset[]
  trace: ChainTrace | null
  row_status: ChainRowStatus | null
}

export interface ChainListItem {
  chain_dir: string
  // A record that can't be used has only `problem`.
  problem?: string
  version?: number
  name?: string
  created_at?: string | null
  prepared_root?: string
  last_filter_root?: string
  startable?: boolean
  problems?: string[]
  funnel?: ChainFunnel
  stages?: Pick<ChainStage, 'key' | 'label' | 'state' | 'progress' | 'verdict'>[]
  trace?: { finished: boolean; verdict: string | null } | null
  row_status?: { finished: boolean; verdict: string | null } | null
}

export interface ChainsListResponse {
  root: string
  exists: boolean
  chains: ChainListItem[]
}

export interface ChainStepRequest {
  chain_dir: string
  stage: string
}

// What a coverage report says about one scanner.
export interface CoverageSection {
  file: string
  status: string | null
  reason?: string | null
  parts: { name: string; status: string; reason: string | null; files_affected: number }[]
  files: {
    expected: number | null
    read: number | null
    missing: ShortList<string>
    extra: ShortList<string>
    different: ShortList<string>
    not_finished: ShortList<string>
  }
  rows: { read: number | null; with_text: number | null; without_text: number | null; findings: number | null } | null
  details: Record<string, unknown> | null
  problems: ShortList<{ scanner: string | null; run: string | null; path: string | null; message: string }>
}

// The first items of a long list, and how many more there were.
export interface ShortList<T> {
  items: T[]
  more: number
}

export type Counts = Record<string, number>

export interface DedupDetails {
  mode: string | null
  input_mode: string | null
  files: number | null
  rows_total: number | null
  rows_hashed: number | null
  rows_without_text: number | null
  candidate_pairs: number | null
  duplicate_rows: number | null
  configuration: Record<string, unknown> | null
}

export interface RegexPiiDetails {
  stages_finished: number
  stages: number
  // Rows the filter removed for each category (null until the row status is built).
  removed_by_category: Counts | null
}

export interface NerDetails {
  created_at?: string | null
  expected_units: number | null
  completed_units: number | null
  rows_read: number | null
  rows_with_text: number | null
  rows_without_text: number | null
  messages: number | null
  findings: number | null
  spans: number | null
  policy_eligible_spans: number | null
  llm_candidates: number | null
  tiers: Counts | null
  policy_exclusions: Counts | null
  labels?: Counts | null
  missing_units?: number | null
}

export interface ToxicityDetails {
  shards: number | null
  shards_finished?: number
  shards_judged?: number
  rows_read?: number
  messages_scored?: number
  messages_flagged?: number
  guard_records?: number
  guard_errors?: number
  guard_unknown?: number
  verdicts?: Counts
  categories?: Counts
}

export interface CoverageStageDetails {
  report: string | null
  created_at?: string | null
  root?: string | null
  verdict?: string | null
  runs?: { found: number | null; checked: number | null; files: number | null; rows: number | null } | null
  scanners_checked?: string[]
  scanners_not_run?: string[]
  scanner_status?: Record<string, string | null>
  problems?: ShortList<{ scanner: string | null; run: string | null; path: string | null; message: string }>
}

export interface RowStatusCounts {
  by_reason: Counts
  by_ner_label: Counts
  by_regex_category: Counts
  by_toxicity_verdict: Counts
}

export interface FilterStageDetails {
  input_root: string
  output_root: string
  runs: number
  rows_read: number
  kept: number
  removed: number
  removed_by_reason: Counts
  rules: Record<string, unknown> | null
  // True if some run of the step used other rules from the first.
  rules_differ: boolean
  scanner_results: Record<string, { results_dir: string; tool: string; mode: string | null }> | null
  failed_runs: ShortList<{ run: string; failures: string[] }>
  row_status_counts: RowStatusCounts | null
}

export interface TraceStageDetails {
  // Only set (to null) when the trace isn't finished.
  summary?: null
  created_at?: string | null
  verdict?: string | null
  totals?: {
    findings: number | null
    distinct_lines_walked: number | null
    prepared_rows_looked_up: number | null
    by_status: Counts | null
  } | null
  sources?: Record<string, { findings: number | null; by_status: Counts | null }>
  checks?: { passed: boolean; failures: string[] } | null
}

export interface RowStatusStageDetails {
  // Only set (to null) when the row status isn't finished.
  summary?: null
  created_at?: string | null
  verdict?: string | null
  report_version?: number | null
  totals?: {
    prepared_rows: number | null
    kept: number | null
    removed: number | null
    removed_by_step: Counts | null
    removed_by_step_and_reason: Record<string, Counts> | null
    rows_without_origin: number | null
  } | null
  checks?: { passed: boolean; failures: string[] } | null
}

export interface ChainStepResponse {
  chain_dir: string
  stage: Pick<ChainStage, 'key' | 'label' | 'kind' | 'step' | 'path' | 'state' | 'progress' | 'verdict'>
  // For a scan: what the step's coverage report says about it.
  coverage: CoverageSection | null
  // Depends on the stage: see the interfaces above.
  details: unknown
}

// ---- Rows (POST /api/row, /api/row/from-clean, /api/row/from-original) ----

// Where a Prepared row is: its run (a path under the chain's Prepared folder),
// the file in the run, and the line (from 1).
export interface RowLocation {
  run: string
  file: string
  line: number
}

// One text a scanner reads from a row: its `text`, or one message's text.
// `message_index` is null for `text`.
export interface RowSegment {
  message_index: number | null
  role: string
  text: string
  // The full length; `text` is cut at a maximum size.
  length: number
  truncated: boolean
}

export type RowOutcome = 'kept' | 'removed' | 'not_reached'

export interface RowReason {
  reason: string
  scanner: string
  findings: Record<string, unknown>[]
}

export interface RowScannerEntry {
  // scanned: the scanner read the row. no_text: it found no text to read.
  // not_reached: an earlier step removed the row first.
  state: 'scanned' | 'no_text' | 'not_reached'
  findings?: Record<string, unknown>[]
  duplicated_by?: { row: RowPlace }[]
}

export interface RowStep {
  step: number
  outcome: RowOutcome
  // Where the row is in this step's output, when it was kept.
  at?: { file: string; line: number; byte_offset: number }
  reasons?: RowReason[]
  scanners: Record<string, RowScannerEntry>
}

// An entry of the chain's row status: everything known about one Prepared row.
export interface RowStatusEntry {
  prepared: { run: string; file: string; line: number; byte_offset: number; source_sha256: string }
  origin: { path: string; row?: number; line?: number; byte?: number } | null
  steps: RowStep[]
  final: { status: 'kept' | 'removed'; step?: number; run?: string; file?: string; line?: number; byte_offset?: number }
  not_scanned: { step: number; scanner: string; why: string }[]
}

export interface RowPlace {
  step?: number
  run: string
  file: string
  line: number
}

// A row's fate in a few fields.
export interface RowSummary {
  status: 'kept' | 'removed' | null
  removed_at_step: number | null
  reasons: string[]
  clean: { run: string; file: string; line: number; byte_offset: number } | null
  step_outcomes: { step: number; outcome: RowOutcome }[]
}

// A finding placed in the text as a character range of one message.
export interface RowHighlight {
  step: number
  scanner: 'ner' | 'regex_pii'
  message_index: number | null
  start: number
  end: number
  // True for a finding the filter named as a reason for removing the row.
  caused_removal: boolean
  // NER:
  label?: string
  tier?: string
  eligible?: boolean | null
  confidence?: number | null
  matched?: string
  // Regex PII:
  category?: string
  value?: string
}

// A toxicity finding: it marks a whole message.
export interface RowMessageFlag {
  step: number
  scanner: 'toxicity'
  message_index: number | null
  role: string | null
  verdict: string | null
  outcome: string | null
  error: string | null
  categories: string[] | null
  detoxify_score: number | null
  caused_removal: boolean
}

// A finding that couldn't be placed in the text, and why.
export interface RowUnplaced {
  step: number
  scanner: 'ner' | 'regex_pii' | 'toxicity'
  why: string
  message_index?: number | null
  label?: string
  start?: number
  end?: number
  category?: string
  value?: string
  verdict?: string | null
  caused_removal: boolean
}

// What became of the kept copy a duplicate was removed in favour of.
export interface DuplicateCopy {
  place: RowPlace
  story: RowSummary | null
  origin?: RowStatusEntry['origin']
  segments: RowSegment[] | null
  run_dir?: string | null
  problem: string | null
}

export interface RowStory {
  chain_dir: string
  location: RowLocation
  summary: RowSummary
  entry: RowStatusEntry
  prepared: { run_dir: string | null; fingerprint_ok: boolean | null; problem: string | null }
  // The full `_origin` note of the Prepared line; null when the line isn't shown.
  origin: Origin | null
  metadata: Record<string, unknown> | null
  // Null when the Prepared line couldn't be read (see prepared.problem).
  segments: RowSegment[] | null
  highlights: RowHighlight[]
  message_flags: RowMessageFlag[]
  unplaced: RowUnplaced[]
  findings_cut: boolean
  duplicate_of: DuplicateCopy[]
  duplicated_by: { items: RowPlace[]; more: number }
  // Only from /api/row/from-clean: the clean line and each hop back to the Prepared line.
  from_clean?: {
    clean: RowLocation
    hops: { step: number; file: string; line: number; parent: { file: string; line: number } }[]
    fingerprint_matches: boolean
  }
}

export interface RowRequest {
  chain_dir: string
  run: string
  file: string
  line: number
}

export interface OriginalRowRequest {
  chain_dir: string
  original_path: string
  row?: number | null
  line?: number | null
  split?: string | null
}

export interface OriginalRowMatch {
  status: 'found' | 'skipped' | 'left_out' | 'not_in_file' | 'not_found'
  method?: 'computed' | 'searched'
  // The Prepared run's path under the chain's Prepared folder.
  run: string
  run_dir: string
  prepared: { run_dir: string; file: string; line: number } | null
  note?: string | null
  error?: string
}

export interface OriginalRowResponse {
  path: string
  runs_checked: number
  matches: OriginalRowMatch[]
  row: RowStory | null
}

// A row to show: the chain, and the row's place, in the Prepared data or in the clean data.
export interface RowTarget extends RowLocation {
  chainDir: string
  from: 'prepared' | 'clean'
}

// ---- Removed rows (POST /api/removed/options, POST /api/removed) ----

// What the removed-rows index holds for one dataset and one filter step.
export interface RemovedCounts {
  entries: number | null
  by_reason: Counts
  by_ner_label: Counts
  by_regex_category: Counts
  by_toxicity_verdict: Counts
}

export interface RemovedRunOption {
  run: string
  rows: { prepared: number; kept: number; removed: number } | null
  // By filter step (as a string: "1", "2").
  steps: Record<string, RemovedCounts>
}

export interface RemovedOptionsResponse {
  chain_dir: string
  steps: number[]
  // The last filter step's output folder: the clean data, one run per dataset.
  clean_root: string | null
  has_removed_index: boolean
  runs: RemovedRunOption[]
}

export interface RemovedRequest {
  chain_dir: string
  run: string
  step: number
  reason?: string | null
  ner_label?: string | null
  regex_category?: string | null
  toxicity_verdict?: string | null
  page?: number
  page_size?: number
}

// One removed row: where it is in the Prepared data, why it was removed, what was found.
export interface RemovedEntry {
  run: string
  step: number
  file: string
  line: number
  byte_offset: number
  reasons: string[]
  ner_labels: Counts
  regex_categories: Counts
  toxicity_verdicts: Counts
  original: { path: string; row?: number; line?: number; byte?: number } | null
}

export interface RemovedPageResponse {
  run: string
  step: number
  page: number
  page_size: number
  // The rows that match the filters.
  total: number
  has_next: boolean
  entries: RemovedEntry[]
}

// Where the Removed page starts: a chain, a dataset and a filter step.
export interface RemovedFilters {
  chainDir: string
  run: string
  step: number
}

// ---- The steps of a chain as jobs (GET /api/steps/jobs, POST /api/steps/status, POST /api/steps/cancel) ----

export type StepOutcome = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'unknown'

export interface StepResources {
  partition: string
  // A step whose job scripts fix these says them too; a record may lack any of them.
  cpus?: number
  mem?: string
  time?: string
}

// One SLURM job a step started.
export interface StepJobView {
  job_id: string | null
  role: string | null
  depends_on: string[]
  outcome: StepOutcome
  reason: string | null
  slurm_state: string | null
  exit_code: string | null
  elapsed: string | null
  max_rss_bytes: number | null
  out_log: string | null
  err_log: string | null
  // Only in the full view.
  out_tail?: string | null
  err_tail?: string | null
}

export interface StepRecordView {
  id: string
  chain_dir: string
  chain: string
  stage: string
  label: string
  submitted_at: string | null
  resources: StepResources | null
  gpus: number
  inputs: string[] | null
  output: string | null
  outcome: StepOutcome
  reason: string | null
  // A step the backend ran itself (a coverage report): how its command ended.
  inline: { exit_code: number | null; [key: string]: unknown } | null
  warnings: string[]
  jobs: StepJobView[]
  has_active: boolean
  // Where the stage stands, from its own files.
  stage_state: StageState | null
  progress: StageProgress | null
  stage_verdict: string | null
  // Only in the full view.
  command_text?: string | null
  settings?: Record<string, unknown> | null
  wrapper_output?: string | null
  slurm_error?: string | null
}

export interface StepJobsResponse {
  root: string
  slurm_error: string | null
  problems: string[]
  records: StepRecordView[]
}

export interface StepCancelResponse {
  cancelled: string[]
  not_active: string[]
}

// ---- Making a chain and starting its steps (GET /api/steps/spec, POST /api/steps/check,
// POST /api/steps/submit, GET /api/chains/prepared-roots, POST /api/chains/create) ----

export interface StepFieldBase {
  name: string
  label: string
  help: string
}

export interface StepChoiceField extends StepFieldBase {
  type: 'choice'
  choices: string[]
  default: string
}

export interface StepIntegerField extends StepFieldBase {
  type: 'integer'
  min: number
  max: number
  default: number | null
}

// Names separated by commas.
export interface StepListField extends StepFieldBase {
  type: 'list'
  default: string[]
}

// Any of a fixed set of choices.
export interface StepMultiField extends StepFieldBase {
  type: 'multi'
  choices: string[]
  default: string[]
}

export type StepField = StepChoiceField | StepIntegerField | StepListField | StepMultiField

// What a job script asks for, from its #SBATCH lines.
export interface FixedResources {
  cpus: number | null
  mem: string | null
  time: string | null
  gpus: number
}

export interface StepStageSpec {
  label: string
  // wrapper: a script in scripts/; sbatch: a job script submitted directly; inline: run by the backend.
  mode: 'wrapper' | 'sbatch' | 'inline'
  // The default CPUs, memory and time; null where the job scripts fix them (or there is no job).
  resources: { cpus: number; mem: string; time: string } | null
  fixed_resources: Record<string, FixedResources> | null
  fields: StepField[]
}

export interface StepSpecResponse {
  partitions: string[]
  default_partition: string
  max_cpus: number
  slurm_error: string | null
  chains_root: string
  stages: Record<string, StepStageSpec>
}

export interface StepInputSummary {
  root: string
  runs_found: number
  runs_read: number
  runs_skipped: number
  files: number
  rows: number
  skipped: { kind: string; path: string; status: string | null; reason: string }[]
  skipped_more: number
}

export interface StepCost {
  gpus: number
  cpus: number | null
  mem: string | null
  time: string | null
  summary: string
}

// What a step would do, and what stops it: the answer of the check.
export interface StepPlan {
  chain_dir: string
  chain: string
  stage: string
  label: string
  mode: 'wrapper' | 'sbatch' | 'inline'
  state: StageState
  inputs: string[]
  input_summaries: StepInputSummary[]
  output: string
  // new: not there yet. empty: a folder with nothing in it. has_files: something is there already.
  output_state: 'new' | 'empty' | 'has_files'
  settings: Record<string, unknown>
  resources: { partition: string; cpus?: number; mem?: string; time?: string } | null
  cost: StepCost | null
  needs_gpu_confirmation: boolean
  // Jobs the step waits for before it starts.
  dependency: string[]
  command: string[] | null
  command_text: string | null
  blocked: string[]
  warnings: string[]
  can_start: boolean
}

export interface StepCheckRequest {
  chain_dir: string
  stage: string
  settings: Record<string, unknown>
  resources: Record<string, unknown>
}

export interface StepSubmitRequest extends StepCheckRequest {
  confirm_gpu: boolean
}

export interface PreparedRootCandidate {
  path: string
  label: string
  runs: number
  unfinished: number
  rows: number
}

export interface PreparedRootsResponse {
  root: string
  chains_root: string
  candidates: PreparedRootCandidate[]
}

export interface CreateChainResponse {
  chain_dir: string
  name: string
  prepared_root: string
  runs: number
}
