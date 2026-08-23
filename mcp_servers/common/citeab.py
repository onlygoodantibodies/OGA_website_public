"""What CiteAb records a PAPER as having used a reagent for.

The connector's applications come from a model that read the Methods, which is a
far better source than the browser extension's page-proximity guess — but it is
still one reading of one document, and it is the reading whose mistakes nobody
downstream can check. CiteAb has independently recorded, for ~29,000 published
papers, which reagent each one used and for what. Where the two agree, that is
worth knowing. Where they disagree, THAT is worth knowing, and the disagreement
is the finding.

NOTHING HERE PICKS A WINNER. Two sources, drawn side by side, exactly as supplier
claims sit beside OGA verdicts and as ``target_matched_via`` names an alias match
rather than hiding it. A conflict usually means one of three ordinary things — the
paper used the reagent twice, CiteAb attributed the wrong product, or the model
read one use and missed another — and the server can distinguish none of them.

THREE STATES, AND ONLY ONE IS ABOUT THE PAPER
---------------------------------------------
``unavailable``  no snapshot is deployed, or it could not be read. About us.
``not_covered``  the snapshot was searched and does not hold this paper. About
                 the snapshot: CiteAb documents its own coverage gaps, and the
                 join behind it is on catalogue number because CiteAb's schema
                 holds no RRID.
``covered``      the paper is in it.

None of the three ever means "this paper does not cite the reagent", and a reply
that lets them collapse into each other says something false.

A FOURTH LIMIT WORTH STATING: the snapshot is keyed on RRID, because the extension
index it shares is. An antibody with no RRID is outside its scope by construction,
so a reagent that resolved to no OGA record can never join — and that absence is
about the key, not about the literature.

CELLS OR TISSUE IS THE AXIS, NOT THE DETECTION LABEL
----------------------------------------------------
OGA's IF verdict is an **ICC-IF** result: cultured cells. Whether an epitope
survives is a property of how the antigen is presented, not of the fluorophore,
so the line runs between preparations and not between technique names:

    ICC, ICC-IF        cultured cells  -> the IF verdict speaks to it
    IHC, IHC-P,
    IHC-Fr, IHC-IF     tissue          -> it does NOT, whatever the name says
    bare `IF`                          -> the term does not say which

``IHC-IF`` is the one that catches people out: it ends in IF and is tissue.

So a CiteAb term this server cannot place on the cells side of that line is
reported as an application **with no OGA verdict**, and never scored against the
cultured-cell result. That agrees with the snapshot's own ``application_map``,
which was built on the same rule.

This is the SAME rule ``portal`` applies to the applications a caller reports:
``_normalise_apps`` places a term on the cells side or nowhere, and
``_out_of_scope_apps`` recognises the tissue terms so they are named rather than
dropped. Both readers are passed in here rather than reimplemented, so the two
paths cannot answer one question differently — a model reporting `IHC` and CiteAb
reporting `IHC` get the same treatment, which is the point.

A term with no verdict is answered with CONTEXT, not silence: what OGA did find,
in the applications it did test. "No tissue verdict; tested and not recommended in
three other applications" is a sentence a reader can act on. A borrowed verdict is
not, and neither is nothing.
"""

from __future__ import annotations

# The snapshot and its encoding have exactly one reader, in `core`. The extension
# index and this server read the same file through the same functions, which is
# what stops them disagreeing about what a stored flag means.
from core import citations as _citations


def for_paper(doi=None, pmid=None, title=None, year=None):
    """``(state, records)`` for one paper.

    ``state`` is the envelope a reply carries; ``records`` is ``{rrid: {...}}``
    or ``None``. Any lookup failure is a state, never an exception: a citation
    layer that can take the whole tool down is worse than no citation layer.
    """
    if not any((doi, pmid, title)):
        return {"status": "not_asked",
                "note": ("No paper identifier was supplied, so the citation "
                         "record was not consulted. Pass `paper_doi`, "
                         "`paper_pmid` or `paper_title` to have it checked.")}, None
    try:
        status, records = _citations.paper_for(doi=doi, pmid=pmid,
                                               title=title, year=year)
    except Exception:                                   # pragma: no cover
        # Deliberately broad. This is an enhancement bolted onto a tool whose
        # primary answers are database facts; nothing here is worth failing the
        # whole call for.
        return {"status": "unavailable",
                "note": "The citation record could not be read on this server."}, None

    state = {"status": status, "snapshot": _citations.snapshot_note()}
    if status == "unavailable":
        state["note"] = ("No citation snapshot is deployed on this server, so "
                         "nothing below reflects what the literature records. "
                         "This says nothing about the paper.")
    elif status == "not_covered":
        state["note"] = ("This paper is not in the citation snapshot. That is an "
                         "absence in CiteAb's record — which documents its own "
                         "gaps, and is joined on catalogue number because CiteAb "
                         "holds no RRID — and NOT evidence that the paper cites "
                         "none of these reagents.")
    else:
        state["reagents_recorded"] = len(records or {})
    return state, records


