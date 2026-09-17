"""Email validation rules."""

import re


EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
RESERVED_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "localhost",
}


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split("|", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def validate_email(email):
    """Keep syntactically plausible, non-reserved email addresses."""
    value = extract_pii_value(email).strip(" \t\r\n<>()[]{}*`'\",;:")
    if len(value) > 254 or value.count("@") != 1:
        return False

    local, domain = value.rsplit("@", 1)
    if not local or len(local) > 64 or not EMAIL_LOCAL_RE.fullmatch(local):
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False

    domain = domain.rstrip(".").lower()
    if not domain or len(domain) > 253:
        return False
    if (
        domain in RESERVED_DOMAINS
        or domain.endswith(".example")
        or domain.endswith(".invalid")
        or domain.endswith(".test")
        or domain.endswith(".localhost")
    ):
        return False

    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_domain.split(".")
    return all(DOMAIN_LABEL_RE.fullmatch(label) for label in labels)
