"""Server A serialisation via the canonical public API code.

Server A's structured tools return data serialised by the SAME functions the
public data portal + website use — ``core/api_views.py::_serialise_antibody`` /
``_target_has_recommendations`` / ``_get_report_link`` — so an assistant sees
*exactly* what a data-portal / API consumer sees (identical field shape, image
URLs, embed links). No hand-maintained SQL views to drift.

How the boundary is enforced now:
  * The ORM runs through the read-only role — ``pipeline_db`` is pointed at
    ``MCP_READONLY_DATABASE_URL`` (SELECT-only, ``default_transaction_read_only``,
    a statement timeout, and NO access to ``auth_user``; see ``roles.sql``).
  * The PUBLIC boundary is the app-level filter ``publication_images__isnull=
    False`` — the exact filter ``antibodies_feed`` / ``gene_detail`` apply — so
    Server A only ever returns antibodies that carry a published figure (and the
    targets that own them). This is the same boundary the public API trusts.
"""
from __future__ import annotations

import os
import re as _re

from mcp_servers.common import manuscript  # identifier resolution, no Django
from mcp_servers.common import briefing, sections  # what the caller knows and read
from mcp_servers.common import citeab        # what the LITERATURE records
from mcp_servers.common import confusions   # whose target it actually is

_MISSING = object()
from mcp_servers.common.controls_rubric import (CONTROL_CLASSES, CONTROLS_BINDS,
                                                CONTROLS_RUBRIC,
                                                CONTROLS_SCAFFOLD,
                                                SCAFFOLD_VERSION,
                                                UNCLASSIFIED_NOTE, canonical_type,
                                                CONTROLS_RUBRIC_VERSION,
                                                SELECTIVITY_CLASSES)

_READY = False


def setup():
    """Idempotently boot Django with ``pipeline_db`` aimed at the read-only role."""
    global _READY
    if _READY:
        return
    ro = (os.environ.get("MCP_READONLY_DATABASE_URL") or "").strip()
    if ro:
        # Server A is READ-ONLY: always route the ORM's pipeline_db through the
        # least-privilege read-only role, OVERRIDING any leftover
        # PIPELINE_DATABASE_URL (e.g. the retired write server's writer DSN, which
        # would otherwise cause a wrong-credential/auth failure and — worse — a
        # write-capable connection). The read-only DSN is the only one Server A
        # should ever use.
        os.environ["PIPELINE_DATABASE_URL"] = ro
    from mcp_servers.common.django_bootstrap import setup_django
    setup_django()
    _READY = True


# ---------------------------------------------------------------------------
# Lazy accessors (import only after Django is configured)
# ---------------------------------------------------------------------------

def _api():
    from core import api_views as A
    return A


def _published_antibodies():
    """The published set, from the shared definition in ``pipeline.public``.

    Same reasoning as ``_public_targets`` below: this was a hand copy of the
    ``publication_images__isnull=False`` boundary, so the connector's
    ``total_antibodies`` tracked the site's by coincidence rather than by
    construction. Only the fetching strategy is the connector's own.
    """
    from pipeline.public import published_antibodies
    return (published_antibodies()
            .select_related("target", "company")
            .prefetch_related("publication_images"))


def _public_targets():
    """The set of genes the public site counts/lists — the shared definition in
    ``pipeline.public``, so Server A's gene count matches the homepage, the gene
    pages and the browser extension rather than tracking them by hand."""
    from pipeline.public import public_targets
    return public_targets()


def _reports(target):
    # Only the public-facing report links (F1000 / Zenodo DOIs + dates) — the same
    # thing the gene page shows. NOT the report's internal workflow status.
    from django.db.models import Q
    rows = target.reports.filter(Q(f1000_doi__gt="") | Q(zenodo_doi__gt="")).order_by("id")
    return [{
        "f1000_doi": r.f1000_doi or None, "zenodo_doi": r.zenodo_doi or None,
        "f1000_date": r.f1000_date.isoformat() if r.f1000_date else None,
        "zenodo_date": r.zenodo_date.isoformat() if r.zenodo_date else None,
    } for r in rows]


def _report_dois(reports):
    """The DOI(s) to cite for a gene — F1000 if one exists, otherwise Zenodo.

    An F1000 article SUPERSEDES the Zenodo preprint of the same work, so quoting
    both makes one report look like two independent ones. Previously this took
    ``f1000 or zenodo`` per report ROW, which still yielded a Zenodo DOI alongside
    an F1000 one whenever a gene had more than one row.
    """
    f1000 = [r["f1000_doi"] for r in reports if r.get("f1000_doi")]
    if f1000:
        return list(dict.fromkeys(f1000))
    return list(dict.fromkeys(r["zenodo_doi"] for r in reports if r.get("zenodo_doi")))


# ---------------------------------------------------------------------------
# 4b — factual, provenance-anchored interpretation layer
#
# Every application gets one of three controlled-vocabulary verdicts, so a
# connected assistant reports FACTS, never inferences:
#   * recommended      — tested by YCharOS with KO controls and recommended.
#   * not_recommended  — tested by YCharOS with KO controls; did NOT meet the bar.
#   * not_tested       — no independent assessment for this application (≠ "bad").
# Absence from the dataset entirely is reported separately (in_dataset=False),
# so "not in the dataset" is never confused with "no evidence yet".
# ---------------------------------------------------------------------------

_APP_REC = {"WB": "wb_recommended", "IP": "ip_recommended",
            "IF": "if_recommended", "FC": "fc_recommended"}
# application code -> the PublicationImage.application_type it is stored under
_APP_IMG = {"WB": "WB", "IP": "IP", "IF": "ICC-IF", "FC": "FC"}
#: inverse of _APP_IMG — a PublicationImage.application_type back to the code the
#: `assessment` dict is keyed by. Images are stored as "ICC-IF" while every verdict
#: is keyed "IF", so anything joining a figure to its verdict had to know that
#: quirk. Normalise once here instead.
_IMG_APP = {v: k for k, v in _APP_IMG.items()}
_APPS = ("WB", "IP", "IF", "FC")


def _assessment(ab, image_types, gene_has_recommendations,
                axes_by_app=None):
    """Per-application verdict, from the SAME public signals the portal is built on:
    the curated ``*_recommended`` flag, whether a published figure exists for that
    application, and whether the GENE has been curated at all
    (``gene_has_recommendations``).

    The gene-level gate is what separates "assessed and did not make the cut" from
    "not assessed yet", and it is the piece that was missing:

      * flag set                        -> recommended.
      * no flag, published figure for
        this application, AND the gene
        has recommendations             -> not_recommended. Someone curated this
                                           gene, picked winners, and did not pick
                                           this one — a real negative result.
      * anything else                   -> not_tested.

    Without the gene gate, every published antibody of an UNCURATED gene was
    reported as "tested and not recommended" purely because a figure existed. On the
    live dataset that is several genes' worth of named commercial products being
    told they failed independent testing when nobody has assessed them yet. An
    uncurated gene now reports not_tested, which is what it is.

    Deliberately does NOT read the pipeline's per-session result models
    (``wb_results`` and friends): those are internal lab records, not public data,
    and this connector carries what the live site and portal API carry.
    """
    out = {}
    for app in _APPS:
        recommended = bool(getattr(ab, _APP_REC[app]))
        has_figure = _APP_IMG[app] in image_types
        if recommended:
            status, basis = "recommended", ["recommendation_flag"]
            if has_figure:
                basis.append("published_figure")
        elif has_figure and gene_has_recommendations:
            status = "not_recommended"
            basis = ["published_figure", "gene_curated"]
        else:
            status, basis = "not_tested", []
        # The clause after the result, on either side of it: a supportive
        # western blot that is not selective, or a negative that did detect,
        # enrich or give a selective signal. `core/recommendations.py` is the
        # one reader — a model asked "is this antibody any good" is exactly the
        # caller that should not be handed a bare yes/no when the bench
        # recorded more.
        support, clause, words, sentence = _wording_for(
            ab, app, status, axes_by_app)
        # `support` FIRST, because a model reads a dict in order and takes the
        # first field that looks like the answer. It is the four-rung value the
        # whole site prints; `status` below is the three-value legacy form,
        # which cannot tell *Limited support* from *Not supportive* and so
        # reports an antibody that did what the application is for as though it
        # had shown nothing. Both ship: `status` is what the connector has
        # always returned, and the reversal in a report is worse than the
        # duplication.
        entry = {"support": support or _SUPPORT_FALLBACK.get(status, status),
                 "verdict": words,
                 "verdict_sentence": sentence,
                 "status": status, "tested": bool(basis),
                 "recommended": recommended, "tested_from": basis}
        if clause:
            entry["qualifier"] = clause
        if not words:
            # `_wording_for` degrades to "" when the site's module is not
            # importable in this process. A key holding an empty string reads
            # as "we looked and there is nothing", which is a different claim
            # from "this build could not resolve the wording".
            entry.pop("verdict")
            entry.pop("verdict_sentence")
        if status != "not_tested":
            # Only claim a source for a verdict that was actually reached.
            entry["verdict_source"] = (
                "curated recommendation flag" if recommended else
                "no recommendation flag on a curated gene that published this "
                "application's figure")
        out[app] = entry
    return out


#: What a rung degrades to when the site's module is not importable in this
#: process, so `_wording_for` could not resolve one.
#:
#: **It takes the harsher of the two negatives, and that is not a judgement
#: call** — it is exactly what `status` has always said on that path, so the
#: degraded answer introduces no claim the connector was not already making.
#: The middle rung cannot be reached here by construction: it exists only where
#: the capability axes say the antibody did what the application is for, and
#: those come from the same import that just failed. Saying `limited_support`
#: without them would be a guess, and it would be the flattering one.
_SUPPORT_FALLBACK = {"recommended": "supportive",
                     "not_recommended": "not_supportive",
                     "not_tested": "not_tested"}


def _wording_for(ab, app, status, axes_by_app):
    """``(support, qualifier, words, sentence)`` from the site's own definition.

    The words are `core/recommendations.py`'s — *Supportive*, *Limited
    support*, *Not supportive*, *Not tested* — because a model answering from
    this connector is quoting a public page to somebody who may go and read it,
    and two vocabularies for one result is the drift that module exists to stop.

    ``support`` is that same rung as a controlled value, which is the half a
    model can be told to switch on. It comes from ``describe`` rather than being
    mapped from ``status`` here: the mapping is not one-to-one — a
    ``not_recommended`` that tempers is *Limited support* and one that does not
    is *Not supportive* — so deriving it locally would be a second reader for a
    question that already has one, and the wrong answer would be the plausible
    one.

    Guarded: the MCP is a separate process with its own requirements, and a
    Django import that is unavailable there must degrade to the bare controlled
    value rather than take the tool down.
    """
    try:
        from core.recommendations import (NOT_RECOMMENDED, NOT_TESTED,
                                          RECOMMENDED, qualified, support,
                                          words as words_for, cell_caption)
    except Exception:
        return "", "", "", ""

    value = {"recommended": RECOMMENDED, "not_recommended": NOT_RECOMMENDED,
             "not_tested": NOT_TESTED}.get(status, NOT_TESTED)
    if value == NOT_TESTED:
        rung = support(value)
        return rung, "", words_for(value), words_for(value)

    db_app = _APP_FIELD_TO_DB.get(app, app)
    axes = (axes_by_app or {}).get(db_app)
    if not axes:
        rung = support(value)
        return rung, "", words_for(value), words_for(value)

    clause, tempers = qualified(db_app, value, axes)
    rung = support(value, tempers)
    return (rung, clause, words_for(value, tempers),
            cell_caption(db_app, value, axes))


#: This module keys immunofluorescence `IF`; the database says `ICC-IF`.
_APP_FIELD_TO_DB = {"WB": "WB", "IP": "IP", "IF": "ICC-IF", "FC": "FC"}


def _factual_summary(ab, assessment, dois):
    """A plain-fact sentence: what was tested, the verdict per application, and the
    consensus-protocol DOI it was assessed under. No adjectives, no marketing."""
    gene = ab.target.gene_name
    ident = ab.catalogue_number + (f" (RRID {ab.rrid})" if ab.rrid else "")
    # Grouped by the FOUR rungs, not the three legacy statuses. Grouping on
    # `status` put *Limited support* in the same clause as an outright negative
    # — 491 of 1,833 negatives on live data — so the one sentence a model is
    # most likely to quote verbatim was the one place the distinction was lost
    # after being carried correctly everywhere else in the response.
    def _of(rung):
        return [a for a in _APPS if assessment[a].get("support") == rung]

    supportive = _of("supportive")
    limited = _of("limited_support")
    notsup = _of("not_supportive")
    nottested = _of("not_tested")
    parts = [f"{ident} against {gene} is in the dataset."]
    # The site's frame, not the connector's own: OGA characterises antibodies
    # and a gene page is headed "characterisation data", so the sentence
    # describes evidence rather than issuing advice.
    if supportive:
        parts.append("Characterisation data supports "
                     + ", ".join(supportive) + ".")
    if limited:
        parts.append("Limited support for " + ", ".join(limited)
                     + " — tested, not supported overall, and the antibody was "
                       "still seen to do what the application is for.")
    if notsup:
        parts.append("Tested; data not supportive for " + ", ".join(notsup) + ".")
    if nottested:
        parts.append("Not tested for " + ", ".join(nottested) + ".")
    # What the bench recorded beyond the rung, where it says more: a supportive
    # blot that is not selective, or a negative that did detect, enrich or give
    # a selective signal. A model asked "is this any good" is exactly the caller
    # that should not get a bare yes/no when there is more.
    qualified = [f"{a} — {assessment[a]['qualifier']}"
                 for a in _APPS if assessment[a].get("qualifier")]
    if qualified:
        parts.append("With qualifications: " + "; ".join(qualified) + ".")
    if dois:
        parts.append("Assessed with knockout controls under the consensus "
                     "protocol described in " + "; ".join(dois) + ".")
    return " ".join(parts)


def _axes_for(antibodies):
    """The capability behind every verdict, for a whole list in one pass.

    ``_enrich`` is called from four list comprehensions with a cap of 500, so a
    per-antibody lookup here is 8 queries × 500 — the N+1 that shows up as a
    slow tool rather than a wrong answer. Degrades to ``None`` if the site's
    module is not importable in this process: the MCP is a separate service
    with its own requirements, and a missing import must cost the qualifier,
    not the tool.
    """
    ids = [ab.pk for ab in antibodies]
    if not ids:
        return {}
    try:
        from core.recommendations import capability_axes
        return capability_axes(ids)
    except Exception:
        return {}


def _enrich(ab, axes=None):
    """Serialise ``ab`` exactly as the public API does, then add the factual
    interpretation layer (per-application assessment + summary + provenance) and
    the KO-controlled evidence + report DOIs behind it.

    ``axes`` comes from ``_axes_for`` — resolved once for the whole list by the
    caller, the same way the public API's feeds pass it down.
    """
    A = _api()
    rec = A._target_has_recommendations(ab.target)
    d = A._serialise_antibody(ab, rec, include_recs=True,
                              axes={k: v for k, v in (axes or {}).items()
                                    if k[0] == ab.pk} or None)
    reports = _reports(ab.target)
    dois = _report_dois(reports)
    image_types = {img.application_type for img in ab.publication_images.all()}
    per_app = {app: (axes or {}).get((ab.pk, app))
               for app in ("WB", "IP", "ICC-IF", "FC")}
    assessment = _assessment(ab, image_types, rec, per_app)
    d["assessment"] = assessment
    d["summary"] = _factual_summary(ab, assessment, dois)
    d["provenance"] = {
        "rrid": ab.rrid or None,
        "rrid_registry": ab.rrid_link or None,
        "report_dois": dois,
        "gene_page_url": d["gene_page_url"],
    }
    d["reports"] = reports
    # `embed_urls` are the embed-CARD links. Every caller is told to use the direct
    # media files in `experiments` instead, so shipping the cards too was ~15% of
    # every antibody record spent on something we actively tell people not to use.
    d.pop("embed_urls", None)
    return d


def _target_is_public(target) -> bool:
    from pipeline.models import Antibody
    return Antibody.objects.filter(target=target,
                                   publication_images__isnull=False).exists()


# ---------------------------------------------------------------------------
# Query + serialise functions (each returns plain JSON-able data)
# ---------------------------------------------------------------------------

def list_targets(only_with_recommendations=False):
    # Public genes only — the SAME set the site's homepage counts (see
    # _public_targets / get_live_stats). We NEVER expose the internal pipeline
    # workflow status (Target.status) — it isn't on the gene pages and is
    # unreliable — and there is no status filter for the same reason.
    from django.db.models import Q
    qs = _public_targets()
    if only_with_recommendations:
        qs = qs.filter(
            Q(antibodies__wb_recommended=True) | Q(antibodies__ip_recommended=True)
            | Q(antibodies__if_recommended=True) | Q(antibodies__fc_recommended=True)
        ).distinct()
    rows = [{"id": t.id, "gene_name": t.gene_name, "protein_name": t.protein_name,
             "uniprot_id": t.uniprot_id}
            for t in qs.order_by("gene_name")]
    return rows


