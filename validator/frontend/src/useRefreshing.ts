import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './api'
import { cacheGeneration, getCached, setCached } from './cache'

// Loads something from the backend, then loads it again every `pollMs` for as
// long as `keepPolling(data)` says it's still changing (a job queued or
// running). `loadKey` names what is being loaded: a new key loads again.
//
// A request that fails while it was still changing (say, the backend being
// restarted) is tried again, so the page picks up where it left off. A first
// request that fails is not: `reload` tries it again on demand.
//
// `cacheKey` (optional) names what is kept in the in-memory cache (cache.ts). With it, the
// page starts from the last answer it got, and `refreshing` is true while the first
// up-to-date answer is on its way. `load` is told whether to ask for live data (`fresh`):
// the first load of a key is, polls and `reload` accept what the backend saved a moment ago.
// A failed load leaves the data that is shown as it is.
export function useRefreshing<T>(
  load: (fresh: boolean) => Promise<T>,
  loadKey: string,
  pollMs: number,
  keepPolling: (data: T) => boolean,
  cacheKey?: string,
) {
  const [data, setData] = useState<T | null>(() => (cacheKey === undefined ? null : (getCached<T>(cacheKey) ?? null)))
  const [error, setError] = useState<string | null>(null)
  // Bumped to run the effect below again, by the timer or by `reload`.
  const [loads, setLoads] = useState(0)
  const stillChanging = useRef(false)
  // The key whose first up-to-date answer has arrived: the ref is read by the effect, the
  // state tells the page.
  const syncedKeyRef = useRef<string | null>(null)
  const [syncedKey, setSyncedKey] = useState<string | null>(null)

  // A new cache key starts from what the cache has for it, if anything. Done while rendering
  // (not in an effect) so the page never shows the old key's data as the new one's cached copy.
  const [seededFor, setSeededFor] = useState(cacheKey)
  if (cacheKey !== seededFor) {
    setSeededFor(cacheKey)
    const cached = cacheKey === undefined ? undefined : getCached<T>(cacheKey)
    if (cached !== undefined) setData(cached)
  }

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const loadAgainSoon = () => {
      timer = window.setTimeout(() => setLoads((count) => count + 1), pollMs)
    }
    const generation = cacheGeneration()
    load(syncedKeyRef.current !== loadKey)
      .then((response) => {
        if (cancelled) return
        if (cacheKey !== undefined) setCached(cacheKey, response, generation)
        syncedKeyRef.current = loadKey
        setSyncedKey(loadKey)
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
    // `load`, `keepPolling` and `cacheKey` are new values on every render; `loadKey`
    // says when what they load has really changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadKey, loads, pollMs])

  const reload = useCallback(() => setLoads((count) => count + 1), [])
  // Data is on screen (from the cache or an earlier key) and the up-to-date answer hasn't
  // come yet. Polls don't count: they are routine, and the page shouldn't flash on each.
  const refreshing = data !== null && syncedKey !== loadKey && error === null
  return { data, error, reload, refreshing }
}
