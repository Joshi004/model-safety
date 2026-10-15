import { useCallback, useEffect, useMemo, useState } from 'react'
import { errorMessage, getScanners } from './api'
import { EMPTY_RESOURCES, initialResources, toRequestResources } from './resources'
import type { ResourceInputs } from './resources'
import type { ResourceRequest, ScannerInfo, ScannersResponse } from './types'

// What the scan dialog needs from the backend for one scanner: the scanners and the
// partitions SLURM lists (`response`), the scanner itself, and the text of its SLURM
// resource boxes. The boxes start at the scanner's own defaults. Nothing is sent
// (`requestResources` is null) until the user edits a box, so the backend uses those same
// defaults.
//
// The scanner can change while the dialog is open (a choice of method): what was typed is kept
// for each scanner on its own, so going back to a method finds its boxes as they were left.
// A scanner whose job scripts fix the CPUs, memory and time only sends its partition.
export function useScanResources(scannerKey: string) {
  const [response, setResponse] = useState<ScannersResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  // What the user typed, by scanner. A scanner that isn't here shows its defaults.
  const [edits, setEdits] = useState<Record<string, ResourceInputs>>({})
  // Bumped to ask again (SLURM's partitions couldn't be read, or the first request failed).
  const [loads, setLoads] = useState(0)

  useEffect(() => {
    let cancelled = false
    getScanners()
      .then((next) => {
        if (cancelled) return
        setResponse(next)
        setError(null)
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(errorMessage(caught))
      })
    return () => {
      cancelled = true
    }
  }, [loads])

  const reload = useCallback(() => setLoads((count) => count + 1), [])
  const change = useCallback(
    (next: ResourceInputs) => setEdits((current) => ({ ...current, [scannerKey]: next })),
    [scannerKey],
  )

  const scanner: ScannerInfo | null = response?.scanners[scannerKey] ?? null
  const defaults = useMemo(
    () =>
      response !== null && scanner !== null
        ? initialResources({ partitions: response.partitions, default_resources: scanner.default_resources })
        : EMPTY_RESOURCES,
    [response, scanner],
  )
  const edited = scannerKey in edits
  const inputs = edits[scannerKey] ?? defaults

  let requestResources: ResourceRequest | null = null
  if (edited) requestResources = scanner?.resources_fixed ? { partition: inputs.partition } : toRequestResources(inputs)
  return { response, scanner, error, reload, inputs, change, edited, requestResources }
}
