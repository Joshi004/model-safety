// Pure progress-counting helpers over the /api/sources catalog (see
// ../types.ts's TeamCatalog / SourceGroupInfo / SourceEntry) -- no React
// here, same style as readable.ts. "Verified" is the UI's one browser-only
// mark per dataset path (see storage.ts and SourcePicker.tsx); these
// functions only ever read it, never decide what it means.
//
// An entry this backend can't actually open (`available: false`) is
// tracked separately as `unavailable` and left out of `total` -- there's
// nothing to verify about a dataset that can't be opened, so counting it
// against the total would permanently cap a team or group below 100% for
// teammates who simply can't reach every path.

import type { SourceEntry, SourceGroupInfo, TeamCatalog } from './types'

export interface ProgressCount {
  done: number
  total: number
  unavailable: number
}

const EMPTY_COUNT: ProgressCount = { done: 0, total: 0, unavailable: 0 }

function addCounts(a: ProgressCount, b: ProgressCount): ProgressCount {
  return {
    done: a.done + b.done,
    total: a.total + b.total,
    unavailable: a.unavailable + b.unavailable,
  }
}

export function countEntries(entries: SourceEntry[], verified: Record<string, true>): ProgressCount {
  return entries.reduce<ProgressCount>((count, entry) => {
    if (!entry.available) {
      return { ...count, unavailable: count.unavailable + 1 }
    }
    return {
      done: count.done + (verified[entry.path] ? 1 : 0),
      total: count.total + 1,
      unavailable: count.unavailable,
    }
  }, EMPTY_COUNT)
}

export function countGroup(group: SourceGroupInfo, verified: Record<string, true>): ProgressCount {
  return countEntries(group.entries, verified)
}

export function countTeam(team: TeamCatalog, verified: Record<string, true>): ProgressCount {
  return team.groups.reduce((count, group) => addCounts(count, countGroup(group, verified)), EMPTY_COUNT)
}

export function countCatalog(teams: TeamCatalog[], verified: Record<string, true>): ProgressCount {
  return teams.reduce((count, team) => addCounts(count, countTeam(team, verified)), EMPTY_COUNT)
}

export interface FoundEntry {
  team: TeamCatalog
  group: SourceGroupInfo
  entry: SourceEntry
}

/** The catalog entry at exactly this path, if any -- used so the dataset
 * header can show a picked entry's description/screening note/Verified
 * checkbox, and render nothing extra for a hand-typed path that doesn't
 * match the catalog at all. */
export function findEntry(teams: TeamCatalog[], path: string): FoundEntry | null {
  for (const team of teams) {
    for (const group of team.groups) {
      for (const entry of group.entries) {
        if (entry.path === path) return { team, group, entry }
      }
    }
  }
  return null
}
