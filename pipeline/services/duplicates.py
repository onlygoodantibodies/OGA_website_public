"""
Duplicate-antibody detection: the shared *signals* used to decide that two
`pipeline.Antibody` rows are the same physical product listed twice.

Why this exists
---------------
The live dataset was assembled from three sources (legacy core SQLite, the
Access DB, the Leicester Excel) and is still added to by hand and by bulk
upload. The same product can therefore land twice under slightly different
spellings — a different RRID for the same catalogue number, "Mouse" vs "mouse",
an rndsystems.com link vs a bio-techne.com one. When both rows carry publication
images, BOTH show on the public gene page and the antibody looks like two
different reagents.

Two commands share these keys, so a group reported by one is the same group the
other acts on:
  * `find_duplicate_antibodies`  — read-only report (what is duplicated?)
  * `merge_duplicate_antibodies` — the merge (dry-run by default)

Normalisation
-------------
Catalogue numbers and clone IDs are compared case- and punctuation-insensitively
("MAB3418" == "mab-3418"), because that variation is a data-entry artefact and
never a real product difference. RRIDs go through `rrid_utils.normalize_rrid`,
which also treats junk placeholders ("?", "n/a") as missing.
"""

import os
import re

from pipeline.rrid_utils import normalize_rrid

# Catalogue/clone values that carry no information — never group on them.
JUNK_TEXT = {"", "-", "?", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "TBD"}

# Child relations holding real data hanging off an antibody row. A merge
# re-points these onto the survivor rather than losing them, and their count is
# how we decide which row of a duplicate group is the richer one.
CHILD_RELATIONS = [
    "wb_results",
    "ip_results",
    "if_results",
    "fc_results",
    "publication_images",
    "locations",
]


def payload_count(ab):
    """How many child data records this antibody row owns."""
    return sum(getattr(ab, rel).count() for rel in CHILD_RELATIONS)


# The OGA per-application recommendation flags. A row with none of them set has
# no published verdict of its own.
RECOMMENDATION_FIELDS = [
    ("WB", "wb_recommended"),
    ("IP", "ip_recommended"),
    ("ICC-IF", "if_recommended"),
    ("FC", "fc_recommended"),
]


def recommendations(ab):
    """The applications this antibody is recommended for, e.g. ['WB']."""
    return [name for name, field in RECOMMENDATION_FIELDS if getattr(ab, field)]


def image_files(ab):
    """
    The publication image FILES this row points at, by basename.

    Two antibody rows naming the same file are backed by the same figure in
    object storage — the strongest evidence that they are one antibody entered
    twice, because a genuinely separate reagent would have its own blot.
    Basename, not full path, because the same file re-uploaded lands under a
    different `upload_to` year directory.
    """
    return {os.path.basename(im.image.name or "")
            for im in ab.publication_images.all()} - {""}


def group_by_shared_image(rows):
    """
    Cluster rows that reference at least one publication image file in common.

    Returns a list of row-lists (>1 row each), ordered deterministically. Uses
    transitive grouping: if A shares its WB image with B and B shares its IP
    image with C, all three land in one cluster — they are the same antibody.
    """
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_file = {}
    for ab in rows:
        for name in image_files(ab):
            by_file.setdefault(name, []).append(ab.id)
    for ids in by_file.values():
        for other in ids[1:]:
            union(ids[0], other)

    clusters = {}
    for ab in rows:
        if ab.id in parent:
            clusters.setdefault(find(ab.id), []).append(ab)
    return [sorted(v, key=lambda r: r.id)
            for k, v in sorted(clusters.items()) if len(v) > 1]


def canonical_code(value):
    """
    Normalised form of a catalogue number / clone ID for comparison, or None
    when the value is blank or a placeholder. Case- and punctuation-insensitive:
    'MAB-3418 ' and 'mab3418' both give 'mab3418'.
    """
    s = (value or "").strip()
    if s.upper() in JUNK_TEXT:
        return None
    key = re.sub(r"[^a-z0-9]", "", s.lower())
    return key or None


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
# Each signal maps a row to a grouping key, or to None to leave the row out of
# that signal entirely. Ordered strongest-evidence first.

def _key_rrid(ab):
    """Same RRID => the registry says it is the same product."""
    return normalize_rrid(ab.rrid)


def _key_catalogue(ab):
    """
    Same gene + supplier + catalogue number, ignoring lot. This is the signal
    that catches a product listed twice under two RRIDs. Different lots of one
    catalogue number are the *same product* for the public site's purposes, so
    lot is deliberately not part of the key.
    """
    cat = canonical_code(ab.catalogue_number)
    if cat is None or ab.company_id is None:
        return None
    return (ab.target_id, ab.company_id, cat)


def _key_clone(ab):
    """
    Same gene + supplier + clone ID. Catches a clone re-listed under a second
    catalogue number (a supplier rebrand, e.g. R&D -> Bio-Techne). Polyclonals
    have no clone ID and are excluded.
    """
    clone = canonical_code(ab.clone_id)
    if clone is None or ab.company_id is None:
        return None
    return (ab.target_id, ab.company_id, clone)


SIGNALS = {
    "rrid": _key_rrid,
    "catalogue": _key_catalogue,
    "clone": _key_clone,
}

SIGNAL_LABELS = {
    "rrid": "same RRID",
    "catalogue": "same gene + supplier + catalogue number",
    "clone": "same gene + supplier + clone ID",
}


def group_by_signal(rows, signal):
    """
    Bucket `rows` by one signal and return only the buckets holding more than
    one row, as a list of (key, rows) ordered deterministically so a report and
    a later merge agree run to run.
    """
    keyfunc = SIGNALS[signal]
    buckets = {}
    for ab in rows:
        key = keyfunc(ab)
        if key is None:
            continue
        buckets.setdefault(key, []).append(ab)
    return [(k, sorted(buckets[k], key=lambda r: r.id))
            for k in sorted(buckets, key=str) if len(buckets[k]) > 1]


def find_duplicate_groups(rows, signals=None):
    """
    Run every signal over `rows` and return one entry per distinct duplicate
    group, de-duplicated across signals so a pair caught by both `rrid` and
    `catalogue` is reported once, listing every signal that caught it.

    Returns a list of dicts: {"rows": [...], "signals": [...], "keys": {...}}.
    """
    signals = list(signals or SIGNALS)
    by_rowset = {}
    for signal in signals:
        for key, group in group_by_signal(rows, signal):
            ident = tuple(r.id for r in group)
            entry = by_rowset.setdefault(
                ident, {"rows": group, "signals": [], "keys": {}}
            )
            entry["signals"].append(signal)
            entry["keys"][signal] = key
    # Largest groups first, then by lowest row id — stable and reads well.
    return sorted(by_rowset.values(), key=lambda e: (-len(e["rows"]), e["rows"][0].id))
