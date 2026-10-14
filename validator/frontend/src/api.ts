// Plain same-origin fetch wrapper -- no auth. This tool assumes a trusted
// network (see ../run.sh and backend/app.py): anyone who can reach the
// backend at all can use these routes.
//
// Only registerDataset and unregisterDataset act on anything: they write the
// dataset registry (and unregisterDataset deletes that dataset's own folder
// under the service root). Every other call here is read-only.
import type {
  Dataset,
  DatasetsResponse,
  HealthResponse,
  InspectResult,
  RegisterDatasetRequest,
  TeamsResponse,
  UnregisterResponse,
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

// Removes the entry and the dataset's own folder under the service root. The original data is not touched.
export function unregisterDataset(datasetId: string): Promise<UnregisterResponse> {
  return request<UnregisterResponse>(`/api/datasets/${encodeURIComponent(datasetId)}/unregister`, {})
}
