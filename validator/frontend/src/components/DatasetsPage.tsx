import { useEffect, useMemo, useState } from 'react'
import { errorMessage, getTeams, listDatasets } from '../api'
import { loadString, saveString } from '../storage'
import type { Dataset, Team, UnregisterResponse } from '../types'
import ActionBar from './ActionBar'
import DatasetsTable from './DatasetsTable'
import RegisterDatasetDialog from './RegisterDatasetDialog'
import UnregisterDialog from './UnregisterDialog'

const TEAM_KEY = 'datasets.team'

export default function DatasetsPage() {
  const [teams, setTeams] = useState<Team[]>([])
  const [datasets, setDatasets] = useState<Dataset[] | null>(null)
  const [serviceRoot, setServiceRoot] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)

  // '' is the "All" tab. The last tab is remembered in this browser.
  const [teamChoice, setTeamChoice] = useState(() => loadString(TEAM_KEY, ''))
  const [search, setSearch] = useState('')
  // Ticked datasets, by id. Kept while the team tab or the search changes.
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [registering, setRegistering] = useState(false)
  const [unregistering, setUnregistering] = useState<Dataset | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Bumped to read the list again (after a register or unregister, or Retry).
  const [reloads, setReloads] = useState(0)

  useEffect(() => {
    // An answer that arrives after a newer request started, or after leaving the page, is dropped.
    let stale = false
    Promise.all([getTeams(), listDatasets()])
      .then(([teamsResponse, datasetsResponse]) => {
        if (stale) return
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
          </div>

          {selected.size > 0 && <ActionBar count={selected.size} onClear={() => setSelected(new Set())} />}

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
