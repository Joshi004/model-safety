import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, getChain, getRowFromOriginal, listChains } from '../api'
import { rowHash } from '../rows'
import type { ChainListItem, ChainResponse, OriginalRowResponse, RowTarget } from '../types'
import { useRefreshing } from '../useRefreshing'
import RowPage from './RowPage'

// The "Rows" view: pick a row three ways (a Prepared row, a row of the clean data,
// or an Original row) and read its story. Any other page that shows a row links
// here. Reading never changes anything.
//
// Safety: every value shown here comes from files on disk and renders as plain
// React text, never as markup.

type Mode = 'prepared' | 'clean' | 'original'

const DEFAULT_FILE = 'chunk-0/data_000000.jsonl'

const MODE_LABELS: Record<Mode, string> = {
  prepared: 'A Prepared row',
  clean: 'A row of the clean data',
  original: 'An Original row',
}

// What an Original row's lookup found, when it didn't find a row.
function describeMissing(result: OriginalRowResponse): string {
  if (result.matches.length === 0) {
    return result.runs_checked === 0
      ? 'No finished Prepared run read that file.'
      : `${result.runs_checked} Prepared run${result.runs_checked === 1 ? '' : 's'} read that file, but none belongs to this chain.`
  }
  return result.matches
    .map((match) => `${match.run}: ${match.error ?? match.note ?? match.status.replace('_', ' ')}`)
    .join('\n')
}