def _typeset_tolerant(qs):
    """Annotate a queryset with a space-stripped catalogue number.

    The input side is normalised by ``manuscript.resolution_variants``, and for a
    while that was assumed to be the whole job. It is not, because **the stored
    side carries typesetting too**: seven published catalogue numbers hold a
    digit-group space, Synaptic Systems' own house style — ``107 102``,
    ``110 043``, ``320 003``. A paper that prints one of those closed up, which
    most do, cannot reach the row however carefully the input is cleaned.

    Stripping the space from the stored side collides with nothing (checked across
    all 1,611 published catalogue numbers), and only the space needs it — no stored
    number carries a dash variant or a zero-width character.

    ``_cat_collapsed`` goes further and takes the punctuation out too, because the
    publisher does not only ADD typesetting — it MOVES the number's own hyphen, and
    a hyphen the paper dropped cannot be put back by cleaning the input.
    ``MA5-11154`` is printed ``MA511154`` and ``11820-1-AP`` as ``11820-1AP``, so
    the only place those two strings meet is with all punctuation gone from both.
    That form is deliberately the LAST one tried and refuses anything shorter than
    ``manuscript.MIN_COLLAPSED``.
    """
    from django.db.models import Value
    from django.db.models.functions import Replace
    collapsed = Replace("catalogue_number", Value(" "), Value(""))
    for ch in ("-", ".", "_", "/"):
        collapsed = Replace(collapsed, Value(ch), Value(""))
    return qs.annotate(
        _cat_plain=Replace("catalogue_number", Value(" "), Value("")),
        _cat_collapsed=collapsed)


def _collapsed_keys(value):
    """Every alphanumerics-only form worth trying for ``value`` (may be empty)."""
    out = []
    for form in manuscript.resolution_variants(value):
        c = manuscript.collapse_identifier(form)
        if c and c.lower() not in out:
            out.append(c.lower())
    return out


def _identifier_q(value, field):
    """Q matching ``value`` against ``field``, tolerant of typesetting BOTH sides.

    Every lookup path must use this. It existed on ``check_manuscript`` alone,
    which is why a re-run reported the normalisation as never having landed: the
    fixtures went through ``antibody_validation``, whose ``catalogue_number__iexact``
    saw the raw string. A fix on one of three doors is a fix for one door — the
    same shape as the supplier preview on the boards.
    """
    from django.db.models import Q
    q = Q()
    for form in manuscript.resolution_variants(value):
        q |= Q(**{f"{field}__iexact": form})
        if field == "catalogue_number":
            # …and against the stored number with its own spaces taken out.
            q |= Q(_cat_plain__iexact=form.replace(" ", ""))
    if field == "catalogue_number":
        for c in _collapsed_keys(value):
            q |= Q(_cat_collapsed__iexact=c)
    return q


def antibody_validation(rrid=None, catalogue=None, gene=None, cap=500):
    qs = _published_antibodies()
    if rrid:
        qs = _typeset_tolerant(qs).filter(_identifier_q(rrid, "rrid"))
    if catalogue:
        qs = _typeset_tolerant(qs).filter(_identifier_q(catalogue, "catalogue_number"))
    if gene:
        t = _resolve_target(gene)
        qs = qs.filter(target=t) if t else qs.none()
    if not (rrid or catalogue or gene):
        raise ValueError("Provide at least one of: rrid, catalogue, gene.")
    rows = list(qs.order_by("target__gene_name", "catalogue_number")[:cap + 1])
    axes = _axes_for(rows)
    out = [_enrich(ab, axes) for ab in rows]
    truncated = len(out) > cap
    return out[:cap], truncated


def confusion_lookup(rrid=None, catalogue=None, doi=None):
    """The ``target_confusion`` block for a one-antibody lookup, or ``None``.

    ``antibody_validation`` answers on an identifier alone and has no paper, so
    its miss was the reply most at risk: "not in the dataset, and absence is not
    a judgement about the antibody" about a reagent whose supplier says it does
    not bind the protein the reader asked about. Here so the tool layer does not
    have to know how the forms are expanded — the same
    ``manuscript.resolution_variants`` order the dataset query used, or the two
    lookups could disagree about the same printed string.
    """
    forms = []
    for given in (catalogue, rrid):
        if not given:
            continue
        for form in manuscript.resolution_variants(given):
            if form not in forms:
                forms.append(form)
    return confusions.for_identifier(forms, doi=doi, doi_supplied=bool(doi))


def gene_detail(gene):
    A = _api()
    # Resolve by symbol, protein name or alias — a caller asking about
    # "alpha-synuclein" must reach SNCA rather than be told we hold nothing.
    target = _resolve_target(gene)
    if target is None:
        # Same framing as antibody_validation's miss: absence from the dataset is
        # not a judgement about the gene or its antibodies.
        return {"found": False, "gene": gene, "note": _MAN_NOTE}

    qs = _published_antibodies().filter(target=target).order_by("company__name",
                                                                "catalogue_number")
    rec = A._target_has_recommendations(target)
    antibodies, supplier_summary = [], {}
    rows = list(qs)
    axes = _axes_for(rows)
    for ab in rows:
        s = _enrich(ab, axes)
        antibodies.append(s)
        supplier = s["metadata"].get("supplier") or "Unknown"
        ss = supplier_summary.setdefault(supplier, {"count": 0, "recommended": 0})
        ss["count"] += 1
        if any(s["recommendations"].get(a) for a in ("WB", "ICC-IF", "IP", "FC")):
            ss["recommended"] += 1

    return {
        "found": True,
        "gene": target.gene_name,
        "matched_on": (gene if (gene or "").strip().lower() != target.gene_name.lower()
                       else None),
        "gene_page_url": f"{A.BASE_URL}/antibodies/{target.gene_name}/",
        "has_recommendations": rec,
        "total_antibodies": len(antibodies),
        "supplier_summary": supplier_summary,
        "antibodies": antibodies,
        "reports": _reports(target),
    }


_APP_FIELD = {"WB": "wb_recommended", "IP": "ip_recommended",
              "IF": "if_recommended", "ICC-IF": "if_recommended",
              "FC": "fc_recommended"}


#: The four rungs this connector can be asked for, and what each one is.
SUPPORT_RUNGS = ("supportive", "limited_support", "not_supportive", "not_tested")


def antibodies_by_support(application, support="supportive", gene=None, cap=500):
    """Within one gene, the antibodies at one rung for one application.

    **Filtered on the resolved rung, not on the recommendation flag.** It
    replaced a boolean form (removed 14 Sep 2026) whose two negatives were one
    answer: an antibody the bench saw detect its target and one that showed
    nothing came back in the same list, and no caller could ask for either
    alone.

    The flag was also the wrong question in two smaller ways the rung gets
    right. A ``False`` flag on an application with no published figure, or on
    an uncurated gene, is ``not_tested`` rather than a negative — the old form
    returned those among the failures. And an ICC-IF result whose measured
    ratio falls below the floor is not supportive however the flag is set, the
    same veto the gene pages apply.

    The rung is not a column, so it cannot be a ``filter()`` — it is resolved
    per antibody from the flag, the figures, the curated-gene gate and the
    capability axes. So the gene's published antibodies are enriched and then
    filtered, which is the same work ``target_report`` already does for the same
    scope: a gene holds tens of antibodies, not thousands, and the axes are
    resolved once for the batch.
    """
    app = (application or "").strip().upper()
    if app not in _APP_FIELD:
        raise ValueError(
            f"Unknown application '{application}'. Use one of: "
            + ", ".join(sorted(set(_APP_FIELD))) + ".")
    rung = (support or "").strip().lower()
    if rung not in SUPPORT_RUNGS:
        raise ValueError(
            f"Unknown support value '{support}'. Use one of: "
            + ", ".join(SUPPORT_RUNGS) + ".")
    # A gene is REQUIRED — this tool answers "for gene X, which antibodies sit
    # at rung R for app Y?", not a whole-database list (which would let a
    # caller tally pass rates by vendor). Whole-DB analytics is out of scope by
    # policy.
    if not (gene or "").strip():
        raise ValueError(
            "A 'gene' is required. This tool reports results within a "
            "single gene; it does not return a whole-database list.")
    t = _resolve_target(gene)
    qs = (_published_antibodies().filter(target=t)
          if t else _published_antibodies().none())
    rows = list(qs.order_by("company__name", "catalogue_number"))
    axes = _axes_for(rows)
    # `_APPS` keys assessment by IF; the caller may have said ICC-IF.
    key = "IF" if app == "ICC-IF" else app
    out = []
    for ab in rows:
        enriched = _enrich(ab, axes)
        if (enriched.get("assessment", {}).get(key, {}).get("support") == rung):
            out.append(enriched)
        if len(out) > cap:
            break
    truncated = len(out) > cap
    return out[:cap], truncated


def search_antibodies(text, limit=50, cap=500):
    """Locate a specific published antibody by catalogue number, RRID, or gene —
    the "is this antibody in the dataset?" lookup. Deliberately does NOT match by
    company, so it can't be used to enumerate one vendor's antibodies (whole-DB /
    per-vendor analytics is out of scope by policy)."""
    from django.db.models import Q
    text = (text or "").strip()
    if not text:
        return [], False
    # Substring search on what was typed, plus an exact match on every typeset
    # form — `icontains` on the raw string cannot find `14060-1-AP` from a paper's
    # `14,060–1-AP`, because the en-dash is a character the stored number does not
    # contain anywhere.
    q = Q(catalogue_number__icontains=text) | Q(rrid__icontains=text)
    q |= _identifier_q(text, "catalogue_number") | _identifier_q(text, "rrid")
    t = _resolve_target(text)          # symbol, protein name or alias
    q |= Q(target=t) if t else Q(target__gene_name__iexact=text)
    qs = _typeset_tolerant(_published_antibodies()).filter(q)
    lim = min(limit, cap)
    rows = list(qs.order_by("target__gene_name", "catalogue_number")[:lim + 1])
    axes = _axes_for(rows)
    out = [_enrich(ab, axes) for ab in rows]
    truncated = len(out) > lim
    return out[:lim], truncated


# ---------------------------------------------------------------------------
# Manuscript tools — the "I have a paper" front door.
#
# The CALLER reads the paper and reports the reagents, genes and controls it found;
# this layer resolves them against the DB and groups the hits by verdict. Reading a
# manuscript is the model's job. Deciding what is in the dataset, and what a
# control counts as, is the server's.
# ---------------------------------------------------------------------------

_MAN_NOTE = (
    "not_in_dataset means YCharOS has not independently characterised that "
    "antibody with knockout controls — it is untested, NOT unreliable; absence "
    "is not a verdict on quality. THE ONE EXCEPTION is an entry carrying "
    "`target_confusion`: that reagent is documented as an antibody to a "
    "DIFFERENT protein from the one it shares a name with, so it is not an "
    "absence at all and this sentence does not apply to it — read its own "
    "`this_paper.note`, which differs per paper. "
    "Verdicts are PER APPLICATION (WB/IP/IF/FC): "
    "never assume a result in one application transfers to another. "
    "ALTERNATIVES: TWO kinds of entry carry `gene` and earn this — a "
    "not_in_dataset one (untested, so the reader is waiting on evidence) AND a "
    "TESTED one whose verdict is not supportive for an application the paper used "
    "(the reader is not waiting on evidence, they have it and it is against the "
    "reagent in their hands). The second is the sharper case and needs the pointer "
    "MORE, not less: name the alternatives on both. Either way — say OGA has "
    "characterised that target, link `gene_page_url`, and name the "
    "antibodies in `characterised_alternatives[gene]`, as a pointer, never as "
    "criticism of the reagent the paper used. `characterised_alternatives[gene]` "
    "is a SHORTLIST of `alternatives_listed`: when `alternatives_truncated` is true "
    "say \"N of M\" and send the reader to the gene page (or "
    "antibodies_by_support) for the rest — never present the shortlist as "
    "the complete set. M is `recommended_for_requested_applications` when that "
    "field is present (the caller named an application, so that is what is "
    "available to them), and `recommended_alternatives` — the gene's total, any "
    "application — otherwise. `recommended_for_requested_applications: 0` on a "
    "characterised target is ITSELF the answer: OGA has worked on this gene but "
    "recommends nothing for the application the paper used, so there is no drop-in "
    "swap. Say that, rather than reaching for an antibody recommended for something "
    "else. Check `recommended_for` on any row you cite. "
    "TARGET ATTRIBUTION: when an entry carries `target_matched_via: \"alias\"`, the "
    "reagent reached that gene through a stored synonym, not through its own symbol "
    "or protein name — SAY SO in the same breath as the gene, naming both sides "
    "(matched via the alias \"p65\" → SYT1). Short synonyms are shared between "
    "unrelated proteins, and the reader is the only one who knows which they meant; "
    "if it is the wrong one, that one line is what tells them, and the alternatives "
    "underneath are for a protein they are not studying. "
    "When an entry carries `target_ambiguous`, the name the paper used is shared "
    "between genes (`candidates`) and the server has deliberately resolved it to "
    "NONE of them — no gene, and no alternatives. Say which name was ambiguous and "
    "name the candidates, and ask which was meant rather than picking: only one of "
    "them may be in the dataset, and that is not evidence it is the right one."
)


def _overall_bucket(assessment):
    """One headline bucket per antibody for scannability. The per-application
    ``assessment`` is ALWAYS carried on the hit too, so nothing generalises a
    single-application verdict — this is only an index."""
    statuses = [assessment[a]["status"] for a in _APPS]
    if "recommended" in statuses:
        return "recommended"
    if "not_recommended" in statuses:
        return "not_recommended"
    return "not_tested"


def _manuscript_hit(found, kind, enriched):
    a = enriched["assessment"]
    return {
        "identifier": found,                 # exactly as found in the text
        "matched_as": kind,
        "antibody_name": enriched.get("antibody_name"),
        "gene": enriched.get("gene"),
        "applications": {app: a[app]["status"] for app in _APPS},
        "assessment": a,                     # full per-application verdict + evidence flags
        "summary": enriched.get("summary"),
        "rrid": enriched["provenance"]["rrid"],
        "product_link": (enriched.get("metadata") or {}).get("product_link") or None,
        "report_dois": enriched["provenance"]["report_dois"],
        "gene_page_url": enriched["provenance"]["gene_page_url"],
        "experiments": enriched.get("experiments"),    # per-application image URLs
    }


#: Aliases that are a CURRENT common name for one gene and a HISTORICAL name for
#: another we happen to hold. The collision is in the literature, not in our data —
#: nothing in the database can detect it, because only one of the two genes is in
#: the dataset and so nothing competes for the name.
#:
#: `p65` is the whole reason this exists: it is the everyday name for RELA and an
#: old name for synaptotagmin-1, and six reagents across four benchmark papers meant
#: RELA. Disclosure alone was not enough — the reply still named SYT1 and still
#: offered five SYT1 antibodies, and a recommendation is what turns a resolution
#: error into a reagent someone might actually buy.
#:
#: Values are the genes a reader might have meant, most likely first.
#:
#: **Do not "fix" this by deleting the alias.** It is not a bad row: `Target.aliases`
#: is HGNC's own `alias_symbol` / `prev_symbol`, fetched by
#: `core/management/commands/populate_aliases.py` and copied across by
#: `copy_core_to_pipeline`. Synaptotagmin-1 really was called p65 — it was
#: characterised as a 65 kDa synaptic vesicle protein — and HGNC still records it.
#: Deleting it would discard something true, would have to be done in two places,
#: and would not survive the next `populate_aliases --overwrite`. It would also be
#: WORSE for the reader than this is: a deleted alias makes `p65` resolve to
#: nothing silently, where this resolves to nothing and says why.
AMBIGUOUS_ALIASES = {
    "p65": ("RELA", "SYT1"),
}


def _resolve_target_how(name):
    """``(target, how)`` — find a public target, and say WHICH kind of name did it.

    ``how`` is one of ``gene_symbol``, ``protein_name`` or ``alias``, and it is not
    decoration. An alias match is the weakest of the three and the only one that can
    silently mean a different protein: ``p65`` is a historical name for
    synaptotagmin-1 AND the everyday name for RELA, so four benchmark papers
    studying NF-κB were attached to SYT1 and offered synaptotagmin antibodies, with
    nothing in the reply to show the switch had happened.

    There is no rule that separates that from ``p62`` → SQSTM1, which is the same
    mechanism working correctly and the reason this lookup exists: both are short,
    both are real, both match exactly. A minimum length or a word boundary rejects
    either both or neither. What distinguishes them is not in our data at all —
    RELA is not in the dataset to compete for the name.

    So the fix is not to guess better, it is to SAY. The caller is told which kind
    of name resolved, so a reagent attached to a gene by a historical synonym is
    disclosed rather than asserted, and a reader studying the other protein can see
    it in one line instead of following the alternatives.
    """
    from pipeline.models import Target
    name = (name or "").strip()
    if not name:
        return None, None
    t = Target.objects.filter(gene_name__iexact=name).first()
    how = "gene_symbol"
    if t is None:
        t = Target.objects.filter(protein_name__iexact=name).first()
        how = "protein_name"
    if t is None:
        how = "alias"
        # An alias known to be shared with a gene we do not hold resolves to
        # NOTHING, rather than confidently to the one we happen to have.
        if name.lower() in AMBIGUOUS_ALIASES:
            return None, "ambiguous_alias"
        # aliases are stored comma-separated on the target
        for cand in Target.objects.filter(aliases__icontains=name):
            if any(a.strip().lower() == name.lower()
                   for a in (cand.aliases or "").split(",")):
                t = cand
                break
    if t is None or not _target_is_public(t):
        return None, None
    return t, how


def _resolve_target(name):
    """Find a public target by gene symbol, HGNC alias, or protein name.

    A caller naming the PROTEIN ("alpha-synuclein") or a common alias ("Beclin-1",
    "p62") must still reach our data — reporting SNCA as absent because the paper
    called it alpha-synuclein would be a false negative on the one thing this
    connector exists to answer. Matching a name against our own target list is a
    lookup, not text parsing: it is bounded by the data and cheap to keep correct.
    """
    return _resolve_target_how(name)[0]


