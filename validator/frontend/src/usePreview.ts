import { useEffect, useState } from 'react'
import { errorMessage, inspectPath, openRun, runPreview } from './api'
import type { InspectResult, PreviewRequest, PreviewResponse, RunResponse } from './types'

interface Outcome<T> {
  key: string
  data: T | null
  error: string | null
}

// Loads something when `key` changes (null means nothing to load). An answer that arrives
// for an older key is dropped, so a slow request can't overwrite a newer one. While the
// newest request is running, `data` stays the last answer so the page doesn't blink, and
// `loading` is true.
export function useRequest<T>(key: string | null, load: () => Promise<T>) {
  const [outcome, setOutcome] = useState<Outcome<T> | null>(null)

  useEffect(() => {
    if (key === null) return
    let cancelled = false
    load()
      .then((data) => {
        if (!cancelled) setOutcome({ key, data, error: null })
      })
      .catch((caught: unknown) => {
        if (!cancelled) setOutcome({ key, data: null, error: errorMessage(caught) })
      })
    return () => {
      cancelled = true
    }
    // `load` is a new function on every render; `key` says when what it loads has changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  if (key === null) return { data: null, error: null, loading: false }
  const failed = outcome?.key === key && outcome.error !== null
  return {
    data: failed ? null : (outcome?.data ?? null),
    error: failed ? outcome.error : null,
    loading: outcome?.key !== key,
  }
}

// POST /api/preview: a bounded sample of rows and what converting them would do. Pass null
// to load nothing. Read-only.
export function usePreview(request: PreviewRequest | null) {
  return useRequest<PreviewResponse>(request === null ? null : JSON.stringify(request), () =>
    runPreview(request as PreviewRequest),
  )
}

// POST /api/inspect: what is at a path. Pass null to load nothing. Read-only.
export function useInspect(path: string | null) {
  return useRequest<InspectResult>(path, () => inspectPath(path as string))
}

// POST /api/run: the summary of a Prepared version's folder. Pass null to load nothing.
export function useRun(runDir: string | null) {
  return useRequest<RunResponse>(runDir, () => openRun(runDir as string))
}
