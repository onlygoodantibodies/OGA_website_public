"""
Test the antibody-table parser (spec §4). Pure-Python; no Django/DB.
Run:  python pipeline/services/cropper/tests/test_metadata.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, ROOT)

from pipeline.services.cropper import metadata as M  # noqa: E402


def test_clonality():
    assert M.parse_clonality("Recombinant Monoclonal") == ("monoclonal", True)
    assert M.parse_clonality("Monoclonal") == ("monoclonal", False)
    assert M.parse_clonality("polyclonal") == ("polyclonal", False)
    assert M.parse_clonality("Recombinant") == ("recombinant", True)
    assert M.parse_clonality("") == ("unknown", False)


def test_header_paste():
    tab = ("Catalogue\tCompany\tRRID\tClonality\tHost\tClone\n"
           "ab318262\tAbcam\tAB_2665480\tRecombinant Monoclonal\tRabbit\tEPR9999\n"
           "MAB17291R\tBio-Techne\tAB_2941516\tMonoclonal\tRat\t-")
    rows = M.parse_table(tab)
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["catalogue"] == "ab318262" and r0["company"] == "Abcam"
    assert r0["rrid"] == "AB_2665480" and r0["clonality"] == "monoclonal" and r0["is_recombinant"]
    assert r0["host"] == "rabbit" and r0["clone_id"] == "EPR9999"
    assert rows[1]["clone_id"] == ""          # "-" -> empty


def test_pattern_paste_and_rrid_collision():
    # no header, multi-space; the Abcam catalogue `ab318262` must NOT be read as an RRID
    ms = ("ab318262   Abcam   AB_2665480   Rabbit   Monoclonal\n"
          "GTX637386   GeneTex   AB_2909876   Rabbit   Polyclonal")
    rows = M.parse_table(ms)
    assert len(rows) == 2
    assert rows[0]["catalogue"] == "ab318262" and rows[0]["rrid"] == "AB_2665480"
    assert rows[1]["catalogue"] == "GTX637386"


def test_aviva_suffix_and_asterisks():
    av = "ARP49413_P050**   Aviva Systems Biology   AB_10000   Rabbit   Polyclonal"
    r = M.parse_table(av)[0]
    assert r["catalogue"] == "ARP49413_P050"     # asterisks stripped, Aviva suffix kept
    assert r["company"] == "Aviva Systems Biology"


def test_wrapped_pdf_paste():
    # real ARID2 table copied from a PDF: cells wrap across lines, company-first,
    # a multi-line vendor name with a "(DSHB)" abbreviation, an en-dash catalogue.
    txt = (
        "Company Catalog\nnumber\nLot\nnumber\nRRID (Antibody\nRegistry)\n"
        "Clonality Clone ID Host\n"
        "Cell Signaling Technology 82342** 3 AB_2799992 recombinant\nmono\nD8D8U rabbit 0.24 Wb, IP\n"
        "Developmental Studies\nHybridoma Bank (DSHB)\nPCRPARID2-\n1A1*\n12/13/18 AB_2618409 monoclonal PCRPARID2-\n1A1\nmouse 0.06 IP\n"
        "GeneTex GTX129443 41551 AB_2885997 polyclonal - rabbit 1.00 Wb, IP, IF\n"
        "GeneTex GTX632011* 44517 AB_2888275 monoclonal GT7311 mouse 1.00 Wb, IF\n"
        "Proteintech 23406–1-AP 00025260 AB_2918080 polyclonal - rabbit 0.23 Wb")
    rows = M.parse_table(txt)
    assert len(rows) == 5, f"expected 5 rows, got {len(rows)}: {[r['catalogue'] for r in rows]}"
    by_rrid = {r["rrid"]: r for r in rows}
    r = by_rrid["AB_2799992"]
    assert r["catalogue"] == "82342" and r["company"] == "Cell Signaling Technology"
    assert r["clonality"] == "monoclonal" and r["is_recombinant"] and r["host"] == "rabbit"
    assert by_rrid["AB_2918080"]["catalogue"] == "23406-1-AP"        # en-dash normalised
    assert by_rrid["AB_2618409"]["company"] == "Developmental Studies Hybridoma Bank"
    # lot + supplier-recommended applications captured from the same table
    assert r["lot"] == "3" and r["supplier_apps"] == ["WB", "IP"]     # CST 82342, "Wb, IP"
    assert by_rrid["AB_2918080"]["supplier_apps"] == ["WB"]           # Proteintech, "Wb"
    # concentration kept as pasted (µg/µL bare number), read from AFTER the host
    assert r["concentration"] == "0.24"                              # CST 82342, "…rabbit 0.24 Wb, IP"
    assert by_rrid["AB_2918080"]["concentration"] == "0.23"          # Proteintech "…rabbit 0.23 Wb"


def test_gefh1_wrapped_pdf_leading_unknown_vendor():
    # real GEF-H1 (ARHGEF2) table from a PDF: company-first, wrapped cells, and
    # crucially the FIRST row is Abbexa (a vendor that must be recognised or the
    # whole row is dropped), with letter-leading lots (A2511652Q, QC…) that must
    # not be fused onto the catalogue.
    txt = (
        "Company Catalog number Lot number RRID (Antibody Registry) Clonality "
        "Clone ID Host Concentration Vendors recommended applications\n"
        "Abbexa abx422627** A2511652Q AB_3739799 recombinant mono J175 rabbit 1.00 Wb, IF\n"
        "Abcam ab155785 1099750-14 AB_2818944 polyclonal - rabbit 0.71 Wb, IP, IF\n"
        "Abcam ab201687** 1121851-1 AB_3740827 recombinant mono EPR17963 rabbit 0.59 Wb, IF, other\n"
        "Aviva Systems Biology ARP59669_P050 QC30631-40645 AB_10880757 polyclonal - rabbit 0.50 Wb\n"
        "Aviva Systems Biology ARP84595_P050 QC60787-42921 AB_3740825 polyclonal - rabbit 0.50 Wb\n"
        "GeneTex GTX125893 45476 AB_11177391 polyclonal - rabbit 0.71 Wb, IP, IF\n"
        "Proteintech 24472-1-AP 00082122 AB_2879560 polyclonal - rabbit 1.00 Wb, IF, other\n"
        "Thermo Fisher Scientific MA5-34750** 79532277 AB_2848658 recombinant mono JG36-46 rabbit 1.00 Wb, IF, other")
    rows = M.parse_table(txt)
    by_rrid = {r["rrid"]: r for r in rows}
    # the Abbexa row must be present and its catalogue clean (lot not fused)
    assert "AB_3739799" in by_rrid, f"Abbexa row dropped: {[r['catalogue'] for r in rows]}"
    abx = by_rrid["AB_3739799"]
    assert abx["catalogue"] == "abx422627" and abx["company"] == "Abbexa"
    assert abx["clonality"] == "monoclonal" and abx["is_recombinant"] and abx["host"] == "rabbit"
    assert abx["clone_id"] == "J175"
    # Aviva letter-leading lots must be stripped, not fused
    assert by_rrid["AB_10880757"]["catalogue"] == "ARP59669_P050"
    assert by_rrid["AB_3740825"]["catalogue"] == "ARP84595_P050"
    # a normal digit-lot row still parses
    assert by_rrid["AB_11177391"]["catalogue"] == "GTX125893"


def test_slit1_ipi_vendor_and_clone_collision():
    # real SLIT1 table: the "Institute for Protein Innovation (IPI)" rows were
    # dropped (unknown vendor). Its clone ids are literally "IPI-Slit1.NN", so a
    # bare "IPI" vendor alias must NOT be used (it would split every row).
    txt = (
        "Company Catalog number Lot number RRID (Antibody Registry) Clonality "
        "Clone ID Host Concentration Vendors recommended applications\n"
        "Abcam ab151724** 1094105-1 AB_3233479 recombinant mono EP5797(2) rabbit 1.95 Wb\n"
        "GeneTex GTX134122 43243 AB_2887222 polyclonal - rabbit 0.81 Wb, IF\n"
        "Institute for Protein Innovation (IPI) TAB0010278-013 3 AB_3741700 recombinant mono IPI-Slit1.11 rabbit 0.50 IP\n"
        "Institute for Protein Innovation (IPI) TAB0010288-013 3 AB_3741702 recombinant mono IPI-Slit1.21 rabbit 0.50 IP\n"
        "Institute for Protein Innovation (IPI) TAB0010315-013 3 AB_3741705 recombinant mono IPI-Slit1.48 rabbit 0.50 IP")
    rows = M.parse_table(txt)
    by_rrid = {r["rrid"]: r for r in rows}
    assert len(rows) == 5, f"expected 5 rows, got {len(rows)}: {[r['catalogue'] for r in rows]}"
    r = by_rrid["AB_3741700"]
    assert r["catalogue"] == "TAB0010278-013"
    assert r["company"] == "Institute for Protein Innovation"
    assert r["clonality"] == "monoclonal" and r["is_recombinant"] and r["host"] == "rabbit"
    assert r["clone_id"] == "IPI-Slit1.11"          # clone id kept intact, row not split
    assert by_rrid["AB_3741705"]["catalogue"] == "TAB0010315-013"


def test_unknown_vendor_wrapped_not_dropped():
    # wrapped-PDF paste (so the anchored path is used) containing a vendor that is
    # NOT in KNOWN_VENDORS. The safety net must still capture the row from its
    # RRID — catalogue/clonality/host filled, company left blank for the human.
    txt = (
        "Company Catalog\nnumber Lot RRID Clonality Clone Host\n"
        "Abcam ab111 100 AB_2000001 polyclonal - rabbit\n"
        "Weird Bio\nCompany XZ-999\n201 AB_2000002 recombinant\nmono CL9 mouse\n"
        "GeneTex GTX222 300 AB_2000003 polyclonal - rabbit")
    rows = M.parse_table(txt)
    by = {r["rrid"]: r for r in rows}
    assert len(rows) == 3, f"unknown-vendor row dropped: {[r['catalogue'] for r in rows]}"
    u = by["AB_2000002"]
    assert u["catalogue"] == "XZ-999" and u["host"] == "mouse"
    assert u["clonality"] == "monoclonal" and u["is_recombinant"]
    assert u["clone_id"] == "CL9"
    assert u["company"] == ""          # unknown vendor → blank, flagged for the human
    # the known-vendor rows are unaffected
    assert by["AB_2000001"]["company"] == "Abcam"
    assert by["AB_2000003"]["company"] == "GeneTex"


def main():
    test_clonality()
    test_header_paste()
    test_pattern_paste_and_rrid_collision()
    test_aviva_suffix_and_asterisks()
    test_wrapped_pdf_paste()
    test_gefh1_wrapped_pdf_leading_unknown_vendor()
    test_slit1_ipi_vendor_and_clone_collision()
    test_unknown_vendor_wrapped_not_dropped()
    print("OK — metadata parser: clonality, header + pattern pastes, RRID/catalogue "
          "collision, Aviva suffix all verified.")


if __name__ == "__main__":
    main()


# ── the two things uOttawa's first spreadsheet found ─────────────────────────
#
# 44 antibodies, in the lab's own layout, uploaded as **0 rows** — and had they
# been read, every concentration would have gone in a thousand times too low.
# Both are pinned here because both are invisible from the screen: one shows as
# an empty preview a person would read as "already applied", the other as a
# number nothing on any page contradicts.

def _sheet(header, *rows):
    return "\n".join("\t".join(r) for r in ((header,) + rows))


def test_catalogue_spelled_with_a_hash_is_still_a_catalogue():
    # `catalogue #` was in no alias while `cat#` and `catalogue number` both
    # were. `parse_table` drops every row with no catalogue, so one missing
    # spelling read a full sheet as nothing at all.
    rows = M.parse_table(
        _sheet(("Gene Name", "Catalogue #", "Company", "Lot #"),
               ("ADAM10", "ab124695", "Abcam", "1102604-18")),
        header_led=True)
    assert len(rows) == 1
    assert rows[0]["catalogue"] == "ab124695" and rows[0]["lot"] == "1102604-18"


def test_punctuation_on_a_heading_is_decoration():
    # The general rule behind the fix above: `catalogue #` must not be the only
    # spelling anybody ever thinks of again.
    assert M.header_field("Cat.No.") == "catalogue"
    assert M.header_field("Clone #") == "clone_id"
    assert M.header_field("Conc.") == "concentration"
    assert M.header_field("Box #") == "box"
    # And a heading that names nothing still names nothing.
    assert M.header_field("Ab request reference") == ""


def test_a_heading_that_names_a_unit_means_its_cells_are_in_that_unit():
    # The silent one. `Concentration (mg/mL)` over a bare `0.498` was stored as
    # 0.498 µg/mL — the exact thousand-fold error services/concentration.py
    # exists to prevent, arriving through the header instead of the cell.
    rows = M.parse_table(
        _sheet(("Gene Name", "Catalogue #", "Concentration (mg/mL)"),
               ("ADAM10", "ab124695", "0.498")),
        header_led=True)
    assert rows[0]["concentration"] == "0.498 mg/ml"
    assert rows[0]["concentration_unit_from"] == "heading"


def test_a_unit_in_the_cell_beats_the_heading():
    rows = M.parse_table(
        _sheet(("Gene Name", "Catalogue #", "Concentration (mg/mL)"),
               ("ADAM10", "ab124695", "500 ug/mL")),
        header_led=True)
    assert rows[0]["concentration"] == "500 ug/mL"
    assert rows[0].get("concentration_unit_from", "") == ""


def test_this_apps_own_export_stores_the_same_number():
    # The heading rule must not rescale the round trip it was written to
    # protect. This app's own sheet is headed `(ug/mL)`, which is the stored
    # unit, so the cell gains the unit as text and the *value* is untouched —
    # which is the half that matters, and the half a string comparison would
    # have missed in the wrong direction.
    from pipeline.services import concentration as C
    rows = M.parse_table(
        _sheet(("gene", "catalogue", "concentration (ug/mL)"),
               ("ADAM10", "ab124695", "1000")),
        header_led=True)
    assert C.parse(rows[0]["concentration"])[0] == C.parse("1000")[0]


def test_the_heading_unit_is_what_gets_stored():
    # The end of the chain: mg/mL in the heading, bare digits in the cell, and
    # the number that reaches the column is a thousand times the digits.
    from pipeline.services import concentration as C
    rows = M.parse_table(
        _sheet(("Gene Name", "Catalogue #", "Concentration (mg/mL)"),
               ("ADAM10", "ab124695", "0.498")),
        header_led=True)
    value, err = C.parse(rows[0]["concentration"])
    assert err == "" and float(value) == 498.0


def test_storage_box_and_received_are_read():
    rows = M.parse_table(
        _sheet(("Gene Name", "Catalogue #", "Storage", "Box #", "Date received"),
               ("ADAM10", "ab124695", "(-20°C)", "42", "August 2026")),
        header_led=True)
    r = rows[0]
    assert r["storage_type"] == "(-20°C)" and r["box"] == "42"
    assert r["received"] == "August 2026"


def test_a_wrapped_pdf_row_never_invents_a_freezer():
    # The anchored walk reads published tables. A freezer box is not in one, and
    # a guess about somebody's freezer is worse than a blank.
    rows = M.parse_table(
        "ab318262   Abcam   AB_2665480   Rabbit   Monoclonal\n"
        "GTX637386   GeneTex   AB_2909876   Rabbit   Polyclonal")
    assert all(r["box"] == "" and r["storage_type"] == "" and r["received"] == ""
               for r in rows)
