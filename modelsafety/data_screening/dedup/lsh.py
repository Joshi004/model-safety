"""The MinHash/LSH arithmetic that doesn't need datasketch, so the validator service can use it
without importing the scan.
"""

from __future__ import annotations

import math


def collision_probability_midpoint(bands: int, rows: int) -> float:
    """The similarity at which a pair of rows has a 50% chance of sharing a band, for an LSH
    of `bands` bands of `rows` rows each. Rows more similar than this are flagged more often
    than not."""
    return (1.0 - math.pow(0.5, 1.0 / bands)) ** (1.0 / rows)
