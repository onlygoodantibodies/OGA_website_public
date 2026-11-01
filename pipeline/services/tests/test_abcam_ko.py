"""Abcam's knockout lines on the feasibility page, each beside DepMap's figure
for its parental line.

The silent halves to pin: a parental Abcam spells ``U-87 MG`` must find
DepMap's ``U87MG`` (or every such line reads as missing data); ``HEK293T`` must
not borrow ``HEK293``'s number; and a line DepMap does not hold must read as
*not in DepMap*, never as *below threshold* — a blank read as "not expressed"
would steer somebody away from a usable control.
"""
from __future__ import annotations

from unittest import mock

ROWS = {
    "GENEX": [
        {"gene_name": "GENEX", "line_name": "GENEX KO U-87MG", "parental_line": "U-87 MG",
         "status": "Unvalidated", "catalogue": "", "note": "New product"},
        {"gene_name": "GENEX", "line_name": "GENEX KO HEK293T", "parental_line": "HEK293T",
         "status": "Pass", "catalogue": "ab1", "note": ""},
        {"gene_name": "GENEX", "line_name": "GENEX KO A-431", "parental_line": "A-431",
         "status": "Pass", "catalogue": "ab2", "note": ""},
    ],
}
EXPRESSION = {"found": True, "cell_lines": [
    {"name": "U87MG", "tpm": 4.1}, {"name": "HEK293", "tpm": 6.0}, {"name": "A431", "tpm": 1.2},
]}


# Mass spec measured U-87 MG and A-431 and detected the protein in one of them;
# it never measured HEK293T.
PROTEIN = ({"U87MG": 21.4}, {"U87MG", "A431"})


def _lookup(gene):
    from pipeline.services import abcam_ko
    with mock.patch.object(abcam_ko, "_by_gene", return_value=ROWS), \
         mock.patch.object(abcam_ko, "_protein_readout", return_value=PROTEIN):
        return abcam_ko.lookup(gene, EXPRESSION)


def test_parental_names_match_across_spellings_and_never_borrow():
    by_parent = {ln["parental_line"]: ln for ln in _lookup("genex")["lines"]}
    assert by_parent["U-87 MG"]["tpm"] == 4.1 and by_parent["U-87 MG"]["above_threshold"] is True
    assert by_parent["A-431"]["above_threshold"] is False
    hek = by_parent["HEK293T"]
    assert hek["in_depmap"] is False and hek["tpm"] is None and hek["above_threshold"] is None


def test_protein_is_three_answers_and_not_detected_is_not_not_measured():
    by_parent = {ln["parental_line"]: ln for ln in _lookup("GENEX")["lines"]}
    assert (by_parent["U-87 MG"]["protein"], by_parent["U-87 MG"]["protein_intensity"]) == ("detected", 21.4)
    assert by_parent["A-431"]["protein"] == "not_detected"
    assert by_parent["HEK293T"]["protein"] == "not_measured"


def test_lncap_matches_across_the_two_spellings():
    from pipeline.services import abcam_ko
    assert abcam_ko._line_key("LNCaP clone FGC") == abcam_ko._line_key("LnCaP")


def test_counts_and_verified_first():
    r = _lookup("GENEX")
    assert (r["found"], r["count"], r["pass_count"]) == (True, 3, 2)
    assert [ln["status"] for ln in r["lines"]] == ["Pass", "Pass", "Unvalidated"]


def test_a_gene_not_on_the_list_is_none_listed_not_an_error():
    r = _lookup("NOPE")
    assert r["found"] is False and r["error"] is None


def test_the_committed_list_loads_and_matches_the_workbook_totals():
    import pytest
    from OGA_website.public_snapshot import REASON, withheld
    from pipeline.services import abcam_ko
    if withheld(abcam_ko.CSV_PATH):
        pytest.skip(REASON)
    index = abcam_ko._by_gene()
    assert sum(len(v) for v in index.values()) == 2794
    assert len(index) == 1987
