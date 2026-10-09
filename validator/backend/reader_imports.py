"""Single place that imports modelsafety/readers/prepare_rows.py directly.

detect.py and preview.py call the real functions from here instead of
reimplementing them, so the preview can't quietly drift from what a real
`prepare_rows.py` run does. Nothing here is a copy -- it's the same code,
imported.

`prepare_rows.py` expects its own directory on `sys.path` (it does
`from chunked_writer import write_chunked_output` as a bare import), so
that's added below before importing it. Importing `prepare_rows` also runs
its own top-level `sys.path.insert(...)` for the repo root, which is what
then makes `modelsafety.contract.textract` importable too -- that's why the
import order below matters.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
READERS_DIR = REPO_ROOT / "modelsafety" / "readers"
if str(READERS_DIR) not in sys.path:
    sys.path.insert(0, str(READERS_DIR))

import prepare_rows  # noqa: E402  (needs the sys.path insert above first)
import progress_file  # noqa: E402

from modelsafety.contract.fingerprints import (  # noqa: E402
    FINGERPRINT_RECIPE_VERSION,
    line_fingerprint,
    row_fingerprint,
)
from modelsafety.contract.origin import ORIGIN_KEY, json_safe  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    JOBS_FOLDER,
    MAX_RUN_FOLDERS,
    PREPARE_ROWS_TOOL,
    STALE_AFTER_SECONDS,
    list_runs,
    may_have_stopped,
    read_progress,
)
from modelsafety.contract.run_identity import (  # noqa: E402
    IDENTITY_FILENAME,
    RunIdentityError,
    compare_run_identity,
)
from modelsafety.contract.textract import extract_record_text  # noqa: E402

# Re-exported as plain names so callers don't need to know these are
# "private" (leading-underscore) helpers inside prepare_rows.py.
clean_value = prepare_rows._clean_value
prepare_row = prepare_rows._prepare_row
parse_mapping = prepare_rows._parse_mapping
is_present = prepare_rows._is_present
arrow_ipc_row_count = prepare_rows._arrow_ipc_row_count
source_file_entry = prepare_rows._source_file_entry
build_parser = prepare_rows.build_parser
discover_input_files = prepare_rows._discover_input_files
run_identity = prepare_rows._run_identity
SKIPPED_ROWS_FILENAME = prepare_rows.SKIPPED_ROWS_FILENAME
PROGRESS_FILENAME = progress_file.PROGRESS_FILENAME
PROGRESS_TOOL = progress_file.PROGRESS_TOOL
PROGRESS_PHASES = progress_file.PHASES
PROGRESS_END_PHASES = progress_file.END_PHASES

__all__ = [
    "REPO_ROOT",
    "FINGERPRINT_RECIPE_VERSION",
    "ORIGIN_KEY",
    "SKIPPED_ROWS_FILENAME",
    "PROGRESS_FILENAME",
    "PROGRESS_TOOL",
    "PROGRESS_PHASES",
    "PROGRESS_END_PHASES",
    "JOBS_FOLDER",
    "MAX_RUN_FOLDERS",
    "PREPARE_ROWS_TOOL",
    "STALE_AFTER_SECONDS",
    "list_runs",
    "may_have_stopped",
    "read_progress",
    "extract_record_text",
    "json_safe",
    "line_fingerprint",
    "row_fingerprint",
    "clean_value",
    "prepare_row",
    "parse_mapping",
    "is_present",
    "arrow_ipc_row_count",
    "source_file_entry",
    "build_parser",
    "discover_input_files",
    "run_identity",
    "compare_run_identity",
    "RunIdentityError",
    "IDENTITY_FILENAME",
]
