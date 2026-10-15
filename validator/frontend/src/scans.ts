// Words for scans and scanners, and what the scan dialog does with a scanner's settings.
// No React here.
//
// The Datasets page doesn't ask GET /api/scanners (that runs `sinfo`), so the few words it
// needs for a scanner are here. A scanner this file doesn't know is shown by its key.
import type { ChipTone } from './components/Chip'
import { formatCount } from './format'
import type { ChoiceField, ScanSettings, ScanState, ScanSummary, ScannerField, ScannerInfo } from './types'

const SCANNER_LABELS: Record<string, string> = {
  dedup: 'Duplicates',
  pii_ner: 'PII (NER)',
  pii_regex: 'PII (rules)',
}

// What a finished scan's headline count counts, by scanner.
const SCANNER_COUNT_LABELS: Record<string, string> = {
  dedup: 'duplicate rows',
  pii_ner: 'PII findings',
  pii_regex: 'PII findings',
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

// A choice whose only two values are "0" and "1" is a checkbox.
export function isToggle(field: ChoiceField): boolean {
  return field.choices.length === 2 && field.choices.includes('0') && field.choices.includes('1')
}

// The default of every setting the dialog offers.
export function defaultSettings(scanner: ScannerInfo): ScanSettings {
  return Object.fromEntries(visibleFields(scanner).map((field) => [field.name, field.default]))
}

// How a choice is named, by `<scanner>/<setting>`. A choice not listed is shown as it is.
const CHOICE_LABELS: Record<string, Record<string, string>> = {
  'dedup/text_scope': {
    prompt: 'What was asked (the prompt)',
    all: 'The whole conversation',
  },
  'pii_ner/size': {
    full: 'Full node (every GPU of one node)',
    one_gpu: 'One GPU',
  },
}

export function choiceLabel(scannerKey: string, fieldName: string, value: string): string {
  const named = CHOICE_LABELS[`${scannerKey}/${fieldName}`]?.[value]
  if (named !== undefined) return named
  if (scannerKey === 'pii_ner' && fieldName === 'labels') return nerLabelName(value)
  return value
}

// ---- PII: the NER model's categories, and the two methods behind "Scan for PII" ----

// "date_of_birth" as "Date of birth".
function sentenceCase(name: string): string {
  const words = name.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// The categories of the NER model. Most read well as their name with spaces; these don't.
const NER_LABEL_NAMES: Record<string, string> = {
  api_key: 'API key',
  credit_debit_card: 'Credit or debit card',
  customer_id: 'Customer ID',
  cvv: 'Card security code (CVV)',
  date_time: 'Date and time',
  employee_id: 'Employee ID',
  http_cookie: 'HTTP cookie',
  ipv4: 'IPv4 address',
  ipv6: 'IPv6 address',
  mac_address: 'MAC address',
  national_id: 'National ID',
  pin: 'PIN',
  race_ethnicity: 'Race or ethnicity',
  ssn: 'Social security number',
  swift_bic: 'SWIFT/BIC code',
  tax_id: 'Tax ID',
  unique_id: 'Unique ID',
  url: 'URL',
}

export function nerLabelName(label: string): string {
  return NER_LABEL_NAMES[label] ?? sentenceCase(label)
}

// The categories of the rule-based scan.
const RULE_CATEGORY_NAMES: Record<string, string> = {
  emails: 'Email addresses',
  phones: 'Phone numbers',
  phones_with_exts: 'Phone numbers with an extension',
  ukphones: 'UK phone numbers',
  street_addresses: 'Street addresses',
  po_boxes: 'PO boxes',
  credit_cards: 'Credit card numbers',
  ips: 'IPv4 addresses',
  ipv6s: 'IPv6 addresses',
  btc_addresses: 'Bitcoin addresses',
}

export function ruleCategoryName(category: string): string {
  return RULE_CATEGORY_NAMES[category] ?? sentenceCase(category)
}

// The groups the NER categories are shown in, by the backend's key (the policy's tiers).
const GROUP_TITLES: Record<string, string> = {
  direct_identifier: 'Direct identifiers',
  person_linked: 'Names',
  contextual: 'Contextual',
}

const GROUP_HELP: Record<string, string> = {
  direct_identifier: 'Identify a person or account on their own: credentials, IDs, contact details, financial and health numbers.',
  person_linked: 'First and last names.',
  contextual: 'Places, dates, demographics, jobs and languages. They are common in text with no personal data in it, so they are off unless you turn them on.',
}

export function groupTitle(key: string): string {
  return GROUP_TITLES[key] ?? sentenceCase(key)
}

export function groupHelp(key: string): string | null {
  return GROUP_HELP[key] ?? null
}

// An action of the action bar: one scan dialog, over one scanner or a choice between several
// ("Scan for PII" is NER or rules). The first scanner is the one selected to begin with.
export interface ScanActionInfo {
  title: string
  scannerKeys: string[]
}

export const SCAN_ACTIONS: Record<string, ScanActionInfo> = {
  dedup: { title: 'Scan for duplicates', scannerKeys: ['dedup'] },
  pii: { title: 'Scan for PII', scannerKeys: ['pii_ner', 'pii_regex'] },
}

// How the methods of an action with a choice are named and told apart, by scanner.
export const METHOD_INFO: Record<string, { title: string; blurb: string }> = {
  pii_ner: {
    title: 'Model (NER)',
    blurb: 'A model reads every message and marks personal information from its context, names included. It needs GPUs.',
  },
  pii_regex: {
    title: 'Rules',
    blurb: 'Fixed patterns for emails, phone numbers, cards and the like. It runs on CPUs and is much faster.',
  },
}

// What a scanner can't do, shown with the list of what it finds.
export function scannerNote(scannerKey: string): string | null {
  return scannerKey === 'pii_regex'
    ? "Rules can't find names, or ID numbers that have no fixed pattern. Use the model for those."
    : null
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
