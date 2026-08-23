"""The lab's own conventions, checked against the shipped database.

Every assertion here comes from reading a dump of the live pipeline database
(31 Jul 2026) against what the code expected, after the owner's own review of
the app. Three of the app's stated rules were wrong about the data:

* **A cell line is named for the line, not the knockout.** `name` is the bare
  parental — `HAP1` 151 times, `HeLa` 54, `HEK293T` 32 — with the gene in its
  own column. 395 of the 398 knockouts do not mention their gene in the name,
  and the Add panel's example row said `HAP1 SNCA KO`.
* **A parent is written as a C-number.** 145 of the 169 recorded parents are
  C-numbers, 24 are names, and `resolve_parent` said "matched by name, not by
  C-number". 102 of those numbers are a *vial's* C-number rather than a line's,
  which is why only 2 of 564 rows had a parent linked at all.
* **`NA` in a gene column means "there isn't one".** It created a Target called
  NA; 96 of the 136 wild types point at it, so the board printed NA in the GENE
  cell of exactly the rows whose point is that they have no gene.

Plus the example row itself, which the review said does not read as an example.
"""
from __future__ import annotations

import io
import re

from django.contrib.auth.models import User
from django.test import Client, TestCase

from pipeline.models import CellLine, CellLineVial, Member, Site, Target
from pipeline.services import bulk_cell_lines as bulkcl
from pipeline.services import cell_lines as cell_line_svc
from pipeline.services import example_row
from pipeline.services import targets as target_svc
from pipeline.services import cell_line_board
from pipeline.views.imports import CELL_LINE_COLUMNS, CELL_LINE_EXAMPLE

DB = "pipeline_db"


class NotApplicableIsAnAnswerTests(TestCase):
    """`NA` says a row has no gene. It must never become one."""

    databases = {"default", DB}

    def test_na_and_its_spellings_are_recognised(self):
        for said in ("NA", "na", "N/A", "n.a.", "None", "not applicable", "-"):
            self.assertTrue(target_svc.is_not_applicable(said), said)
        for gene in ("SNCA", "NAT1", "NANS", ""):
            self.assertFalse(target_svc.is_not_applicable(gene), gene)

    def test_na_never_resolves_to_a_target_even_when_one_exists(self):
        # The live database has exactly this row, from the Access import.
        Target.objects.create(gene_name="NA", protein_name="Not applicable")
        self.assertIsNone(target_svc.resolve_target("NA"))

    def test_na_never_creates_a_target(self):
        target, created = target_svc.resolve_or_create_target("NA")
        self.assertIsNone(target)
        self.assertFalse(created)
        self.assertFalse(Target.objects.filter(gene_name__iexact="NA").exists())

    def test_the_na_target_reads_as_no_gene(self):
        na = Target.objects.create(gene_name="NA", protein_name="Not applicable")
        real = Target.objects.create(gene_name="SNCA", protein_name="Synuclein")
        self.assertEqual(target_svc.gene_of(na), "")
        self.assertEqual(target_svc.gene_of(real), "SNCA")

    def test_a_wild_type_attached_to_the_na_target_shows_no_gene_on_the_board(self):
        na = Target.objects.create(gene_name="NA", protein_name="Not applicable")
        line = CellLine.objects.create(name="HeLa", genotype="WT", target=na)
        self.assertEqual(cell_line_board.row_for(line)["gene"], "")

    def test_a_wild_type_row_saying_na_is_planned_as_a_wild_type(self):
        rows = bulkcl.parse("name\tgene\nHAP1\tNA")
        item, = bulkcl.plan(rows)
        self.assertEqual(item["genotype"], "WT")
        self.assertEqual(item["gene"], "")
        self.assertIsNone(item["target_id"])
        self.assertNotEqual(item["status"], "blocked")

    def test_a_knockout_row_saying_na_is_refused_and_says_why(self):
        rows = bulkcl.parse("name\tgene\tgenotype\nHAP1\tNA\tKO")
        item, = bulkcl.plan(rows)
        self.assertEqual(item["status"], "blocked")
        self.assertIn("knockout", item["note"])
        self.assertIn("NA", item["note"])


