"""Whole-dataset round-trip for the logistics / workflow families added to the
download: assignments (TargetAssignment), reagent_requests (ReagentRequest) and
culture_events (CellCultureEvent).

Proves they behave exactly like the other scalar families — export nested under
the target, edit by id, gap-fill applies, overwrite needs confirmation, a blank
cell never clears, no-id is blocked — AND that the people columns are export-only
reference that never writes back (no individual's data round-trips)."""
from __future__ import annotations

import io
import json as _json
from datetime import date

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import (Target, Company, CellLine, Site, Project,
                                 TargetAssignment, ReagentRequest, CellCultureEvent)
    for M in (CellCultureEvent, ReagentRequest, TargetAssignment, CellLine, Company,
              Project, Target):
        M.objects.using(DB).all().delete()
    site, _ = Site.objects.using(DB).get_or_create(short_code="LOG", defaults={"name": "Log Site"})
    tgt = Target.objects.using(DB).create(protein_name="Superoxide dismutase", gene_name="SOD1")
    comp = Company.objects.using(DB).create(name="Proteintech")
    cl = CellLine.objects.using(DB).create(target=tgt, name="SW620 KO", genotype="KO")
    asn = TargetAssignment.objects.using(DB).create(
        target=tgt, site=site, task_type="WB", status="planned", notes="")
    rr = ReagentRequest.objects.using(DB).create(
        target=tgt, site=site, item_type="antibody", company=comp,
        status="draft", catalogue_numbers="")
    ce = CellCultureEvent.objects.using(DB).create(
        cell_line=cl, event_type="thaw", date=date(2024, 1, 1), notes="")
    return {"site": site, "tgt": tgt, "comp": comp, "cl": cl,
            "asn": asn, "rr": rr, "ce": ce}


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


def _json_upload(payload):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("d.json", _json.dumps(payload).encode(),
                              content_type="application/json")


# ── export ───────────────────────────────────────────────────────────────────

def test_export_nests_logistics_under_target_without_people(seeded):
    from pipeline.services import dataset
    payload = dataset.build_json(dataset.resolve_targets(genes="SOD1"),
                                 selection=dataset.parse_selection("all"))
    node = next(t for t in payload["targets"] if t["gene"] == "SOD1")
    # families present and nested under the gene
    assert {"assignments", "reagent_requests", "culture_events"} <= set(node)
    assert node["assignments"][0]["id"] == seeded["asn"].pk
    assert node["reagent_requests"][0]["id"] == seeded["rr"].pk
    assert node["culture_events"][0]["id"] == seeded["ce"].pk          # grandchild via cell line
    # reference context is exported...
    assert node["assignments"][0]["site"] == "Log Site"
    assert node["reagent_requests"][0]["company"] == "Proteintech"
    # ...but no person columns exist anywhere in the exported rows
    for fam in ("assignments", "reagent_requests", "culture_events"):
        cols = set(payload["_columns"][fam])
        assert not (cols & {"assigned_to", "assigned_by", "requested_by",
                            "performed_by", "created_by"})


# ── gap-fill / overwrite ─────────────────────────────────────────────────────

