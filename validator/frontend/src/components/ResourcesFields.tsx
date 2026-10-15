import { useId } from 'react'
import type { ResourceInputs, ResourceOptions } from '../resources'

// The SLURM resource boxes shared by the Conversion tab, the Convert-only dialog and the
// scan dialog: partition, CPUs, memory and time limit, all required, with the note on why
// and the banner for when SLURM's partitions couldn't be read. The ids come from useId so
// two copies on one page don't share them.
//
// `jobWord` says what needs a partition ("Converting", "Scanning"). `usesGpu` is for a scan
// that runs on GPUs: only then is the note about GPUs left out, because these boxes don't
// ask for any, and the scan's own jobs do. `partitionOnly` is for a scan whose job scripts fix
// the CPUs, memory, time and GPUs: only the partition is asked for.
export default function ResourcesFields({
  settings,
  settingsError,
  onReloadSettings,
  resources,
  onResourcesChange,
  jobWord = 'Converting',
  usesGpu = false,
  partitionOnly = false,
}: {
  settings: ResourceOptions | null
  settingsError: string | null
  onReloadSettings: () => void
  resources: ResourceInputs
  onResourcesChange: (next: ResourceInputs) => void
  jobWord?: string
  usesGpu?: boolean
  partitionOnly?: boolean
}) {
  const id = useId()
  const partitions = settings?.partitions ?? []
  const partitionChoices =
    partitions.includes(resources.partition) || !resources.partition
      ? partitions
      : [resources.partition, ...partitions]

  function setResource(name: keyof ResourceInputs, value: string) {
    onResourcesChange({ ...resources, [name]: value })
  }

  return (
    <>
      <div className="start-resources">
        <div className="start-field">
          <label htmlFor={`${id}-partition`}>Partition</label>
          <select
            id={`${id}-partition`}
            value={resources.partition}
            onChange={(event) => setResource('partition', event.target.value)}
          >
            {!resources.partition && <option value="">Choose…</option>}
            {partitionChoices.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        {!partitionOnly && (
          <>
            <div className="start-field">
              <label htmlFor={`${id}-cpus`}>CPUs{settings ? ` (1–${settings.max_cpus})` : ''}</label>
              <input
                id={`${id}-cpus`}
                type="text"
                inputMode="numeric"
                value={resources.cpus}
                onChange={(event) => setResource('cpus', event.target.value)}
                autoComplete="off"
              />
            </div>
            <div className="start-field">
              <label htmlFor={`${id}-mem`}>Memory</label>
              <input
                id={`${id}-mem`}
                type="text"
                value={resources.mem}
                placeholder="32G"
                onChange={(event) => setResource('mem', event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            <div className="start-field">
              <label htmlFor={`${id}-time`}>Time limit</label>
              <input
                id={`${id}-time`}
                type="text"
                value={resources.time}
                placeholder="04:00:00"
                onChange={(event) => setResource('time', event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </div>
          </>
        )}
      </div>
      {partitionOnly ? (
        <p className="muted small">
          This scan's job scripts set its CPUs, memory, time limit and GPUs, always with a memory and a time limit, so
          that a job never gets a whole node's memory and no end. What they ask for is in the cost shown with the scan.
        </p>
      ) : (
        <p className="muted small">
          Memory and time are always required: on this cluster a job without them is given a whole GPU node's
          memory and no end time, which blocks that node for everyone else.
          {!usesGpu && ' No GPU is ever requested.'}
        </p>
      )}

      {(settingsError || settings?.slurm_error) && (
        <div className="banner banner-error small">
          {settingsError ?? settings?.slurm_error} {jobWord} needs a partition from SLURM.{' '}
          <button type="button" className="secondary" onClick={onReloadSettings}>
            Try again
          </button>
        </div>
      )}
    </>
  )
}
