"""A reagent whose declared target is not the protein the paper is discussing.

WHAT THIS DEFENDS is one sentence not being served. ``_MAN_NOTE`` tells every
caller that ``not_in_dataset`` means "untested, NOT unreliable; absence is not a
verdict on quality" — correct for an untested antibody, and for the eleven
documented product codes it told a reader to treat as an open question a reagent
whose own manufacturer states it does not bind the protein they asked about. A
documented problem handed over as unknown, which is the harmful direction and the
same failure the identifier normalisers exist to prevent, one field along.

The state that needs a test most is ``documented_as_declared``. Seventeen of the
406 p16 papers used the reagent CORRECTLY, for ARPC5, and a model handed "this
product is usually misused" will write the accusation anyway unless the row tells
it not to. That instruction is one string away from vanishing at all times, and
no other test in this suite would notice.

Nothing here is seeded into the test database: the notices come from committed
files under ``core/data/target_confusions/``, so they are the real lists.
"""
from __future__ import annotations

import pytest

from core import target_confusions
from mcp_servers.common import confusions, portal

# The Nature paper the p16 list records as a mistaken use of ab51243.
MISTAKEN_DOI = "10.1038/s41586-019-0885-0"
# A paper the list read and recorded as a CORRECT use of the ARPC5 antibody.
AS_DECLARED_DOI = "10.1038/jhg.2011.126"
# On the list; the reviewer could not reach the full text.
NOT_CHECKED_DOI = "10.1016/j.bcp.2022.114935"


def _ab(identifier, **kw):
    return {"identifier": identifier, "role": "primary", **kw}


def _entry(res, identifier):
    for e in res["not_in_dataset"]:
        if e["identifier"] == identifier:
            return e
    for bucket in res["antibody_hits"].values():
        for hit in bucket:
            if hit["identifier"] == identifier:
                return hit
    raise AssertionError(f"{identifier} is in neither half of the reply")


# ── the lists are on file at all ─────────────────────────────────────────────

def test_the_lists_load_and_nothing_was_dropped():
    # A dropped row makes a documented paper read as one nobody reviewed, which
    # is the failure `core.W005` exists to say out loud.
    assert target_confusions.problems() == ()
    ids = {n["id"] for n in target_confusions.load()}
    assert {"p16-ink4a", "beta-galactosidase"} <= ids


def test_none_of_the_documented_codes_is_in_the_dataset():
    """The premise the whole feature rests on, checked rather than assumed.

    If one of these ever IS characterised, the reply has to carry a verdict AND
    the mismatch, and this test failing is the notice that the day has come.
    """
    codes = [a["identifier"] for n in target_confusions.load()
             for a in n["antibodies"]]
    res = portal.check_manuscript(reagents=[_ab(c) for c in codes])
    resolved = [h["identifier"] for b in res["antibody_hits"].values() for h in b]
    assert resolved == [], (
        "a documented product code now resolves to an OGA record: check that "
        "the reply carries both the verdict and `target_confusion`")


# ── check_manuscript ────────────────────────────────────────────────────────

def test_a_documented_code_is_not_served_as_a_plain_absence():
    res = portal.check_manuscript(reagents=[_ab("ab51243", target="p16")])
    block = _entry(res, "ab51243")["target_confusion"]
    assert block["declared_target"] == "ARPC5 (p16-ARC)"
    assert block["commonly_bought_for"] == "CDKN2A (p16-INK4a)"
    assert block["supplier"] == "Abcam"
    assert "no observed cross reactivity" in block["supplier_statement"].lower()
    assert block["documented_in"]["url"].startswith("https://forbetterscience.com/")
    # And the reply as a whole says to read it rather than the absence framing.
    assert "target_confusion_note" in res
    assert res["counts"]["target_confusions"] == 1


def test_the_standing_note_carves_itself_out():
    # The note is what a caller reads to learn what `not_in_dataset` means, so
    # the exception has to be IN it — a caveat that lives only on the row is one
    # a model meets after it has already believed the general rule.
    assert "target_confusion" in portal._MAN_NOTE
    assert "not an absence" in confusions.reply_note([{"x": 1}])


def test_nothing_in_the_block_is_a_performance_claim():
    res = portal.check_manuscript(reagents=[_ab("ab51243")])
    block = _entry(res, "ab51243")["target_confusion"]
    assert "NOT a performance verdict" in block["not_a_performance_verdict"]
    assert "has not characterised this antibody" in block["not_a_performance_verdict"]
    # No verdict fields anywhere on it, however tempting the shape.
    assert not {"applications", "assessment", "recommended_for"} & set(block)


