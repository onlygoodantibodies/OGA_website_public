"""
Antibody-table paste parser (spec §4). Deterministic, NO AI.

Turns a pasted table (from F1000Research / the Zenodo report) into structured
per-antibody metadata rows. Layouts vary, so this is best-effort by design — the
result is ALWAYS shown in an editable grid for the human to correct before use.

Field identity is by pattern, not position (RRID = AB_\\d+, clonality keywords,
host list, known-vendor list, catalogue = the remaining short token), with a
header-mapping path for column pastes.

Clonality follows the strict rule (spec §4): map to the Antibody enum
(monoclonal/polyclonal/recombinant/unknown) + the separate is_recombinant bool —
never store the raw string, so the tool doesn't add to the existing clonality drift.
"""
from __future__ import annotations

import re

# Stdlib-only, like this module: `services/concentration.py` imports nothing
# from Django, so reading it here keeps the parser importable on its own.
from pipeline.services import concentration as concentration_svc

# Known vendors (spec §4). Matched as a substring of the line, longest first so
# "Bio-Techne" wins over a bare token. Kept lowercase for comparison.
KNOWN_VENDORS = [
    # NB: only add a bare abbreviation (e.g. "DSHB") when it can't collide with a
    # clone id. We deliberately do NOT add "IPI" as an alias — the IPI clone ids
    # are literally "IPI-Slit1.11" etc., so a bare "IPI" would false-match inside
    # every clone and split the rows. The full name below is unambiguous.
    "Institute for Protein Innovation",
    "Aviva Systems Biology", "Cell Signaling Technology", "Thermo Fisher Scientific",
    "Developmental Studies Hybridoma Bank", "R&D Systems", "Bio-Techne", "MilliporeSigma",
    "Sigma-Aldrich", "Synaptic Systems", "Novus Biologicals", "Proteintech", "GeneTex",
    "Invitrogen", "Abcam", "Abbexa", "Novus", "Bethyl", "Atlas Antibodies",
    "BD Biosciences", "BioLegend", "Santa Cruz", "DSHB", "Millipore", "Sigma", "Merck",
]

HOSTS = {"rabbit", "mouse", "rat", "goat", "sheep", "human", "chicken", "donkey",
         "guinea pig", "hamster", "llama"}

