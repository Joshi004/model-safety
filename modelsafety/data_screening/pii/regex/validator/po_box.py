"""Post-office box validation rules."""

import re


PO_BOX_RE = re.compile(
    r"^(?:P(?:OST)?\.?\s*O(?:FFICE)?\.?\s+BOX|POB)\s*#?\s*[A-Z0-9][A-Z0-9-]{0,11}$",
    re.IGNORECASE,
)


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split("|", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def validate_po_box(value):
    """Keep syntactically plausible PO box identifiers."""
    candidate = extract_pii_value(value).strip(" \t\r\n`'\"()[]{}<>,;")
    candidate = re.sub(r"\s+", " ", candidate)
    return bool(PO_BOX_RE.fullmatch(candidate))
