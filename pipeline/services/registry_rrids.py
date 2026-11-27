"""RRIDs from the Antibody Registry, asked from inside the pipeline.

Two doors use this, and it is the one place either asks the registry:

* **an antibody being entered** — the Check on every Add and Upload panel
  (``bulk_antibodies.look_up_rrids``);
* **antibodies already on file** — *Find missing RRIDs* on the antibodies
  board (``check`` and ``fill`` below), the page-sized version of
  ``backfill_rrid_from_registry``, which takes ~15 minutes for 200 and so cannot
  run inside a web request.

**The check owns the network; the write owns the rows and makes no call.** The
same split `bulk_targets` makes for UniProt. A registry lookup is 4–5 s, the
site is four threads, and a write runs inside a transaction — holding
PostgreSQL open across a call to somebody else's server is rule 3 of
``tests_timeouts.py``. So ``ask`` runs the lookups in parallel under a
deadline, a row it did not reach comes back ``unchecked``, and the answers are
cached; ``fill`` reads the cache and the row's own link, never the network.

The match is ``scicrunch.lookup_and_match`` — the gate the backfill command
uses: the registry's catalogue matches ours, the gene or the vendor agrees, and
neither contradicts. Only that HIGH match is ever filled; a REVIEW is named and
left for a person.
"""
from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.cache import cache
from django.db import transaction

from pipeline import rrid_utils

logger = logging.getLogger(__name__)

DB = "pipeline_db"

BUDGET_SECONDS = 15.0
WORKERS = 6
# How many antibodies one press of *Check the next* looks up. Six workers at
# 4–5 s a lookup is about 18 inside the deadline; a Cell Signaling number can
# cost a second call (`scicrunch.catalogue_variants`), so this leaves room.
BATCH = 18
# A registry answer for one catalogue is the same answer tomorrow, and the board
# button's `fill` reads it back rather than asking again. An outage is never
# cached.
CACHE_SECONDS = 24 * 3600

FOUND, REVIEW, NONE, UNCHECKED = "found", "review", "none", "unchecked"

NOTES = {
    REVIEW: ("The Antibody Registry has a record under this catalogue number "
             "that does not clearly match this antibody, so no RRID will be "
             "filled in. Check it on antibodyregistry.org and type it in."),
    NONE: "Not in the Antibody Registry, so the RRID stays blank.",
    UNCHECKED: ("The Antibody Registry did not answer in time, so this was not "
                "looked up. Check again to retry."),
}


def key(catalogue, company, gene) -> str:
    """The identity a registry answer belongs to."""
    return "|".join(str(v or "").strip().lower() for v in (catalogue, company, gene))


def _cache_key(k: str) -> str:
    return "rrid-registry:v1:" + hashlib.sha256(k.encode()).hexdigest()


def cached(k: str):
    return cache.get(_cache_key(k))


def _ask_one(catalogue, company, gene, aliases):
    """One antibody's answer, as {status, rrid, url}. Runs on a worker thread,
    so it touches no database — everything it needs is passed in."""
    from pipeline.services import scicrunch
    found = scicrunch.lookup_and_match(catalogue, company, gene, aliases=aliases)
    if found["status"] == "high":
        return {"status": FOUND, "rrid": found["rrid"], "url": found["url"] or ""}
    if found["status"] == "nomatch":
        err = ((found.get("reg") or {}).get("error") or "")
        if "unavailable" in err or "not set" in err or "parsing" in err:
            return {"status": UNCHECKED}
        return {"status": NONE}
    return {"status": REVIEW}


