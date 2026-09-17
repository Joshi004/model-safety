"""IPv6 validation rules."""

import ipaddress

from .ip import DOCUMENTATION_NETWORKS, extract_pii_value


def validate_ipv6(value):
    """Keep valid unicast IPv6 addresses, including private addresses."""
    candidate = extract_pii_value(value).strip(" \t\r\n`'\"()[]{}<>,;")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.version != 6:
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