def facts_for(record, normalise_apps, out_of_scope_apps, application_notes):
    """The fields one reagent's CiteAb record contributes to a row.

    The three readers are passed in rather than imported: this module stays free of
    ``portal`` (which imports it), and CiteAb's terms go through the SAME tables
    the caller's own applications do. Two tables would mean `IHC` scoring one way
    from the model and another way from CiteAb, in adjacent columns, with nothing
    on the row saying why.
    """
    if record is None:
        return {}

    raw_terms = list(record.get("untested_terms") or [])
    # The stored bits are already OGA codes: the snapshot's application_map placed
    # them on the CELLS side of the line. The raw terms are tokens that map could
    # not place, and they go through the connector's own readers -- which now
    # answer the same way, so a term is either one of the four or has no verdict,
    # whichever side it arrived from.
    codes = sorted(set(record.get("applications") or []) | set(normalise_apps(raw_terms)))
    no_verdict = [t for t in raw_terms if not normalise_apps([t])]

    facts = {
        # Verbatim. Re-spelling CiteAb's term to match ours would hide exactly
        # the question a reader needs to be able to ask.
        "citeab_applications": codes + raw_terms,
        "citeab_applications_as_oga_codes": codes,
    }
    if no_verdict:
        # Used for something OGA holds no verdict on -- tissue work (IHC, IHC-P,
        # IHC-Fr, IHC-IF) and applications never assessed at all (ChIP, ELISA,
        # PLA). NOT the same as "tested and not recommended", and not the same as
        # "untested" either: for most of these there is nothing an OGA result
        # could have said, because the assay was never in scope.
        facts["citeab_applications_without_oga_verdict"] = no_verdict
        tissue = out_of_scope_apps(no_verdict)
        facts["citeab_no_verdict_reason"] = (
            ("OGA's IF result is ICC-IF — cultured cells. Whether an epitope "
             "survives depends on how the antigen is presented, so tissue is a "
             "different question and the cultured-cell verdict does not carry "
             "over to it; IHC-IF is tissue despite its name. "
             if tissue else
             "OGA does not assess these applications at all, so there is no "
             "result to be had either way — this is not 'untested'. ")
            + "What OGA did find is in `oga_result`: report it as context for "
              "judging the reagent, and be explicit that it is not a result in "
              "the application this paper used.")
    notes = application_notes(raw_terms)
    if notes:
        # Kept apart from the row's own `application_notes`, which describe what
        # the MODEL read. Merging them would attribute CiteAb's term to the caller.
        facts["citeab_application_notes"] = notes
    if not facts["citeab_applications"]:
        # The 23% case: CiteAb records this paper citing this reagent and records
        # no application for it. A real and different statement from "not
        # recorded as using it", and one an empty list alone does not make.
        facts["citeab_link_recorded_without_application"] = True
    if record.get("ambiguous"):
        facts["citeab_application_ambiguous"] = (
            "CiteAb's term does not say which preparation — bare `IF` covers "
            "cultured cells and tissue alike, and OGA's IF verdict is cultured "
            "cells only. Reported, not applied.")
    return facts


def conflict(model_codes, citeab_codes):
    """Where the model's applications and CiteAb's disagree. Never who is right.

    Compares CODES to CODES, so the comparison is not an artefact of how the two
    sources spell things. Returns ``None`` unless BOTH sides said something and
    they differ — silence from either is not a disagreement, and reporting it as
    one would put a conflict on every reagent CiteAb has never seen.
    """
    model, citeab = set(model_codes or []), set(citeab_codes or [])
    if not model or not citeab or model == citeab:
        return None
    return {
        "you_read": sorted(model),
        "citeab_records": sorted(citeab),
        "only_you": sorted(model - citeab),
        "only_citeab": sorted(citeab - model),
        "compared_on": "OGA application codes",
        "note": ("These two sources disagree about what this paper used this "
                 "reagent for, and neither is authoritative. Ordinary "
                 "explanations: the paper used it more than once and one source "
                 "caught one use; CiteAb attributed the wrong product (their "
                 "join is on catalogue number, which is not unique across "
                 "suppliers); or the reagent appears in a section that was not "
                 "read. Report the disagreement — do not pick a side, and do not "
                 "merge the two lists."),
    }
