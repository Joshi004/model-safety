// The shapes the backend sends and receives (validator/backend/app.py and registry.py).

export interface HealthResponse {
  ok: boolean
}

// ---- Teams and datasets (GET /api/teams, /api/datasets, POST /api/datasets/{id}/unregister) ----

export interface Team {
  name: string
  description: string
}

export interface TeamsResponse {
  teams: Team[]
}

// The conversion settings saved on a dataset, or null until they are set (Phase 3).
export interface DatasetConversion {
  input_type: string
  file: string | null
  split: string
  hf_config: string
  map: Record<string, string>
  require: string[]
  exclude_metadata: string[]
}

// A Prepared version: the converted rows of a dataset. The dataset's `prepared` is the
// current one, or null until it is converted. `folder` is the absolute path of the
// version's folder, which the run and preview calls take as `run_dir`. `kept` is set when
// a filtered dataset was made from it (Phase 10): converting again then keeps the folder.
export interface DatasetPrepared {
  version_id: string
  folder: string
  rows: number | null
  rows_read: number | null
  skipped: number | null
  finished_at: string | null
  kept: boolean
  // A filtered dataset's version only: the rows its filter job dropped.
  removed?: number | null
}

// The state of a filtered dataset's own filter job (the job that makes its Prepared version).
// The states are the scans'.
export type FilterState = Extract<ScanState, 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'unknown'>

// `filter` of a filtered dataset in GET /api/datasets[/{id}]. `removed` is what the filter
// reported once it finished, and what was chosen before.
export interface FilterSummary {
  state: FilterState
  reason: string | null
  removed: number | null
  parent_id: string
  parent_name: string | null
  version_id: string | null
}

// One of the filtered versions made from a dataset: GET /api/datasets/{id}'s `filtered_versions`.
export interface FilteredVersion {
  id: string
  name: string
  created_at: string
  rows: number | null
  state: FilterState
  reason: string | null
  removed: number | null
}

export interface Dataset {
  id: string
  team: string
  name: string
  // The absolute path of the original data. Never changed after registering.
  path: string
  description: string
  notes: string
  created_at: string
  conversion: DatasetConversion | null
  prepared: DatasetPrepared | null
  // Earlier versions that stay on disk because a filtered dataset needs them (Phase 10).
  kept_versions: DatasetPrepared[]
  // Set for a filtered dataset (Phase 10): the dataset it was made from.
  parent: string | null
  // Whether the original path exists and can be read right now.
  available: boolean
  unavailable_reason: string | null
  // The state of each scanner on this dataset, by scanner key (Phase 4). Only
  // GET /api/datasets and GET /api/datasets/{id} send it; register and PATCH don't.
  scans?: Record<string, ScanSummary>
  // Only for a filtered dataset (and only in those two routes): the state of its filter job.
  filter?: FilterSummary | null
  // Only in GET /api/datasets/{id}: the filtered versions made from this dataset, newest first.
  filtered_versions?: FilteredVersion[]
}

export interface DatasetsResponse {
  service_root: string
  datasets: Dataset[]
}

export interface RegisterDatasetRequest {
  team: string
  name: string
  path: string
  description: string
}

// PATCH /api/datasets/{id}: only the fields sent change. `conversion` is replaced as a
// whole (null clears it). The path can't be changed.
export interface UpdateDatasetRequest {
  team?: string
  name?: string
  description?: string
  notes?: string
  conversion?: DatasetConversion | null
}

export interface UnregisterResponse {
  id: string
  name: string
  removed_folder: string | null
}

// ---- Looking at a path (POST /api/inspect) ----

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

// ---- Previewing rows and their conversion (POST /api/preview, backend/preview.py) ----

export interface MappingHint {
  field: string
  severity: string
  message: string
}

// `map` is a plain object `{target: source}`, e.g. `{"messages": "conversations"}`.
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
  // null when the format's own metadata doesn't say how many rows there are.
  expected_rows: number | null
  expected_rows_from: string
  files_discovered: number | null
  sampled_from_file: string | null
  // jsonl only: the file each sampled row came from, in sample order.
  row_files?: string[]
  declared_schema: unknown | null
}

