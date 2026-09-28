"""A knockdown is a control, and it is not a knockout.

The Access database recorded 115 knockdown blots as a tick on the blot with
the lane pointing at the plain wild type (18 Sep 2026), so nothing downstream
could tell a knockdown result from a knockout one and every public surface
said "knockout-controlled" over both. `CellLine.Genotype.KNOCKDOWN` is the
row a knockdown gets; `services/cell_lines.py::CONTROL` is what a session's
second slot asks for. Pinned here: the label says KD and reads back, the
control slot finds both kinds, the paste box reads the lab's words, and the
public phrase changes only when a figure says knockdown.
"""
from __future__ import annotations

import pytest

DB = "pipeline_db"


@pytest.fixture
def kd(_pipeline_db):
    from pipeline.models import CellLine, Site, Target
    CellLine.objects.using(DB).all().delete()
    Target.objects.using(DB).filter(gene_name="GPNMB").delete()
    mcg, _ = Site.objects.using(DB).get_or_create(
        short_code="MCG", defaults={"name": "McGill"})
    gpnmb = Target.objects.using(DB).create(gene_name="GPNMB", protein_name="Glycoprotein NMB")
    wt = CellLine.objects.using(DB).create(name="U-87 MG", genotype="WT", site=mcg)
    kd = CellLine.objects.using(DB).create(
        name="U-87 MG", genotype="KD", site=mcg, target=gpnmb, parent_line=wt,
        knockdown_method="siRNA", knockdown_supplier="Dharmacon",
        knockdown_catalogue="L-012345-00-0005", transfection_reagent="RNAiMAX")
    ko = CellLine.objects.using(DB).create(
        name="U-87 MG", genotype="KO", site=mcg, target=gpnmb, parent_line=wt)
    return {"site": mcg, "target": gpnmb, "wt": wt, "kd": kd, "ko": ko}


def test_the_label_says_which_kind_of_control_and_reads_back(kd):
    from pipeline.services import cell_lines as clines
    assert clines.label(kd["kd"]) == "U-87 MG GPNMB KD — McGill"
    assert clines.label(kd["ko"]) == "U-87 MG GPNMB KO — McGill"
    # Round trip: the rendered label finds exactly the row it was rendered from.
    line, err = clines.resolve("U-87 MG GPNMB KD — McGill")
    assert err is None and line.pk == kd["kd"].pk
    line, err = clines.resolve("U-87 MG GPNMB KO — McGill")
    assert err is None and line.pk == kd["ko"].pk


def test_the_control_slot_takes_either_kind_and_the_ko_slot_only_a_knockout(kd):
    from pipeline.services import cell_lines as clines
    both = clines.candidates("U-87 MG", genotype=clines.CONTROL)
    assert {cl.pk for cl in both} == {kd["kd"].pk, kd["ko"].pk}
    assert [cl.pk for cl in clines.candidates("U-87 MG", genotype="KO")] == [kd["ko"].pk]
    assert [cl.pk for cl in clines.candidates("U-87 MG", genotype="KD")] == [kd["kd"].pk]
    # A wild-type slot still never sees a knockdown.
    assert [cl.pk for cl in clines.candidates("U-87 MG", genotype="WT")] == [kd["wt"].pk]


def test_a_session_offers_the_knockdown_in_the_control_box(kd):
    from pipeline.services import cell_lines as clines
    opts = clines.session_options(kd["target"])
    labels = [o["label"] for o in opts["ko"]]
    assert any("GPNMB KD" in l for l in labels), labels
    assert any("GPNMB KO" in l for l in labels), labels
    assert all("KD" not in o["label"] for o in opts["wt"])


def test_a_refusal_names_the_kind_it_wanted(kd):
    from pipeline.services import cell_lines as clines
    line, err = clines.resolve("U-87 MG", genotype="KD", site_id=kd["site"].pk)
    assert line is not None and line.pk == kd["kd"].pk
    _, err = clines.resolve("HAP1", genotype=clines.CONTROL)
    assert "knockout or knockdown" in err


