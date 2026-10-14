// Hash routing: the address is `#/datasets`, `#/datasets/<id>`, `#/datasets/<id>/<tab>` or
// `#/activity`. Anything else (an old link, a typo, no hash at all) is replaced with `#/datasets`.
import { useEffect, useState } from 'react'

export const DATASET_TABS = ['overview', 'data', 'conversion', 'results', 'history'] as const
export type DatasetTab = (typeof DATASET_TABS)[number]

export type Route =
  | { page: 'datasets' }
  // `#/datasets/<id>` is the overview.
  | { page: 'dataset'; id: string; tab: DatasetTab }
  | { page: 'activity' }

// What a link can ask for: a dataset link may leave the tab out (the overview).
export type Target = { page: 'datasets' } | { page: 'dataset'; id: string; tab?: DatasetTab } | { page: 'activity' }

const DATASETS_ROUTE: Route = { page: 'datasets' }

// The same pattern as backend/registry.py's DATASET_ID.
const DATASET_ID = /^ds-[0-9a-f]{12}$/
const DATASET_PATH = /^\/datasets\/([^/]+)(?:\/([a-z]+))?$/

function isTab(value: string): value is DatasetTab {
  return (DATASET_TABS as readonly string[]).includes(value)
}

// The route a hash stands for, or null when it isn't one of ours.
export function parseHash(hash: string): Route | null {
  const path = hash.startsWith('#') ? hash.slice(1) : hash
  if (path === '/datasets') return { page: 'datasets' }
  if (path === '/activity') return { page: 'activity' }
  const match = DATASET_PATH.exec(path)
  if (match === null || !DATASET_ID.test(match[1])) return null
  const tab = match[2] ?? 'overview'
  return isTab(tab) ? { page: 'dataset', id: match[1], tab } : null
}

export function hrefFor(target: Target): string {
  switch (target.page) {
    case 'datasets':
      return '#/datasets'
    case 'dataset':
      return target.tab === undefined || target.tab === 'overview'
        ? `#/datasets/${target.id}`
        : `#/datasets/${target.id}/${target.tab}`
    case 'activity':
      return '#/activity'
  }
}

function currentRoute(): Route {
  return parseHash(window.location.hash) ?? DATASETS_ROUTE
}

// Replaces the address instead of adding to the history, so Back doesn't land on the bad one.
function fixAddress(): void {
  if (parseHash(window.location.hash) === null) {
    window.history.replaceState(null, '', hrefFor(DATASETS_ROUTE))
  }
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(currentRoute)

  useEffect(() => {
    function onHashChange() {
      fixAddress()
      setRoute(currentRoute())
    }
    fixAddress()
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return route
}
