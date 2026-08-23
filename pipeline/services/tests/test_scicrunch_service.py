"""Tests for the SciCrunch service: the gene antibody-count (feasibility) and the
shared catalogue->RRID resolver (antibody entry). HTTP is mocked — no network.

Schema mirrors the live registry _source confirmed 2026-07: target gene at
antibodies.primary[].targets[].name, clonality at
antibodies.primary[].clonality.name, catalogue at vendors[].catalogNumber.
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
