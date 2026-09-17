"""Street-address validation rules."""

import re


STREET_SUFFIXES = {
    'avenue', 'ave',
    'boulevard', 'blvd',
    'circle', 'cir',
    'court',
    'drive', 'dr',
    'highway', 'hwy',
    'lane', 'ln',
    'parkway', 'pkwy',
    'place', 'pl',
    'road', 'rd',
    'street', 'st',
    'terrace', 'ter',
    'way',
}

STREET_DIRECTIONS = {
    'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw',
    'north', 'south', 'east', 'west',
}

STREET_CONTEXT_FALSE_POSITIVES = {
    'a', 'act', 'affect', 'against', 'agonist', 'alone', 'and',
    'actively', 'along', 'antagonist', 'antibodies', 'apcs', 'arrest',
    'as', 'at', 'before', 'beyond',
    'are', 'billion', 'binds', 'blood', 'boost', 'breast', 'by', 'can',
    'cannot', 'carriers', 'cells', 'channels', 'cm', 'collectively',
    'compared', 'complexes', 'constitutional', 'continuous', 'copies',
    'cost', 'could', 'currently', 'cytokines', 'days', 'db', 'dcs',
    'defect', 'described', 'develops', 'diameter', 'did', 'direct',
    'directly', 'divided', 'dose',
    'does', 'drives', 'driving', 'early', 'effect', 'european',
    'expression', 'federal', 'first', 'for', 'fr', 'from', 'further',
    'gave', 'gene', 'global', 'grams', 'has', 'have', 'help', 'helps',
    'high', 'highest', 'hour', 'hours', 'immune', 'in', 'include',
    'included', 'includes', 'independently', 'induction', 'inhibitors',
    'interacts', 'involved', 'is', 'kb', 'kcal', 'kg', 'kda', 'kev',
    'last', 'lb', 'least', 'led', 'levels', 'ligands', 'like', 'lists',
    'liters', 'macrophages', 'may', 'mg', 'microglia', 'might', 'miles',
    'million', 'minute', 'minutes', 'mins', 'mm', 'months', 'most',
    'msv',
    'must', 'mutants', 'mutations', 'not', 'of', 'often', 'on',
    'oncogenes', 'or', 'outperforms', 'patients', 'pathway', 'per',
    'perfect', 'phase', 'plant',
    'primarily', 'proliferative', 'protein', 'punnett', 'receptor',
    'remains', 'represents', 'rest', 'risk', 'should', 'show',
    'release', 'responses', 'showing', 'shows', 'specifically',
    'standard', 'state', 'step',
    'strains', 'studies', 'study', 'suggest', 'supreme',
    'students', 'synergistically', 'team', 'test', 'tfh', 'than',
    'that', 'the', 'then', 'times', 'to', 'tonnes', 'toward', 'trial',
    'trials', 'under', 'unit', 'uptake', 'us', 'vaccinated',
    'vaccination', 'variants', 'version', 'via', 'was', 'weeks', 'what',
    'when', 'where', 'which', 'who', 'winner', 'with', 'without',
    'words', 'workers', 'would', 'years',
}

STREET_CONTEXT_FALSE_POSITIVES.update({
    'adjuvants', 'after', 'also', 'amplifications', 'astrocytes',
    'booster', 'bsg', 'carries', 'causes', 'clearly', 'confirms',
    'data', 'diabetes', 'effectors', 'emissions', 'emt', 'eu', 'feet',
    'fusions', 'g', 'gb', 'gradients', 'group', 'had', 'hard',
    'heterodimers', 'higher', 'i', 'indicates', 'interactions',
    'itself', 'less', 'loss', 'lymphocytes', 'mainly', 'mechanical',
    'mentioned', 'more', 'mv', 'my', 'nanoclusters',
    'oncogenic', 'oscillations', 'pathways', 'produces', 'reduced',
    'rolling', 'saw', 'signaling', 'status', 'subsets', 'supporting',
    'their', 'tregs', 'usb', 'use', 'usually',
})

STREET_PLACEHOLDER_NAMES = {
    ('mount',),
    ('wall',),
    ('main',),
    ('oak',),
    ('resource', 'allocation'),
    ('resource', 'types'),
    ('science', 'park'),
    ('tech', 'park'),
    ('virtual', 'test'),
}

STREET_ADDRESS_RE = re.compile(
    r"""
    ^
    (?P<number>\d{1,6}[a-z]?)
    \s+
    (?P<name>[a-z][a-z0-9.'-]*(?:\s+[a-z][a-z0-9.'-]*){0,5})
    \s+
    (?P<suffix>avenue|ave|boulevard|blvd|circle|cir|court|drive|dr|highway|hwy|
        lane|ln|parkway|pkwy|place|pl|road|rd|street|st|terrace|ter|way)
    \.?
    (?:
        \s*,?
        \s*(?:apt|apartment|suite|ste|unit|\#)\s*[a-z0-9-]+
    )?
    \s*[`'")\],.;:]*\s*
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_pii_value(entry):
    """Accept either a full scanner output line or a raw PII value."""
    parts = entry.split('|', 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return entry.strip()


def validate_street_address(address):
    """Keep only entries shaped like a real street address."""
    raw_addr_text = extract_pii_value(address).strip(" \t\r\n`'\"()[]{}")
    addr_text = raw_addr_text.lower()

    if not addr_text:
        return False

    match = STREET_ADDRESS_RE.match(raw_addr_text)
    if not match:
        return False

    house_number = re.sub(r'\D', '', match.group('number'))
    if not house_number or set(house_number) == {'0'}:
        return False

    suffix = match.group('suffix').rstrip('.').lower()
    if suffix not in STREET_SUFFIXES:
        return False

    if suffix == 'court' and 1900 <= int(house_number) <= 2029:
        return False

    name_tokens = [
        token.strip(".,'\"`")
        for token in match.group('name').lower().split()
        if token.strip(".,'\"`")
    ]
    non_direction_tokens = [
        token for token in name_tokens
        if token not in STREET_DIRECTIONS
    ]

    if not non_direction_tokens and len(name_tokens) < 2:
        return False

    if any(token in STREET_CONTEXT_FALSE_POSITIVES for token in name_tokens):
        return False

    raw_name_tokens = [
        token.strip(".,'\"`")
        for token in match.group('name').split()
        if token.strip(".,'\"`")
    ]
    has_proper_name_token = any(
        raw_token[0].isupper()
        for raw_token in raw_name_tokens
        if raw_token.lower() not in STREET_DIRECTIONS
    )
    has_directional_name = (
        len(raw_name_tokens) >= 2
        and all(token.lower() in STREET_DIRECTIONS for token in name_tokens)
        and any(raw_token[0].isupper() for raw_token in raw_name_tokens)
    )
    if not has_proper_name_token and not has_directional_name:
        return False

    if tuple(non_direction_tokens) in STREET_PLACEHOLDER_NAMES:
        return False

    return True
