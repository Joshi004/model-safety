// Plain same-origin fetch wrapper -- no auth. This tool assumes a trusted
// network (see ../run.sh and backend/app.py): anyone who can reach the
// backend at all can use these routes.
//
// Only the jobs calls at the bottom (submitRuns, cancelBatchJobs,
// deleteRunFolders) act on anything: they start SLURM conversion jobs,
// cancel them, and delete run folders inside the runs root. Every other call
// here is read-only.
import type {
  BatchStatusResponse,
  BatchesResponse,
  CancelResponse,
  CheckRequest,
  CheckResponse,
  DeleteResponse,
  HealthResponse,
  InspectResult,
  JobResources,
  JobsSettings,
  LookupRequest,
  LookupResponse,
  PlannedRun,
  PreviewRequest,
  PreviewResponse,
  RunProgressResponse,
  RunResponse,
  RunsListResponse,
  SkippedPageRequest,
  SkippedPageResponse,
  SourcesResponse,
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

export function openRun(runDir: string): Promise<RunResponse> {
  return request<RunResponse>('/api/run', { run_dir: runDir })
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
