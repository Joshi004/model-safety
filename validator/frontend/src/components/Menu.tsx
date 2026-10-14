import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

// A button that opens a small list of actions. Put `<button role="menuitem" className="menu-item">`
// or `<a role="menuitem" className="menu-item">` in the children. Clicking an item, clicking
// outside, or pressing Escape closes it.
export default function Menu({
  label,
  ariaLabel,
  align = 'left',
  children,
}: {
  label: ReactNode
  ariaLabel: string
  align?: 'left' | 'right'
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onMouseDown(event: MouseEvent) {
      if (rootRef.current !== null && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="menu" ref={rootRef}>
      <button
        type="button"
        className="secondary"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => setOpen((current) => !current)}
      >
        {label}
      </button>
      {open && (
        <div className={`menu-list menu-${align}`} role="menu" onClick={() => setOpen(false)}>
          {children}
        </div>
      )}
    </div>
  )
}