@pytest.mark.parametrize("doi,state", [
    (MISTAKEN_DOI, "documented_as_mistaken"),
    (AS_DECLARED_DOI, "documented_as_declared"),
    (NOT_CHECKED_DOI, "documented_not_checked"),
    ("10.9999/not-a-real-paper", "not_on_the_list"),
    (None, "no_paper_identifier"),
])
def test_all_four_paper_states_are_drawn_apart(doi, state):
    res = portal.check_manuscript(reagents=[_ab("ab51243")], paper_doi=doi)
    assert _entry(res, "ab51243")["target_confusion"]["this_paper"]["status"] == state


def test_a_correct_use_is_told_to_raise_no_concern():
    res = portal.check_manuscript(reagents=[_ab("ab51243")],
                                  paper_doi=AS_DECLARED_DOI)
    note = _entry(res, "ab51243")["target_confusion"]["this_paper"]["note"]
    assert "RAISE NO CONCERN" in note
    assert "false accusation" in note
    # It must not read as a softened version of the accusation.
    assert "may have used the wrong" not in note.lower()


def test_an_unchecked_paper_is_not_told_it_was_wrong():
    res = portal.check_manuscript(reagents=[_ab("ab51243")],
                                  paper_doi=NOT_CHECKED_DOI)
    note = _entry(res, "ab51243")["target_confusion"]["this_paper"]["note"]
    assert "could not be reached" in note
    assert "Do NOT report it as a documented misuse" in note


def test_no_doi_and_an_unlisted_doi_say_different_things():
    """Two absences, and only one of them is about the paper.

    The same distinction `citeab.status` makes between `unavailable` and
    `not_covered`: "nobody asked" is a fact about the call, "asked and not
    found" is a fact about the list.
    """
    without = portal.check_manuscript(reagents=[_ab("ab51243")])
    unlisted = portal.check_manuscript(reagents=[_ab("ab51243")],
                                       paper_doi="10.9999/nope")
    a = _entry(without, "ab51243")["target_confusion"]["this_paper"]
    b = _entry(unlisted, "ab51243")["target_confusion"]["this_paper"]
    assert a["status"] != b["status"]
    assert "Pass `paper_doi`" in a["note"]
    assert "not on the review's list" in b["note"]


def test_the_verdict_is_per_paper_AND_per_product():
    """A paper on a list is not a paper on a list about the reagent in hand.

    `10.1016/j.exger.2019.110805` is listed for sc-166760. Reporting its verdict
    against ab51243 would be the citation layer's wrong-paper failure one field
    along, and it is invisible: both codes are on the same list.
    """
    res = portal.check_manuscript(reagents=[_ab("ab51243"), _ab("sc-166760")],
                                  paper_doi="10.1016/j.exger.2019.110805")
    assert _entry(res, "sc-166760")["target_confusion"]["this_paper"]["status"] \
        == "documented_not_checked"
    assert _entry(res, "ab51243")["target_confusion"]["this_paper"]["status"] \
        == "not_on_the_list"


def test_an_ordinary_reagent_in_a_listed_paper_carries_nothing():
    res = portal.check_manuscript(reagents=[_ab("ab212184", target="SNCA")],
                                  paper_doi=MISTAKEN_DOI)
    assert "target_confusion" not in _entry(res, "ab212184")
    assert "target_confusion_note" not in res


# ── the paper half on its own ───────────────────────────────────────────────

def test_a_listed_paper_is_reported_even_with_no_reagent_sent():
    # A caller holding the paper and none of its antibodies is still holding a
    # paper somebody reviewed. A reply that knows and does not say is worse.
    res = portal.check_manuscript(reagents=[], paper_doi=MISTAKEN_DOI)
    listed = res["paper_target_confusions"]
    assert [r["product_code"] for r in listed] == ["ab51243"]
    assert listed[0]["status"] == "documented_as_mistaken"


def test_an_unlisted_paper_gets_no_paper_block_at_all():
    res = portal.check_manuscript(reagents=[], paper_doi="10.9999/nope")
    assert "paper_target_confusions" not in res


def test_a_paper_on_both_lists_reports_both():
    # Three DOIs appear on both reviews. Serving one and dropping the other
    # would be a silent omission on exactly the papers with two problems.
    res = portal.check_manuscript(reagents=[], paper_doi="10.1139/bcb-2018-0126")
    # Each code in the SUPPLIER's spelling, not the reviewer's: the two sheets
    # disagree about the case of the same Abcam product (`AB9361` against
    # `ab9361`), and what a reader will search for is the one on the datasheet.
    assert {r["product_code"] for r in res["paper_target_confusions"]} \
        == {"ab51243", "ab9361"}


# ── the identifier, however the paper printed it ────────────────────────────

