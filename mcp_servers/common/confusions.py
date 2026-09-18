"""Antibodies whose DECLARED target is not the one the paper is discussing.

One step before every verdict this server serves. ``antibody_validation`` and
``check_manuscript`` answer *how did this antibody perform when it was tested*;
this answers *is it an antibody to that protein at all*, which is not a question
the dataset can be asked — it has never tested any of the listed product codes
across the two lists.

WHY THIS IS NOT A COSMETIC ADDITION
-----------------------------------
Without it the connector's answer was not merely thin, it was wrong in the
harmful direction. ``ab51243`` resolved to no record, so it was filed under
``not_in_dataset``, and ``_MAN_NOTE`` — served on every reply — instructs the
caller that ``not_in_dataset`` means "untested, NOT unreliable; absence is not a
verdict on quality". That sentence exists to stop a reader inferring a failure
from a gap, and it is exactly right for an untested antibody. Here it told a
reader to treat as an open question a reagent whose own manufacturer states it
does not bind the protein in the paper. A documented problem handed over as
unknown, which is the same failure the identifier normalisers exist to prevent,
one field along.

THE PAPER HALF IS THE HALF THE CALLER HAS TO BE STOPPED FROM INVENTING
----------------------------------------------------------------------
``core.target_confusions`` records a verdict per (DOI, product code) pair, and
**all four states are drawn apart**:

``documented_as_mistaken``   the review read this paper and recorded it as using
                             the reagent against the mistaken target.
``documented_as_declared``   the review read it and recorded a CORRECT use, for
                             the target the antibody is sold against. Seventeen
                             of the p16 papers. The note tells the caller to say
                             so and **not** to raise a concern — a model handed
                             "this reagent is often misused" and a paper that
                             used it properly will write the accusation anyway
                             unless the row says outright not to.
``documented_not_checked``   on the list, full text unreachable, so the reviewer
                             could not say. Seventy-two of them.
``not_on_the_list`` /
``no_paper_identifier``      the product fact stands and nothing about this paper
                             is known. The strongest permitted sentence is *may
                             have*, and the note says so in those words.

Same rule as ``control_status`` in the controls rubric: three-valued, surfaced in
its own words, never collapsed into the harder answer. The server cannot tell the
states apart from the text, so it does not try — it reports which one the list
holds.

ONE READER WITH THE EXTENSION
-----------------------------
Both tools read ``core.target_confusions``, the same arrangement as
``core/citations.py`` behind ``citeab.py``. The extension draws its red mark from
``index_payload()``; this serves the same fields as prose. A reagent that lights
up red on a publisher's page and comes back clean here is the failure that
arrangement exists to prevent.
"""

from __future__ import annotations

from core import target_confusions as _confusions

#: Said on every row that carries a notice, because the whole risk in this
#: feature is a model reading it as a verdict. It is not one: OGA has tested none
#: of these products, and nothing here is evidence about how well any of them
#: detects the protein it IS raised against.
NOT_A_VERDICT = (
    "This is NOT a performance verdict and NOT an OGA result. OGA has not "
    "characterised this antibody. What is documented is which protein it is "
    "raised against, from the supplier's own datasheet, and a published review "
    "of papers that cited it. Report it as a question about reagent identity — "
    "never as evidence that the antibody is of poor quality, and never as an "
    "OGA recommendation."
)

