"""What a single-cell save must hand back, and what it must cost.

The board used to refetch every row (plus a full consortium-wide duplicate scan)
after each cell edit. It now redraws one row from the save's own response, so
these pin the three things that makes load-bearing:

  * the recomputed row comes back, so derived values stay truthful
  * ``matches`` says whether the row still belongs under the active filter
  * ``duplicate_sites`` refreshes that gene's badge

Plus the cost ceiling: the rows endpoint must not issue more queries as the
board grows. N+1 is how a page like this dies, and it dies in production on a
full dataset while looking fine on a handful of dev rows.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from pipeline.models import (Member, Report, Site, Target, TargetNomination)

DB = "pipeline_db"


class BoardPatchTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="McGill", short_code="MCG")
        self.site2 = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="carl")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.create(user_id=pu.pk, site_id=self.site.pk, role="admin",
                              is_active=True)
        self.client = Client()
        self.assertTrue(self.client.login(username="carl", password="pw"))
        self.target = Target.objects.create(protein_name="Synuclein", gene_name="SNCA")

    def _patch(self, query="", **body):
        return self.client.post(f"/pipeline/targets/board/patch/?{query}", body)

    def test_the_page_loads_the_shared_board_module(self):
        """Every board's grid, inline editing and error handling live in
        board.js. Drop the tag and the page renders but nothing works."""
        html = self.client.get("/pipeline/targets/board/").content.decode()
        self.assertIn("pipeline/board.js", html)
        self.assertIn("OGABoard.create(", html)

    # ── what comes back ────────────────────────────────────────────────

    def test_save_returns_the_recomputed_row(self):
        response = self._patch(target_id=self.target.pk, field="essential_gene", value="NO")
        self.assertEqual(response.status_code, 200, response.content[:300])
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["matches"])
        self.assertEqual(data["row"]["id"], self.target.pk)
        self.assertEqual(data["row"]["gene"], "SNCA")

    def test_derived_completed_flips_in_the_returned_row(self):
        """The reason the row is recomputed rather than patched client-side."""
        before = self._patch(target_id=self.target.pk, field="f1000_note", value="x")
        self.assertFalse(before.json()["row"]["completed"])
        after = self._patch(target_id=self.target.pk, field="zenodo_doi",
                            value="10.5281/zenodo.1")
        self.assertTrue(after.json()["row"]["completed"])
        self.assertEqual(Report.objects.using(DB).filter(target=self.target).count(), 1)

    def test_row_that_leaves_the_active_filter_reports_matches_false(self):
        nom = TargetNomination.objects.create(target=self.target, site=self.site, funded=True)
        still = self._patch("funded=yes", target_id=self.target.pk, field="comments",
                            value="unchanged by the filter", nomination_id=nom.pk)
        self.assertTrue(still.json()["matches"])

        gone = self._patch("funded=yes", target_id=self.target.pk, field="funded",
                           value="", nomination_id=nom.pk)
        self.assertTrue(gone.json()["ok"])
        self.assertFalse(gone.json()["matches"])
        self.assertIsNone(gone.json()["row"])

    def test_duplicate_sites_reflect_the_gene_across_sites(self):
        n1 = TargetNomination.objects.create(target=self.target, site=self.site)
        TargetNomination.objects.create(target=self.target, site=self.site2)
        data = self._patch(target_id=self.target.pk, field="comments",
                           value="two sites", nomination_id=n1.pk).json()
        self.assertEqual(sorted(data["duplicate_sites"]), ["Leicester", "McGill"])

    def test_single_site_gene_is_not_flagged_duplicate(self):
        nom = TargetNomination.objects.create(target=self.target, site=self.site)
        data = self._patch(target_id=self.target.pk, field="comments",
                           value="one site", nomination_id=nom.pk).json()
        self.assertEqual(data["duplicate_sites"], [])

    # ── what must not come back ────────────────────────────────────────

    def test_a_field_that_is_not_editable_is_named_in_the_error(self):
        response = self._patch(target_id=self.target.pk, field="uniprot_id", value="P37840")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not editable", response.json()["error"])

    def test_an_unexpected_failure_does_not_leak_python_exception_text(self):
        """The message used to be str(e) — raw tracebacks in front of a scientist.

        A ValueError *is* passed through now, because those are refusals this view
        phrases itself ("there is no site called …"). Anything else is a crash and
        still gets the generic sentence, which is what this pins.
        """
        nom = TargetNomination.objects.create(target=self.target, site=self.site)
        with mock.patch("pipeline.views.target_board._strict_site_id",
                        side_effect=RuntimeError("psycopg2 OperationalError: boom")):
            response = self._patch(target_id=self.target.pk, field="site",
                                   value="McGill", nomination_id=nom.pk)
        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertNotIn("psycopg2", error)
        self.assertNotIn("boom", error)
        self.assertIn("left unchanged", error)

    def test_unknown_target_is_a_404_not_a_crash(self):
        response = self._patch(target_id=999999, field="essential_gene", value="NO")
        self.assertEqual(response.status_code, 404)


class TheDoiCellCanBeCorrectedTests(TestCase):
    """A typed DOI is read, and a wrong one can be taken back out.

    ``zenodo_doi`` is a ``URLField``, which validates nothing on ``save()``, and
    this endpoint stored whatever arrived. The thirteenth field test typed
    ``definitely not a doi 12345``: it saved, the gene went **completed**, the gene
    page grew a green **Reported** badge, the text was drawn as a link that
    resolved against our own site — and the cell then stopped being editable, so
    there was no way to correct it from any screen in the app.

    What is pinned is the pair. Refusing a bad value is the cheap half; the half
    that matters is that a value already stored can be **cleared**, that clearing
    it reopens the gene, and that it lands on the ``Report`` row the board is
    actually drawing.
    """
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="McGill", short_code="MCG")
        for alias in ("academy_db", DB):
            u = User(username="carl")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.create(user_id=pu.pk, site_id=self.site.pk, role="admin",
                              is_active=True)
        self.client = Client()
        self.assertTrue(self.client.login(username="carl", password="pw"))
        self.target = Target.objects.create(protein_name="TRP A1", gene_name="TRPA1")

    def _patch(self, **body):
        return self.client.post("/pipeline/targets/board/patch/", body)

    def _reports(self):
        return list(Report.objects.using(DB).filter(target=self.target).order_by("pk"))

    def test_rubbish_is_refused_and_nothing_is_written(self):
        response = self._patch(target_id=self.target.pk, field="zenodo_doi",
                               value="definitely not a doi 12345")
        self.assertEqual(response.status_code, 400)
        self.assertIn("definitely not a doi 12345", response.json()["error"])
        # No Report row at all: a refusal that still creates the record it was
        # refusing is the shape of a gene reading "completed" over nothing.
        self.assertEqual(self._reports(), [])

    def test_a_typed_doi_is_stored_as_an_address(self):
        row = self._patch(target_id=self.target.pk, field="zenodo_doi",
                          value="10.5281/zenodo.16812915").json()["row"]
        self.assertEqual(self._reports()[0].zenodo_doi,
                         "https://doi.org/10.5281/zenodo.16812915")
        # What the board draws: the DOI as text, and an href it can follow.
        self.assertEqual(row["zenodo_text"], "10.5281/zenodo.16812915")
        self.assertEqual(row["zenodo_link"], "https://doi.org/10.5281/zenodo.16812915")
        self.assertTrue(row["completed"])

    def test_clearing_the_cell_empties_it_and_reopens_the_gene(self):
        """The whole finding. Until this, a single typo was permanent."""
        self._patch(target_id=self.target.pk, field="zenodo_doi",
                    value="10.5281/zenodo.1")
        row = self._patch(target_id=self.target.pk, field="zenodo_doi",
                          value="").json()["row"]
        self.assertFalse(row["completed"])
        self.assertEqual(row["zenodo_text"], "")
        self.assertEqual(row["zenodo_link"], "")

    def test_clearing_the_only_value_takes_the_row_with_it(self):
        """The tail of the same finding, and it survived the first fix.

        Typing a DOI onto a gene with no report *creates* the row; clearing the
        DOI stepped the status down and left the row standing. So a corrected
        typo left the gene page reading **REPORTS (1) · Draft — No DOI linked
        yet** about a deposit nobody made, on a gene where Generate Report had
        never been pressed — under the panel's own promise that a draft never
        appears there. The sixteenth field test found it by doing exactly the
        round trip the previous fix invited.
        """
        self._patch(target_id=self.target.pk, field="zenodo_doi",
                    value="10.5281/zenodo.1")
        self.assertEqual(len(self._reports()), 1)
        self._patch(target_id=self.target.pk, field="zenodo_doi", value="")
        self.assertEqual(self._reports(), [],
                         "a row stating nothing is residue, not a record")

    def test_clearing_it_takes_the_published_status_back_down(self):
        """`completed` is derived and flips back on its own; `Report.status` is
        stored and only ever went up — so the gene page kept a green **Published**
        pill over a record with nothing in it.

        Asserted on a row that carries something else, because a row carrying
        *only* the DOI is now removed outright by the test above.
        """
        Report.objects.using(DB).create(target=self.target,
                                        f1000_priority="YES with IF repeat")
        self._patch(target_id=self.target.pk, field="zenodo_doi",
                    value="10.5281/zenodo.1")
        self.assertEqual(self._reports()[0].status, Report.ReportStatus.PUBLISHED)
        self._patch(target_id=self.target.pk, field="zenodo_doi", value="")
        self.assertEqual(self._reports()[0].status, Report.ReportStatus.DRAFT)

    def test_a_row_that_records_anything_else_is_kept(self):
        """The other side of the removal, and the one that would lose data.

        The Access import wrote Carl's F1000 column — "YES with IF repeat",
        "coming" — onto rows carrying no DOI at all, and his file is the only
        copy of some of it. Emptying the Zenodo cell must not take that with it.
        """
        Report.objects.using(DB).create(target=self.target,
                                        f1000_priority="YES with IF repeat")
        self._patch(target_id=self.target.pk, field="zenodo_doi",
                    value="10.5281/zenodo.1")
        self._patch(target_id=self.target.pk, field="zenodo_doi", value="")
        kept = self._reports()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].zenodo_doi, "")
        self.assertEqual(kept[0].f1000_priority, "YES with IF repeat")

    def test_it_edits_the_report_row_the_board_is_showing(self):
        """A target may carry more than one Report. The board drew the first row
        *with a DOI* and this endpoint wrote to the first row *by pk*, so on such a
        gene clearing the cell wrote a blank over a blank and the value stayed on
        screen — "I deleted it and it came back", on the one field somebody is
        trying to correct."""
        empty = Report.objects.using(DB).create(target=self.target)
        holds = Report.objects.using(DB).create(
            target=self.target, zenodo_doi="https://doi.org/10.5281/zenodo.1",
            f1000_priority="YES with IF repeat")
        self.assertLess(empty.pk, holds.pk)

        row = self._patch(target_id=self.target.pk, field="zenodo_doi",
                          value="").json()["row"]
        holds.refresh_from_db(using=DB)
        self.assertEqual(holds.zenodo_doi, "")
        self.assertEqual(row["zenodo_text"], "")
        self.assertFalse(row["completed"])

    def test_a_value_on_file_that_is_not_an_address_is_drawn_as_text(self):
        """Rows predating this hold whatever was typed or imported, and drawing
        one as an `href` gives a relative link — it resolves against this site and
        answers Not Found, which reads as the record being missing."""
        Report.objects.using(DB).create(target=self.target,
                                        zenodo_doi="definitely not a doi 12345")
        response = self.client.get("/pipeline/targets/board/rows/")
        row = next(r for r in response.json()["rows"] if r["id"] == self.target.pk)
        self.assertEqual(row["zenodo_text"], "definitely not a doi 12345")
        self.assertEqual(row["zenodo_link"], "")

    def test_the_gene_page_lists_the_published_record_it_promises(self):
        """The panel's own paragraph says this list is the published record — "a
        Zenodo deposit or an F1000 paper — so a draft never appears here" — and
        it then drew every ``Report`` row on the target, drafts included. One
        screen holding a rule and its own counter-example.

        Split by ``is_completed_report``, the in-memory half of
        ``completed_report_q`` — the app's single definition of that same
        sentence — so the list and the paragraph cannot drift.

        Asserted on the payload rather than on the rendered words: the panel is
        drawn from ``board_row`` by the same renderer every redraw uses, because
        typing an F1000 date has to change the list *and* the cells together. A
        body grep would now match the renderer's own source and pass whatever
        the split did.
        """
        Report.objects.using(DB).create(
            target=self.target, zenodo_doi="https://doi.org/10.5281/zenodo.1")
        draft = Report.objects.using(DB).create(
            target=self.target, f1000_priority="YES with IF repeat")

        resp = self.client.get(f"/pipeline/target/{self.target.pk}/")
        reports = resp.context["board_row"]["reports"]
        self.assertEqual([p["zenodo_text"] for p in reports["published"]],
                         ["10.5281/zenodo.1"])
        # Counted, not silently dropped: the Access import wrote Carl's F1000
        # column onto rows with no DOI, and his file is the only copy of some of
        # it. A record that vanishes from the one page about its gene is how a
        # value nobody can see becomes a value nobody can fix.
        self.assertEqual(reports["unpublished"], 1)
        self.assertTrue(Report.objects.using(DB).filter(pk=draft.pk).exists())

    def test_the_f1000_column_is_read_the_same_way(self):
        """The column beside it, and the same shape of problem — it was drawn as a
        link and could not be edited once set. It does *not* feed `completed`
        (`f1000_date` does), so only the DOI half is asserted here."""
        refused = self._patch(target_id=self.target.pk, field="f1000_doi",
                              value="publish elsewhere")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("F1000 DOI", refused.json()["error"])
        row = self._patch(target_id=self.target.pk, field="f1000_doi",
                          value="10.12688/f1000research.1.2").json()["row"]
        self.assertEqual(row["f1000_text"], "10.12688/f1000research.1.2")
        self.assertEqual(row["f1000_link"],
                         "https://doi.org/10.12688/f1000research.1.2")


class BoardQueryCostTests(TestCase):
    """The rows endpoint must cost the same whether the board holds 3 rows or 30.

    Asserting a fixed number would just churn whenever a legitimate query is
    added. Asserting it does not *grow* with the row count is the property that
    actually matters, and it is what catches an N+1 the moment it appears.
    """
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        site = Site.objects.create(name="McGill", short_code="MCG")
        for alias in ("academy_db", DB):
            u = User(username="carl")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.create(user_id=pu.pk, site_id=site.pk, role="admin", is_active=True)
        self.site = site
        self.client = Client()
        self.assertTrue(self.client.login(username="carl", password="pw"))

    def _seed(self, n, prefix):
        for i in range(n):
            t = Target.objects.create(protein_name=f"P{prefix}{i}", gene_name=f"{prefix}{i}")
            TargetNomination.objects.create(target=t, site=self.site, funded=bool(i % 2))
            Report.objects.create(target=t)

    def _queries_for_rows(self):
        with CaptureQueriesContext(connections[DB]) as ctx:
            response = self.client.get("/pipeline/targets/board/rows/")
            self.assertEqual(response.status_code, 200)
        return len(ctx), response.json()["count"]

    def test_rows_query_count_does_not_grow_with_the_board(self):
        self._seed(3, "AAA")
        small_queries, small_count = self._queries_for_rows()
        self.assertEqual(small_count, 3)

        self._seed(27, "BBB")
        large_queries, large_count = self._queries_for_rows()
        self.assertEqual(large_count, 30)

        self.assertEqual(
            small_queries, large_queries,
            f"rows/ issued {small_queries} queries for 3 targets but "
            f"{large_queries} for 30 — that is an N+1")

    def _queries_for_patch(self, target):
        with CaptureQueriesContext(connections[DB]) as ctx:
            response = self.client.post(
                "/pipeline/targets/board/patch/",
                {"target_id": target.pk, "field": "essential_gene", "value": "NO"})
            self.assertEqual(response.status_code, 200)
        return len(ctx), response.json()

    def test_patch_cost_does_not_grow_with_the_board(self):
        """A cell edit is O(1) in board size.

        Not fewer *queries* than the rows endpoint — that one is already flat —
        but a fixed, tiny amount of work regardless of how many targets exist.
        The old flow paid this cost *and* a full rows redraw on every keystroke-
        sized edit, including a consortium-wide scan of every nomination.
        """
        self._seed(3, "AAA")
        target = Target.objects.using(DB).order_by("pk").first()
        small_queries, _ = self._queries_for_patch(target)

        self._seed(27, "BBB")
        large_queries, _ = self._queries_for_patch(target)

        self.assertEqual(
            small_queries, large_queries,
            f"one cell edit issued {small_queries} queries on a 3-row board but "
            f"{large_queries} on a 30-row board — the save is not O(1)")

    def test_patch_returns_one_row_where_rows_returns_the_whole_board(self):
        self._seed(30, "DDD")
        target = Target.objects.using(DB).order_by("pk").first()
        _, patched = self._queries_for_patch(target)
        board = self.client.get("/pipeline/targets/board/rows/").json()

        self.assertEqual(patched["row"]["id"], target.pk)
        self.assertEqual(board["count"], 30)
        self.assertEqual(len(board["rows"]), 30)


class RecordingAPublicationFromTheGenePageTests(TestCase):
    """The gene's own page can record what the target board records.

    An F1000 paper came out for PKN2, the owner opened the gene's page to write
    it down, and there was nowhere to do it — the Reports panel was a read-only
    list that said "fix it on the target board" twice. These pin the fix at the
    layer both doors share: one patch endpoint, one row payload, and the fields
    the endpoint has always accepted now present in it.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="carl")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.using(DB).create(user_id=pu.pk, site_id=self.site.pk,
                                        role="admin", is_active=True)
        self.client = Client()
        self.assertTrue(self.client.login(username="carl", password="pw"))
        self.target = Target.objects.using(DB).create(gene_name="PKN2")

    def _page(self):
        return self.client.get(f"/pipeline/target/{self.target.pk}/")

    def _patch(self, **body):
        return self.client.post("/pipeline/targets/board/patch/", body)

    def test_the_gene_page_posts_to_the_boards_own_writer(self):
        """Not a second editor. A second one would be a second set of rules
        about what a Zenodo DOI is, and this repo has thirteen field tests'
        worth of evidence about what two doors that disagree cost."""
        html = self._page().content.decode()
        self.assertIn("pipeline/board.js", html)
        self.assertIn("/pipeline/targets/board/patch/", html)
        # Both panels mount, and both are drawn from the payload the view put in
        # `board-row` — a grid with no renderer is a panel that stays empty.
        self.assertIn('id="funding-grid"', html)
        self.assertIn('id="publication-grid"', html)
        self.assertIn('id="board-row"', html)

    def test_the_payload_carries_every_field_the_endpoint_will_write(self):
        """A field the write path accepts and no screen draws is reachable only
        through a spreadsheet — which is how `essential_gene`, both dates and
        the two F1000 notes stood for as long as the board has existed."""
        row = self._page().context["board_row"]
        for field in ("essential_gene", "zenodo_doi", "zenodo_date", "f1000_doi",
                      "f1000_date", "f1000_priority", "f1000_note",
                      "class_rows", "nominations", "reports"):
            self.assertIn(field, row)

    def test_an_f1000_date_is_what_marks_the_gene_completed(self):
        """A DOI alone does not: a paper carries one before it is accepted, and
        `completed_report_q` asks for the *date*. Recording the publication from
        this page therefore has to reach a field the board never drew."""
        after_doi = self._patch(target_id=self.target.pk, field="f1000_doi",
                                value="10.12688/f1000research.185471.1").json()["row"]
        self.assertEqual(after_doi["f1000_text"], "10.12688/f1000research.185471.1")
        self.assertIs(after_doi["completed"], False)

        after_date = self._patch(target_id=self.target.pk, field="f1000_date",
                                 value="2026-08-05").json()["row"]
        self.assertIs(after_date["completed"], True)
        self.assertEqual(after_date["f1000_date"], "2026-08-05")
        # …and it moves into the published list the panel draws, rather than
        # being counted under it as something not listed.
        self.assertEqual(after_date["reports"]["unpublished"], 0)
        self.assertEqual([p["f1000_date"] for p in after_date["reports"]["published"]],
                         ["2026-08-05"])

    def test_a_date_that_cannot_be_read_is_refused_by_name(self):
        """`target_list_io.parse_date` answers None for anything it cannot read,
        which is right for a 585-row workbook and exactly wrong for a box
        somebody opened and typed into: nothing was written, the cell redrew
        empty, and a save that stored nothing looked like one that worked."""
        Report.objects.using(DB).create(target=self.target,
                                        f1000_date="2026-01-01")
        resp = self._patch(target_id=self.target.pk, field="f1000_date",
                           value="last tuesday")
        self.assertEqual(resp.status_code, 400)
        error = resp.json()["error"]
        self.assertIn("F1000 publication date", error)
        self.assertIn("2026-08-06", error, "a refusal names the format it takes")
        # Nothing written — the value on file is the one that was there.
        self.assertEqual(
            str(Report.objects.using(DB).get(target=self.target).f1000_date),
            "2026-01-01")

    def test_clearing_a_date_is_still_how_a_mistake_is_corrected(self):
        self._patch(target_id=self.target.pk, field="f1000_date", value="2026-08-05")
        row = self._patch(target_id=self.target.pk, field="f1000_date",
                          value="").json()["row"]
        self.assertEqual(row["f1000_date"], "")
        self.assertIs(row["completed"], False)

    def test_the_two_readings_of_completed_agree(self):
        """`completed_report_q` is the app's one definition of "done" and
        `is_completed_report` is the same sentence asked in memory. Two answers
        to "how many have we finished" is the defect this file records three
        surfaces getting wrong; a third reading written by hand would be a
        fourth."""
        from pipeline.services import target_board as board

        rows = [
            Report(target_id=self.target.pk),
            Report(target_id=self.target.pk, zenodo_doi="https://doi.org/10.5281/zenodo.1"),
            Report(target_id=self.target.pk, f1000_doi="https://doi.org/10.12688/x"),
            Report(target_id=self.target.pk, f1000_date="2026-08-05"),
        ]
        for r in rows:
            r.save(using=DB)
        by_query = set(Report.objects.using(DB)
                       .filter(board.completed_report_q(), target_id=self.target.pk)
                       .values_list("pk", flat=True))
        by_python = {r.pk for r in
                     Report.objects.using(DB).filter(target_id=self.target.pk)
                     if board.is_completed_report(r)}
        self.assertEqual(by_query, by_python)
        # And it is the DOI/date split, not "any report row".
        self.assertEqual(len(by_query), 2)

    def test_a_protein_class_says_whether_it_can_be_taken_off(self):
        """`remove_class` only ever deletes the hand-added row, so a × on a
        UniProt-derived label would be a control that does nothing, silently."""
        from pipeline.models import TargetClassification as TC

        TC.objects.using(DB).create(target_id=self.target.pk, label="Kinase",
                                    source=TC.Source.UNIPROT)
        TC.objects.using(DB).create(target_id=self.target.pk, label="Kinase",
                                    source=TC.Source.MANUAL)
        TC.objects.using(DB).create(target_id=self.target.pk, label="PKN family",
                                    source=TC.Source.FAMILY)

        rows = {c["label"]: c for c in self._page().context["board_row"]["class_rows"]}
        # One entry per label even where two sources agree — that is one finding
        # to a reader, not two.
        self.assertEqual(sorted(rows), ["Kinase", "PKN family"])
        self.assertIs(rows["Kinase"]["removable"], True)
        self.assertIs(rows["PKN family"]["removable"], False)
        self.assertIn("derived from", rows["PKN family"]["from"])
