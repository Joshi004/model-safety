"""Credit-card validation rules."""

import re


KNOWN_TEST_NUMBERS = {
    "378282246310005",
    "4012888888881881",
    "4111111111111111",
    "4222222222222",
    "5555555555554444",
    "6011111111111117",
}


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split('|', 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def passes_luhn(number):
    """Return whether a digit string has a valid Luhn checksum."""
    total = 0
    parity = len(number) % 2
    for index, character in enumerate(number):
        digit = int(character)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def validate_credit_card(cc):
    """Keep card-shaped values with a valid checksum."""
    value = extract_pii_value(cc).strip(" \t\r\n`'\"()[]{}")
    if not re.fullmatch(r"[\d -]+", value):
        return False
    number = re.sub(r"\D", "", value)

    if not (13 <= len(number) <= 19):
        return False
    if number in KNOWN_TEST_NUMBERS:
        return False
    if len(set(number)) <= 2:
        return False
    return passes_luhn(number)
