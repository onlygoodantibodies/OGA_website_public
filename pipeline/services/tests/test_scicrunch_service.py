"""Tests for the SciCrunch service: the gene antibody-count (feasibility) and the
shared catalogue->RRID resolver (antibody entry). HTTP is mocked — no network.

Schema mirrors the live registry _source confirmed 2026-07: target at
antibodies.primary[].targets[].name, clonality at
antibodies.primary[].clonality.name, catalogue at vendors[].catalogNumber.

**``targets[].name`` is a product title, not a gene symbol**, and the fixtures
in the first half of this file do not show that — they carry a bare ``APOE``
where live returns ``ADAM10 antibody [EPR5622]``. That gap let an equality
comparison ship and report "registry target gene differs" on every exact
catalogue-and-vendor match. The second half of the file uses the real shape;
add new fixtures there, not above.
"""
from __future__ import annotations

from pipeline.services import scicrunch


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _hit(gene, clonality, vendor, cat):
    return {"_source": {
        "item": {"identifier": f"AB_{cat}"},
        "antibodies": {"primary": [{"clonality": {"name": clonality},
                                    "targets": [{"name": gene}]}]},
        "vendors": [{"name": vendor, "catalogNumber": cat}],
    }}


def test_extract_clonality():
    src = {"antibodies": {"primary": [{"clonality": {"name": "Recombinant"}}]}}
    assert scicrunch._extract_clonality(src) == "recombinant"
    assert scicrunch._extract_clonality({}) == ""


def test_count_antibodies_tally_and_gene_filter(monkeypatch):
    monkeypatch.setattr(scicrunch, "API_KEY", "testkey")
    data = {"hits": {"hits": [
        _hit("APOE", "recombinant", "Abcam", "1"),
        _hit("APOE", "polyclonal", "Proteintech", "2"),
        _hit("APOE", "monoclonal", "Abcam", "3"),
        _hit("OTHERGENE", "polyclonal", "Abcam", "4"),   # different target → excluded
    ]}}
    monkeypatch.setattr(scicrunch.requests, "get", lambda *a, **k: _FakeResp(data))

    r = scicrunch.count_antibodies("APOE")
    assert r["found"] is True
    assert r["total_count"] == 3                 # the OTHERGENE hit is filtered out
    assert r["recombinant_count"] == 1
    assert r["monoclonal_count"] == 1
    assert r["polyclonal_count"] == 1
    vendors = {v["vendor"]: v["count"] for v in r["top_vendors"]}
    assert vendors == {"Abcam": 2, "Proteintech": 1}


def test_count_antibodies_falls_back_when_scoped_query_errors(monkeypatch):
    import requests as _rq
    monkeypatch.setattr(scicrunch, "API_KEY", "testkey")
    on_target = {"hits": {"hits": [_hit("APOE", "polyclonal", "Abcam", "9")]}}

    def _get(url, params=None, **kw):
        q = (params or {}).get("q", "")
        if q.startswith("antibodies.primary.targets.name"):
            raise _rq.exceptions.RequestException("field not supported")  # scoped query 400s
        return _FakeResp(on_target)                                       # vanilla succeeds

    monkeypatch.setattr(scicrunch.requests, "get", _get)
    r = scicrunch.count_antibodies("APOE")
    assert r["found"] is True and r["total_count"] == 1


def test_count_antibodies_requires_key(monkeypatch):
    monkeypatch.setattr(scicrunch, "API_KEY", "")
    r = scicrunch.count_antibodies("APOE")
    assert r["found"] is False and "not set" in r["error"]


def test_count_antibodies_none_found(monkeypatch):
    monkeypatch.setattr(scicrunch, "API_KEY", "testkey")
    monkeypatch.setattr(scicrunch.requests, "get", lambda *a, **k: _FakeResp({"hits": {"hits": []}}))
    r = scicrunch.count_antibodies("ZZZ")
    assert r["found"] is False and r["total_count"] == 0


def test_resolve_rrid_high(monkeypatch):
    def _fake(cat, size=25):
        return {"found": True, "error": None, "records": [
            {"rrid": "AB_302669", "genes": ["APOE"],
             "vendors": [{"vendor": "Abcam", "catalogue": "ab1907", "url": "https://x"}]}]}
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue", _fake)
    rrid, url, note = scicrunch.resolve_rrid("ab1907", "Abcam", "APOE")
    assert rrid == "AB_302669" and url == "https://x" and "confirmed" in note


