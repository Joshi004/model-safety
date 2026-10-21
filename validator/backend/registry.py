"""The saved list of datasets (the registry) and the service root it lives in.

Every dataset belongs to one team and points at the absolute path of its original
data. The list is `<service root>/registry/datasets.json`. It is read and written
here and nowhere else.

Rules this module keeps:

* The original data a record points at is only looked at (`stat`, `os.access`),
  never opened, written to, moved or deleted.
* The only thing this module deletes is `<service root>/datasets/<id>`, when a dataset is
  unregistered, after checking the id and that the folder really is that path.
  (conversions.py deletes the Prepared version folders inside it, with its own checks.)
  An unregistered dataset's toxicity policy override is removed too, by policies.py.
* `prepared` (the current Prepared version) and `kept_versions` (versions kept on disk
  after a re-conversion) are only changed by `set_prepared`, `clear_prepared` and `mark_kept`,
  which conversions.py and filters.py call. They are not part of `update`.
* `parent` is only set by `register_filtered` (filters.py): a filtered dataset is a new
  dataset made by dropping rows from its parent's Prepared version. It copies the parent's
  `path` and `conversion` (so its overview and original rows still work) but is never
  converted, so it is left out of the "same input twice" check, and its `conversion` can't be
  edited. A dataset that has filtered versions can't be unregistered: they trace their rows
  back through its Prepared version.
* `results` (the scan that is the dataset's current result, for each scanner) is only
  changed by `set_result`, which scans.py calls, and emptied by `clear_prepared`: every
  result was made from the version that is being replaced. `update_result` (results.py)
  adds the exact count and the coverage verdict once the scan's analysis has finished.
  It is not part of `update`.
* The `folder` of a Prepared version (`prepared`, `kept_versions`) is saved relative to the
  project root when it is inside it, so the file still works after the project is moved or
  cloned somewhere else. `set_prepared` saves it that way, and every read that leaves this
  module (`_with_availability`, `filtered_versions`) gives it back as an absolute path. A
  dataset's `path` is the original data, outside the project, and stays absolute.
* One lock covers every read and write of the file, because FastAPI runs these
  handlers in a thread pool. A write goes to a temp file first and is then moved
  into place with `os.replace`, so the file is never half written.
* A `datasets.json` that can't be read is never overwritten or seeded again: the
  request fails and says why.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import detect
from reader_imports import ORIGIN_KEY, REPO_ROOT, SUPPORTED_INPUT_TYPES, from_saved, to_saved

SERVICE_ROOT_ENV = "VALIDATOR_SERVICE_ROOT"
REGISTRY_VERSION = 1

# The fixed list of teams (D1). Adding a team means editing this list.
TEAMS = [
    {"name": "BotPsy", "description": "Tool team"},
    {"name": "BitPsy", "description": "LowBit"},
    {"name": "MedPsy", "description": ""},
    {"name": "VisionPsy", "description": ""},
    {"name": "TranslatePsy", "description": ""},
    {"name": "TranscribePsy", "description": ""},
]

DATASET_ID = re.compile(r"^ds-[0-9a-f]{12}$")
# A filtered version is named "<parent name>, filtered (N removed)" (D11), so the rule has to
# allow ',' '(' and ')'. A name only ever reaches a command through `jobs.slug`.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._,()-]{0,99}$")
SPLIT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HF_CONFIG = re.compile(r"^[A-Za-z0-9_.-]{0,100}$")

NAME_MAX_CHARS = 100
MAX_DESCRIPTION = 2000
MAX_NOTES = 5000
MAX_FILE_CHARS = 500
MAX_SETTING_CHARS = 200
MAX_MAP_ENTRIES = 50
MAX_COLUMN_LIST = 100
MAX_CHILDREN_LISTED = 5
# How many numbered names ("... 2", "... 3") are tried before giving up on a filtered version's name.
MAX_NAME_ATTEMPTS = 1000

CONVERSION_FIELDS = {"input_type", "file", "split", "hf_config", "map", "require", "exclude_metadata"}
UPDATABLE_FIELDS = ("team", "name", "description", "notes", "conversion")

_LOCK = threading.Lock()


class RegistryError(ValueError):
    """The request can't be carried out; the message says why."""


