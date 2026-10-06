import { useState } from 'react'

// The first four are shared-data paths (not anything under a home
// directory) -- these resolve the same for every teammate, since
// /home/shared/agentic_slm/data is readable by everyone on the team.
// Each one demonstrates a different, previously-
// verified case: a parquet file that needs --require despite already
// being named "messages", a nested conversation the reader can't
// auto-map, and parallel from/value arrays.
const EXAMPLE_PATHS: { label: string; path: string }[] = [
  {
    label: 'Parquet, needs --require messages',
    path: '/home/shared/agentic_slm/data/sft/curriculum/mixes/general_warmup_sft_0903/clean_parquet/medmcqa_think.parquet',
  },
  {
    label: 'JSONL, nested conversation (unsupported)',
    path: '/home/shared/agentic_slm/data/rl/raw/nemo_gym_recipe/Tool-N1-hermes',
  },
  {
    label: 'JSONL, parallel from/value arrays (unsupported)',
    path: '/home/shared/agentic_slm/data/sft/raw/toolbench',
  },
  {
    label: 'HuggingFace save_to_disk() folder',
    path: '/home/shared/agentic_slm/data/sft/_sdft_cache/281f1a0ca6b5300c626f/train',
  },
  // The next three are the tool team's own already-converted output
  // (see output/tool_team_converted/{sft_mix,mopd_mix,rl_mix} --
  // TOOL_TEAM_DATA_READINESS.md and TOXICITY_SCAN_EXPLAINED.md), not raw
  // source data -- every row here already has exactly {messages,
  // metadata}, verified directly against each folder's own summary.json.
  // Unlike the four above, this path is under this checkout's own repo
  // folder, not the shared data root, so it's only guaranteed to resolve
  // for whoever's running this from their own model-safety checkout
  // (not identical across teammates like the shared-root ones are).
  {
    label: "JSONL, tool team's processed SFT data (hermes_fc_v2)",
    path: '/home/naresh/model-safety/output/tool_team_converted/sft_mix/hermes_fc_v2',
  },
  {
    label: "JSONL, tool team's processed MOPD data",
    path: '/home/naresh/model-safety/output/tool_team_converted/mopd_mix/train',
  },
  {
    label: "JSONL, tool team's processed RL data",
    path: '/home/naresh/model-safety/output/tool_team_converted/rl_mix/train',
  },
]

export default function PathForm({
  onInspect,
  loading,
}: {
  onInspect: (path: string) => void
  loading: boolean
}) {
  const [path, setPath] = useState('')

  function pickExample(examplePath: string) {
    setPath(examplePath)
    onInspect(examplePath)
  }

  return (
    <form
      className="path-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (path.trim()) onInspect(path.trim())
      }}
    >
      <label htmlFor="path-input">Path on the cluster (file or folder)</label>
      <div className="path-form-row">
        <input
          id="path-input"
          type="text"
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="/home/shared/agentic_slm/data/..."
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" disabled={loading || !path.trim()}>
          {loading ? 'Inspecting…' : 'Inspect'}
        </button>
      </div>
      <p className="muted small">Any absolute path your account can read.</p>

      <div className="example-paths">
        <span className="muted small">Try an example:</span>
        {EXAMPLE_PATHS.map((example) => (
          <button
            key={example.path}
            type="button"
            className="example-path"
            disabled={loading}
            onClick={() => pickExample(example.path)}
            title={example.path}
          >
            {example.label}
          </button>
        ))}
      </div>
    </form>
  )
}
