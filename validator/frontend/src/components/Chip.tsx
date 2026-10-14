export type ChipTone = 'ok' | 'warn' | 'error' | 'neutral'

// A small coloured label. `label` and `title` are shown as plain text.
export default function Chip({
  label,
  tone = 'neutral',
  title,
}: {
  label: string
  tone?: ChipTone
  title?: string
}) {
  return (
    <span className={`chip chip-${tone}`} title={title}>
      {label}
    </span>
  )
}
