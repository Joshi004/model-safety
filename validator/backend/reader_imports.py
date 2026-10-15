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
from modelsafety.contract.chain import (  # noqa: E402
    LAYOUT as CHAIN_LAYOUT,
    ChainError,
    chain_folders,
    create_planned_chain,
    list_chains,
    read_chain,
)
from modelsafety.contract.origin import ORIGIN_KEY, json_safe  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    FILTER_ROWS_TOOL,
    JOBS_FOLDER,
    MAX_RUN_FOLDERS,
    PREPARE_ROWS_TOOL,
    STALE_AFTER_SECONDS,
    RunFinderError,
    list_runs,
    may_have_stopped,
    read_json_or_none,
    read_progress,
    select_scan_files,
)
from modelsafety.contract.run_identity import (  # noqa: E402
    IDENTITY_FILENAME,
    RunIdentityError,
    compare_run_identity,
)
from modelsafety.contract.textract import extract_record_text  # noqa: E402

# The values the filter's rule flags accept, so the UI offers exactly those.
from modelsafety.data_screening.pii.regex.pii_output import (  # noqa: E402
    PII_CATEGORIES,
    STAGE1_NAME as PII_STAGE1_NAME,
    STAGE2_NAME as PII_STAGE2_NAME,
    STAGE3_NAME as PII_STAGE3_NAME,
    SUCCESS_FILENAME as PII_SUCCESS_FILENAME,
    SUMMARY_FILENAME as PII_SUMMARY_FILENAME,
)
from modelsafety.filter.rules import (  # noqa: E402
    DROP_LIST_REASON,
    GUARD_ERROR_CHOICES,
    MAX_DROP_LIST_REASONS,
    TOXICITY_REMOVABLE_VERDICTS,
)

# The text of one message as the NER scan cut it: its spans' start and end are positions in it.
from modelsafety.data_screening.pii.model.common import content_text, label_tier  # noqa: E402

# The NER policy file's reader: the NER scanner checks the policy it writes with it, so a bad
# file is refused when the scan starts and not by the GPU job.
from modelsafety.data_screening.pii.model.policy import CandidatePolicy  # noqa: E402

# The lookup behind "Show original", and the helpers the preview shares with it.
# It lives in modelsafety/trace/original.py so the trace tool uses the same code.
from modelsafety.trace.find_prepared import find_prepared_row, run_reads_path  # noqa: E402
from modelsafety.trace.row_status_read import (  # noqa: E402
    RowStatusReadError,
    read_removed_page,
    read_row,
    read_summary as read_row_status_summary,
)
from modelsafety.trace.original import (  # noqa: E402
    HF_MARKER_FILES,
    PARQUET_BATCH_ROWS,
    RunError,
    find_run_above,
    load_json_rows,
    lookup,
    may_convert_differently,
    open_run,
)

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
SUPPORTED_INPUT_TYPES = prepare_rows.SUPPORTED_INPUT_TYPES
PROGRESS_FILENAME = progress_file.PROGRESS_FILENAME
PROGRESS_TOOL = progress_file.PROGRESS_TOOL
PROGRESS_PHASES = progress_file.PHASES
PROGRESS_END_PHASES = progress_file.END_PHASES

__all__ = [
    "REPO_ROOT",
    "FINGERPRINT_RECIPE_VERSION",
    "ORIGIN_KEY",
    "SKIPPED_ROWS_FILENAME",
    "SUPPORTED_INPUT_TYPES",
    "PROGRESS_FILENAME",
    "PROGRESS_TOOL",
    "PROGRESS_PHASES",
    "PROGRESS_END_PHASES",
    "JOBS_FOLDER",
    "MAX_RUN_FOLDERS",
    "PREPARE_ROWS_TOOL",
    "FILTER_ROWS_TOOL",
    "STALE_AFTER_SECONDS",
    "RunFinderError",
    "select_scan_files",
    "PII_CATEGORIES",
    "PII_STAGE1_NAME",
    "PII_STAGE2_NAME",
    "PII_STAGE3_NAME",
    "PII_SUCCESS_FILENAME",
    "PII_SUMMARY_FILENAME",
    "GUARD_ERROR_CHOICES",
    "TOXICITY_REMOVABLE_VERDICTS",
    "DROP_LIST_REASON",
    "MAX_DROP_LIST_REASONS",
    "CHAIN_LAYOUT",
    "ChainError",
    "chain_folders",
    "create_planned_chain",
    "find_prepared_row",
    "list_chains",
    "read_chain",
    "RowStatusReadError",
    "read_removed_page",
    "read_row",
    "read_row_status_summary",
    "list_runs",
    "may_have_stopped",
    "read_json_or_none",
    "read_progress",
    "run_reads_path",
    "extract_record_text",
    "content_text",
    "label_tier",
    "CandidatePolicy",
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
    "HF_MARKER_FILES",
    "PARQUET_BATCH_ROWS",
    "RunError",
    "find_run_above",
    "load_json_rows",
    "lookup",
    "may_convert_differently",
    "open_run",
]
