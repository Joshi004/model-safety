import { useState } from 'react'
import { listChains } from '../api'
import { ACTIVE_STATES, STATE_LABELS, describeProgress } from '../chains'
import { formatCount, formatTimestamp } from '../format'
import type { ChainListItem, RemovedFilters } from '../types'
import { useRefreshing } from '../useRefreshing'
import ChainPage from './ChainPage'
import NewChainForm from './NewChainForm'
import { VerdictChip } from './StateChip'

// The "Chains" view: every chain under the chains root (newest first), what is
// started but not finished across them, and one chain's page once it's opened.
// Reading never changes anything; "New chain" makes one new folder, and a chain's
// page can start its steps (StartStepPanel).
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

const POLL_MS = 5000

function funnelText(chain: ChainListItem): string {
  const funnel = chain.funnel
  if (funnel === undefined) return '—'
  const counts = [funnel.prepared, ...funnel.steps.map((step) => step.kept)]
  return counts.map((count) => formatCount(count)).join(' → ')
}

function stageSummary(chain: ChainListItem): string {
  const stages = chain.stages ?? []
  const finished = stages.filter((stage) => stage.state === 'finished').length
  return `${finished} of ${stages.length} finished`
}

function hasUnfinished(chain: ChainListItem): boolean {
  return (chain.stages ?? []).some((stage) => ACTIVE_STATES.includes(stage.state))
}

function ChainsList({ onOpen }: { onOpen: (chainDir: string) => void }) {
  const [making, setMaking] = useState(false)
  const { data, error, reload } = useRefreshing(
    () => listChains(),
    'chains',
    POLL_MS,
    (response) => response.chains.some(hasUnfinished),
  )

  const chains = data?.chains ?? []
  // Newest first. A chain that can't be read has no date, so it goes last.
  const sorted = [...chains].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
  const unfinished = sorted.flatMap((chain) =>
    (chain.stages ?? [])
      .filter((stage) => ACTIVE_STATES.includes(stage.state))
      .map((stage) => ({ chain, stage })),
  )

  return (
    <div className="runs-list">
      <div className="runs-list-header">
        <h3>Chains</h3>
        <span>
          <button type="button" className="secondary" onClick={() => setMaking((current) => !current)}>
            New chain
          </button>{' '}
          <button type="button" className="secondary" onClick={reload}>
            Refresh
          </button>
        </span>
      </div>

      {making && (
        <NewChainForm
          onCancel={() => setMaking(false)}
          onCreated={(chainDir) => {
            setMaking(false)
            reload()
            onOpen(chainDir)
          }}
        />
      )}

      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}

      {data && (
        <>
          <p className="muted small">
            Chains root: <span className="mono location-text">{data.root}</span>
          </p>

          {unfinished.length > 0 && (
            <div className="banner banner-warning small chain-unfinished">
              <strong>Under way</strong> (queued, running, or started and not finished: where a step was started
              from here, its SLURM state says which)
              <ul className="lookup-differences">
                {unfinished.map(({ chain, stage }) => (
                  <li key={`${chain.chain_dir}|${stage.key}`}>
                    <button type="button" className="link-button" onClick={() => onOpen(chain.chain_dir)}>
                      {chain.name}
                    </button>{' '}
                    / {stage.label}: {STATE_LABELS[stage.state]}
                    {describeProgress(stage.progress) !== null && ` (${describeProgress(stage.progress)})`}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {sorted.length === 0 && (
            <p className="muted">
              No chains yet. A chain is a folder directly under the chains root with a{' '}
              <code>chain.json</code> in it.
            </p>
          )}

          {sorted.length > 0 && (
            <div className="skipped-table-wrap">
              <table className="skipped-table">
                <thead>
                  <tr>
                    <th>Chain</th>
                    <th>Created</th>
                    <th>Rows: Prepared → step 1 → clean</th>
                    <th>Stages</th>
                    <th>Trace</th>
                    <th>Row status</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((chain) => (
                    <tr key={chain.chain_dir} onClick={() => onOpen(chain.chain_dir)}>
                      <td title={chain.chain_dir}>
                        {/* A click on the button reaches the row's own onClick. */}
                        <button type="button" className="link-button">
                          {chain.name ?? chain.chain_dir}
                        </button>
                        {chain.problem !== undefined && <div className="error small">{chain.problem}</div>}
                        {chain.problems !== undefined && chain.problems.length > 0 && (
                          <div className="error small">
                            {chain.problems.length} problem{chain.problems.length === 1 ? '' : 's'}
                          </div>
                        )}
                      </td>
                      <td>{chain.created_at ? formatTimestamp(chain.created_at) : '—'}</td>
                      <td className="mono">{funnelText(chain)}</td>
                      <td>{chain.stages === undefined ? '—' : stageSummary(chain)}</td>
                      <td>
                        {chain.trace ? <VerdictChip verdict={chain.trace.verdict} /> : <span className="muted">—</span>}
                        {chain.trace && !chain.trace.finished && <span className="muted small">not finished</span>}
                      </td>
                      <td>
                        {chain.row_status ? (
                          <VerdictChip verdict={chain.row_status.verdict} />
                        ) : (
                          <span className="muted">—</span>
                        )}
                        {chain.row_status && !chain.row_status.finished && (
                          <span className="muted small">not finished</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small">Refreshes every {POLL_MS / 1000} seconds while any stage is under way.</p>
        </>
      )}
    </div>
  )
}

export default function ChainsView({
  chainDir,
  onSelectChain,
  onOpenRun,
  onOpenRemoved,
  onOpenJobs,
}: {
  chainDir: string | null
  onSelectChain: (chainDir: string | null) => void
  onOpenRun: (runDir: string) => void
  onOpenRemoved: (filters: RemovedFilters) => void
  onOpenJobs: () => void
}) {
  if (chainDir !== null) {
    return (
      <ChainPage
        key={chainDir}
        chainDir={chainDir}
        onBack={() => onSelectChain(null)}
        onOpenRun={onOpenRun}
        onOpenRemoved={onOpenRemoved}
        onOpenJobs={onOpenJobs}
      />
    )
  }
  return <ChainsList onOpen={onSelectChain} />
}