def test_the_paste_box_reads_the_labs_words_for_a_knockdown(_pipeline_db):
    from pipeline.services.bulk_cell_lines import norm_genotype, _derived_name
    for word in ("KD", "knockdown", "knock-down", "siRNA", "shRNA", "CRISPRi"):
        assert norm_genotype(word) == "KD", word
    assert norm_genotype("", "U-87 MG GPNMB KD") == "KD"
    assert norm_genotype("", "HAP1 KO") == "KO"
    assert norm_genotype("KO") == "KO"
    assert _derived_name({"name": "", "gene": "GPNMB"}, "KD") == "GPNMB KD"


def test_the_knockdown_summary_names_what_it_was_made_with_and_nothing_else(kd):
    from pipeline.services import cell_lines as clines
    assert clines.knockdown_summary(kd["kd"]) == "siRNA · Dharmacon L-012345-00-0005 · RNAiMAX"
    assert clines.knockdown_summary(kd["ko"]) == ""
    kd["kd"].knockdown_method = ""
    kd["kd"].knockdown_supplier = ""
    kd["kd"].knockdown_catalogue = ""
    kd["kd"].transfection_reagent = ""
    assert clines.knockdown_summary(kd["kd"]) == "", "an empty record must not invent a method"


def test_the_public_phrase_changes_only_when_a_figure_says_knockdown(_pipeline_db):
    from pipeline.public import control_phrase, control_noun
    assert control_phrase(set()) == "Knockout-controlled"
    assert control_phrase({"KO"}) == "Knockout-controlled"
    assert control_phrase({"KD"}) == "Knockdown-controlled"
    assert control_phrase({"KO", "KD"}) == "Knockout- and knockdown-controlled"
    assert control_phrase({"KD"}, capital=False) == "knockdown-controlled"
    assert control_noun({"KD"}) == "knockdown controls"
    assert control_noun({"KO", "KD"}) == "knockout and knockdown controls"


def test_every_procedure_records_how_a_knockdown_was_done(_pipeline_db):
    from pipeline.views.session_entry import (PROCEDURE_CONDITION_FIELDS,
                                              KNOCKDOWN_CONDITION_FIELDS)
    keys = {k for k, *_ in KNOCKDOWN_CONDITION_FIELDS}
    assert {"kd_reagent", "kd_transfection_reagent", "kd_concentration_nm",
            "kd_hours_post_transfection", "kd_control"} == keys
    for proc, fields in PROCEDURE_CONDITION_FIELDS.items():
        assert keys <= {k for k, *_ in fields}, proc


def test_a_cell_line_sheet_from_before_the_knockdown_columns_still_reads(_pipeline_db):
    """The template gained five columns on 18 Sep 2026. A sheet downloaded
    before that — the original nineteen headings — is read by heading, so
    every row lands with the knockdown fields blank and nothing refused."""
    from pipeline.services import bulk_cell_lines
    old_headings = ["name", "gene", "genotype", "parent", "c number", "cellosaurus",
                    "supplier", "catalogue", "lot", "site", "medium",
                    "growth properties", "species", "clone", "storage", "freezer",
                    "box", "position", "comments"]
    text = "\t".join(old_headings) + "\n" + "\t".join(
        ["HAP1", "SNCA", "KO", "", "", "", "Horizon", "HZGHC001", "", "", "IMDM",
         "adherent", "Human", "", "-80", "F2", "B1", "A1", "old sheet"])
    rows = bulk_cell_lines.parse(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "HAP1" and row["gene"] == "SNCA" and row["catalogue"] == "HZGHC001"
    assert row["origin_comments"] == "old sheet"
    for key in ("knockdown_method", "knockdown_supplier", "knockdown_catalogue",
                "knockdown_sequence", "transfection_reagent"):
        assert not row.get(key), key