class DatasetNotFound(RegistryError):
    """No dataset has the id that was asked for."""


class RegistryCorrupt(RuntimeError):
    """`datasets.json` exists but can't be read or isn't a registry file."""


# -------------------------------------------------------------------------
# Where things live
# -------------------------------------------------------------------------


def service_root() -> Path:
    """`VALIDATOR_SERVICE_ROOT` if set, otherwise `<repo>/output/service`."""
    configured = os.environ.get(SERVICE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "output" / "service").resolve()


def registry_file() -> Path:
    return service_root() / "registry" / "datasets.json"


def _ensure_layout() -> None:
    root = service_root()
    for folder in ("registry", "datasets", "scans"):
        (root / folder).mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -------------------------------------------------------------------------
# Reading and writing datasets.json (call these only while holding _LOCK)
# -------------------------------------------------------------------------


def _shape_problem(data: Any) -> str | None:
    """None when `data` looks like a registry file, otherwise what is wrong."""
    if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
        return f"it isn't a version {REGISTRY_VERSION} registry file"
    if not isinstance(data.get("datasets"), list):
        return "it has no 'datasets' list"
    for record in data["datasets"]:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            return "an entry has no id"
        if DATASET_ID.fullmatch(record["id"]) is None:
            return f"'{record['id']}' isn't a dataset id"
        for key in ("team", "name", "path"):
            if not isinstance(record.get(key), str):
                return f"entry {record['id']} has no '{key}'"
    return None


def _save(data: dict[str, Any]) -> None:
    _ensure_layout()
    path = registry_file()
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _seed() -> dict[str, Any]:
    # Imported here because registry_seed uses this module's constants.
    import registry_seed

    now = _now()
    records, skipped = registry_seed.seed_records(now)
    data = {
        "version": REGISTRY_VERSION,
        "seeded_at": now,
        "seed_skipped": skipped,
        "datasets": records,
    }
    _save(data)
    return data


def _read_registry() -> dict[str, Any]:
    """The registry file's content. When the file doesn't exist yet, it is created
    from today's catalog (D12)."""
    path = registry_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _seed()
    except (OSError, ValueError) as exc:
        raise RegistryCorrupt(
            f"Can't read {path}: {exc}. It was left as it is. Fix or move it, then try again."
        ) from exc
    problem = _shape_problem(data)
    if problem is not None:
        raise RegistryCorrupt(f"{path} can't be used: {problem}. It was left as it is. Fix or move it, then try again.")
    return data


def new_id(taken: set[str]) -> str:
    while True:
        candidate = "ds-" + secrets.token_hex(6)
        if candidate not in taken:
            return candidate


def make_record(
    *,
    dataset_id: str,
    team: str,
    name: str,
    path: str,
    description: str,
    notes: str,
    conversion: dict[str, Any] | None,
    created_at: str,
) -> dict[str, Any]:
    """One registry entry. `prepared` is set when a conversion finishes (Phase 3), `results`
    when a scan finishes (Phase 4) and `parent` by `register_filtered` (Phase 10)."""
    return {
        "id": dataset_id,
        "team": team,
        "name": name,
        "path": path,
        "description": description,
        "notes": notes,
        "created_at": created_at,
        "conversion": conversion,
        "prepared": None,
        "kept_versions": [],
        "results": {},
        "parent": None,
    }


# -------------------------------------------------------------------------
# Validation
# -------------------------------------------------------------------------


