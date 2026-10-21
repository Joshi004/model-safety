import type { JobFailure } from '../types'

const ROLE_LABELS: Record<string, string> = { plan: 'planning job', scan: 'scan job' }

// "scan job 526492 on toolcall-27 (FAILED, exit code 1:0)": which SLURM job failed, where it
// ran and how SLURM says it ended. Any part SLURM didn't give is left out.
function describeFailedJob(failure: JobFailure): string {
  const kind = failure.job_role === null ? 'job' : (ROLE_LABELS[failure.job_role] ?? `${failure.job_role} job`)
  const name = failure.job_id === null ? kind : `${kind} ${failure.job_id}`
  const where = failure.node === null ? '' : ` on ${failure.node}`
  const ended = [failure.slurm_state, failure.exit_code === null ? null : `exit code ${failure.exit_code}`].filter(
    (part): part is string => part !== null,
  )
  return `${name}${where}${ended.length > 0 ? ` (${ended.join(', ')})` : ''}`
}

// What the page of a failed job shows in place of a bare reason: the reason, which job failed
// and where, the lines of its logs that look like errors, and what the few known causes
// usually mean. `after` is anything else to say under it (what to do next).
//
// Safety: every value shown here comes from a file on disk or from SLURM and renders as plain
// React text, never as markup.
export default function FailureDetails({
  reason,
  failure,
  after,
}: {
  reason: string | null
  failure: JobFailure | null
  after?: string
}) {
  if (reason === null && failure === null) return null
  return (
    <div className="banner banner-error failure-details" role="alert">
      {reason !== null && <p>{reason}</p>}
      {failure !== null && (
        <>
          <p className="small">
            <strong>Failed in:</strong> {describeFailedJob(failure)}
          </p>
          {failure.error_lines.length > 0 && <pre className="raw-json failure-lines">{failure.error_lines.join('\n')}</pre>}
          {failure.hint !== null && (
            <p className="small">
              <strong>What it may mean:</strong> {failure.hint}
            </p>
          )}
          {failure.log_path !== null && (
            <p className="small">
              These lines are from <code>{failure.log_path}</code>
            </p>
          )}
        </>
      )}
      {after !== undefined && <p className="small">{after}</p>}
    </div>
  )
}
