"""Mapping suggestions, the real per-row preview, and the matching
`prepare_rows.py` command line.

The actual mapping step (`_prepare_row`), the JSON-serializability check
`chunked_writer.write_chunked_output()` makes right before writing, and
the scanner's own text extractor are all imported from `reader_imports`
and called directly here -- nothing in this file reimplements them, so a
preview can't quietly disagree with what a real run would do.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from reader_imports import extract_record_text, json_safe, prepare_row

# -------------------------------------------------------------------------
# Structure tree -- inferred from sample rows. Not part of the API
# response (the UI shows the real sample rows directly instead -- see
# `json_safe()`), but still built internally: `suggest_mapping()`
# reads this tree, not the raw rows, to decide what to suggest.
# -------------------------------------------------------------------------

MAX_EXAMPLE_STR_LEN = 200
MAX_FIELDS_PER_NODE = 200
MAX_TREE_DEPTH = 8

_TYPE_LABELS = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "struct",
    "list": "list",
    "NoneType": "null",
}


def _type_label(value: Any) -> str:
    return _TYPE_LABELS.get(type(value).__name__, type(value).__name__)


def _example_repr(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_EXAMPLE_STR_LEN:
            return value
        return value[:MAX_EXAMPLE_STR_LEN] + f"... [{len(value)} chars total]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return f"{{...}} ({len(value)} field(s))"
    if isinstance(value, list):
        return f"[...] ({len(value)} item(s))"
    return str(value)[:MAX_EXAMPLE_STR_LEN]


def _new_node() -> dict[str, Any]:
    return {"types": set(), "count": 0, "fields": {}, "item": None, "example": None, "_truncated": False}


def _merge_into(node: dict[str, Any], value: Any, depth: int) -> None:
    node["count"] += 1
    node["types"].add(_type_label(value))
    if node["example"] is None and value is not None:
        node["example"] = _example_repr(value)

    if depth >= MAX_TREE_DEPTH:
        return
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if key not in node["fields"]:
                if len(node["fields"]) >= MAX_FIELDS_PER_NODE:
                    node["_truncated"] = True
                    continue
                node["fields"][key] = _new_node()
            _merge_into(node["fields"][key], sub_value, depth + 1)
    elif isinstance(value, list):
        if node["item"] is None:
            node["item"] = _new_node()
        for element in value:
            _merge_into(node["item"], element, depth + 1)


def _finalize_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "types": sorted(node["types"]),
        "count": node["count"],
        "fields": {key: _finalize_node(child) for key, child in node["fields"].items()},
        "fields_truncated": node["_truncated"],
        "item": _finalize_node(node["item"]) if node["item"] is not None else None,
        "example": node["example"],
    }


def build_structure_tree(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A tree of every field seen across `rows`, used internally by
    `suggest_mapping()` below -- not part of the API response.

    `count` at the top level means "how many sampled rows had this field";
    inside a list's `item`, it means "how many elements had this shape"
    (usually larger than the row count). `types` can hold more than one
    name if the field isn't consistent across rows/elements -- that
    inconsistency is itself useful to show, not an error.
    """
    root = _new_node()
    for row in rows:
        _merge_into(root, row, depth=0)
    tree = _finalize_node(root)
    tree["sample_size"] = len(rows)
    return tree


# -------------------------------------------------------------------------
# Mapping suggestions -- a starting guess, plus hints for the two shapes
# documented (and tested against real data) in HOW_DATA_CONVERSION_WORKS.md
# as looking conversation-like but not actually working with a plain
# top-level --map rename: a conversation nested one level inside another
# field, and turns stored as two parallel arrays instead of one list of
# {role, content} pairs.
# -------------------------------------------------------------------------

_TEXT_FIELD_NAME_HINTS = ("text", "prompt", "content", "input", "document")


def _is_text_field(field: dict[str, Any]) -> bool:
    return field["types"] == ["string"]


