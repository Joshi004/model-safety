"""``progress.json``: where a ``prepare_rows.py`` run has got to.

``prepare_rows.py`` writes it into its own run folder, after the run
identity check has passed, so anyone (the validator UI, a person with
``cat``) can watch a run. It never changes what the run writes into
``chunk-*``, ``skipped_rows.ndjson`` or ``summary.json``.

The file is replaced whole each time (a temp file, then ``os.replace``), so a
reader never sees half of it.

Standard library only, because both ``prepare_rows.py`` and the validator
backend import it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROGRESS_FILENAME = "progress.json"
PROGRESS_TOOL = "prepare_rows.py"

# In the order a run goes through them; a phase that doesn't apply to the
# input type is passed over.
PHASES = (
    "finding_files",
    "fingerprinting_files",
    "reading",
    "fingerprinting_rows",
    "preparing",
    "checking_text",
    "writing",
    "finishing",
)
# The two ways a run ends.
END_PHASES = ("done", "failed")

# Loops only look at the clock once per this many rows or lines.
ROWS_PER_CLOCK_CHECK = 1000
# Within one phase, the file is rewritten at most this often (in seconds).
WRITE_INTERVAL_SECONDS = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProgressFile:
    """Writes ``<run_dir>/progress.json`` as a run moves through its phases."""

    def __init__(self, run_dir: str | Path) -> None:
        run_dir = Path(run_dir)
        self._path = run_dir / PROGRESS_FILENAME
        self._tmp_path = run_dir / f"{PROGRESS_FILENAME}.tmp"
        now = _now()
        self._state: Dict[str, Any] = {
            "tool": PROGRESS_TOOL,
            "phase": None,
            "started_at": now,
            "phase_started_at": now,
            "updated_at": now,
            "unit": None,
            "done": 0,
            "total": None,
            "files_done": None,
            "files_total": None,
            "rows_written": 0,
            "rows_skipped": 0,
            "error": None,
        }
        self._last_write = time.monotonic()

    def phase(
        self,
        name: str,
        unit: Optional[str] = None,
        total: Optional[int] = None,
        files_total: Optional[int] = None,
    ) -> None:
        """Start a phase and write the file straight away.

        ``rows_written`` and ``rows_skipped`` carry over from the phase before.
        """
        if name not in PHASES and name not in END_PHASES:
            raise ValueError(f"Unknown progress phase: {name}")
        self._state.update(
            phase=name,
            phase_started_at=_now(),
            unit=unit,
            done=0,
            total=total,
            files_done=0 if files_total is not None else None,
            files_total=files_total,
        )
        self._write()

    def update(
        self,
        done: int,
        files_done: Optional[int] = None,
        rows_written: Optional[int] = None,
        rows_skipped: Optional[int] = None,
    ) -> None:
        """Record progress within the current phase.

        ``None`` leaves a value as it was. The file is only rewritten when
        ``WRITE_INTERVAL_SECONDS`` have passed since the last write.
        """
        self._state["done"] = done
        if files_done is not None:
            self._state["files_done"] = files_done
        if rows_written is not None:
            self._state["rows_written"] = rows_written
        if rows_skipped is not None:
            self._state["rows_skipped"] = rows_skipped
        if time.monotonic() - self._last_write >= WRITE_INTERVAL_SECONDS:
            self._write()

    def fail(self, message: str) -> None:
        self._state["error"] = message
        self.phase("failed")

    def _write(self) -> None:
        self._state["updated_at"] = _now()
        self._tmp_path.write_text(json.dumps(self._state, indent=2) + "\n", encoding="utf-8")
        os.replace(self._tmp_path, self._path)
        self._last_write = time.monotonic()
