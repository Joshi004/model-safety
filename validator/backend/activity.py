"""
A short log of who started work on which dataset, so that a person about to start the same
work can be warned. It only ever warns: nothing is blocked because of it.

The service root can be shared by several people, each running their own copy of the service.
Their jobs are visible to everyone (`squeue` lists every user's jobs), but nothing says which
dataset a job is for. This log says it: every job a user starts for datasets adds one line to

    <service root>/activity/dataset_activity.jsonl

    {"at": "<UTC time>", "user": "...", "host": "...", "action": "conversion",
     "dataset_ids": ["ds-..."], "job_ids": ["123456"]}

`warnings_for` reads it and names every other user's job on those datasets that SLURM still
lists (queued or running). A job that is gone, or one of the current user's own, isn't a warning.

The file is only appended to and never rewritten, and a failure to write it never stops a job
from being started. Two people appending at the very same moment can, on a shared disk, lose a
line; that is accepted, as the log is a hint and not a lock.
"""

from __future__ import annotations

import getpass
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import registry

ACTIVITY_FOLDER = "activity"
ACTIVITY_FILE = "dataset_activity.jsonl"


def _log(message: str) -> None:
    print(f"[activity] {message}", file=sys.stderr, flush=True)


def _activity_file() -> Path:
    return registry.service_root() / ACTIVITY_FOLDER / ACTIVITY_FILE


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def record(action: str, dataset_ids: list[str], job_ids: list[str]) -> None:
    """Note that the current user started these SLURM jobs for these datasets. `action` is a
    few words that read well in a sentence ("conversion", "duplicates scan", "drop"). Nothing
    is written when there are no jobs: work that didn't start isn't worth a warning."""
    if not dataset_ids or not job_ids:
        return
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": _current_user(),
        "host": socket.gethostname(),
        "action": action,
        "dataset_ids": list(dataset_ids),
        "job_ids": [str(job_id) for job_id in job_ids],
    }
    path = _activity_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as exc:
        _log(f"couldn't note the {action} in {path}: {exc}")


def _read_entries() -> list[dict[str, Any]]:
    """Every readable line of the log, oldest first. A line that isn't a JSON object is skipped."""
    path = _activity_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        _log(f"couldn't read {path}: {exc}")
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def warnings_for(dataset_ids: list[str], active: dict[str, dict[str, Any]] | None) -> list[str]:
    """One sentence for each job of another user, on any of these datasets, that `active` (the
    result of `jobs.active_jobs()`) still lists. `active` is None when SLURM couldn't be asked;
    then nothing can be said and there are no warnings."""
    if active is None or not dataset_ids:
        return []
    wanted = set(dataset_ids)
    me = _current_user()
    found: list[tuple[str, dict[str, Any], list[str]]] = []
    for entry in _read_entries():
        if entry.get("user") == me:
            continue
        shared = [dataset_id for dataset_id in _strings(entry.get("dataset_ids")) if dataset_id in wanted]
        going = [job_id for job_id in _strings(entry.get("job_ids")) if job_id in active]
        if shared and going:
            found.extend((dataset_id, entry, going) for dataset_id in shared)
    if not found:
        return []

    names = registry.dataset_names()
    messages: list[str] = []
    for dataset_id, entry, going in found:
        name = names.get(dataset_id, dataset_id)
        jobs_text = ", ".join(going)
        message = (
            f"{entry.get('user', 'Someone')} started a {entry.get('action', 'job')} on '{name}' "
            f"(SLURM job {jobs_text}, {entry.get('at', 'time unknown')}) that is still queued or running."
        )
        if message not in messages:
            messages.append(message)
    return messages
