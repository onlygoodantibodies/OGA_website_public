"""Phase 4 — filled session template → sessions + results. Create-only, with a
read-only preview and an explicit commit."""
from __future__ import annotations

import io

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from pipeline.models import (Target, Company, Antibody, CellLine, Site, Member,
                                 ProtocolTemplate, ExperimentSession, WbResult, IfResult,
                                 IpResult, FcResult, Sample)
    from django.contrib.auth.models import User
    for M in (WbResult, IpResult, IfResult, FcResult, ExperimentSession, Sample,
              ProtocolTemplate, Antibody, CellLine, Company, Target):
        M.objects.using(DB).all().delete()
    site, _ = Site.objects.using(DB).get_or_create(short_code="MTL", defaults={"name": "Montreal"})
    user, _ = User.objects.using(DB).get_or_create(username="benchuser")
    member, _ = Member.objects.using(DB).get_or_create(
        user_id=user.pk, defaults={"site": site, "role": "experimenter",
                                   "is_active": True, "display_name": "Bench User"})
    tgt = Target.objects.using(DB).create(protein_name="SOD1", gene_name="SOD1")
    comp = Company.objects.using(DB).create(name="Proteintech")
    Antibody.objects.using(DB).create(target=tgt, company=comp, catalogue_number="12A8")
    Antibody.objects.using(DB).create(target=tgt, company=comp, catalogue_number="sc-101523")
    CellLine.objects.using(DB).create(target=tgt, name="SW620 WT", genotype="WT")
    CellLine.objects.using(DB).create(target=tgt, name="SW620 KO", genotype="KO")
    return {"site": site, "member": member, "target": tgt}


