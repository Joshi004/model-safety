"""Keeps the answers of a few slow read routes for a short time (stale-while-revalidate).

A route like GET /api/datasets asks SLURM and reads many files on a shared disk before it can
answer, which takes a second or more. `get` saves the answer and:

* returns it at once while it is at most `max_age` seconds old;
* returns it at once when it is older, and rebuilds it in a background thread, so the next
  request gets the new one;
* builds it while the caller waits when there is none, or when the caller asks for a live
  answer (`fresh`), which is what a page does when it opens.

Only one build of a key runs at a time. A caller that has to build waits for the one that is
running and uses its answer, so several pages opening together ask SLURM once.

Everything is in this process's memory (the service is one uvicorn process). Every route that
changes something calls `clear`, so a read after a change is built from what is there now.
Nothing here reads or writes any file.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

MAX_AGE_SECONDS = 15

# Guards the three below. Never held while a build runs.
_LOCK = threading.Lock()
# key -> (when its build ended, as time.monotonic(), the answer)
_SNAPSHOTS: dict[str, tuple[float, Any]] = {}
# key -> the lock its build holds, so only one build of a key runs at a time.
_BUILD_LOCKS: dict[str, threading.Lock] = {}
# Bumped by `clear`: an answer built before it describes what was there before the change.
_generation = 0


def _log(message: str) -> None:
    print(f"[snapshot] {message}", file=sys.stderr, flush=True)


def _saved(key: str) -> tuple[float, Any] | None:
    with _LOCK:
        return _SNAPSHOTS.get(key)


def _build_lock(key: str) -> threading.Lock:
    with _LOCK:
        return _BUILD_LOCKS.setdefault(key, threading.Lock())


def _build_and_save(key: str, build: Callable[[], Any]) -> Any:
    """Build the answer and save it, unless `clear` was called while it was being built. The
    caller holds the key's build lock."""
    with _LOCK:
        started_under = _generation
    value = build()
    with _LOCK:
        if started_under == _generation:
            _SNAPSHOTS[key] = (time.monotonic(), value)
    return value


def _rebuild_in_background(key: str, build: Callable[[], Any], lock: threading.Lock) -> None:
    """Runs in its own thread, which owns `lock` (taken by `get`) and gives it back. A failure
    keeps the old answer: the next request tries again."""
    try:
        _build_and_save(key, build)
    except Exception as exc:  # whatever the route raises, the old answer stays
        _log(f"rebuild of {key} failed: {exc!r}")
    finally:
        lock.release()


def get(key: str, build: Callable[[], Any], max_age: float = MAX_AGE_SECONDS, fresh: bool = False) -> Any:
    """The answer for `key`, built by `build()` (which raises what the route would raise).
    `fresh` asks for an answer built now, or by a build that is already running."""
    requested = time.monotonic()
    saved = _saved(key)
    lock = _build_lock(key)

    if saved is not None and not fresh:
        built_at, value = saved
        if requested - built_at > max_age and lock.acquire(blocking=False):
            threading.Thread(
                target=_rebuild_in_background, args=(key, build, lock), name=f"snapshot-{key}", daemon=True
            ).start()
        return value

    with lock:
        # A build that ended while this one waited for the lock is as new as one made now.
        again = _saved(key)
        if again is not None and again[0] >= requested:
            return again[1]
        return _build_and_save(key, build)


def clear() -> None:
    """Forget every answer. Called by every route that changes something."""
    global _generation
    with _LOCK:
        _generation += 1
        _SNAPSHOTS.clear()
