"""The registry's starting datasets (D12): today's `sources.CATALOG`, as registry records.

It runs once, when `datasets.json` doesn't exist yet (see `registry._seed`). The old
"Tools team" becomes BotPsy and "medpsy" becomes MedPsy. Groups that set `children_of`
are expanded into one dataset per dataset-shaped subfolder, as the old picker did.
Phase 2 moves the catalog data into this file and deletes `sources.py`.

Seeded paths aren't required to exist or be readable: the seed runs only once and must
not lose entries, so one the current user can't read is kept and listed as unavailable.
A folder that can't be expanded, or an entry that can't be seeded, is recorded in
`seed_skipped` (kept in `datasets.json`) with the reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

import detect
import registry
import sources

TEAM_NAMES = {"Tools team": "BotPsy", "medpsy": "MedPsy"}

NAME_JUNK = re.compile(r"[^A-Za-z0-9._-]+")


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


def _group_entries(group: sources.SourceGroup, skipped: list[dict[str, str]]) -> Iterator[sources.SourceEntry]:
    """The entries of a group: the expanded subfolders first, then the listed ones, as
    `sources._group_to_dict` orders them."""
    if group.children_of:
        folder = Path(group.children_of)
        reason = sources._unavailable_reason(folder)
        children = [] if reason else sources._expand_children(folder, group.children_screening)
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

    for catalog in sources.CATALOG:
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
