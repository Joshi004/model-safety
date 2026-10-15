import { formatCount } from '../format'
import type { ConversionWillDelete } from '../types'

export interface DeleteItem {
  id: string
  name: string
  // What "convert again" deletes first; null when there is nothing to delete.
  will_delete: ConversionWillDelete | null
  // Folders of failed or cancelled attempts, deleted when the conversion starts.
  leftovers: string[]
}

// What starting conversions deletes, dataset by dataset: shown before the user confirms.
// Everything is plain text.
export default function DeleteList({ items }: { items: DeleteItem[] }) {
  return (
    <ul>
      {items.map((item) => (
        <li key={item.id}>
          <strong>{item.name}</strong>
          <ul>
            {item.will_delete !== null && !item.will_delete.kept && (
              <li>
                its current converted data (<code>{item.will_delete.folder}</code>
                {item.will_delete.rows !== null && `, ${formatCount(item.will_delete.rows)} rows`}) and the results of
                every scan that read it
              </li>
            )}
            {item.will_delete !== null && item.will_delete.kept && (
              <li>
                nothing of its current converted data: a filtered dataset was made from it, so it stays on disk (the
                dataset shows as not converted until the new conversion finishes)
              </li>
            )}
            {item.leftovers.map((folder) => (
              <li key={folder}>
                the folder of a failed or cancelled attempt: <code>{folder}</code>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  )
}
