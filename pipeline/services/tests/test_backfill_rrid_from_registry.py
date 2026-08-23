"""Tests for ``backfill_rrid_from_registry`` and the corrected SciCrunch
catalogue lookup. The registry HTTP call is mocked (no network).

Schema mirrors the live Antibody Registry ``_source`` (confirmed 2026-07):
RRID at ``item.identifier``, target gene at
``antibodies.primary[].targets[].name``, catalogue at ``vendors[].catalogNumber``.

Covers: the defensive ``_source`` parser; the catalogue + gene/vendor matcher
(gene cross-check catches a catalogue that resolves to a different antibody);
and the command end-to-end.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command

from pipeline.services import scicrunch   # module has no Django-model import → safe at top

DB = "pipeline_db"


# ── the defensive _source parser (real registry shape) ───────────────────────

def test_parse_hit_live_shape():
    src = {
        "item": {"identifier": "AB_3698202", "curie": "ab:3698202",
                 "name": "14-3-3 protein gamma",
                 "alternateIdentifiers": [{"identifier": "AB_3698202", "curie": "ab:3698202"}]},
        "antibodies": {"primary": [{"clonality": {"name": "unknown"},
                                    "targets": [{"name": "YWHAG"}]}]},
        "vendors": [{"vendor": "Abcam", "catalogNumber": "ab133538",
                     "url": "https://abcam.com/ab133538"}],
        "rrid": {"curie": "RRID:AB_3698202"},
    }
    rec = scicrunch._parse_hit(src)
    assert rec["rrid"] == "AB_3698202"          # from item.identifier, NOT item.curie ("ab:..")
    assert rec["genes"] == ["YWHAG"]
    assert rec["vendors"] == [{"vendor": "Abcam", "catalogue": "ab133538",
                               "url": "https://abcam.com/ab133538"}]


def test_parse_hit_rejects_placeholder_url():
    # the registry 'link' field is often the literal "no" (not a URL) → drop it
    src = {"item": {"identifier": "AB_1"},
           "antibodies": {"primary": [{"targets": [{"name": "APOE"}]}]},
           "vendors": [{"name": "Abcam", "catalogNumber": "c1", "link": "no"}]}
    rec = scicrunch._parse_hit(src)
    assert rec["vendors"][0]["url"] == ""


def test_parse_hit_curie_fallback_and_snakecase_vendor():
    # no item.identifier → fall back to rrid.curie; item.curie ("ab:..") must be ignored
    src = {"item": {"curie": "ab:11033178"},
           "rrid": {"curie": "RRID:AB_11033178"},
           "antibodies": {"primary": [{"targets": [{"name": "SOD1"}]}]},
           "vendors": [{"vendorName": "Novus", "catalog_number": "NBP1-53199"}]}
    rec = scicrunch._parse_hit(src)
    assert rec["rrid"] == "AB_11033178"
    assert rec["genes"] == ["SOD1"]
    assert rec["vendors"][0]["vendor"] == "Novus"
    assert rec["vendors"][0]["catalogue"] == "NBP1-53199"


# ── the matcher (catalogue + gene/vendor cross-check) ────────────────────────

@pytest.fixture()
def matcher():
    from pipeline.management.commands.backfill_rrid_from_registry import match_registry
    return match_registry


def _rec(rrid, vendor, cat, genes, url=""):
    return {"rrid": rrid, "genes": genes,
            "vendors": [{"vendor": vendor, "catalogue": cat, "url": url}]}


def test_catalogue_vendor_and_gene_is_high(matcher):
    recs = [_rec("AB_302669", "Abcam", "ab1907", ["APOE"], "https://abcam.com/ab1907")]
    status, rrid, url, note = matcher("ab1907", "Abcam", "APOE", recs)
    assert status == "high" and rrid == "AB_302669" and url == "https://abcam.com/ab1907"
    assert "gene" in note and "vendor" in note


def test_gene_confirms_even_when_vendor_differs(matcher):
    recs = [_rec("AB_1", "Some Distributor", "100", ["APOE"])]
    status, rrid, _, note = matcher("100", "Abcam", "APOE", recs)
    assert status == "high" and rrid == "AB_1" and note == "confirmed by gene"


def test_gene_mismatch_is_review(matcher):
    # exact catalogue but the registry record targets a different gene → do NOT trust
    recs = [_rec("AB_1", "Abcam", "100", ["PSMC3"])]
    status, rrid, _, note = matcher("100", "Abcam", "APOE", recs)
    assert status == "review" and rrid == "" and "gene differs" in note


def test_separator_insensitive_is_high(matcher):
    recs = [_rec("AB_11033178", "Novus Biologicals", "NBP1 53199", ["APOE"])]
    status, rrid, _, _ = matcher("NBP1-53199", "Novus Biologicals", "APOE", recs)
    assert status == "high" and rrid == "AB_11033178"


def test_conflicting_rrids_is_review(matcher):
    recs = [_rec("AB_1", "Abcam", "X9", ["APOE"]), _rec("AB_2", "Abcam", "X9", ["APOE"])]
    status, _, _, note = matcher("X9", "Abcam", "APOE", recs)
    assert status == "review" and "multiple confirmed" in note


def test_cannot_confirm_is_review(matcher):
    # vendor differs and the record carries no gene → nothing confirms it
    recs = [_rec("AB_1", "Some Distributor", "100", [])]
    status, _, _, note = matcher("100", "Abcam", "APOE", recs)
    assert status == "review" and "could not confirm" in note


def test_no_catalogue_match_is_nomatch(matcher):
    recs = [_rec("AB_1", "Abcam", "Z1", ["APOE"])]
    status, _, _, _ = matcher("Q7", "Abcam", "APOE", recs)
    assert status == "nomatch"


# ── the command end-to-end (registry mocked) ────────────────────────────────

@pytest.fixture()
def fake_registry(monkeypatch):
    data = {
        "ab1907": [_rec("AB_302669", "Abcam", "ab1907", ["APOE"], "https://abcam.com/ab1907")],
        "13366": [_rec("AB_2798191", "Cell Signaling Technology", "13366", ["APOE"])],  # no url
        "junkurl": [_rec("AB_777", "Abcam", "junkurl", ["APOE"], "no")],  # placeholder url
        "WRONGGENE": [_rec("AB_999999", "Abcam", "WRONGGENE", ["PSMC3"])],  # gene mismatch
    }

    def _fake(catalogue, size=25):
        recs = data.get((catalogue or "").strip(), [])
        return {"found": bool(recs), "records": recs,
                "error": None if recs else f"No registry record for catalogue '{catalogue}'"}

    monkeypatch.setattr(scicrunch, "lookup_by_catalogue", _fake)


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import Target, Company, Antibody
    for M in (Antibody, Company, Target):
        M.objects.using(DB).all().delete()
    apoe = Target.objects.using(DB).create(protein_name="Apolipoprotein E",
                                            gene_name="APOE", status="published")
    abcam = Company.objects.using(DB).create(name="Abcam")
    cst = Company.objects.using(DB).create(name="Cell Signaling Technology")
    def mk(**kw):
        kw.setdefault("rrid", "")
        kw.setdefault("rrid_link", "")
        return Antibody.objects.using(DB).create(target=apoe, **kw)
    return {
        "match": mk(company=abcam, catalogue_number="ab1907"),
        "urlmatch": mk(company=cst, catalogue_number="13366", supplier_url=""),
        "junkurl": mk(company=abcam, catalogue_number="junkurl", supplier_url=""),
        "genebad": mk(company=abcam, catalogue_number="WRONGGENE"),   # registry gene != APOE
        "nomatch": mk(company=abcam, catalogue_number="ZZZ999"),
        "hasrrid": Antibody.objects.using(DB).create(target=apoe, company=abcam,
                                                     catalogue_number="ab1907", rrid="AB_999"),
        "haslink": mk(company=abcam, catalogue_number="ab99",
                      rrid_link="https://www.antibodyregistry.org/AB_555"),
    }


def test_apply_fills_high_only(seeded, fake_registry):
    call_command("backfill_rrid_from_registry", "--apply")
    for ab in seeded.values():
        ab.refresh_from_db(using=DB)

    assert seeded["match"].rrid == "AB_302669"
    assert seeded["match"].rrid_link == "https://www.antibodyregistry.org/AB_302669"
    assert seeded["match"].supplier_url == "https://abcam.com/ab1907"

    assert seeded["urlmatch"].rrid == "AB_2798191"
    assert seeded["urlmatch"].supplier_url == ""     # registry had no url → stays blank

    assert seeded["junkurl"].rrid == "AB_777"        # rrid filled...
    assert seeded["junkurl"].supplier_url == ""      # ...but placeholder "no" NOT written as url

    assert seeded["genebad"].rrid == ""              # gene mismatch → REVIEW, not written
    assert seeded["nomatch"].rrid == ""
    assert seeded["hasrrid"].rrid == "AB_999"        # already had rrid → untouched
    assert seeded["haslink"].rrid == ""              # link route handles it → skipped here


def test_dry_run_writes_nothing(seeded, fake_registry):
    call_command("backfill_rrid_from_registry")   # no --apply
    seeded["match"].refresh_from_db(using=DB)
    assert seeded["match"].rrid == ""