def _is_messages_field(field: dict[str, Any]) -> bool:
    """A list of {role, content} turns -- the one shape extract_record_text()
    actually reads out of `messages`."""
    if "list" not in field["types"] or not field.get("item"):
        return False
    item = field["item"]
    item_fields = item.get("fields", {})
    return "struct" in item.get("types", []) and "role" in item_fields and "content" in item_fields


def suggest_mapping(tree: dict[str, Any]) -> dict[str, Any]:
    """Guesses a starting --map, plus hints for shapes the reader can't
    turn into scannable text with a plain top-level rename.

    A column that's already named `text` or `messages` *and* already
    shaped the way `extract_record_text()` wants it is suggested as a
    (redundant-looking but real) `--map text=text` / `--map
    messages=messages` -- confirmed directly against the real tool, not
    assumed: prepare_rows.py with no flags at all on a parquet file whose
    column is already named "messages" reports 0% text coverage and exits
    2, because nothing told it to promote that column out of metadata.

    An exact name+shape match like that always wins over a weaker
    name-hint guess (a column called "prompt" that's just a plain
    string) -- checked in a first pass over every field, so the result
    can't depend on which field happens to come first in the sampled
    data.
    """
    top_fields = tree.get("fields", {})
    suggested_map: dict[str, str] = {}

    # Pass 1: already correctly named and shaped -- used as-is.
    for name, field in top_fields.items():
        if name == "text" and _is_text_field(field):
            suggested_map["text"] = "text"
        if name == "messages" and _is_messages_field(field):
            suggested_map["messages"] = "messages"

    # Pass 2: a plausible rename, only for whichever target pass 1 left
    # unfilled.
    for name, field in top_fields.items():
        if (
            "text" not in suggested_map
            and name != "text"
            and _is_text_field(field)
            and name in _TEXT_FIELD_NAME_HINTS
        ):
            suggested_map["text"] = name
        if "messages" not in suggested_map and name != "messages" and _is_messages_field(field):
            suggested_map["messages"] = name

    hints: list[dict[str, str]] = []
    for name, field in top_fields.items():
        types = field["types"]

        if "list" in types and field.get("item"):
            item = field["item"]
            item_fields = item.get("fields", {})
            if "struct" in item.get("types", []) and "role" in item_fields and "content" not in item_fields:
                other_keys = sorted(k for k in item_fields if k != "role")
                hints.append(
                    {
                        "field": name,
                        "severity": "warning",
                        "message": (
                            f"'{name}' looks like a list of chat turns (each one has 'role'), "
                            f"but none have a 'content' field -- they have {other_keys} instead. "
                            "The scanner only reads a field literally called 'content', so "
                            f"mapping messages={name} would still scan as empty."
                        ),
                    }
                )

        if "struct" in types:
            sub_fields = field.get("fields", {})
            for sub_name, sub_field in sub_fields.items():
                if "list" not in sub_field.get("types", []):
                    continue
                sub_item = sub_field.get("item") or {}
                if "struct" in sub_item.get("types", []) and "role" in sub_item.get("fields", {}):
                    hints.append(
                        {
                            "field": f"{name}.{sub_name}",
                            "severity": "warning",
                            "message": (
                                f"The conversation is inside '{name}', under '{sub_name}' -- not "
                                f"at the top level. Mapping can only move a whole top-level key, "
                                f"so pointing messages at '{name}' would hand the scanner the "
                                f"entire '{name}' object (not just the turns inside it), which "
                                "scans as empty. This key can't be mapped from here."
                            ),
                        }
                    )

            # Only lists of plain scalars (strings, e.g. toolbench's
            # "from"/"value") count as parallel-array turn candidates.
            # Lists of structs (e.g. nemo_gym's "input"/"tools", a message
            # list alongside a tool-definitions list) are a different,
            # unrelated shape, not turns split into two arrays -- checked
            # against both real datasets before adding this condition.
            scalar_list_fields = sorted(
                n
                for n, f in sub_fields.items()
                if "list" in f.get("types", [])
                and f.get("item")
                and "struct" not in f["item"].get("types", [])
                and "list" not in f["item"].get("types", [])
            )
            if len(scalar_list_fields) >= 2:
                hints.append(
                    {
                        "field": name,
                        "severity": "warning",
                        "message": (
                            f"'{name}' stores the conversation as separate lists -- "
                            f"{scalar_list_fields} -- instead of one list of turns. Mapping can "
                            "only rename a column, it can't combine separate lists into turns, "
                            "so this can't be mapped from here."
                        ),
                    }
                )

    return {"map": suggested_map, "hints": hints}