def test_assignment_gapfill_and_overwrite_by_id(seeded):
    from pipeline.services import dataset
    asn = seeded["asn"]                          # status='planned', priority=0, notes=''
    sheet = {"Assignments": [["id", "gene", "status", "priority", "notes"],
                             [asn.pk, "SOD1", "complete", "5", "kickoff done"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert plan["ok"]
    assert any(i["field"] == "notes" for i in plan["assignments"]["fills"])   # '' → value
    # status ('planned') and priority (0) are already populated → overwrite/confirm
    assert {"status", "priority"} <= {i["field"] for i in plan["assignments"]["overwrites"]}

    # gap-fill only: notes lands; the populated fields are held back
    res = dataset.apply_upload(parsed, apply_overwrites=False)
    assert res["assignments_updated"] == 1
    asn.refresh_from_db(using=DB)
    assert asn.notes == "kickoff done"
    assert asn.status == "planned" and asn.priority == 0

    # confirm → the overwrites land
    dataset.apply_upload(dataset.parse_upload(_xlsx(sheet)), apply_overwrites=True)
    asn.refresh_from_db(using=DB)
    assert asn.status == "complete" and asn.priority == 5


def test_reagent_request_gapfill(seeded):
    from pipeline.services import dataset
    rr = seeded["rr"]                            # catalogue_numbers='', quantity=1 (default)
    sheet = {"Reagent requests": [["id", "gene", "catalogue_numbers"],
                                  [rr.pk, "SOD1", "ab138501\nab32057"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert any(i["field"] == "catalogue_numbers" for i in plan["reagent_requests"]["fills"])
    res = dataset.apply_upload(parsed, apply_overwrites=False)
    assert res["reagent_requests_updated"] == 1
    rr.refresh_from_db(using=DB)
    assert "ab138501" in rr.catalogue_numbers
    assert rr.quantity == 1                      # absent from the file → untouched


def test_culture_event_gapfill_json_roundtrip(seeded):
    """Download → edit the nested culture event → re-upload the JSON tree."""
    from pipeline.services import dataset
    ce = seeded["ce"]
    payload = dataset.build_json(dataset.resolve_targets(genes="SOD1"),
                                 selection=dataset.parse_selection("all"))
    for t in payload["targets"]:
        for e in t.get("culture_events", []):
            e["notes"] = "thawed vial A2"
            e["passage_number"] = "12"
    dataset.apply_upload(dataset.parse_upload(_json_upload(payload)), apply_overwrites=False)
    ce.refresh_from_db(using=DB)
    assert ce.notes == "thawed vial A2" and ce.passage_number == 12


# ── guarantees ───────────────────────────────────────────────────────────────

def test_people_columns_never_write_back(seeded):
    """A file carrying person columns leaves those FKs untouched — they're not
    in the editable map, so they're silently ignored."""
    from pipeline.services import dataset
    asn, rr, ce = seeded["asn"], seeded["rr"], seeded["ce"]
    up = _xlsx({
        "Assignments": [["id", "assigned_to", "assigned_by", "notes"],
                        [asn.pk, "999", "888", "note A"]],
        "Reagent requests": [["id", "requested_by", "status"],
                             [rr.pk, "999", "sent"]],
        "Cell culture events": [["id", "performed_by", "notes"],
                                [ce.pk, "999", "note C"]],
    })
    dataset.apply_upload(dataset.parse_upload(up), apply_overwrites=True)
    asn.refresh_from_db(using=DB); rr.refresh_from_db(using=DB); ce.refresh_from_db(using=DB)
    # editable fields applied...
    assert asn.notes == "note A" and rr.status == "sent" and ce.notes == "note C"
    # ...but no person link was ever set
    assert asn.assigned_to_id is None and asn.assigned_by_id is None
    assert rr.requested_by_id is None and ce.performed_by_id is None


def test_logistics_blank_never_clears(seeded):
    from pipeline.services import dataset
    asn = seeded["asn"]
    asn.notes = "keep me"; asn.save(using=DB)
    sheet = {"Assignments": [["id", "gene", "notes"], [asn.pk, "SOD1", ""]]}
    dataset.apply_upload(dataset.parse_upload(_xlsx(sheet)), apply_overwrites=True)
    asn.refresh_from_db(using=DB)
    assert asn.notes == "keep me"


def test_logistics_no_id_is_blocked_not_created(seeded):
    from pipeline.services import dataset
    from pipeline.models import ReagentRequest
    before = ReagentRequest.objects.using(DB).count()
    sheet = {"Reagent requests": [["id", "gene", "status"], ["", "SOD1", "sent"]]}
    parsed = dataset.parse_upload(_xlsx(sheet))
    plan = dataset.plan_upload(parsed)
    assert plan["reagent_requests"]["blocked"]
    dataset.apply_upload(parsed, apply_overwrites=True)
    assert ReagentRequest.objects.using(DB).count() == before
