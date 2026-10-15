# Dataset scan service: frontend

React 19 and TypeScript, built with Vite, plain CSS (`src/index.css`, `src/App.css`), no UI
library. It is the browser side of the dataset scan service: register datasets, set up and run
their conversion, scan them for duplicates, PII and toxicity, review what was found, and drop
chosen rows into a filtered version. The backend is `../backend/` (FastAPI); `../run.sh`
builds this app into `dist/` and the backend serves it, so there is one address and no CORS.
`../run.sh`'s header says what the service reads, writes, deletes and asks confirmation for.

## Commands

```bash
npm install
npm run build   # tsc -b, then vite build into dist/ (what run.sh serves)
npm run lint    # oxlint
npm run dev     # Vite dev server; forwards /api/* to a backend on 127.0.0.1:18234
```

There are no tests. Check a change with `npm run build` and `npm run lint`, and by using the
pages. `tsconfig.app.json` turns on `noUnusedLocals` and `noUnusedParameters`.

## Pages

The address is a hash (`src/route.ts`); anything else is replaced with `#/datasets`.

- `#/datasets`: the Datasets page (`DatasetsPage`). Team tabs, search, a table with a status chip per scanner, multi-select, the action bar (scan actions, "More"), "Register dataset".
- `#/datasets/<id>[/<tab>]`: one dataset (`DatasetPage`). Tabs: `overview`, `data`, `conversion`, `results`, `history`.
- `#/datasets/<id>/results/<scanner>`: one scanner's result for that dataset (`ResultPage`): summary, coverage, breakdowns, the flagged-rows table, and the row detail. `<scanner>` is one of `dedup`, `pii_ner`, `pii_regex`, `toxicity` (`SCANNER_KEYS` in `scans.ts`; keep it in step with `../backend/scanners.py`).
- `#/activity[/<scan id>]`: every conversion, scan and filter job, newest first, with Cancel and details (`ActivityPage`). A scan id opens that scan's details.

## Where things are

`src/` (no React unless noted):

- `api.ts`: one function per backend route, over same-origin `fetch`. `types.ts` holds the shapes it sends and receives.
- `route.ts`: hash routing and `hrefFor`. `storage.ts`: `localStorage` helpers; the only key in use is `validator.datasets.team` (the selected team tab).
- `scans.ts`: words and rules for scanners and the scan dialog (`SCAN_ACTIONS`, labels, which settings are hidden or advanced). `conversions.ts`, `format.ts`, `progress.ts`, `resources.ts`: words and helpers for jobs, sizes and times.
- `drop.ts`, `flagged.ts`: the rows chosen for a drop across result pages, and how a flagged row's reasons are named.
- `mapping.ts`, `source.ts`, `describe.ts`, `readable.ts`, `origin.ts`: the conversion form's state and the plain-language views of sampled and converted rows.
- Hooks: `usePreview.ts` (preview, inspect, open a version), `useRefreshing.ts`, `useConversionResources.ts`, `useScanResources.ts`.

`src/components/`, by area:

- Pages and tabs: `DatasetsPage`, `DatasetPage`, `OverviewTab`, `DataTab`, `ConversionTab`, `ResultsTab`, `HistoryTab`, `ResultPage`, `ActivityPage`.
- Datasets list: `DatasetsTable`, `ActionBar`, `ScannerChips`, `CoverageBadge`, `RegisterDatasetDialog`, `UnregisterDialog`.
- Conversion: `FormatPicker`, `InputFields`, `OutputPreview`, `TargetFormat`, `PreparedRows`, `SkippedRows`, `SkippedEntryDetails`, `ConvertDialog`, `ConversionJob`, `ResourcesFields`.
- Scans and results: `ScanDialog`, `ScanJob`, `ToxicityFilter`, `FlaggedRows`, `FlaggedRowPanel`, `RowText`, `AutoDropCard`.
- Dropping rows: `DropDialog` (also holds the sticky `DropBar`), `FilterJob`.
- Shared: `Dialog` (the native `<dialog>`), `Menu`, `Chip`, `CommandBox`, `DeleteList`, `RowNavigator`, `RecordView`, `ValueView`, `OriginalLookup`.

## Rules the code keeps

- Every value read from a file or from the backend renders as React text, never as markup.
- The command a dialog shows is the command the backend runs.
- Anything that deletes (unregister, convert again, a scan that converts again) lists what it deletes and asks first; GPU scans need their own confirmation; a drop shows its row counts before it starts.
- Pages that show jobs read again every few seconds while something is going, and keep the user's selection and search.
