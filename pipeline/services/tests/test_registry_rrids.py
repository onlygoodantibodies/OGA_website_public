"""Find missing RRIDs on the antibodies board (`services/registry_rrids.py`).

Pinned: what would be silently wrong. The fill writes only what the server
itself found (never an RRID the page sends), makes no registry call, never
replaces an RRID on file, and never writes another bench's row. The registry
is mocked.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db, monkeypatch):
    from django.core.cache import cache
    from pipeline.models import Antibody, Company, Site, Target
    from pipeline.services import scicrunch
    # Sites are left alone: other modules' members and cell lines point at
    # theirs (PROTECT), so this fixture brings two of its own.
    for M in (Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    cache.clear()
    monkeypatch.setattr(scicrunch, "API_KEY", "test-key")
    lei, _ = Site.objects.using(DB).get_or_create(
        name="RR Leicester", defaults={"short_code": "RRL"})
    mcg, _ = Site.objects.using(DB).get_or_create(
        name="RR McGill", defaults={"short_code": "RRM"})
    t = Target.objects.using(DB).create(gene_name="SNCA", protein_name="Syn")
    c = Company.objects.using(DB).create(name="Abcam")
    mk = lambda cat, site, **kw: Antibody.objects.using(DB).create(
        target=t, company=c, catalogue_number=cat, site=site, **kw).pk
    ids = {
        "a": mk("ab-a", lei),
        "b": mk("ab-b", lei),
        "has": mk("ab-has", lei, rrid="AB_111"),
        "link": mk("ab-link", lei, rrid_link="https://antibodyregistry.org/AB_555"),
        "mcgill": mk("ab-mcg", mcg),
    }
    yield SimpleNamespace(ids=ids, lei=lei, mcg=mcg)
    cache.clear()


def _registry(monkeypatch, by_cat):
    from pipeline.services import scicrunch
    asked = []

    def fake(catalogue, company, gene, aliases=(), lookup=None):
        asked.append(catalogue)
        ans = {"status": "nomatch", "rrid": "", "url": "", "note": "",
               "reg": {"records": [], "error": None}, "catalogue": catalogue}
        ans.update(by_cat.get(catalogue, {}))
        return ans
    monkeypatch.setattr(scicrunch, "lookup_and_match", fake)
    return asked


def _all(s):
    return list(s.ids.values())


def test_check_reports_each_row_and_writes_nothing(seeded, monkeypatch):
    from pipeline.models import Antibody
    from pipeline.services import registry_rrids
    _registry(monkeypatch, {"ab-a": {"status": "high", "rrid": "AB_1"},
                            "ab-b": {"status": "review"}})
    d = registry_rrids.check(_all(seeded), is_superuser=True)
    by = {i["id"]: i for i in d["items"]}
    assert d["total"] == 4                      # the one with an RRID is not a candidate
    assert by[seeded.ids["a"]]["status"] == "found"
    assert by[seeded.ids["b"]]["status"] == "review"
    assert by[seeded.ids["link"]] == dict(by[seeded.ids["link"]], status="found",
                                          rrid="AB_555", source="link")
    assert by[seeded.ids["mcgill"]]["status"] == "none"
    assert not Antibody.objects.using(DB).filter(rrid="AB_1").exists()


def test_fill_writes_what_the_check_found_without_asking_again(seeded, monkeypatch):
    from pipeline.models import Antibody
    from pipeline.services import registry_rrids, scicrunch
    _registry(monkeypatch, {"ab-a": {"status": "high", "rrid": "AB_1",
                                     "url": "https://example.com/a"}})
    registry_rrids.check(_all(seeded), is_superuser=True)

    def boom(*a, **k):
        raise AssertionError("the fill asked the registry")
    monkeypatch.setattr(scicrunch, "lookup_and_match", boom)
    ids = [seeded.ids["a"], seeded.ids["link"]]
    out = registry_rrids.fill(ids, consented_count=2, is_superuser=True)
    assert {f["rrid"] for f in out["filled"]} == {"AB_1", "AB_555"}
    a = Antibody.objects.using(DB).get(pk=seeded.ids["a"])
    assert a.rrid == "AB_1" and a.supplier_url == "https://example.com/a"
    assert a.rrid_link == "https://www.antibodyregistry.org/AB_1"


def test_fill_never_writes_a_row_the_check_did_not_find(seeded, monkeypatch):
    """The page sends ids, never RRIDs; a row with no cached find is skipped
    and named, not filled with anything the request carried."""
    from pipeline.models import Antibody
    from pipeline.services import registry_rrids
    _registry(monkeypatch, {"ab-b": {"status": "review"}})
    registry_rrids.check(_all(seeded), is_superuser=True)
    out = registry_rrids.fill([seeded.ids["b"]], consented_count=1, is_superuser=True)
    assert out["filled"] == [] and len(out["skipped"]) == 1
    assert Antibody.objects.using(DB).get(pk=seeded.ids["b"]).rrid == ""


def test_fill_never_replaces_an_rrid_on_file(seeded, monkeypatch):
    from pipeline.models import Antibody
    from pipeline.services import registry_rrids
    _registry(monkeypatch, {"ab-a": {"status": "high", "rrid": "AB_1"}})
    registry_rrids.check(_all(seeded), is_superuser=True)
    Antibody.objects.using(DB).filter(pk=seeded.ids["a"]).update(rrid="AB_TYPED")
    out = registry_rrids.fill([seeded.ids["a"]], consented_count=1, is_superuser=True)
    assert out["filled"] == []
    assert Antibody.objects.using(DB).get(pk=seeded.ids["a"]).rrid == "AB_TYPED"


def test_a_member_is_offered_and_fills_only_their_own_bench(seeded, monkeypatch):
    from pipeline.models import Antibody
    from pipeline.services import registry_rrids
    _registry(monkeypatch, {"ab-mcg": {"status": "high", "rrid": "AB_9"}})
    vera = SimpleNamespace(site_id=seeded.lei.pk)
    d = registry_rrids.check(_all(seeded), member=vera)
    assert d["other_sites"] == 1
    assert seeded.ids["mcgill"] not in {i["id"] for i in d["items"]}
    # Even asked directly, the other bench's row is refused by name.
    registry_rrids.check(_all(seeded), is_superuser=True)   # caches the find
    out = registry_rrids.fill([seeded.ids["mcgill"]], consented_count=1, member=vera)
    assert out["filled"] == [] and "McGill" in out["skipped"][0]["why"]
    assert Antibody.objects.using(DB).get(pk=seeded.ids["mcgill"]).rrid == ""


def test_a_changed_count_refuses_the_press(seeded):
    from pipeline.services import registry_rrids
    with pytest.raises(registry_rrids.Refused):
        registry_rrids.fill([seeded.ids["a"]], consented_count=2, is_superuser=True)


def test_batches_walk_the_set_in_order(seeded, monkeypatch):
    from pipeline.services import registry_rrids
    _registry(monkeypatch, {})
    monkeypatch.setattr(registry_rrids, "BATCH", 3)
    first = registry_rrids.check(_all(seeded), is_superuser=True)
    second = registry_rrids.check(_all(seeded), offset=first["next_offset"],
                                  is_superuser=True)
    assert len(first["items"]) == 3 and len(second["items"]) == 1
    assert second["next_offset"] == first["total"] == 4


def test_the_registration_sheet_lists_only_what_the_check_found_missing(seeded, monkeypatch):
    """Not registered and checked → on the sheet; found → left out (Fill it);
    never asked → left out and named, since "not checked" is not a finding.
    A row with no product link says so rather than being dropped."""
    from pipeline.models import Antibody
    from pipeline.services import registry_rrids
    Antibody.objects.using(DB).filter(pk=seeded.ids["b"]).update(
        supplier_url="https://example.com/b", clonality="monoclonal", is_recombinant=True)
    _registry(monkeypatch, {"ab-a": {"status": "high", "rrid": "AB_1"}})
    monkeypatch.setattr(registry_rrids, "BATCH", 3)
    registry_rrids.check(_all(seeded), is_superuser=True)     # a, b, link — not mcgill
    sheet = registry_rrids.registration_sheet(_all(seeded), is_superuser=True)
    on = {r["catalogNum"]: r for r in sheet["rows"]}
    assert set(on) == {"ab-b"}
    b = on["ab-b"]
    assert b["clonality"] == "recombinant monoclonal"
    assert b["url"] == "https://example.com/b" and b["missing before submitting"] == ""
    assert b["cloneId"] == "" and b["epitope"] == ""            # blank, never invented
    why = {r["antibody"].split(" · ")[1]: r["why"] for r in sheet["left_out"]}
    assert "Fill" in why["ab-a"] and "Fill" in why["ab-link"]
    assert "not checked" in why["ab-mcg"]
    # Once checked, the unregistered McGill row joins it — saying it needs a link.
    registry_rrids.check(_all(seeded), offset=3, is_superuser=True)
    sheet = registry_rrids.registration_sheet(_all(seeded), is_superuser=True)
    mcg = {r["catalogNum"]: r for r in sheet["rows"]}["ab-mcg"]
    assert "product page link" in mcg["missing before submitting"]
    assert registry_rrids.registration_workbook(sheet)[:2] == b"PK"   # an .xlsx
