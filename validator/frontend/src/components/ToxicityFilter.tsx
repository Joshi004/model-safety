import { choiceLabel } from '../scans'
import type { ToxicityFilter as Filter } from '../types'

function sameItems(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((item) => b.includes(item))
}

// What counts as flagged in a toxicity result: the judge's verdicts and categories. Changing
// them only changes what is shown below (D8): nothing is saved and no job is started. The
// Detoxify threshold is not among them, because it decided what the judge saw.
export default function ToxicityFilter({
  filter,
  categories,
  verdicts,
  onChange,
}: {
  filter: Filter
  // What is ticked now (which can be ahead of what the numbers below were made with).
  categories: string[]
  verdicts: string[]
  onChange: (choice: { categories: string[]; verdicts: string[] }) => void
}) {
  const unchanged = sameItems(categories, filter.saved_categories) && sameItems(verdicts, filter.saved_verdicts)

  function toggle(list: string[], all: string[], item: string, checked: boolean): string[] {
    // Kept in the order of the full list, so the same choice always reads the same.
    return all.filter((name) => (name === item ? checked : list.includes(name)))
  }

  return (
    <div className="card">
      <div className="page-header">
        <h3 className="scan-heading">What counts as flagged</h3>
        <button
          type="button"
          className="secondary push-right"
          disabled={unchanged}
          onClick={() => onChange({ categories: filter.saved_categories, verdicts: filter.saved_verdicts })}
        >
          Use the scan's choices
        </button>
      </div>
      <p className="muted small">
        The judge rated every message in every category. These choices only change what is counted below: nothing is
        saved and nothing is run again. A message the judge gave no usable verdict (a guard error), and one it gave no
        category, always count as flagged.
        {filter.threshold !== null && (
          <>
            {' '}
            The Detoxify threshold this scan used was {filter.threshold}; a different one needs a new scan.
          </>
        )}
      </p>

      <fieldset className="scan-setting">
        <legend>Verdicts</legend>
        {filter.verdicts.map((verdict) => (
          <label key={verdict} className="scan-choice">
            <input
              type="checkbox"
              checked={verdicts.includes(verdict)}
              onChange={(event) =>
                onChange({ categories, verdicts: toggle(verdicts, filter.verdicts, verdict, event.target.checked) })
              }
            />
            {choiceLabel('toxicity', 'verdicts', verdict)}
          </label>
        ))}
      </fieldset>

      <fieldset className="scan-setting">
        <legend>Categories</legend>
        <div className="scan-group-tools">
          <button
            type="button"
            className="link-button"
            onClick={() => onChange({ categories: filter.categories, verdicts })}
          >
            Select all
          </button>
          <button type="button" className="link-button" onClick={() => onChange({ categories: [], verdicts })}>
            Select none
          </button>
        </div>
        {filter.categories.map((category) => (
          <label key={category} className="scan-choice">
            <input
              type="checkbox"
              checked={categories.includes(category)}
              onChange={(event) =>
                onChange({ categories: toggle(categories, filter.categories, category, event.target.checked), verdicts })
              }
            />
            {choiceLabel('toxicity', 'categories', category)}
          </label>
        ))}
      </fieldset>
    </div>
  )
}
