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
// setTeamPolicy, setDatasetPolicy and clearDatasetPolicy write the saved toxicity policies (a
// scan copies the policy it uses into its own folder when it starts, so they never change a scan
// that has started). checkResult submits one CPU job that checks a finished scan's coverage
// again. submitDrop drops the chosen rows: it registers a new (filtered) dataset and submits one
// CPU job that copies the other rows into it, and cancelFilter stops that job; the dataset the
// rows were dropped from is not changed. retryFilter runs a drop that failed or was cancelled
// again from the drop list it already has, into a new folder of the same filtered dataset.
// Every other call here is read-only, including the two that read a result's flagged rows,
// checkDrop, which only counts, and checkFilterRetry, which only describes a retry.
import type {
  AllConversionsResponse,
  AnyRunResponse,
  CancelResponse,
  CheckAgainResponse,
  ConversionCheckResponse,
  ConversionListResponse,
  ConversionSettings,
  ConversionStatusResponse,
  ConversionSubmitResponse,
  ConversionsRequest,
  Dataset,
  DatasetPolicy,
  DatasetResultResponse,
  DatasetResultsResponse,
  DatasetsResponse,
  DropCheckResponse,
  DropRequest,
  DropSubmitResponse,
  FilterRetryCheckResponse,
  FilterRetryResponse,
  FilterStatusResponse,
  FiltersResponse,
  FlaggedFilters,
  FlaggedPage,
  FlaggedRowDetail,
  HealthResponse,
  InspectResult,
  LookupRequest,
  LookupResponse,
  PartitionUsageResponse,
  PreviewRequest,
  PreviewResponse,
  RegisterDatasetRequest,
  ScanCancelResponse,
  ScanCheckResponse,
  ScanDetail,
  ScanSubmitResponse,
  ScannersResponse,
  ScansRequest,
  ScansResponse,
  SkippedPageRequest,
  SkippedPageResponse,
  TeamPolicy,
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

// GET without a body, POST with one, or the method asked for (PATCH, PUT or DELETE).
async function request<T>(path: string, body?: unknown, method?: 'PATCH' | 'PUT' | 'DELETE'): Promise<T> {
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

// ---- Toxicity policies ----

export function getTeamPolicy(team: string): Promise<TeamPolicy> {
  return request<TeamPolicy>(`/api/teams/${encodeURIComponent(team)}/toxicity-policy`)
}

// Saves the team's policy: every dataset of the team without a policy of its own follows it.
export function setTeamPolicy(team: string, text: string): Promise<TeamPolicy> {
  return request<TeamPolicy>(`/api/teams/${encodeURIComponent(team)}/toxicity-policy`, { text }, 'PUT')
}

// The policy a dataset uses (its own, or its team's) and both texts.
export function getDatasetPolicy(datasetId: string): Promise<DatasetPolicy> {
  return request<DatasetPolicy>(`/api/datasets/${encodeURIComponent(datasetId)}/toxicity-policy`)
}

// Saves a policy for this dataset only, in place of its team's.
export function setDatasetPolicy(datasetId: string, text: string): Promise<DatasetPolicy> {
  return request<DatasetPolicy>(`/api/datasets/${encodeURIComponent(datasetId)}/toxicity-policy`, { text }, 'PUT')
}

// Removes the dataset's own policy, so it follows its team's again.
export function clearDatasetPolicy(datasetId: string): Promise<DatasetPolicy> {
  return request<DatasetPolicy>(`/api/datasets/${encodeURIComponent(datasetId)}/toxicity-policy`, undefined, 'DELETE')
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

// A version's summary.json and run_identity.json: a Prepared version's, or (for a filtered
// dataset) a filter run's. `kind` says which.
export function openRun(runDir: string): Promise<AnyRunResponse> {
  return request<AnyRunResponse>('/api/run', { run_dir: runDir })
}

export function getSkippedPage(body: SkippedPageRequest): Promise<SkippedPageResponse> {
  return request<SkippedPageResponse>('/api/run/skipped', body)
}

// The original row an `_origin` note points at, as it is now.
export function lookupOrigin(body: LookupRequest): Promise<LookupResponse> {
  return request<LookupResponse>('/api/origin', body)
}

// ---- Converting datasets ----

// How busy each partition is: free nodes and GPUs, and who holds the others. Read-only.
export function getPartitionUsage(): Promise<PartitionUsageResponse> {
  return request<PartitionUsageResponse>('/api/cluster/partitions')
}

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

// ---- A result's flagged rows ----

// The part of the address that says which toxicity categories and verdicts count as flagged: an
// empty list means none, null leaves the scan's own.
function toxicityParams(params: URLSearchParams, choice: { categories: string[] | null; verdicts: string[] | null }) {
  if (choice.categories !== null) params.set('categories', choice.categories.join(','))
  if (choice.verdicts !== null) params.set('verdicts', choice.verdicts.join(','))
}

// One page of a result's flagged rows, from its findings index. Read-only.
export function getFlaggedPage(
  datasetId: string,
  scanner: string,
  page: number,
  pageSize: number,
  filters: FlaggedFilters,
): Promise<FlaggedPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (filters.reason !== null) params.set('reason', filters.reason)
  if (filters.name !== null) params.set('name', filters.name)
  toxicityParams(params, filters)
  return request<FlaggedPage>(
    `/api/datasets/${encodeURIComponent(datasetId)}/results/${encodeURIComponent(scanner)}/flagged?${params.toString()}`,
  )
}

// One flagged row: its Prepared text, what was found and where, and for a duplicate the copy it
// matched. Read-only.
export function getFlaggedRow(
  datasetId: string,
  scanner: string,
  file: string,
  line: number,
  choice: { categories: string[] | null; verdicts: string[] | null },
): Promise<FlaggedRowDetail> {
  const params = new URLSearchParams({ file, line: String(line) })
  toxicityParams(params, choice)
  return request<FlaggedRowDetail>(
    `/api/datasets/${encodeURIComponent(datasetId)}/results/${encodeURIComponent(scanner)}/flagged/row?${params.toString()}`,
  )
}

// ---- Dropping rows ----

// What dropping the chosen rows would do: the rows counted by scanner and reason, the name the new
// dataset would get, warnings and the exact command. Starts nothing.
export function checkDrop(datasetId: string, body: DropRequest): Promise<DropCheckResponse> {
  return request<DropCheckResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/drop/check`, body)
}

// Drops the chosen rows: registers the new filtered dataset and submits the job that makes it.
// The dataset the rows are dropped from is not changed.
export function submitDrop(datasetId: string, body: DropRequest): Promise<DropSubmitResponse> {
  return request<DropSubmitResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/drop/submit`, body)
}

// Every filter job of every filtered dataset, newest first (the Activity page).
export function listFilters(): Promise<FiltersResponse> {
  return request<FiltersResponse>('/api/filters')
}

// One filtered dataset's filter job: its state, what it drops, the command and log tails.
export function getFilter(datasetId: string): Promise<FilterStatusResponse> {
  return request<FilterStatusResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/filter`)
}

// Runs `scancel` on the one SLURM job the filtered dataset's record lists.
export function cancelFilter(datasetId: string): Promise<CancelResponse> {
  return request<CancelResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/filter/cancel`, {})
}

// What retrying a failed or cancelled drop would do: the same saved rows, a new output folder, the
// resources and the exact command. Starts nothing. Fails with the reason when it can't be retried.
export function checkFilterRetry(datasetId: string): Promise<FilterRetryCheckResponse> {
  return request<FilterRetryCheckResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/filter/retry/check`, {})
}

// Runs a failed or cancelled drop again as a new SLURM job on the same filtered dataset. The failed
// attempt's folder and logs are kept.
export function retryFilter(datasetId: string): Promise<FilterRetryResponse> {
  return request<FilterRetryResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/filter/retry`, {})
}
