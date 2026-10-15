// Plain same-origin fetch wrapper -- no auth. This tool assumes a trusted
// network (see ../run.sh and backend/app.py): anyone who can reach the
// backend at all can use these routes.
//
// Only these calls act on anything: registerDataset, updateDataset and
// unregisterDataset write the dataset registry (and unregisterDataset deletes that
// dataset's own folder under the service root), submitConversions starts SLURM
// conversion jobs (and "convert again" deletes the dataset's current Prepared
// version first), and cancelConversion cancels one. submitScans starts the conversions
// the chosen datasets need and then the scans (SLURM jobs that only read the converted
// data; "convert again" deletes the current Prepared version first), and cancelScan
// stops one. Reading a dataset, a conversion or a scan also saves a finished conversion
// as the dataset's Prepared version, and a finished scan as the dataset's result.
// checkResult submits one CPU job that checks a finished scan's coverage again. Every
// other call here is read-only.
import type {
  AllConversionsResponse,
  CancelResponse,
  CheckAgainResponse,
  ConversionCheckResponse,
  ConversionListResponse,
  ConversionSettings,
  ConversionStatusResponse,
  ConversionSubmitResponse,
  ConversionsRequest,
  Dataset,
  DatasetResultResponse,
  DatasetResultsResponse,
  DatasetsResponse,
  HealthResponse,
  InspectResult,
  LookupRequest,
  LookupResponse,
  PreviewRequest,
  PreviewResponse,
  RegisterDatasetRequest,
  RunResponse,
  ScanCancelResponse,
  ScanCheckResponse,
  ScanDetail,
  ScanSubmitResponse,
  ScannersResponse,
  ScansRequest,
  ScansResponse,
  SkippedPageRequest,
  SkippedPageResponse,
  TeamsResponse,
  UnregisterResponse,
  UpdateDatasetRequest,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// GET without a body, POST with one, or the method asked for (PATCH).
async function request<T>(path: string, body?: unknown, method?: 'PATCH'): Promise<T> {
  const response = await fetch(path, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
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

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error)
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health')
}

export function getTeams(): Promise<TeamsResponse> {
  return request<TeamsResponse>('/api/teams')
}

export function listDatasets(): Promise<DatasetsResponse> {
  return request<DatasetsResponse>('/api/datasets')
}

export function getDataset(datasetId: string): Promise<Dataset> {
  return request<Dataset>(`/api/datasets/${encodeURIComponent(datasetId)}`)
}

// Looks at a path and reports what is in it: its formats, files and sizes. Read-only.
export function inspectPath(path: string): Promise<InspectResult> {
  return request<InspectResult>('/api/inspect', { path })
}

export function registerDataset(body: RegisterDatasetRequest): Promise<Dataset> {
  return request<Dataset>('/api/datasets', body)
}

// Changes only the fields sent. `conversion` is replaced as a whole.
export function updateDataset(datasetId: string, body: UpdateDatasetRequest): Promise<Dataset> {
  return request<Dataset>(`/api/datasets/${encodeURIComponent(datasetId)}`, body, 'PATCH')
}

// Reads a bounded sample of rows from a path, and says what converting them would do. Read-only.
export function runPreview(body: PreviewRequest): Promise<PreviewResponse> {
  return request<PreviewResponse>('/api/preview', body)
}

// A Prepared version's summary.json and run_identity.json.
export function openRun(runDir: string): Promise<RunResponse> {
  return request<RunResponse>('/api/run', { run_dir: runDir })
}

export function getSkippedPage(body: SkippedPageRequest): Promise<SkippedPageResponse> {
  return request<SkippedPageResponse>('/api/run/skipped', body)
}

// The original row an `_origin` note points at, as it is now.
export function lookupOrigin(body: LookupRequest): Promise<LookupResponse> {
  return request<LookupResponse>('/api/origin', body)
}

// ---- Converting datasets ----

export function getConversionSettings(): Promise<ConversionSettings> {
  return request<ConversionSettings>('/api/conversions/settings')
}

// For each dataset: whether a conversion can start, what it would delete, and the commands.
export function checkConversions(body: ConversionsRequest): Promise<ConversionCheckResponse> {
  return request<ConversionCheckResponse>('/api/conversions/check', body)
}

// Starts one SLURM job per dataset. The whole request is refused if any one can't start.
export function submitConversions(body: ConversionsRequest): Promise<ConversionSubmitResponse> {
  return request<ConversionSubmitResponse>('/api/conversions/submit', body)
}

export function listConversions(datasetId: string): Promise<ConversionListResponse> {
  return request<ConversionListResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/conversions`)
}

// Every conversion of every dataset, newest first (the Activity page).
export function listAllConversions(): Promise<AllConversionsResponse> {
  return request<AllConversionsResponse>('/api/conversions')
}

export function getConversion(datasetId: string, jobId: string): Promise<ConversionStatusResponse> {
  return request<ConversionStatusResponse>(
    `/api/datasets/${encodeURIComponent(datasetId)}/conversions/${encodeURIComponent(jobId)}`,
  )
}

export function cancelConversion(datasetId: string, jobId: string): Promise<CancelResponse> {
  return request<CancelResponse>(
    `/api/datasets/${encodeURIComponent(datasetId)}/conversions/${encodeURIComponent(jobId)}/cancel`,
    {},
  )
}

// ---- Scanning datasets ----

// The scanners with their settings, modes and default resources, and the partitions SLURM lists.
export function getScanners(): Promise<ScannersResponse> {
  return request<ScannersResponse>('/api/scanners')
}

// What a request would do: what each dataset needs first, the commands, the cost and what
// stops it. Starts and deletes nothing.
export function checkScans(body: ScansRequest): Promise<ScanCheckResponse> {
  return request<ScanCheckResponse>('/api/scans/check', body)
}

// Starts the conversions the datasets need, and the scans (a scan whose datasets are all
// converted starts now, any other waits for its conversions). The whole request is refused
// if any scan can't be started.
export function submitScans(body: ScansRequest): Promise<ScanSubmitResponse> {
  return request<ScanSubmitResponse>('/api/scans/submit', body)
}

export function listScans(): Promise<ScansResponse> {
  return request<ScansResponse>('/api/scans')
}

export function getScan(scanId: string): Promise<ScanDetail> {
  return request<ScanDetail>(`/api/scans/${encodeURIComponent(scanId)}`)
}

// Stops a waiting scan from ever starting, or runs `scancel` on the scan's one job. The
// conversions it waits for keep running.
export function cancelScan(scanId: string): Promise<ScanCancelResponse> {
  return request<ScanCancelResponse>(`/api/scans/${encodeURIComponent(scanId)}/cancel`, {})
}

// Removes the entry and the dataset's own folder under the service root. The original data is not touched.
export function unregisterDataset(datasetId: string): Promise<UnregisterResponse> {
  return request<UnregisterResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/unregister`, {})
}

