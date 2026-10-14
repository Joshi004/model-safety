"""The registry's starting datasets (D12): the old training-data catalog, as registry records.

It runs once, when `datasets.json` doesn't exist yet (see `registry._seed`). After that the
registry is the only list of datasets, and editing `CATALOG` below changes nothing for a
service root that already has a `datasets.json`.

The catalog is a flat Python list on purpose, not something found by walking the
filesystem: which folders actually feed training is a judgment call (a sibling "raw" or
"eval" folder next to a training file is not itself training data), and this list is where
that judgment is written down. The old "Tools team" becomes BotPsy and "medpsy" becomes
MedPsy. Groups that set `children_of` are expanded into one dataset per dataset-shaped
subfolder, as the old picker did (the Tools team's 61-component SFT mix, and medpsy's
no-think dual-mode copies).

Row counts quoted in the descriptions come from the sources named next to them
(`train_shards.json`, `*.summary.json` and `build_summary.json` for the Tools team,
`DATASET_REPORT.md` and `identification/lineage.json` for medpsy's packed mix). They were
not measured again here.

A medpsy entry's screening note says whether that file or folder is under
`final_cleaned_data/filtered` (medpsy's own PII and toxicity filter output). "Not screened"
does not mean "known to be unsafe": it means medpsy's own filter never ran on it, which is
the gap this service exists to let you check. The note becomes the dataset's `notes`.

Seeded paths aren't required to exist or be readable: the seed runs only once and must
not lose entries, so one the current user can't read is kept and listed as unavailable.
A folder that can't be expanded, or an entry that can't be seeded, is recorded in
`seed_skipped` (kept in `datasets.json`) with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import detect
import registry

# -------------------------------------------------------------------------
# Roots (an entry under one the current user can't read shows up disabled)
# -------------------------------------------------------------------------

TOOLS_DATA = "/home/shared/agentic_slm/data"
MEDPSY_MIX = "/home/alex/health/data/fsmix_dm_filt_id_ft/with_sys_prompt"
MEDPSY_SYNTH = "/home/alex/health/qvac-research-medpsy/synth-data-gen/synth_data_gen"
MEDPSY_BRIDGE = (
    "/home/alex/health/qvac-research-medpsy/sft/sft/data/preprocessed/"
    "FSMIX_DMFILT_DECOY20_ID@Qwen3.5/bridge"
)
MEDPSY_RL_CURRICULUM = (
    "/home/alex/health/qvac-research-medpsy/rl/rl/methods/med-curriculum-dapo/data/alphamed_curated_19k"
)
MEDPSY_RL_MGDA = (
    "/home/alex/health/qvac-research-medpsy/rl/rl/methods/multitask-mgda/data/safety_reliability"
)

# -------------------------------------------------------------------------
# Reusable screening-note text
# -------------------------------------------------------------------------

TOOLS_SCREENING = (
    "Team-side: benchmark decontamination only (data_processing/decontamination/). "
    "No PII/toxicity filter is documented as having run on this data."
)
MEDPSY_FILTERED = (
    "From final_cleaned_data/filtered -- passed medpsy's own PII filter (regex, then "
    "Gemma-4-31B confirms real private-person PII) and toxicity filter (Detoxify at a "
    "0.5 threshold, then Qwen3Guard-Gen-8B on the hits); either one drops the whole row."
)
MEDPSY_NOT_FILTERED = (
    "Not from medpsy's cleaned tree -- this folder's own lineage.json lists it as a "
    "separate input, not final_cleaned_data/filtered, so medpsy's PII/toxicity filter "
    "never ran on it."
)
MEDPSY_NOTHINK_NOTE = (
    "No-think dual-mode copy; same source and medpsy screening status as its "
    "think-mode counterpart (see the matching entry above)."
)


@dataclass
class SourceEntry:
    label: str
    path: str
    description: str = ""
    screening: str = ""


@dataclass
class SourceGroup:
    name: str
    description: str = ""
    screening: str = ""  # shown once for the group when every entry shares it
    entries: list[SourceEntry] = field(default_factory=list)
    # Expand this folder's dataset-shaped immediate children instead of
    # using `entries` -- see `_expand_children`. Every expanded child gets
    # `screening` above (or MEDPSY_NOTHINK_NOTE, set per-group below).
    children_of: str | None = None
    children_screening: str = ""


@dataclass
class TeamCatalog:
    team: str
    groups: list[SourceGroup]


CATALOG: list[TeamCatalog] = [
    TeamCatalog(
        team="Tools team",
        groups=[
            SourceGroup(
                name="Mid-training text",
                description=(
                    "Plain {\"text\": ...} rows later tokenized for mid-training -- "
                    "first shard of 8 per category; the rest are siblings in the same folder."
                ),
                screening=TOOLS_SCREENING,
                entries=[
                    SourceEntry(
                        label="code",
                        path=f"{TOOLS_DATA}/mid/_megatron/full/jsonl/code_000.jsonl",
                        description="Code-repo text (owner/repo/file-path headers).",
                    ),
                    SourceEntry(
                        label="web",
                        path=f"{TOOLS_DATA}/mid/_megatron/full/jsonl/web_000.jsonl",
                        description="Crawled web text.",
                    ),
                    SourceEntry(
                        label="pdf",
                        path=f"{TOOLS_DATA}/mid/_megatron/full/jsonl/pdf_000.jsonl",
                        description="Extracted PDF text.",
                    ),
                    SourceEntry(
                        label="native-agent-traj",
                        path=f"{TOOLS_DATA}/mid/_megatron/full/jsonl/native-agent-traj_000.jsonl",
                        description="Agent rollout traces rendered as plain chat-template text (tool definitions and calls included as text, not structured fields).",
                    ),
                ],
            ),
            SourceGroup(
                name="SFT mix s1_2_0824",
                description=(
                    "61 HuggingFace save_to_disk() components under train_shards_keep/. "
                    "About 595K rows exist across them; the mix recipe trains on ~303K "
                    "(see mix_recipe.json / train_shards.json in this folder's parent)."
                ),
                screening=TOOLS_SCREENING,
                children_of=f"{TOOLS_DATA}/sft/processed/qwen3_5/s1_2_0824/train_shards_keep",
                children_screening=TOOLS_SCREENING,
            ),
            SourceGroup(
                name="RL mixes",
                description="verl-shaped rows (prompt / reward_model / extra_info); the same two mixes scripts/tool_team_data.sh already screens.",
                screening=TOOLS_SCREENING,
                entries=[
                    SourceEntry(
                        label="mopd_mixv4_0907 (train)",
                        path=f"{TOOLS_DATA}/rl/processed/qwen35-xml/decontaminated/mopd_mixv4_0907/train.parquet",
                        description="MOPD RL mix, 310,537 rows -- multi-turn tool-use conversations.",
                    ),
                    SourceEntry(
                        label="rl_mix_v12_ifmt_0903 (train)",
                        path=f"{TOOLS_DATA}/rl/processed/qwen35-xml/decontaminated/rl_mix_v12_ifmt_0903/train.parquet",
                        description="RL mix v12 (instruction-following + tool-use), 236,641 rows.",
                    ),
                ],
            ),
        ],
    ),
    TeamCatalog(
        team="medpsy",
        groups=[
            SourceGroup(
                name="SFT, think mode -- stage 1 / open-ended",
                description="The largest sources in the FSMIX_DMFILT_DECOY20_ID training mix; every one of these is under medpsy's own cleaned tree.",
                screening=MEDPSY_FILTERED,
                entries=[
                    SourceEntry(
                        label="stage_1_genesis",
                        path=f"{MEDPSY_MIX}/stage1/stage_1_genesis.jsonl",
                        description="Closed-ended MCQ + long reasoning, 10,221,306 rows.",
                    ),
                    SourceEntry(
                        label="stage_1_open_reasoning",
                        path=f"{MEDPSY_MIX}/stage1/stage_1_open_reasoning.jsonl",
                        description="Closed-ended open-reasoning questions, 2,163,848 rows.",
                    ),
                    SourceEntry(
                        label="oe_health",
                        path=f"{MEDPSY_MIX}/oe_health/open_ended_pipeline.jsonl",
                        description="Open-ended health Q&A, 3,860,889 rows.",
                    ),
                    SourceEntry(
                        label="oe_medical",
                        path=f"{MEDPSY_MIX}/oe_medical/open_ended_pipeline.jsonl",
                        description="Open-ended medical Q&A, 4,034,510 rows.",
                    ),
                ],
            ),
            SourceGroup(
                name="SFT, think mode -- stage 2 (benchmark-style MCQ)",
                description="Smaller benchmark-style MCQ/SAQ sets, all under medpsy's cleaned tree.",
                screening=MEDPSY_FILTERED,
                entries=[
                    SourceEntry(
                        label="baichuan_pubmedqa",
                        path=f"{MEDPSY_MIX}/stage2/baichuan_pubmedqa.jsonl",
                        description="PubMedQA, 211,269 rows.",
                    ),
                    SourceEntry(
                        label="medmcqa_full",
                        path=f"{MEDPSY_MIX}/stage2/medmcqa_full.jsonl",
                        description="MedMCQA, 181,798 rows.",
                    ),
                    SourceEntry(
                        label="medpsy1_afrimed_mcq",
                        path=f"{MEDPSY_MIX}/stage2/medpsy1_afrimed_mcq.jsonl",
                        description="AfriMed multiple-choice, 124 rows.",
                    ),
                    SourceEntry(
                        label="medpsy1_afrimed_saq",
                        path=f"{MEDPSY_MIX}/stage2/medpsy1_afrimed_saq.jsonl",
                        description="AfriMed short-answer, 838 rows.",
                    ),
                    SourceEntry(
                        label="medpsy1_pubmedqa",
                        path=f"{MEDPSY_MIX}/stage2/medpsy1_pubmedqa.jsonl",
                        description="PubMedQA (medpsy-1 carryover), 391 rows.",
                    ),
                ],
            ),
            SourceGroup(
                name="SFT, think mode -- trust/safety tasks (misc/)",
                description=(
                    "Mixed lineage: 3 of these 5 files are under medpsy's cleaned tree, "
                    "2 are not -- see each entry's own screening note."
                ),
                entries=[
                    SourceEntry(
                        label="medec_deepseek",
                        path=f"{MEDPSY_MIX}/misc/medec_deepseek.jsonl",
                        description="MEDEC medical-error detection, 2,189 rows.",
                        screening=MEDPSY_FILTERED,
                    ),
                    SourceEntry(
                        label="baichuan_m3_medhallu_with_counterparts_and_knowledge",
                        path=f"{MEDPSY_MIX}/misc/baichuan_m3_medhallu_with_counterparts_and_knowledge.jsonl",
                        description="MedHallu factual-vs-hallucinated answer pairs with knowledge context, 36,000 rows.",
                        screening=(
                            "Not from final_cleaned_data/filtered -- this file's lineage.json points "
                            "at medhallu_data/ directly (medpsy's un-filtered tree), not filtered/medhallu_data."
                        ),
                    ),
                    SourceEntry(
                        label="baichuan_m3_medsafety",
                        path=f"{MEDPSY_MIX}/misc/baichuan_m3_medsafety.jsonl",
                        description="MedSafety refusal examples, 897 rows.",
                        screening=MEDPSY_FILTERED,
                    ),
                    SourceEntry(
                        label="baichuan_m3_toxigen",
                        path=f"{MEDPSY_MIX}/misc/baichuan_m3_toxigen.jsonl",
                        description="ToxiGen hate-speech judgments, 8,347 rows.",
                        screening=MEDPSY_FILTERED,
                    ),
                    SourceEntry(
                        label="q35_toolcall_think_unrolled_200k",
                        path=f"{MEDPSY_MIX}/misc/q35_toolcall_think_unrolled_200k.jsonl",
                        description="Tool-call conversations, 265,211 rows.",
                        screening=(
                            "Not medpsy data at all -- this file's lineage.json points at "
                            "data/tool_call/think/q35/unrolled_200k/, alex's own tool-call data, "
                            "so medpsy's PII/toxicity filter was never applicable to it."
                        ),
                    ),
                ],
            ),
            SourceGroup(
                name="SFT, think mode -- general IF / benchmark-synth / historical (MHB)",
                description="None of these three come from medpsy's cleaned tree.",
                screening=MEDPSY_NOT_FILTERED,
                entries=[
                    SourceEntry(
                        label="ultradata_think",
                        path=f"{MEDPSY_MIX}/ultradata_think",
                        description="General instruction-following data (UltraData), 20 shards, 199,883 rows.",
                    ),
                    SourceEntry(
                        label="medqa_chunks",
                        path=f"{MEDPSY_MIX}/medqa_chunks",
                        description="MedQA benchmark-synth chunks, 10,178 rows total.",
                    ),
                    SourceEntry(
                        label="mhb/baichuan_answer",
                        path=f"{MEDPSY_SYNTH}/mhb/baichuan_answer",
                        description="Historical medpsy synthetic dialogues (MHB), 5 chunks, 5,000 rows.",
                    ),
                ],
            ),
            SourceGroup(
                name="SFT -- identification",
                description="Small identity Q&A sets; neither is from medpsy's cleaned tree.",
                screening=MEDPSY_NOT_FILTERED,
                entries=[
                    SourceEntry(
                        label="identification/",
                        path=f"{MEDPSY_MIX}/identification",
                        description=(
                            "Identity Q&A (\"what is Tether/medpsy?\"); the mix's DATASET_REPORT.md "
                            "lists 111 rows across this folder's 3 files as used."
                        ),
                    ),
                    SourceEntry(
                        label="rendered_medpsy2.clean",
                        path=f"{MEDPSY_SYNTH}/output/self_identification/final_extraction_ok/rendered_medpsy2.clean.jsonl",
                        description="Identity-correction dialogues, 6,364 rows.",
                        screening=(
                            MEDPSY_NOT_FILTERED
                            + " It also stores extra text in each turn's reasoning_content, which "
                            "the scanner's extract_record_text() does not read -- see the Readable view."
                        ),
                    ),
                ],
            ),
            SourceGroup(
                name="SFT, no-think 25% (dual-mode)",
                description=(
                    "9 folders, each a no-think copy of a think-mode source above -- same rows, "
                    "rendered without <think> blocks, used at a 25% ratio in the mix."
                ),
                # Every entry in this group (expanded children + the one static
                # entry below) gets this exact same note, so it's also set here
                # at the group level -- the frontend shows a per-entry note
                # only when it differs from this one, which for this group it
                # never does.
                screening=MEDPSY_NOTHINK_NOTE,
                children_of=f"{MEDPSY_MIX}/nothink25",
                children_screening=MEDPSY_NOTHINK_NOTE,
                entries=[
                    SourceEntry(
                        label="nothink25_stage2_only/",
                        path=f"{MEDPSY_MIX}/nothink25_stage2_only",
                        description="No-think copy of ultradata_think + misc/q35_toolcall_think_unrolled_200k.jsonl, 115,835 rows.",
                        screening=MEDPSY_NOTHINK_NOTE,
                    ),
                ],
            ),
            SourceGroup(
                name="Final packed training file",
                description="",
                entries=[
                    SourceEntry(
                        label="bridge/training.jsonl",
                        path=f"{MEDPSY_BRIDGE}/training.jsonl",
                        description=(
                            "The actual packed SFT training file -- every source above combined, "
                            "27,664,342 source rows (per the mix's DATASET_REPORT.md)."
                        ),
                        screening=(
                            "Combines sources above after decoy-tool and system-prompt injection "
                            "(inject_prob 0.2, system_prob 0.1) -- that injected text ran AFTER "
                            "medpsy's own PII/toxicity filter, so it was never screened by it, "
                            "even for rows whose base content was."
                        ),
                    ),
                ],
            ),
            SourceGroup(
                name="RL",
                description="",
                entries=[
                    SourceEntry(
                        label="alphamed_curated_19k (train)",
                        path=f"{MEDPSY_RL_CURRICULUM}/train.parquet",
                        description="RL curriculum data (medical-exam MCQs), 18,304 rows.",
                        screening=(
                            "Not under final_cleaned_data/filtered, and this RL curriculum folder "
                            "has no lineage.json of its own -- whether it was screened separately "
                            "isn't documented here."
                        ),
                    ),
                    SourceEntry(
                        label="safety_reliability (train)",
                        path=f"{MEDPSY_RL_MGDA}/safety_train.parquet",
                        description="RL safety-objective data (MedHallu/MEDEC/ToxiGen/MedSafety), 21,982 rows.",
                        screening=(
                            "Built from medpsy's final_cleaned_data/filtered medhallu/medec/toxigen/"
                            "medsafety sources (safety_reliability.manifest.json in this folder) -- "
                            "individual toxic/unsafe examples are deliberately kept in as the RL "
                            "training signal, not filtered out."
                        ),
                    ),
                ],
            ),
        ],
    ),
]


TEAM_NAMES = {"Tools team": "BotPsy", "medpsy": "MedPsy"}

NAME_JUNK = re.compile(r"[^A-Za-z0-9._-]+")


# -------------------------------------------------------------------------
# children_of expansion
# -------------------------------------------------------------------------


def _is_dataset_dir(path: Path) -> bool:
    """A folder `prepare_rows.py` (and so this UI) can actually read as one
    dataset: a HuggingFace save_to_disk() folder, or a folder with at
    least one .jsonl/.parquet file directly inside it."""
    if (path / "state.json").is_file() or (path / "dataset_dict.json").is_file():
        return True
    try:
        return any(path.glob("*.jsonl")) or any(path.glob("*.parquet"))
    except OSError:
        return False


def _expand_children(folder: Path, screening: str) -> list[SourceEntry]:
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return []
    entries = []
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        if _is_dataset_dir(child):
            entries.append(SourceEntry(label=child.name, path=str(child), screening=screening))
    return entries


def _clean_name(label: str) -> str:
    """`label` as a name the registry accepts: every run of other characters becomes one
    space, and the name starts with a letter or digit ("mopd_mixv4_0907 (train)" becomes
    "mopd_mixv4_0907 train")."""
    cleaned = NAME_JUNK.sub(" ", label).strip().lstrip("._- ")
    return cleaned[: registry.NAME_MAX_CHARS]


def _unique_name(taken: set[str], label: str, folder_name: str) -> str:
    """A cleaned name that no other dataset of the team has. On a clash the parent
    folder's name is added, then 2, 3 and so on."""
    name = _clean_name(label) or _clean_name(folder_name) or "dataset"
    candidates = [name]
    suffix = _clean_name(folder_name)
    if suffix:
        candidates.append(f"{name[: registry.NAME_MAX_CHARS - len(suffix) - 1]} {suffix}")
    for candidate in candidates:
        if candidate.lower() not in taken:
            return candidate
    number = 2
    while True:
        ending = f" {number}"
        candidate = f"{candidates[-1][: registry.NAME_MAX_CHARS - len(ending)]}{ending}"
        if candidate.lower() not in taken:
            return candidate
        number += 1


