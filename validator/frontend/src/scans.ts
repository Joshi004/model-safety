// Words for scans and scanners, and what the scan dialog does with a scanner's settings.
// No React here.
//
// The Datasets page doesn't ask GET /api/scanners (that runs `sinfo`), so the few words it
// needs for a scanner are here. A scanner this file doesn't know is shown by its key.
import type { ChipTone } from './components/Chip'
import { formatCount } from './format'
import type { ScanState, ScanSummary, ScannerField, ScannerInfo } from './types'

const SCANNER_LABELS: Record<string, string> = {
  dedup: 'Duplicates',
}

// What a finished scan's headline count counts, by scanner.
const SCANNER_COUNT_LABELS: Record<string, string> = {
  dedup: 'duplicate rows',
}

export function scannerLabel(key: string): string {
  return SCANNER_LABELS[key] ?? key
}

export const STATE_LABELS: Record<ScanState, string> = {
  not_scanned: 'Not scanned',
  waiting_for_conversion: 'Converting',
  starting: 'Starting',
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
  conversion_failed: 'Conversion failed',
  unknown: 'Unknown',
}

// The Activity page spells out what a failed conversion means for the scan (D3).
export function stateLabelLong(state: ScanState): string {
  return state === 'conversion_failed' ? 'Not started: conversion failed' : STATE_LABELS[state]
}

export const STATE_TONES: Record<ScanState, ChipTone> = {
  not_scanned: 'neutral',
  waiting_for_conversion: 'neutral',
  starting: 'neutral',
  queued: 'neutral',
  running: 'neutral',
  succeeded: 'ok',
  failed: 'error',
  cancelled: 'warn',
  conversion_failed: 'error',
  unknown: 'warn',
}

// The states of a scan that hasn't ended: the same as backend/scans.py's HOLDING_STATES. A
// scan whose state couldn't be read counts, because its job may still be running.
export const HOLDING_STATES: readonly ScanState[] = ['waiting_for_conversion', 'starting', 'queued', 'running', 'unknown']

export function isHolding(state: ScanState): boolean {
  return HOLDING_STATES.includes(state)
}

// "Done (12)" for a finished scan with a count, otherwise the state's words.
export function chipLabel(summary: ScanSummary): string {
  if (summary.state === 'succeeded' && summary.count !== null) return `Done (${formatCount(summary.count)})`
  return STATE_LABELS[summary.state]
}

// What the chip's tooltip says: why, when there is a reason, or what the count counts.
export function chipTitle(scannerKey: string, summary: ScanSummary): string | undefined {
  if (summary.reason !== null && summary.reason !== '') return summary.reason
  if (summary.state === 'succeeded' && summary.count !== null) {
    return `${formatCount(summary.count)} ${SCANNER_COUNT_LABELS[scannerKey] ?? 'rows'} flagged`
  }
  return undefined
}

// ---- The scan dialog's settings ----

// Settings the dialog doesn't offer. They are never sent, so the scanner's own default
// applies. "Write filtered copies" would write copies nobody uses: a scan only flags.
const HIDDEN_SETTINGS: Record<string, string[]> = {
  dedup: ['write_filtered'],
}

export function visibleFields(scanner: ScannerInfo): ScannerField[] {
  const hidden = HIDDEN_SETTINGS[scanner.key] ?? []
  return scanner.fields.filter((field) => !hidden.includes(field.name))
}

// A setting whose only two values are "0" and "1" is a checkbox.
export function isToggle(field: ScannerField): boolean {
  return field.choices.length === 2 && field.choices.includes('0') && field.choices.includes('1')
}

// The default of every setting the dialog offers.
export function defaultSettings(scanner: ScannerInfo): Record<string, string> {
  return Object.fromEntries(visibleFields(scanner).map((field) => [field.name, field.default]))
}

// How a choice is named, by `<scanner>/<setting>`. A choice not listed is shown as it is.
const CHOICE_LABELS: Record<string, Record<string, string>> = {
  'dedup/text_scope': {
    prompt: 'What was asked (the prompt)',
    all: 'The whole conversation',
  },
}

export function choiceLabel(scannerKey: string, fieldName: string, value: string): string {
  return CHOICE_LABELS[`${scannerKey}/${fieldName}`]?.[value] ?? value
}

// ---- Modes (D4): one scan over all the chosen datasets, or one for each ----

const MODE_LABELS: Record<string, Record<string, string>> = {
  dedup: {
    across: 'Compare across the chosen datasets',
    within: 'Find duplicates within each dataset',
  },
}

// `fallback` is the scanner's own description of the mode.
export function modeLabel(scannerKey: string, mode: string, fallback: string): string {
  return MODE_LABELS[scannerKey]?.[mode] ?? fallback
}

// The note under the mode choice, which follows the choice (D10). `mode` is the mode that
// will apply: a single dataset is always compared with itself.
export function modeNote(scannerKey: string, mode: string | null, datasetCount: number): string | null {
  if (scannerKey !== 'dedup') return null
  if (mode === 'across') {
    return (
      `Rows are compared across all ${datasetCount} datasets you chose. When rows match, the one in the ` +
      'dataset that comes first by path counts as the original, and the others are flagged as duplicates.'
    )
  }
  return datasetCount > 1 ? 'Each dataset is compared only with itself.' : null
}

// Always shown for a scan that only flags (D10).
export function dropNote(scannerKey: string): string | null {
  return scannerKey === 'dedup' ? 'Nothing is dropped by this scan. You choose what to drop from the results.' : null
}

// "within this dataset", "across: A, B": which datasets a scan compared. `names` are the
// datasets' names, in the order the scan read them.
export function describeMode(mode: string | null, names: string[]): string {
  if (mode === 'across') return `across: ${names.join(', ')}`
  if (mode === 'within') return 'within this dataset'
  return names.join(', ')
}
