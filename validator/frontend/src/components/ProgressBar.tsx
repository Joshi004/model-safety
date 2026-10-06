import type { ProgressCount } from '../catalog'

// Reused in two spots: the sidebar's overall "Verified" count ('full',
// with a wider visible track) and team-tab/group-summary counts
// ('compact', a smaller inline bar) -- see catalog.ts for how `progress`
// itself is computed. The "+N unavailable" note only shows on the 'full'
// variant -- a team tab or group summary stays a short "x/y" so it still
// fits next to its name.
export default function ProgressBar({
  progress,
  label,
  variant = 'full',
}: {
  progress: ProgressCount
  label?: string
  variant?: 'full' | 'compact'
}) {
  const { done, total, unavailable } = progress
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  // total === 0 happens when every entry here is unavailable (e.g. none
  // of this group's paths are readable by this user) -- "0/0" alone would read as "nothing to
  // verify" rather than "nothing you can reach", so that case gets its
  // own wording instead.
  const countText = total > 0 ? `${done}/${total}` : unavailable > 0 ? `${unavailable} unavailable` : '0/0'

  return (
    <div className={`progress-bar progress-bar-${variant}`}>
      {label && <span className="progress-bar-label">{label}</span>}
      <div
        className="progress-bar-track"
        role="progressbar"
        aria-valuenow={done}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label={`${label ? `${label}: ` : ''}${done} of ${total} verified`}
      >
        <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="progress-bar-count muted small">
        {countText}
        {variant === 'full' && total > 0 && unavailable > 0 ? ` (+${unavailable} unavailable)` : ''}
      </span>
    </div>
  )
}