class AParentIsNamedOrNumberedTests(TestCase):
    """145 of 169 parents on file are C-numbers, and 102 are a *vial's*."""

    databases = {"default", DB}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        self.other = Site.objects.create(name="McGill", short_code="MCG")
        self.target = Target.objects.create(gene_name="SNCA", protein_name="Syn")
        self.wt = CellLine.objects.create(name="HAP1", genotype="WT",
                                          site=self.site, c_number=48)
        # A parental whose number lives on a freeze-down batch, not on the line —
        # the commoner case in the live data, and the one nothing joined.
        self.wt_by_vial = CellLine.objects.create(name="U2OS", genotype="WT",
                                                  site=self.site)
        CellLineVial.objects.create(cell_line=self.wt_by_vial, c_number=124)

    def test_a_c_number_on_the_line_finds_it(self):
        self.assertEqual([c.pk for c in cell_line_svc.by_c_number("C-48")],
                         [self.wt.pk])

    def test_a_c_number_on_a_vial_finds_its_line(self):
        self.assertEqual([c.pk for c in cell_line_svc.by_c_number("C-124")],
                         [self.wt_by_vial.pk])

    def test_the_written_forms_all_read(self):
        for written in ("C-48", "c-48", "C48", "48", "#48"):
            self.assertEqual([c.pk for c in cell_line_svc.by_c_number(written)],
                             [self.wt.pk], written)

    def test_a_parent_given_as_a_c_number_resolves_and_says_so(self):
        parent, note = bulkcl.resolve_parent("C-48", site_id=self.site.pk)
        self.assertEqual(parent.pk, self.wt.pk)
        self.assertIn("C-48", note)
        self.assertIn("HAP1", note)

    def test_a_parent_given_by_name_still_resolves(self):
        parent, note = bulkcl.resolve_parent("HAP1", site_id=self.site.pk)
        self.assertEqual(parent.pk, self.wt.pk)
        self.assertIn("your site", note)

    def test_several_parents_link_the_first_and_keep_the_rest(self):
        # `C-153/C-420/C-421` — nineteen rows in the live database are written
        # this way, and parent_line is a single ForeignKey.
        parent, note = bulkcl.resolve_parent("C-48/C-124", site_id=self.site.pk)
        self.assertEqual(parent.pk, self.wt.pk)
        self.assertIn("C-124", note)
        self.assertIn("also recorded", note)

    def test_a_parent_that_matches_nothing_names_both_forms(self):
        parent, note = bulkcl.resolve_parent("C-9999", site_id=self.site.pk)
        self.assertIsNone(parent)
        self.assertIn("C-", note)
        self.assertIn("named", note)

    def test_your_own_site_wins_a_shared_name(self):
        theirs = CellLine.objects.create(name="HAP1", genotype="WT", site=self.other)
        parent, _note = bulkcl.resolve_parent("HAP1", site_id=self.other.pk)
        self.assertEqual(parent.pk, theirs.pk)


class AKnockoutRowNeverMatchesAWildTypeTests(TestCase):
    """Teaching the true naming convention makes this collision the normal case.

    `find_cell_line` separated the two genotypes by `target` — which works only
    while the gene is known. A KO naming a gene that is *not in the pipeline yet*
    resolves to `target=None`, so the name branch asked for a line called HAP1
    with no target and found the **parental**. The preview said "already on file
    — will be updated" about the wild type, and a commit would have filled that
    row's blanks from the knockout's cells.

    It was survivable while the example taught `HAP1 SNCA KO`, because those
    names are nearly unique. The real convention is the bare parental name, and
    **437 of the 562 lines on file carry a name that some row of the opposite
    genotype also carries** — HAP1, HeLa, HCT116, SH-SY5Y, U2OS, A549. Found by
    driving a browser at the corrected example row.
    """

    databases = {"default", DB}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        self.wt = CellLine.objects.create(name="HAP1", genotype="WT", site=self.site)

    def test_a_knockout_of_an_unknown_gene_does_not_land_on_the_parental(self):
        rows = bulkcl.parse("name\tgene\tgenotype\nHAP1\tTRPA1\tKO")
        item, = bulkcl.plan(rows, create_targets=False)
        self.assertEqual(item["status"], "blocked")
        self.assertNotIn("already on file", item["note"])
        self.assertIn("not in the pipeline", item["note"])

    def test_a_knockout_row_does_not_update_a_wild_type_of_the_same_name(self):
        rows = bulkcl.parse("name\tgenotype\nHAP1\tKO")
        found = bulkcl.find_cell_line(rows[0], None, site_id=self.site.pk)
        self.assertIsNone(found)

    def test_a_wild_type_row_still_finds_the_wild_type(self):
        rows = bulkcl.parse("name\tgenotype\nHAP1\tWT")
        found = bulkcl.find_cell_line(rows[0], None, site_id=self.site.pk)
        self.assertEqual(found.pk, self.wt.pk)

    def test_a_knockout_finds_its_own_row_when_the_gene_is_known(self):
        target = Target.objects.create(gene_name="SNCA", protein_name="Syn")
        ko = CellLine.objects.create(name="HAP1", genotype="KO", target=target,
                                     site=self.site)
        rows = bulkcl.parse("name\tgene\tgenotype\nHAP1\tSNCA\tKO")
        found = bulkcl.find_cell_line(rows[0], target, site_id=self.site.pk)
        self.assertEqual(found.pk, ko.pk)

    def test_a_row_from_the_access_import_with_no_genotype_still_matches(self):
        # Nothing in the live data has a blank genotype, but a sheet's own round
        # trip must not start creating copies if one ever does.
        legacy = CellLine.objects.create(name="HEK293T", genotype="", site=self.site)
        rows = bulkcl.parse("name\tgenotype\nHEK293T\tWT")
        found = bulkcl.find_cell_line(rows[0], None, site_id=self.site.pk)
        self.assertEqual(found.pk, legacy.pk)


