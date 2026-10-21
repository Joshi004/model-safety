"""The toxicity policies: the written rules the policy judge (Granite Guardian) applies to a
message Qwen3Guard rated Unsafe or Controversial.

There is one policy for each team, and a dataset can have a policy of its own that replaces its
team's (an override). A dataset without an override uses its team's policy as it is now, so a
change to the team's policy reaches every dataset that doesn't have an override. A scan copies
the policy it uses into its own folder when it starts (scanners.py), so a later change never
alters a scan that has started.

The policies are saved in `<service root>/registry/toxicity_policies.json`:

    {"version": 1,
     "teams":    {"<team>": {"text": ..., "updated_at": ...}},
     "datasets": {"<dataset id>": {"text": ..., "updated_at": ...}}}

Rules this module keeps:

* The file is created, the first time it is read, from the policy texts in the repo's
  `modelsafety/data_screening/toxicity/policies/`: MedPsy gets the medical policy, every other
  team the generic one. A team that is added to `registry.TEAMS` later, and so isn't in the file,
  is shown (and used) with the same starting text.
* One lock covers every read and write of the file, and a write goes to a temp file first and is
  then moved into place with `os.replace`, so the file is never half written.
* A file that can't be read is never overwritten: the request fails and says why.
* The lock is never held while calling into registry.py, and `forget_dataset` and
  `copy_dataset_policy` never call it, because registry.py calls `forget_dataset` while it holds
  its own lock.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import registry
from reader_imports import REPO_ROOT

POLICIES_VERSION = 1

POLICY_TEXTS_FOLDER = REPO_ROOT / "modelsafety" / "data_screening" / "toxicity" / "policies"
GENERIC_POLICY_FILE = "generic_v1.txt"
# The teams that don't start with the generic policy, and the file each starts with.
TEAM_POLICY_FILES = {"MedPsy": "medpsy_medical_sexual_v1.txt"}

# The policy and the conversation both have to fit the judge's 8,192-token context. The medical
# policy is about 2,000 characters.
MAX_POLICY_CHARS = 6000

SOURCE_TEAM = "team"
SOURCE_DATASET = "dataset"

_LOCK = threading.Lock()


class PolicyError(registry.RegistryError):
    """The request can't be carried out; the message says why. A kind of RegistryError, so the
    routes that already answer those with a 400 answer this the same way."""


class PolicyCorrupt(registry.RegistryCorrupt):
    """`toxicity_policies.json` exists but can't be read or isn't a policies file."""


# -------------------------------------------------------------------------
# Checking
# -------------------------------------------------------------------------


def _team(value: Any) -> str:
    names = [team["name"] for team in registry.TEAMS]
    if value not in names:
        raise PolicyError(f"Choose a team: one of {', '.join(names)}.")
    return value


def clean_text(value: Any) -> str:
    """A policy's text, checked: text, with its line ends made `\\n` and its ends trimmed, 1 to
    MAX_POLICY_CHARS characters, no control characters other than new lines and tabs."""
    if not isinstance(value, str):
        raise PolicyError("The policy must be text.")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text == "":
        raise PolicyError("The policy can't be empty.")
    if len(text) > MAX_POLICY_CHARS:
        raise PolicyError(f"The policy is {len(text)} characters long; the limit is {MAX_POLICY_CHARS}.")
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in text):
        raise PolicyError("The policy can't contain control characters.")
    return text


def _starting_text(team: str) -> str:
    """The text a team starts with: the repo's policy file for it, or the generic one."""
    path = POLICY_TEXTS_FOLDER / TEAM_POLICY_FILES.get(team, GENERIC_POLICY_FILE)
    try:
        return clean_text(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"Can't read the starting policy {path}: {exc}") from exc


# -------------------------------------------------------------------------
# Reading and writing the file (call these only while holding _LOCK)
# -------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def policies_file() -> Path:
    return registry.service_root() / "registry" / "toxicity_policies.json"