// ---- A dataset's results ----

// Every scanner's result for the dataset: the numbers for this dataset only, the coverage
// verdict and the state of the check that made them.
export function getDatasetResults(datasetId: string): Promise<DatasetResultsResponse> {
  return request<DatasetResultsResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/results`)
}

// One scanner's result in full. For toxicity, `categories` and `verdicts` change what counts as
// flagged in the answer and nothing else (an empty list means none); null leaves the scan's own.
export function getDatasetResult(
  datasetId: string,
  scanner: string,
  choice?: { categories: string[] | null; verdicts: string[] | null },
): Promise<DatasetResultResponse> {
  const params: string[] = []
  if (choice?.categories != null) params.push(`categories=${encodeURIComponent(choice.categories.join(','))}`)
  if (choice?.verdicts != null) params.push(`verdicts=${encodeURIComponent(choice.verdicts.join(','))}`)
  const query = params.length > 0 ? `?${params.join('&')}` : ''
  return request<DatasetResultResponse>(
    `/api/datasets/${encodeURIComponent(datasetId)}/results/${encodeURIComponent(scanner)}${query}`,
  )
}

// Runs the check of a finished scan again: one CPU SLURM job. Refused while one is going.
export function checkResult(datasetId: string, scanner: string): Promise<CheckAgainResponse> {
  return request<CheckAgainResponse>(
    `/api/datasets/${encodeURIComponent(datasetId)}/results/${encodeURIComponent(scanner)}/check`,
    {},
  )
}
