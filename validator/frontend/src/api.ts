// Plain same-origin fetch wrapper -- no auth. This tool assumes a trusted
// network (see ../run.sh and backend/app.py): anyone who can reach the
// backend at all can use these routes.
//
// Only these calls act on anything: submitRuns, cancelBatchJobs and
// deleteRunFolders (they start SLURM conversion jobs, cancel them, and delete
// run folders inside the runs root), and, for chains, createChain (makes one
// folder under the chains root), submitStep (starts one step of a chain) and
// cancelStepJobs (scancel on a step's own jobs). Every other call here is
// read-only.
import type {
  BatchStatusResponse,
  BatchesResponse,
  CancelResponse,
  CheckRequest,
  ChainResponse,
  ChainStepResponse,
  ChainsListResponse,
  CheckResponse,
  CreateChainResponse,
  DeleteResponse,
  HealthResponse,
  InspectResult,
  JobResources,
  JobsSettings,
  LookupRequest,
  LookupResponse,
  OriginalRowRequest,
  OriginalRowResponse,
  PlannedRun,
  PreparedRootsResponse,
  PreviewRequest,
  OpenedRun,
  PreviewResponse,
  RemovedOptionsResponse,
  RemovedPageResponse,
  RemovedRequest,
  RowRequest,
  RowStory,
  RunProgressResponse,
  RunsListResponse,
  SkippedPageRequest,
  SkippedPageResponse,
  SourcesResponse,
  StepCancelResponse,
  StepCheckRequest,
  StepJobsResponse,
  StepPlan,
  StepRecordView,
  StepSpecResponse,
  StepSubmitRequest,
  SubmitResponse,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`
    try {
      const data = await response.json()
      if (data && typeof data.detail === 'string') detail = data.detail
    } catch {
      // Body wasn't JSON -- keep the statusText-based message above.
    }
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health')
}

export function getSources(): Promise<SourcesResponse> {
  return request<SourcesResponse>('/api/sources')
}

export function inspectPath(path: string): Promise<InspectResult> {
  return request<InspectResult>('/api/inspect', { path })
}

export function runPreview(body: PreviewRequest): Promise<PreviewResponse> {
  return request<PreviewResponse>('/api/preview', body)
}

export function listRuns(): Promise<RunsListResponse> {
  return request<RunsListResponse>('/api/runs')
}

// A Prepared run or a filter run: `kind` says which.
export function openRun(runDir: string): Promise<OpenedRun> {
  return request<OpenedRun>('/api/run', { run_dir: runDir })
}

export function getRunProgress(runDir: string): Promise<RunProgressResponse> {
  return request<RunProgressResponse>('/api/run/progress', { run_dir: runDir })
}

export function getSkippedPage(body: SkippedPageRequest): Promise<SkippedPageResponse> {
  return request<SkippedPageResponse>('/api/run/skipped', body)
}

export function lookupOrigin(body: LookupRequest): Promise<LookupResponse> {
  return request<LookupResponse>('/api/origin', body)
}

// ---- Chains (read-only) ----

export function listChains(): Promise<ChainsListResponse> {
  return request<ChainsListResponse>('/api/chains')
}

export function getChain(chainDir: string): Promise<ChainResponse> {
  return request<ChainResponse>('/api/chain', { chain_dir: chainDir })
}

export function getChainStep(chainDir: string, stage: string): Promise<ChainStepResponse> {
  return request<ChainStepResponse>('/api/chain/step', { chain_dir: chainDir, stage })
}

// ---- The rows a filter step removed (read-only) ----

export function getRemovedOptions(chainDir: string): Promise<RemovedOptionsResponse> {
  return request<RemovedOptionsResponse>('/api/removed/options', { chain_dir: chainDir })
}

export function getRemovedPage(body: RemovedRequest): Promise<RemovedPageResponse> {
  return request<RemovedPageResponse>('/api/removed', body)
}

// ---- One row of a chain (read-only) ----

// A Prepared row, by its run, file and line.
export function getRow(body: RowRequest): Promise<RowStory> {
  return request<RowStory>('/api/row', body)
}

// A line of the clean data: the backend follows the line maps back to the Prepared row.
export function getRowFromClean(body: RowRequest): Promise<RowStory> {
  return request<RowStory>('/api/row/from-clean', body)
}

// An Original row: where it went in the chain's Prepared runs, and its story if found.
export function getRowFromOriginal(body: OriginalRowRequest): Promise<OriginalRowResponse> {
  return request<OriginalRowResponse>('/api/row/from-original', body)
}

// ---- Starting, watching, cancelling and deleting conversions (jobs.py) ----

export function getJobsSettings(): Promise<JobsSettings> {
  return request<JobsSettings>('/api/jobs/settings')
}

// Read-only: where each run would go, what's already there, the commands.
export function checkRuns(body: CheckRequest): Promise<CheckResponse> {
  return request<CheckResponse>('/api/jobs/check', body)
}

// Deletes run folders (absolute paths from a check) inside the runs root.
export function deleteRunFolders(runDirs: string[]): Promise<DeleteResponse> {
  return request<DeleteResponse>('/api/jobs/delete', { run_dirs: runDirs })
}

// Starts one SLURM job per run, as one batch.
export function submitRuns(runs: PlannedRun[], resources: JobResources): Promise<SubmitResponse> {
  return request<SubmitResponse>('/api/jobs/submit', { runs, resources })
}

export function listBatches(): Promise<BatchesResponse> {
  return request<BatchesResponse>('/api/jobs/batches')
}

export function getBatchStatus(batchId: string): Promise<BatchStatusResponse> {
  return request<BatchStatusResponse>('/api/jobs/status', { batch_id: batchId })
}

// Cancels a batch's queued or running jobs (all of them, or just `jobIds`).
export function cancelBatchJobs(batchId: string, jobIds?: string[]): Promise<CancelResponse> {
  return request<CancelResponse>('/api/jobs/cancel', { batch_id: batchId, job_ids: jobIds ?? null })
}

// ---- The steps of a chain as jobs ----

export function getStepJobs(): Promise<StepJobsResponse> {
  return request<StepJobsResponse>('/api/steps/jobs')
}

// One step record in full: the command, what it printed, each job and its log tails.
export function getStepStatus(chainDir: string, recordId: string): Promise<StepRecordView> {
  return request<StepRecordView>('/api/steps/status', { chain_dir: chainDir, record_id: recordId })
}

// `scancel` on a step record's queued or running jobs (all of them, or just `jobIds`).
export function cancelStepJobs(chainDir: string, recordId: string, jobIds?: string[]): Promise<StepCancelResponse> {
  return request<StepCancelResponse>('/api/steps/cancel', {
    chain_dir: chainDir,
    record_id: recordId,
    job_ids: jobIds ?? null,
  })
}

// ---- Making a chain and starting its steps ----

// What the start form needs: the partitions, and each stage's resources and settings.
export function getStepSpec(): Promise<StepSpecResponse> {
  return request<StepSpecResponse>('/api/steps/spec')
}

// Read-only: what the step reads and skips, the exact command, its cost, and what stops it.
export function checkStep(body: StepCheckRequest): Promise<StepPlan> {
  return request<StepPlan>('/api/steps/check', body)
}

// Starts the step (the backend checks it again first). A step that uses GPUs needs `confirm_gpu`.
export function submitStep(body: StepSubmitRequest): Promise<StepRecordView> {
  return request<StepRecordView>('/api/steps/submit', body)
}

export function getPreparedRoots(): Promise<PreparedRootsResponse> {
  return request<PreparedRootsResponse>('/api/chains/prepared-roots')
}

// Makes a new chain folder under the chains root.
export function createChain(name: string, preparedRoot: string): Promise<CreateChainResponse> {
  return request<CreateChainResponse>('/api/chains/create', { name, prepared_root: preparedRoot })
}
