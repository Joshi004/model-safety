import { useId } from 'react'
import type { ResourceInputs, ResourceOptions } from '../resources'

// The SLURM resource boxes shared by the Conversion tab, the Convert-only dialog and the
// scan dialog: CPUs, memory and time limit, all required, with the note on why. The
// partition is chosen in the PartitionPicker above them, not here. The ids come from useId
// so two copies on one page don't share them.
//
// `usesGpu` is for a scan that runs on GPUs: only then is the note about GPUs left out,
// because these boxes don't ask for any, and the scan's own jobs do. `partitionOnly` is for
// a scan whose job scripts fix the CPUs, memory, time and GPUs: no box is shown, only the
// note.
export default function ResourcesFields({
  settings,
  resources,
  onResourcesChange,
  usesGpu = false,
  partitionOnly = false,
}: {
  settings: ResourceOptions | null
  resources: ResourceInputs
  onResourcesChange: (next: ResourceInputs) => void
  usesGpu?: boolean
  partitionOnly?: boolean
}) {
  const id = useId()

  function setResource(name: keyof ResourceInputs, value: string) {
    onResourcesChange({ ...resources, [name]: value })
  }

  return (
    <>
      {!partitionOnly && (
        <div className="start-resources">
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
      )}
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
    </>
  )
}