#: Characters that separate independent NAMES inside one target label. A paper
#: writes "Nrf2 (NFE2L2)" or "SQSTM1/p62" as a single string; each side is a name
#: in its own right. Hyphens are NOT here on purpose — "alpha-synuclein" is one
#: name, not two.
_NAME_SPLIT = _re.compile(r"[()\[\]{}/,;|]+")
#: Leading noise a methods section puts in front of the name it actually means.
#: ONLY "anti" — a Greek-letter prefix is part of the name ("Alpha-synuclein" is
#: not "synuclein"), so those are transliterated below, never stripped.
_NAME_PREFIX = _re.compile(r"^anti[\s\-]+", _re.I)
#: Greek letters as papers print them -> as our protein names spell them.
_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon"}
#: Words that are never a gene and would only cost a query.
_NAME_STOPWORDS = {"antibody", "antibodies", "protein", "human", "mouse", "rat",
                   "monoclonal", "polyclonal", "recombinant", "ab", "mab", "pab"}

#: Ordinary English that turns up inside a reagent label. Only the last-resort word
#: split below is measured against this — see ``_target_names``.
_PHRASE_STOPWORDS = {
    "and", "the", "for", "with", "from", "against", "anti", "conjugated",
    "labeled", "labelled", "conjugate", "clone", "cat", "catalog", "catalogue",
    "kit", "assay", "reagent", "dye", "stain", "probe", "marker", "control",
    "total", "full", "length", "chain", "light", "heavy", "alpha", "beta",
    "gamma", "delta", "type", "form", "subunit", "receptor", "domain",
}

#: A word lifted OUT of a longer phrase is much weaker evidence than the phrase
#: itself, so it clears a higher bar. Two characters is below it, which is what
#: ``"ApoTrack (Cytochrome c in Apoptosis)"`` needed: the split emitted ``in``, and
#: ``IN`` is a correct, current HGNC alias for CD44 — the Indian blood group
#: antigen. The alias table was right; the tokeniser handed it a preposition, and a
#: reagent that has nothing to do with CD44 came back attached to it.
_MIN_SPLIT_WORD = 3


def _target_names(text):
    """The candidate NAMES inside one free-text target label, most specific first.

    The lookup used to match the whole label and nothing else, so a target written
    the way papers actually write it never resolved:

        NFE2L2 / nfe2l2 / Nrf2   -> resolved
        Nrf2 (NFE2L2)            -> did NOT
        SQSTM1/p62               -> did NOT

    Both failing strings contain a name that resolves on its own, and in the
    reported case both genes were ALSO in the caller's ``genes`` list and came back
    with recommendations in the same response — so the connector held the answer
    and still reported the reagent as bare "not in dataset". Splitting the label is
    a lookup against our own bounded vocabulary, not text parsing.

    The whole label always goes first: protein names contain spaces ("Alpha-synuclein",
    "Microtubule-associated protein tau") and must never be split before being tried.
    """
    text = (text or "").strip()
    if not text:
        return []
    out, seen = [], set()

    def add(v, from_split=False):
        v = _NAME_PREFIX.sub("", (v or "").strip(" \t.:–—-")).strip()
        # A bare number or single letter is not a gene; a stopword only costs queries.
        if len(v) < 2 or v.isdigit() or v.lower() in _NAME_STOPWORDS:
            return
        # A word taken out of a phrase has to clear a higher bar than the phrase
        # did, because the phrase was the caller's own reading and this is ours.
        if from_split and (len(v) < _MIN_SPLIT_WORD
                           or v.lower() in _PHRASE_STOPWORDS):
            return
        variants = [v]
        # Papers print "α-synuclein"; our protein names spell it "Alpha-synuclein".
        spelled = v
        for greek, latin in _GREEK.items():
            spelled = spelled.replace(greek, latin)
        if spelled != v:
            variants.append(spelled)
        for cand in variants:
            if cand.lower() not in seen:
                seen.add(cand.lower())
                out.append(cand)

    add(text)
    segments = [s for s in _NAME_SPLIT.split(text) if s.strip()]
    for s in segments:                     # "Nrf2 (NFE2L2)" -> "Nrf2", "NFE2L2"
        add(s)
    for s in segments:                     # last resort: "Tau protein" -> "Tau"
        for w in s.split():
            add(w, from_split=True)
    return out


def _resolve_target_text(text, gene_index=None):
    """Resolve a free-text target label to a public target.

    Returns ``(target, matched_on, how)``, where ``matched_on`` is the name inside
    the label that actually resolved and ``how`` says what KIND of name it was —
    so a caller can see why a reagent was attached to a gene rather than having to
    trust it. See ``_resolve_target_how``.

    ``gene_index`` is the caller's own ``genes`` list, already resolved. Checking it
    first is both cheaper and safer than a database lookup: it is a vocabulary the
    caller supplied for this very paper, so matching against it cannot drag in a
    gene the paper never mentioned — which is also why a hit there is reported as
    ``caller_gene_list`` and never needs the alias caveat.
    """
    ambiguous = None
    for name in _target_names(text):
        if gene_index:
            t = gene_index.get(name.lower())
            if t is not None:
                return t, name, "caller_gene_list"
        t, how = _resolve_target_how(name)
        if t is not None:
            return t, name, how
        # Remembered, not returned yet: a name later in the label may still resolve
        # cleanly, and a real match outranks a collision. "NF-κB p65" has nothing
        # else, so the ambiguity is what comes back — but `SQSTM1/p65` would resolve
        # on SQSTM1 and never mention it.
        if how == "ambiguous_alias" and ambiguous is None:
            ambiguous = name
    if ambiguous is not None:
        return None, ambiguous, "ambiguous_alias"
    return None, None, None


def _gene_index(genes):
    """``{name -> Target}`` for the gene symbols the CALLER said the paper studies.

    Every form of each resolved gene is keyed — the string as supplied, the
    canonical symbol, and each stored alias — so joining a reagent's target label
    against the paper's own gene list works whichever name the methods section used.

    A gene is resolved the same way a reagent's target label is — name by name — so
    a caller writing "SQSTM1/p62" in the gene list is not silently dropped from
    ``gene_hits`` while the identical string on a reagent resolves fine.

    Returns ``(index, [(supplied, target)])``; unresolved genes carry a ``None``
    target so the caller's list order survives.
    """
    idx, out = {}, []
    for g in genes:
        t, matched_on, _how = _resolve_target_text(g)
        out.append((g, t))
        if t is None:
            continue
        forms = ([g, matched_on, t.gene_name]
                 + [a.strip() for a in (t.aliases or "").split(",")])
        for f in forms:
            f = (f or "").strip().lower()
            if f and f not in idx:
                idx[f] = t
    return idx, out


def _gene_hit(gene, target=None):
    A = _api()
    t = target if target is not None else _resolve_target(gene)
    if t is None:
        return None
    hit = {
        "gene": t.gene_name,
        "gene_page_url": f"{A.BASE_URL}/antibodies/{t.gene_name}/",
        "has_recommendations": A._target_has_recommendations(t),
        "published_antibodies": _published_antibodies().filter(target=t).count(),
    }
    # Present ONLY when there is something to say: the name the CALLER used, when it
    # is not already the canonical symbol. Nothing else on a gene hit carries the
    # caller's own string, so that — not the sub-name that resolved — is the useful
    # half. It used to sit here as an explicit null on every hit, which reads as a
    # field that never works next to a `target_matched_on` that does.
    if (gene or "").strip().lower() != t.gene_name.lower():
        hit["matched_on"] = gene
    return hit


#: Reagent roles that are NOT primary antibodies under test. The controls rubric
#: excludes all of them. This is the field a caller can simply state and a text
#: parser could only ever guess at — which is why an isotype control used to end up
#: in the antibody table.
_NON_PRIMARY_ROLES = {"isotype_control", "isotype", "secondary", "loading_control",
                      "loading", "tag", "dye", "stain"}