#: What the caller may say about the paper, per state. Written as instructions
#: because the states are asymmetric and the model cannot infer that: three of
#: them permit a concern and one FORBIDS one.
_PAPER_NOTES = {
    "documented_as_mistaken": (
        "The review read this paper and recorded it as using this antibody "
        "against {mistaken}. State that plainly, name the review, and link it. "
        "Do not soften it to 'may have' — this paper is on the list."),
    "documented_as_declared": (
        "The review read this paper and recorded a CORRECT use: it used this "
        "antibody for {declared}, the target it is sold against. RAISE NO "
        "CONCERN about this reagent in this paper. If the reader asks, say the "
        "use was reviewed and found correct. Reporting it as a possible error "
        "would be a false accusation against a paper somebody checked."),
    "documented_not_checked": (
        "This paper is on the review's list, but its full text could not be "
        "reached, so the reviewer could not establish which protein the "
        "antibody was used for. Say that it could not be established. Do NOT "
        "report it as a documented misuse, and do not assume it followed the "
        "majority."),
    "not_on_the_list": (
        "This paper is not on the review's list, so nothing is known about how "
        "it used the reagent. The strongest thing you may say is that the paper "
        "MAY have used the wrong antibody, and the reason is the product fact "
        "above, not anything about this paper."),
    "no_paper_identifier": (
        "No paper DOI was supplied, so the review's list could not be consulted "
        "for this paper. Pass `paper_doi` to have it checked. Until then the "
        "strongest thing you may say is that the paper MAY have used the wrong "
        "antibody."),
}

#: The two states that say nothing about the paper in front of the caller.
#:
#: Both notes end "the strongest thing you may say is that the paper MAY have
#: used the wrong antibody", which is a fair caution where the product is mostly
#: misused and a false accusation where it is not. A `papers_only` notice is
#: withheld entirely in these two states -- see `for_identifier`.
_NOTHING_KNOWN = ("not_on_the_list", "no_paper_identifier")

_STATE_FOR_VERDICT = {
    _confusions.MISTAKEN: "documented_as_mistaken",
    _confusions.AS_DECLARED: "documented_as_declared",
    _confusions.NOT_CHECKED: "documented_not_checked",
}


def _source(notice):
    source = dict(notice.get("source") or {})
    return {k: source.get(k) for k in ("title", "author", "publication", "url", "date")}


def _paper_block(notice, antibody, doi, doi_supplied, pmid=None,
                 title=None, year=None):
    if not doi_supplied:
        state = "no_paper_identifier"
    else:
        verdict = _confusions.verdict_for(doi, antibody["identifier"],
                                          pmid=pmid, title=title, year=year)
        state = _STATE_FOR_VERDICT.get(verdict, "not_on_the_list")
    return {
        "status": state,
        "note": _PAPER_NOTES[state].format(
            mistaken=notice["mistaken_for"]["protein"],
            declared=notice["declared_target"]["protein"]),
    }


def for_identifier(forms, doi=None, doi_supplied=None, pmid=None,
                   title=None, year=None):
    """The ``target_confusion`` block for one reagent, or ``None``.

    ``forms`` is every spelling of the identifier the caller gave, in the order
    ``manuscript.resolution_variants`` produced them — as-given first, so a later
    form can only ever ADD a match. That is the same ordering the dataset lookup
    uses, and asking the two questions the same way is what keeps a reagent from
    resolving in one and missing in the other.

    ``doi_supplied`` is kept apart from ``doi`` being truthy on purpose: "the
    caller passed no paper identifier" and "the caller named a paper that is on
    no list" are different facts and the reply says which. Its name predates the
    PMID and title keys and is kept rather than churned through the callers; what
    it means is *the caller named the paper somehow*.
    """
    if doi_supplied is None:
        doi_supplied = bool(doi or pmid or (title and year))
    for form in forms or []:
        notice, antibody = _confusions.for_identifier(form)
        if notice is None:
            continue
        paper = _paper_block(notice, antibody, doi, doi_supplied,
                             pmid=pmid, title=title, year=year)
        if notice.get("papers_only") and paper["status"] in _NOTHING_KNOWN:
            # SOME LISTS MAY ONLY SPEAK ABOUT A PAPER THEY NAME, and this is the
            # half of that rule the connector owns.
            #
            # A notice is found by the product code, so without a listed DOI the
            # only thing either tool can say is "this paper MAY have used the
            # wrong antibody". That is a fair caution where the product is
            # mostly misused -- 317 of the 406 p16 papers used `ab51243` as a
            # p16-INK4a antibody. It is the wrong bet on PERK: `ab65142` is a
            # perfectly good PERK antibody and nearly every paper citing it used
            # it correctly for the unfolded protein response, so the caution
            # would be a false accusation against a correct paper. Same
            # reasoning, and the same flag, as `matcher.js`; the two tools must
            # not answer this question differently.
            #
            # Withheld rather than reworded: the reagent then falls through to
            # `not_in_dataset`, whose standing note is the true thing to say --
            # OGA has not tested it, and nothing is known about this paper's use
            # of it. Telling a caller with no DOI to supply one WOULD be useful
            # and carries no accusation; it is left out because it would have
            # the connector volunteer something the extension stays silent
            # about, and that is the owner's call rather than a detail.
            #
            # `continue`, not `return None`: a later spelling of the identifier
            # may legitimately match a different notice.
            continue
        declared, mistaken = notice["declared_target"], notice["mistaken_for"]
        block = {
            "declared_target": f"{declared['gene']} ({declared['protein']})",
            "declared_target_gene": declared["gene"],
            "commonly_bought_for": f"{mistaken['gene']} ({mistaken['protein']})",
            "commonly_bought_for_gene": mistaken["gene"],
            # The supplier's spelling of the code, beside the caller's. A reply
            # that silently renamed the reader's reagent would be harder to check
            # than one that did not -- the rule `normalise_identifier` states.
            "product_code": antibody["identifier"],
            "matched_on": form,
            "supplier": antibody.get("supplier"),
            "explanation": notice.get("explanation") or notice["short"],
            "documented_in": _source(notice),
            "papers_reviewed": _confusions.paper_counts(notice)["papers"],
            "papers_recorded_as_mistaken":
                _confusions.paper_counts(notice)[_confusions.MISTAKEN],
            "this_paper": paper,
            "not_a_performance_verdict": NOT_A_VERDICT,
        }
        if antibody.get("supplier_statement"):
            # The manufacturer's own words. The strongest evidence in the block
            # and the only part of it that is not ours, so it is quoted verbatim
            # and attributed rather than paraphrased into our sentence.
            block["supplier_statement"] = antibody["supplier_statement"]
        return block
    return None


