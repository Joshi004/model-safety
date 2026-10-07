import { useId } from 'react'
import type { ResourceInputs } from '../resources'
import type { JobsSettings } from '../types'

// The SLURM resource boxes shared by the Start panel and the batch review:
// partition, CPUs, memory and time limit, all required, with the note on why
// and the banner for when SLURM's partitions couldn't be read. The ids come
// from useId because the Inspect view and the review view are both mounted
// at the same time, so two copies of this exist on the page.
export default function ResourcesFields({
  settings,
  settingsError,
  onReloadSettings,
  resources,
  onResourcesChange,
}: {
  settings: JobsSettings | null
  settingsError: string | null
  onReloadSettings: () => void
  resources: ResourceInputs
  onResourcesChange: (next: ResourceInputs) => void
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
      </div>
      <p className="muted small">
        Memory and time are always required: on this cluster a job without them is given a whole GPU node's
        memory and no end time, which blocks that node for everyone else. No GPU is ever requested.
      </p>

      {(settingsError || settings?.slurm_error) && (
        <div className="banner banner-error small">
          {settingsError ?? settings?.slurm_error} Starting needs a partition from SLURM.{' '}
          <button type="button" className="secondary" onClick={onReloadSettings}>
            Try again
          </button>
        </div>
      )}
    </>
  )
}
