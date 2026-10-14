// Hash routing: the address is `#/datasets`, `#/datasets/<id>` or `#/activity`.
// Anything else (an old link, a typo, no hash at all) is replaced with `#/datasets`.
import { useEffect, useState } from 'react'

export type Route = { page: 'datasets' } | { page: 'dataset'; id: string } | { page: 'activity' }

const DATASETS_ROUTE: Route = { page: 'datasets' }

// The same pattern as backend/registry.py's DATASET_ID.
const DATASET_ID = /^ds-[0-9a-f]{12}$/
const DATASET_PATH = /^\/datasets\/([^/]+)$/

// The route a hash stands for, or null when it isn't one of ours.
export function parseHash(hash: string): Route | null {
  const path = hash.startsWith('#') ? hash.slice(1) : hash
  if (path === '/datasets') return { page: 'datasets' }
  if (path === '/activity') return { page: 'activity' }
  const match = DATASET_PATH.exec(path)
  if (match !== null && DATASET_ID.test(match[1])) return { page: 'dataset', id: match[1] }
  return null
}

export function hrefFor(route: Route): string {
  switch (route.page) {
    case 'datasets':
      return '#/datasets'
    case 'dataset':
      return `#/datasets/${route.id}`
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