HEADER_ALIASES = {
    "gene": "gene", "gene name": "gene", "gene symbol": "gene", "target": "gene",
    "target gene": "gene", "symbol": "gene",
    "catalogue": "catalogue", "catalog": "catalogue", "catalogue number": "catalogue",
    "catalog number": "catalogue", "catalog no": "catalogue", "cat": "catalogue",
    "cat no": "catalogue", "cat. no.": "catalogue", "cat#": "catalogue", "product": "catalogue",
    "cat num": "catalogue", "cat number": "catalogue", "catalog num": "catalogue",
    "catalogue no": "catalogue", "product number": "catalogue",
    "company": "company", "supplier": "company", "vendor": "company", "manufacturer": "company",
    "source": "company",
    "concentration": "concentration", "conc": "concentration", "concentration ugml": "concentration",
    "concentration ugul": "concentration", "concentration mgml": "concentration",
    "rrid": "rrid", "rrid id": "rrid",
    # The lab's own A-number, as written on the freezer box. Leave the cell
    # empty and one is issued from the site's own run of numbers
    # (`services/lab_numbers.py`); fill it in and that is what the record gets.
    # A cell holding a bare record id — which is what a sheet downloaded before
    # A-numbers were issued again carries in this column — is left alone, since
    # only the `A` prefix says the value is a lab number.
    "ab": "ab_number", "ab#": "ab_number", "ab #": "ab_number",
    "ab number": "ab_number", "a number": "ab_number", "a#": "ab_number",
    "a #": "ab_number", "antibody number": "ab_number",
    # A note in a person's own words, at the moment the rows are created. The
    # field is editable on the board but was not in the grid, so tagging a batch
    # meant a second pass, cell by cell — twenty-four of them on the sixth run.
    "comments": "comments", "comment": "comments", "notes": "comments",
    "note": "comments",
    "clonality": "clonality_raw", "clone type": "clonality_raw", "type": "clonality_raw",
    # **In kind or purchased.** `Antibody.acquisition_method` has held this
    # since the Access import and is filled on 3,261 of 3,261 rows — 3,034 in
    # kind, 146 purchased, 81 unknown — and was drawn on no screen and in no
    # sheet, so uOttawa asked for it believing the portal had never recorded it
    # (4 Sep 2026). Same dark shape as the freezer box and the received date.
    "acquisition": "acquisition_raw", "acquisition method": "acquisition_raw",
    "in kind": "acquisition_raw", "in kind or purchased": "acquisition_raw",
    "purchased or in kind": "acquisition_raw", "how acquired": "acquisition_raw",
    "host": "host", "host species": "host", "raised in": "host",
    "clone": "clone_id", "clone id": "clone_id", "clone number": "clone_id", "clone #": "clone_id",
    "lot": "lot", "lot number": "lot", "lot no": "lot", "lot #": "lot", "lot number ": "lot",
    # Which bench's vial this is. Half of what makes a row a vial rather than a
    # product, so a downloaded sheet carries it and an upload honours it instead
    # of stamping every row with whoever uploaded the file.
    "site": "site", "lab": "site", "institution": "site", "centre": "site",
    "center": "site", "node": "site",
    "link": "supplier_url", "product link": "supplier_url", "url": "supplier_url",
    "product url": "supplier_url", "website": "supplier_url",
    "discontinued": "discontinued", "out of market": "discontinued",
    # **What the supplier says the antibody is for.** The column is
    # `supplier recommendations` on every sheet now, because it was called three
    # different things and one of them could not be read back: the blank template
    # said `applications`, the board's download said `supplier claims`, and the
    # whole-dataset sheet said `applications` again — for one field, next to a
    # column headed `OGA recommends`, which is the *other* verdict and the one a
    # reader must not confuse it with.
    #
    # `supplier claims` was the harmful half. `board_columns` declared it
    # writable, so it was not listed among the read-only headers and no preview
    # named it as ignored — but no alias here matched it, so a scientist who
    # downloaded the antibodies sheet, corrected what the supplier recommends and
    # uploaded it back had the change dropped in silence.
    #
    # Every older spelling stays, because sheets already downloaded are still on
    # people's disks and a rename must not turn them into silent no-ops.
    "supplier recommendations": "apps_raw", "supplier recommendation": "apps_raw",
    "supplier recommends": "apps_raw", "supplier claims": "apps_raw",
    "applications": "apps_raw", "application": "apps_raw",
    "recommended applications": "apps_raw", "vendors recommended applications": "apps_raw",
    "validated applications": "apps_raw", "supplier applications": "apps_raw",
    # **Where the vial is, and when it turned up.** Both facts have been in the
    # database all along — 3,058 antibodies carry an `InventoryLocation` with a
    # box and 2,841 a `received_date` — and neither was in any sheet, so the
    # first bench to keep its own spreadsheet wrote columns for them and had
    # them dropped in silence. `services/storage.py` and `services/received.py`
    # are the readers.
    #
    # Two more of the same shape: `isotype` on 2,823
    # antibodies and `species_reactivity` on 2,798. **Reactivity is a supplier
    # claim**, like `supplier recommendations` beside it — what the datasheet
    # says the antibody cross-reacts with, not something OGA tested — so the
    # spellings people write it under are accepted and the sheet's own heading
    # says whose claim it is.
    "isotype": "isotype", "ig isotype": "isotype", "immunoglobulin": "isotype",
    "reactivity": "species_reactivity",
    "species reactivity": "species_reactivity",
    "reactivity supplier": "species_reactivity",
    "species": "species_reactivity", "cross reactivity": "species_reactivity",
    "storage": "storage_type", "storage type": "storage_type",
    "storage temp": "storage_type", "storage temperature": "storage_type",
    "temperature": "storage_type", "temp": "storage_type",
    "box": "box", "box number": "box", "freezer box": "box",
    "freezer": "freezer", "position": "position", "pos": "position",
    "slot": "position",
    "received": "received", "received date": "received",
    "date received": "received", "arrived": "received",
    "arrival date": "received", "date of receipt": "received",
}

