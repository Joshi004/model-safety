// The SLURM resource boxes of the conversion forms: what they hold, their starting
// values, and how they're sent. No React here.

import type { JobResources } from './types'

// What the resource boxes hold: text, exactly as typed.
export interface ResourceInputs {
  partition: string
  cpus: string
  mem: string
  time: string
}

// What the boxes offer: the partitions SLURM lists and the most CPUs a job may ask for, and
// the error when SLURM couldn't be asked (then there are no partitions). Both the
// conversion settings and the scanners' settings have these.
export interface ResourceOptions {
  partitions: string[]
  max_cpus: number
  slurm_error: string | null
}

// What the starting values come from: the partitions SLURM lists, and the defaults of the
// kind of job (a conversion's, or one scanner's).
export interface ResourceDefaults {
  partitions: string[]
  default_resources: JobResources
}

export const EMPTY_RESOURCES: ResourceInputs = { partition: '', cpus: '', mem: '', time: '' }

export function initialResources(source: ResourceDefaults): ResourceInputs {
  const defaults = source.default_resources
  const partition = source.partitions.includes(defaults.partition)
    ? defaults.partition
    : (source.partitions[0] ?? defaults.partition)
  return { partition, cpus: String(defaults.cpus), mem: defaults.mem, time: defaults.time }
}

// The boxes filled in with the resources a job already ran with (a retry of that job).
export function inputsFromResources(resources: JobResources): ResourceInputs {
  return {
    partition: resources.partition,
    cpus: String(resources.cpus),
    mem: resources.mem,
    time: resources.time,
  }
}

export function hasTypedAnything(inputs: ResourceInputs): boolean {
  return Boolean(inputs.partition || inputs.cpus || inputs.mem || inputs.time)
}

// A CPU count that isn't a whole number goes out as NaN, which JSON turns into
// null; the backend then refuses it in words.
export function toRequestResources(inputs: ResourceInputs): JobResources {
  const cpus = inputs.cpus.trim()
  return {
    partition: inputs.partition,
    cpus: /^\d+$/.test(cpus) ? Number(cpus) : Number.NaN,
    mem: inputs.mem.trim(),
    time: inputs.time.trim(),
  }
}
