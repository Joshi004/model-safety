"""Phone-number validation rules."""

import re


NANP_TOLL_FREE_AREA_CODES = {
    '800', '833', '844', '855', '866', '877', '888',
}

NANP_PLACEHOLDER_AREA_CODES = {
    '300', '333',
}

NANP_PLACEHOLDER_LAST_FOUR = {
    '1234', '5678',
}

KNOWN_SERVICE_NUMBERS = {
    '18002221222',  # Poison Control
    '8002221222',
    '18002738255',  # Suicide & Crisis Lifeline legacy number
    '8002738255',
    '18006624357',  # SAMHSA
    '8006624357',
    '18004887386',  # Trevor Project
    '8004887386',
    '18006564673',  # RAINN
    '8006564673',
    '18884264435',  # ASPCA Animal Poison Control
    '8884264435',
    '18557647661',
    '8557647661',
    '18002272345',  # American Cancer Society
    '8002272345',
    '18006771116',  # Eldercare Locator
    '8006771116',
    '18883737888',  # Human trafficking hotline
    '8883737888',
    '18005224700',  # Gambling helpline
    '8005224700',
    '18004483000',
    '8004483000',
}

TEST_PATTERNS = {
    '0000000000',
    '1111111111',
    '2222222222',
    '3333333333',
    '4444444444',
    '5555555555',
    '6666666666',
    '7777777777',
    '8888888888',
    '9999999999',
    '0123456789',
    '1234567890',
    '9876543210',
}


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split('|', 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def normalize_digits(phone_text):
    return re.sub(r'\D', '', phone_text)


def is_numeric_range(phone_text):
    """Reject year/range-like fragments such as 2014-2016 or 500-1000."""
    return bool(re.fullmatch(r'\d{2,5}\s*[-–]\s*\d{2,5}', phone_text))


def is_date_or_identifier_fragment(phone_text):
    if re.fullmatch(r'(?:19|20)\d{4}[-–]\d{4}', phone_text):
        return True
    if re.fullmatch(r'1\.\d{3}[-.]\d{3}[-.]\d{4}', phone_text):
        return True
    return False


def is_decimal_or_math_fragment(phone_text):
    if re.fullmatch(r'[+-]?\d+\.\d+', phone_text):
        return True
    if any(operator in phone_text for operator in ('*', '/', '=')):
        return True
    return False


def has_phone_formatting(phone_text):
    return bool(re.search(r'[()+\-\s]', phone_text))


def has_sequential_digits(digits):
    for sequence in ('0123456789', '1234567890', '9876543210'):
        if sequence in digits:
            return True
    return False


def validate_nanp_phone(digits):
    """Validate North American Numbering Plan numbers."""
    if len(digits) == 11 and digits.startswith('1'):
        national_digits = digits[1:]
    elif len(digits) == 10:
        national_digits = digits
    else:
        return False

    area_code = national_digits[:3]
    exchange = national_digits[3:6]

    if area_code[0] in '01' or exchange[0] in '01':
        return False
    if area_code in NANP_TOLL_FREE_AREA_CODES or area_code in NANP_PLACEHOLDER_AREA_CODES:
        return False
    if area_code == '555' or exchange == '555' or national_digits in TEST_PATTERNS:
        return False
    if national_digits[-4:] in NANP_PLACEHOLDER_LAST_FOUR:
        return False

    return True


def validate_international_phone(phone_text, digits):
    """Validate plus-prefixed international-looking numbers."""
    if not phone_text.startswith('+'):
        return False
    if not (8 <= len(digits) <= 15):
        return False
    if digits.startswith('0'):
        return False
    if len(set(digits)) <= 3:
        return False
    return True


def validate_phone(phone):
    """Keep only phone-shaped values while filtering obvious numeric noise."""
    phone_text = extract_pii_value(phone).strip(" \t\r\n`'\"()[]{}")
    if not phone_text:
        return False

    digits = normalize_digits(phone_text)
    if not (7 <= len(digits) <= 15):
        return False

    if (
        is_numeric_range(phone_text)
        or is_date_or_identifier_fragment(phone_text)
        or is_decimal_or_math_fragment(phone_text)
    ):
        return False

    if digits in TEST_PATTERNS or has_sequential_digits(digits):
        return False

    if digits in KNOWN_SERVICE_NUMBERS:
        return False

    # Raw digit blobs are mostly IDs/counts in the samples; require either
    # separators/parentheses or an explicit international '+' prefix.
    if not has_phone_formatting(phone_text):
        return False

    if phone_text.startswith('+') and len(digits) == 11 and digits.startswith('1'):
        return validate_nanp_phone(digits)

    if validate_international_phone(phone_text, digits):
        return True

    if len(digits) == 11 and digits.startswith('1') and not re.match(r'^1[-.\s(]', phone_text):
        return False

    return validate_nanp_phone(digits)
