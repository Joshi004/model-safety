// Toxicity policies: what the editor checks before saving, and how a policy's source is named.
import type { PolicySource } from './types'

// The same limit as backend/policies.py's MAX_POLICY_CHARS, which checks it again.
export const POLICY_MAX_CHARS = 6000

// Why a policy can't be saved as it is typed, or null when it can.
export function policyProblem(text: string): string | null {
  const length = text.trim().length
  if (length === 0) return "The policy can't be empty."
  if (length > POLICY_MAX_CHARS) {
    return `The policy is ${length} characters long; the limit is ${POLICY_MAX_CHARS}.`
  }
  return null
}

// "the MedPsy team's policy" or "this dataset's own policy".
export function sourceLabel(source: PolicySource, team: string): string {
  return source === 'dataset' ? "this dataset's own policy" : `the ${team} team's policy`
}