def ask(questions: dict, *, budget_seconds=BUDGET_SECONDS, workers=WORKERS,
        now=time.monotonic) -> dict:
    """``{key: (catalogue, company, gene, aliases)}`` → ``{key: answer}``.

    Answers come from the cache where there is one; the rest are asked in
    parallel until the deadline. A key with no answer is simply absent — the
    caller reads that as ``unchecked``.
    """
    from pipeline.services import scicrunch
    answers = {}
    for k in questions:
        hit = cached(k)
        if hit:
            answers[k] = hit
    to_ask = [k for k in questions if k not in answers]
    if not to_ask or not scicrunch.API_KEY:
        return answers

    deadline = now() + budget_seconds
    # **Not** `with ThreadPoolExecutor(...)`: its exit waits for the slowest
    # call, so the deadline would decide what got recorded while the request
    # sat there anyway. See `bulk_targets._lookup`.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(_ask_one, *questions[k]): k for k in to_ask}
        try:
            for fut in as_completed(futures, timeout=max(0.0, deadline - now())):
                k = futures[fut]
                try:
                    answers[k] = fut.result()
                except Exception:
                    logger.exception("Antibody Registry lookup failed for %r", k)
                    continue
                if answers[k]["status"] != UNCHECKED:
                    cache.set(_cache_key(k), answers[k], CACHE_SECONDS)
        except TimeoutError:
            logger.info("Antibody Registry budget of %.1fs spent; %d of %d asked",
                        budget_seconds, len(answers), len(questions))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return answers


# =============================================================================
# Antibodies already on file — the board's *Find missing RRIDs*
# =============================================================================

def _company(ab) -> str:
    # The spelling the backfill command matches on: the public name first.
    if not ab.company_id:
        return ""
    return ab.company.display_name or ab.company.name or ""


def _key_for(ab) -> str:
    return key(ab.catalogue_number, _company(ab),
               ab.target.gene_name if ab.target_id else "")


def _label(ab) -> str:
    from pipeline.services.cropper import db as cdb
    parts = [ab.target.gene_name if ab.target_id else "",
             ab.catalogue_number,
             cdb.company_label(ab.company) if ab.company_id else "",
             ab.site.name if ab.site_id else ""]
    return " · ".join(p for p in parts if p)


def _blank(ids, member, is_superuser):
    """The antibodies among ``ids`` with no RRID and a catalogue number, in the
    order given, split into the ones this person may write and a count of the
    ones that are another bench's."""
    from pipeline.models import Antibody
    from pipeline.services import ownership
    rows = {a.pk: a for a in Antibody.objects.using(DB)
            .select_related("target", "company", "site")
            .filter(pk__in=list(ids), rrid="")
            .exclude(catalogue_number="")}
    mine, others = [], 0
    for pk in ids:
        ab = rows.get(pk)
        if ab is None:
            continue
        if ownership.refusal({ab.site_id}, member, is_superuser):
            others += 1
            continue
        mine.append(ab)
    return mine, others


def _answer_for(ab, answers):
    """What this row would be filled with, from its own link first — the RRID is
    often already sitting in `rrid_link` — then the registry's answer."""
    from_link = rrid_utils.normalize_rrid(ab.rrid_link or "")
    if from_link:
        return {"status": FOUND, "rrid": from_link, "url": "", "source": "link"}
    answer = answers.get(_key_for(ab))
    if not answer:
        return {"status": UNCHECKED}
    return dict(answer, source="registry") if answer["status"] == FOUND else answer


def check(ids, *, offset=0, member=None, is_superuser=False, **ask_kw) -> dict:
    """Look up the next ``BATCH`` blank-RRID antibodies among ``ids``. Writes
    nothing. ``ids`` is the board's filtered set, in the board's order."""
    from pipeline.services import targets as targets_svc
    mine, others = _blank(ids, member, is_superuser)
    offset = max(0, min(int(offset or 0), len(mine)))
    batch = mine[offset:offset + BATCH]

    questions = {}
    for ab in batch:
        if rrid_utils.normalize_rrid(ab.rrid_link or ""):
            continue
        k = _key_for(ab)
        if k not in questions:
            questions[k] = (ab.catalogue_number, _company(ab),
                            ab.target.gene_name if ab.target_id else "",
                            targets_svc.registry_names(ab.target) if ab.target_id else [])
    answers = ask(questions, **ask_kw) if questions else {}

    items = []
    for ab in batch:
        a = _answer_for(ab, answers)
        if a["status"] == FOUND:
            note = (f"{a['rrid']} is already in this row's registry link"
                    if a.get("source") == "link" else
                    f"{a['rrid']} from the Antibody Registry")
        else:
            note = NOTES.get(a["status"], "")
        items.append({"id": ab.pk, "label": _label(ab), "status": a["status"],
                      "rrid": a.get("rrid", ""), "source": a.get("source", ""),
                      "note": note})
    return {"total": len(mine), "other_sites": others,
            "offset": offset, "next_offset": offset + len(batch),
            "items": items}