// A sampled row as the converter receives it, made JSON-safe (see describe.ts).
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
  // 0-indexed. Which row the sample starts at.
  start_row?: number
  map?: Record<string, string>
  require?: string[]
  exclude_metadata?: string[]
  metadata_column?: string
  output_dir?: string
}

// ---- A Prepared version on disk (POST /api/run, /api/run/skipped, /api/origin) ----

// An `_origin` note (modelsafety/contract/origin.py): where a Prepared row came from. A
// JSONL note has `line` and `byte`; every other format has `row`; an Arrow folder also has
// `split`.
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

// One entry of summary.json's `output.files_written`. `path` is relative to the run folder.
export interface WrittenFile {
  path: string
  rows: number
  bytes: number
}

// The parts of a prepare_rows.py summary.json the Data tab shows.
export interface RunSummary {
  created_at: string
  input: {
    path: string
    type: string
    map: Record<string, string>
    require: string[]
    max_rows: number | null
  }
  source: { rows_read: number }
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

// POST /api/run for a Prepared version.
export interface RunResponse {
  kind: 'prepared'
  run_dir: string
  summary: RunSummary
  run_identity: { settings?: Record<string, unknown>; created_at?: string } | null
}

// The summary.json of a filter run: a filtered dataset's version, made by dropping rows from
// its parent's. It reads like a Prepared run's where the two overlap (`source`, `rows.written`,
// `output`), but it removes rows instead of skipping them.
export interface FilterRunSummary {
  created_at: string
  tool: string
  parent: { run: string; run_dir: string; tool: string | null; scan_root: string; files: number }
  source: { rows_read: number }
  rows: { written: number; removed: number }
  removed_by_reason: Record<string, number>
  output: {
    files: number
    rows_on_disk: number
    bytes_on_disk: number
    files_written?: WrittenFile[]
  }
  removed_rows: { path: string; bytes: number; sha256: string; entries: number }
  drop_list?: { path: string; sha256: string; entries: number }
  checks: { passed: boolean; failures: string[]; warnings: string[] }
}

// POST /api/run for a filtered dataset's version.
export interface FilterRunResponse {
  kind: 'filter'
  run_dir: string
  summary: FilterRunSummary
  run_identity: { settings?: Record<string, unknown>; created_at?: string } | null
  // The Prepared run it was made from, which "Show original" follows; null, with the reason,
  // when it can't be found.
  prepared_run_dir: string | null
  prepared_run_problem: string | null
}

export type AnyRunResponse = RunResponse | FilterRunResponse

// One line of skipped_rows.ndjson. `row` is the original row as the reader saw it.
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

// progress.json (modelsafety/readers/progress_file.py), written by prepare_rows.py while it
// runs. The first eight phases are in the order a run goes through them; `done` and
// `failed` end it.
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
  // What `done` and `total` count; null for CSV and JSON reading, and for the end phases.
  unit: 'bytes' | 'files' | 'rows' | null
  done: number
  total: number | null
  files_done: number | null
  files_total: number | null
  rows_written: number
  rows_skipped: number
  error: string | null
}

// ---- Converting a dataset (backend/conversions.py) ----

// What a SLURM job asks for. All four are always sent: there is no "leave it to SLURM's
// default" (see the note in ResourcesFields.tsx).
export interface JobResources {
  partition: string
  cpus: number
  mem: string
  time: string
}

// GET /api/conversions/settings.
export interface ConversionSettings {
  service_root: string
  partitions: string[]
  default_resources: JobResources
  max_cpus: number
  // Set when sinfo couldn't be read (then `partitions` is empty).
  slurm_error: string | null
}

// Why a dataset can or can't be converted now. Only `ready` can be started.
export type ConversionStatus =
  | 'ready'
  | 'converted'
  | 'filtered'
  | 'no_settings'
  | 'unavailable'
  | 'active'
  | 'invalid'
  | 'slurm_error'