def _template_file(sheets):
    """sheets = {proc: (header, [rows...])} → in-memory .xlsx upload."""
    import openpyxl
    from django.core.files.uploadedfile import SimpleUploadedFile
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for proc, (header, rows) in sheets.items():
        ws = wb.create_sheet(proc)
        ws.append(header)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO(); wb.save(buf)
    return SimpleUploadedFile("filled.xlsx", buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


_WB_HEADER = ["session_ref", "gene", "antibody", "company", "cell_line_wt", "cell_line_ko",
              "experimenter", "date", "lysis_buffer", "protein_loading_ug",
              "dilution", "signal", "rating"]


def _wb_row(cat, signal="specific band", rating="pass"):
    return ["WB · SOD1 (new)", "SOD1", cat, "Proteintech", "SW620 WT", "SW620 KO",
            "Bench User", "2024-05-01", "RIPA", "20", "1:1000", signal, rating]


def test_preview_counts_and_conditions(seeded):
    from pipeline.services import session_import
    f = _template_file({"WB": (_WB_HEADER, [_wb_row("12A8"), _wb_row("sc-101523")])})
    parsed = session_import.parse_template(f)
    plan = session_import.plan_import(parsed, uploader=seeded["member"])
    assert plan["ok"]
    assert plan["counts"]["sessions"] == 1
    assert plan["counts"]["results"] == 2
    s = plan["sessions"][0]
    assert s["procedure"] == "WB" and s["gene"] == "SOD1"
    assert s["mode"] == "create"
    # The preview shows the line the write will *store*, not the cell as typed —
    # the same rule the supplier preview holds. `SW620 KO` is the stored name and
    # `SW620 SOD1 KO` is how it is named on screen, gene included, because a
    # parental's knockouts for two genes are otherwise the same string.
    assert s["cell_line_wt"] == "SW620 WT" and s["cell_line_ko"] == "SW620 SOD1 KO"
    assert s["cell_line_wt_error"] is None and s["cell_line_ko_error"] is None
    assert s["conditions"] >= 2                       # lysis_buffer + protein_loading_ug


def test_commit_creates_session_and_results(seeded):
    from pipeline.services import session_import
    from pipeline.models import ExperimentSession, WbResult
    f = _template_file({"WB": (_WB_HEADER, [_wb_row("12A8"), _wb_row("sc-101523")])})
    res = session_import.apply_import(session_import.parse_template(f), uploader=seeded["member"])
    assert res["ok"] and res["sessions_created"] == 1 and res["results_created"] == 2

    sess = ExperimentSession.objects.using(DB).get(procedure_type="WB", target=seeded["target"])
    assert sess.experimenter_id == seeded["member"].pk
    assert sess.cell_line_wt is not None and sess.cell_line_ko is not None
    assert sess.session_conditions.get("lysis_buffer") == "RIPA"
    assert str(sess.protein_loading_ug).startswith("20")
    results = WbResult.objects.using(DB).filter(session=sess)
    assert results.count() == 2
    assert set(results.values_list("signal", flat=True)) == {"specific band"}


def test_unmatched_antibody_is_skipped_not_guessed(seeded):
    from pipeline.services import session_import
    from pipeline.models import ExperimentSession, WbResult
    f = _template_file({"WB": (_WB_HEADER, [_wb_row("12A8"), _wb_row("ab-DOES-NOT-EXIST")])})
    parsed = session_import.parse_template(f)
    plan = session_import.plan_import(parsed, uploader=seeded["member"])
    assert plan["counts"]["results"] == 1 and plan["counts"]["blocked"] >= 1

    res = session_import.apply_import(session_import.parse_template(f), uploader=seeded["member"])
    assert res["results_created"] == 1                # only the matched row
    assert WbResult.objects.using(DB).count() == 1


def test_empty_file_refused(seeded):
    from pipeline.services import session_import
    f = _template_file({"WB": (_WB_HEADER, [])})       # header only, no data
    parsed = session_import.parse_template(f)
    plan = session_import.plan_import(parsed, uploader=seeded["member"])
    assert not plan["ok"]


def test_view_preview_and_commit(seeded):
    from django.test import RequestFactory
    from django.contrib.auth.models import User
    from pipeline import views
    import json
    user = User.objects.using(DB).get(username="benchuser")
    rf = RequestFactory()

    up = _template_file({"WB": (_WB_HEADER, [_wb_row("12A8")])})
    req = rf.post("/pipeline/session/template/upload/preview/", {"file": up}); req.user = user
    plan = json.loads(views.session_template_upload_preview(req).content)
    assert plan["ok"] and plan["counts"]["sessions"] == 1

    up2 = _template_file({"WB": (_WB_HEADER, [_wb_row("12A8")])})
    req = rf.post("/pipeline/session/template/upload/commit/", {"file": up2}); req.user = user
    res = json.loads(views.session_template_upload_commit(req).content)
    assert res["ok"] and res["sessions_created"] == 1 and res["results_created"] == 1


# ── one field, one column ────────────────────────────────────────────────────
#
# `WbResult.dilution` and `WbResult.primary_ab_dilution` are the same
# measurement: the Access import wrote `1AbDilution` into both. The workbook
# shipped a column for each, so one blot could come back with two dilutions and
# `report_generator` would print whichever `dilution or primary_ab_dilution`
# happened to pick.

_OLD_WB_HEADER = _WB_HEADER + ["primary_ab_dilution"]


def _old_wb_row(cat, dilution="", primary=""):
    row = _wb_row(cat)
    row[_WB_HEADER.index("dilution")] = dilution
    return row + [primary]


def test_the_wb_tab_ships_one_dilution_column(seeded):
    from pipeline.services import session_template
    cols = session_template._RESULT_COLS["WB"]
    assert "dilution" in cols
    assert "primary_ab_dilution" not in cols


def test_a_workbook_downloaded_before_the_merge_still_records_its_dilution(seeded):
    """The trap that makes an alias necessary rather than optional: a header the
    sheet no longer knows is read as a **session condition**, so every workbook
    already on somebody's disk would have filed its dilution as protocol
    metadata — one value for the whole tab — and said nothing about it."""
    from pipeline.models import WbResult
    from pipeline.services import session_import
    f = _template_file({"WB": (_OLD_WB_HEADER,
                               [_old_wb_row("12A8", primary="1:2000")])})
    parsed = session_import.parse_template(f)
    out = session_import.apply_import(parsed, uploader=seeded["member"])
    assert out["results_created"] == 1
    result = WbResult.objects.using(DB).get()
    assert result.dilution == "1:2000"
    assert result.session.session_conditions.get("primary_ab_dilution") is None


def test_the_column_the_sheet_still_ships_wins(seeded):
    """Both present is possible on a hand-edited sheet. The current name is the
    one the reader was looking at."""
    from pipeline.models import WbResult
    from pipeline.services import session_import
    f = _template_file({"WB": (_OLD_WB_HEADER,
                               [_old_wb_row("12A8", dilution="1:1000",
                                            primary="1:2000")])})
    parsed = session_import.parse_template(f)
    session_import.apply_import(parsed, uploader=seeded["member"])
    assert WbResult.objects.using(DB).get().dilution == "1:1000"


def test_an_old_column_is_a_reading_so_the_tab_is_an_experiment(seeded):
    """`_has_result` gates whether a tab becomes a session at all. A row whose
    only reading is in the retired column is still a row somebody wrote on."""
    from pipeline.services import session_import
    header = ["session_ref", "gene", "antibody", "company", "cell_line_wt",
              "cell_line_ko", "experimenter", "date", "primary_ab_dilution"]
    row = ["WB · SOD1 (new)", "SOD1", "12A8", "Proteintech", "SW620 WT",
           "SW620 KO", "Bench User", "2024-05-01", "1:2000"]
    parsed = session_import.parse_template(_template_file({"WB": (header, [row])}))
    plan = session_import.plan_import(parsed, uploader=seeded["member"])
    assert plan["counts"]["sessions"] == 1


def test_a_retired_column_is_not_reported_as_one_nobody_recognises(seeded):
    """It is recognised — it is the same field under its old name — so naming it
    in the amber "not recognised" panel would be an accusation about a sheet the
    app itself produced."""
    from pipeline.services import session_import
    parsed = session_import.parse_template(
        _template_file({"WB": (_OLD_WB_HEADER, [_old_wb_row("12A8", primary="1:2000")])}))
    plan = session_import.plan_import(parsed, uploader=seeded["member"])
    unknown = [u["header"] for u in plan["sessions"][0].get("unknown_columns", [])]
    assert "primary_ab_dilution" not in unknown


def test_a_cell_line_the_save_will_drop_is_named_at_the_check(seeded):
    """**A preview that is silent about a refusal is a refusal deferred.**

    `plan_import` has resolved the WT and KO since it was written and reported
    each refusal in `cell_line_wt_error`/`cell_line_ko_error` — and no panel drew
    them. So the eleventh field test previewed *"2 sessions to record, 6 results
    in them"* with nothing said about skipping, pressed Save, and got *"2 rows
    skipped"* naming neither the rows nor the reason. They were not rows: they
    were one unresolvable cell line per tab.

    Not a blocker — the session and its readings are still worth recording, the
    same bargain `bulk_antibodies` strikes with a concentration whose unit it
    cannot convert. Which is exactly why it has to be counted at the check.
    """
    from pipeline.services import session_import as si
    row = _wb_row("12A8")
    row[_WB_HEADER.index("cell_line_ko")] = "SW620 NOSUCH KO"
    parsed = si.parse_template(_template_file({"WB": (_WB_HEADER, [row])}))
    plan = si.plan_import(parsed, uploader=seeded["member"])

    assert plan["counts"]["sessions"] == 1
    assert plan["counts"]["dropped"] == 1, "the save will drop it — say so first"
    said = " ".join(d["reason"] for d in plan["dropped"])
    assert "NOSUCH" in said and "WB" in plan["dropped"][0]["label"]

    # And the count the check printed is the count the save reports, or the two
    # halves of one message disagree about what happened.
    out = si.apply_import(parsed, uploader=seeded["member"])
    assert out["sessions_created"] == 1 and out["results_created"] == 1
    assert len(out["skipped"]) == plan["counts"]["dropped"]


def test_a_clean_sheet_previews_and_saves_with_nothing_dropped(seeded):
    """The ordinary case says zero on both sides — a warning that fires on
    everything is a warning nobody reads."""
    from pipeline.services import session_import as si
    parsed = si.parse_template(_template_file({"WB": (_WB_HEADER, [_wb_row("12A8")])}))
    assert si.plan_import(parsed, uploader=seeded["member"])["counts"]["dropped"] == 0
    assert si.apply_import(parsed, uploader=seeded["member"])["skipped"] == []


def test_recording_by_workbook_issues_the_a_numbers_planning_would(seeded):
    """Run 19: planning STMN2 on the board gave its antibodies A-6 and A-7;
    recording ELP3 by workbook gave nothing, at the same bench the same
    afternoon. A number exists before the experiment through every door, and
    the receipt names what was issued — a number the app gives a record is a
    thing the app did."""
    from pipeline.models import Antibody
    from pipeline.services import session_import
    Antibody.objects.using(DB).filter(target=seeded["target"]).update(site=seeded["site"])
    f = _template_file({"WB": (_WB_HEADER, [_wb_row("12A8"), _wb_row("sc-101523")])})
    res = session_import.apply_import(session_import.parse_template(f), uploader=seeded["member"])
    assert res["ok"]
    numbers = sorted(Antibody.objects.using(DB).filter(target=seeded["target"])
                     .values_list("ab_number", flat=True))
    assert numbers == [1, 2], numbers
    assert [d["number"] for d in res["numbers_issued"]] == ["A-1", "A-2"]