def _team(value: Any) -> str:
    names = [team["name"] for team in TEAMS]
    if value not in names:
        raise RegistryError(f"Choose a team: one of {', '.join(names)}.")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str):
        raise RegistryError("Give the dataset a name.")
    name = value.strip()
    if NAME.fullmatch(name) is None:
        raise RegistryError(
            "The name must be 1 to 100 characters: letters, digits, spaces, '.', '_', '-', ',', '(' and ')', "
            "and it must start with a letter or digit."
        )
    return name


def _optional_text(value: Any, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RegistryError(f"The {label} must be text.")
    text = value.strip()
    if len(text) > limit:
        raise RegistryError(f"The {label} is {len(text)} characters long; the limit is {limit}.")
    return text


def _setting_text(value: Any, label: str, limit: int) -> str:
    """One short piece of a conversion setting: text, 1 to `limit` characters, no control characters."""
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"Every {label} must be non-empty text.")
    text = value.strip()
    if len(text) > limit:
        raise RegistryError(f"A {label} is {len(text)} characters long; the limit is {limit}.")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise RegistryError(f"A {label} can't contain control characters.")
    return text


def unavailable_reason(path: Path) -> str | None:
    """None when `path` exists and the user running this service can read it,
    otherwise a short reason. Only `stat` and `os.access`; nothing is opened."""
    try:
        if not path.exists():
            return f"Not found: {path}"
        needed = os.R_OK | os.X_OK if path.is_dir() else os.R_OK
        if not os.access(path, needed):
            return f"Not readable by the user running this service: {path}"
    except OSError as exc:
        return f"Can't look at {path}: {exc}"
    return None


def _readable_path(value: Any) -> Path:
    if not isinstance(value, str):
        raise RegistryError("Give the absolute path of the dataset.")
    try:
        resolved = detect.resolve_input_path(value)
    except detect.InvalidPath as exc:
        raise RegistryError(str(exc)) from exc
    except (OSError, RuntimeError) as exc:  # a symbolic link loop, for one
        raise RegistryError(f"Can't resolve the path {value}: {exc}") from exc
    reason = unavailable_reason(resolved)
    if reason is not None:
        raise RegistryError(reason)
    return resolved


def _conversion_file(value: Any, path: Path, input_type: str) -> str | None:
    """The `file` setting: a file inside the dataset's folder, or None."""
    if value is None or value == "":
        if input_type in ("csv", "json") and path.is_dir():
            raise RegistryError(f"A {input_type} dataset in a folder needs the file to read (conversion.file).")
        return None
    file = _setting_text(value, "file name", MAX_FILE_CHARS)
    if not path.is_dir():
        raise RegistryError("conversion.file can only be used when the dataset's path is a folder.")
    relative = PurePosixPath(file)
    if relative.is_absolute() or ".." in relative.parts:
        raise RegistryError("conversion.file must be a path inside the dataset's folder.")
    try:
        (path / file).resolve().relative_to(path)
    except (ValueError, OSError, RuntimeError) as exc:
        raise RegistryError("conversion.file must be a path inside the dataset's folder.") from exc
    if not (path / file).is_file():
        raise RegistryError(f"conversion.file isn't a file in the dataset's folder: {file}")
    return file


def _conversion_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RegistryError("conversion.map must be an object of target: source pairs.")
    if len(value) > MAX_MAP_ENTRIES:
        raise RegistryError(f"conversion.map has {len(value)} entries; the limit is {MAX_MAP_ENTRIES}.")
    mapping: dict[str, str] = {}
    for raw_target, raw_source in value.items():
        target = _setting_text(raw_target, "mapping target", MAX_SETTING_CHARS)
        source = _setting_text(raw_source, "mapping source", MAX_SETTING_CHARS)
        if "=" in target:
            raise RegistryError(f"The mapping target '{target}' can't contain '='.")
        if target == ORIGIN_KEY:
            raise RegistryError(
                f"'{ORIGIN_KEY}' can't be a mapping target: the preparing step adds its own '{ORIGIN_KEY}' "
                "note to every row."
            )
        if target in mapping:
            raise RegistryError(f"The mapping target '{target}' is given twice.")
        mapping[target] = source
    return mapping


