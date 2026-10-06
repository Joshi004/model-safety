// Pure parsing functions for the "Readable" record view (RecordView.tsx)
// -- no React here, just functions that take a real sampled row (or a
// piece of one) and return a parsed shape to render. Every rule below
// mirrors a pattern directly confirmed in the Tools team / medpsy
// training data itself (real rows sampled from toolbench_v1,
// when2call_v2, hermes_fc_v2, toolmind__xlam, sealtools_train,
// smolagents_traces, rl_mix_v12_ifmt_0903, mopd_mixv4_0907,
// alphamed_curated_19k, safety_reliability, the medpsy stage2 MCQ files,
// and the identification files) -- none of it is guessed from the
// format's name alone.
//
// IMPORTANT: none of this is "what the PII/toxicity scanner reads" --
// that's always the backend's own real `scanned_text`
// (modelsafety/contract/textract.py's extract_record_text(), which only
// ever reads `text`, or else `messages`), shown verbatim in RecordView's
// scanner-coverage section. A row can render a full, readable
// conversation here while the real scanner saw 0 characters of it --
// e.g. a verl row whose conversation lives in `prompt`, a field
// extract_record_text() never looks at. That mismatch is exactly the
// kind of gap this view exists to surface, not paper over.

import { isChatTurnList } from './describe'

// Turning a turn's `content` into plain text is extractContentText(),
// imported from describe.ts directly wherever it's needed (RecordView.tsx)
// -- the same function used elsewhere in this app, and the same rule
// extract_content_text() itself uses.

// ---- Conversation -------------------------------------------------------

// The turns to render as a conversation: `messages` if it's a real turn
// list, else verl's `prompt` (same {role, content} shape -- confirmed
// directly against real RL parquet rows, e.g. rl_mix_v12_ifmt_0903).
export function getConversationTurns(record: Record<string, unknown>): Record<string, unknown>[] | null {
  if (isChatTurnList(record.messages)) return record.messages
  if (isChatTurnList(record.prompt)) return record.prompt
  return null
}

// ---- Reasoning -----------------------------------------------------------

export interface ReasoningSplit {
  reasoning: string | null
  // What's left of the turn's content text once the reasoning (if any)
  // is removed -- parse tool calls etc. from this, not the original text,
  // so the reasoning doesn't get shown twice.
  visibleText: string
}

const THINK_BLOCK_RE = /<think>([\s\S]*?)<\/think>/
const LONE_THINK_CLOSE = '</think>'

// A turn's reasoning, in the three shapes actually seen in the training
// data, checked in this order (first match wins):
//  1. A paired <think>...</think> block (most SFT data, e.g. toolbench_v1,
//     smolagents_traces).
//  2. Everything before a lone closing </think> with no opening tag
//     (medpsy stage-2 style).
//  3. A separate `reasoning_content` field on the turn itself, sibling to
//     `role`/`content` (confirmed in the identification data).
export function splitReasoning(contentText: string, turn: Record<string, unknown>): ReasoningSplit {
  const pairedMatch = contentText.match(THINK_BLOCK_RE)
  if (pairedMatch) {
    const start = pairedMatch.index ?? 0
    const before = contentText.slice(0, start)
    const after = contentText.slice(start + pairedMatch[0].length)
    return { reasoning: pairedMatch[1].trim(), visibleText: (before + after).trim() }
  }

  const loneCloseIndex = contentText.indexOf(LONE_THINK_CLOSE)
  if (loneCloseIndex !== -1) {
    const before = contentText.slice(0, loneCloseIndex)
    const after = contentText.slice(loneCloseIndex + LONE_THINK_CLOSE.length)
    return { reasoning: before.trim(), visibleText: after.trim() }
  }

  const reasoningContent = turn.reasoning_content
  if (typeof reasoningContent === 'string' && reasoningContent.trim()) {
    return { reasoning: reasoningContent, visibleText: contentText }
  }

  return { reasoning: null, visibleText: contentText }
}

// ---- Tool calls (Qwen-XML) ------------------------------------------------

export interface ToolCallArgument {
  key: string
  value: string
}

export interface ParsedToolCall {
  // The exact "<tool_call>...</tool_call>" text, always kept -- so
  // something that doesn't match the <function=...> shape inside can
  // still be shown, raw, instead of silently dropped.
  raw: string
  functionName: string | null
  arguments: ToolCallArgument[] | null
}

const TOOL_CALL_BLOCK_RE = /<tool_call>([\s\S]*?)<\/tool_call>/g
const FUNCTION_BLOCK_RE = /<function=([^>]*)>([\s\S]*?)<\/function>/
const PARAMETER_RE = /<parameter=([^>]*)>([\s\S]*?)<\/parameter>/g

function parseOneToolCall(raw: string, innerText: string): ParsedToolCall {
  const functionMatch = innerText.match(FUNCTION_BLOCK_RE)
  if (!functionMatch) {
    return { raw, functionName: null, arguments: null }
  }
  const args: ToolCallArgument[] = []
  for (const parameterMatch of functionMatch[2].matchAll(PARAMETER_RE)) {
    args.push({ key: parameterMatch[1].trim(), value: parameterMatch[2].trim() })
  }
  return { raw, functionName: functionMatch[1].trim(), arguments: args }
}

export interface ToolCallSplit {
  toolCalls: ParsedToolCall[]
  // `text` with every <tool_call>...</tool_call> block removed, so a
  // turn's plain prose and its structured tool calls don't both end up
  // shown twice.
  remainingText: string
}