# -------------------------------------------------------------------------
# The real preview -- every sample row through prepare_rows.py's own
# _prepare_row(), the same json.dumps() call chunked_writer makes right
# before writing, then the scanner's own extract_record_text().
# -------------------------------------------------------------------------


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except TypeError:
        return False


def _find_unserializable_column(prepared: dict[str, Any], metadata_column: str) -> dict[str, Any] | None:
    """Finds which value in `prepared` breaks json.dumps(), and whether
    `--exclude-metadata` could actually fix it.

    `--exclude-metadata` only ever removes a *source* column from the
    `metadata` dict (see `_prepare_row`'s `excluded` set) -- it can't touch
    a mapped/required top-level column like `text` or `messages`. So the
    two cases need different advice, and the metadata case needs the bare
    source key (e.g. "ts"), not the display path ("metadata.ts") -- that
    display path isn't a valid --exclude-metadata value.
    """
    for key, value in prepared.items():
        if key == metadata_column:
            continue
        if not _is_json_serializable(value):
            return {"display_path": key, "exclude_metadata_key": None, "in_metadata": False}
    metadata = prepared.get(metadata_column)
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if not _is_json_serializable(value):
                return {
                    "display_path": f"{metadata_column}.{key}",
                    "exclude_metadata_key": key,
                    "in_metadata": True,
                }
    return None


def _write_error_hint(bad: dict[str, Any] | None) -> str:
    if bad is None:
        return "Could not isolate which value caused this; see the error message above."
    if bad["in_metadata"]:
        return (
            f"Add '{bad['exclude_metadata_key']}' to --exclude-metadata to drop it, "
            "or map it to text/messages if it's actually the content you want scanned."
        )
    return (
        f"'{bad['display_path']}' is itself a mapped or required column, not just "
        "extra metadata, so --exclude-metadata can't remove it -- the source "
        f"data's '{bad['display_path']}' value needs to become a plain string, "
        "number, boolean, or nested list/dict of those before this can convert."
    )