def _column_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise RegistryError(f"conversion.{label} must be a list.")
    if len(value) > MAX_COLUMN_LIST:
        raise RegistryError(f"conversion.{label} has {len(value)} items; the limit is {MAX_COLUMN_LIST}.")
    items: list[str] = []
    for raw in value:
        item = _setting_text(raw, f"{label} column", MAX_SETTING_CHARS)
        if item not in items:
            items.append(item)
    return items


def _conversion(value: Any, path: Path) -> dict[str, Any] | None:
    """The saved conversion settings, checked and filled in; None when `value` is None."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RegistryError("conversion must be an object, or null.")
    unknown = sorted(str(key) for key in set(value) - CONVERSION_FIELDS)
    if unknown:
        raise RegistryError(f"Unknown conversion settings: {', '.join(unknown)}.")

    input_type = value.get("input_type")
    if input_type not in SUPPORTED_INPUT_TYPES:
        raise RegistryError(f"conversion.input_type must be one of {', '.join(SUPPORTED_INPUT_TYPES)}.")
    split = value.get("split", "train")
    if not isinstance(split, str) or SPLIT.fullmatch(split) is None:
        raise RegistryError("conversion.split must be 1 to 64 characters: letters, digits, '.', '_' and '-'.")
    hf_config = value.get("hf_config", "")
    if not isinstance(hf_config, str) or HF_CONFIG.fullmatch(hf_config) is None:
        raise RegistryError("conversion.hf_config must be at most 100 characters: letters, digits, '.', '_' and '-'.")

    return {
        "input_type": input_type,
        "file": _conversion_file(value.get("file"), path, input_type),
        "split": split,
        "hf_config": hf_config,
        "map": _conversion_map(value.get("map", {})),
        "require": _column_list(value.get("require", []), "require"),
        "exclude_metadata": _column_list(value.get("exclude_metadata", []), "exclude_metadata"),
    }


def _input_key(path: str, conversion: dict[str, Any] | None) -> tuple[str, str | None]:
    """What the converter would read: the path (plus the file, for csv and json, which
    take one file), and the split (for huggingface only), as `preview.prepare_args` does."""
    if conversion is None:
        return path, None
    input_type = conversion.get("input_type")
    file = conversion.get("file")
    target = str(Path(path) / file) if input_type in ("csv", "json") and file else path
    split = conversion.get("split") if input_type == "huggingface" else None
    return target, split


def _check_name_free(records: list[dict[str, Any]], team: str, name: str, ignore_id: str | None) -> None:
    for other in records:
        if other["id"] != ignore_id and other["team"] == team and other["name"].lower() == name.lower():
            raise RegistryError(f"{team} already has a dataset named '{other['name']}'. Choose another name.")


def _check_input_free(
    records: list[dict[str, Any]], path: str, conversion: dict[str, Any] | None, ignore_id: str | None
) -> None:
    key = _input_key(path, conversion)
    for other in records:
        # A filtered dataset shares its parent's path on purpose, and is never converted.
        if other["id"] == ignore_id or other.get("parent"):
            continue
        if _input_key(other["path"], other.get("conversion")) == key:
            split = f" (split {key[1]})" if key[1] else ""
            raise RegistryError(
                f"'{other['name']}' in {other['team']} is already registered for {key[0]}{split}."
            )


# -------------------------------------------------------------------------
# Reading datasets
# -------------------------------------------------------------------------


def _index_of(records: list[dict[str, Any]], dataset_id: Any) -> int:
    if not isinstance(dataset_id, str) or DATASET_ID.fullmatch(dataset_id) is None:
        raise RegistryError(f"'{dataset_id}' isn't a dataset id.")
    for index, record in enumerate(records):
        if record["id"] == dataset_id:
            return index
    raise DatasetNotFound(f"No dataset has the id {dataset_id}.")


def _version_with_folder(version: Any, convert: Callable[[str], str]) -> Any:
    """A copy of a Prepared version's record with its `folder` passed through `convert`.
    Anything that isn't such a record is returned as it is."""
    if isinstance(version, dict) and isinstance(version.get("folder"), str):
        return {**version, "folder": convert(version["folder"])}
    return version