def _shape_problem(data: Any) -> str | None:
    """None when `data` looks like a policies file, otherwise what is wrong."""
    if not isinstance(data, dict) or data.get("version") != POLICIES_VERSION:
        return f"it isn't a version {POLICIES_VERSION} policies file"
    for section in ("teams", "datasets"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            return f"it has no '{section}' object"
        for name, entry in entries.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                return f"the entry '{name}' in '{section}' has no text"
    return None


def _save(data: dict[str, Any]) -> None:
    path = policies_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _seed() -> dict[str, Any]:
    now = _now()
    data: dict[str, Any] = {
        "version": POLICIES_VERSION,
        "teams": {team["name"]: {"text": _starting_text(team["name"]), "updated_at": now} for team in registry.TEAMS},
        "datasets": {},
    }
    _save(data)
    return data


def _read() -> dict[str, Any]:
    """The file's content. When the file doesn't exist yet, it is created from the policy texts."""
    path = policies_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _seed()
    except (OSError, ValueError) as exc:
        raise PolicyCorrupt(
            f"Can't read {path}: {exc}. It was left as it is. Fix or move it, then try again."
        ) from exc
    problem = _shape_problem(data)
    if problem is not None:
        raise PolicyCorrupt(f"{path} can't be used: {problem}. It was left as it is. Fix or move it, then try again.")
    return data


def _team_entry(data: dict[str, Any], team: str) -> dict[str, Any]:
    entry = data["teams"].get(team)
    if entry is None:
        # A team added after the file was made.
        return {"text": _starting_text(team), "updated_at": None}
    return {"text": entry["text"], "updated_at": entry.get("updated_at")}


# -------------------------------------------------------------------------
# Teams
# -------------------------------------------------------------------------


def team_policy(team: Any) -> dict[str, Any]:
    """`{team, text, updated_at}`: the team's policy."""
    name = _team(team)
    with _LOCK:
        entry = _team_entry(_read(), name)
    return {"team": name, **entry}


def set_team_policy(team: Any, text: Any) -> dict[str, Any]:
    """Save a new policy for the team. Every dataset of the team without an override uses it
    from now on."""
    name = _team(team)
    cleaned = clean_text(text)
    with _LOCK:
        data = _read()
        entry = {"text": cleaned, "updated_at": _now()}
        data["teams"][name] = entry
        _save(data)
    return {"team": name, **entry}


# -------------------------------------------------------------------------
# Datasets
# -------------------------------------------------------------------------


def _describe(dataset_id: str, team: str, data: dict[str, Any]) -> dict[str, Any]:
    team_entry = _team_entry(data, team)
    override = data["datasets"].get(dataset_id)
    used = override if override is not None else team_entry
    return {
        "dataset_id": dataset_id,
        "team": team,
        # Which of the two `text` is: SOURCE_DATASET for an override, SOURCE_TEAM otherwise.
        "source": SOURCE_DATASET if override is not None else SOURCE_TEAM,
        "text": used["text"],
        "updated_at": used.get("updated_at"),
        "team_text": team_entry["text"],
        "override_text": override["text"] if override is not None else None,
    }


def dataset_policy(dataset_id: str) -> dict[str, Any]:
    """The policy a dataset uses: `{dataset_id, team, source, text, updated_at, team_text,
    override_text}`. `override_text` is None when the dataset has no override."""
    team = registry.get_dataset(dataset_id)["team"]  # an unknown or malformed id fails here
    with _LOCK:
        return _describe(dataset_id, team, _read())


def set_dataset_policy(dataset_id: str, text: Any) -> dict[str, Any]:
    """Save an override for the dataset: its policy from now on, whatever its team's is."""
    team = registry.get_dataset(dataset_id)["team"]
    cleaned = clean_text(text)
    with _LOCK:
        data = _read()
        data["datasets"][dataset_id] = {"text": cleaned, "updated_at": _now()}
        _save(data)
        return _describe(dataset_id, team, data)


def clear_dataset_policy(dataset_id: str) -> dict[str, Any]:
    """Remove the dataset's override, so it uses its team's policy again. Nothing to do when it
    has none."""
    team = registry.get_dataset(dataset_id)["team"]
    with _LOCK:
        data = _read()
        if data["datasets"].pop(dataset_id, None) is not None:
            _save(data)
        return _describe(dataset_id, team, data)


def forget_dataset(dataset_id: str) -> None:
    """Remove the dataset's override, for a dataset that is being unregistered. Never calls
    registry.py (it is called while registry.py holds its lock), and so doesn't check that the
    dataset exists."""
    with _LOCK:
        data = _read()
        if data["datasets"].pop(dataset_id, None) is not None:
            _save(data)


def copy_dataset_policy(from_dataset_id: str, to_dataset_id: str) -> None:
    """Give `to_dataset_id` a copy of the override of `from_dataset_id`, if it has one: a
    filtered version keeps the policy of the dataset it was made from, instead of going back to
    the team's. Never calls registry.py."""
    with _LOCK:
        data = _read()
        source = data["datasets"].get(from_dataset_id)
        if source is None:
            return
        data["datasets"][to_dataset_id] = {"text": source["text"], "updated_at": _now()}
        _save(data)
