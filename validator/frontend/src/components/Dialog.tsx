import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'

// A modal dialog built on the browser's own <dialog>. It opens when it is rendered and
// goes away when the parent stops rendering it. Escape, the backdrop and `onClose` all ask
// the parent to stop rendering it, except while `busy` (a request is running). `wide` is
// for a dialog with a lot in it.
export default function Dialog({
  title,
  onClose,
  busy = false,
  wide = false,
  children,
}: {
  title: string
  onClose: () => void
  busy?: boolean
  wide?: boolean
  children: ReactNode
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    // Not open yet: React runs effects twice in development, and showModal() on an
    // open dialog throws. No cleanup: removing the element closes it.
    if (dialog !== null && !dialog.open) dialog.showModal()
  }, [])

  return (
    <dialog
      ref={dialogRef}
      className={wide ? 'dialog dialog-wide' : 'dialog'}
      aria-label={title}
      onCancel={(event) => {
        if (busy) event.preventDefault()
      }}
      onClose={onClose}
      onMouseDown={(event) => {
        // A press on the backdrop lands on the <dialog> itself; its content has its own padding box.
        if (event.target === dialogRef.current && !busy) onClose()
      }}
    >
      <div className="dialog-body">
        <h2>{title}</h2>
        {children}
      </div>
    </dialog>
  )
}