def _absolute_folder(saved: str) -> str:
    return str(from_saved(saved))


def _with_absolute_folders(record: dict[str, Any]) -> dict[str, Any]:
    """A copy of `record` whose Prepared versions' folders are absolute paths, as everything
    outside this module uses them. The file holds them relative to the project root."""
    return {
        **record,
        "prepared": _version_with_folder(record.get("prepared"), _absolute_folder),
        "kept_versions": [
            _version_with_folder(version, _absolute_folder) for version in record.get("kept_versions") or []
        ],
    }


def _with_availability(record: dict[str, Any]) -> dict[str, Any]:
    reason = unavailable_reason(Path(record["path"]))
    return {
        **_with_absolute_folders(record),
        # Entries saved before Phase 3 have no such list, and before Phase 4 no results.
        "results": record.get("results") or {},
        "available": reason is None,
        "unavailable_reason": reason,
    }


def list_datasets(team: str | None = None) -> list[dict[str, Any]]:
    """Every dataset (or those of one team), each with whether its path can still be read."""
    if team is not None:
        _team(team)
    with _LOCK:
        records = _read_registry()["datasets"]
    return [_with_availability(record) for record in records if team is None or record["team"] == team]


def get_dataset(dataset_id: str) -> dict[str, Any]:
    with _LOCK:
        records = _read_registry()["datasets"]
        record = records[_index_of(records, dataset_id)]
    return _with_availability(record)


def dataset_names() -> dict[str, str]:
    """The name of every dataset by id, without looking at any dataset's path."""
    with _LOCK:
        records = _read_registry()["datasets"]
    return {record["id"]: record["name"] for record in records}


def filtered_versions(dataset_id: str) -> list[dict[str, Any]]:
    """The datasets made from this one by dropping rows (their `parent` is it), newest first,
    as the registry has them. A malformed or unknown id gives an empty list."""
    with _LOCK:
        records = _read_registry()["datasets"]
    children = [_with_absolute_folders(record) for record in records if record.get("parent") == dataset_id]
    return sorted(children, key=lambda record: record["created_at"], reverse=True)


# -------------------------------------------------------------------------
# Changing the registry
# -------------------------------------------------------------------------


def register(fields: dict[str, Any]) -> dict[str, Any]:
    """Add a dataset. `fields` holds team, name and path, and optionally description,
    notes and conversion."""
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        team = _team(fields.get("team"))
        name = _name(fields.get("name"))
        path = _readable_path(fields.get("path"))
        conversion = _conversion(fields.get("conversion"), path)
        description = _optional_text(fields.get("description"), "description", MAX_DESCRIPTION)
        notes = _optional_text(fields.get("notes"), "notes", MAX_NOTES)
        _check_name_free(records, team, name, None)
        _check_input_free(records, str(path), conversion, None)

        record = make_record(
            dataset_id=new_id({item["id"] for item in records}),
            team=team,
            name=name,
            path=str(path),
            description=description,
            notes=notes,
            conversion=conversion,
            created_at=_now(),
        )
        records.append(record)
        _save(data)
    return _with_availability(record)


def _filtered_name(parent_name: str, removed: int, taken: set[str]) -> str:
    """D11's name, "<parent name>, filtered (N removed)", cut short enough to fit the name rule,
    and numbered (" 2", " 3", ...) if the team already has a dataset of that name."""
    suffix = f", filtered ({removed} removed)"
    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        tail = suffix if attempt == 1 else f"{suffix} {attempt}"
        candidate = parent_name[: NAME_MAX_CHARS - len(tail)].rstrip() + tail
        if candidate.lower() not in taken:
            return _name(candidate)
    raise RegistryError(f"Couldn't find a free name for a filtered version of '{parent_name}'.")


