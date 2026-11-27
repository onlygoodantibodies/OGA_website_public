"""What the antibody paste says about the vial a lot-less row will update.

A row that names no lot matched a vial carrying lot 1057445-2 while its note
said the vial "has no lot recorded" (27 Nov 2026). And where a bench holds
several vials of one product, `find_vial` updates the oldest — so the note has
to say which one, and how to choose another.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture()
def product(_pipeline_db):
    from pipeline.models import Antibody, Company, Site, Target
    for M in (Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    site, _ = Site.objects.using(DB).get_or_create(
        name="Lot Note Bench", defaults={"short_code": "LNB"})
    t = Target.objects.using(DB).create(gene_name="MAPT", protein_name="Tau")
    c = Company.objects.using(DB).create(name="Abcam")
    mk = lambda lot: Antibody.objects.using(DB).create(
        target=t, company=c, catalogue_number="ab254150", lot_number=lot, site=site)
    return mk


def _note(lot=""):
    from pipeline.services import bulk_antibodies
    row = {"gene": "MAPT", "catalogue": "ab254150", "company": "Abcam",
           "lot": lot, "site": "Lot Note Bench"}
    items = bulk_antibodies.plan([row])
    assert items[0]["status"] == "update"
    return items[0]["note"]


def test_one_vial_with_a_lot_is_not_called_lotless(product):
    product("1057445-2")
    note = _note()
    assert "no lot recorded" not in note
    assert "lot 1057445-2" in note and "names no lot" in note


def test_several_vials_name_them_all_and_say_which_is_updated(product):
    product("1057445-1")
    product("1057445-2")
    note = _note()
    assert "2 vials" in note
    assert "1057445-1" in note and "1057445-2" in note
    assert "it will update the one with lot 1057445-1" in note   # the oldest
    assert "type the lot" in note


def test_a_lotless_vial_still_says_so(product):
    product("")
    assert "which has no lot recorded" in _note()