def test_resolve_rrid_gene_mismatch_returns_none(monkeypatch):
    def _fake(cat, size=25):
        return {"found": True, "error": None, "records": [
            {"rrid": "AB_1", "genes": ["PSMC3"],
             "vendors": [{"vendor": "Abcam", "catalogue": "x", "url": ""}]}]}
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue", _fake)
    rrid, _url, note = scicrunch.resolve_rrid("x", "Abcam", "APOE")
    assert rrid is None and "gene differs" in note


def test_resolve_rrid_no_record(monkeypatch):
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: {"found": False, "records": [], "error": "none"})
    rrid, _url, _note = scicrunch.resolve_rrid("nope", "Abcam", "APOE")
    assert rrid is None


def test_resolve_rrid_never_raises(monkeypatch):
    def _boom(cat, size=25):
        raise RuntimeError("network down")
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue", _boom)
    rrid, _url, note = scicrunch.resolve_rrid("x", "Abcam", "APOE")
    assert rrid is None and "lookup failed" in note


# --- the registry's target is a product TITLE, not a gene symbol -----------
#
# Everything below is the shape live actually returns, confirmed against the
# registry on 30 Aug 2026 while backfilling NR3C1. The fixtures above carry a
# bare `APOE` where the real field holds `ADAM10 antibody [EPR5622]`, and that
# difference is the whole reason the equality check shipped: the suite could
# not see it, and against live it turned every exact catalogue-and-vendor match
# into "registry target gene differs" and filled nothing.

def _record(target, *, vendor="Abcam", catalogue="ab124695", rrid="AB_10972023",
            url="https://x"):
    return {"found": True, "error": None, "records": [
        {"rrid": rrid, "genes": [target],
         "vendors": [{"vendor": vendor, "catalogue": catalogue, "url": url}]}]}


def test_gene_is_matched_as_a_word_of_the_registry_title(monkeypatch):
    """The live ADAM10 case: exact catalogue, exact vendor, and the symbol is
    the first word of the title. This must fill, not go to review."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("ADAM10 antibody [EPR5622]"))
    rrid, url, note = scicrunch.resolve_rrid("ab124695", "Abcam", "ADAM10")
    assert rrid == "AB_10972023"
    assert "gene" in note and "vendor" in note


def test_a_title_naming_the_protein_does_not_veto_a_vendor_match(monkeypatch):
    """NR3C1's Abcam records are titled 'Glucocorticoid Receptor antibody'.
    Our symbol is absent, which says nothing either way — the catalogue and the
    vendor still identify the product, so it fills and the note says which
    signal carried it."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record(
                            "Glucocorticoid Receptor antibody",
                            catalogue="ab302677", rrid="AB_3096001"))
    rrid, _url, note = scicrunch.resolve_rrid("ab302677", "Abcam", "NR3C1")
    assert rrid == "AB_3096001"
    assert "vendor" in note and "gene" not in note


def test_a_bare_symbol_that_differs_still_vetoes(monkeypatch):
    """The check that earns its keep. A registry target of exactly `PSMC3`
    against our `APOE` is a claim we can contradict, so it is never written —
    either our gene is wrong or the catalogue is, and both want a human."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("PSMC3", catalogue="x"))
    rrid, _url, note = scicrunch.resolve_rrid("x", "Abcam", "APOE")
    assert rrid is None and "gene differs" in note


def test_a_prefix_of_another_symbol_is_not_a_match(monkeypatch):
    """`ADAM1` must not match `ADAM10 antibody`. Vendor is deliberately wrong
    so nothing else can carry the record — a substring test would fill here."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("ADAM10 antibody [EPR5622]",
                                                     vendor="Some Other Co"))
    rrid, _url, note = scicrunch.resolve_rrid("ab124695", "Abcam", "ADAM1")
    assert rrid is None and "could not confirm" in note