def _group_entries(group: SourceGroup, skipped: list[dict[str, str]]) -> Iterator[SourceEntry]:
    """The entries of a group: the expanded subfolders first, then the listed ones, as
    the old picker listed them."""
    if group.children_of:
        folder = Path(group.children_of)
        reason = registry.unavailable_reason(folder)
        children = [] if reason else _expand_children(folder, group.children_screening)
        if not children:
            reason = reason or "No dataset folders were found inside it (empty, or it can't be listed)."
            skipped.append({"path": group.children_of, "reason": f"{group.name}: {reason}"})
        yield from children
    yield from group.entries


def seed_records(now: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """`(records, skipped)`: every starting dataset, and what couldn't be seeded."""
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    ids: set[str] = set()
    names: dict[str, set[str]] = {}
    inputs: dict[str, str] = {}

    for catalog in CATALOG:
        team = TEAM_NAMES[catalog.team]
        taken = names.setdefault(team, set())
        for group in catalog.groups:
            for entry in _group_entries(group, skipped):
                try:
                    path = str(detect.resolve_input_path(entry.path))
                except (detect.InvalidPath, OSError, RuntimeError) as exc:
                    skipped.append({"path": entry.path, "reason": str(exc)})
                    continue
                if path in inputs:
                    skipped.append({"path": path, "reason": f"Same path as '{inputs[path]}'."})
                    continue

                name = _unique_name(taken, entry.label, Path(path).parent.name)
                taken.add(name.lower())
                inputs[path] = name

                detail = entry.description or group.description
                description = f"{group.name}: {detail}" if detail else group.name
                notes = entry.screening or group.screening or group.children_screening
                dataset_id = registry.new_id(ids)
                ids.add(dataset_id)
                records.append(
                    registry.make_record(
                        dataset_id=dataset_id,
                        team=team,
                        name=name,
                        path=path,
                        description=description[: registry.MAX_DESCRIPTION],
                        notes=notes[: registry.MAX_NOTES],
                        conversion=None,
                        created_at=now,
                    )
                )
    return records, skipped
