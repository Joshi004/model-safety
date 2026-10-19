"""Paths saved in records, kept relative to the project root.

A path to something inside this project (the service's datasets, scans and results) is
saved relative to the project root, so a record written here still works after the project
is moved or cloned somewhere else. A path to anything outside it (the original data a
dataset points at, say) is saved as the absolute path it is.

``to_saved`` turns a path into what a record should hold; ``from_saved`` turns what a
record holds back into a path that can be opened.

Standard library only, so shell launchers can import it with the system ``python3``.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def to_saved(path: str | os.PathLike[str]) -> str:
    """What a record should hold for ``path``.

    An absolute path inside the project root becomes relative to it (``output/service/...``);
    any other absolute path is kept as it is. A path that is not absolute is taken to be in
    saved form already and returned unchanged, so calling this twice is harmless.
    """
    text = os.fspath(path)
    if not os.path.isabs(text):
        return text
    normal = Path(os.path.normpath(text))
    try:
        return normal.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(normal)


def from_saved(text: str | os.PathLike[str]) -> Path:
    """The path a saved value stands for: a relative one is under the project root, an
    absolute one (a record written before paths were saved relative, or a path outside
    the project) is used as it is."""
    path = Path(os.fspath(text)).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