class Refused(Exception):
    pass


def fill(ids, *, consented_count, member=None, is_superuser=False) -> dict:
    """Write the RRIDs a check found, for the antibodies the reader agreed to.

    The browser sends ids, never RRIDs: each one is re-derived here from the
    row's own link or the cached registry answer, so what is written is what
    the server found. No network. A row filled since, or whose answer has left
    the cache, is named and skipped; a different count from the one on the
    button refuses the whole press, as `renumber.apply` does.
    """
    from pipeline.models import Antibody
    from pipeline.services import ownership
    ids = [int(i) for i in ids]
    if len(ids) != consented_count:
        raise Refused("What you agreed to has changed — check again.")
    rows = {a.pk: a for a in Antibody.objects.using(DB)
            .select_related("target", "company", "site").filter(pk__in=ids)}
    filled, skipped = [], []
    with transaction.atomic(using=DB):
        for pk in ids:
            ab = rows.get(pk)
            if ab is None:
                skipped.append({"id": pk, "label": f"#{pk}",
                                "why": "no longer on file"})
                continue
            if (ab.rrid or "").strip():
                skipped.append({"id": pk, "label": _label(ab),
                                "why": f"already has {ab.rrid}"})
                continue
            refusal = ownership.refusal({ab.site_id}, member, is_superuser)
            if refusal:
                skipped.append({"id": pk, "label": _label(ab), "why": refusal})
                continue
            a = _answer_for(ab, {_key_for(ab): cached(_key_for(ab))}
                            if cached(_key_for(ab)) else {})
            if a["status"] != FOUND:
                skipped.append({"id": pk, "label": _label(ab),
                                "why": "the registry's answer has expired — check again"})
                continue
            ab.rrid = a["rrid"]
            ab.rrid_link = rrid_utils.registry_url(a["rrid"])
            url = a.get("url") or ""
            if url.lower().startswith(("http://", "https://")) and not (ab.supplier_url or "").strip():
                ab.supplier_url = url
            ab.save(using=DB)
            filled.append({"id": pk, "label": _label(ab), "rrid": a["rrid"]})
    return {"filled": filled, "skipped": skipped}


# =============================================================================
# The registration sheet — the ones the registry does not hold
# =============================================================================
#
# The registry's create endpoint (`POST /api/antibodies`) takes a logged-in
# person's browser token and nothing an app can hold, so new registrations go
# by its advertised route instead: a spreadsheet emailed to abr-help for bulk
# upload. The headings are the registry API's own field names (`AddAntibody`
# in its openapi.json), so the curators can map it without asking.
#
# **Only what the check said is not there.** A row the registry was never
# asked about (no cached answer) is left out and listed, because "not in the
# registry" is a finding and "not checked" is not. **Never invent a value**:
# a field we do not hold is left blank — no composed product names, no
# guessed commercial type — and a row with no product link (the one field the
# registry requires) says so in its own column.

REGISTRY_CLONALITY = {
    "Monoclonal": "monoclonal",
    "Polyclonal": "polyclonal",
    "Recombinant monoclonal": "recombinant monoclonal",
    "Recombinant polyclonal": "recombinant polyclonal",
    "Recombinant": "recombinant",
}

