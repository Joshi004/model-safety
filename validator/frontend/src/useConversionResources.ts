import { useCallback, useEffect, useState } from 'react'
import { errorMessage, getConversionSettings } from './api'
import { EMPTY_RESOURCES, hasTypedAnything, initialResources, toRequestResources } from './resources'
import type { ResourceInputs } from './resources'
import type { ConversionSettings, JobResources } from './types'

// What a form that starts conversions needs for its SLURM resources: the partitions
// SLURM lists, the boxes' text (starting at the defaults), and what to send. Nothing is
// sent (`requestResources` is null) until the user edits a box, so the backend uses its
// own defaults.
//
// `start` fills the boxes from the beginning (a retry of a conversion keeps the resources it ran
// with). They then count as typed, so they are sent.
export function useConversionResources(start?: ResourceInputs) {
  const [settings, setSettings] = useState<ConversionSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inputs, setInputs] = useState<ResourceInputs>(start ?? EMPTY_RESOURCES)
  const [edited, setEdited] = useState(start !== undefined)
  // Bumped to ask SLURM for the partitions again.
  const [loads, setLoads] = useState(0)

  useEffect(() => {
    let cancelled = false
    getConversionSettings()
      .then((response) => {
        if (cancelled) return
        setSettings(response)
        setError(null)
        // Anything already typed stays; otherwise start from the defaults.
        setInputs((current) => (hasTypedAnything(current) ? current : initialResources(response)))
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(errorMessage(caught))
      })
    return () => {
      cancelled = true
    }
  }, [loads])

  const reload = useCallback(() => setLoads((count) => count + 1), [])
  const change = useCallback((next: ResourceInputs) => {
    setInputs(next)
    setEdited(true)
  }, [])

  const requestResources: JobResources | null = edited ? toRequestResources(inputs) : null
  return { settings, error, reload, inputs, change, edited, requestResources }
}