def preview_rows(
    rows: list[dict[str, Any]],
    mapping: dict[str, str],
    required: list[str],
    exclude_metadata: list[str],
    metadata_column: str = "metadata",
) -> dict[str, Any]:
    excluded = set(exclude_metadata)
    output_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []
    write_errors: list[dict[str, Any]] = []
    rows_with_text = 0

    for index, row in enumerate(rows):
        prepared, missing_column = prepare_row(
            row=row,
            mapping=mapping,
            required_columns=required,
            metadata_column=metadata_column,
            exclude_metadata=excluded,
        )
        if prepared is None:
            skipped_rows.append({"row_index": index, "missing_column": missing_column})
            continue

        try:
            json.dumps(prepared, ensure_ascii=False)
        except TypeError as exc:
            bad = _find_unserializable_column(prepared, metadata_column)
            write_errors.append(
                {
                    "row_index": index,
                    "error": str(exc),
                    "likely_column": bad["display_path"] if bad else None,
                    "exclude_metadata_key": bad["exclude_metadata_key"] if bad else None,
                    "hint": _write_error_hint(bad),
                }
            )
            continue

        # The write check above ran on the raw `prepared` dict -- exactly
        # what a real run would try to json.dumps(). Only now, once that's
        # confirmed to succeed, is it converted for display: json_safe()
        # would otherwise quietly mask the very problem the check above is
        # looking for.
        scanned_text = extract_record_text(prepared)
        if scanned_text:
            rows_with_text += 1
        output_rows.append(
            {
                "row_index": index,
                "prepared": json_safe(prepared),
                "scanned_text": scanned_text,
                "scanned_text_length": len(scanned_text),
            }
        )

    written = len(output_rows) + len(write_errors)
    coverage = (rows_with_text / written) if written else 0.0

    return {
        "sample_size": len(rows),
        "written": written,
        "skipped": len(skipped_rows),
        "skipped_rows": skipped_rows,
        "write_errors": write_errors,
        "fatal_if_run": bool(write_errors),
        "fatal_note": (
            "prepare_rows.py does not skip a row like this -- json.dumps() runs inside "
            "write_chunked_output()'s write loop, so the first row like this crashes the "
            "whole run. Every error found in this sample is shown so they can all be fixed "
            "at once, not just the first."
            if write_errors
            else ""
        ),
        "rows": output_rows,
        "rows_with_text": rows_with_text,
        "coverage": coverage,
    }


# -------------------------------------------------------------------------
# Command builder
# -------------------------------------------------------------------------


READERS_PYTHON = "modelsafety/readers/.venv/bin/python"
PREPARE_ROWS_SCRIPT = "modelsafety/readers/prepare_rows.py"


def prepare_args(
    *,
    input_path: str,
    input_type: str,
    file: str | None,
    output_dir: str,
    mapping: dict[str, str],
    required: list[str],
    exclude_metadata: list[str],
    split: str = "train",
    hf_config: str = "",
) -> list[str]:
    """The prepare_rows.py arguments (without the interpreter) for the
    mapping being previewed. The command shown to the user and the job the
    UI submits are both built from this one list, so they can't differ.

    For jsonl/parquet/huggingface, `input_path` is used as-is (a file or a
    folder) regardless of `file` -- a real run processes every matching
    file in a folder, so `file` only narrows what the *preview sample*
    came from, never what the real conversion would be told to process.
    For csv/json, the real reader only ever takes one file, so `file` (when
    the inspected path is a folder) is appended to pick which one.

    --map, --require and --exclude-metadata are sorted, because
    run_identity.json stores them as lists: the same choices clicked in a
    different order must still count as the same run.
    """
    if input_type in ("csv", "json") and file:
        target_path = f"{input_path.rstrip('/')}/{file}"
    else:
        target_path = input_path

    args = [
        "--input-path",
        target_path,
        "--input-type",
        input_type,
        "--output-dir",
        output_dir,
    ]
    for target in sorted(mapping):
        args += ["--map", f"{target}={mapping[target]}"]
    for column in sorted(required):
        args += ["--require", column]
    for column in sorted(exclude_metadata):
        args += ["--exclude-metadata", column]
    if input_type == "huggingface":
        args += ["--split", split]
        if hf_config:
            args += ["--hf-config", hf_config]
    return args


def build_command(
    *,
    input_path: str,
    input_type: str,
    file: str | None,
    mapping: dict[str, str],
    required: list[str],
    exclude_metadata: list[str],
    output_dir: str = "./converted",
    split: str = "train",
    hf_config: str = "",
) -> str:
    """The prepare_rows.py command line (run from the repo root) that matches
    the mapping being previewed -- `prepare_args()` shown as a command."""
    args = prepare_args(
        input_path=input_path,
        input_type=input_type,
        file=file,
        output_dir=output_dir,
        mapping=mapping,
        required=required,
        exclude_metadata=exclude_metadata,
        split=split,
        hf_config=hf_config,
    )
    return f"{READERS_PYTHON} {PREPARE_ROWS_SCRIPT} {shlex.join(args)}"
