import { useCallback, useEffect, useState } from 'react'
import { errorMessage, getScanners } from './api'
import { EMPTY_RESOURCES, hasTypedAnything, initialResources, toRequestResources } from './resources'
import type { ResourceInputs } from './resources'
import type { JobResources, ScannerInfo, ScannersResponse } from './types'

// What the scan dialog needs from the backend for one scanner: the scanners and the
// partitions SLURM lists (`response`), the scanner itself, and the text of its SLURM
// resource boxes. The boxes start at the scanner's own defaults. Nothing is sent
// (`requestResources` is null) until the user edits a box, so the backend uses those same
// defaults.
export function useScanResources(scannerKey: string) {
  const [response, setResponse] = useState<ScannersResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inputs, setInputs] = useState<ResourceInputs>(EMPTY_RESOURCES)
  const [edited, setEdited] = useState(false)
  // Bumped to ask again (SLURM's partitions couldn't be read, or the first request failed).
  const [loads, setLoads] = useState(0)

  useEffect(() => {
    let cancelled = false
    getScanners()
      .then((next) => {
        if (cancelled) return
        setResponse(next)
        setError(null)
        const scanner = next.scanners[scannerKey]
        // Anything already typed stays; otherwise start from the scanner's defaults.
        setInputs((current) =>
          hasTypedAnything(current) || scanner === undefined
            ? current
            : initialResources({ partitions: next.partitions, default_resources: scanner.default_resources }),
        )
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(errorMessage(caught))
      })
    return () => {
      cancelled = true
    }
  }, [loads, scannerKey])

  const reload = useCallback(() => setLoads((count) => count + 1), [])
  const change = useCallback((next: ResourceInputs) => {
    setInputs(next)
    setEdited(true)
  }, [])

  const scanner: ScannerInfo | null = response?.scanners[scannerKey] ?? null
  const requestResources: JobResources | null = edited ? toRequestResources(inputs) : null
  return { response, scanner, error, reload, inputs, change, edited, requestResources }
}
