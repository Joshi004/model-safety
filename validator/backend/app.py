"""FastAPI backend for the input validator UI.

Started via ../run.sh, which sets VALIDATOR_FRONTEND_DIST and binds this
process to 127.0.0.1 only. There is no per-request auth and no path
allowlist: any absolute path the running user can read can be inspected,
and anyone who can already reach this machine's localhost (e.g. another
user logged into the same login node) can reach this too. That's a
deliberate choice for a trusted-network deployment -- the remaining
guardrail is being read-only (see below), not a secret.

This process never writes to the data it inspects: /api/inspect and
/api/preview only read a bounded sample and report what would happen,
they never run prepare_rows.py's actual write step. /api/sources (the
training-data catalog in sources.py, for the UI's sidebar dataset picker)
is the same: it only reads directory listings and stat()/exists() to
mark each entry available or not, never a row.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import detect
import preview
import sources
from reader_imports import json_safe

DEFAULT_SAMPLE_ROWS = 2
MAX_SAMPLE_ROWS = 200

app = FastAPI(title="Input validator UI")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/sources")
def sources_catalog() -> dict[str, Any]:
    """The curated Tools-team / medpsy training-data catalog (sources.py),
    for the sidebar's dataset picker. Read-only, same as every other
    route here -- this only reads directory listings and stat()/exists()
    to mark each entry available or not, it never opens a row."""
    return {"teams": sources.list_sources()}


class InspectRequest(BaseModel):
    path: str


@app.post("/api/inspect")
def inspect(body: InspectRequest) -> dict[str, Any]:
    try:
        resolved = detect.resolve_input_path(body.path)
    except detect.InvalidPath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return detect.inspect_path(resolved)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class PreviewRequest(BaseModel):
    path: str
    input_type: str
    file: str | None = None
    split: str = "train"
    max_rows: int = DEFAULT_SAMPLE_ROWS
    # Which row to start the sample at (0-indexed) -- lets the UI's row
    # navigator look at an arbitrary position in the file instead of
    # always the first `max_rows` rows. See detect.sample_rows() for how
    # each format reaches it.
    start_row: int = 0
    map: dict[str, str] = Field(default_factory=dict)
    require: list[str] = Field(default_factory=list)
    exclude_metadata: list[str] = Field(default_factory=list)
    metadata_column: str = "metadata"
    output_dir: str = "./converted"


@app.post("/api/preview")
def run_preview(body: PreviewRequest) -> dict[str, Any]:
    try:
        resolved = detect.resolve_input_path(body.path)
    except detect.InvalidPath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    max_rows = min(max(1, body.max_rows), MAX_SAMPLE_ROWS)
    start_row = max(0, body.start_row)

    try:
        rows, source_info = detect.sample_rows(
            resolved, body.input_type, body.file, max_rows, body.split, start_row
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # a real reader failure -- surface it to the UI, don't 500 blindly
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    # The structure tree is still built -- suggest_mapping() needs it -- but
    # it's not sent to the browser. The UI shows these same sample rows
    # directly (as "input_rows" below) instead of a tree summarizing them.
    structure = preview.build_structure_tree(rows)
    suggestion = preview.suggest_mapping(structure)
    result = preview.preview_rows(
        rows, body.map, body.require, body.exclude_metadata, body.metadata_column
    )
    command = preview.build_command(
        input_path=str(resolved),
        input_type=body.input_type,
        file=body.file,
        mapping=body.map,
        required=body.require,
        exclude_metadata=body.exclude_metadata,
        output_dir=body.output_dir,
    )

    return {
        "source_info": source_info,
        # The exact rows _prepare_row() receives -- same cleaned sample,
        # just made JSON-safe for the browser (see origin.json_safe).
        "input_rows": [json_safe(row) for row in rows],
        "suggested_mapping": suggestion,
        "preview": result,
        "command": command,
    }


# Serves the built frontend (validator/frontend/dist, built by run.sh) at
# every path /api/ doesn't claim. Registered last -- FastAPI/Starlette try
# routes in the order they were added, so the explicit /api/* routes above
# always get first chance to match.
_frontend_dist = os.environ.get("VALIDATOR_FRONTEND_DIST")
if _frontend_dist and Path(_frontend_dist).is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