def filtered_name(parent_id: str, removed_rows: int) -> str:
    """The name `register_filtered` would give now. Changes nothing: for the confirmation."""
    with _LOCK:
        records = _read_registry()["datasets"]
        parent = records[_index_of(records, parent_id)]
        taken = {other["name"].lower() for other in records if other["team"] == parent["team"]}
        return _filtered_name(parent["name"], removed_rows, taken)


def filtered_without_version() -> list[str]:
    """The ids of the filtered datasets that have no Prepared version yet: their filter job is
    going, or it failed."""
    with _LOCK:
        records = _read_registry()["datasets"]
    return [record["id"] for record in records if record.get("parent") and record.get("prepared") is None]


def register_filtered(parent_id: str, removed_rows: int, description: str) -> dict[str, Any]:
    """Register the dataset a drop will make (D11): in the parent's team, named
    "<parent name>, filtered (N removed)", with `parent` set. It copies the parent's path and
    conversion settings, so its overview and original rows work, but has no Prepared version
    until the filter job has finished (filters.py sets it with `set_prepared`)."""
    if isinstance(removed_rows, bool) or not isinstance(removed_rows, int) or removed_rows < 1:
        raise RegistryError("A filtered version has to drop at least one row.")
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        parent = records[_index_of(records, parent_id)]
        if not isinstance(parent.get("prepared"), dict):
            raise RegistryError(f"'{parent['name']}' isn't converted, so no rows can be dropped from it.")
        taken = {other["name"].lower() for other in records if other["team"] == parent["team"]}
        record = make_record(
            dataset_id=new_id({item["id"] for item in records}),
            team=parent["team"],
            name=_filtered_name(parent["name"], removed_rows, taken),
            path=parent["path"],
            description=_optional_text(description, "description", MAX_DESCRIPTION),
            notes="",
            conversion=json.loads(json.dumps(parent["conversion"])),
            created_at=_now(),
        )
        record["parent"] = parent_id
        records.append(record)
        _save(data)
    return _with_availability(record)


