"""Phase 3 — write-back for extra antibody fields + the scalar families
(targets, cell lines, samples). Proves the same guarantees hold: gap-fill
applies, overwrite needs confirmation, blank never clears, no-id is blocked,
booleans never silently flip, nothing is deleted."""
from __future__ import annotations

import io
from datetime import date

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import (Target, Company, Antibody, CellLine, Sample, Site)
    for M in (Sample, Antibody, CellLine, Company, Target):
        M.objects.using(DB).all().delete()
    site, _ = Site.objects.using(DB).get_or_create(short_code="WBK", defaults={"name": "WB Site"})
    tgt = Target.objects.using(DB).create(protein_name="Superoxide dismutase", gene_name="SOD1")
    comp = Company.objects.using(DB).create(name="Proteintech")
    ab = Antibody.objects.using(DB).create(target=tgt, company=comp, catalogue_number="12A8",
                                           antigen="", out_of_market=False)
    cl = CellLine.objects.using(DB).create(target=tgt, name="SW620 KO", genotype="KO",
                                           medium="")   # blank medium → gap-fill target
    sm = Sample.objects.using(DB).create(cell_line=cl, sample_type="lysate", site=site,
                                         preparation_date=date(2024, 1, 1), lysis_buffer="")
    return {"tgt": tgt, "ab": ab, "cl": cl, "sm": sm}


def _xlsx(sheets):
    import openpyxl
    from django.core.files.uploadedfile import SimpleUploadedFile
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title[:31])
        for r in rows:
            ws.append(r)
    buf = io.BytesIO(); wb.save(buf)
    return SimpleUploadedFile("d.xlsx", buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── antibody extra fields ────────────────────────────────────────────────────

def test_antibody_extra_gapfill_and_bool_overwrite(seeded):
    from pipeline.services import dataset
    ab = seeded["ab"]
    # antigen blank → gap-fill; out_of_market False→True is a bool change (overwrite)
    sheet = {"Antibodies": [["id", "gene", "catalogue", "antigen", "in_kind_value", "out_of_market"],
                            [ab.pk, "SOD1", "12A8", "SOD1 peptide", "150", "yes"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert plan["ok"]
    fills = {i["field"] for i in plan["antibodies"]["fills"]}
    ovs = {i["field"] for i in plan["antibodies"]["overwrites"]}
    assert {"antigen", "in_kind_value"} <= fills
    assert "out_of_market" in ovs                      # bool change → confirm

    # gap-fill only: antigen + in_kind_value land, bool stays False
    dataset.apply_upload(parsed, apply_overwrites=False)
    ab.refresh_from_db(using=DB)
    assert ab.antigen == "SOD1 peptide"
    assert str(ab.in_kind_value) .startswith("150")
    assert ab.out_of_market is False

    # confirm → bool flips
    dataset.apply_upload(dataset.parse_upload(_xlsx(sheet)), apply_overwrites=True)
    ab.refresh_from_db(using=DB)
    assert ab.out_of_market is True


def test_antibody_extra_only_row_with_id_matches(seeded):
    """A row that carries only id + an extra (no catalogue column) still matches."""
    from pipeline.services import dataset
    ab = seeded["ab"]
    sheet = {"Antibodies": [["id", "isotype"], [ab.pk, "IgG1"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    assert parsed["error"] is None
    dataset.apply_upload(parsed, apply_overwrites=False)
    ab.refresh_from_db(using=DB)
    assert ab.isotype == "IgG1"


# ── cell lines / targets / samples ───────────────────────────────────────────

def test_cell_line_gapfill_by_id(seeded):
    from pipeline.services import dataset
    cl = seeded["cl"]
    sheet = {"Cell lines": [["id", "c number", "gene", "medium", "growth_properties"],
                            [cl.pk, "", "SOD1", "DMEM + 10% FBS", "adherent"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert {i["field"] for i in plan["cell_lines"]["fills"]} >= {"medium", "growth_properties"}
    res = dataset.apply_upload(parsed, apply_overwrites=False)
    assert res["cell_lines_updated"] == 1
    cl.refresh_from_db(using=DB)
    assert cl.medium == "DMEM + 10% FBS" and cl.growth_properties == "adherent"


def test_target_overwrite_needs_confirmation(seeded):
    from pipeline.services import dataset
    tgt = seeded["tgt"]                       # protein_name already populated
    sheet = {"Targets": [["id", "gene", "protein", "protein_type"],
                         [tgt.pk, "SOD1", "CHANGED NAME", "enzyme"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert any(i["field"] == "protein" for i in plan["targets"]["overwrites"])
    assert any(i["field"] == "protein_type" for i in plan["targets"]["fills"])

    dataset.apply_upload(parsed, apply_overwrites=False)     # no confirm
    tgt.refresh_from_db(using=DB)
    assert tgt.protein_name == "Superoxide dismutase"       # unchanged
    assert tgt.protein_type == "enzyme"                     # gap-fill applied

    dataset.apply_upload(dataset.parse_upload(_xlsx(sheet)), apply_overwrites=True)
    tgt.refresh_from_db(using=DB)
    assert tgt.protein_name == "CHANGED NAME"


def test_scalar_blank_never_clears(seeded):
    from pipeline.services import dataset
    cl = seeded["cl"]
    cl.medium = "keep me"; cl.save(using=DB)
    sheet = {"Cell lines": [["id", "c number", "gene", "medium"], [cl.pk, "", "SOD1", ""]]}
    dataset.apply_upload(dataset.parse_upload(_xlsx(sheet)), apply_overwrites=True)
    cl.refresh_from_db(using=DB)
    assert cl.medium == "keep me"


def test_scalar_no_id_is_blocked_not_created(seeded):
    from pipeline.services import dataset
    from pipeline.models import CellLine
    before = CellLine.objects.using(DB).count()
    sheet = {"Cell lines": [["id", "c number", "gene", "medium"], ["", "", "SOD1", "X medium"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert plan["cell_lines"]["blocked"]                   # no id → blocked
    dataset.apply_upload(parsed, apply_overwrites=True)
    assert CellLine.objects.using(DB).count() == before    # nothing created


def test_sample_gapfill(seeded):
    from pipeline.services import dataset
    sm = seeded["sm"]
    sheet = {"Samples": [["id", "cell_line id", "gene", "lysis_buffer", "protein_concentration"],
                         [sm.pk, sm.cell_line_id, "SOD1", "RIPA", "2.5"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    res = dataset.apply_upload(parsed, apply_overwrites=False)
    assert res["samples_updated"] == 1
    sm.refresh_from_db(using=DB)
    assert sm.lysis_buffer == "RIPA" and str(sm.protein_concentration).startswith("2.5")


def test_selected_export_round_trips_extra_field(seeded):
    """Export an extra field via the selector, edit it, re-upload — the loop."""
    from pipeline.services import dataset
    sel = dataset.parse_selection("antibodies:antigen")
    payload = dataset.build_json(dataset.resolve_targets(genes="SOD1"), selection=sel)
    for t in payload["targets"]:
        for ab in t["antibodies"]:
            ab["antigen"] = "round-trip antigen"
    from django.core.files.uploadedfile import SimpleUploadedFile
    import json as _json
    up = SimpleUploadedFile("d.json", _json.dumps(payload).encode(), content_type="application/json")
    dataset.apply_upload(dataset.parse_upload(up), apply_overwrites=False)
    seeded["ab"].refresh_from_db(using=DB)
    assert seeded["ab"].antigen == "round-trip antigen"
