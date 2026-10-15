// Hash routing: the address is `#/datasets`, `#/datasets/<id>`, `#/datasets/<id>/<tab>`,
// `#/datasets/<id>/results/<scanner>`, `#/activity` or `#/activity/<scan id>`. Anything else
// (an old link, a typo, no hash at all) is replaced with `#/datasets`.
import { useEffect, useState } from 'react'
import { isScannerKey } from './scans'

export const DATASET_TABS = ['overview', 'data', 'conversion', 'results', 'history'] as const
export type DatasetTab = (typeof DATASET_TABS)[number]

export type Route =
  | { page: 'datasets' }
  // `#/datasets/<id>` is the overview. `scanner` is only ever set with the results tab: the
  // result page of one scanner.
  | { page: 'dataset'; id: string; tab: DatasetTab; scanner: string | null }
  // `scan` is a scan to show open: `#/activity/<scan id>`.
  | { page: 'activity'; scan: string | null }

// What a link can ask for: a dataset link may leave the tab out (the overview).
export type Target =
  | { page: 'datasets' }
  | { page: 'dataset'; id: string; tab?: DatasetTab; scanner?: string }
  | { page: 'activity'; scan?: string }

const DATASETS_ROUTE: { page: 'datasets' } = { page: 'datasets' }

// The same patterns as backend/registry.py's DATASET_ID and backend/jobs.py's BATCH_ID.
const DATASET_ID = /^ds-[0-9a-f]{12}$/
const SCAN_ID = /^\d{8}-\d{6}-[0-9a-f]{4}$/
const DATASET_PATH = /^\/datasets\/([^/]+)(?:\/([a-z]+)(?:\/([a-z_]+))?)?$/
const ACTIVITY_PATH = /^\/activity(?:\/([^/]+))?$/

function isTab(value: string): value is DatasetTab {
  return (DATASET_TABS as readonly string[]).includes(value)
}

// The route a hash stands for, or null when it isn't one of ours.
export function parseHash(hash: string): Route | null {
  const path = hash.startsWith('#') ? hash.slice(1) : hash
  if (path === '/datasets') return { page: 'datasets' }
  const activity = ACTIVITY_PATH.exec(path)
  if (activity !== null) {
    const scan = activity[1] ?? null
    return scan === null || SCAN_ID.test(scan) ? { page: 'activity', scan } : null
  }
  const match = DATASET_PATH.exec(path)
  if (match === null || !DATASET_ID.test(match[1])) return null
  const tab = match[2] ?? 'overview'
  if (!isTab(tab)) return null
  const scanner = match[3] ?? null
  if (scanner === null) return { page: 'dataset', id: match[1], tab, scanner: null }
  // A scanner only goes with the results tab, and has to be one the service has.
  return tab === 'results' && isScannerKey(scanner) ? { page: 'dataset', id: match[1], tab, scanner } : null
}

export function hrefFor(target: Target): string {
  switch (target.page) {
    case 'datasets':
      return '#/datasets'
    case 'dataset': {
      if (target.scanner !== undefined) return `#/datasets/${target.id}/results/${target.scanner}`
      return target.tab === undefined || target.tab === 'overview'
        ? `#/datasets/${target.id}`
        : `#/datasets/${target.id}/${target.tab}`
    }
    case 'activity':
      return target.scan === undefined ? '#/activity' : `#/activity/${target.scan}`
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