// Pulls every <tool_call>...</tool_call> block out of a turn's visible
// text. There can be more than one in a single turn -- parallel calls,
// confirmed directly in real hermes_fc_v2 rows.
export function splitToolCalls(text: string): ToolCallSplit {
  const toolCalls: ParsedToolCall[] = []
  const remainingText = text
    .replace(TOOL_CALL_BLOCK_RE, (raw: string, innerText: string) => {
      toolCalls.push(parseOneToolCall(raw, innerText))
      return ''
    })
    .trim()
  return { toolCalls, remainingText }
}

// ---- Tool definitions ------------------------------------------------------

export interface ToolDefinition {
  name: string
  description: string
  parameters: unknown
  // The exact JSON line this came from -- kept so a shape this doesn't
  // recognize can still be shown instead of silently dropped.
  raw: string
}

const TOOLS_BLOCK_RE = /<tools>([\s\S]*?)<\/tools>/

// The tool definitions inside a system prompt's <tools>...</tools> --
// one JSON object per line (JSON-lines, not a JSON array -- confirmed
// directly against real toolbench_v1 / when2call_v2 / hermes_fc_v2 /
// toolmind__xlam system prompts), each shaped like
// {"type": "function", "function": {"name", "description", "parameters"}}.
export function parseToolDefinitions(text: string): ToolDefinition[] {
  const match = text.match(TOOLS_BLOCK_RE)
  if (!match) return []

  const lines = match[1]
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)

  return lines.map((line) => {
    let parsed: unknown
    try {
      parsed = JSON.parse(line)
    } catch {
      return { name: '(not valid JSON)', description: '', parameters: null, raw: line }
    }
    const container =
      parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed) && 'function' in parsed
        ? (parsed as Record<string, unknown>).function
        : parsed
    if (container === null || typeof container !== 'object' || Array.isArray(container)) {
      return { name: '(unrecognized shape)', description: '', parameters: null, raw: line }
    }
    const fields = container as Record<string, unknown>
    return {
      name: typeof fields.name === 'string' ? fields.name : '(unnamed)',
      description: typeof fields.description === 'string' ? fields.description : '',
      parameters: fields.parameters ?? null,
      raw: line,
    }
  })
}

// ---- Tool results, and anything else that might be JSON-shaped text -----

// Tries to parse a string as JSON, returning null (never throwing) when
// it isn't one -- used for a `role: tool` turn's content (pretty-printed
// when it parses -- real tool results here are things like
// '\n\n{"session_token": "abc123xyz"}') and for `reward_model.
// ground_truth`, which is always stored as a plain string even when it's
// semantically a JSON list (confirmed directly: ground_truth values like
// '[]' and '[{"name": "get_game_info", "arguments": {...}}]').
export function tryParseJson(text: string): unknown {
  const trimmed = text.trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed)
  } catch {
    return null
  }
}

// ---- Reference answer ------------------------------------------------------

// `reward_model.ground_truth` -- always a plain string on disk (verl
// rows), even when its content is a JSON tool-call list. Try
// tryParseJson() on the result to tell which it is.
export function getGroundTruth(record: Record<string, unknown>): string | null {
  const rewardModel = record.reward_model
  if (rewardModel === null || typeof rewardModel !== 'object' || Array.isArray(rewardModel)) return null
  const groundTruth = (rewardModel as Record<string, unknown>).ground_truth
  return typeof groundTruth === 'string' ? groundTruth : null
}

// The plain-JSONL sibling of ground_truth -- medpsy's stage-2 MCQ rows
// (e.g. medmcqa_full.jsonl) use a top-level `target` instead, a bare
// letter ("B") when `metadata.options` is present, or free text
// otherwise (e.g. "A. yes" for a yes/no row with no options list).
export function getTarget(record: Record<string, unknown>): string | null {
  return typeof record.target === 'string' ? record.target : null
}

export interface ReferenceOption {
  // This option's own letter/key, when one can be determined ('A', 'B',
  // ...) -- null if the option text has no recognizable prefix.
  key: string | null
  // Exactly as stored: e.g. "A. Dubin Johnson syndrome" for the
  // list-of-strings shape, or just the choice text for the dict shape.
  text: string
  isCorrect: boolean
}

// `metadata.options`, in the two shapes confirmed in the medpsy stage-2
// MCQ files: a list of already-lettered strings ("A. ...", "B. ...",
// ...; medmcqa_full.jsonl, medpsy1_afrimed_mcq.jsonl), or a dict keyed by
// letter ({"A": "...", "B": "...", ...}; medpsy1_afrimed_saq.jsonl).
// `target` (a bare letter, e.g. "B") says which one is correct. Returns
// null when there's no recognizable `options` field at all (e.g.
// baichuan_pubmedqa.jsonl's metadata has no `options` key).
export function getReferenceOptions(
  record: Record<string, unknown>,
  target: string | null,
): ReferenceOption[] | null {
  const metadata = record.metadata
  if (metadata === null || typeof metadata !== 'object' || Array.isArray(metadata)) return null
  const options = (metadata as Record<string, unknown>).options
  const normalizedTarget = target?.trim().toUpperCase() ?? null

  if (Array.isArray(options)) {
    const stringOptions = options.filter((item): item is string => typeof item === 'string')
    if (stringOptions.length === 0) return null
    return stringOptions.map((text) => {
      const letterMatch = text.match(/^([A-Za-z])[.)]/)
      const key = letterMatch ? letterMatch[1].toUpperCase() : null
      return { key, text, isCorrect: key !== null && key === normalizedTarget }
    })
  }

  if (options !== null && typeof options === 'object') {
    const entries = Object.entries(options as Record<string, unknown>).filter(
      (entry): entry is [string, string] => typeof entry[1] === 'string',
    )
    if (entries.length === 0) return null
    return entries.map(([key, text]) => ({
      key,
      text,
      isCorrect: key.trim().toUpperCase() === normalizedTarget,
    }))
  }

  return null
}