class TheNameIsTheLineTests(TestCase):
    """`name` is the parental line; the gene has its own column."""

    databases = {"default", DB}

    def test_the_example_row_is_named_the_way_the_database_is(self):
        example = dict(zip(CELL_LINE_COLUMNS, CELL_LINE_EXAMPLE))
        self.assertEqual(example["name"], "HAP1")
        self.assertEqual(example["gene"], "SNCA")
        self.assertEqual(example["genotype"], "KO")
        # The name must not carry the gene or the genotype — that is the
        # convention this example used to teach and the data does not use.
        self.assertNotIn("SNCA", example["name"])
        self.assertNotIn("KO", example["name"])

    def test_the_example_parent_is_a_c_number(self):
        example = dict(zip(CELL_LINE_COLUMNS, CELL_LINE_EXAMPLE))
        self.assertTrue(re.fullmatch(r"C-\d+", example["parent"]), example["parent"])

    def test_the_paired_ko_column_says_who_it_is_for(self):
        heading = next(c for c in CELL_LINE_COLUMNS if c.startswith("paired ko"))
        self.assertIn("WT", heading)
        # …and the example, being a knockout, leaves it blank.
        self.assertEqual(dict(zip(CELL_LINE_COLUMNS, CELL_LINE_EXAMPLE))[heading], "")

    def test_both_spellings_of_the_heading_still_parse(self):
        for heading in ("paired ko", "paired ko (WT rows)"):
            rows = bulkcl.parse(f"name\tgenotype\t{heading}\nHAP1\tWT\t631")
            self.assertEqual(rows[0]["pair_c"], "631", heading)


