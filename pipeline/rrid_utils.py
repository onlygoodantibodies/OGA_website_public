"""
Shared helpers for parsing and canonicalising RRIDs (Research Resource IDs).

The canonical stored form of an antibody RRID is the BARE identifier
`AB_<digits>` (matching the `Antibody.rrid` field's documented intent,
e.g. "AB_10677043"). The full antibodyregistry.org URL belongs in
`Antibody.rrid_link`.

Historically the `rrid` column drifted to holding full URLs and junk
placeholders ("?"). These helpers are the single source of truth used by:
  * pipeline/management/commands/standardize_rrid_format.py  (fixes the data)
  * pipeline/management/commands/merge_duplicate_antibodies.py  (groups by RRID)
  * core/views.py  (embed lookup accepts either form, backward-compatible)
"""

import re

# RRID values that are placeholders for "unknown", not real identifiers.
JUNK_RRID = {"", "?", "-", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "TBD"}

_AB_RE = re.compile(r"AB_\d+", re.IGNORECASE)

REGISTRY_URL_TEMPLATE = "https://www.antibodyregistry.org/{}"


def normalize_rrid(raw):
    """
    Canonicalise an RRID to its bare 'AB_<digits>' form, or return None if the
    value is missing/junk/unrecognisable. Accepts bare ids, full registry URLs,
    and mixed case; returns None for '?', blanks, and non-AB strings.
    """
    s = (raw or "").strip()
    if s.upper() in JUNK_RRID:
        return None
    m = _AB_RE.search(s)
    return m.group(0).upper() if m else None


def registry_url(bare):
    """Canonical antibodyregistry URL for a bare AB_<n> id."""
    return REGISTRY_URL_TEMPLATE.format(bare)


def rrid_match_candidates(raw):
    """
    Given an incoming ?rrid= parameter (bare OR full URL), return the list of
    stored-value forms it could match, so a lookup works whether the database
    stores the bare id (new, standardised) or the full URL (legacy). Always
    includes the original string for an exact fallback.
    """
    candidates = [raw]
    bare = normalize_rrid(raw)
    if bare:
        candidates += [bare, registry_url(bare)]
    # De-duplicate while preserving order.
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out
