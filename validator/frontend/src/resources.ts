// The SLURM resource boxes of the conversion forms: what they hold, their starting
// values, and how they're sent. No React here.

import type { ConversionSettings, JobResources } from './types'

// What the resource boxes hold: text, exactly as typed.
export interface ResourceInputs {
  partition: string
  cpus: string
  mem: string
  time: string
}

export const EMPTY_RESOURCES: ResourceInputs = { partition: '', cpus: '', mem: '', time: '' }

export function initialResources(settings: ConversionSettings): ResourceInputs {
  const defaults = settings.default_resources
  const partition = settings.partitions.includes(defaults.partition)
    ? defaults.partition
    : (settings.partitions[0] ?? defaults.partition)
  return { partition, cpus: String(defaults.cpus), mem: defaults.mem, time: defaults.time }
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