# ── reading a heading ────────────────────────────────────────────────────────
#
# `_norm_header` keeps `#` and `.`, so `Catalogue #` normalises to
# `catalogue #` — which was in no alias above, while `cat#` and `catalogue
# number` both were. That one gap is the whole reason uOttawa's first
# spreadsheet uploaded as **0 rows out of 44**: `parse_table` drops every row
# with no catalogue, and the catalogue column was there, spelled with a `#`.
#
# Two normalisers is the root of it, and they disagreed in exactly one way:
# `services/workbook.py::norm` strips a trailing ` .:#` and this one keeps them.
# So the sheet-picker recognised `Catalogue #` as the catalogue column, chose the
# right tab on the strength of it, and handed the rows to a parser that then did
# not know what that heading was. Everything upstream said the file was fine.
#
# Adding `catalogue #` to the map would fix that sheet and not the next one.
# Punctuation is decoration on a heading — `Lot #`, `Clone #`, `Conc.`,
# `Cat.No.` all mean what their unpunctuated spelling means — so the lookup
# retries with `#` and `.` removed. It can only ever widen matching to headings
# that already differ from a known alias by punctuation alone, and every alias
# that *needs* its `#` (`ab #`, `a #`) has an unpunctuated twin above that means
# the same field, so nothing changes meaning on the way through.


def _depunctuate(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("#", " ").replace(".", " ")).strip()


def header_field(h: str) -> str:
    """Which parsed field a column heading names, or ``""``.

    The one reader for that question — `_looks_like_header` and `_row_by_header`
    both ask it, so a heading cannot count towards "this line is a header" and
    then be ignored when the row under it is read.
    """
    norm = _norm_header(h)
    return HEADER_ALIASES.get(norm) or HEADER_ALIASES.get(_depunctuate(norm)) or ""


def heading_unit(h: str) -> str:
    """The concentration unit a heading names, normalised, or ``""``.

    `Concentration (mg/mL)` is a heading that says what its cells mean, and the
    alias map above deliberately flattens all three unit spellings onto one
    field — so the unit was read as part of the *name* and then thrown away,
    and the bare numbers underneath were stored as µg/mL. A sheet of 42 stock
    concentrations, every one a thousand times too low, and nothing on any
    screen to catch it by: the exact failure `services/concentration.py` was
    written to prevent, arriving through the heading instead of the cell.

    A unit in the **cell** still wins — it is the more specific statement, and
    it is what a mixed sheet needs.
    """
    text = (h or "")
    inner = re.search(r"[\(\[]([^)\]]+)[\)\]]", text)
    candidates = [inner.group(1)] if inner else []
    candidates.append(re.sub(r"(?i)^\s*conc(entration)?\b", "", text))
    for raw in candidates:
        unit = concentration_svc.known_unit(raw)
        if unit:
            return unit
    return ""


def _apps_from(text: str):
    """Application codes present in a free-text applications cell, e.g.
    "Wb, IF" → ["WB", "IF"]. Abbreviations are matched as whole tokens so a clone
    id like "IPI-Slit1" never reads as "IP".

    **IHC and ELISA are read here even though OGA does not test them.** This is
    what the *supplier* says the antibody is for, and a supplier's datasheet
    routinely claims more applications than the four OGA characterises. They
    were not in this list, so a field test typing ``WB, IHC`` got WB stored and
    IHC dropped without a word — and `Antibody.supplier_validated_ihc` has
    existed as a column since the Access import, so nothing was gained by
    losing it. The OGA verdict is still the four (`board_columns._APPS`); it is
    a different question and stays a different list.
    """
    low = (text or "").lower()
    toks = set(re.findall(r"[a-z]+", low))
    out = []
    if "wb" in toks or "western" in low:
        out.append("WB")
    if "ip" in toks or "immunoprecip" in low:
        out.append("IP")
    if "if" in toks or "icc" in toks or "immunofluor" in low:
        out.append("IF")
    if "fc" in toks or "flow" in low:
        out.append("FC")
    # Checked before `if`/`ip` cannot fire on them: `ihc` and `elisa` are their
    # own tokens, so no ordering trap here.
    if "ihc" in toks or "immunohisto" in low:
        out.append("IHC")
    if "elisa" in toks:
        out.append("ELISA")
    return out

# RRIDs are always AB_<digits> (mandatory underscore). Requiring the underscore
# stops Abcam catalogues like `ab318262` being mis-read as an RRID.
_RRID_RE = re.compile(r"AB_\d+", re.I)


#: What a person might write for each acquisition method, folded to the stored
#: code. The labels are here because a downloaded sheet shows what the *board*
#: shows to a reader who then edits it, and `In Kind` coming back as an
#: unrecognised value would be a round trip that loses the column it just
#: displayed.
_ACQUISITION = {
    "in kind": "in_kind", "inkind": "in_kind", "gift": "in_kind",
    "donated": "in_kind", "contributed": "in_kind",
    "purchased": "purchased", "purchase": "purchased", "bought": "purchased",
    "unknown": "unknown",
}