export interface ConversionWillDelete {
  version_id: string
  folder: string
  rows: number | null
  // Kept on disk instead of deleted, because a filtered dataset was made from it.
  kept: boolean
}

// One dataset of POST /api/conversions/check.
export interface ConversionPlanItem {
  dataset_id: string
  name: string
  team: string
  reconvert: boolean
  status: ConversionStatus
  reason: string | null
  can_start: boolean
  prepared: DatasetPrepared | null
  // What "convert again" deletes first; null when there is nothing to delete.
  will_delete: ConversionWillDelete | null
  // Folders of failed or cancelled attempts, deleted when the conversion starts.
  leftovers: string[]
  // The folder name in these commands is "<version-id>" until the job starts; the job's own
  // record then holds the exact command that ran.
  command: string | null
  sbatch_command: string | null
}

export interface ConversionCheckResponse {
  service_root: string
  slurm_error: string | null
  resources: JobResources | null
  resources_error: string | null
  items: ConversionPlanItem[]
}

export interface ConversionItemRequest {
  id: string
  reconvert: boolean
}

// Null resources means the defaults.
export interface ConversionsRequest {
  items: ConversionItemRequest[]
  resources?: JobResources | null
}

export interface ConversionSubmitResponse {
  started: { dataset_id: string; name: string; job_id: string; slurm_job_id: string | null }[]
  errors: string[]
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

// The numbers of a finished run's summary.json.
export interface RunBrief {
  created_at: string | null
  rows_read: number | null
  written: number | null
  skipped: number | null
  checks_passed: boolean
}

// One conversion of a dataset (GET /api/datasets/{id}/conversions[/{job_id}]).
export interface ConversionJob {
  job_id: string
  dataset_id: string
  created_at: string | null
  submitted_at: string | null
  outcome: JobOutcome
  reason: string | null
  // True while this conversion's version is the dataset's current Prepared version.
  current: boolean
  folder: string
  folder_exists: boolean
  reconvert: boolean
  conversion: DatasetConversion | null
  resources: JobResources | null
  // The exact commands that ran.
  command: string | null
  sbatch_command: string | null
  // Folders this start deleted before it began.
  deleted: string[]
  // Set once it became the current version.
  result: { finished_at: string | null; rows: number | null; rows_read: number | null; skipped: number | null } | null
  slurm_job_id: string | null
  slurm_state: string | null
  exit_code: string | null
  elapsed: string | null
  submit_error: string | null
  progress: RunProgress | null
  may_have_stopped: boolean
  summary: RunBrief | null
  out_log: string | null
  err_log: string | null
  // Only in the single-conversion route.
  out_tail: string | null
  err_tail: string | null
}

export interface ConversionStatusResponse extends ConversionJob {
  slurm_error: string | null
}

export interface ConversionListResponse {
  dataset_id: string
  slurm_error: string | null
  problems: string[]
  has_active: boolean
  conversions: ConversionJob[]
}

export interface CancelResponse {
  cancelled: string[]
  not_active: string[]
}

// GET /api/conversions: every conversion of every dataset, for the Activity page. Each has
// what a dataset's own list has (the log tails are always null here), plus the dataset's name.
export interface ActivityConversion extends ConversionJob {
  dataset_name: string
}

export interface AllConversionsResponse {
  slurm_error: string | null
  problems: string[]
  has_active: boolean
  conversions: ActivityConversion[]
}

// ---- Scanners and scans (backend/scanners.py, scans.py) ----

interface ScannerFieldBase {
  name: string
  label: string
  help: string
}

// One of `choices`. `hints` is a short line for some or all of them, shown next to the choice.
export interface ChoiceField extends ScannerFieldBase {
  type: 'choice'
  choices: string[]
  default: string
  hints?: Record<string, string>
}

// A whole number from `min` to `max`. Empty is allowed when the default is null.
export interface IntegerField extends ScannerFieldBase {
  type: 'integer'
  min: number
  max: number
  default: number | null
}

// A number from `min` to `max` in steps of `step`, with at most two decimals ("0.05").
export interface DecimalField extends ScannerFieldBase {
  type: 'decimal'
  min: number
  max: number
  step: number
  default: number
}

// At least one of `choices`. `groups` only says how to show them together: each group holds
// some of the choices, and every choice is in one.
export interface MultiField extends ScannerFieldBase {
  type: 'multi'
  choices: string[]
  default: string[]
  groups?: { key: string; choices: string[] }[]
}

// A list of ids of datasets chosen for the scan. The dialog draws these itself (see
// CUSTOM_SETTINGS in scans.ts), because what to offer depends on the datasets chosen.
export interface DatasetsField extends ScannerFieldBase {
  type: 'datasets'
  default: string[]
}

// One setting of a scanner. The backend checks every value against its field.
export type ScannerField = ChoiceField | IntegerField | DecimalField | MultiField | DatasetsField

// What a setting holds: a choice, a number (a whole number is null when it is empty; a number
// being typed is the text as typed), or a list of choices.
export type SettingValue = string | number | string[] | null
export type ScanSettings = Record<string, SettingValue>

export interface ScannerInfo {
  key: string
  label: string
  description: string
  stages: string[]
  gpu: boolean
  fields: ScannerField[]
  default_resources: JobResources
  // True when the scanner's job scripts fix the CPUs, memory and time (and so the GPUs): only
  // the partition is chosen, and the check says what the scripts ask for.
  resources_fixed: boolean
  // What the scanner looks for when the settings don't choose it; empty when they do.
  detects: string[]
  // How a scan over several datasets can be made, by mode; null when every dataset gets its own scan.
  modes: Record<string, string> | null
  default_mode: string | null
  // What the headline count of a finished scan counts ("duplicate rows").
  count_label: string
  // Whether a scan can be asked to drop what it flags by itself when it has finished and its
  // coverage check has passed (D17).
  offers_auto_drop: boolean
}

// GET /api/scanners.
export interface ScannersResponse {
  service_root: string
  scanners: Record<string, ScannerInfo>
  partitions: string[]
  default_partition: string
  max_cpus: number
  // Set when sinfo couldn't be read (then `partitions` is empty).
  slurm_error: string | null
}

export type ScanState =
  | 'not_scanned'
  | 'waiting_for_conversion'
  | 'starting'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'conversion_failed'
  | 'unknown'

// One scanner's state on one dataset: a dataset's `scans[scanner]`.
export interface ScanSummary {
  state: ScanState
  scan_id: string | null
  finished_at: string | null
  // The headline number of a finished scan for this dataset (duplicate rows for dedup).
  count: number | null
  mode: string | null
  reason: string | null
  // Whether the finished scan read exactly this dataset's converted data; null until its
  // coverage check has been made.
  coverage: CoverageVerdict | null
  // The other datasets the scan compared this one with, in the order it read them (a name is
  // null when that dataset isn't registered any more). Empty for a scan of this dataset alone.
  compared_with: { id: string; name: string | null }[]
  // The strictness the scan was started with; only the duplicates scan has one.
  strictness: string | null
}

export interface ScanDatasetRequest {
  id: string
  reconvert: boolean
}

// What a scan request asks for: all four, or only the partition for a scanner whose job
// scripts fix the rest (`ScannerInfo.resources_fixed`).
export type ResourceRequest = Partial<JobResources>

// The body of POST /api/scans/check and /api/scans/submit. `settings` holds a value for each
// setting the user can change; one that is left out gets its default. Null resources mean the
// scanner's own defaults. Scans use the default resources for any conversion they start.
export interface ScansRequest {
  scanner: string
  datasets: ScanDatasetRequest[]
  mode: string | null
  settings: ScanSettings
  resources: ResourceRequest | null
  confirm_gpu: boolean
  // Drop what the scan flags by itself once it has finished and its coverage check has passed.
  auto_drop: boolean
}

export type ScanAction = 'use_existing' | 'convert' | 'convert_again'

export interface ScanCost {
  gpus: number
  cpus: number
  mem: string
  time: string
  summary: string
}

// One dataset of one scan in POST /api/scans/check. `action` is null when the dataset can't
// be made ready (`reason` says why).
export interface ScanUnitDataset {
  id: string
  name: string
  team: string
  action: ScanAction | null
  status: ConversionStatus
  reason: string | null
  prepared: { version_id: string; rows: number | null; finished_at: string | null } | null
  will_delete: ConversionWillDelete | null
  leftovers: string[]
  // The folder name in the conversion commands is "<version-id>" until the job starts.
  conversion_command: string | null
  conversion_sbatch_command: string | null
}

// One scan the request would make. Datasets are in the order the scan reads them.
export interface ScanUnit {
  mode: string | null
  datasets: ScanUnitDataset[]
  blocked: string[]
  warnings: string[]
  cost: ScanCost | null
  needs_gpu_confirmation: boolean
  // "<version-id>" and "<scan-id>" stand for what isn't known until the jobs start.
  command_text: string | null
}

export interface ScanCheckResponse {
  scanner: string
  label: string
  mode: string | null
  service_root: string
  slurm_error: string | null
  settings: ScanSettings
  resources: JobResources | null
  // What is wrong with the request as a whole; what is wrong with one scan is in its unit.
  blocked: string[]
  units: ScanUnit[]
  can_start: boolean
}

// A conversion a scan waits for.
export interface ScanWaitingFor {
  dataset_id: string
  name: string
  job_id: string
  outcome: JobOutcome
}

export interface ScanJobRef {
  role: string
  job_id: string
}

// One scan, as GET /api/scans lists it.
export interface ScanView {
  id: string
  scanner: string
  label: string
  mode: string | null
  datasets: { id: string; name: string | null }[]
  state: ScanState
  reason: string | null
  waiting_for: ScanWaitingFor[]
  // SLURM's run time of the job ("1-02:03:04"). Null without a job, and once the scan has
  // become a result.
  elapsed: string | null
  created_at: string | null
  submitted_at: string | null
  finished_at: string | null
  cancelled_at: string | null
  // The scan's own job.
  job_id: string | null
  // Every SLURM job the scan started, in order, each with its role: "plan" for a job the scan's
  // own job waits for, "scan" for its own.
  jobs: ScanJobRef[]
  settings: ScanSettings
  resources: JobResources
  // Whether the scan was started to drop what it flags by itself (D17).
  auto_drop: boolean
  // The conversions this scan started or waits for, by dataset id.
  conversions: Record<string, { reconvert: boolean; job_id: string }>
  folder: string
  // Whether this scan is the current result of its datasets.
  is_result: boolean
  // How far the scan has got, one entry for each stage that can be counted. Null unless the
  // scanner can tell and its own job is running.
  progress: ScanProgress[] | null
}

// One counted stage of a running scan ("Quick screen: 3 of 8 shards").
export interface ScanProgress {
  stage: string
  done: number
  total: number
  unit: string
}

export interface ScansResponse {
  service_root: string
  slurm_error: string | null
  problems: string[]
  scans: ScanView[]
}

export interface ScanLog {
  name: string
  path: string
  tail: string | null
}

// GET /api/scans/{id}: a scan in full.
export interface ScanDetail extends ScanView {
  slurm_error: string | null
  // The Prepared version read for each dataset, once the scan has started.
  versions: Record<string, { version_id: string; folder: string; rows: number | null }> | null
  command_text: string | null
  wrapper_output: string | null
  // What a finished scan found (the scanner decides the keys); null until then.
  details: Record<string, unknown> | null
  logs: ScanLog[]
}

export interface ScanSubmitResponse {
  slurm_error: string | null
  scans: ScanView[]
  conversions: { dataset_id: string; name: string; job_id: string; slurm_job_id: string | null }[]
  errors: string[]
}

export interface ScanCancelResponse {
  scan_id: string
  cancelled: string[]
  not_active: string[]
  stopped_waiting: boolean
}

// ---- Results per dataset ----

export type CoverageVerdict = 'pass' | 'fail'

// The state of the job that checks a finished scan: `pending` until it has been submitted, then
// the states of a SLURM job.
export type AnalysisState = 'pending' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'unknown'

export interface ResultAnalysis {
  state: AnalysisState
  reason: string | null
  // "full" checks coverage and counts; "coverage" only checks coverage again.
  kind: 'full' | 'coverage' | null
  job_id: string | null
  submitted_at: string | null
  elapsed: string | null
  // Only in the answer for one scanner.
  command_text?: string | null
  logs?: ScanLog[]
}

export interface CoverageProblem {
  message: string
  path: string | null
}

export interface ResultCoverage {
  verdict: CoverageVerdict
  problems: CoverageProblem[]
  // How many there were, of which `problems` lists the first few.
  problems_total: number
  checked_at: string | null
}

export interface ResultFlagged {
  // Null until the scan's analysis has counted it.
  count: number | null
  unit: string
  // Rows with at least one finding, when the scanner can tell.
  rows: number | null
}

export interface ResultBreakdown {
  title: string
  items: { name: string; count: number }[]
  note: string | null
}

export interface ResultFact {
  label: string
  value: number
}

// A line of text about how the scan was made ("Original copy: first by path").
export interface ResultNote {
  label: string
  text: string
}

// What counts as flagged in a toxicity result. `selected_*` is what the numbers were made with;
// `saved_*` is what the scan was started with (D8).
export interface ToxicityFilter {
  categories: string[]
  verdicts: string[]
  selected_categories: string[]
  selected_verdicts: string[]
  saved_categories: string[]
  saved_verdicts: string[]
  threshold: number | null
}

export interface ScannerResult {
  scan_id: string
  finished_at: string | null
  mode: string | null
  settings: ScanSettings
  // The datasets the scan covered, in the order it read them. A name is null when the dataset
  // isn't registered any more.
  datasets: { id: string; name: string | null }[]
  analysis: ResultAnalysis
  // Null until the analysis has succeeded.
  coverage: ResultCoverage | null
  rows: { scanned: number | null; with_text: number | null; without_text: number | null }
  // Whether the flagged rows of this dataset can be listed: the check that makes the index has
  // finished (GET .../flagged).
  has_index: boolean
  flagged: ResultFlagged
  breakdowns: ResultBreakdown[]
  facts: ResultFact[]
  // Empty for a scanner with nothing to say about how it was made.
  notes: ResultNote[]
  // Toxicity only.
  filter?: ToxicityFilter
}

// One scanner's side of a dataset's results: its state now, and the current result (which can
// exist while a newer scan is running).
export interface ScannerResultView {
  scanner: string
  label: string
  count_label: string
  stages: string[]
  summary: ScanSummary
  result: ScannerResult | null
}

// GET /api/datasets/{id}/results.
export interface DatasetResultsResponse {
  dataset_id: string
  name: string
  prepared: { version_id: string | null; rows: number | null } | null
  slurm_error: string | null
  scanners: Record<string, ScannerResultView>
}

// What came of a scan's automatic drop (D17). `waiting` until it is decided (nothing is saved
// before); `dropping` while its filtered versions are being made; `dropped` when at least one was
// made; `nothing_to_drop` when nothing was flagged; `not_dropped` when the coverage check failed;
// `failed` when nothing could be made; `interrupted` when the service stopped part way.
export type AutoDropOutcome =
  | 'waiting'
  | 'dropping'
  | 'dropped'
  | 'nothing_to_drop'
  | 'not_dropped'
  | 'failed'
  | 'interrupted'

// One dataset's part of an automatic drop.
export interface AutoDropDataset {
  dataset_id: string
  dataset_name: string | null
  // The flagged rows dropped, or that there were.
  rows: number | null
  // The filtered version made from it (a dataset of its own), if there was one.
  filtered_id: string | null
  filtered_name: string | null
  error: string | null
  note: string | null
}

export interface AutoDropView {
  scan_id: string
  outcome: AutoDropOutcome
  decided_at: string | null
  reason: string | null
  // The coverage problems, when it wasn't dropped for that.
  problems: CoverageProblem[]
  problems_total: number
  datasets: AutoDropDataset[]
}

// GET /api/datasets/{id}/results/{scanner}.
export interface DatasetResultResponse extends ScannerResultView {
  dataset_id: string
  name: string
  slurm_error: string | null
  // Null when the scan wasn't started with an automatic drop.
  auto_drop: AutoDropView | null
}

export interface CheckAgainResponse {
  scan_id: string
  job_id: string | null
  submit_error: string | null
}

// ---- Flagged rows ----

// One row of the table of flagged rows (GET .../flagged). `file` and `line` are its place in the
// dataset's Prepared version.
export interface FlaggedEntry {
  file: string
  line: number
  preview: string
  // Why it was flagged: "ner", "regex_pii", "duplicate" or "toxicity_<outcome>".
  reasons: string[]
  // NER labels or regex categories in the row, each with how many findings it has.
  names: Record<string, number>
  // Toxicity: the parts of the row that count as flagged under the choice of categories and
  // verdicts: "error", or "<verdict>|<category>" ("<verdict>|" for a verdict with no category).
  atoms: string[]
  // Duplicates: the name of the dataset that holds the copy the row matched.
  kept_in: string | null
}

// A choice for a filter, with the number of rows that have it.
export interface FlaggedOption {
  name: string
  // What to show instead of the name (a dataset's name, for duplicates).
  label?: string
  count: number
}

export interface FlaggedPage {
  page: number
  page_size: number
  // The rows that pass the filters.
  total: number
  has_next: boolean
  entries: FlaggedEntry[]
  // Empty for toxicity, whose table follows its categories and verdicts.
  options: { reasons: FlaggedOption[]; names: FlaggedOption[] }
}

// What a page of flagged rows is asked for. `categories` and `verdicts` are toxicity's choice
// (null leaves the scan's own); the other two filter every other scanner's rows.
export interface FlaggedFilters {
  reason: string | null
  name: string | null
  categories: string[] | null
  verdicts: string[] | null
}

// A message of a row, or its `text`: what a scanner read, cut the way the scans cut it.
export interface RowSegment {
  // Null for a row's `text`.
  message_index: number | null
  role: string
  text: string
  length: number
  truncated: boolean
}

// A mark in a message's text: an NER span, a place a regex PII value was found, or (for a
// duplicate) a run of words that are the same in both rows or only in this one.
export interface RowHighlight {
  scanner: 'ner' | 'regex_pii' | 'dedup'
  message_index: number | null
  start: number
  end: number
  // Duplicates
  kind?: 'same' | 'different'
  // NER
  label?: string | null
  tier?: string | null
  confidence?: number | null
  matched?: string
  // Regex PII
  category?: string | null
  value?: string | null
}

// A message the toxicity guard judged. `counts` is whether it counts as flagged under the choice.
export interface RowMessageFlag {
  message_index: number | null
  role: string | null
  verdict: string | null
  error: string | null
  categories: string[]
  detoxify_score: number | null
  detoxify_category: string | null
  counts: boolean
}

// A finding that couldn't be placed in the text, and why.
export interface RowUnplaced {
  scanner: string
  why: string
  label?: string | null
  category?: string | null
  value?: string | null
  message_index?: number | null
  verdict?: string | null
}

// For a duplicate: the copy it matched.
export interface KeptCopy {
  dataset_id: string | null
  dataset: string | null
  file: string | null
  line: number | null
  run_dir: string | null
  segments: RowSegment[] | null
  origin: Origin | null
  problem: string | null
}

// For a duplicate: the word-by-word comparison of the row with its kept copy. `*_compared` are
// the messages (null for a row's `text`) the scan compared; the others were left out of the scan.
export interface DuplicateComparison {
  row_highlights: RowHighlight[]
  kept_highlights: RowHighlight[]
  row_compared: (number | null)[]
  kept_compared: (number | null)[]
  // 0 to 1, over the compared words of both rows; null when nothing was compared.
  same_share: number | null
  // Some marks were left out.
  cut: boolean
  problem: string | null
}

// GET .../flagged/row.
export interface FlaggedRowDetail {
  dataset_id: string
  scanner: string
  location: { file: string; line: number }
  reasons: string[]
  prepared: { run_dir: string; fingerprint_ok: boolean | null; problem: string | null }
  // The `_origin` note of the Prepared line, for "Show original".
  origin: Origin | null
  segments: RowSegment[] | null
  highlights: RowHighlight[]
  message_flags: RowMessageFlag[]
  unplaced: RowUnplaced[]
  // More findings than are shown.
  findings_cut: boolean
  findings_total: number | null
  duplicate_of: KeptCopy | null
  comparison: DuplicateComparison | null
}

// ---- Dropping rows (backend/filters.py) ----

// A row of a dataset's Prepared version: its file (relative to the version) and line.
export interface DropRow {
  file: string
  line: number
}

// The rows chosen from one scanner's result: some rows (`rows`), or every row matching the
// filters (`all_matching`, which the server resolves). Exactly one of the two is set.
// `categories` and `verdicts` are toxicity's choice at the time (null: the scan's own).
export interface DropSelectionRequest {
  scanner: string
  scan_id: string
  rows: DropRow[] | null
  all_matching: { reason: string | null; name: string | null } | null
  categories: string[] | null
  verdicts: string[] | null
}

export interface DropRequest {
  // The dataset's converted data the rows were chosen from.
  version_id: string
  selections: DropSelectionRequest[]
}

export interface DropReasonCount {
  reason: string
  count: number
}

export interface DropScannerCounts {
  scanner: string
  label: string
  scan_id: string
  // Rows of this scanner's result that will be dropped.
  rows: number
  // A row can have more than one reason, so these can add up to more than `rows`.
  by_reason: DropReasonCount[]
}

export interface DropCounts {
  // Distinct rows that will be dropped: a row several scanners flagged counts once.
  rows: number
  // Lines of the drop list: a row once for each scanner that flagged it.
  entries: number
  // `entries` - `rows`: rows that more than one scanner flagged.
  overlap: number
  by_scanner: DropScannerCounts[]
}

// POST /api/datasets/{id}/drop/check
export interface DropCheckResponse {
  dataset_id: string
  dataset_name: string
  version_id: string
  rows_in_dataset: number | null
  counts: DropCounts
  // The name the new dataset will get.
  new_name: string
  warnings: string[]
  // Why it can't be started right now (SLURM can't be reached, say); empty when it can.
  blocked: string[]
  can_start: boolean
  resources: JobResources | null
  // The exact command, with <new-id> and <version-id> where those aren't known yet.
  command_text: string | null
}

// POST /api/datasets/{id}/drop/submit
export interface DropSubmitResponse {
  dataset_id: string
  name: string
  parent_id: string
  rows: number
  counts: DropCounts
  // The SLURM job; null when sbatch failed (then `submit_error` says why).
  job_id: string | null
  version_id: string
  submit_error: string | null
}

// One filtered dataset's filter job, as GET /api/filters lists it.
export interface FilterJob extends FilterSummary {
  dataset_id: string
  dataset_name: string
  created_at: string | null
  submitted_at: string | null
  finished_at: string | null
  elapsed: string | null
  job_id: string | null
  drop: DropCounts | null
  // The scan this version was dropped from by itself (D17); null for one a user chose rows for.
  automatic: { scan_id: string } | null
  folder: string | null
}

export interface FiltersResponse {
  slurm_error: string | null
  has_active: boolean
  filters: FilterJob[]
}

// GET /api/datasets/{id}/filter
export interface FilterStatusResponse extends FilterJob {
  command_text: string | null
  wrapper_output: string | null
  logs: ScanLog[]
  slurm_error: string | null
}
