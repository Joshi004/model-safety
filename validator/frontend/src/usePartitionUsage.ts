import { getPartitionUsage } from './api'
import { useRefreshing } from './useRefreshing'

// How often the partition table asks SLURM again while a form is open.
const POLL_MS = 30_000

// How busy each partition is, read when a form opens and again every 30 seconds. `reload`
// reads it now.
export function usePartitionUsage() {
  return useRefreshing(getPartitionUsage, 'partition-usage', POLL_MS, () => true)
}
