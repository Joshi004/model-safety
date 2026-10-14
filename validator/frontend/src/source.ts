// Which part of a dataset's original data a preview reads, and the conversion settings
// built from it. No React here.
import type { RequestFlags } from './mapping'
import type { DatasetConversion, InspectResult } from './types'

// The settings that decide what a preview or a conversion reads. The mapping is separate.
export interface SourceChoice {
  input_type: string
  // A csv or json dataset in a folder reads one file, named relative to the folder.
  file: string | null
  split: string
  hf_config: string
}

export function choiceFromConversion(conversion: DatasetConversion): SourceChoice {
  return {
    input_type: conversion.input_type,
    file: conversion.file,
    split: conversion.split,
    hf_config: conversion.hf_config,
  }
}

// The conversion settings to save, from what the Conversion tab holds.
export function conversionOf(choice: SourceChoice, flags: RequestFlags): DatasetConversion {
  return {
    input_type: choice.input_type,
    file: choice.file,
    split: choice.split,
    hf_config: choice.hf_config,
    map: flags.map,
    require: flags.require,
    exclude_metadata: flags.exclude_metadata,
  }
}

// The settings in a form that compares equal when they mean the same: lists and mappings
// sorted, as the converter's own command sorts them.
function normalised(conversion: DatasetConversion): string {
  return JSON.stringify([
    conversion.input_type,
    conversion.file ?? null,
    conversion.split,
    conversion.hf_config,
    Object.entries(conversion.map).sort(),
    [...conversion.require].sort(),
    [...conversion.exclude_metadata].sort(),
  ])
}

export function sameConversion(a: DatasetConversion, b: DatasetConversion): boolean {
  return normalised(a) === normalised(b)
}

// The first format a reader understands, and for a folder of csv or json files, its first
// file. Null when nothing in the inspected path can be converted.
export function defaultChoice(inspect: InspectResult): SourceChoice | null {
  const group = inspect.groups.find((item) => item.input_type !== null)
  if (group === undefined || group.input_type === null) return null
  const needsFile = group.sample_target === 'file' && inspect.kind !== 'file'
  return { input_type: group.input_type, file: needsFile ? (group.files[0] ?? null) : null, split: 'train', hf_config: '' }
}
