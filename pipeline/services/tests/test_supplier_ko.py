"""Suppliers' knockout lines on the feasibility page — Abcam's and Thermo
Fisher's — each beside DepMap's figure for its parental line.

The silent halves to pin: a parental a supplier spells ``U-87 MG`` must find
DepMap's ``U87MG`` (or every such line reads as missing data); ``HEK293T`` must
not borrow ``HEK293``'s number; and a line DepMap does not hold must read as
*not in DepMap*, never as *below threshold* — a blank read as "not expressed"
would steer somebody away from a usable control. Thermo Fisher's sheet names
proteins, not genes, so its lines must be found under the gene its UniProt
accession names (HER4 is ERBB4), or a lookup by gene silently finds nothing.
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


def _lookup(gene, list_key="abcam"):
    from pipeline.services import supplier_ko
    with mock.patch.object(supplier_ko, "_by_gene", return_value=ROWS), \
         mock.patch.object(supplier_ko, "_protein_readout", return_value=PROTEIN):
        return supplier_ko.lookup(list_key, gene, EXPRESSION)


def _real_list(list_key):
    """The committed file, or a skip in the public copy, which leaves it out."""
    import pytest
    from OGA_website.public_snapshot import REASON, withheld
    from pipeline.services import supplier_ko
    if withheld(supplier_ko.LISTS[list_key].csv_path):
        pytest.skip(REASON)
    return supplier_ko._by_gene(list_key)


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
    from pipeline.services import supplier_ko
    assert supplier_ko._line_key("LNCaP clone FGC") == supplier_ko._line_key("LnCaP")


def test_counts_and_finished_first():
    r = _lookup("GENEX")
    assert (r["found"], r["count"], r["done_count"], r["done_word"]) == (True, 3, 2, "verified")
    assert [ln["status"] for ln in r["lines"]] == ["Pass", "Pass", "Unvalidated"]


def test_a_gene_not_on_the_list_is_none_listed_not_an_error():
    r = _lookup("NOPE")
    assert r["found"] is False and r["error"] is None


def test_the_abcam_list_matches_the_workbook_totals():
    index = _real_list("abcam")
    assert sum(len(v) for v in index.values()) == 2794
    assert len(index) == 1987


def test_the_thermo_list_is_keyed_on_the_gene_its_accession_names():
    index = _real_list("thermo")
    assert sum(len(v) for v in index.values()) == 305
    # The sheet's protein names are kept, but are not where a line is found.
    assert "HER4" not in index and index["ERBB4"][0]["protein_name"] == "HER4"
    assert "NUCLEOLIN" not in index and "NCL" in index
    assert index["SLC6A4"][0]["protein_name"] == "HTT" and "HTT" not in index


def test_thermo_counts_completed_lines():
    from pipeline.services import supplier_ko
    rows = {"GENEX": [
        {"gene_name": "GENEX", "uniprot": "P1", "protein_name": "GX", "parental_line": "HeLa",
         "status": "Completed", "roadmap": "RM_2021"},
        {"gene_name": "GENEX", "uniprot": "P1", "protein_name": "GX", "parental_line": "A431",
         "status": "KO confirmed by NGS - Application in progress", "roadmap": "RM_2023"},
    ]}
    with mock.patch.object(supplier_ko, "_by_gene", return_value=rows), \
         mock.patch.object(supplier_ko, "_protein_readout", return_value=PROTEIN):
        r = supplier_ko.lookup("thermo", "GENEX", EXPRESSION)
    assert (r["supplier"], r["count"], r["done_count"], r["done_word"]) == ("Thermo Fisher", 2, 1, "completed")
    assert r["lines"][0]["status"] == "Completed"
