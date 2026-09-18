"""The opt-in RRID auto-fill on antibody entry (bulk_antibodies.apply
lookup_rrids=True). The registry resolver is mocked — no network."""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import Target, Company, Antibody
    for M in (Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    Target.objects.using(DB).create(protein_name="Apolipoprotein E", gene_name="APOE")
    Company.objects.using(DB).create(name="Abcam")


def _row(rrid=""):
    return {"gene": "APOE", "catalogue": "ab1907", "company": "Abcam", "rrid": rrid}


def test_lookup_fills_blank_rrid(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies, scicrunch
    from pipeline.models import Antibody
    # Signature must match the real one, aliases included: the caller swallows
    # exceptions so a stale stub would silently report "no RRID found" rather
    # than failing -- which is how this test earned its keep.
    monkeypatch.setattr(scicrunch, "resolve_rrid",
                        lambda cat, comp, gene, aliases=(): (
                            "AB_302669", "https://abcam.com/ab1907",
                            "confirmed by gene+vendor"))

    out = bulk_antibodies.apply([_row()], lookup_rrids=True)
    assert out["rrids_filled"] == 1
    ab = Antibody.objects.using(DB).get(catalogue_number="ab1907")
    assert ab.rrid == "AB_302669"
    assert ab.rrid_link == "https://www.antibodyregistry.org/AB_302669"
    assert ab.supplier_url == "https://abcam.com/ab1907"


def test_lookup_off_by_default(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies, scicrunch
    from pipeline.models import Antibody
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return ("AB_1", "", "")
    monkeypatch.setattr(scicrunch, "resolve_rrid", _spy)

    bulk_antibodies.apply([_row()])   # lookup_rrids defaults False
    assert called["n"] == 0
    assert Antibody.objects.using(DB).get(catalogue_number="ab1907").rrid == ""


def test_user_supplied_rrid_not_overwritten_by_lookup(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies, scicrunch
    from pipeline.models import Antibody
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return ("AB_999", "", "")
    monkeypatch.setattr(scicrunch, "resolve_rrid", _spy)

    bulk_antibodies.apply([_row(rrid="AB_302669")], lookup_rrids=True)
    assert called["n"] == 0   # rrid already present → no lookup
    assert Antibody.objects.using(DB).get(catalogue_number="ab1907").rrid == "AB_302669"


def test_lookup_miss_leaves_blank(seeded, monkeypatch):
    from pipeline.services import bulk_antibodies, scicrunch
    from pipeline.models import Antibody
    monkeypatch.setattr(scicrunch, "resolve_rrid", lambda cat, comp, gene: (None, "", "no registry record"))

    out = bulk_antibodies.apply([_row()], lookup_rrids=True)
    assert out["rrids_filled"] == 0
    assert Antibody.objects.using(DB).get(catalogue_number="ab1907").rrid == ""