def for_paper(doi, pmid=None, title=None, year=None):
    """What the lists record for one paper, whatever reagents the caller sent.

    Answers on the paper alone, so a caller that passed a paper and no reagents --
    or passed a different reagent from the one the list names -- is still told
    the paper is documented and which product code it is documented for. The
    alternative is a reply that knows something about the paper in front of the
    reader and does not mention it.

    ``[]`` means no list holds this paper, which is not evidence the paper is
    clean: these lists are two people's reviews of three and eight product codes,
    and nothing else.
    """
    found = []
    for record in _confusions.for_paper(doi, pmid=pmid, title=title, year=year):
        notice = record["notice"]
        declared, mistaken = notice["declared_target"], notice["mistaken_for"]
        state = _STATE_FOR_VERDICT.get(record["verdict"], "not_on_the_list")
        found.append({
            "product_code": record["identifier"],
            "supplier": record.get("supplier"),
            "declared_target": f"{declared['gene']} ({declared['protein']})",
            "commonly_bought_for": f"{mistaken['gene']} ({mistaken['protein']})",
            "status": state,
            "note": _PAPER_NOTES[state].format(
                mistaken=mistaken["protein"], declared=declared["protein"]),
            "documented_in": _source(notice),
        })
    return found


def reply_note(blocks):
    """The paragraph a reply carries when any row holds a notice, or ``None``.

    Served only when there is something for it to be about. A standing note on
    every reply is one a caller learns to skip, and this one has to be read.
    """
    if not blocks:
        return None
    return (
        "TARGET CONFUSION — read before reporting any of these reagents. One or "
        "more reagents in this paper is an antibody to a DIFFERENT protein from "
        "the one whose name it shares, per the supplier's own datasheet. This is "
        "a question about reagent IDENTITY, not about quality, and it is not an "
        "OGA verdict: OGA has not characterised these products. Report the "
        "declared target, name and link the review under `documented_in`, and "
        "follow `this_paper.note` exactly — it differs per paper, and one of its "
        "states records a CORRECT use that you must not raise a concern about. "
        "Do not fold a reagent carrying this into `not_in_dataset`'s framing: "
        "absence from the dataset is not a verdict on quality, but this is not "
        "an absence, it is a documented mismatch."
    )