function Finder({
  chains,
  initial,
  onOpenRow,
}: {
  chains: ChainListItem[]
  initial: RowTarget | null
  onOpenRow: (target: RowTarget) => void
}) {
  const [mode, setMode] = useState<Mode>(initial?.from ?? 'prepared')
  const [chosenChain, setChosenChain] = useState(initial?.chainDir ?? '')
  const [run, setRun] = useState(initial?.run ?? '')
  const [file, setFile] = useState(initial?.file ?? DEFAULT_FILE)
  const [line, setLine] = useState(String(initial?.line ?? 1))
  const [originalPath, setOriginalPath] = useState('')
  const [originalKind, setOriginalKind] = useState<'row' | 'line'>('row')
  const [originalNumber, setOriginalNumber] = useState('0')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const chainDir = chosenChain || chains[0]?.chain_dir || ''
  // The chain's dataset names, to pick a run from.
  const { data: chain } = useRefreshing<ChainResponse | null>(
    () => (chainDir ? getChain(chainDir) : Promise.resolve(null)),
    chainDir,
    0,
    () => false,
  )

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setMessage(null)
    if (chainDir === '') {
      setMessage('There is no chain with a finished row status to read rows from.')
      return
    }
    if (mode === 'original') {
      const number = Number(originalNumber)
      const first = originalKind === 'row' ? 0 : 1
      if (originalPath.trim() === '' || !Number.isInteger(number) || number < first) {
        setMessage(`Give the Original file's absolute path and a whole ${originalKind} number of at least ${first}.`)
        return
      }
      setBusy(true)
      try {
        const result = await getRowFromOriginal({
          chain_dir: chainDir,
          original_path: originalPath.trim(),
          row: originalKind === 'row' ? number : null,
          line: originalKind === 'line' ? number : null,
        })
        if (result.row !== null) onOpenRow({ chainDir, ...result.row.location, from: 'prepared' })
        else setMessage(describeMissing(result))
      } catch (caught) {
        setMessage(caught instanceof ApiError ? caught.message : String(caught))
      } finally {
        setBusy(false)
      }
      return
    }
    const number = Number(line)
    if (run.trim() === '' || file.trim() === '' || !Number.isInteger(number) || number < 1) {
      setMessage('Give the run, the file in it, and a line number of at least 1.')
      return
    }
    onOpenRow({ chainDir, run: run.trim(), file: file.trim(), line: number, from: mode })
  }

  return (
    <form className="row-finder" onSubmit={handleSubmit}>
      <div className="row-tabs">
        {(Object.keys(MODE_LABELS) as Mode[]).map((item) => (
          <button
            key={item}
            type="button"
            className={mode === item ? 'row-tab selected' : 'row-tab'}
            onClick={() => setMode(item)}
          >
            {MODE_LABELS[item]}
          </button>
        ))}
      </div>

      <div className="start-field">
        <label htmlFor="row-chain">Chain</label>
        <select id="row-chain" value={chainDir} onChange={(event) => setChosenChain(event.target.value)}>
          {chains.map((item) => (
            <option key={item.chain_dir} value={item.chain_dir}>
              {item.name}
            </option>
          ))}
        </select>
      </div>

      {mode !== 'original' ? (
        <div className="start-resources">
          <div className="start-field">
            <label htmlFor="row-run">Dataset (run)</label>
            <input id="row-run" type="text" list="row-runs" value={run} onChange={(event) => setRun(event.target.value)} placeholder="rl_mix/train" />
            <datalist id="row-runs">
              {(chain?.datasets ?? []).map((item) => (
                <option key={item.run} value={item.run} />
              ))}
            </datalist>
          </div>
          <div className="start-field">
            <label htmlFor="row-file">{mode === 'clean' ? 'File in the clean data' : 'Prepared file'}</label>
            <input id="row-file" type="text" value={file} onChange={(event) => setFile(event.target.value)} />
          </div>
          <div className="start-field">
            <label htmlFor="row-line">Line (from 1)</label>
            <input id="row-line" type="number" min={1} value={line} onChange={(event) => setLine(event.target.value)} />
          </div>
        </div>
      ) : (
        <div className="start-resources">
          <div className="start-field wide-field">
            <label htmlFor="row-original-path">Original file (absolute path)</label>
            <input id="row-original-path" type="text" value={originalPath} onChange={(event) => setOriginalPath(event.target.value)} />
          </div>
          <div className="start-field">
            <label htmlFor="row-original-kind">Named by</label>
            <select
              id="row-original-kind"
              value={originalKind}
              onChange={(event) => {
                const kind = event.target.value as 'row' | 'line'
                setOriginalKind(kind)
                setOriginalNumber(kind === 'row' ? '0' : '1')
              }}
            >
              <option value="row">row (from 0)</option>
              <option value="line">line (from 1, JSONL files)</option>
            </select>
          </div>
          <div className="start-field">
            <label htmlFor="row-original-number">Number</label>
            <input
              id="row-original-number"
              type="number"
              min={originalKind === 'row' ? 0 : 1}
              value={originalNumber}
              onChange={(event) => setOriginalNumber(event.target.value)}
            />
          </div>
        </div>
      )}
      {mode === 'original' && (
        <p className="muted small">
          Looking an Original row up can take a few seconds: when it can't be worked out from the run's counts, the Prepared
          files are searched.
        </p>
      )}

      <div className="start-actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Looking…' : 'Show row'}
        </button>
      </div>
      {message !== null && <p className="error job-error">{message}</p>}
    </form>
  )
}

export default function RowsView({
  target,
  onOpenRow,
}: {
  target: RowTarget | null
  onOpenRow: (target: RowTarget) => void
}) {
  const { data, error } = useRefreshing(() => listChains(), 'chains-for-rows', 0, () => false)
  // Only a chain whose row status is finished can show rows.
  const chains = (data?.chains ?? []).filter((chain) => chain.row_status?.finished === true)

  return (
    <div className="rows-view">
      <h3>Rows</h3>
      {error && <p className="error">{error}</p>}
      {data === null && error === null && <p className="muted">Loading…</p>}
      {data && chains.length === 0 && (
        <p className="muted">No chain has a finished row status yet, so there are no rows to follow.</p>
      )}
      {data && chains.length > 0 && (
        // A new target (a link, or the browser's back button) starts the form again from it.
        <Finder key={target ? rowHash(target) : 'none'} chains={chains} initial={target} onOpenRow={onOpenRow} />
      )}
      {target !== null && <RowPage target={target} onOpenRow={onOpenRow} />}
      {target === null && data && chains.length > 0 && (
        <p className="muted">Pick a row above, or open one from a link on another page.</p>
      )}
    </div>
  )
}