_SUPPLIER_APP_NAMES = (
    ("supplier_validated_wb", "Western Blot"),
    ("supplier_validated_ip", "Immunoprecipitation"),
    ("supplier_validated_if", "Immunofluorescence"),
    ("supplier_validated_ihc", "Immunohistochemistry"),
    ("supplier_validated_elisa", "ELISA"),
    ("supplier_validated_fc", "Flow Cytometry"),
)

SHEET_COLUMNS = (
    "catalogNum", "vendorName", "abTarget", "abTargetUniprotId", "clonality",
    "cloneId", "sourceOrganism", "productIsotype", "targetSpecies",
    "applications", "epitope", "url",
    # Ours, after the registry's: what a curator replying about a row needs.
    "OGA site", "OGA A-number", "registry check", "missing before submitting",
)

_CHECK_WORDS = {
    NONE: "not in the Antibody Registry",
    REVIEW: ("the registry holds a different product under a similar number — "
             "this one is not registered"),
}


def _registration_row(ab, status) -> dict:
    from pipeline.services import clonality, lab_numbers
    from pipeline.services.cropper import db as cdb
    apps = [name for field, name in _SUPPLIER_APP_NAMES if getattr(ab, field, False)]
    url = (ab.supplier_url or "").strip()
    return {
        "catalogNum": ab.catalogue_number,
        "vendorName": cdb.company_label(ab.company) if ab.company_id else "",
        "abTarget": ab.target.gene_name if ab.target_id else "",
        "abTargetUniprotId": (ab.target.uniprot_id or "") if ab.target_id else "",
        "clonality": REGISTRY_CLONALITY.get(clonality.label(ab), ""),
        "cloneId": ab.clone_id or "",
        "sourceOrganism": ab.host_species or "",
        "productIsotype": ab.isotype or "",
        "targetSpecies": ab.species_reactivity or "",
        "applications": "; ".join(apps),
        "epitope": ab.antigen or "",
        "url": url,
        "OGA site": ab.site.name if ab.site_id else "",
        "OGA A-number": lab_numbers.label(ab.ab_number, kind=lab_numbers.ANTIBODY) or "",
        "registry check": _CHECK_WORDS[status],
        "missing before submitting": "" if url.lower().startswith(("http://", "https://"))
                                     else "product page link (the registry requires one)",
    }


def registration_sheet(ids, *, member=None, is_superuser=False) -> dict:
    """The rows to register and the rows left out, each with its reason.
    Reads the cache the checks filled; makes no registry call."""
    mine, others = _blank(ids, member, is_superuser)
    rows, left_out = [], []
    for ab in mine:
        a = _answer_for(ab, {_key_for(ab): cached(_key_for(ab))}
                        if cached(_key_for(ab)) else {})
        if a["status"] in (NONE, REVIEW):
            rows.append(_registration_row(ab, a["status"]))
        else:
            left_out.append({"antibody": _label(ab), "why": {
                FOUND: "the registry has it — press Fill instead",
                UNCHECKED: "not checked against the registry yet — press Check first",
            }.get(a["status"], a["status"])})
    return {"rows": rows, "left_out": left_out, "other_sites": others}


def registration_workbook(sheet) -> bytes:
    import io
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Register"
    ws.append(list(SHEET_COLUMNS))
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in sheet["rows"]:
        ws.append([r[k] for k in SHEET_COLUMNS])
    for col, width in zip("ABCDEFGHIJKLMNOP",
                          (16, 26, 12, 14, 22, 14, 12, 12, 18, 40, 30, 50,
                           12, 12, 40, 36)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    out = wb.create_sheet("Left out")
    out.append(["antibody", "why it is not on the Register tab"])
    for c in out[1]:
        c.font = Font(bold=True)
    for r in sheet["left_out"]:
        out.append([r["antibody"], r["why"]])
    if sheet["other_sites"]:
        out.append([f"{sheet['other_sites']} antibodies at other benches",
                    "not yours to register — a superuser's download includes them"])
    out.column_dimensions["A"].width = 60
    out.column_dimensions["B"].width = 60
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
