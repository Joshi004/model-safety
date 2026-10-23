import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { errorMessage, getTeams, listDatasets } from '../api'
import { cacheGeneration, getCached, setCached } from '../cache'
import { hrefFor } from '../route'
import { isHolding } from '../scans'
import { loadString, saveString } from '../storage'
import type {
  ConversionSubmitResponse,
  Dataset,
  DatasetsResponse,
  ScanSubmitResponse,
  Team,
  UnregisterResponse,
} from '../types'
import ActionBar from './ActionBar'
import ConvertDialog from './ConvertDialog'
import DatasetsTable from './DatasetsTable'
import RegisterDatasetDialog from './RegisterDatasetDialog'
import ScanDialog from './ScanDialog'
import TeamPolicyDialog from './TeamPolicyDialog'
import UnregisterDialog from './UnregisterDialog'

const TEAM_KEY = 'datasets.team'
// Where the last answers are kept in the in-memory cache (cache.ts), so the page opens with them.
const TEAMS_CACHE = 'teams'
const DATASETS_CACHE = 'datasets'
// How often the list is read again while a scan is going, so the scan chips follow it.
const SCAN_POLL_MS = 10_000

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

export default function DatasetsPage() {
  const [teams, setTeams] = useState<Team[]>(() => getCached<Team[]>(TEAMS_CACHE) ?? [])
  const [datasets, setDatasets] = useState<Dataset[] | null>(
    () => getCached<DatasetsResponse>(DATASETS_CACHE)?.datasets ?? null,
  )
  const [serviceRoot, setServiceRoot] = useState(() => getCached<DatasetsResponse>(DATASETS_CACHE)?.service_root ?? '')
  const [loadError, setLoadError] = useState<string | null>(null)
  // Whether a read has come back since the page opened. The first one asks the backend for live
  // data; later ones (polls, reloads after a change) accept what it saved a moment ago. Until
  // the first one is back, what is shown may be the cached list.
  const hasLoaded = useRef(false)
  const [synced, setSynced] = useState(false)

  // '' is the "All" tab. The last tab is remembered in this browser.
  const [teamChoice, setTeamChoice] = useState(() => loadString(TEAM_KEY, ''))
  const [search, setSearch] = useState('')
  // Ticked datasets, by id. Kept while the team tab or the search changes.
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [registering, setRegistering] = useState(false)
  const [converting, setConverting] = useState(false)
  // The key (in SCAN_ACTIONS) of the scan action whose dialog is open, or null.
  const [scanning, setScanning] = useState<string | null>(null)
  const [unregistering, setUnregistering] = useState<Dataset | null>(null)
  // The team whose toxicity policy is being edited, or null.
  const [editingPolicy, setEditingPolicy] = useState<string | null>(null)
  const [notice, setNotice] = useState<ReactNode>(null)

  // Bumped to read the list again (after a register or unregister, or Retry).
  const [reloads, setReloads] = useState(0)

  useEffect(() => {
    // An answer that arrives after a newer request started, or after leaving the page, is dropped.
    let stale = false
    const generation = cacheGeneration()
    Promise.all([getTeams(), listDatasets(!hasLoaded.current)])
      .then(([teamsResponse, datasetsResponse]) => {
        if (stale) return
        setCached(TEAMS_CACHE, teamsResponse.teams, generation)
        setCached(DATASETS_CACHE, datasetsResponse, generation)
        hasLoaded.current = true
        setSynced(true)
        setLoadError(null)
        setTeams(teamsResponse.teams)
        setServiceRoot(datasetsResponse.service_root)
        setDatasets(datasetsResponse.datasets)
        // A dataset that is gone can't stay ticked.
        const ids = new Set(datasetsResponse.datasets.map((dataset) => dataset.id))
        setSelected((current) => new Set([...current].filter((id) => ids.has(id))))
      })
      .catch((error: unknown) => {
        if (!stale) setLoadError(errorMessage(error))
      })
    return () => {
      stale = true
    }
  }, [reloads])

  function reload() {
    setReloads((count) => count + 1)
  }

  // While any scan is waiting for a conversion, queued or running, or a filtered version's job is,
  // the list is read again every few seconds. The selection and the search are kept.
  const scanGoing = (datasets ?? []).some(
    (dataset) =>
      Object.values(dataset.scans ?? {}).some((summary) => isHolding(summary.state)) ||
      (dataset.filter != null && isHolding(dataset.filter.state)),
  )
  useEffect(() => {
    if (!scanGoing) return
    const timer = window.setInterval(() => setReloads((count) => count + 1), SCAN_POLL_MS)
    return () => window.clearInterval(timer)
  }, [scanGoing])

  // A remembered team that is no longer in the list counts as "All".
  const activeTeam = teams.some((team) => team.name === teamChoice) ? teamChoice : ''

  const countsByTeam = useMemo(() => {
    const counts = new Map<string, number>()
    for (const dataset of datasets ?? []) counts.set(dataset.team, (counts.get(dataset.team) ?? 0) + 1)
    return counts
  }, [datasets])

  const query = search.trim().toLowerCase()
  const visible = useMemo(
    () =>
      (datasets ?? []).filter(
        (dataset) =>
          (activeTeam === '' || dataset.team === activeTeam) &&
          (query === '' || dataset.name.toLowerCase().includes(query) || dataset.path.toLowerCase().includes(query)),
      ),
    [datasets, activeTeam, query],
  )

  function chooseTeam(name: string) {
    setTeamChoice(name)
    saveString(TEAM_KEY, name)
  }

  function toggleOne(id: string, checked: boolean) {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function toggleVisible(checked: boolean) {
    setSelected((current) => {
      const next = new Set(current)
      for (const dataset of visible) {
        if (checked) next.add(dataset.id)
        else next.delete(dataset.id)
      }
      return next
    })
  }

  function handleRegistered(dataset: Dataset) {
    setRegistering(false)
    setNotice(`Registered "${dataset.name}" under ${dataset.team}.`)
    // Make sure the new dataset isn't hidden by the current tab or search.
    if (activeTeam !== '' && activeTeam !== dataset.team) chooseTeam(dataset.team)
    setSearch('')
    reload()
  }

  function handleUnregistered(result: UnregisterResponse) {
    setUnregistering(null)
    setNotice(`Unregistered "${result.name}". The original data was not touched.`)
    setDatasets((current) => (current === null ? current : current.filter((dataset) => dataset.id !== result.id)))
    setSelected((current) => {
      const next = new Set(current)
      next.delete(result.id)
      return next
    })
    reload()
  }

  function handlePolicySaved(team: string) {
    setEditingPolicy(null)
    setNotice(
      `Saved the toxicity policy of ${team}. Scans started from now on use it for every ${team} dataset that has no policy of its own.`,
    )
  }

  function handleConverting() {
    setNotice(null)
    setConverting(true)
  }

  function handleConversionsStarted(result: ConversionSubmitResponse) {
    setConverting(false)
    const problems = result.errors.length > 0 ? ` Problems: ${result.errors.join(' ')}` : ''
    setNotice(
      <>
        Started {plural(result.started.length, 'conversion', 'conversions')}. Watch them on the{' '}
        <a href={hrefFor({ page: 'activity' })}>Activity page</a>.{problems}
      </>,
    )
    reload()
  }

  function handleScan(action: string) {
    setNotice(null)
    setScanning(action)
  }

  function handleScansStarted(result: ScanSubmitResponse) {
    setScanning(null)
    const jobs = result.scans.length + result.conversions.length
    const conversions =
      result.conversions.length > 0 ? ` and ${plural(result.conversions.length, 'conversion', 'conversions')}` : ''
    const problems = result.errors.length > 0 ? ` Problems: ${result.errors.join(' ')}` : ''
    setNotice(
      <>
        Started {plural(result.scans.length, 'scan', 'scans')}
        {conversions}. Watch {jobs === 1 ? 'it' : 'them'} on the <a href={hrefFor({ page: 'activity' })}>Activity page</a>.
        {problems}
      </>,
    )
    reload()
  }

  function emptyMessage(): string {
    if (datasets !== null && datasets.length === 0) {
      return 'No datasets are registered yet. Use "Register dataset" to add one.'
    }
    if (query !== '') return `No dataset in ${activeTeam === '' ? 'any team' : activeTeam} matches "${search.trim()}".`
    return `No datasets in ${activeTeam} yet.`
  }

  return (
    <>
      <div className="page-header">
        <h1>Datasets</h1>
        {datasets !== null && !synced && loadError === null && <span className="muted small">Updating…</span>}
        <button
          type="button"
          className="primary push-right"
          onClick={() => {
            setNotice(null)
            setRegistering(true)
          }}
        >
          Register dataset
        </button>
      </div>

      {notice !== null && (
        <div className="banner banner-ok" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      {loadError !== null && (
        <div className="banner banner-error" role="alert">
          <span>Couldn't load the datasets: {loadError}</span>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              setLoadError(null)
              reload()
            }}
          >
            Retry
          </button>
        </div>
      )}

      {datasets === null && loadError === null && (
        <div className="card empty-state">
          <p>Loading datasets…</p>
          <p className="muted small">The very first start builds the list from the old catalog, which can take a little while.</p>
        </div>
      )}

      {datasets !== null && (
        <div className="card">
          <div className="tabs" role="tablist" aria-label="Team">
            <button
              type="button"
              role="tab"
              aria-selected={activeTeam === ''}
              className={activeTeam === '' ? 'tab selected' : 'tab'}
              onClick={() => chooseTeam('')}
            >
              All <span className="tab-count">{datasets.length}</span>
            </button>
            {teams.map((team) => (
              <button
                key={team.name}
                type="button"
                role="tab"
                aria-selected={activeTeam === team.name}
                className={activeTeam === team.name ? 'tab selected' : 'tab'}
                title={team.description || undefined}
                onClick={() => chooseTeam(team.name)}
              >
                {team.name} <span className="tab-count">{countsByTeam.get(team.name) ?? 0}</span>
              </button>
            ))}
          </div>

          <div className="toolbar">
            <input
              type="search"
              className="search-input"
              placeholder="Search by name or path"
              aria-label="Search datasets by name or path"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <span className="muted small">
              {visible.length} {visible.length === 1 ? 'dataset' : 'datasets'} shown
            </span>
            {activeTeam !== '' && (
              <button
                type="button"
                className="secondary push-right"
                title={`The toxicity policy every ${activeTeam} dataset follows, unless it has one of its own`}
                onClick={() => {
                  setNotice(null)
                  setEditingPolicy(activeTeam)
                }}
              >
                Edit toxicity policy
              </button>
            )}
          </div>

          {selected.size > 0 && (
            <ActionBar
              count={selected.size}
              onClear={() => setSelected(new Set())}
              onScan={handleScan}
              onConvertOnly={handleConverting}
            />
          )}

          {visible.length === 0 ? (
            <p className="empty-state muted">{emptyMessage()}</p>
          ) : (
            <DatasetsTable
              datasets={visible}
              selected={selected}
              onToggle={toggleOne}
              onToggleAll={toggleVisible}
              onUnregister={setUnregistering}
            />
          )}
        </div>
      )}

      {registering && (
        <RegisterDatasetDialog
          teams={teams}
          defaultTeam={activeTeam}
          onClose={() => setRegistering(false)}
          onRegistered={handleRegistered}
        />
      )}

      {converting && datasets !== null && (
        <ConvertDialog
          datasets={datasets.filter((dataset) => selected.has(dataset.id))}
          onClose={() => setConverting(false)}
          onStarted={handleConversionsStarted}
        />
      )}

      {scanning !== null && datasets !== null && (
        <ScanDialog
          action={scanning}
          datasets={datasets.filter((dataset) => selected.has(dataset.id))}
          onClose={() => setScanning(null)}
          onStarted={handleScansStarted}
        />
      )}

      {editingPolicy !== null && (
        <TeamPolicyDialog team={editingPolicy} onClose={() => setEditingPolicy(null)} onSaved={handlePolicySaved} />
      )}

      {unregistering !== null && (
        <UnregisterDialog
          dataset={unregistering}
          serviceRoot={serviceRoot}
          onClose={() => setUnregistering(null)}
          onUnregistered={handleUnregistered}
        />
      )}
    </>
  )
}
