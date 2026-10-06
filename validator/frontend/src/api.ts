// Plain same-origin fetch wrapper -- no auth. This tool assumes a trusted
// network (see ../run.sh and backend/app.py): anyone who can reach the
// backend at all can use these routes.
import type {
  HealthResponse,
  InspectResult,
  LookupRequest,
  LookupResponse,
  PreviewRequest,
  PreviewResponse,
  RunResponse,
  SkippedPageRequest,
  SkippedPageResponse,
  SourcesResponse,
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

export function openRun(runDir: string): Promise<RunResponse> {
  return request<RunResponse>('/api/run', { run_dir: runDir })
}

export function getSkippedPage(body: SkippedPageRequest): Promise<SkippedPageResponse> {
  return request<SkippedPageResponse>('/api/run/skipped', body)
}

export function lookupOrigin(body: LookupRequest): Promise<LookupResponse> {
  return request<LookupResponse>('/api/origin', body)
}
