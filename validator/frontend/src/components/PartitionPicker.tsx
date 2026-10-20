import { useId } from 'react'
import { formatCount } from '../format'
import { usePartitionUsage } from '../usePartitionUsage'

// The partition choice of the Convert and Scan forms, shown before the Advanced section:
// a table of every partition with how many nodes and GPUs are free and who holds the rest,
// so a partition can be chosen knowing how busy it is. Clicking a row chooses it.
//
// `partitions` is what SLURM lists for the form. If the usage can't be read, the
// partitions are offered as a plain list, so one can still be chosen.
//
// `error` is why the form couldn't get the partitions at all; `jobWord` says what needs one
// ("Converting", "Scanning").
export default function PartitionPicker({
  partitions,
  selected,
  onSelect,
  error,
  onReload,
  jobWord = 'Converting',
}: {
  partitions: string[]
  selected: string
  onSelect: (partition: string) => void
  error: string | null
  onReload: () => void
  jobWord?: string
}) {
  const id = useId()
  const usage = usePartitionUsage()
  const rows = usage.data?.partitions ?? []
  const usageProblem = usage.error ?? usage.data?.slurm_error ?? null

  const choices = partitions.includes(selected) || !selected ? partitions : [selected, ...partitions]

  return (
    <div className="partition-picker">
      <h3 className="scan-heading">Partition</h3>

      {rows.length > 0 ? (
        <>
          <table className="partition-table">
            <thead>
              <tr>
                <th>Partition</th>
                <th title="Nodes with no job on them, out of all the partition's nodes.">Free nodes</th>
                <th title="GPUs no job holds, out of all the partition's GPUs. Nodes that are draining or down give none.">
                  Free GPUs
                </th>
                <th title="GPUs held by jobs that run in this partition.">Used here</th>
                <th title="GPUs held by jobs in the background partition, on this partition's nodes.">
                  Used by background
                </th>
                <th title="GPUs held by jobs of any other partition that share these nodes.">Used by others</th>
                <th title="Free GPUs plus the ones background jobs hold: a job here can preempt background jobs.">
                  Free if background is preempted
                </th>
                <th title="Jobs running and waiting in this partition, and the GPUs the waiting ones ask for.">
                  Running / queued
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.name}
                  className={row.name === selected ? 'selected' : undefined}
                  onClick={() => onSelect(row.name)}
                >
                  <td>
                    <label className="scan-choice">
                      <input
                        type="radio"
                        name={`${id}-partition`}
                        checked={row.name === selected}
                        onChange={() => onSelect(row.name)}
                      />
                      <strong>{row.name}</strong>
                    </label>
                  </td>
                  <td>
                    {formatCount(row.nodes_free)} / {formatCount(row.nodes_total)}
                    {row.nodes_unavailable > 0 && (
                      <span className="muted small" title="Nodes that are draining or down.">
                        {' '}
                        ({row.nodes_unavailable} out)
                      </span>
                    )}
                  </td>
                  <td>
                    {formatCount(row.gpus_free)} / {formatCount(row.gpus_total)}
                  </td>
                  <td>{formatCount(row.gpus_used_by_self)}</td>
                  <td>{formatCount(row.gpus_used_by_background)}</td>
                  <td>{formatCount(row.gpus_used_by_others)}</td>
                  <td>{formatCount(row.gpus_reclaimable)}</td>
                  <td>
                    {formatCount(row.jobs_running)} / {formatCount(row.jobs_queued)}
                    {row.jobs_queued > 0 && row.gpus_queued > 0 && (
                      <span className="muted small"> ({formatCount(row.gpus_queued)} GPUs waiting)</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            {usage.data !== null && `Updated ${new Date(usage.data.read_at).toLocaleTimeString()}. `}
            It refreshes every 30 seconds.{' '}
            <button type="button" className="link-button" onClick={usage.reload}>
              Refresh now
            </button>
          </p>
        </>
      ) : (
        <>
          {usageProblem === null && usage.data === null && <p className="muted small">Reading how busy the partitions are…</p>}
          <div className="start-field">
            <label htmlFor={`${id}-partition`}>Partition</label>
            <select id={`${id}-partition`} value={selected} onChange={(event) => onSelect(event.target.value)}>
              {!selected && <option value="">Choose…</option>}
              {choices.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {usageProblem !== null && (
        <div className="banner banner-warning small">
          How busy the partitions are couldn't be read. {usageProblem}{' '}
          <button type="button" className="secondary" onClick={usage.reload}>
            Try again
          </button>
        </div>
      )}

      {selected === 'background' && (
        <p className="muted small">
          The background partition has the lowest priority. A job there can be preempted by other jobs and then
          restarts from the beginning. Pick another partition for work that has to finish on time.
        </p>
      )}

      {error !== null && (
        <div className="banner banner-error small">
          {error} {jobWord} needs a partition from SLURM.{' '}
          <button type="button" className="secondary" onClick={onReload}>
            Try again
          </button>
        </div>
      )}
    </div>
  )
}