def update(dataset_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Change team, name, description, notes or conversion of a dataset. Only the keys
    in `fields` change; `conversion` is replaced as a whole (None clears it)."""
    unknown = sorted(set(fields) - set(UPDATABLE_FIELDS))
    if unknown:
        raise RegistryError(
            f"Can't change {', '.join(unknown)}. Only {', '.join(UPDATABLE_FIELDS)} can be changed."
        )
    if not fields:
        raise RegistryError(f"Nothing to change. Send at least one of {', '.join(UPDATABLE_FIELDS)}.")

    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        index = _index_of(records, dataset_id)
        record = records[index]
        if "conversion" in fields and record.get("parent"):
            raise RegistryError(
                f"'{record['name']}' is a filtered version, which is never converted, so its conversion "
                "settings can't be changed."
            )
        changed = dict(record)

        if "team" in fields:
            changed["team"] = _team(fields["team"])
        if "name" in fields:
            changed["name"] = _name(fields["name"])
        if "description" in fields:
            changed["description"] = _optional_text(fields["description"], "description", MAX_DESCRIPTION)
        if "notes" in fields:
            changed["notes"] = _optional_text(fields["notes"], "notes", MAX_NOTES)
        if "conversion" in fields:
            changed["conversion"] = _conversion(fields["conversion"], Path(record["path"]))

        if "team" in fields or "name" in fields:
            _check_name_free(records, changed["team"], changed["name"], dataset_id)
        if "conversion" in fields:
            _check_input_free(records, record["path"], changed["conversion"], dataset_id)

        records[index] = changed
        _save(data)
    return _with_availability(changed)


def unconverted_ids() -> list[str]:
    """The ids of the datasets that have no current Prepared version."""
    with _LOCK:
        records = _read_registry()["datasets"]
    return [record["id"] for record in records if record.get("prepared") is None]


def set_prepared(dataset_id: str, prepared: dict[str, Any]) -> bool:
    """Make `prepared` the dataset's current Prepared version, but only while it has none.
    Returns whether it was set. Called by conversions.py when a conversion has finished and
    its checks passed. Not reachable from a request: `update` refuses the field."""
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        record = records[_index_of(records, dataset_id)]
        if record.get("prepared") is not None:
            return False
        record["prepared"] = _version_with_folder(prepared, to_saved)
        _save(data)
    return True


def clear_prepared(dataset_id: str, version_id: str) -> bool:
    """Forget the current Prepared version when it is `version_id`, because it is being
    replaced. A version marked `kept` (a filtered dataset was made from it) is added to
    `kept_versions` so its folder is never deleted. Returns whether anything changed.
    Every scan result was made from the version being forgotten, so the dataset's `results`
    are emptied with it (D6: "not scanned" until the new jobs finish); scans.py deletes the
    scan folders nothing points at any more. This only changes the registry;
    conversions.py deletes the folder."""
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        record = records[_index_of(records, dataset_id)]
        current = record.get("prepared")
        if not isinstance(current, dict) or current.get("version_id") != version_id:
            return False
        record["prepared"] = None
        record["results"] = {}
        if current.get("kept"):
            record["kept_versions"] = [*(record.get("kept_versions") or []), current]
        _save(data)
    return True


def mark_kept(dataset_id: str, version_id: str) -> bool:
    """Mark the dataset's current Prepared version `kept`: a filtered dataset is being made from
    it, so "convert again" sets the folder aside instead of deleting it (D6). Returns whether
    `version_id` is the current version (False: it changed, and nothing was marked). Called by
    filters.py when a drop starts, before the filter job can read the folder. It stays marked
    for good: unregistering the filtered dataset doesn't unmark it."""
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        record = records[_index_of(records, dataset_id)]
        current = record.get("prepared")
        if not isinstance(current, dict) or current.get("version_id") != version_id:
            return False
        if not current.get("kept"):
            current["kept"] = True
            _save(data)
    return True


def set_result(dataset_id: str, scanner: str, result: dict[str, Any], version_id: str) -> str | None:
    """Make `result` (`scan_id`, `finished_at`, `mode`, `count`) the dataset's current result
    for `scanner`, but only while the dataset's current Prepared version is `version_id`, the
    one the scan read: a conversion that replaced it in the meantime has already emptied the
    results, and the scan no longer describes the dataset. Returns the scan ID this replaced,
    or None when nothing was replaced (nothing was there, it is the same scan, or the
    version had changed). Called by scans.py when a scan has finished. Not reachable from a
    request: `update` refuses the field."""
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        record = records[_index_of(records, dataset_id)]
        prepared = record.get("prepared")
        if not isinstance(prepared, dict) or prepared.get("version_id") != version_id:
            return None
        results = dict(record.get("results") or {})
        previous = results.get(scanner)
        previous_id = previous.get("scan_id") if isinstance(previous, dict) else None
        if previous_id == result["scan_id"]:
            return None
        results[scanner] = result
        record["results"] = results
        _save(data)
    return previous_id


RESULT_COUNT_KEY = "count"
RESULT_COVERAGE_KEY = "coverage"
COVERAGE_VERDICTS = ("pass", "fail")


def update_result(dataset_id: str, scanner: str, scan_id: str, fields: dict[str, Any]) -> bool:
    """Set `count` and/or `coverage` ("pass" or "fail", or None) on the dataset's current
    result for `scanner`, but only while that result is still `scan_id`: a newer scan may have
    replaced it, and the numbers describe the old one. Called by results.py once a scan's
    analysis has finished. Returns whether anything was written. Not reachable from a request."""
    unknown = sorted(set(fields) - {RESULT_COUNT_KEY, RESULT_COVERAGE_KEY})
    if unknown:
        raise RegistryError(f"A result's {', '.join(unknown)} can't be set this way.")
    count = fields.get(RESULT_COUNT_KEY)
    if RESULT_COUNT_KEY in fields and count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise RegistryError("A result's count must be a whole number, or None.")
    if RESULT_COVERAGE_KEY in fields and fields[RESULT_COVERAGE_KEY] not in (*COVERAGE_VERDICTS, None):
        raise RegistryError(f"A result's coverage must be one of {', '.join(COVERAGE_VERDICTS)}, or None.")
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        record = records[_index_of(records, dataset_id)]
        results = dict(record.get("results") or {})
        current = results.get(scanner)
        if not isinstance(current, dict) or current.get("scan_id") != scan_id:
            return False
        results[scanner] = {**current, **fields}
        record["results"] = results
        _save(data)
    return True


def result_pointers() -> dict[str, list[str]]:
    """Every scan that is some dataset's current result, with the datasets it is the result
    of. A scan ID is unique across scanners, so it is the key. scans.py deletes a scan folder
    that was a result once nothing points at it."""
    with _LOCK:
        records = _read_registry()["datasets"]
    pointers: dict[str, list[str]] = {}
    for record in records:
        results = record.get("results")
        for result in results.values() if isinstance(results, dict) else []:
            scan_id = result.get("scan_id") if isinstance(result, dict) else None
            if isinstance(scan_id, str):
                pointers.setdefault(scan_id, []).append(record["id"])
    return pointers


def unregister(dataset_id: str) -> dict[str, Any]:
    """Remove a dataset's entry and its folder under `<service root>/datasets`.
    The original data is never touched.

    The folder is deleted first and the entry afterwards, so a folder that can't be
    deleted leaves the dataset registered and the request can be repeated. The
    folder holds the dataset's Prepared versions and conversion records. A running
    conversion is checked by conversions.unregister, and a running scan by
    scans.unregister, which the route calls; the scan folders that were this dataset's
    results are deleted by scans.py afterwards, unless another dataset still uses them."""
    with _LOCK:
        data = _read_registry()
        records = data["datasets"]
        index = _index_of(records, dataset_id)
        record = records[index]

        children = [other["name"] for other in records if other.get("parent") == dataset_id]
        if children:
            listed = ", ".join(f"'{name}'" for name in children[:MAX_CHILDREN_LISTED])
            more = f" and {len(children) - MAX_CHILDREN_LISTED} more" if len(children) > MAX_CHILDREN_LISTED else ""
            raise RegistryError(
                f"'{record['name']}' has filtered versions ({listed}{more}). They trace their rows back "
                "through its converted data, so unregister them first."
            )

        folder = service_root() / "datasets" / dataset_id
        removed_folder: str | None = None
        if folder.is_symlink():
            raise RegistryError(f"{folder} is a symbolic link, so it isn't deleted.")
        if folder.exists():
            if folder.resolve() != folder:
                raise RegistryError(f"{folder} isn't where it should be (it resolves to {folder.resolve()}), so it isn't deleted.")
            if not folder.is_dir():
                raise RegistryError(f"{folder} isn't a folder, so it isn't deleted.")
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                raise RegistryError(f"Couldn't delete {folder} ({exc}). The dataset is still registered.") from exc
            removed_folder = str(folder)

        del records[index]
        _save(data)

        # The dataset's toxicity policy override goes with it. Imported here because policies.py
        # uses this module. The dataset is gone either way: an override that couldn't be removed
        # belongs to an id that is never used again, so it doesn't fail the request.
        import policies

        try:
            policies.forget_dataset(dataset_id)
        except (RegistryError, RegistryCorrupt, OSError):
            pass
    return {"id": dataset_id, "name": record["name"], "removed_folder": removed_folder}