@pytest.mark.parametrize("printed", [
    "ab51243", "AB51243", "#ab51243", "cat. no. ab51243", "Abcam ab51243",
    "ab‑51243", "sc-166760", "sc–166760", "14-6773-81",
])
def test_typesetting_cannot_hide_a_notice(printed):
    """The same forms the dataset lookup tries, asked in the same order.

    A reagent that resolves in the browser extension and comes back clean here
    is the exact failure the shared normalisers exist to prevent — and it is the
    harmful direction, since this reply is the one a model quotes.
    """
    res = portal.check_manuscript(reagents=[_ab(printed)])
    assert _entry(res, printed).get("target_confusion"), printed


def test_a_short_code_needing_its_supplier_still_resolves_here():
    """The extension gates AB986 on the supplier being named; this does not.

    Deliberately different, and not a drift: the extension TOKENISES A PAGE and
    can meet `AB986` in any sentence, where Millipore's number is typeset exactly
    like an Abcam one. Here a model has read the Methods and passed one reagent
    it identified as an antibody, which is a far better source — the same
    asymmetry that lets this server scope a verdict per application while the
    extension may not.
    """
    res = portal.check_manuscript(reagents=[_ab("AB986")])
    block = _entry(res, "AB986")["target_confusion"]
    assert block["declared_target"].startswith("lacZ")
    assert block["supplier"] == "Merck Millipore"


def test_the_reply_never_renames_the_readers_reagent():
    res = portal.check_manuscript(reagents=[_ab("cat. no. AB51243")])
    block = _entry(res, "cat. no. AB51243")["target_confusion"]
    # What the reader will search for, beside what resolved.
    assert block["product_code"] == "ab51243"
    assert block["matched_on"] in ("AB51243", "ab51243")


# ── the single-antibody lookup ──────────────────────────────────────────────

def test_the_one_antibody_lookup_carries_it_too():
    # The reply most at risk: no paper around it to soften a bare "absence is
    # not a judgement about the antibody".
    assert portal.confusion_lookup(catalogue="ab51243")["declared_target"] \
        == "ARPC5 (p16-ARC)"
    assert portal.confusion_lookup(catalogue="ab212184") is None
    assert portal.confusion_lookup() is None


# ── the controls assessment ─────────────────────────────────────────────────

def test_a_controls_row_carries_the_notice():
    """Where it matters most: a paper can show a flawless knockout control for a
    protein its antibody does not bind, and the rubric cannot see that."""
    res = portal.scan_controls(
        reagents=[_ab("ab51243", target="p16", figures=["Fig 1"])],
        controls=[], paper_doi=MISTAKEN_DOI)
    row = next(r for r in res["table"] if r["antibody"] == "ab51243")
    assert row["target_confusion"]["declared_target"] == "ARPC5 (p16-ARC)"
    assert "target_confusion_note" in res
    # And it must be in `table`, which the tool tells the caller to render, not
    # in `others` — whose own description is "untested is NOT a verdict on
    # quality", the sentence this notice exists to carve an exception out of.
    # `_matters` dropped it there until 12 Sep 2026, reduced to two keys.
    assert "ab51243" not in [o["antibody"] for o in res["others"]["antibodies"]]


# ─────────────────────────────────────────────────────────────────────────────
# A list that may only speak about the papers it names.
#
# A notice is found by the product code, so without a listed DOI the only thing
# either tool can say is that the paper MAY have used the wrong antibody. That
# is a fair caution where the product is mostly misused — 317 of the 406 p16
# papers used `ab51243` as a p16-INK4a antibody — and the wrong bet on PERK,
# where `ab65142` is a perfectly good PERK antibody and nearly every paper
# citing it used it correctly for the unfolded protein response.
#
# `papers_only` is per notice, so both directions are pinned: the PERK lists go
# quiet off their own papers, and the two 2026 For Better Science lists keep the
# caution they shipped with.
# ─────────────────────────────────────────────────────────────────────────────

#: On the PERK list: a 2016 Oncotarget paper that stained p-ERK with ab65142.
PERK_LISTED_DOI = "10.18632/oncotarget.10087"
UNLISTED_DOI = "10.1038/nature12345"


