"""RRIDs from the Antibody Registry when an antibody is entered.

The check asks (`bulk_antibodies.look_up_rrids`); the save takes what the check
found as a value and makes no call itself. Pinned here: the things that would
be silently wrong — an RRID written over one somebody typed or one on file, a
registry call made from inside the save, a malformed value from the page being
stored, and an outage read as "not in the registry". The registry is mocked.
"""
from __future__ import annotations

import time

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db, monkeypatch):
    from django.core.cache import cache
    from pipeline.models import Target, Company, Antibody
    from pipeline.services import scicrunch
    for M in (Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    Target.objects.using(DB).create(protein_name="Apolipoprotein E", gene_name="APOE")
    Company.objects.using(DB).create(name="Abcam")
    cache.clear()
    # Unset in tests, which is the "no key" branch; most tests want a key.
    monkeypatch.setattr(scicrunch, "API_KEY", "test-key")
    yield
    cache.clear()


def _row(rrid="", catalogue="ab1907"):
    return {"gene": "APOE", "catalogue": catalogue, "company": "Abcam", "rrid": rrid}


def _registry(monkeypatch, answer):
    """Mock the matcher; returns a list of the catalogues it was asked about."""
    from pipeline.services import scicrunch
    asked = []

    def fake(catalogue, company, gene, aliases=(), lookup=None):
        asked.append(catalogue)
        return dict({"status": "nomatch", "rrid": "", "url": "", "note": "",
                     "reg": {"records": [], "error": None}, "catalogue": catalogue},
                    **answer)
    monkeypatch.setattr(scicrunch, "lookup_and_match", fake)
    return asked


HIGH = {"status": "high", "rrid": "AB_302669", "url": "https://abcam.com/ab1907"}


def _check(rows):
    from pipeline.services import bulk_antibodies
    items = bulk_antibodies.plan(rows)
    finds = bulk_antibodies.look_up_rrids(items)
    return items, finds, bulk_antibodies.summarize(items)


def test_check_finds_and_save_fills(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies
    from pipeline.models import Antibody
    _registry(monkeypatch, HIGH)
    items, finds, summary = _check([_row()])
    assert summary["registry_found"] == 1
    assert "AB_302669" in items[0]["note"]

    out = bulk_antibodies.apply([_row()], registry_rrids=finds)
    ab = Antibody.objects.using(DB).get(catalogue_number="ab1907")
    assert ab.rrid == "AB_302669"
    assert ab.rrid_link == "https://www.antibodyregistry.org/AB_302669"
    assert ab.supplier_url == "https://abcam.com/ab1907"
    assert out["rrids_from_registry"] == [{"catalogue": "ab1907", "rrid": "AB_302669"}]


def test_save_makes_no_registry_call(seeded, monkeypatch):
    """The save runs inside one transaction; a 4-5 s call from there holds
    PostgreSQL open (tests_timeouts rule 3). It takes the check's find as a
    value and asks nothing."""
    from pipeline.services import bulk_antibodies, scicrunch

    def boom(*a, **k):
        raise AssertionError("the save called the registry")
    monkeypatch.setattr(scicrunch, "lookup_and_match", boom)
    monkeypatch.setattr(scicrunch, "lookup_by_catalogue", boom)
    finds = {bulk_antibodies.registry_key("ab1907", "Abcam", "APOE"):
             {"rrid": "AB_302669", "url": ""}}
    out = bulk_antibodies.apply([_row()], registry_rrids=finds)
    assert out["rrids_filled"] == 1


def test_typed_rrid_is_not_looked_up_or_replaced(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies
    from pipeline.models import Antibody
    asked = _registry(monkeypatch, HIGH)
    items, finds, _ = _check([_row(rrid="AB_111")])
    assert asked == [] and finds == {}
    # Even a find for this key from somewhere else does not displace it.
    stale = {bulk_antibodies.registry_key("ab1907", "Abcam", "APOE"):
             {"rrid": "AB_999", "url": ""}}
    bulk_antibodies.apply([_row(rrid="AB_111")], registry_rrids=stale)
    assert Antibody.objects.using(DB).get(catalogue_number="ab1907").rrid == "AB_111"


def test_rrid_on_file_is_not_looked_up_or_replaced(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies
    from pipeline.models import Antibody
    bulk_antibodies.apply([_row(rrid="AB_111")])
    asked = _registry(monkeypatch, HIGH)
    items, finds, _ = _check([_row()])
    assert items[0]["status"] == "update"
    assert asked == [] and finds == {}
    stale = {bulk_antibodies.registry_key("ab1907", "Abcam", "APOE"):
             {"rrid": "AB_999", "url": ""}}
    bulk_antibodies.apply([_row()], registry_rrids=stale)
    assert Antibody.objects.using(DB).get(catalogue_number="ab1907").rrid == "AB_111"


def test_review_is_named_and_never_filled(seeded, monkeypatch):
    _registry(monkeypatch, {"status": "review", "note": "gene differs"})
    items, finds, summary = _check([_row()])
    assert finds == {}
    assert summary["registry_review"] == 1
    assert "does not clearly match" in items[0]["note"]


def test_outage_is_unchecked_not_absent_and_not_cached(seeded, monkeypatch):
    """"Could not be reached" is not "not in the registry" — and an outage
    must not be remembered as an answer for a day."""
    asked = _registry(monkeypatch, {
        "status": "nomatch",
        "reg": {"records": [], "error": "SciCrunch API unavailable: timeout"}})
    _, _, summary = _check([_row()])
    assert summary["registry_unchecked"] == 1 and summary["registry_none"] == 0
    _check([_row()])
    assert len(asked) == 2


def test_absent_is_said_and_cached(seeded, monkeypatch):
    asked = _registry(monkeypatch, {"status": "nomatch"})
    items, _, summary = _check([_row()])
    assert summary["registry_none"] == 1
    assert "Not in the Antibody Registry" in items[0]["note"]
    _check([_row()])
    assert len(asked) == 1


def test_no_key_asks_nothing_and_says_unchecked(seeded, monkeypatch):
    from pipeline.services import scicrunch
    monkeypatch.setattr(scicrunch, "API_KEY", "")
    asked = _registry(monkeypatch, HIGH)
    _, finds, summary = _check([_row()])
    assert asked == [] and finds == {}
    assert summary["registry_unchecked"] == 1


def test_deadline_bounds_the_wall_clock(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies, scicrunch

    def slow(*a, **k):
        time.sleep(1.0)
        return {"status": "high", "rrid": "AB_1", "url": "", "reg": None}
    monkeypatch.setattr(scicrunch, "lookup_and_match", slow)
    items = bulk_antibodies.plan([_row()])
    started = time.monotonic()
    finds = bulk_antibodies.look_up_rrids(items, budget_seconds=0.05)
    assert time.monotonic() - started < 0.5
    assert finds == {}
    assert items[0]["registry"]["status"] == "unchecked"


def test_malformed_value_from_the_page_is_ignored(seeded):
    from pipeline.services import bulk_antibodies
    from pipeline.models import Antibody
    bad = {bulk_antibodies.registry_key("ab1907", "Abcam", "APOE"):
           {"rrid": "<script>", "url": "javascript:alert(1)"}}
    out = bulk_antibodies.apply([_row()], registry_rrids=bad)
    assert out["rrids_filled"] == 0
    ab = Antibody.objects.using(DB).get(catalogue_number="ab1907")
    assert ab.rrid == "" and ab.supplier_url == ""


def test_find_for_a_different_row_does_not_land(seeded):
    """Keyed on catalogue, supplier and gene: a row edited after the check
    finds nothing rather than another antibody's RRID."""
    from pipeline.services import bulk_antibodies
    from pipeline.models import Antibody
    other = {bulk_antibodies.registry_key("ab9999", "Abcam", "APOE"):
             {"rrid": "AB_302669", "url": ""}}
    bulk_antibodies.apply([_row()], registry_rrids=other)
    assert Antibody.objects.using(DB).get(catalogue_number="ab1907").rrid == ""
