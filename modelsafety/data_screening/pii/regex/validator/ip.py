"""IP address validation rules."""

import ipaddress


DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split('|', 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def validate_ip(ip):
    """Keep valid unicast IP addresses, including private-network addresses."""
    value = extract_pii_value(ip).strip(" \t\r\n`'\"()[]{}<>,;")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    if any(address in network for network in DOCUMENTATION_NETWORKS):
        return False
    return not (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    )