def test_a_hyphenated_symbol_matches(monkeypatch):
    """A hyphen is inside plenty of symbols, so the tokeniser must not split on
    it or `NKX2-1` could never match."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("NKX2-1 antibody [EP1584Y]"))
    rrid, _url, _note = scicrunch.resolve_rrid("ab124695", "Abcam", "NKX2-1")
    assert rrid == "AB_10972023"


def test_a_synonym_of_our_gene_confirms_rather_than_vetoes(monkeypatch):
    """The registry names a gene by its older official symbol often enough that
    this decided 28 rows on the first live pass: OGA's is MGEA5, and we already
    store it. Without the alias list, ten PDPN antibodies from six vendors read
    as ten different antibodies."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("MGEA5"))
    rrid, _url, note = scicrunch.resolve_rrid(
        "ab124695", "Some Other Co", "OGA", aliases=["MGEA5", "MEA5", "HEXC"])
    assert rrid == "AB_10972023"
    assert "gene" in note


def test_an_unrelated_bare_symbol_still_vetoes_despite_aliases(monkeypatch):
    """Aliases widen what counts as ours; they must not widen it to anything."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("PSMC3"))
    rrid, _url, note = scicrunch.resolve_rrid(
        "ab124695", "Abcam", "OGA", aliases=["MGEA5", "MEA5"])
    assert rrid is None and "gene differs" in note


def test_a_review_note_says_what_the_registry_said(monkeypatch):
    """'differs' without the other side is a refusal you cannot act on — the
    first live pass produced 28 of these and settling one meant querying the
    registry again by hand."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("PSMC3", catalogue="x"))
    _rrid, _url, note = scicrunch.resolve_rrid("x", "Abcam", "APOE")
    assert "PSMC3" in note and "ours=APOE" in note


def test_the_protein_name_confirms_the_gene(monkeypatch):
    """The registry names a target by its protein: 'Podoplanin' for PDPN,
    'Alpha-1-antitrypsin' for SERPINA1. Ten PDPN antibodies from six vendors sat
    in REVIEW for want of this, because the display alias list drops exactly the
    name the registry uses."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("Podoplanin",
                                                     vendor="Some Other Co"))
    rrid, _url, note = scicrunch.resolve_rrid(
        "ab124695", "Abcam", "PDPN", aliases=["Podoplanin", "GP36", "Aggrus"])
    assert rrid == "AB_10972023" and "gene" in note


def test_a_multiword_protein_name_matches_only_as_a_whole(monkeypatch):
    """'Ras-related protein Rab-11A' and 'Ras-related protein Rab-32' share the
    words 'protein' and 'Ras-related'. The whole-string comparison is an
    equality precisely so those two never match each other."""
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue",
                        lambda cat, size=25: _record("Ras-related protein Rab-32",
                                                     vendor="Some Other Co"))
    rrid, _url, note = scicrunch.resolve_rrid(
        "ab124695", "Abcam", "RAB11A", aliases=["Ras-related protein Rab-11A"])
    assert rrid is None and "could not confirm" in note


def test_one_confirming_name_is_not_vetoed_by_another_on_the_same_record():
    """ab32127 lists 'Syp', 'Synaptophysin antibody [YE269]' and
    'Synaptophysin'. gene_bad is OR-ed across a record's names, so requiring
    `not gene_bad` let the third name veto what the first had confirmed."""
    records = [{"rrid": "AB_1",
                "genes": ["Syp", "Synaptophysin antibody [YE269]", "Synaptophysin"],
                "vendors": [{"vendor": "Abcam", "catalogue": "ab32127", "url": ""}]}]
    status, rrid, _url, _note = scicrunch.match_registry(
        "ab32127", "Abcam", "SYP", records, aliases=["Synaptophysin"])
    assert status == "high" and rrid == "AB_1"


def test_a_contradiction_with_nothing_confirming_still_refuses():
    """The other side of that change: gene_bad still decides where no name
    confirmed. Vendor agrees and the gene does not, which is the case that means
    either our gene or the catalogue is wrong."""
    records = [{"rrid": "AB_1", "genes": ["PSMC3"],
                "vendors": [{"vendor": "Abcam", "catalogue": "x", "url": ""}]}]
    status, rrid, _url, note = scicrunch.match_registry(
        "x", "Abcam", "APOE", records, aliases=["Apolipoprotein E"])
    assert status == "review" and not rrid and "PSMC3" in note