class TestAPaperIsNamedThreeWays:
    """The connector was already given the PMID and threw it away.

    ``check_manuscript`` has taken ``paper_pmid``, ``paper_title`` and
    ``paper_year`` since it shipped, and hands all four to the citation layer —
    but the declared-target lookup asked on ``paper_doi`` alone. So a caller who
    named a paper by PubMed id got the citation answer and, silently, no notice.

    It matters most where a DOI does not exist to pass: four of the PERK papers
    are e-Century titles, which register none with Crossref at all.
    """

    PMID = "36915767"      # Am J Transl Res 2023, listed against #3179
    CODE = "#3179"

    def test_a_paper_named_by_pmid_gets_its_verdict(self):
        reply = portal.check_manuscript(reagents=[_ab(self.CODE)],
                                        paper_pmid=self.PMID)
        block = _entry(reply, self.CODE)["target_confusion"]["this_paper"]
        assert block["status"] == "documented_as_mistaken"

    def test_a_pmid_and_a_doi_agree_about_the_same_paper(self):
        # The p16 list is keyed on DOIs, so this asks the other direction: a
        # caller passing a DOI must be unaffected by the widening.
        reply = portal.check_manuscript(reagents=[_ab("ab51243")],
                                        paper_doi="10.1038/s41586-019-0885-0")
        block = _entry(reply, "ab51243")["target_confusion"]["this_paper"]
        assert block["status"] == "documented_as_mistaken"

    def test_naming_a_paper_by_pmid_alone_is_naming_a_paper(self):
        # `no_paper_identifier` means "the caller named no paper", and a PMID
        # is naming one — so an unlisted PMID must read as "asked and not on
        # the list", not as "nobody asked".
        listed = portal.check_manuscript(reagents=[_ab("ab51243")],
                                         paper_pmid="99999999")
        silent = portal.check_manuscript(reagents=[_ab("ab51243")])
        a = _entry(listed, "ab51243")["target_confusion"]["this_paper"]
        b = _entry(silent, "ab51243")["target_confusion"]["this_paper"]
        assert a["status"] != b["status"]
        assert a["status"] == "not_on_the_list"

    def test_the_paper_level_block_answers_to_a_pmid_too(self):
        # `paper_target_confusions` is served even when the caller sent no
        # reagent the lists name, and it asked on the DOI alone.
        reply = portal.check_manuscript(reagents=[_ab("ab-not-a-real-code")],
                                        paper_pmid="36105027")
        listed = reply.get("paper_target_confusions") or []
        assert [r["product_code"] for r in listed] == ["ab229912"]

    def test_the_four_papers_with_no_doi_all_resolve(self):
        for pmid, code in (("36915767", "#3179"), ("32194908", "#3192"),
                           ("36105027", "ab229912"), ("34873488", "ab229912")):
            assert target_confusions.verdict_for(None, code, pmid=pmid) == \
                target_confusions.MISTAKEN, f"{pmid} / {code}"


class TestPapersOnlyNotices:
    def test_a_listed_paper_is_reported_plainly(self):
        block = confusions.for_identifier(["ab65142"], doi=PERK_LISTED_DOI)
        assert block is not None
        assert block["this_paper"]["status"] == "documented_as_mistaken"
        assert "EIF2AK3" in block["declared_target"]
        assert "ERK" in block["commonly_bought_for"]

    def test_an_unlisted_paper_gets_no_block_at_all(self):
        """The reagent falls through to `not_in_dataset`, which is the true
        thing to say: OGA has not tested it, and nothing is known about how
        this paper used it. A block here would carry the note ending "the
        strongest thing you may say is that the paper MAY have used the wrong
        antibody" — a false accusation against a paper that very likely used a
        PERK antibody for PERK."""
        assert confusions.for_identifier(["ab65142"], doi=UNLISTED_DOI) is None

    def test_no_doi_gets_no_block_either(self):
        """Without a DOI the connector cannot tell a correct PERK paper from a
        listed one, and the note it would otherwise carry is the accusation."""
        assert confusions.for_identifier(["ab65142"]) is None

    def test_the_reverse_direction_is_its_own_notice(self):
        """sc-7383 is a p-ERK antibody used for PERK. One declared target per
        notice, so the other direction is a second list rather than a field."""
        block = confusions.for_identifier(
            ["sc-7383"], doi="10.1016/j.metabol.2009.04.002")
        assert block is not None
        assert block["this_paper"]["status"] == "documented_as_mistaken"
        assert block["commonly_bought_for_gene"] == "EIF2AK3"

    def test_the_p16_list_still_cautions_on_an_unlisted_paper(self):
        """The flag is per notice, and this is what makes that true."""
        block = confusions.for_identifier(["ab51243"], doi=UNLISTED_DOI)
        assert block is not None
        assert block["this_paper"]["status"] == "not_on_the_list"
        assert "MAY have used the wrong antibody" in block["this_paper"]["note"]

    def test_and_still_cautions_when_no_doi_was_supplied(self):
        block = confusions.for_identifier(["ab51243"])
        assert block is not None
        assert block["this_paper"]["status"] == "no_paper_identifier"