class TheExampleRowSaysItIsAnExampleTests(TestCase):
    """It is labelled `e.g.`, and no parser ever ingests it."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        Member.objects.create(user_id=pu.pk, site_id=self.site.pk,
                              role="experimenter", is_active=True, display_name="Vera")
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def test_the_mark_is_recognised_on_the_first_cell_that_has_anything(self):
        self.assertTrue(example_row.is_example(["e.g. HAP1", "SNCA"]))
        self.assertTrue(example_row.is_example(["", "e.g. SNCA"]))
        self.assertFalse(example_row.is_example(["HAP1", "SNCA"]))
        self.assertFalse(example_row.is_example([]))

    def test_a_pasted_template_does_not_create_its_own_example(self):
        text = ("name\tgene\tgenotype\n"
                "e.g. HAP1\tSNCA\tKO\n"
                "HeLa\tSOD1\tKO")
        rows = bulkcl.parse(text)
        self.assertEqual([r["name"] for r in rows], ["HeLa"])

    def test_every_template_ships_the_example_labelled(self):
        import openpyxl
        for kind in ("antibodies", "cell-lines", "sessions", "targets"):
            resp = self.client.get(f"/pipeline/import/template/{kind}/")
            self.assertEqual(resp.status_code, 200, kind)
            ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
            first = str(ws.cell(row=2, column=1).value or "")
            self.assertTrue(first.lower().startswith(example_row.MARK),
                            f"{kind}: row 2 is {first!r}")
            # Row 3 is where a person's own first record goes, and the freeze
            # keeps both the headings and the example on screen.
            self.assertEqual(ws.freeze_panes, "A3", kind)

    def test_a_downloaded_template_uploaded_untouched_creates_nothing(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.get("/pipeline/import/template/cell-lines/")
        upload = self.client.post(
            "/pipeline/import/upload/cell-lines/",
            {"file": SimpleUploadedFile("cell_lines_template.xlsx", resp.content)})
        self.assertEqual(upload.status_code, 200)
        # The headings are read, the example is dropped, and nothing is offered
        # for creation — which is what an untouched template should amount to.
        self.assertEqual(upload.json()["items"], [])


class TheGridShowsTheExampleWithoutOfferingItTests(TestCase):
    """board.js draws it as a pinned header row, not as placeholder text."""

    def setUp(self):
        from pathlib import Path
        self.js = Path("pipeline/static/pipeline/board.js").read_text()

    def test_the_example_is_a_row_of_its_own(self):
        self.assertIn("function exampleRow(", self.js)
        self.assertIn("${exampleRow(cols, cfg.example)}", self.js)

    def test_it_is_inside_the_head_where_tsv_cannot_reach_it(self):
        head = self.js[self.js.index("<thead class=\"bg-gray-50\">"):]
        head = head[:head.index("</thead>")]
        self.assertIn("exampleRow(cols, cfg.example)", head)

    def test_row_one_has_no_placeholder_pretending_to_be_data(self):
        # The whole complaint: placeholder text on row 0 reads as a filled-in
        # first row, and vanishes exactly when it is being used.
        self.assertNotIn("placeholder=\"${esc(cfg.example[i])}\"", self.js)

    def test_the_duplicate_legend_underneath_is_gone(self):
        self.assertNotIn("exampleLegend", self.js)


class BothDoorsToAGeneTests(TestCase):
    """The hub said adding a gene was the only way. The target board is another."""

    databases = {"default", DB, "academy_db"}

    def test_the_hub_does_not_claim_one_door(self):
        import importlib
        hub = importlib.import_module("pipeline.views.hub")
        text = " ".join(d[2] for d in hub.TASK_DEFS) + " " + (hub.__doc__ or "")
        self.assertNotIn("The only way a target gets", text)
        self.assertNotIn("there is no other route", text.lower())

    def test_both_doors_are_in_the_first_step(self):
        from pipeline.views.hub import PHASES
        first = next(p for p in PHASES if p[0] == "start")
        self.assertIn("feasibility", first[3])
        self.assertIn("board", first[3])

    def test_every_task_still_appears_exactly_once(self):
        from pipeline.views.hub import ACROSS, PHASES, TASK_DEFS
        placed = [i for _pid, _t, _b, ids, _n in PHASES for i in ids] + list(ACROSS[1])
        self.assertEqual(sorted(placed), sorted(d[0] for d in TASK_DEFS))


class ARefusalNamesAControlThatExistsTests(TestCase):
    """`tick "create targets"` named no control on any page."""

    databases = {"default", DB}

    def test_the_cell_line_refusal_quotes_the_checkbox_label(self):
        rows = bulkcl.parse("name\tgene\tgenotype\nHAP1\tTRPA1\tKO")
        item, = bulkcl.plan(rows, create_targets=False)
        self.assertEqual(item["status"], "blocked")
        self.assertIn("Add the gene as a new target", item["note"])
        self.assertNotIn("create targets", item["note"])

    def test_it_offers_the_way_to_the_target_board(self):
        rows = bulkcl.parse("name\tgene\tgenotype\nHAP1\tTRPA1\tKO")
        item, = bulkcl.plan(rows, create_targets=False)
        self.assertIn("gene=TRPA1", item["add_target_url"])
        self.assertIn("/targets/board/", item["add_target_url"])

    def test_the_antibody_refusal_says_the_same_thing(self):
        from pipeline.services import bulk_antibodies as bulkab
        rows = bulkab.parse("gene\tcatalogue\nTRPA1\tab12345")
        item, = bulkab.plan(rows, create_targets=False)
        self.assertEqual(item["status"], "no-target")
        self.assertIn("Add the gene as a new target", item["note"])
        self.assertIn("gene=TRPA1", item["add_target_url"])

    def test_both_previews_render_it_through_the_shared_file(self):
        from pathlib import Path
        js = Path("pipeline/static/pipeline/board.js").read_text()
        self.assertIn("add_target_url", js)
        # In previewRows, so every surface that previews a paste gets it.
        block = js[js.index("function previewRows("):]
        block = block[:block.index("\n  }")]
        self.assertIn("addTargetLink(it)", block)


class AMissingRowSaysWhichFieldTests(TestCase):
    """One refusal named three possible causes for a row with one problem."""

    databases = {"default", DB}

    def test_a_row_with_no_name_says_so(self):
        item, = bulkcl.plan(bulkcl.parse("name\tgenotype\n\tWT"))
        self.assertEqual(item["status"], "blocked")
        self.assertIn("name", item["note"])
        self.assertNotIn("usually", item["note"])

    def test_a_knockout_with_no_gene_says_so(self):
        item, = bulkcl.plan(bulkcl.parse("name\tgenotype\nHAP1\tKO"))
        self.assertEqual(item["status"], "blocked")
        self.assertIn("gene", item["note"])
