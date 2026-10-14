// The shapes the backend sends and receives (validator/backend/app.py and registry.py).

export interface HealthResponse {
  ok: boolean
}

// ---- Teams and datasets (GET /api/teams, /api/datasets, POST /api/datasets/{id}/unregister) ----

export interface Team {
  name: string
  description: string
}

export interface TeamsResponse {
  teams: Team[]
}

// The conversion settings saved on a dataset, or null until they are set (Phase 3).
export interface DatasetConversion {
  input_type: string
  file: string | null
  split: string
  hf_config: string
  map: Record<string, string>
  require: string[]
  exclude_metadata: string[]
}

// The current Prepared version (the converted rows), or null until the dataset is converted.
// Phase 3 sets it, so this shape is a placeholder until then.
export interface DatasetPrepared {
  folder: string
  rows: number | null
  finished_at: string | null
}

export interface Dataset {
  id: string
  team: string
  name: string
  // The absolute path of the original data. Never changed after registering.
  path: string
  description: string
  notes: string
  created_at: string
  conversion: DatasetConversion | null
  prepared: DatasetPrepared | null
  // Set for a filtered dataset (Phase 10): the dataset it was made from.
  parent: string | null
  // Whether the original path exists and can be read right now.
  available: boolean
  unavailable_reason: string | null
}

export interface DatasetsResponse {
  service_root: string
  datasets: Dataset[]
}

export interface RegisterDatasetRequest {
  team: string
  name: string
  path: string
  description: string
}

export interface UnregisterResponse {
  id: string
  name: string
  removed_folder: string | null
}

// ---- Looking at a path (POST /api/inspect) ----

export interface InspectGroup {
  format: string
  input_type: string | null
  sample_target: 'folder' | 'file' | null
  files: string[]
  file_count: number
  total_bytes: number
  truncated: boolean
  note: string
}

export interface InspectResult {
  kind: 'file' | 'directory' | 'huggingface_dataset'
  path: string
  groups: InspectGroup[]
  walk_truncated?: boolean
}