def parse_acquisition(raw: str) -> str:
    """The stored acquisition code for a typed value, or "" if it says nothing.

    `""` rather than `unknown`, because the two are different answers. A blank
    cell means *this sheet does not say*, and must leave the stored value
    alone; `unknown` is somebody recording that nobody knows. Collapsing them
    would let a sheet with the column left empty overwrite 3,034 rows.
    """
    key = re.sub(r"[\s_-]+", " ", (raw or "").strip().lower())
    return _ACQUISITION.get(key, "")


def parse_clonality(raw: str):
    """(clonality_enum, is_recombinant) from a pasted clonality string.
    Only ever returns one of the four enum values (spec §4)."""
    s = (raw or "").strip().lower()
    recombinant = s.startswith("recombinant")
    if "monoclonal" in s:
        return "monoclonal", recombinant
    if "polyclonal" in s:
        return "polyclonal", recombinant
    if "recombinant" in s:
        return "recombinant", True
    return "unknown", False


def _split(line: str):
    if "\t" in line:
        return [t.strip() for t in line.split("\t")]
    return [t.strip() for t in re.split(r"\s{2,}", line.strip())]


def _norm_header(h: str):
    return re.sub(r"[^a-z0-9 #.]", "", (h or "").lower()).strip()


def _looks_like_header(line: str) -> bool:
    toks = _split(line)
    if not toks:
        return False
    hits = sum(1 for t in toks if header_field(t))
    return hits >= max(2, len(toks) // 2)


def _clean_catalogue(tok: str) -> str:
    # strip trailing asterisks and surrounding punctuation/whitespace; keep the rest
    return tok.strip().strip("*").strip().strip(",;").strip()


def _find_vendor(line_low: str):
    for v in sorted(KNOWN_VENDORS, key=len, reverse=True):
        if v.lower() in line_low:
            return v
    return ""


def _row_by_pattern(toks, line):
    low = line.lower()
    row = {"gene": "", "catalogue": "", "company": "", "rrid": "", "clonality_raw": "",
           "host": "", "clone_id": "", "lot": "", "concentration": "",
           "comments": "", "supplier_apps": [],
           "supplier_url": "", "discontinued": False}
    row["company"] = _find_vendor(low)
    row["supplier_apps"] = _apps_from(low)
    for t in toks:
        m = _RRID_RE.search(t)
        if m:
            row["rrid"] = m.group(0)
            break
    if "monoclonal" in low:
        row["clonality_raw"] = ("recombinant monoclonal" if "recombinant" in low else "monoclonal")
    elif "polyclonal" in low:
        row["clonality_raw"] = ("recombinant polyclonal" if "recombinant" in low else "polyclonal")
    elif "recombinant" in low:
        row["clonality_raw"] = "recombinant"
    for t in toks:
        if t.lower() in HOSTS:
            row["host"] = t.lower()
            break
    for t in toks:
        if t.lower().startswith("http"):
            row["supplier_url"] = t
            break
    # catalogue = the remaining short token that has a digit and isn't the RRID
    skip = {row["rrid"], row["host"]}
    for t in toks:
        if t in skip or not t:
            continue
        if _RRID_RE.fullmatch(t):
            continue
        if t.lower() in ("monoclonal", "polyclonal", "recombinant"):
            continue
        if t.lower().startswith("http"):
            continue
        if re.search(r"\d", t) and len(t) <= 24:
            row["catalogue"] = _clean_catalogue(t)
            break
    return row


def _row_by_header(header, toks):
    row = {"gene": "", "catalogue": "", "company": "", "rrid": "", "clonality_raw": "",
           "host": "", "clone_id": "", "lot": "", "concentration": "",
           "comments": "", "supplier_apps": [],
           "supplier_url": "", "discontinued": False}
    for h, t in zip(header, toks):
        field = header_field(h)
        if not field or not t:
            continue
        if field == "concentration":
            # A heading that names a unit means its cells are in that unit —
            # unless a cell says otherwise, which is the more specific
            # statement and wins. Carried as text on the value so one parser
            # does the arithmetic and one preview can say what will be stored.
            unit = heading_unit(h)
            if unit and not concentration_svc.normalise_unit(
                    re.sub(r"^[-+]?[\d.]+(?:[eE][-+]?\d+)?", "", t.strip())):
                row["concentration"] = f"{t.strip()} {unit}"
                row["concentration_unit_from"] = "heading"
            else:
                row["concentration"] = t
        elif field == "rrid":
            m = _RRID_RE.search(t)
            row["rrid"] = m.group(0) if m else t
        elif field == "catalogue":
            row["catalogue"] = _clean_catalogue(t)
        elif field == "discontinued":
            row["discontinued"] = t.strip().lower() in ("yes", "true", "1", "discontinued", "y")
        elif field == "host":
            row["host"] = t.lower()
        elif field == "apps_raw":
            row["supplier_apps"] = _apps_from(t)
            # Kept beside the codes, not consumed by them. The cell is free
            # text and only recognised codes become flags, so the preview has
            # to be able to compare what was typed against what will be stored
            # — `WB, IHC` used to store WB and lose IHC without a word, and
            # nothing downstream could say so because the typed string was gone
            # by then. See `bulk_antibodies._with_supplier_apps_note`.
            row["apps_raw"] = t
        else:
            row[field] = t
    return row


def _flatten(text: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces and normalise
    en/em dashes — so a PDF paste whose cells wrap across lines becomes one
    stream we can anchor on."""
    t = (text or "").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip()


def _vendor_hits(flat: str):
    """Positions of known-vendor names in the flattened text, de-overlapped,
    in order — these delimit the start of each antibody row."""
    low = flat.lower()
    hits = []
    for v in sorted(KNOWN_VENDORS, key=len, reverse=True):
        vl, start = v.lower(), 0
        while True:
            i = low.find(vl, start)
            if i < 0:
                break
            hits.append((i, i + len(v), v))
            start = i + len(v)
    hits.sort()
    out = []
    for h in hits:
        # skip a hit that overlaps, or sits within a few chars of, the previous one
        # (e.g. the "DSHB" inside "…Hybridoma Bank (DSHB)" is the same vendor)
        if out and h[0] < out[-1][1] + 4:
            continue
        out.append(h)
    return out


_LOT_RE = re.compile(r"[\d][\d/.\-]{0,11}$")   # trailing lot / date token


def _clonality_from(text_low: str) -> str:
    recomb = "recombinant" in text_low
    if "monoclonal" in text_low or "mono" in text_low:
        return "recombinant monoclonal" if recomb else "monoclonal"
    if "polyclonal" in text_low or "poly" in text_low:
        return "recombinant polyclonal" if recomb else "polyclonal"
    return "recombinant" if recomb else ""


def _conc_after_host(ptoks, host):
    """The concentration decimal, taken from AFTER the host token so a clone id
    like "IPI-Slit1.11" (which sits before the host) is never read as it. Kept in
    the pasted format — a bare number, no unit conversion."""
    if host and host in ptoks:
        for t in ptoks[ptoks.index(host) + 1:]:
            if re.fullmatch(r"\d+(?:\.\d+)?", t):
                return t
    return ""


def _parse_anchored(flat: str):
    """Vendor+RRID anchored parse (robust to PDF line-wrapping). Each row runs
    from a vendor name to the next; the RRID splits catalogue/lot (before) from
    clonality/clone/host (after)."""
    vendors = _vendor_hits(flat)
    rows = []
    for i, (vs, ve, vname) in enumerate(vendors):
        seg = flat[ve:(vendors[i + 1][0] if i + 1 < len(vendors) else len(flat))]
        rrid_m = _RRID_RE.search(seg)
        rrid = rrid_m.group(0) if rrid_m else ""
        pre = (seg[:rrid_m.start()] if rrid_m else seg).strip()   # catalogue + lot
        pre = re.sub(r"^\s*\([^)]*\)\s*", "", pre).strip()        # drop a leading "(DSHB)" etc.
        post = (seg[rrid_m.end():] if rrid_m else "").strip()      # clonality/clone/host…
        toks = pre.split()
        # drop trailing DIGIT-leading lot/date tokens (e.g. "1099750-14", "45476")
        cat_toks = toks[:]
        while len(cat_toks) > 1 and _LOT_RE.fullmatch(cat_toks[-1]):
            cat_toks.pop()
        # The catalogue is the FIRST token, plus any continuation of a line-wrapped
        # catalogue (a token ending in "-", e.g. "PCRPARID2-" + "1A1"). Any tokens
        # after that are an alphanumeric lot the digit-only rule can't catch
        # (e.g. Abbexa "A2511652Q", Aviva "QC30631-40645") — drop them so the lot
        # isn't fused onto the catalogue.
        cat_parts = cat_toks[:1] or toks[:1]
        k = 0
        while k < len(cat_toks) - 1 and cat_toks[k].endswith("-"):
            cat_parts.append(cat_toks[k + 1])
            k += 1
        catalogue = _clean_catalogue(re.sub(r"\s+", "", "".join(cat_parts)))
        lot = " ".join(toks[len(cat_parts):]).strip()   # what's left after the catalogue
        low = post.lower()
        host = next((h for h in HOSTS if h in low.split()), "")
        # clone = the token just before the host, if it looks like a clone id
        clone = ""
        ptoks = post.split()
        if host in ptoks:
            j = ptoks.index(host)
            if j > 0:
                c = ptoks[j - 1].strip("-,")
                if re.search(r"\d", c) and re.search(r"[A-Za-z]", c):
                    clone = c
        rows.append({
            "gene": "", "catalogue": catalogue, "company": vname, "rrid": rrid,
            "clonality_raw": _clonality_from(low), "host": host,
            "clone_id": clone, "lot": lot, "concentration": _conc_after_host(ptoks, host),
            "supplier_apps": _apps_from(post),
            "supplier_url": "", "discontinued": False,
        })
    return rows


def _row_from_rrid(flat: str, positions, i):
    """Best-effort row built around a single RRID. Used as a safety net when the
    vendor-anchored parse didn't capture this antibody — e.g. the vendor isn't in
    KNOWN_VENDORS. Every antibody row has exactly one RRID, so anchoring on it
    means an unrecognised vendor never silently drops the row; `company` is left
    blank for the human to fill. `positions` is the ordered list of
    (start, end, rrid) from _RRID_RE.finditer(flat)."""
    rs, _re, rrid = positions[i]
    left = positions[i - 1][1] if i > 0 else 0
    right = positions[i + 1][0] if i + 1 < len(positions) else len(flat)
    before = flat[left:rs].strip()      # …[prev row's tail][this row: vendor cat lot]
    after = flat[_re:right].strip()     # [this row: clonality clone host …][next row…]

    # catalogue: walk backward from the RRID — drop trailing digit-lot tokens,
    # then take the catalogue (plus a hyphen-wrapped continuation).
    toks = before.split()
    j = len(toks) - 1
    while j >= 0 and _LOT_RE.fullmatch(toks[j]):
        j -= 1
    if j < 0:
        return None
    cat = toks[j]
    if j - 1 >= 0 and toks[j - 1].endswith("-"):
        cat = toks[j - 1] + cat
    catalogue = _clean_catalogue(re.sub(r"\s+", "", cat))
    if not catalogue:
        return None
    lot = " ".join(toks[j + 1:]).strip()     # digit-lot tokens after the catalogue

    company = _find_vendor(before.lower())   # blank if the vendor isn't known
    ptoks = after.split()
    low = [t.lower() for t in ptoks]
    host = next((h for h in HOSTS if h in low), "")
    clone, head = "", " ".join(ptoks[:6])
    if host in low:
        k = low.index(host)
        head = " ".join(ptoks[:k + 1])       # bound clonality to THIS row (up to its host)
        if k > 0:
            c = ptoks[k - 1].strip("-,")
            if re.search(r"\d", c) and re.search(r"[A-Za-z]", c):
                clone = c
    return {
        "gene": "", "catalogue": catalogue, "company": company, "rrid": rrid,
        "clonality_raw": _clonality_from(head.lower()), "host": host,
        "clone_id": clone, "lot": lot, "concentration": _conc_after_host(ptoks, host),
        "supplier_apps": _apps_from(after),
        "supplier_url": "", "discontinued": False,
    }


def _parse_lines(text: str):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    header, body = None, lines
    if _looks_like_header(lines[0]):
        header, body = _split(lines[0]), lines[1:]
    return [
        _row_by_header(header, _split(ln)) if header else _row_by_pattern(_split(ln), ln)
        for ln in body
    ]


def _finalize(raw_rows):
    rows = []
    for row in raw_rows:
        if not row.get("catalogue"):
            continue
        clon, recomb = parse_clonality(row.pop("clonality_raw", ""))
        row["clonality"] = clon
        row["is_recombinant"] = recomb
        if row.get("clone_id", "").strip() == "-":
            row["clone_id"] = ""
        row.setdefault("lot", "")
        row.setdefault("supplier_apps", [])
        row.setdefault("concentration", "")
        row.setdefault("comments", "")
        row.setdefault("gene", "")
        row.setdefault("site", "")
        # Where the vial is and when it arrived — always present, so every
        # reader downstream can ask without knowing which parse produced the
        # row. The anchored (wrapped-PDF) walk never fills them: a freezer box
        # is not in a published table, and guessing one would be inventing an
        # answer about somebody's freezer.
        for key in ("storage_type", "box", "freezer", "position", "received",
                    "isotype", "species_reactivity"):
            row.setdefault(key, "")
        # In kind or purchased, read through the one reader so `In Kind` off a
        # downloaded sheet and `in_kind` off the database mean the same thing.
        row["acquisition"] = parse_acquisition(row.pop("acquisition_raw", ""))
        if row.get("lot", "").strip() in ("-", "n/a", "na"):
            row["lot"] = ""
        rows.append(row)
    return rows


def parse_table(text: str, *, header_led: bool = False):
    """Return a list of metadata dicts, one per antibody row.

    ``header_led`` says the text came from a file that **has** a header row —
    an upload — so the anchored fallback below is skipped and a sheet whose
    headings do not carry a catalogue reads as no rows rather than as guesses.
    See the note above that branch. Pastes leave it ``False``.

    Try the simple line/header parse first (correct for clean pastes, any column
    order). If it clearly missed antibodies — fewer catalogued rows than there are
    RRIDs, which is what a wrapped PDF paste does — fall back to the vendor+RRID
    anchored parse, which is order-tolerant and immune to line-wrapping.

    The anchored parse walks by known vendor, so an *unrecognised* vendor would
    drop its rows. To stop that, every RRID is then reconciled: walk all RRIDs in
    reading order, use the accurate vendor-anchored row where we have one, and
    otherwise build a best-effort row straight from the RRID (company blank). So
    an unknown vendor is flagged, never silently lost. The result is always shown
    in the editable grid for the human to correct."""
    flat = _flatten(text)
    positions = [(m.start(), m.end(), m.group(0)) for m in _RRID_RE.finditer(flat)]
    n_rrid = len(positions)
    line = _parse_lines(text)
    line_cat = [r for r in line if r.get("catalogue")]
    # a *clean* line row has both a catalogue AND its RRID; wrapped-PDF garbage
    # fragments have a catalogue but no RRID.
    line_clean = [r for r in line_cat if r.get("rrid")]

    if n_rrid < 2 or len(line_clean) >= n_rrid:
        return _finalize(line_cat)                    # clean paste — trust the line parse

    # **A file that arrives with a header row is read by its header row.**
    # Everything below guesses: it walks flattened text by known vendor name,
    # which is right for a wrapped PDF paste and wrong for a spreadsheet, and
    # the two were not told apart. A board export carries an RRID on every row,
    # so merging one pair of header cells — enough to lose `catalogue` — sent a
    # perfectly ordinary sheet down this path, where the walk put the **gene**
    # in the catalogue field: `ALKBH2` where `ARP54321_P050` belongs. The rows
    # then previewed as real, and from a gene's page (which supplies the gene
    # the mis-parse had emptied) they were creatable — four antibodies whose
    # catalogue number is a gene symbol, off a sheet with one merged cell.
    # `workbook.read` picks an uploaded sheet *by recognising its headings*, so
    # an upload is header-led by construction and has nothing to gain here.
    if header_led:
        return _finalize(line_cat)

    anchored = [r for r in _parse_anchored(flat) if r.get("catalogue")]
    by_rrid = {r["rrid"]: r for r in anchored if r.get("rrid")}
    out, seen = [], set()
    for i, (rs, _re, rrid) in enumerate(positions):
        if rrid in seen:
            continue
        seen.add(rrid)
        row = by_rrid.get(rrid) or _row_from_rrid(flat, positions, i)
        if row and row.get("catalogue"):
            out.append(row)
    out += [r for r in anchored if not r.get("rrid")]   # rare: anchored row w/o RRID
    return _finalize(out or line_cat)