def _clean(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


#: How the applications a paper reports get written, mapped onto the four codes
#: every verdict is keyed by. Optional on a reagent — but when the caller supplies
#: it, an alternative can be matched to the application it is actually needed for.
_APP_ALIASES = {
    "wb": "WB", "western": "WB", "western blot": "WB", "westernblot": "WB",
    "immunoblot": "WB", "immunoblotting": "WB", "blot": "WB",
    "ip": "IP", "immunoprecipitation": "IP", "co-ip": "IP", "coip": "IP",
    "if": "IF", "icc": "IF", "icc-if": "IF", "immunofluorescence": "IF",
    "immunocytochemistry": "IF",
    # Tissue. All of these land on the IF column — see _TISSUE_APPS for why that
    # is a substitution and not a synonym. They are spelled out because `IHC-IF`,
    # `IHC-P` and `IHC-Fr` previously matched nothing at all and were DROPPED: the
    # row then showed no application, so the OGA verdict could not be scoped to
    # what the paper did and a concern fell back to all four applications. A
    # silently discarded application is worse than a substituted one.
    # TISSUE TERMS ARE DELIBERATELY ABSENT FROM THIS TABLE. They used to resolve
    # to IF; they now resolve to nothing, and `_TISSUE_APPS` catches them so they
    # are still RECOGNISED and never silently dropped. See the note there.
    # Staining, preparation unstated — see _AMBIGUOUS_APPS. Counted, because both
    # sides of the ambiguity land on IF anyway and dropping the term loses the
    # application entirely; flagged, because which side it is decides whether the
    # tissue caveat applies.
    "immunostaining": "IF", "immunostain": "IF", "immunostained": "IF",
    "immunolabelling": "IF", "immunolabeling": "IF", "immunolabel": "IF",
    "immunolabelled": "IF", "immunolabeled": "IF",
    "fc": "FC", "flow": "FC", "flow cytometry": "FC", "facs": "FC",
}


#: TISSUE applications: RECOGNISED, and given no verdict.
#:
#: **The axis is antigen presentation — the sample — not the detection
#: chemistry.** IHC and IHC-IF are both tissue: fixed, embedded or frozen,
#: sectioned, usually antigen-retrieved. They group TOGETHER whether the label is
#: chromogenic or fluorescent, and `IHC-IF` is the one that catches people out
#: because it ends in `IF`. ICC-IF is cultured cells on a coverslip, and
#: **ICC-IF is what OGA tests** — every IF verdict in the dataset is an ICC-IF
#: result (``_APP_IMG["IF"] == "ICC-IF"``).
#:
#: These terms were previously FOLDED onto the IF column, disclosed by a note.
#: That traded one wrong answer for another: an epitope can be available in one
#: preparation and not the other, so a tissue use scored against a cultured-cell
#: verdict is a verdict about a different experiment, and a caveat underneath does
#: not make the number in the column right.
#:
#: They are not dropped either — dropping was the original defect, and it left the
#: row unable to say what the paper did, so the OGA verdict was reported across
#: all four applications and a concern was judged against applications the paper
#: never used.
#:
#: So: recognised, named on the row, scoped to nothing, and answered with CONTEXT
#: — what OGA did find, in the applications it did test. "No tissue verdict; this
#: antibody was tested and not recommended in three other applications" is a
#: sentence a reader can act on. A borrowed verdict is not.
#:
#: The browser extension does the same thing, which is the point: the two tools
#: must not answer one question differently.
_TISSUE_APPS = {"ihc", "immunohistochemistry", "ihc-p", "ihc-fr", "ihc-f",
                "ihc-if", "if-ihc", "immunohistofluorescence",
                "immunohistochemistry-if", "mihc", "ihc-frozen"}

_TISSUE_APP_NOTE = (
    "The paper used this antibody on TISSUE ({what}), and OGA has no verdict for "
    "tissue. Every OGA IF result is an ICC-IF result — cultured cells on a "
    "coverslip — and antigen presentation differs: tissue is fixed, embedded and "
    "antigen-retrieved, so an epitope can be available in one preparation and not "
    "the other. IHC and IHC-IF group together on that axis; the detection label is "
    "not what separates them. This use is therefore NOT scored against the IF "
    "verdict. What is known about the antibody is in `oga_result`, which covers "
    "the applications OGA did test — report that as context, and be explicit that "
    "it is not a result in this preparation.")

#: Staining terms that name the assay but NOT the preparation. "Immunostaining"
#: is done on sections and on coverslips alike, and the whole point of the tissue
#: note above is that those are the two sides of the antigen-presentation line.
#: So they resolve — both sides land on IF, and dropping the term would lose the
#: application altogether — and the row says the preparation is unstated rather
#: than picking one. Same rule as the shared gene alias: a resolver that cannot
#: pick correctly must disclose, not guess.
_AMBIGUOUS_APPS = {"immunostaining", "immunostain", "immunostained",
                   "immunolabelling", "immunolabeling", "immunolabel",
                   "immunolabelled", "immunolabeled"}

#: The unambiguous CELL side — what OGA's IF verdicts are actually measured on.
#: Naming one of these alongside a vague term settles the question, so the note
#: above stays quiet.
_CELL_APPS = {"icc", "icc-if", "immunocytochemistry"}

_AMBIGUOUS_APP_NOTE = (
    "{what} names the assay but not the preparation — it is done on tissue "
    "sections and on cultured cells alike, and that is the distinction that "
    "matters here. Counted as IF. OGA's IF verdicts are ICC-IF results on "
    "cultured cells, so if this was tissue the verdict is indicative for it "
    "rather than a result in it. The Methods are where the preparation is stated; "
    "say which it was, or say that the paper does not.")

_UNKNOWN_APP_NOTE = (
    "Not counted in `applications_in_paper`, because these are not application "
    "terms this connector recognises: {what}. Verdicts are keyed WB / IP / "
    "IF (ICC-IF) / FC — name the assay in those terms (or as IHC, ICC-IF, "
    "immunoblot, flow cytometry …) or the OGA verdict cannot be scoped to what "
    "the paper actually did.")


def _application_notes(values):
    """What to disclose about how the caller's application terms were read.

    Two things are worth a line: a term that landed on a column measured on a
    different preparation, and a term that landed on nothing at all. The second
    used to be silent, which is the worse of the two — a dropped application
    leaves the row unable to say what the paper did with the antibody, so the OGA
    verdict is reported across all four applications and a concern is judged
    against applications the paper never used.
    """
    tissue, ambiguous, unknown, out = [], [], [], []
    named_cells = False
    for v in (values or []):
        raw = (str(v) if v is not None else "").strip()
        key = raw.lower()
        if not key:
            continue
        if key in _CELL_APPS:
            named_cells = True
        if key in _TISSUE_APPS:
            if raw not in tissue:
                tissue.append(raw)
        elif key in _AMBIGUOUS_APPS:
            if raw not in ambiguous:
                ambiguous.append(raw)
        elif (key not in _APP_ALIASES and raw.upper() not in _APPS
                and raw not in unknown):
            unknown.append(raw)
    if tissue:
        out.append(_TISSUE_APP_NOTE.format(what=", ".join(tissue)))
    # …unless the caller ALSO named the preparation, in which case the ambiguity is
    # already settled and asking again is noise.
    if ambiguous and not (tissue or named_cells):
        out.append(_AMBIGUOUS_APP_NOTE.format(what=", ".join(ambiguous)))
    if unknown:
        out.append(_UNKNOWN_APP_NOTE.format(
            what=", ".join(f'"{u}"' for u in unknown)))
    return out


def _out_of_scope_apps(values):
    """Applications this connector RECOGNISES and holds no verdict for.

    Tissue work, today. Kept apart from `_normalise_apps` (which answers "which of
    our four") and from the unknown-term list (which answers "what did we fail to
    read at all"), because they are three different states and a row that cannot
    tell them apart cannot say anything useful about any of them.
    """
    out = []
    for v in (values or []):
        raw = (str(v) if v is not None else "").strip()
        if raw and raw.lower() in _TISSUE_APPS and raw not in out:
            out.append(raw)
    return out


def _normalise_apps(values):
    """Caller-reported applications -> the ``{WB, IP, IF, FC}`` verdict vocabulary.

    A tissue term resolves to NOTHING here on purpose — see ``_TISSUE_APPS``. It is
    still recognised, by ``_out_of_scope_apps``.
    """
    out = set()
    for v in (values or []):
        v = (str(v) if v is not None else "").strip().lower()
        if not v:
            continue
        code = _APP_ALIASES.get(v) or (v.upper() if v.upper() in _APPS else None)
        if code:
            out.add(code)
    return out


def _order_alternatives(rows, wanted):
    """Rank candidate alternatives so a cap keeps the USEFUL ones.

    Two things were wrong with ordering by supplier name:

      * For a gene whose alternatives are mostly from one supplier, an alphabetical
        cap returned that supplier's catalogue and nothing else — every other
        supplier's recommended reagents were invisible, which reads as a shortlist
        and is really a sort artefact.
      * It ignored the application. An antibody recommended for FC only is not an
        alternative for a western blot, but it could outrank three WB-recommended
        antibodies on supplier name alone.

    So RANK first, then diversify: antibodies recommended for an application the
    paper actually used come first (when the caller said which), then the ones
    recommended for MORE applications. Suppliers take turns only to break ties
    between equally-ranked antibodies — a cap then spans the field without letting
    turn-taking promote a narrower antibody over a broader one.

    Rank has to lead, because the shortlist is truncated: a reader told they are
    seeing "5 of 10" will assume they are the best 5. Interleaving suppliers first
    made that untrue — it spent slots on single-application antibodies while
    broader ones fell outside the cap.
    """
    def tier(r):
        wants = len(wanted & set(r["recommended_for"])) if wanted else 0
        return (-wants,                        # covers the paper's application
                -len(r["recommended_for"]))    # then: recommended for more of them

    def within(r):                     # stable, so a tie is not arbitrary
        return (r["antibody"] or "").lower()

    out = []
    for t in sorted({tier(r) for r in rows}):
        buckets = {}
        for r in sorted((r for r in rows if tier(r) == t), key=within):
            buckets.setdefault((r["supplier"] or "").lower(), []).append(r)
        order = sorted(buckets)
        while any(buckets[s] for s in order):
            for s in order:
                if buckets[s]:
                    out.append(buckets[s].pop(0))
    return out


def _recommended_alternatives(target, wanted=None, cap=5, exclude_pks=()):
    """Compact rows for the antibodies OGA RECOMMENDS against ``target``.

    This is the answer to the question a reader actually has when their paper's
    antibody is untested: "so what should I have used?" It is per-GENE, and stays
    within the per-gene scope policy — the same question ``antibodies_by_support``
    answers. Kept deliberately small (no assessment blocks, no evidence, one image)
    because it can appear for many reagents at once.

    When ``wanted`` names applications, antibodies that cover none of them are
    EXCLUDED, not merely ranked below: an antibody recommended for FC is not an
    alternative for a western blot, so offering it spends a slot on a row the caller
    is told to discard. Filtering also makes the empty case sayable — a gene that is
    characterised but has nothing recommended for the application in hand is a real
    finding, and ranking hid it behind FC-only rows sorted to the top.

    ``exclude_pks`` drops antibodies the paper itself used and OGA does not support
    for what it used them for. Nothing is an alternative to itself, and the case is
    real rather than theoretical: a reagent declined for WB and recommended for IP,
    in a paper that did both, is a concern on the WB and would otherwise be offered
    as the swap for its own shortfall. A reagent the paper used that OGA DOES
    support stays listed — it is a genuine pointer, and the reader can see it is
    already in their hands.

    Returns ``(rows, total, matching)``:
      * ``total``   — every recommended antibody on the gene, whatever the
        application. Not the length of the capped list: reporting the cap as the
        total understated a gene with eleven recommended antibodies as having five.
      * ``matching`` — how many cover ``wanted``; ``None`` when no application was
        named. This is the honest denominator for "N of M" once the caller has said
        what they need, where ``total`` would overstate what is available to them.
    """
    from django.db.models import Q
    A = _api()
    qs = (_published_antibodies().filter(target=target)
          .filter(Q(wb_recommended=True) | Q(ip_recommended=True)
                  | Q(if_recommended=True) | Q(fc_recommended=True))
          .order_by("company__name", "catalogue_number"))
    if exclude_pks:
        qs = qs.exclude(pk__in=list(exclude_pks))
    rows = []
    for ab in qs:
        apps = [app for app in _APPS if getattr(ab, _APP_REC[app])]
        img = next((i for i in ab.publication_images.all()
                    if _IMG_APP.get(i.application_type) in apps and i.image), None)
        rows.append({
            "antibody": ab.catalogue_number,
            "supplier": (ab.company.display_name or ab.company.name) if ab.company else None,
            "rrid": ab.rrid or None,
            "recommended_for": apps,
            "product_link": ab.supplier_url or None,
            "image": ((img.image.url if img.image.url.startswith("http")
                       else f"{A.BASE_URL}{img.image.url}") if img else None),
        })
    total = len(rows)
    if wanted:
        rows = [r for r in rows if wanted & set(r["recommended_for"])]
    matching = len(rows) if wanted else None
    return _order_alternatives(rows, wanted or set())[:cap], total, matching


def _is_concern_hit(applications, apps):
    """OGA tested it and did not support it FOR WHAT THE PAPER DID WITH IT.

    The same question ``scan_controls``' ``_is_concern`` asks one layer up, and
    deliberately the same answer: two readings of "is this a concern" is how a
    reagent comes to be offered alternatives on one surface and not the other.
    Scoped to the applications the caller named, because a verdict is per
    application — an antibody the paper blotted with, declined for flow cytometry
    and supported for WB, is not a concern about this paper. With no applications
    named there is nothing to scope to, so any application counts; that is the
    honest fallback and `coverage.limits` already says they were not supplied.
    """
    if apps:
        return any(applications.get(a) == "not_recommended" for a in apps)
    return any(v == "not_recommended" for v in applications.values())


def _alternatives_context(target, wanted=None, exclude_pks=()):
    """What OGA recommends against this gene — the "so what should I use?" answer.

    Called for TWO kinds of reagent, and the distinction is the reader's, not this
    function's:

      * one the dataset does not hold at all — untested is not a verdict, and the
        decision in front of the reader is whether a characterised alternative
        exists for the same target;
      * one the dataset DOES hold and does not support for what the paper used it
        for. That reader has the sharper problem of the two: they are not waiting
        on evidence, they have it, and it goes against the reagent in their hands.
        Offering alternatives only to the first was the gap — the case that most
        needs somewhere to go next was the one with nowhere.

    Neither is a criticism of the reagent the paper used. It is a pointer to data
    the reader can act on, and the caller is told to present it as one.

    ``target`` is an already-resolved public target. Returns the gene page plus the
    recommended antibodies — because an antibody being untested is NOT a verdict,
    and the reader's real decision is whether a characterised alternative exists for
    that same target. That link is the single most actionable thing in the whole
    response, and it was missing: the reagent was reported as absent with nowhere to
    go next.

    ``wanted`` is the set of applications the paper used this target's antibodies
    for, POOLED across every reagent that points at this gene — the shortlist is a
    property of the gene and is listed once, so it has to serve every reagent that
    points at it. A gene blotted and stained therefore pools to {WB, IF} and keeps
    antibodies recommended for either.
    """
    A = _api()
    alts, total, matching = _recommended_alternatives(
        target, wanted=wanted, exclude_pks=exclude_pks)
    return {
        "gene": target.gene_name,
        "gene_page_url": f"{A.BASE_URL}/antibodies/{target.gene_name}/",
        "gene_has_recommendations": A._target_has_recommendations(target),
        "alternatives": alts,                  # the capped SHORTLIST
        "alternatives_total": total,           # every recommended antibody on the gene
        "alternatives_matching": matching,     # …of those, how many cover `wanted`
        "report_dois": _report_dois(_reports(target)),
    }


def _reagent_candidates(reagents, cap=200):
    """One candidate per REAGENT, carrying every identifier form to try for it.

    Grouping by reagent (rather than emitting one candidate per identifier) is what
    stops a single antibody being reported twice. A reagent given as
    ``{"identifier": "ab212184", "rrid": "AB_NOTOURS"}`` has one form that resolves
    and one that does not; flattened, that produced a validated hit AND an
    "untested" row for the same antibody. A reagent resolves, or it does not.
    """
    out, seen = [], set()
    for r in (reagents or []):
        if not isinstance(r, dict):
            continue
        if str(r.get("role") or "primary").strip().lower() in _NON_PRIMARY_ROLES:
            continue
        forms, kinds = [], []
        for key in ("identifier", "catalogue", "rrid"):
            val = _clean(r.get(key))
            if val and val.lower() not in {f.lower() for f in forms}:
                forms.append(val)
                kinds.append("RRID" if key == "rrid" else "caller-supplied")
        if not forms:
            continue
        dedupe = tuple(sorted(f.lower() for f in forms))
        if dedupe in seen:                 # the same reagent listed twice
            continue
        seen.add(dedupe)
        out.append({"identifier": forms[0], "kind": kinds[0],
                    "forms": forms, "reagent": r})
    return out[:cap], len(out) > cap


def check_manuscript(reagents=None, genes=None, cap=200,
                     paper_doi=None, paper_pmid=None, paper_title=None,
                     paper_year=None):
    """Resolve the reagents a caller read out of a paper against the OGA dataset.

    The caller does the reading; this does the looking-up. ``reagents`` is a list of
    dicts, each with any of ``identifier`` / ``catalogue`` / ``rrid``, plus
    ``target``, ``role`` (primary / isotype_control / secondary / loading_control /
    tag / dye) and ``figures``. Non-primary roles are dropped here — the rubric
    excludes them, and ``role`` is exactly the thing the old text parser could not
    determine.

    ``genes`` is the gene symbols the paper discusses; each is looked up for a
    public gene page. Antibody hits are grouped by verdict (recommended /
    not_recommended / not_tested) with an explicit ``not_in_dataset`` list.
    """
    from django.db.models import Q
    cand, reagents_truncated = _reagent_candidates(reagents, cap=cap)

    # Resolved ONCE per call, not per reagent: it is one dictionary lookup against
    # a file, and asking three times is how a reply comes to report one paper in
    # one column and another paper in the next.
    citeab_state, citeab_paper = citeab.for_paper(
        doi=paper_doi, pmid=paper_pmid, title=paper_title, year=paper_year)

    grouped = {"recommended": [], "not_recommended": [], "not_tested": []}
    not_in_dataset = []
    # Reagents whose DECLARED target is not the protein they share a name with.
    # Collected as they are resolved so the reply-level note is served only when
    # there is something for it to be about -- a standing note on every reply is
    # one a caller learns to skip, and this one has to be read.
    confusion_blocks = []

    # The caller's gene list is resolved FIRST so unresolved reagents can be joined
    # against it. It is the paper's own vocabulary, which makes it both the cheapest
    # and the most trustworthy thing to match a target label against.
    supplied_genes = [g for g in ((_clean(x) for x in (genes or []))) if g]
    genes_truncated = len(supplied_genes) > cap
    gene_idx, resolved_genes = _gene_index(supplied_genes[:cap])
    gene_hits = [h for h in (_gene_hit(g, t) for g, t in resolved_genes) if h]

    resolved = {}      # target label (lower) -> (Target | None, matched_on)
    pending = []       # entries awaiting their gene's alternatives
    wanted_apps = {}   # gene name -> applications the paper used it for
    concern_pks = {}   # gene name -> pks of the paper's own unsupported reagents

    # Resolve every candidate in a single query, then match case-insensitively.
    by_cat, by_rrid, by_cat_collapsed = {}, {}, {}
    if cand:
        q = Q()
        for c in cand:
            for given in c["forms"]:
                q |= (_identifier_q(given, "catalogue_number")
                      | _identifier_q(given, "rrid"))
        for ab in _typeset_tolerant(_published_antibodies()).filter(q):
            if ab.catalogue_number:
                key = ab.catalogue_number.lower()
                by_cat.setdefault(key, ab)
                # Keyed on the stored number AND on it with its spaces taken out,
                # because the dict is the second half of the match: widening the
                # query alone still misses when `107 102` is on file and the paper
                # printed `107102`, since the lookup below asks for the input form.
                by_cat.setdefault(key.replace(" ", ""), ab)
                # …and once more with every separator gone, for the number whose
                # own hyphen the publisher moved. Kept in its OWN dict: a collapsed
                # key is the weakest form there is, and merging it in would let it
                # answer a lookup that an exactly-stored number should have won.
                collapsed = manuscript.collapse_identifier(ab.catalogue_number)
                if collapsed:
                    by_cat_collapsed.setdefault(collapsed.lower(), ab)
            if ab.rrid:
                by_rrid.setdefault(ab.rrid.lower(), ab)

    seen = set()
    for c in cand:
        # Try EVERY form this reagent was given before concluding it is unknown.
        ab = None
        for given in c["forms"]:
            for form in manuscript.resolution_variants(given):
                key = form.lower()
                ab = (by_rrid.get(key) or by_cat.get(key)
                      or by_cat.get(key.replace(" ", "")))
                if ab is not None:
                    break
            if ab is not None:
                break
        if ab is None:
            # Last resort, and only once every exact form has failed: compare with
            # all punctuation gone from both sides.
            for given in c["forms"]:
                for key in _collapsed_keys(given):
                    ab = by_cat_collapsed.get(key)
                    if ab is not None:
                        break
                if ab is not None:
                    break
        r = c["reagent"]
        # Asked for EVERY reagent, resolved or not, and asked with the same
        # expanded forms the dataset lookup used. Two reasons it is not confined
        # to the unresolved branch: an antibody could in principle carry both a
        # record and a notice (none of the listed codes does today), and a rule
        # applied on one branch is a rule for one branch.
        confusion = confusions.for_identifier(
            [v for given in c["forms"] for v in manuscript.resolution_variants(given)],
            doi=paper_doi, pmid=paper_pmid, title=paper_title, year=paper_year,
            doi_supplied=bool(paper_doi or paper_pmid
                              or (paper_title and paper_year)))
        if confusion:
            confusion_blocks.append(confusion)
        if ab is None:
            # Keep the caller's reading of an unresolved reagent — its target is the
            # only one there is, since the database has nothing to supply.
            entry = {"identifier": c["identifier"], "matched_as": c["kind"]}
            if confusion:
                # Placed BEFORE the target resolution below, so a reader of the
                # entry meets the mismatch before the alternatives — and so the
                # key is present even on an entry whose target did not resolve.
                entry["target_confusion"] = confusion
            tgt = _clean(r.get("target"))
            if tgt:
                entry["target"] = tgt
                # The reagent is untested, but its TARGET may be characterised. If
                # so, say where to look and what was recommended instead — this is
                # the reader who has a decision to make. The label is matched name
                # by name, so "Nrf2 (NFE2L2)" reaches NFE2L2 the way the bare symbol
                # already did.
                found = resolved.get(tgt.lower(), _MISSING)
                if found is _MISSING:
                    found = _resolve_target_text(tgt, gene_idx)
                    resolved[tgt.lower()] = found
                t, matched_on, how = found
                if t is None and how == "ambiguous_alias":
                    # No gene, and therefore no alternatives — that suppression IS
                    # the fix. Say why, though: silence here reads as "OGA has never
                    # worked on this target", which for RELA is true and for a reader
                    # who meant SYT1 is not.
                    entry["target_ambiguous"] = {
                        "matched_on": matched_on,
                        "candidates": list(AMBIGUOUS_ALIASES[matched_on.lower()]),
                    }
                if t is not None:
                    entry["gene"] = t.gene_name
                    if matched_on and matched_on.lower() != tgt.lower():
                        # Say WHICH name in the label resolved, so attaching a
                        # reagent to a gene is auditable rather than asserted.
                        entry["target_matched_on"] = matched_on
                    # And say what KIND of name it was. Only present for an alias,
                    # because that is the only one worth a reader's attention: it is
                    # how four papers studying RELA were attached to SYT1 on the
                    # shared historical name `p65`. The note tells the caller to
                    # state the mapping out loud when this is set.
                    if how == "alias":
                        entry["target_matched_via"] = "alias"
                    apps = _normalise_apps(r.get("applications"))
                    wanted_apps.setdefault(t.gene_name, set()).update(apps)
                    pending.append((entry, t))
            if r.get("figures"):
                entry["figures"] = list(r["figures"])
            not_in_dataset.append(entry)
            continue
        if ab.pk in seen:
            continue
        seen.add(ab.pk)
        enriched = _enrich(ab, _axes_for([ab]))
        hit = _manuscript_hit(c["identifier"], c["kind"], enriched)
        if confusion:
            # Unreachable on today's data and correct anyway: which protein an
            # antibody is raised against comes before how well it detects it, so
            # a verdict must never be served without the mismatch beside it.
            hit["target_confusion"] = confusion
        if r.get("figures"):
            hit["reported_figures"] = list(r["figures"])
        # Guarded on the RRID being truthy: `_enrich` sets it to None for an
        # antibody that has none, and `.get(None)` would attach one record to
        # every such reagent at once.
        if citeab_paper and hit.get("rrid"):
            hit.update(citeab.facts_for(citeab_paper.get(hit["rrid"]),
                                        _normalise_apps, _out_of_scope_apps,
                                        _application_notes))
        grouped[_overall_bucket(enriched["assessment"])].append(hit)
        # A reagent OGA tested and does NOT support for what the paper used it for
        # gets the gene's alternatives too. It used to be untested reagents only,
        # which offered the pointer to the reader still waiting on evidence and
        # withheld it from the one who already has it and has it against them.
        apps = _normalise_apps(r.get("applications"))
        if ab.target_id and _is_concern_hit(hit["applications"], apps):
            wanted_apps.setdefault(ab.target.gene_name, set()).update(apps)
            concern_pks.setdefault(ab.target.gene_name, set()).add(ab.pk)
            pending.append((hit, ab.target))

    # Alternatives are a property of the GENE, not of each reagent, so they are
    # built ONCE per gene here — after every reagent has been seen, so the shortlist
    # is ranked against every application the paper used that gene's antibodies for
    # — and referenced from the entries above by gene name.
    gene_context, alternatives = {}, {}
    for _entry, t in pending:
        if t.gene_name not in gene_context:
            gene_context[t.gene_name] = _alternatives_context(
                t, wanted=wanted_apps.get(t.gene_name),
                exclude_pks=concern_pks.get(t.gene_name, ()))
    for gene, ctx in gene_context.items():
        if ctx["alternatives"]:
            alternatives[gene] = ctx["alternatives"]
    for entry, t in pending:
        ctx = gene_context[t.gene_name]
        entry["gene_page_url"] = ctx["gene_page_url"]
        entry["target_is_characterised"] = ctx["gene_has_recommendations"]
        # The TOTAL for the gene, not the length of the shortlist. These used to be
        # the same number, which silently understated a gene with eleven recommended
        # antibodies as having five and gave no way to tell it had been shortened.
        #
        # And it counts antibodies RECOMMENDED for at least one application, not
        # antibodies tested — the connector keeps those apart everywhere else, so
        # the field is named for what it holds.
        entry["recommended_alternatives"] = ctx["alternatives_total"]
        entry["alternatives_listed"] = len(ctx["alternatives"])
        # Once the caller has named an application, the gene total is the wrong
        # denominator: "5 of 10" promises four more that would serve them when only
        # one does. Both numbers are carried — the gene total keeps saying how much
        # data exists here, and the scoped count is what "N of M" must be built on.
        universe = ctx["alternatives_total"]
        if ctx["alternatives_matching"] is not None:
            entry["recommended_for_requested_applications"] = ctx["alternatives_matching"]
            universe = ctx["alternatives_matching"]
        entry["alternatives_truncated"] = len(ctx["alternatives"]) < universe

    reply = {
        "antibody_hits": grouped,
        "not_in_dataset": not_in_dataset,
        "characterised_alternatives": alternatives,
        "gene_hits": gene_hits,
        "counts": {
            "recommended": len(grouped["recommended"]),
            "not_recommended": len(grouped["not_recommended"]),
            "not_tested": len(grouped["not_tested"]),
            "not_in_dataset": len(not_in_dataset),
            "genes": len(gene_hits),
            "genes_with_alternatives": len(alternatives),
            "citeab_reagents_with_records": sum(
                1 for bucket in grouped.values() for h in bucket
                if "citeab_applications" in h),
        },
        # Three states, and only one of them is about the paper. See
        # `mcp_servers/common/citeab.py`.
        "citeab": citeab_state,
        "truncated": reagents_truncated or genes_truncated,
        "note": _MAN_NOTE,
    }
    # What the reviews record for THIS PAPER, by whichever way the caller named
    # it. Served even when no reagent the caller sent carries a notice: a caller
    # that passed the paper and a different antibody -- or no antibody at all --
    # is still holding a paper somebody reviewed, and a reply that knows that and
    # does not say it is the worse reply. `[]` is not evidence the paper is
    # clean, and the note on each record says what it is.
    #
    # It asked on the DOI alone until 0.4.2, while this function had `paper_pmid`
    # and `paper_title` in its own signature and was already handing all four to
    # the citation layer -- so a caller who named a paper by PubMed id got the
    # citation answer and silently no notice.
    if paper_doi or paper_pmid or (paper_title and paper_year):
        listed = confusions.for_paper(paper_doi, pmid=paper_pmid,
                                      title=paper_title, year=paper_year)
        if listed:
            reply["paper_target_confusions"] = listed
    note = confusions.reply_note(confusion_blocks)
    if note:
        reply["target_confusion_note"] = note
        reply["counts"]["target_confusions"] = len(confusion_blocks)
    return reply


def _concise_verdict(applications):
    """A short per-application verdict string, e.g.
    "recommended: WB; not recommended: IP/IF; not tested: FC"."""
    order = ["WB", "IP", "IF", "FC"]
    groups = {"recommended": [], "not recommended": [], "not tested": []}
    label = {"recommended": "recommended", "not_recommended": "not recommended",
             "not_tested": "not tested"}
    for app in order:
        st = applications.get(app)
        if st in label:
            groups[label[st]].append(app)
    parts = [f"{k}: {'/'.join(v)}" for k, v in groups.items() if v]
    return "; ".join(parts)


# What every scan_controls result carries with it, so the rules for reading the
# table travel with the table rather than living only in the tool description.
_CONTROLS_NOTE = (
    "OUTPUT: render the ready-made `table` AS-IS (columns: antibody, target, "
    "applications_in_paper, control_status + control figure, oga_tested y/n, "
    "oga_result, image, gene_page). "
    "The table answers five questions in order and you should report them that "
    "way: (1) WHICH ANTIBODY, FOR WHAT — `antibody`, `target`, "
    "`applications_in_paper`. (2) IS THERE INDEPENDENT OGA DATA — `oga_tested`, "
    "and `oga_result_for_paper_applications` for the verdict on what the paper "
    "actually did with it (`oga_result` carries all four applications, which is "
    "the wrong denominator for judging this paper); if the antibody is untested, "
    "`target_is_characterised` says whether OGA has worked on the GENE. "
    "(3) WHAT CONTROLS — `control_status`, `controls[]` (with `control_class`) and "
    "`other_controls`. (4) ARE THEY APPROPRIATE — only `selectivity` tests "
    "selectivity; `detection`, `pseudo` and `orthogonal` do not, and "
    "`controls[].readout_applications` says which application each control was read "
    "out in, so a control in one application is not credited to another. "
    "(5) WHERE TO CHECK — `control_figures` and `control_note` for the paper's own "
    "panels, and `image` / `images` / `report_dois` / `gene_page` for OGA's "
    "independent data. Give the reader (5) every time; it is the whole point. "
    "Where a row carries `application_notes`, say what it says on that row. Either "
    "an application was counted as a different one to reach a verdict — every OGA "
    "IF verdict is an ICC-IF result on cultured cells, so a TISSUE use (IHC, "
    "IHC-IF: same antigen presentation as each other, different from ICC-IF) "
    "borrows it and the verdict is indicative for that use rather than a result in "
    "it — or an application term resolved to nothing, in which case the verdict "
    "shown covers all four applications and is NOT scoped to what the paper did. "
    "CONTROL_STATUS IS THREE-VALUED and each value says a different thing; do NOT "
    "collapse them to yes/no. `demonstrated` = a genetic control for this "
    "antibody's target is in the paper AND the text says this antibody was read "
    "out against the manipulated material — still a CANDIDATE to confirm at the "
    "panel, never proof the control worked. `present_unlinked` = the genetic "
    "control IS there, but the text does not establish that THIS antibody was "
    "tested against it; say that in those words — it is a common and legitimate "
    "state, NOT a missing control, and reporting it as \"no control\" is a false "
    "accusation against a competent paper. `absent` = no genetic manipulation of "
    "this target anywhere in the paper. `paper_control` carries the same answer as "
    "yes / unlinked / no for older renderers; prefer `control_status`. "
    "`control_figures` holds ONLY the panels where THIS antibody can be checked "
    "against manipulated material; a genetic manipulation of the same target that "
    "the text does not tie to this reagent is in `control_figures_unlinked` and is "
    "a DIFFERENT experiment — report it as one, never as a second place to look. "
    "Every row with a control carries `control_note` — one sentence naming where "
    "the control is, what read it out, where the antibody was used and what to "
    "check at the panel — and `controls[].linkage_basis`, which says WHY it counts: "
    "`named` (the paper names this reagent as the detector), `same_panel` (the "
    "control and the antibody's use are one panel, so that panel IS this antibody "
    "on manipulated material) or `stated_readout` (the text names an "
    "antibody-based readout of it). RENDER the note. A control in a DIFFERENT "
    "figure, or in the "
    "supplement, is normal practice and is NOT a defect: papers routinely "
    "establish a reagent in one figure and use it in another. Report the location; "
    "never treat it as a disqualification. Where a row carries "
    "`application_caveat`, the control was read out in an application the paper "
    "does not use the antibody for — say so alongside the `demonstrated`: a "
    "knockout validated by western blot does not validate the same antibody used "
    "for IHC. Where a row carries `linkage_ambiguity`, several antibodies in the "
    "paper share that target and the control names none of them — one of them was "
    "on that panel and the text does not say which, so do not credit them all. "
    "The table holds ONLY antibodies with a candidate control or "
    "independent OGA data. Immediately after it, render `others` as ONE tidy line "
    "(N untested antibodies with no genetic control for their target: name "
    "(target), …) — do NOT add them to "
    "the table, do not enumerate their figures, and do not analyse the whole paper "
    "for them. Untested AND uncontrolled is the DEFAULT across the literature "
    "(~85%): state it once in that one line and move on. For the Image column use "
    "the direct media file(s) in row.image / row.images — NOT embed-card URLs. "
    "If `focus.unsupported_with_characterised_alternatives` is non-empty, that is the "
    "most actionable line in the reply: OGA tested the paper's antibody and the data "
    "is NOT supportive for what the paper used it for, AND the same gene has "
    "antibodies that are. Say both halves in one line per reagent — the verdict, then "
    "the alternatives from `characterised_alternatives[row.gene]` with the gene_page "
    "link — and keep it a pointer to data rather than a verdict on the paper. The "
    "same \"N of M\" and `recommended_for` rules below apply to it. "
    "If `focus.untested_with_characterised_alternatives` is non-empty, say so in one "
    "line per reagent: the paper's antibody has not been independently tested, but "
    "OGA HAS characterised that target — link the row's gene_page and name the "
    "recommended alternatives from `characterised_alternatives[row.gene]` (catalogue + "
    "supplier + which applications they are recommended for); check `recommended_for` "
    "on any row you cite. That list is a SHORTLIST of `alternatives_listed`: when "
    "`alternatives_truncated` is true, say \"N of M\" and point at the gene page for "
    "the rest rather than implying the shortlist is all there is — M being "
    "`recommended_for_requested_applications` when present, else "
    "`recommended_alternatives`. A row with `recommended_for_requested_applications: "
    "0` is a characterised gene with NOTHING recommended for the application the "
    "paper used: say exactly that, and do not reach for an antibody recommended for "
    "something else. Untested is NOT a "
    "criticism of the reagent; this is a pointer to data the reader can act on. "
    "Add prose ONLY for `focus`: antibodies_of_concern (OGA-tested, not recommended for "
    "an application the paper uses — scoped to `applications_in_paper` when the "
    "caller supplied it, any application when it did not — name it, its figure, "
    "result, media image AND "
    "its report_dois, so the reader can check the primary evidence behind a verdict "
    "that may undercut the paper), "
    "uses_not_located (antibodies the Methods name that no figure places: their use "
    "is unlocated, so their controls cannot be checked — say that, and do NOT "
    "report them as uncontrolled), "
    "figures_with_controls (name them so the reader can verify the control does "
    "what the paper implies), controls_demonstrated (name the panel and repeat what "
    "to check) and controls_present_unlinked (name the control, say the paper does "
    "not state this antibody was read out against it, and do NOT report it as a "
    "missing control). If focus is empty, say so in one line and stop. "
    "TWO INDEPENDENT AXES stay separate: control_status / paper_control is a "
    "CANDIDATE the reader "
    "confirms at the figure (presence is not proof) vs oga_result, a database fact. "
    "Only a genetic knockout/knockdown that removes the TARGET tests selectivity, "
    "and only for that same gene; peptide/antigen competition, secondary-only and "
    "isotype are pseudo-controls (a blocking peptide occupies the Fab and abolishes "
    "all binding); a positive/detection control shows detection, not selectivity. "
    "SCOPE — say this whenever you report a paper_control of \"no\" / a "
    "control_status of `absent`: this assessment "
    "counts the GENETIC pillar only (knockout, knockdown, CRISPR, null line). A row "
    "reading `absent` means no genetic selectivity control was found, NOT that the "
    "paper showed no validation at all. Overexpression and recombinant protein "
    "(Uhlen pillar 4) are classed `detection`, orthogonal methods (pillar 2) are "
    "classed `orthogonal`, and neither sets paper_control — but where the paper "
    "performed one it is in that row's `other_controls` as {figure, type, class}. "
    "Report those in the same line as the \"no\", by name: a paper showing a peptide "
    "competition and one showing nothing at all are not the same paper, and only "
    "`other_controls` tells them apart. "
    "NEVER state that a control worked. This server reads text, not images: it can "
    "say a control is present and where, and it cannot say the signal disappeared, "
    "at the right size, in the right application. Send the reader to the panel. "
    "`not_assessed` IS NOT A VERDICT. It is what `absent` becomes when nothing in "
    "the call shows the figures and legends were read, so the server declined to "
    "make a claim about the paper. Report it as what could not be established "
    "here, never as an absence of controls, and never merge it with `absent`. "
    "`coverage.sections` says what was declared, what the payload evidenced, and "
    "where the two contradict each other. "
    "COVERAGE — read `coverage.limits` BEFORE reporting any `absent`, and state "
    "any line it holds. Every answer here rests on what YOU read out of the paper, "
    "and the four things this needs sit in different sections: which figures use "
    "antibodies (figures + legends), WHICH antibodies (Methods — that is where the "
    "catalogue numbers are), which controls and of what kind (Methods + figures + "
    "legends), and whether this antibody was read out against the control material "
    "(figures + legends, SUPPLEMENTS INCLUDED). With no controls passed in, every "
    "row reads `absent` — which describes your input, not the paper. "
    "not_tested / not_in_dataset means UNTESTED, not unreliable; a verdict in one "
    "application never transfers to another. "
    "When a row carries `target_matched_via: \"alias\"`, the reagent reached that "
    "gene through a stored synonym: name both sides (matched via the alias "
    "\"p65\" → SYT1), because short synonyms are shared between unrelated proteins "
    "and only the reader knows which they meant."
)


# ---------------------------------------------------------------------------
# Reagent <-> control LINKAGE (rubric v9)
#
# Three separable questions used to be collapsed into one boolean:
#
#   1. PRESENCE — does the paper show a genetic manipulation of this target?
#      Answerable from text.
#   2. LINKAGE  — was THIS antibody read out against that manipulated material?
#      Answerable only from the figure legend, and frequently not stated anywhere.
#   3. RESULT   — did the signal actually disappear, at the right size, in the
#      right application? NOT answerable from text under any circumstances.
#
# Until v8 the server answered all three with one "yes"/"no", and it decided them
# by asking whether the reagent's figure list and the control's figure list shared
# a string. That is a proxy for (2), and a bad one: papers do not co-locate
# controls with use. The standard structure is to establish the reagent and its
# controls in one figure or the supplement, then USE the antibody to measure
# something in a different figure. A knockout panel in Fig 1 and the disease
# staining in Fig 4 is competent practice, not a missing control — and the gate
# marked those papers uncontrolled.
#
# A 99-paper blinded benchmark varied ONLY that rule, over 72 scoreable pairs:
#
#   figure lists must intersect (v8)          sens 0.696  spec 0.959  kappa 0.695
#   panel-insensitive (S7a matches S7)        sens 0.913  spec 0.959  kappa 0.872
#   no figure test at all                     sens 1.000  spec 0.959  kappa 0.938
#
# Specificity is IDENTICAL under all three. Co-location bought no discrimination
# whatever; it only destroyed sensitivity. So presence is scoped to the paper, and
# the figure is reported rather than required.
#
# The concern co-location stood in for is real and survives as its own value: a
# paper can knock down gene X to study biology while the anti-X antibody is never
# tested against that knockdown — the knockdown confirmed by qPCR, or blotted with
# a different antibody. `present_unlinked` says exactly that, out loud, instead of
# being folded into a "no" that reads as an accusation.
# ---------------------------------------------------------------------------

#: The values ``control_status`` takes, and what each one claims.
#:
#: ``not_assessed`` is the newest and is not a verdict at all: it is what
#: ``absent`` becomes when nothing in the call shows the figures and legends were
#: read. ``absent`` accuses the authors of showing no genetic control anywhere in
#: their paper, and a caller that read only the Methods produces a whole table of
#: it with no basis whatever. See ``sections.absent_is_serveable``.
CONTROL_STATUSES = ("demonstrated", "present_unlinked", "absent", "not_assessed")

#: How ``control_status`` renders in the legacy ``paper_control`` column. It is
#: three-valued now for one reason: folding ``present_unlinked`` into "no" is the
#: false accusation this change exists to stop, and "no" is what a renderer prints.
_PAPER_CONTROL = {"demonstrated": "yes", "present_unlinked": "unlinked",
                  "absent": "no",
                  # NOT "no". Mapping it there would reinstate in the legacy
                  # column the exact accusation the new status exists to withhold,
                  # and this column is described above as what a renderer prints.
                  "not_assessed": "not_assessed"}

#: The statuses that assert NOTHING about the paper, and so never earn a table row
#: on their own. One list, because three places ask the question and a status that
#: is "not a finding" in one of them and "a finding" in another puts a row in the
#: table that says only that we could not tell.
_NO_CLAIM_STATUSES = ("absent", "not_assessed")

#: How a figure legend names an ANTIBODY-based readout of the manipulated
#: material — the evidence that settles LINKAGE. Stems, because legends say
#: "immunostained" at least as often as "immunostaining".
_READOUT_WORDS = [
    (_re.compile(r"western\s*blot|immuno-?blot", _re.I), "WB"),
    (_re.compile(r"immunoprecipitat|pull-?down", _re.I), "IP"),
    (_re.compile(r"immunofluorescen|immuno-?cyto-?chem|immuno-?histo-?chem"
                 r"|immunostain|immunolabel|confocal", _re.I), "IF"),
    (_re.compile(r"flow\s*cytometr|fluorescence-activated", _re.I), "FC"),
]

#: The same, as the abbreviations a legend uses. CASE-SENSITIVE and anchored on
#: word boundaries: "if" is the commonest word in English and "IF" is an assay,
#: and a case-insensitive match here would call every legend an immunofluorescence.
_READOUT_ABBREV = [
    (_re.compile(r"\bWB\b"), "WB"),
    (_re.compile(r"\b(?:co-?)?IP\b"), "IP"),
    (_re.compile(r"\b(?:IF|ICC|IHC|ICC-IF)\b"), "IF"),
    (_re.compile(r"\b(?:FC|FACS)\b"), "FC"),
]

#: Readouts that are NOT antibody-based. A knockdown confirmed by qPCR is a
#: confirmed knockdown and says nothing about this antibody, so naming one of
#: these can never establish linkage — but it is worth reporting, because it is
#: the difference between "the paper is silent" and "the paper checked it another
#: way". "northern blot" is listed ahead of nothing: no bare "blot" is ever
#: matched as an antibody readout, precisely so this cannot be misread.
_NON_ANTIBODY_READOUT = [
    (_re.compile(r"\b(?:RT-?)?q(?:RT-?)?PCR\b|\bRT-?PCR\b|\bPCR\b", _re.I), "qPCR"),
    (_re.compile(r"RNA-?seq|RNA sequencing|transcriptom", _re.I), "RNA-seq"),
    (_re.compile(r"northern\s*blot", _re.I), "northern blot"),
    (_re.compile(r"mass\s*spec|proteomic", _re.I), "mass spectrometry"),
    (_re.compile(r"\bmRNA\b|transcript level", _re.I), "mRNA level"),
]

#: Figure-label noise. Stripped before a label is reduced to its number.
_FIG_WORDS = _re.compile(
    r"\b(?:supp(?:l(?:ementary|emental)?)?|extended\s*data|si|fig(?:ure)?s?|panels?)\b\.?",
    _re.I)
#: A bare `S` in front of the number is how a supplementary figure is written.
_FIG_S = _re.compile(r"^s(?=\d)", _re.I)


def _figure_key(label):
    """``(supplementary, number)`` for a printed figure label — panel letter gone.

    ``S7a`` and ``S7`` are the same figure; so are ``Figure 3F`` and ``Fig. 3``.
    Used ONLY to annotate whether a control sits where the antibody is used, and
    to say whether it is in the supplement. It gates nothing: that is the whole
    point of this version.
    """
    s = (label or "").strip().lower()
    if not s:
        return None
    supp = bool(_re.search(r"\b(?:supp\w*|extended\s*data|si)\b", s))
    s = _FIG_WORDS.sub(" ", s).strip(" .:,-")
    if _FIG_S.match(s):
        supp, s = True, s[1:]
    m = _re.search(r"\d+", s)
    return (supp, m.group(0)) if m else None


def _is_supplementary(label):
    key = _figure_key(label)
    return bool(key and key[0])


def _figure_label(fig):
    """``3b`` -> ``Fig 3b``; a label that already says so is left alone."""
    fig = str(fig).strip()
    return fig if _re.search(r"fig|panel|table", fig, _re.I) else f"Fig {fig}"


def _figures_of(controls):
    """Every figure these controls sit in, in order, without repeats."""
    out = []
    for c in controls:
        for f in c["figures"]:
            if f not in out:
                out.append(f)
    return out


def _figure_overlap(reagent_figs, control_figs):
    """``(same_panel, same_figure)`` — exact, then panel-insensitive.

    Both are ANNOTATIONS. ``same_panel`` is now case-insensitive, which it was not:
    a reagent in ``3B`` and a control in ``3b`` failed the old gate outright.
    """
    a_exact = {str(f).strip().lower() for f in (reagent_figs or [])}
    b_exact = {str(f).strip().lower() for f in (control_figs or [])}
    a_keys = {k for k in (_figure_key(f) for f in (reagent_figs or [])) if k}
    b_keys = {k for k in (_figure_key(f) for f in (control_figs or [])) if k}
    return bool(a_exact & b_exact), bool(a_keys & b_keys)


def _readouts(control):
    """What the paper says was MEASURED on the manipulated material.

    Returns ``(antibody_apps, non_antibody, source)``. A caller-supplied
    ``readout`` wins — the caller read the paper and this layer did not. Failing
    that the quoted ``evidence`` is scanned against the closed vocabularies above,
    which is a lookup, not parsing: it recognises named assays and nothing else.

    ``source`` says which, because the two are not equally strong and a reader
    deserves to know which one they are being shown.
    """
    stated = control.get("readout") or control.get("readout_application")
    # A single application arrives as a bare string at least as often as a list, and
    # iterating a string yields its characters — silently no applications at all.
    apps = _normalise_apps([stated] if isinstance(stated, str) else stated)
    if apps:
        return sorted(apps), [], "caller"
    text = control.get("evidence") or ""
    if not text:
        return [], [], None
    found, non = set(), []
    for rx, app in _READOUT_WORDS:
        if rx.search(text):
            found.add(app)
    for rx, app in _READOUT_ABBREV:
        if rx.search(text):
            found.add(app)
    for rx, name in _NON_ANTIBODY_READOUT:
        if rx.search(text) and name not in non:
            non.append(name)
    if not found and not non:
        return [], [], None
    return sorted(found), non, "evidence_text"


def _normalise_controls(controls):
    """Take the controls a caller reports and decide what each one COUNTS AS.

    The caller says what it SAW and WHERE — a type, a figure, the gene a knockout
    removes, whether the paper performed it, and (v9) what the manipulated material
    was READ OUT with. The server decides the CLASS from the type alone, and
    refuses a caller-supplied class: whether an isotype control can evidence
    selectivity is a fixed property of the method (it cannot), not something any
    reader gets a vote on. An unrecognised type is kept and marked ``unclassified``
    rather than silently promoted.
    """
    out = []
    for c in (controls or []):
        if not isinstance(c, dict):
            continue
        reported = (str(c.get("type") or "").strip().lower().replace(" ", "_")
                    .replace("-", "_"))
        if not reported:
            continue
        # A spelling this server does not know is its vocabulary being narrower
        # than the literature's, not the reader being wrong. `canonical_type` can
        # only ADD a classification -- a type `CONTROL_CLASSES` already names is
        # returned untouched -- so widening the alias map cannot reclassify
        # anything. What it cannot place stays `unclassified` and says so.
        ctype = canonical_type(reported)
        cls = CONTROL_CLASSES.get(ctype, "unclassified")
        performed = c.get("performed_in_paper")
        performed = True if performed is None else bool(performed)
        figures = [str(f) for f in (c.get("figures") or [])]
        apps, non_apps, source = _readouts(c)
        out.append({
            "type": ctype,
            # What the caller called it, when that is not what it was filed as.
            # Same rule as `coverage`: echo what the server PARSED beside what was
            # sent, so a reader can see a resolution happened.
            "type_as_reported": reported if reported != ctype else None,
            "control_class": cls,           # server-decided, never caller-supplied
            "target": _clean(c.get("target")),
            "figures": figures,
            "performed_in_paper": performed,
            "evidence": _clean(c.get("evidence")),
            # Where it sits, reported rather than required.
            "location": ", ".join(_figure_label(f) for f in figures) or None,
            "supplementary": any(_is_supplementary(f) for f in figures),
            # Whether the paper says THIS material was read out with an antibody.
            "readout_applications": apps,
            "non_antibody_readouts": non_apps,
            "readout_source": source,
            "detected_with": _clean(c.get("detected_with")),
        })
    return out


def _target_matches(control, target):
    """Does this control remove the gene THIS antibody is against?

    Unchanged from v8, deliberately: the benchmark reached sensitivity 1.000 with
    the target match exactly as it is, so there was never a reason to loosen it.
    """
    ct = (control["target"] or "").strip().lower()
    tgt = (target or "").strip().lower()
    if not ct:
        return False
    if not tgt:
        return True
    return ct == tgt or ct in tgt or tgt in ct


def _controls_for(controls, target, reagent_figs):
    """``(candidates, others)`` — the controls in scope for ONE reagent.

    SCOPE IS THE PAPER, not the figure. A control is a candidate when it removes
    this antibody's target, the paper performed it, and its type is a genetic
    pillar method. Supplementary figures count fully; a different figure counts
    fully; a reagent whose figures the caller never reported still gets its
    candidates, which under the old rule was structurally impossible.

    ``others`` — detection, orthogonal and pseudo-controls. These never set the
    verdict and never did. They are scoped by TARGET where they name one (a
    blocking peptide and a positive control both do), and fall back to the figure
    test where they name none, because for a secondary-only or an isotype control
    the figure is the only scope there is.
    """
    candidates, others = [], []
    for c in controls:
        if not c["performed_in_paper"]:
            continue
        selectivity = c["control_class"] in SELECTIVITY_CLASSES
        if selectivity:
            # A selectivity control naming no gene validates nothing — unchanged.
            if not _target_matches(c, target):
                continue
            same_panel, same_figure = _figure_overlap(reagent_figs, c["figures"])
            candidates.append(dict(c, same_panel=same_panel, same_figure=same_figure))
            continue
        if c["control_class"] == "unclassified":
            # Kept unconditionally, unlike every other non-selectivity control. A
            # type the server cannot place has no reliable target and often no
            # figure -- the two things the scoping below needs -- so scoping it
            # would drop the one control the reader most needs told about, and
            # drop it SILENTLY. It sets no verdict either way.
            pass
        elif c["target"]:
            if not _target_matches(c, target):
                continue
        else:
            _panel, same_figure = _figure_overlap(reagent_figs, c["figures"])
            if not same_figure:
                continue
        for f in (c["figures"] or [None]):
            entry = {"figure": f, "type": c["type"], "class": c["control_class"]}
            if entry not in others:
                others.append(entry)
    return candidates, others


def _same_reagent(a, b):
    """Do two written reagent names refer to the same thing? Deliberately loose —
    a paper writes ``ab212184`` in the Methods and ``anti-SNCA (ab212184)`` in the
    legend, and both are the same antibody."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ca = manuscript.collapse_identifier(manuscript.strip_printing(a)).lower()
    cb = manuscript.collapse_identifier(manuscript.strip_printing(b)).lower()
    return bool(ca and ca == cb)


def _linkage(candidate, antibody, other_reagents=()):
    """WHY this control counts as having tested THIS antibody — or ``[]``.

    Reported, not merely used. The route says how the claim was reached and where
    a reader should look to check it, which is the thing a person actually needs
    from a controls scan.

    Routes, strongest first:
      * ``named`` — the paper names this reagent as what detected the manipulated
        material (``detected_with``).
      * ``same_panel`` — the control and the antibody's use are the SAME panel, so
        that panel IS this antibody read out on manipulated material. Co-location
        is a poor GATE (it wrongly excludes the Fig 1 / Fig 4 structure, which is
        what the benchmark measured) and good positive EVIDENCE when it fires;
        those are different uses of the signal and only the first was scored.
      * ``stated_readout`` — the text names an antibody-based readout of the
        manipulated material. Taken from wherever the paper says it: a legend
        gives the assay and frequently does NOT name the reagent, while the
        Methods name the reagent and its application. Requiring the legend to
        carry both halves would refuse most competently-controlled papers.

    One route runs the other way. When ``detected_with`` names a DIFFERENT reagent
    the caller also reported, this control was read out with something else — the
    exact hazard the co-location gate stood in for ("blotted with a different
    antibody"), stated outright by the paper. That is negative evidence and it
    outranks every route above.
    """
    detected = candidate.get("detected_with")
    if detected and not _same_reagent(detected, antibody):
        if any(_same_reagent(detected, other) for other in other_reagents):
            return []
    routes = []
    if detected and _same_reagent(detected, antibody):
        routes.append("named")
    if candidate.get("same_panel"):
        routes.append("same_panel")
    if candidate["readout_applications"]:
        routes.append("stated_readout")
    return routes


def _linked(candidate):
    """Whether ``_linkage`` found anything, once it has been attached to the row."""
    return bool(candidate.get("linkage_basis"))


#: What a reader should check at the panel, per readout application. Every
#: ``demonstrated`` carries one: the server sees text, not images, so it can say a
#: control is there and NEVER that it worked.
_WHAT_TO_CHECK = {
    "WB": "confirm the band disappears at the expected size",
    "IP": "confirm the target is no longer pulled down",
    "IF": "confirm the signal is lost in the manipulated cells",
    "FC": "confirm the stained population is lost",
}
_CHECK_DEFAULT = "confirm the signal is lost in the manipulated material"

_TYPE_WORDS = {"knockout": "Knockout", "knockdown": "Knockdown",
               "crispr": "CRISPR knockout", "null_line": "Null (target-lacking) line"}
_APP_WORDS = {"WB": "western blot", "IP": "immunoprecipitation",
              "IF": "immunostaining", "FC": "flow cytometry"}


def _control_note(status, candidates, target, reagent_figs, withheld_note=None,
                  unlinked_figs=()):
    """One sentence a reader can act on: what the control is, WHERE it is, what
    read it out, where the antibody was used, and what to check at the panel.

    Never says the control worked. It cannot: nothing here has seen a figure.
    """
    if status == "not_assessed":
        # The one status whose note says something when there are no candidates:
        # a withheld row has to explain itself, or it reads as a bare gap.
        return withheld_note
    if status == "absent" or not candidates:
        return None
    c = next((x for x in candidates if _linked(x)), candidates[0])
    what = _TYPE_WORDS.get(c["type"], c["type"].replace("_", " ").capitalize())
    where = c["location"] or "a figure the report does not name"
    if c["supplementary"] and c["location"]:
        where = "supplementary " + where
    lead = f"{what} of {target or 'the target'} shown in {where}"
    if c["readout_applications"]:
        lead += " (" + "/".join(_APP_WORDS.get(a, a)
                                for a in c["readout_applications"]) + ")"
    used = (", ".join(_figure_label(f) for f in reagent_figs)
            if reagent_figs else None)
    lead += f"; antibody used in {used}." if used else "; the antibody's own figures were not reported."

    check = _WHAT_TO_CHECK.get(
        (c["readout_applications"] or [None])[0], _CHECK_DEFAULT)
    if status == "present_unlinked":
        if c["detected_with"] and "named" not in (c.get("linkage_basis") or []):
            # The strongest thing a paper can say against linkage, and it says it
            # outright: the manipulated material was read out with something else.
            why = (f"the paper says it was detected with {c['detected_with']}, "
                   f"not with this antibody")
        elif c["non_antibody_readouts"]:
            why = ("the quoted evidence names only a non-antibody readout ("
                   + ", ".join(c["non_antibody_readouts"]) + ")")
        else:
            why = "the text does not say which antibody, if any, was read out against it"
        return (f"{lead} A genetic control for this target is present in the paper, but "
                f"{why}. Check the figure legends — including the supplementary ones "
                f"— for a panel showing THIS antibody on the manipulated material: a "
                f"manipulation existing somewhere in a paper does not by itself "
                f"validate this reagent.")
    if c["detected_with"]:
        lead += f" Detected with {c['detected_with']}."
    # Say WHY it counts, not just that it does. The route is what tells a reader
    # how strong the claim is and where to look to check it.
    basis = c.get("linkage_basis") or []
    if "same_panel" in basis and "stated_readout" not in basis:
        where_word = ("Same panel as the antibody's use, so that panel is this "
                      "antibody on the manipulated material")
        if not c["readout_applications"]:
            check = "confirm the signal is lost there"
    else:
        where_word = ("Same panel" if c["same_panel"]
                      else "Different panel" if c["same_figure"]
                      else "Different figure" if used else "Location not comparable")
    note = (f"{lead} {where_word} — {check}. Presence is not proof: this is a "
            f"candidate control to confirm at the panel, not a verdict.")
    if unlinked_figs:
        # Named, and named as a DIFFERENT errand. Merging these into the "go and
        # check" list is what sent a reader to a dendrite count expecting a blot.
        note += (" The same target is also manipulated in "
                 + ", ".join(_figure_label(f) for f in unlinked_figs)
                 + ", which the text does not tie to this antibody — a different"
                   " experiment, not a second place to check this reagent.")
    return note


def _primary_reagents(reagents):
    return [r for r in (reagents or []) if isinstance(r, dict)
            and str(r.get("role") or "primary").strip().lower()
            not in _NON_PRIMARY_ROLES]


def _label_matches(a, b):
    """Two free-text target labels naming the same thing — the same loose rule
    ``_target_matches`` applies, so a row's resolved gene meets a reagent's label."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b and (a == b or a in b or b in a))


def _linkage_ambiguity(candidates, antibody, target, reagents):
    """Several antibodies against one target, and a control that names none of them.

    A paper's Methods routinely list more than one antibody per target while the
    control panel says only "immunoblot of KO and WT lysates". One of them was on
    that blot. Reporting all of them as ``demonstrated`` with nothing said is how a
    reader comes away believing three reagents were controlled by one experiment.

    Only fires where the ambiguity is real: the row's linkage rests on the stated
    readout ALONE. A reagent the paper names, or one sitting in the control's own
    panel, is distinguished from its neighbours and needs no caveat — which is
    also what makes those two routes worth more than this one.
    """
    linked = [c for c in candidates if _linked(c)]
    if not linked:
        return None
    if any(set(c.get("linkage_basis") or []) - {"stated_readout"} for c in linked):
        return None
    others = [r for r in _primary_reagents(reagents)
              if _label_matches(target, _clean(r.get("target")))
              and not any(_same_reagent(antibody, _clean(r.get(k)))
                          for k in ("identifier", "catalogue", "rrid"))]
    if not others:
        return None
    n = len(others) + 1
    return (f"{n} antibodies in this paper target {target}, and the control names "
            f"none of them — which one it was read out with is not stated. Check "
            f"the panel's legend and the Methods before crediting this reagent "
            f"with it.")


def _uses_not_located(reagents):
    """Antibodies the caller reported that no figure places.

    A Methods section routinely names reagents the figures never account for: a
    supplementary panel whose legend names no antibody, an assay that produces no
    image, an "as previously described", a "data not shown". The antibody WAS
    used; nobody can say where. And a use that cannot be located is a use whose
    controls cannot be checked — which is a finding in its own right, not a gap in
    the scan, and not the same thing as the paper showing no control.

    Reported only when at least one OTHER reagent did carry figures. With none at
    all the honest reading is that the figures were not read, and
    ``coverage.limits`` says that instead — the server cannot tell "the paper does
    not say" from "nobody looked", and guessing between them would put an
    accusation on whichever is wrong.
    """
    primaries = _primary_reagents(reagents)
    located = [r for r in primaries if r.get("figures")]
    if not located or len(located) == len(primaries):
        return []
    return [{"antibody": _clean(r.get("identifier")) or _clean(r.get("catalogue"))
             or _clean(r.get("rrid")), "target": _clean(r.get("target"))}
            for r in primaries if not r.get("figures")]


def _application_caveat(candidates, reagent_apps, unscoped_apps=None):
    """A ``demonstrated`` control read out in an application the paper never used.

    The genuine hazard co-location half-caught: a knockout validated by western
    blot while the antibody is used for immunohistochemistry. It is a CAVEAT, not
    a gate — the benchmark is unambiguous that hard gates on weak signals cost far
    more than they buy — and it only speaks when both sides are actually known.

    ``unscoped_apps`` are the paper's uses this connector recognises and holds no
    verdict for — tissue. They count HERE even though they scope no verdict, and
    that is the point: a knockout read out by western blot says least of all about
    a tissue section, so the case with no verdict of its own is exactly the one the
    caveat is for. Leaving them out silenced it on its own worked example.
    """
    used = set(reagent_apps or []) | set(unscoped_apps or [])
    if not used:
        return None
    linked = [c for c in candidates if _linked(c)]
    if not linked:
        return None
    control_apps = {a for c in linked for a in c["readout_applications"]}
    if not control_apps or (control_apps & set(reagent_apps or [])):
        return None
    return ("The control was read out by "
            + "/".join(sorted(control_apps))
            + "; this paper uses the antibody for "
            + "/".join(sorted(used))
            + ". A control in one application does not carry over to another.")


def _coverage(reagents, norm_controls, sections_read=None,
              legends_read=None):
    """What the caller actually supplied — and what its absence costs.

    Every answer here rests on what was read out of the paper, and the four things
    a controls assessment needs live in different sections: which figures use
    antibodies (figures and legends), WHICH antibodies (Methods — that is where a
    catalogue number is), which controls (Methods, figures and legends), and
    whether this antibody was read out against the control material (figures and
    legends, supplements included). A caller that read only the Methods produces a
    confident-looking table with a hole in it.

    So say what came in. The alternative is the failure this whole surface is built
    to avoid: `absent` on every row means "no genetic manipulation of this target
    anywhere in the paper", and with no controls reported the server has no basis
    whatever for that claim — it looks identical to a paper that genuinely shows
    none.

    ``sections_read`` is the caller's own declaration of which parts of the paper
    it read. It is cross-checked against the payload rather than believed: a model
    asked whether it read something is free to say yes, so the declaration can only
    ever REMOVE an ``absent``, never restore one. ``sections.absent_is_serveable``
    is where that asymmetry lives.
    """
    primaries = _primary_reagents(reagents)
    with_figs = len([r for r in primaries if r.get("figures")])
    with_apps = len([r for r in primaries if _normalise_apps(r.get("applications"))])
    ctl_figs = len([c for c in norm_controls if c["figures"]])
    ctl_quoted = len([c for c in norm_controls if c["evidence"]])
    ctl_readout = len([c for c in norm_controls if c["readout_applications"]])

    declared, unrecognised = sections.normalise(sections_read)
    evidenced = sections.evidenced(primaries, norm_controls)
    counts = {"reagents": len(primaries), "reagents_with_figures": with_figs,
              "controls_with_figures": ctl_figs,
              "controls_with_quoted_evidence": ctl_quoted}
    # Was it read IN FULL? `evidenced` answers "was the section opened", which
    # one reagent carrying one figure satisfies. `absent` is a claim about the
    # WHOLE paper, so the question it actually rests on is coverage — and asking
    # a model whether it finished reading gets a yes. So the caller enumerates,
    # and the enumeration is checked against the payload's own figure references.
    enumerated = legends_read is not None
    read_keys = {k for k in (_figure_key(f) for f in (legends_read or [])) if k}
    cited = [f for r in primaries for f in (r.get("figures") or [])]
    cited += [f for c in norm_controls for f in (c["figures"] or [])]
    cited_keys = {k for k in (_figure_key(f) for f in cited) if k}
    uncovered, gaps = (sections.legend_completeness(read_keys, cited_keys)
                       if enumerated else ([], []))
    legends_complete = enumerated and not uncovered and not gaps

    serveable = (sections.absent_is_serveable(declared, evidenced)
                 and legends_complete)
    clashes = sections.contradictions(declared, evidenced, counts)

    limits = []
    # First, because it is the one line that changes what the reply is allowed to
    # say. The six sentences below explain what a gap COSTS; this one states that
    # a verdict has been withheld.
    if not serveable:
        limits.append(sections.WITHHELD_LIMIT)
    # Immediately after, because these say WHY it was withheld. A withholding
    # whose reason is three paragraphs away reads as the server being difficult.
    if not enumerated:
        limits.append(sections.LEGENDS_NOT_ENUMERATED_LIMIT)
    if uncovered:
        limits.append(sections.LEGENDS_UNREAD_LIMIT.format(
            what=", ".join(_figure_label(("S" if sup else "") + n)
                           for sup, n in uncovered)))
    if gaps:
        limits.append(sections.LEGEND_GAP_LIMIT.format(
            what=", ".join(_figure_label(str(n)) for n in gaps)))
    if unrecognised:
        limits.append(sections.UNKNOWN_SECTION_NOTE.format(
            unknown=", ".join(repr(u) for u in unrecognised)))
    limits.extend(clashes)
    # Only when the caller DID declare: with no declaration at all the payload
    # gate has already spoken, and adding this would caveat every legacy call.
    if declared is not None and "supplementary" not in declared:
        limits.append(sections.SUPPLEMENT_LIMIT)
    unplaceable = sorted({c["type_as_reported"] or c["type"]
                          for c in norm_controls
                          if c["control_class"] == "unclassified"})
    if unplaceable:
        limits.append(UNCLASSIFIED_NOTE.format(
            what=", ".join(repr(u) for u in unplaceable)))
    if not norm_controls:
        limits.append(
            "NO controls were reported, so every row reads `absent`. That is a "
            "statement about what was PASSED IN, not about the paper: if the "
            "Methods and the figure legends (including supplementary) were not "
            "read for knockouts, knockdowns, CRISPR lines and null lines, this is "
            "not evidence the paper shows none. Say so before reporting `absent`.")
    if primaries and not with_figs:
        limits.append(
            "No reagent carried `figures`, so no antibody could be placed in a "
            "panel — one of the three linkage signals, and the only one that does "
            "not depend on the paper naming a reagent. Either the figures and "
            "legends were not read, or the paper never says where these antibodies "
            "were used; those are very different and only you can tell them apart.")
    elif primaries and with_figs < len(primaries):
        limits.append(
            f"{len(primaries) - with_figs} of {len(primaries)} antibodies are named "
            "with no figure placing them. A Methods section routinely lists "
            "reagents the figures never account for — a supplementary panel whose "
            "legend names no antibody, an assay with no image, an 'as previously "
            "described', a 'data not shown'. Their use is UNLOCATED, so their "
            "controls cannot be checked: say that, rather than reporting them as "
            "uncontrolled. They are listed in `focus.uses_not_located`.")
    if primaries and not with_apps:
        limits.append(
            "No reagent carried `applications`, so a control read out in an "
            "application the paper never used the antibody for cannot be flagged, "
            "and alternatives cannot be matched to the job the paper did. The "
            "Methods are where this is stated.")
    if norm_controls and ctl_quoted < len(norm_controls):
        limits.append(
            f"{len(norm_controls) - ctl_quoted} of {len(norm_controls)} controls "
            "carried no quoted text, so linkage for those could only be "
            "established by co-location. Quote the panel's legend.")
    if norm_controls and not ctl_figs:
        limits.append(
            "No control carried `figures`, so none can be located for the reader "
            "and none can be matched to an antibody's panel.")
    return {
        "reagents": len(primaries),
        "reagents_with_figures": with_figs,
        "reagents_with_applications": with_apps,
        "controls": len(norm_controls),
        "controls_with_figures": ctl_figs,
        "controls_with_quoted_evidence": ctl_quoted,
        "controls_with_stated_readout": ctl_readout,
        "limits": limits,
        # Echoes what the server PARSED, not what was sent: an unknown argument is
        # discarded without error by the tool layer, so a caller that misspelled
        # the parameter would otherwise have no way to see that its declaration
        # never arrived.
        "legends": {
            "enumerated": enumerated,
            "read": sorted(("S" if sup else "") + n for sup, n in read_keys),
            "cited_but_not_read": [("S" if sup else "") + n
                                   for sup, n in uncovered],
            "gaps_in_sequence": gaps,
            "complete": legends_complete,
        },
        "sections": {
            "declared": declared,
            "unrecognised": unrecognised,
            "evidenced_by_payload": sorted(evidenced),
            "contradictions": clashes,
            "absent_withheld": not serveable,
            "results_note": sections.RESULTS_NOTE,
        },
    }


def _control_figs_index(controls, target=None):
    """figure -> [{type, class}] for the controls the paper actually performed.

    Paper-level orientation for the ``focus`` block: which figures carry a control
    at all, and of what kind. It is NOT how a row's verdict is reached — that is
    ``_controls_for`` — and passing a ``target`` here only narrows the selectivity
    controls to that gene's.
    """
    idx = {}
    for c in controls:
        if not c["performed_in_paper"]:
            continue
        if c["control_class"] in SELECTIVITY_CLASSES:
            if not c["target"] or not _target_matches(c, target):
                continue
        for f in c["figures"]:
            idx.setdefault(f, []).append({"type": c["type"],
                                          "class": c["control_class"]})
    return idx


def _build_table(base, controls, reagents, apps_for=None, app_notes_for=None,
                 absent_serveable=True, withheld_note=None, unscoped_for=None):
    """Pre-assemble the concise per-antibody summary table. ONE row per antibody:
      antibody | target | control_status (+ legacy paper_control) + control_figures
      (linked panels only) + control_figures_unlinked (same target, different
      experiment) |
      oga_tested (y/n) | oga_result | image (validation card) | gene_page.

    ``control_status`` separates the three questions v8 collapsed into one boolean:
    ``demonstrated`` (a genetic control for this target AND the paper says this
    antibody was read out against it), ``present_unlinked`` (the control is there;
    the text does not establish this antibody was tested against it) and ``absent``
    (no genetic manipulation of this target anywhere in the paper). None of them
    claims the control WORKED — nothing here has seen a figure. Rows come from
    resolved OGA hits plus the caller's primary reagents the dataset does not know.
    """
    apps_for = apps_for or {}
    app_notes_for = app_notes_for or {}
    unscoped_for = unscoped_for or {}
    # Every reagent the caller named, so a control whose `detected_with` points at
    # a DIFFERENT one of them can be recognised as saying so.
    all_identifiers = [v for r in (reagents or []) if isinstance(r, dict)
                       for v in (_clean(r.get("identifier")), _clean(r.get("catalogue")),
                                 _clean(r.get("rrid"))) if v]

    def _row(antibody, target, figs, tested, hit=None, reagent_apps=None,
             app_notes=None, unscoped_apps=None):
        # Rebuilt per antibody: a selectivity control only counts for the antibody
        # whose target it removes.
        candidates, others = _controls_for(controls, target, figs)
        others_named = [i for i in all_identifiers if not _same_reagent(i, antibody)]
        candidates = [dict(c, linkage_basis=_linkage(c, antibody, others_named))
                      for c in candidates]

        # `paper_control` answers ONE question — did this paper show a control that
        # tests SELECTIVITY for this antibody? — so only the class that can answer it
        # may set it. It used to be set by any performed control sharing a figure, so
        # a positive control and a peptide competition both came back "yes", a few
        # keys away from a `control_class` saying `detection` and `pseudo` and a
        # `note` saying in words that neither establishes selectivity. The server
        # contradicted itself, and it did so in the one field the tool instructs the
        # reading model to render as-is.
        #
        # Gating alone would trade a false "yes" for a false "no", though: a
        # detection or orthogonal control the paper genuinely ran would simply
        # vanish. They are kept, reported as what they are, so a reply can tell
        # NO CONTROL apart from A CONTROL THAT DOES NOT ESTABLISH SELECTIVITY —
        # which is the classic mistake the rubric exists to name, and worth more to
        # a reader than either silence or a false pass.
        if not candidates:
            status = "absent"
        elif any(_linked(c) for c in candidates):
            status = "demonstrated"
        else:
            status = "present_unlinked"
        # ONLY `absent` is withheld, and it is withheld paper-wide rather than per
        # row: whether the legends were read is a property of the call, so every
        # row flips together and the reply stays reasonable about. `demonstrated`
        # and `present_unlinked` are FINDINGS -- a paper that shows a knockout
        # still shows it however little of it was read -- and withholding those
        # would punish a caller for honesty and teach it not to declare.
        if status == "absent" and not absent_serveable:
            status = "not_assessed"
        # WHERE A READER CAN CHECK *THIS* ANTIBODY -- which on a `demonstrated`
        # row is the LINKED candidates only.
        #
        # An unlinked candidate is a genetic manipulation of the same target
        # somewhere else in the paper, read out by something else or by nothing
        # the text names. Listing it here sends a reader to watch a band
        # disappear in a panel that has no band in it. Two real papers hit this
        # within an hour of each other: an RNAi knockdown scored by counting
        # dendrites, and a knockout-mouse IHC, each printed beside a western
        # blot as though the three were one errand.
        #
        # They are NOT dropped. A genetic manipulation elsewhere in the paper is
        # real information, and a count with no list under it invents the noun --
        # so they move to `control_figures_unlinked` and the note names them as
        # a different experiment.
        #
        # On `present_unlinked` nothing is linked by definition, and splitting
        # would empty the one column that row exists to point at. There every
        # candidate stays.
        linked = [c for c in candidates if _linked(c)]
        shown = linked if status == "demonstrated" else candidates
        cfigs = _figures_of(shown)
        cfigs_unlinked = [f for f in _figures_of(candidates) if f not in cfigs]
        row = {
            "antibody": antibody,
            "target": target,
            "control_status": status,
            "paper_control": _PAPER_CONTROL[status],
            # WHERE the control is, not where the antibody is. Under the figure
            # gate the two were the same string by construction; they are not now,
            # and the control's own figure is the one a reader has to open.
            "control_figures": cfigs,
            # Same target, same paper, no link to THIS reagent. Named rather
            # than merged, so "go and look here" means one thing.
            "control_figures_unlinked": cfigs_unlinked,
            "control_note": _control_note(status, candidates, target, figs,
                                          withheld_note=withheld_note,
                                          unlinked_figs=cfigs_unlinked),
            "application_caveat": _application_caveat(candidates, reagent_apps,
                                                      unscoped_apps),
            # Several antibodies against one target and a control that names none:
            # one of them was on that blot, and saying which is not this layer's
            # to guess.
            "linkage_ambiguity": _linkage_ambiguity(candidates, antibody, target,
                                                    reagents),
            # The candidate genetic controls themselves, each carrying where it
            # sits, whether that is the supplement, what read it out and whether
            # that is the same panel as the antibody's use.
            "controls": [{k: c[k] for k in
                          ("type", "control_class", "target", "figures", "location",
                           "supplementary", "readout_applications",
                           "non_antibody_readouts", "readout_source",
                           "detected_with", "same_panel", "same_figure",
                           "linkage_basis", "evidence")}
                         for c in candidates],
            # Performed, in scope for this antibody, and NOT selectivity evidence:
            # [{figure, type, class}].
            "other_controls": others,
            # What the PAPER used it for. It drove the alternatives ranking and the
            # application caveat and was never reported back, so the table could
            # not answer the first question a reader asks — which antibody did
            # what — and the OGA verdict beside it covered four applications when
            # only one of them was the paper's.
            "applications_in_paper": sorted(reagent_apps or []),
            "oga_tested": "yes" if tested else "no",
            # Nothing to say on the ordinary path, and present only when there is:
            # a field that is null on every row reads as one that never works.
            "oga_result": None,
            # The OGA verdict RESTRICTED to what the paper did with it. `oga_result`
            # keeps all four, because a reader may be choosing a reagent for
            # something else; this is the half that bears on the paper in hand.
            "oga_result_for_paper_applications": None,
            "image": None,
            "gene_page": None,
        }
        if app_notes:
            row["application_notes"] = list(app_notes)
        if unscoped_apps:
            # Named, and NOT in `applications_in_paper`: that field feeds the
            # verdict scoping, and a tissue term in it would score a tissue use
            # against a cultured-cell result. The reader still learns what the
            # paper did, which is the whole reason these are not dropped.
            row["applications_without_oga_verdict"] = list(unscoped_apps)
        if tested and hit is not None:
            row["oga_result"] = _concise_verdict(hit["applications"])
            if reagent_apps:
                scoped = {a: hit["applications"].get(a, "not_tested")
                          for a in sorted(reagent_apps)}
                row["oga_result_for_paper_applications"] = _concise_verdict(scoped)
                row["oga_applications_used"] = scoped
            elif unscoped_apps:
                # The paper's ONLY use of this antibody is one OGA holds no verdict
                # for. There is nothing to scope, and saying only that would waste
                # what is actually known — so the row states the boundary and then
                # hands over the record. "No tissue verdict; tested and not
                # recommended in three other applications" is actionable in a way
                # that silence is not.
                assessed = [a for a, v in hit["applications"].items()
                            if v in ("recommended", "not_recommended")]
                row["oga_result_context"] = (
                    f"OGA has no verdict for {', '.join(unscoped_apps)}. "
                    + (f"It did test this antibody in "
                       f"{'/'.join(sorted(assessed))} — see `oga_result`. That is "
                       f"context for judging the reagent, NOT a result in the "
                       f"preparation this paper used."
                       if assessed else
                       "It has not been tested in any application OGA assesses, so "
                       "there is no result either way."))
            # Link the direct validation-image MEDIA files (not the embed cards).
            exps = [e for e in (hit.get("experiments") or []) if e.get("image_url")]
            row["images"] = [{"application": _IMG_APP.get(e.get("experiment_type"),
                                                          e.get("experiment_type")),
                              "stored_as": e.get("experiment_type"),
                              "url": e["image_url"]} for e in exps]
            row["image"] = row["images"][0]["url"] if row["images"] else None
            row["gene_page"] = hit.get("gene_page_url")
            # Same reason `target_matched_via` travels: a field that reaches only
            # `antibody_hits` is a field the rendered table never shows.
            for key in ("target_is_characterised", "recommended_alternatives",
                        "alternatives_listed", "alternatives_truncated",
                        "recommended_for_requested_applications"):
                if key in hit:
                    row[key] = hit[key]
            # The peer-reviewed report DOI travels WITH the verdict. When a row says
            # an antibody was tested and not recommended, that can undercut a paper's
            # claim — so the reader must be able to reach the primary evidence from
            # the row itself, not just a figure image. The DOI existed on the hit but
            # never reached the table, which is the only thing the caller is told to
            # render.
            row["report_dois"] = hit.get("report_dois") or []
        if hit is not None and "citeab_applications" in hit:
            # Copied down rather than recomputed: check_manuscript already looked
            # this paper up once, and a second lookup is a second chance to answer
            # about a different paper.
            for key, value in hit.items():
                if key.startswith("citeab_"):
                    row[key] = value
            # The conflict can only exist HERE. A resolved hit carries no
            # caller-supplied applications -- check_manuscript never reads them on
            # that path -- so `reagent_apps`, which only reaches this function, is
            # the only side to compare CiteAb against.
            clash = citeab.conflict(reagent_apps,
                                    hit.get("citeab_applications_as_oga_codes"))
            if clash:
                row["application_conflict"] = clash
        return row

    # Figures the caller reported per identifier, so a row knows where it appears.
    figs_for = {}
    for r in (reagents or []):
        if not isinstance(r, dict):
            continue
        for key in ("identifier", "catalogue", "rrid"):
            v = _clean(r.get(key))
            if v and r.get("figures"):
                figs_for.setdefault(v.lower(), []).extend(str(f) for f in r["figures"])

    table = []
    # 1. resolved hits (tested) — authoritative target + verdict + links
    for bucket in base["antibody_hits"].values():
        for hit in bucket:
            figs = hit.get("reported_figures") or figs_for.get(
                hit["identifier"].lower(), [])
            key = hit["identifier"].lower()
            row = _row(hit["identifier"], hit.get("gene"), figs, True, hit,
                       apps_for.get(key), app_notes_for.get(key),
                       unscoped_apps=unscoped_for.get(key))
            if hit.get("target_confusion"):
                row["target_confusion"] = hit["target_confusion"]
            table.append(row)
    # 2. the caller's reagents the dataset does not know (untested). An untested
    #    reagent still gets its gene page when the TARGET is characterised — that
    #    is the reader with a decision to make, and the link is the point.
    for entry in base["not_in_dataset"]:
        figs = entry.get("figures") or figs_for.get(entry["identifier"].lower(), [])
        key = entry["identifier"].lower()
        row = _row(entry["identifier"], entry.get("target"), figs, False,
                   reagent_apps=apps_for.get(key),
                   app_notes=app_notes_for.get(key),
                   unscoped_apps=unscoped_for.get(key))
        if entry.get("target_confusion"):
            # The table is the only part of this reply the caller is told to
            # render as-is, so a notice that reached only `not_in_dataset` would
            # be a notice nobody sees -- the same reason `target_matched_via`
            # travels onto the row below.
            row["target_confusion"] = entry["target_confusion"]
        if entry.get("gene_page_url"):
            row["gene"] = entry.get("gene")
            row["gene_page"] = entry["gene_page_url"]
            # Travels onto the row, because the table is the only part of this reply
            # the caller is told to render as-is — a caveat that reaches only
            # `not_in_dataset` is a caveat nobody sees.
            if entry.get("target_matched_via"):
                row["target_matched_via"] = entry["target_matched_via"]
                row["target_matched_on"] = entry.get("target_matched_on")
            row["target_is_characterised"] = entry.get("target_is_characterised", False)
            row["recommended_alternatives"] = entry.get("recommended_alternatives", 0)
            row["alternatives_listed"] = entry.get("alternatives_listed", 0)
            row["alternatives_truncated"] = entry.get("alternatives_truncated", False)
            if "recommended_for_requested_applications" in entry:
                row["recommended_for_requested_applications"] = entry[
                    "recommended_for_requested_applications"]
        table.append(row)
    return table


def scan_controls(reagents=None, controls=None, genes=None, cap=200,
                  sections_read=None, legends_read=None,
                  what_shows_selectivity=None, paper_doi=None,
                  paper_pmid=None, paper_title=None, paper_year=None):
    """Assemble a controls assessment from what the CALLER read out of the paper.

    ``reagents`` — the antibodies, with role and figures (see ``check_manuscript``).
    ``controls`` — the controls the paper shows, each ``{type, figures, target,
    performed_in_paper, evidence}``. The caller reports what it saw; the SERVER
    decides what each type counts as (``CONTROL_CLASSES``) and refuses a
    caller-supplied class, so a peptide block can never be entered as evidence of
    selectivity. ``genes`` — gene symbols to look up.

    ``paper_doi`` / ``paper_pmid`` / ``paper_title`` — the paper's own identity,
    used to look up what CiteAb records it as having used each reagent for. Where
    that disagrees with the applications the caller read, the row carries an
    ``application_conflict`` naming both sides and picking neither.

    ``sections_read`` — which parts of the paper the caller actually read
    (``methods``, ``results``, ``figure_legends``, ``supplementary``, or
    ``"full text"``). Checked against the payload, never believed on its own: it
    can only ever WITHHOLD an ``absent``, never grant one. See ``sections``.

    Returns the ``rubric`` inline, a ``table`` of only the antibodies that have
    something to say (a candidate control, or independent OGA data), an ``others``
    list for everything untested AND uncontrolled, and a ``focus`` block. Makes NO
    controls verdict — that is the caller's job, guided by the rubric.
    """
    base = check_manuscript(reagents=reagents, genes=genes, cap=cap,
                            paper_doi=paper_doi, paper_pmid=paper_pmid,
                            paper_title=paper_title, paper_year=paper_year)
    norm_controls = _normalise_controls(controls)
    # Computed once, up here, because the SAME answer has to reach three places:
    # the coverage block that explains it, the table that withholds on it, and the
    # note on each withheld row. Asking the question three times is how the reply
    # comes to caveat one thing and report another.
    coverage = _coverage(reagents, norm_controls, sections_read,
                         legends_read)
    # Asked before the paper was read, so it can change how the paper is read --
    # unlike everything in `coverage`, which can only judge a payload already
    # assembled. It sizes the reply and echoes unjudged; it reaches no verdict.
    briefed, brief_why = briefing.assess(what_shows_selectivity)
    # Add-only, in both directions it can fire from: an answer that did not cover
    # what a selectivity control is, or a payload showing a control this server
    # could not place. Neither can make the reply SHORTER than the binds.
    unplaceable_reported = any(c["control_class"] == "unclassified"
                               for c in norm_controls)
    wants_scaffold = (not briefed) or unplaceable_reported
    scaffold_why = (
        f"Your answer {brief_why}, so the how-to half is included."
        if not briefed else
        "A control type this server could not place was reported, so the how-to "
        "half is included." if unplaceable_reported else
        "Your answer covered what a selectivity control is, so this carries the "
        "rules that bind and not the how-to half. Call the `controls_rubric` "
        "tool for it whenever you want it — nothing is being withheld.")
    if not briefed:
        coverage["limits"].append(briefing.NOT_ANSWERED_NOTE
                                  if brief_why == "not answered" else
                                  f"Your answer on what shows selectivity "
                                  f"{brief_why}, so the rubric below is sent in "
                                  f"full rather than in brief. Nothing is "
                                  f"withheld from you either way.")
    absent_serveable = not coverage["sections"]["absent_withheld"]
    withheld = None if absent_serveable else sections.withheld_note(
        coverage["sections"]["declared"],
        set(coverage["sections"]["evidenced_by_payload"]),
        legends=coverage["legends"])
    # The applications the PAPER used each reagent for, so a control read out in a
    # different application can be flagged as a caveat on its row.
    apps_for, app_notes_for, unscoped_for = {}, {}, {}
    for r in (reagents or []):
        if not isinstance(r, dict):
            continue
        apps = _normalise_apps(r.get("applications"))
        notes = _application_notes(r.get("applications"))
        # Recognised, and no verdict: tissue. Carried separately from `apps`
        # because it must NOT scope a verdict, and separately from the unknown
        # terms because it is not a failure to read.
        unscoped = _out_of_scope_apps(r.get("applications"))
        # Not `if not apps: continue` — a reagent whose applications ALL failed to
        # resolve is precisely the one with something to say, and skipping it threw
        # the explanation away with the value.
        if not apps and not notes and not unscoped:
            continue
        for key in ("identifier", "catalogue", "rrid"):
            v = _clean(r.get(key))
            if not v:
                continue
            if apps:
                apps_for.setdefault(v.lower(), set()).update(apps)
            for u in unscoped:
                if u not in unscoped_for.setdefault(v.lower(), []):
                    unscoped_for[v.lower()].append(u)
            for n in notes:
                if n not in app_notes_for.setdefault(v.lower(), []):
                    app_notes_for[v.lower()].append(n)
    all_rows = _build_table(base, norm_controls, reagents, apps_for,
                            app_notes_for, absent_serveable=absent_serveable,
                            withheld_note=withheld, unscoped_for=unscoped_for)

    # An antibody earns a TABLE ROW only if it has something to say: a candidate
    # control, independent OGA data, OR — new — an untested reagent whose TARGET is
    # characterised and has recommended alternatives. That last case is the paper
    # that used an unassessed antibody against a gene OGA has actually worked on:
    # the most useful thing the connector can tell that reader, and previously
    # invisible because it collapsed into `others` with every other untested row.
    def _matters(r):
        # `other_controls` earns a row for the same reason `paper_control` no longer
        # counts one: an antibody whose only control is a peptide competition is the
        # single most reportable case the rubric has, and tightening `paper_control`
        # without this would have moved it from a false "yes" straight into `others`,
        # the line that says "no candidate control reported".
        #
        # `present_unlinked` earns one for the same reason again, and it is the whole
        # point of v9: a genetic control that is present but whose linkage the text
        # does not establish is a real, common and REPORTABLE state. Dropping it into
        # `others` would be the old false "no" wearing a new name.
        #
        # `target_confusion` earns one for the third time in the same shape, and
        # this one is the sharpest: without it a documented target mismatch --
        # the most reportable thing this server can say about a reagent -- was
        # reduced to `{antibody, target}` and filed under `others`, whose own
        # description reads "untested is NOT a verdict on quality". The sentence
        # the notice exists to carve an exception out of, printed over the row it
        # was carved out for.
        return (r["control_status"] not in _NO_CLAIM_STATUSES
                or r["oga_tested"] == "yes"
                or r.get("other_controls")
                or r.get("target_confusion")
                or (r.get("target_is_characterised") and r.get("recommended_alternatives")))

    table = [r for r in all_rows if _matters(r)]
    others = [{"antibody": r["antibody"], "target": r["target"]}
              for r in all_rows if not _matters(r)]

    sig_figs = _control_figs_index(norm_controls)
    def _is_concern(r):
        """OGA tested it and did not recommend it FOR WHAT THE PAPER DID WITH IT.

        The note has promised that scoping since this list was written; the code
        asked about any application. So an antibody the paper blotted with, where
        OGA recommends it for WB and declines it for flow cytometry, was reported
        as a concern undercutting the paper — about an application the paper never
        used. Verdicts are per application everywhere else in this connector.

        Where the caller did not say what the paper used it for, there is nothing
        to scope to and the old any-application reading stands. That is the honest
        fallback, and `coverage.limits` says the applications were not supplied.
        """
        if r["oga_tested"] != "yes":
            return False
        scoped = r.get("oga_applications_used")
        if scoped:
            return any(v == "not_recommended" for v in scoped.values())
        return "not recommended" in (r["oga_result"] or "")

    concerns = [r for r in table if _is_concern(r)]

    def _brief(r):
        return {"antibody": r["antibody"], "target": r["target"],
                "control_status": r["control_status"],
                "control_figures": r["control_figures"],
                "control_figures_unlinked": r.get("control_figures_unlinked", []),
                "control_note": r["control_note"],
                "application_caveat": r.get("application_caveat"),
                "linkage_ambiguity": r.get("linkage_ambiguity")}

    # The two halves of "a genetic control for this target exists in this paper",
    # kept apart. Together they are the benchmark's winning rule; separately they
    # are the difference between what the paper SHOWED and what it SAID about it.
    demonstrated = [_brief(r) for r in table if r["control_status"] == "demonstrated"]
    unlinked = [_brief(r) for r in table if r["control_status"] == "present_unlinked"]
    # Untested reagents whose TARGET is characterised: the paper used something OGA
    # has never assessed, for a gene where OGA has assessed alternatives. That is a
    # concrete, actionable finding — not a criticism of the reagent — and it was
    # previously invisible, collapsed into `others` with everything else untested.
    # Keyed on what is actually OFFERABLE, not on the gene total. Once applications
    # filter the shortlist, a gene can be characterised and still have nothing for
    # the application in hand — and this list is what the note tells the caller to
    # name alternatives from, so a row here with an empty shortlist would be an
    # instruction to cite something that is not there. That case is still a finding;
    # it earns a table row (via `_matters`) carrying
    # `recommended_for_requested_applications: 0`, which says it exactly.
    swappable = [r for r in all_rows
                 if r["oga_tested"] == "no" and r.get("target_is_characterised")
                 and r.get("alternatives_listed")]
    # The same offer for the sharper case: OGA tested this one and does not support
    # it for what the paper did with it, AND there is something on the gene that is
    # supported for that use. `antibodies_of_concern` already names the problem;
    # this is the half that says what to do about it, and it was missing — the
    # reader with a verdict against their reagent got the finding and no exit.
    # Keyed on `alternatives_listed` rather than on the gene total, for the reason
    # `swappable` is: a row whose shortlist is empty would be an instruction to
    # cite something that is not there. That case is still reported — it keeps its
    # `recommended_for_requested_applications: 0`, which says exactly that OGA has
    # worked on this gene and supports nothing for the application in hand.
    concern_alts = [r for r in concerns if r.get("alternatives_listed")]
    focus = {
        "figures_with_controls": sig_figs,          # {figure: [{type, class}]}
        "antibodies_of_concern": concerns,          # OGA-tested, not recommended
        "untested_with_characterised_alternatives": swappable,
        "unsupported_with_characterised_alternatives": concern_alts,
        "controls_demonstrated": demonstrated,      # …and where to check them
        "controls_present_unlinked": unlinked,      # the honest third state
        # Named in the Methods, placed by no figure — used, and nobody can say
        # where, so their controls cannot be checked either way.
        "uses_not_located": _uses_not_located(reagents),
    }

    counts = dict(base.get("counts", {}))
    counts["controls_reported"] = len(norm_controls)
    counts["antibodies_in_table"] = len(table)
    counts["others"] = len(others)
    counts["figures_with_controls"] = len(sig_figs)
    counts["concerns"] = len(concerns)
    counts["untested_with_alternatives"] = len(swappable)
    counts["controls_demonstrated"] = len(demonstrated)
    counts["controls_present_unlinked"] = len(unlinked)
    counts["controls_absent"] = len([r for r in all_rows
                                     if r["control_status"] == "absent"])
    # Counted apart from `controls_absent`, never folded into it: these are the
    # rows on which the reply declined to say anything, and a count that merged
    # them would report a finding the server explicitly withheld.
    counts["controls_not_assessed"] = len([r for r in all_rows
                                           if r["control_status"] == "not_assessed"])
    counts["uses_not_located"] = len(focus["uses_not_located"])

    return {
        "table": table,
        "others": {
            "count": len(others),
            "description": "untested (not in the OGA dataset) and no genetic "
                           "manipulation of their target anywhere in the paper "
                           "— untested is NOT a verdict on quality",
            "antibodies": others,
        },
        "focus": focus,
        # What was READ, and what its gaps cost. Rendered before any `absent`.
        "coverage": coverage,
        "citeab": base.get("citeab"),
        "antibody_hits": base["antibody_hits"],
        "not_in_dataset": base["not_in_dataset"],
        "gene_hits": base["gene_hits"],
        "characterised_alternatives": base.get("characterised_alternatives", {}),
        # Carried through from `check_manuscript`, which resolved every reagent
        # this reply is about. A controls assessment is exactly where a reagent
        # raised against another protein matters most: a paper can show a perfect
        # knockout control for a protein its antibody does not bind, and the
        # rubric has no way to see that.
        **({"target_confusion_note": base["target_confusion_note"]}
           if base.get("target_confusion_note") else {}),
        **({"paper_target_confusions": base["paper_target_confusions"]}
           if base.get("paper_target_confusions") else {}),
        "controls": norm_controls,
        "counts": counts,
        # The rubric ships INLINE with the data it applies to, and is now the ONLY
        # copy. It used to be an MCP prompt the user had to select and paste in by
        # hand — an awkward manual step at exactly the moment it was needed, and
        # easy to forget, which meant the table could be read without the rules for
        # reading it. That prompt is deregistered: keeping it would only have let a
        # user add a second copy and pay for the rubric twice in one turn.
        "rubric": {
            "version": CONTROLS_RUBRIC_VERSION,
            "scaffold_version": SCAFFOLD_VERSION,
            # The binds are every caller's, always. The scaffold is support, and
            # support nobody needs is a cost: it anchors a way through a paper in
            # place of one this reader might have chosen better.
            "text": CONTROLS_BINDS + (CONTROLS_SCAFFOLD if wants_scaffold else ""),
            "binds_only": not wants_scaffold,
            "why": scaffold_why,
            # The TOOL that returns the whole document. Named on every reply,
            # sent or not: a shorter reply is a shorter reply, never a
            # smaller entitlement.
            "scaffold_tool": "controls_rubric",
        },
        # Echoed, never scored. A reader of this reply can see what understanding
        # the assessment was made against, the same way every controls verdict
        # carries the evidence sentence it rests on.
        "briefing": {
            "question": briefing.QUESTION,
            "answer": (what_shows_selectivity or "").strip() or None,
            "covers_selectivity": briefed,
            "why": brief_why,
        },
        "truncated": base.get("truncated", False),
        "note": _CONTROLS_NOTE,
    }
