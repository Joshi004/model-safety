"""Bitcoin-address validation rules."""

import hashlib


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_VALUES = {character: index for index, character in enumerate(BASE58_ALPHABET)}
BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_VALUES = {character: index for index, character in enumerate(BECH32_ALPHABET)}
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split('|', 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def valid_base58check(address):
    """Validate legacy mainnet or testnet Base58Check addresses."""
    if not (26 <= len(address) <= 35):
        return False
    number = 0
    try:
        for character in address:
            number = number * 58 + BASE58_VALUES[character]
    except KeyError:
        return False

    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    decoded = b"\x00" * (len(address) - len(address.lstrip("1"))) + decoded
    if len(decoded) != 25 or decoded[0] not in {0, 5, 111, 196}:
        return False
    payload, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return checksum == expected


def bech32_polymod(values):
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def bech32_hrp_expand(hrp):
    return [ord(character) >> 5 for character in hrp] + [0] + [
        ord(character) & 31 for character in hrp
    ]


def convert_bits(values, from_bits, to_bits):
    accumulator = 0
    bits = 0
    result = []
    max_value = (1 << to_bits) - 1
    for value in values:
        if value < 0 or value >> from_bits:
            return None
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None
    return result


def valid_bech32(address):
    """Validate SegWit Bech32 and Bech32m addresses."""
    if not (14 <= len(address) <= 90):
        return False
    if address.lower() != address and address.upper() != address:
        return False
    normalized = address.lower()
    separator = normalized.rfind("1")
    if separator < 1 or separator + 7 > len(normalized):
        return False

    hrp = normalized[:separator]
    if hrp not in {"bc", "tb", "bcrt"}:
        return False
    try:
        data = [BECH32_VALUES[character] for character in normalized[separator + 1 :]]
    except KeyError:
        return False

    checksum_type = bech32_polymod(bech32_hrp_expand(hrp) + data)
    if checksum_type not in {BECH32_CONST, BECH32M_CONST}:
        return False
    payload = data[:-6]
    if not payload or payload[0] > 16:
        return False
    program = convert_bits(payload[1:], 5, 8)
    if program is None or not (2 <= len(program) <= 40):
        return False
    if payload[0] == 0:
        return checksum_type == BECH32_CONST and len(program) in {20, 32}
    return checksum_type == BECH32M_CONST


def validate_bitcoin(btc):
    """Keep Bitcoin addresses whose encoded checksums are valid."""
    address = extract_pii_value(btc).strip(" \t\r\n`'\"()[]{}<>,;")
    if address.lower().startswith(("bc1", "tb1", "bcrt1")):
        return valid_bech32(address)
    return valid_base58check(address)
