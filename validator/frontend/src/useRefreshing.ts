import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './api'

// Loads something from the backend, then loads it again every `pollMs` for as
// long as `keepPolling(data)` says it's still changing (a job queued or
// running). `loadKey` names what is being loaded: a new key loads again.
//
// A request that fails while it was still changing (say, the backend being
// restarted) is tried again, so the page picks up where it left off. A first
// request that fails is not: `reload` tries it again on demand.
export function useRefreshing<T>(
  load: () => Promise<T>,
  loadKey: string,
  pollMs: number,
  keepPolling: (data: T) => boolean,
) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumped to run the effect below again, by the timer or by `reload`.
  const [loads, setLoads] = useState(0)
  const stillChanging = useRef(false)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const loadAgainSoon = () => {
      timer = window.setTimeout(() => setLoads((count) => count + 1), pollMs)
    }
    load()
      .then((response) => {
        if (cancelled) return
        setData(response)
        setError(null)
        stillChanging.current = keepPolling(response)
        if (stillChanging.current) loadAgainSoon()
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setError(caught instanceof ApiError ? caught.message : String(caught))
        if (stillChanging.current) loadAgainSoon()
      })
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
    // `load` and `keepPolling` are new functions on every render; `loadKey`
    // says when what they load has really changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadKey, loads, pollMs])

  const reload = useCallback(() => setLoads((count) => count + 1), [])
  return { data, error, reload }
}
