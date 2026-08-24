"""Getting around the app: one search box, one gene, four boards.

The second field test measured this and it was the app's biggest structural cost.
Finding a gene from a cold start was four clicks *and* required knowing which of
four boards to go to first, because there was no search box anywhere in the
chrome. Seeing everything about one gene meant visiting four boards and typing
the same gene name into four different filter boxes — around seventeen
interactions for one question.

What is pinned here is the shape of the fix rather than its styling: every board
takes the same ``?gene=``, that filter survives into the grid, one search box
resolves what someone typed, and the gene's own page says where the gene has got
to using evidence that already exists.
"""
from __future__ import annotations

import re

from django.db import connections
from pathlib import Path

from django.conf import settings
from unittest import mock

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             Member, Report, Site, Target, TargetNomination)
from pipeline.tests_timeouts import DB, _member_client, no_network


class EveryBoardTakesTheSameGeneTests(TestCase):
    """``?gene=STMN2`` means the same thing on all four boards.

    Three of them took it; the sessions board took only the fuzzy ``q``, so
    "STMN2's sessions" was the one question of its kind that could not be asked
    exactly — and a gene could not be carried there from anywhere else.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.stmn2 = Target.objects.using(DB).create(gene_name="STMN2")
        self.elp3 = Target.objects.using(DB).create(gene_name="ELP3")
        company = Company.objects.using(DB).create(name="Proteintech")
        for target, cat in ((self.stmn2, "10586-1-AP"), (self.elp3, "24523-1-AP")):
            Antibody.objects.using(DB).create(
                target_id=target.pk, company_id=company.pk,
                catalogue_number=cat, site_id=self.site.pk)
            CellLine.objects.using(DB).create(
                name=f"HAP1 {target.gene_name} KO", genotype="KO",
                target_id=target.pk, site_id=self.site.pk)
            ExperimentSession.objects.using(DB).create(
                target_id=target.pk, procedure_type="WB", date="2026-07-30",
                site_id=self.site.pk, experimenter_id=self.member.pk)

    ROWS = {
        "targets": "/pipeline/targets/board/rows/",
        "antibodies": "/pipeline/antibodies/board/rows/",
        "cell lines": "/pipeline/cell-lines/board/rows/",
        "sessions": "/pipeline/sessions/board/rows/",
    }

    def test_every_board_narrows_to_one_gene(self):
        for name, url in self.ROWS.items():
            with self.subTest(board=name):
                resp = self.client.get(url, {"gene": "STMN2"})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["count"], 1,
                                 f"the {name} board ignored ?gene=")

    def test_the_gene_filter_is_exact_not_a_substring(self):
        """A gene filter set by a link must not sweep in its neighbours: STMN1
        and STMN2 are different genes, and `q` is the box for guessing."""
        Target.objects.using(DB).create(gene_name="STMN1")
        resp = self.client.get(self.ROWS["targets"], {"gene": "STMN"})
        self.assertEqual(resp.json()["count"], 0)

    def test_the_filter_is_a_field_on_the_form_so_the_grid_keeps_it(self):
        """The grid builds its query from the filter form, so a filter that is
        only in the URL is dropped the moment the rows load — the page would look
        filtered and the data would not be."""
        for url in ("/pipeline/targets/board/", "/pipeline/antibodies/board/",
                    "/pipeline/cell-lines/board/", "/pipeline/sessions/board/"):
            with self.subTest(url=url):
                body = self.client.get(url, {"gene": "STMN2"}).content.decode()
                self.assertIn('name="gene"', body)
                self.assertIn('value="STMN2"', body)

    def test_the_browse_menu_carries_the_gene_between_boards(self):
        body = self.client.get("/pipeline/antibodies/board/",
                               {"gene": "STMN2"}).content.decode()
        for path in ("/pipeline/targets/board/", "/pipeline/cell-lines/board/",
                     "/pipeline/sessions/board/"):
            self.assertIn(f'href="{path}?gene=STMN2"', body)

    def test_the_browse_menu_is_plain_when_no_gene_is_in_play(self):
        body = self.client.get("/pipeline/antibodies/board/").content.decode()
        self.assertIn('href="/pipeline/cell-lines/board/"', body)
        self.assertNotIn("?gene=", body)

    # ---- and it takes a list of them ------------------------------------
    #
    # A bulk add of targets lands on this board narrowed to the genes it just
    # added, which is a filter naming several genes at once. It has to mean the
    # same thing on all four for the same reason one gene does: the Browse menu
    # carries whatever is in the box to the next board, so a list one board
    # understood and another did not would leave a page looking filtered with
    # the filter silently dropped.

    def test_every_board_narrows_to_a_list_of_genes(self):
        for name, url in self.ROWS.items():
            with self.subTest(board=name):
                resp = self.client.get(url, {"gene": "STMN2,ELP3"})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["count"], 2,
                                 f"the {name} board did not take a gene list")

    def test_a_list_is_still_exact_about_each_gene(self):
        """Widening to a list must not widen what one term matches."""
        Target.objects.using(DB).create(gene_name="STMN1")
        resp = self.client.get(self.ROWS["targets"], {"gene": "STMN, ELP"})
        self.assertEqual(resp.json()["count"], 0)

    def test_a_list_may_be_spaced_the_way_a_person_reads_it(self):
        """`?gene=STMN2,ELP3` is what a link carries; `STMN2, ELP3` with the
        space is what somebody types back into the box after reading it."""
        resp = self.client.get(self.ROWS["targets"], {"gene": "STMN2, ELP3"})
        self.assertEqual(resp.json()["count"], 2)

    def test_a_list_carries_between_boards_whole(self):
        body = self.client.get("/pipeline/antibodies/board/",
                               {"gene": "STMN2,ELP3"}).content.decode()
        self.assertIn("/pipeline/targets/board/?gene=STMN2%2CELP3", body)

    def test_the_wild_types_can_be_asked_for_alongside_a_gene(self):
        """`NA` means the wild types on the cell-lines board — the one term that
        is not a gene — so a list has to be built term by term rather than
        handed to a gene matcher whole. Without it, `?gene=NA,STMN2` would
        quietly drop the half that a gene filter structurally cannot find."""
        CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                          site_id=self.site.pk)
        rows = self.client.get(self.ROWS["cell lines"], {"gene": "NA,STMN2"})
        self.assertEqual(rows.json()["count"], 2)

    def test_na_means_no_gene_on_the_antibodies_board_too(self):
        """`?gene=` means the same thing on all four boards, and `NA` is part of
        what it means.

        `Antibody.target` is **not** nullable, so an antibody with no gene is one
        whose target carries a blank ``gene_name`` — 8 of them on live, drawn
        with an empty GENE cell and reachable by no filter at all. The
        thirteenth field test typed the word the cell-lines board's own hint
        teaches for exactly that cell and got nothing back, which from outside is
        a search box that does not work rather than one narrower than its
        neighbour.
        """
        nameless = Target.objects.using(DB).create(gene_name="")
        Antibody.objects.using(DB).create(target_id=nameless.pk,
                                          catalogue_number="orphan-1",
                                          site_id=self.site.pk)
        rows = self.client.get(self.ROWS["antibodies"], {"gene": "NA"})
        self.assertEqual(rows.json()["count"], 1)
        # And it composes with a gene, the same way it does one board over.
        both = self.client.get(self.ROWS["antibodies"], {"gene": "NA,STMN2"})
        self.assertEqual(both.json()["count"], 2)

    def test_the_antibodies_board_says_na_is_available(self):
        """A filter nobody is told about is a filter nobody uses — the cell-lines
        board has said so in its hint for several runs."""
        body = self.client.get("/pipeline/antibodies/board/").content.decode()
        self.assertIn("NA", body)
        self.assertIn("no gene recorded", body)

    def test_a_progress_strip_needs_a_single_gene(self):
        """The strip names "the next step", which is a single thing or nothing.
        A two-gene filter is a board, not a gene, so it gets no strip rather
        than one about whichever gene sorted first."""
        one = self.client.get("/pipeline/targets/board/", {"gene": "STMN2"})
        self.assertEqual(one.context["next_step_gene"], "STMN2")
        both = self.client.get("/pipeline/targets/board/", {"gene": "STMN2,ELP3"})
        self.assertNotIn("next_step_gene", both.context)


class AddingGenesLandsSomewhereTests(TestCase):
    """A save of genes says where they went by *going* there.

    Owner's ask. Adding targets used to end where every other paste ends — on
    the panel you pressed, with a green line — and the one link offered went to
    the whole board: 585 rows with yours somewhere in them, which is a receipt
    rather than a place. A bulk add lands on the targets board **filtered to the
    genes it just added**; the single-gene Add on the feasibility page lands on
    that gene's own page, because that door was about one gene from the start.

    The two halves are pinned in different places, because they fail
    differently: the destination *arithmetic* is `bulk_targets.apply`'s
    ``landed`` (see `services/tests/test_bulk_targets.py`), and what is pinned
    here is that the URL board.js builds by hand is the URL Django serves.
    """

    databases = {"pipeline_db", "academy_db"}

    def _board_js(self):
        return (Path(settings.BASE_DIR) / "pipeline" / "static" / "pipeline"
                / "board.js").read_text()

    def test_the_path_board_js_writes_is_the_path_django_reverses(self):
        """board.js is a static file, so it cannot use `{% url %}` and every
        board template already writes these paths as literals. A literal that has
        drifted from its route is a 404 at the end of a successful save, which
        reads as the save having failed — and nothing else in the suite would
        notice, because the route still resolves for everybody who uses the tag.
        """
        self.assertIn(f"{reverse('pipeline:target_board')}?gene=",
                      self._board_js())

    def test_the_gene_links_are_the_path_django_reverses_too(self):
        """The same literal, in the three grids that link a gene name."""
        stem = reverse("pipeline:target_detail", args=[7]).replace("7/", "")
        root = Path(settings.BASE_DIR) / "pipeline" / "templates" / "pipeline"
        for name in ("target_board.html", "antibody_board.html",
                     "cell_line_board.html"):
            with self.subTest(template=name):
                self.assertIn(f'href="{stem}$', (root / name).read_text(),
                              f"{name}'s gene link is not {stem}<id>/")

    def test_both_doors_send_you_to_the_same_place(self):
        """Every other thing these two doors share has been wrong on one of
        them at some point — the count on the button, the refusal under it,
        whether a nomination is a record. One function, called by both."""
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        client = _member_client(self, site)
        for url in ("/pipeline/targets/board/", "/pipeline/feasibility/"):
            with self.subTest(url=url):
                html = client.get(url).content.decode()
                self.assertIn("OGABoard.targetAddGo", html)
        js = self._board_js()
        self.assertIn("function targetAddGo(", js)
        self.assertIn("targetAddGo,", js, "it is not exported")

    def test_it_only_moves_the_page_when_there_is_nothing_left_to_read(self):
        """`CLAUDE.md`: a page must not reload itself out from under its own
        result line, because that line is often the only place a *dropped* value
        is named. Navigating is that reload wearing a different hat, so the
        quiet check gates it — and it is the one thing here a reader cannot see
        was missing until a gene has already gone unreported."""
        js = self._board_js()
        block = js[js.index("function targetAddGo("):]
        block = block[:block.index("\n  }")]
        self.assertIn("dest.quiet", block,
                      "targetAddGo navigates without asking whether the result "
                      "still has something to say")
        arithmetic = js[js.index("function targetAddDestination("):]
        arithmetic = arithmetic[:arithmetic.index("\n  }")]
        for key in ("not_found", "errors", "skipped", "funding_filled"):
            self.assertIn(key, arithmetic,
                          f"a result carrying {key} would be navigated away from")

    def test_the_single_gene_door_checks_it_actually_created_something(self):
        """`/feasibility/add/` answers with the id of a gene that was **already**
        on the list too, and being carried off to it would read as having just
        added it."""
        html = (_member_client(self, Site.objects.using(DB).create(
            name="McGill", short_code="MCG"))
            .get("/pipeline/feasibility/").content.decode())
        self.assertIn("result.created && result.target_id", html)


class AGeneNameIsAWayToItsPageTests(TestCase):
    """Clicking a gene's name goes to that gene's page, wherever it is printed.

    The targets board's first column always did. The antibodies and cell-lines
    boards printed the gene as plain text — a word you could not act on, in the
    column most likely to be what you are actually following — and the progress
    strip above all four boards named the gene it was about without offering it.

    The sessions board is the deliberate exception: its first column is the gene
    and clicking it opens that session's results, which is the row's own job.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def test_the_antibodies_row_carries_the_id_to_link_with(self):
        company = Company.objects.using(DB).create(name="Proteintech")
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="10586-1-AP", site_id=self.site.pk)
        row = self.client.get("/pipeline/antibodies/board/rows/").json()["rows"][0]
        self.assertEqual(row["gene"], "STMN2")
        self.assertEqual(row["target_id"], self.target.pk)

    def test_the_cell_lines_row_carries_the_id_to_link_with(self):
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        row = self.client.get("/pipeline/cell-lines/board/rows/").json()["rows"][0]
        self.assertEqual(row["gene"], "STMN2")
        self.assertEqual(row["target_id"], self.target.pk)

    def test_a_wild_type_has_no_gene_and_so_no_link(self):
        """A wild type has no gene, and the 96 attached to the Access-era `NA`
        placeholder have a target_id and still no gene. Either would be a link
        to a page about nothing, so the template asks for both."""
        CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                          site_id=self.site.pk)
        row = self.client.get("/pipeline/cell-lines/board/rows/").json()["rows"][0]
        self.assertEqual(row["gene"], "")
        html = self.client.get("/pipeline/cell-lines/board/").content.decode()
        self.assertIn("r.gene && r.target_id", html,
                      "the gene cell links without checking there is a gene")

    def test_the_progress_strip_offers_the_page_it_is_about(self):
        for url in ("/pipeline/targets/board/", "/pipeline/antibodies/board/",
                    "/pipeline/cell-lines/board/", "/pipeline/sessions/board/"):
            with self.subTest(url=url):
                html = self.client.get(url, {"gene": "STMN2"}).content.decode()
                self.assertIn(
                    f'href="{reverse("pipeline:target_detail", args=[self.target.pk])}"',
                    html, f"{url}'s progress strip names STMN2 without offering it")


class OneSearchBoxTests(TestCase):
    """There was no search box anywhere in the app chrome."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.stmn2 = Target.objects.using(DB).create(
            gene_name="STMN2", protein_name="Stathmin 2")
        company = Company.objects.using(DB).create(name="Proteintech")
        self.antibody = Antibody.objects.using(DB).create(
            target_id=self.stmn2.pk, company_id=company.pk,
            catalogue_number="10586-1-AP", site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.stmn2.pk,
            site_id=self.site.pk)

    def test_the_box_is_on_every_page(self):
        for url in ("/pipeline/start/", "/pipeline/targets/board/",
                    "/pipeline/cell-lines/board/", "/pipeline/sessions/board/"):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn('action="/pipeline/find/"', body)

    def test_a_gene_name_goes_straight_to_that_genes_page(self):
        resp = self.client.get("/pipeline/find/", {"q": "stmn2"})
        self.assertRedirects(resp, f"/pipeline/target/{self.stmn2.pk}/")

    def test_a_catalogue_number_reports_where_it_appears(self):
        resp = self.client.get("/pipeline/find/", {"q": "10586-1-AP"})
        self.assertEqual(resp.status_code, 200)
        groups = {g["key"]: g for g in resp.context["results"]["groups"]}
        self.assertIn("antibodies", groups)
        self.assertEqual(groups["antibodies"]["count"], 1)
        self.assertEqual(groups["antibodies"]["rows"][0]["title"], "10586-1-AP")

    def test_a_partial_gene_is_reported_not_jumped_to(self):
        """`STMN` is not a gene. Redirecting to STMN2 for it would be a guess."""
        resp = self.client.get("/pipeline/find/", {"q": "STMN"})
        self.assertEqual(resp.status_code, 200)
        groups = {g["key"]: g for g in resp.context["results"]["groups"]}
        self.assertEqual(groups["targets"]["count"], 1)
        self.assertEqual(groups["cell_lines"]["count"], 1)

    def test_an_antibody_result_links_to_the_board_not_the_retired_page(self):
        resp = self.client.get("/pipeline/find/", {"q": "10586-1-AP"})
        groups = {g["key"]: g for g in resp.context["results"]["groups"]}
        url = groups["antibodies"]["rows"][0]["url"]
        self.assertIn("/pipeline/antibodies/board/", url)
        self.assertNotIn("/pipeline/antibody/", url)

    def test_nothing_found_offers_the_way_to_start_the_gene(self):
        resp = self.client.get("/pipeline/find/", {"q": "NOTAGENE"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["results"]["total"], 0)
        body = resp.content.decode()
        self.assertIn("Nothing matches", body)
        self.assertIn("/pipeline/feasibility/?gene=NOTAGENE", body)

    def test_an_empty_search_does_not_explode(self):
        resp = self.client.get("/pipeline/find/", {"q": "  "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["results"]["total"], 0)

    def test_searching_makes_no_outbound_call(self):
        """Same rule the boards are held to: this sits in the chrome and runs on
        every passing curiosity, so a slow UniProt or SciCrunch must never be in
        its path — a search box that hangs is worse than no search box."""
        with no_network("resolving a search"):
            resp = self.client.get("/pipeline/find/", {"q": "10586-1-AP"})
        self.assertEqual(resp.status_code, 200)


class WhereThisGeneHasGotToTests(TestCase):
    """The gene page gathers everything about a gene and could not say what was
    missing. The facts all existed; nothing put them in order."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def _steps(self):
        resp = self.client.get(f"/pipeline/target/{self.target.pk}/")
        self.assertEqual(resp.status_code, 200)
        return resp, {s["key"]: s for s in resp.context["progress_steps"]}

    def test_a_bare_target_has_nothing_done_and_names_the_first_step(self):
        resp, steps = self._steps()
        self.assertFalse(any(s["done"] for s in steps.values()))
        self.assertEqual(resp.context["next_step"]["key"], "nominated")

    def test_each_step_turns_on_from_a_real_record(self):
        _, steps = self._steps()
        self.assertFalse(steps["nominated"]["done"])

        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk)
        _, steps = self._steps()
        self.assertTrue(steps["nominated"]["done"])
        self.assertEqual(steps["nominated"]["detail"], "Leicester")

        ko = CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        _, steps = self._steps()
        self.assertTrue(steps["ko_line"]["done"])
        self.assertFalse(steps["ko_validated"]["done"])

        ko.ko_validated = True
        ko.save(using=DB, update_fields=["ko_validated"])
        _, steps = self._steps()
        self.assertTrue(steps["ko_validated"]["done"])

        company = Company.objects.using(DB).create(name="Proteintech")
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="10586-1-AP", site_id=self.site.pk)
        _, steps = self._steps()
        self.assertTrue(steps["antibodies"]["done"])

    def test_an_application_is_done_because_a_session_exists(self):
        ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.site.pk, experimenter_id=self.member.pk)
        _, steps = self._steps()
        self.assertTrue(steps["app_WB"]["done"])
        self.assertEqual(steps["app_WB"]["detail"], "Leicester")
        self.assertFalse(steps["app_IP"]["done"])

    def test_a_missing_application_stops_being_the_next_step_once_one_is_run(self):
        """Many targets never need all four. Once WB is done, the absent IP is a
        choice, and nagging about it would call finished work incomplete."""
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk)
        ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.site.pk, experimenter_id=self.member.pk)
        resp, _ = self._steps()
        nxt = resp.context["next_step"]
        self.assertFalse(nxt["key"].startswith("app_"),
                         f"pointed at {nxt['key']} when WB was already run")

    def test_reported_comes_from_a_report_never_a_typed_status(self):
        _, steps = self._steps()
        self.assertFalse(steps["reported"]["done"])
        self.target.status = "published"
        self.target.save(using=DB, update_fields=["status"])
        _, steps = self._steps()
        self.assertFalse(steps["reported"]["done"],
                         "a typed status must not stand in for a report")
        Report.objects.using(DB).create(
            target_id=self.target.pk, zenodo_doi="10.5281/zenodo.1")
        _, steps = self._steps()
        self.assertTrue(steps["reported"]["done"])

    def test_a_step_that_is_not_done_says_what_to_do_about_it(self):
        _, steps = self._steps()
        for key, s in steps.items():
            with self.subTest(step=key):
                self.assertTrue(s["hint"], f"{key} has no hint")
                self.assertTrue(s["url"], f"{key} goes nowhere")

    def test_the_strip_costs_the_same_however_much_data_is_behind_it(self):
        """It renders on a page a curator opens constantly, and N+1 is how these
        pages die — on the real dataset, while looking fine on a handful of dev
        rows. Pinned as "does not grow", not as a fixed number, so a legitimate
        extra step does not fail it.
        """
        from pipeline.services import gene_progress
        company = Company.objects.using(DB).create(name="Proteintech")

        def _add(n):
            for i in range(n):
                Antibody.objects.using(DB).create(
                    target_id=self.target.pk, company_id=company.pk,
                    catalogue_number=f"CAT-{i}-{n}", site_id=self.site.pk)
                CellLine.objects.using(DB).create(
                    name=f"line-{i}-{n}", genotype="KO",
                    target_id=self.target.pk, site_id=self.site.pk)
                ExperimentSession.objects.using(DB).create(
                    target_id=self.target.pk, procedure_type="WB",
                    date="2026-07-30", site_id=self.site.pk,
                    experimenter_id=self.member.pk)

        _add(1)
        with CaptureQueriesContext(connections[DB]) as few:
            gene_progress.steps_for(self.target)
        _add(20)
        with CaptureQueriesContext(connections[DB]) as many:
            gene_progress.steps_for(self.target)
        self.assertEqual(len(many), len(few),
                         "the progress strip queries per record, not per step")


class TheHubPointsAtTheBoardsTests(TestCase):
    """The nav's Browse menu listed the four boards; the hub's own browse row
    listed the three pages they replaced. The front door and the nav bar
    disagreed about where the app was, and the front door won."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def test_the_hub_does_not_send_anyone_to_a_retiring_page(self):
        body = self.client.get("/pipeline/start/").content.decode()
        for legacy in ('href="/pipeline/antibodies/"',
                       'href="/pipeline/cell-lines/"',
                       'href="/pipeline/sessions/"'):
            self.assertNotIn(legacy, body, f"the hub still links to {legacy}")

    def test_the_hub_browse_row_lists_the_four_boards(self):
        """Scoped to the browse row itself. Checking the whole page passes
        vacuously — the task tiles link to all four boards anyway, which is how
        the browse row went on pointing at the retiring pages unnoticed."""
        body = self.client.get("/pipeline/start/").content.decode()
        self.assertIn("Just looking?", body)
        row = body.split("Just looking?", 1)[1].split("</div>", 1)[0]
        for board in ("/pipeline/targets/board/", "/pipeline/antibodies/board/",
                      "/pipeline/cell-lines/board/", "/pipeline/sessions/board/"):
            self.assertIn(f'href="{board}"', row)


class AccessEraTargetsAreNotOrphanedTests(TestCase):
    """Reading nominations must not throw away the import's own record.

    ``Target.site`` is dead for writes, but for the targets imported from Access
    it is the *only* record of whose they are — TargetNomination did not exist
    when they were created. Overview shows McGill with 152 active targets and
    zero antibodies, so every one of those 152 is counted through that field
    alone. Switching those reads to nominations without a fallback would have
    emptied McGill's column overnight and read as data loss.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.leicester = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.mcgill = Site.objects.using(DB).create(
            name="McGill", short_code="MCG", is_active=True)
        self.client = _member_client(self, self.leicester)
        # An Access-era target: a legacy site FK, no nomination, no antibody.
        self.imported = Target.objects.using(DB).create(
            gene_name="ABHD2", status="in_progress", site_id=self.mcgill.pk)

    def test_overview_still_counts_a_target_that_only_the_import_placed(self):
        resp = self.client.get("/pipeline/overview/")
        summaries = {s["site"].name: s for s in resp.context["site_summaries"]}
        self.assertIn("McGill", summaries)
        self.assertEqual(summaries["McGill"]["active_targets"], 1)
        self.assertEqual(summaries["McGill"]["antibodies"], 0)

    def test_the_overview_site_column_falls_back_to_the_import(self):
        resp = self.client.get("/pipeline/overview/")
        codes = {t.gene_name: t.site_codes for t in resp.context["active_targets"]}
        self.assertEqual(codes["ABHD2"], "MCG")

    def test_the_gene_page_says_the_site_and_where_it_came_from(self):
        resp = self.client.get(f"/pipeline/target/{self.imported.pk}/")
        self.assertEqual(resp.context["legacy_site"], "McGill")
        body = resp.content.decode()
        self.assertIn("McGill", body)
        self.assertIn("from the original import", body)
        self.assertNotIn("not nominated by any site yet", body)

    def test_the_first_progress_step_is_not_called_undone(self):
        resp = self.client.get(f"/pipeline/target/{self.imported.pk}/")
        steps = {s["key"]: s for s in resp.context["progress_steps"]}
        self.assertTrue(steps["nominated"]["done"],
                        "an imported target was reported as nobody's")
        self.assertIn("McGill", steps["nominated"]["detail"])
        self.assertIn("import", steps["nominated"]["detail"])

    def test_a_real_nomination_wins_over_the_legacy_field(self):
        TargetNomination.objects.using(DB).create(
            target_id=self.imported.pk, site_id=self.leicester.pk)
        resp = self.client.get(f"/pipeline/target/{self.imported.pk}/")
        self.assertEqual(resp.context["nominated_sites"], ["Leicester"])
        self.assertEqual(resp.context["legacy_site"], "")

    # -- and the *other* screens must say the same thing ---------------------
    #
    # The fallback above was right and was in one screen only. The thirteenth
    # field test opened Overview, read MCG against ABCA7, opened the same gene on
    # the target board and found the Sites cell empty offering "set site" — then
    # checked all 348 genes in the list and found 192 doing it. Two screens, one
    # question, two answers, and the board's answer invites somebody to fill in
    # sites that are already recorded.

    def test_the_target_board_names_the_same_site_overview_does(self):
        from pipeline.services import target_board as board

        overview = self.client.get("/pipeline/overview/")
        codes = {t.gene_name: t.site_codes for t in overview.context["active_targets"]}
        row = next(r for r in board.board_rows() if r["gene"] == "ABHD2")
        self.assertEqual(row["sites"], ["McGill"],
                         f"Overview says {codes['ABHD2']} and the board says "
                         f"{row['sites']} about the same gene")
        self.assertTrue(row["sites_from_import"],
                        "the board must say a site came from the import — a "
                        "nomination has a funder and a date behind it and this "
                        "does not")

    def test_the_board_site_filter_finds_a_target_only_the_import_placed(self):
        from pipeline.services import target_board as board

        for value in (str(self.mcgill.pk), "McGill", "MCG"):
            genes = [r["gene"] for r in board.board_rows(site=value)]
            self.assertIn("ABHD2", genes,
                          f"?site={value} missed the import's own 508 targets")
        self.assertNotIn(
            "ABHD2", [r["gene"] for r in board.board_rows(site=str(self.leicester.pk))])

    def test_no_site_recorded_means_no_site_anywhere(self):
        """`?site=none` is what the Portfolio's caveat links to, so it must not
        hand back 192 rows whose site is on file."""
        from pipeline.services import target_board as board

        nobodys = Target.objects.using(DB).create(gene_name="TRPA1")
        genes = [r["gene"] for r in board.board_rows(site="none")]
        self.assertEqual(genes, ["TRPA1"])
        self.assertEqual(nobodys.gene_name, "TRPA1")

    def test_the_portfolio_counts_it_rather_than_calling_it_missing(self):
        """These totals were *wrong*, not incomplete — the worse of the two, and
        indistinguishable from outside."""
        from pipeline.services.target_board import portfolio

        p = portfolio()
        self.assertEqual(p["by_site"]["McGill"]["total"], 1)
        self.assertEqual(p["by_site"]["McGill"]["from_import"], 1)
        self.assertEqual(p["coverage"]["site_missing"], 0)
        self.assertEqual(p["coverage"]["site_from_import"], 1)
        # Funded is deliberately not raised to match: an import row carries no
        # funder, and a grant figure nobody can decompose is one nobody can defend.
        self.assertEqual(p["by_site"]["McGill"]["funded"], 0)


class OneQuestionOneNumberTests(TestCase):
    """"How many have we finished" had three answers, one per screen.

    Overview's headline tile counted ``Target.status == 'published'`` (134 on
    live), its stage chart counted that *and* "has any Report row" as two
    separate bars (34 + 134), and the target board and the Portfolio counted a
    Zenodo DOI or a published F1000 date (164). All three were internally
    consistent; none of them agreed with the others, and a coordinator asked for
    the number gets a different one depending on which page they are standing on.

    ``target_board.completed_subquery`` is the definition — the owner's, and the
    only derived one. Safe to prefer because ``status='published'`` is a strict
    *subset* of it on the real data: of the 508 rows in
    ``access_csvs/Proteins.csv``, all 134 marked "Report complete" also carry a
    DOI or an F1000 date, so nothing is orphaned by dropping the typed read —
    unlike ``_active``, where the typed statuses are the only record for 152 of
    McGill's targets and had to be kept.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        from datetime import date

        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)

        def target(gene, **kw):
            return Target.objects.using(DB).create(gene_name=gene, **kw)

        # One target per bucket the chart draws, plus the two typed side tallies.
        self.untouched = target("AAAA")                       # feasibility
        self.reagents = target("BBBB")                        # reagents only
        self.running = target("CCCC")                         # experimenting
        self.drafting = target("DDDD")                        # report, no DOI
        self.zenodo = target("EEEE")                          # reported
        self.f1000 = target("FFFF", status="published")       # reported, typed too
        self.held = target("GGGG", status="on_hold")
        self.dropped = target("HHHH", status="cancelled")
        # The bucket that used to double-count: a session that is not a western
        # blot and no antibodies at all. Feasibility asked only about `wb_count`,
        # so this landed in Feasibility *and* in Experimenting.
        self.ip_only = target("IIII")

        Antibody.objects.using(DB).create(
            target_id=self.reagents.pk, catalogue_number="ab1", site_id=self.site.pk)
        Antibody.objects.using(DB).create(
            target_id=self.running.pk, catalogue_number="ab2", site_id=self.site.pk)
        for tgt, proc in ((self.running, "WB"), (self.ip_only, "IP")):
            ExperimentSession.objects.using(DB).create(
                target_id=tgt.pk, site_id=self.site.pk, procedure_type=proc,
                date=date(2026, 8, 1), experimenter_id=self.member.pk)
        Report.objects.using(DB).create(target_id=self.drafting.pk)
        Report.objects.using(DB).create(
            target_id=self.zenodo.pk, zenodo_doi="10.5281/zenodo.1")
        Report.objects.using(DB).create(
            target_id=self.f1000.pk, f1000_date=date(2026, 7, 1))

    def _stage(self, resp, key):
        return next(s for s in resp.context["stages"] if s["key"] == key)["count"]

    def test_every_screen_gives_the_same_answer(self):
        from pipeline.services import target_board as board

        overview = self.client.get("/pipeline/overview/")
        tile = overview.context["overall_stats"]["completed"]
        chart = self._stage(overview, "reported")
        portfolio_total = board.portfolio()["totals"]["completed"]
        board_rows = len(board.board_rows(completed="yes"))

        self.assertEqual({tile, chart, portfolio_total, board_rows}, {2},
                         f"tile={tile} chart={chart} portfolio={portfolio_total} "
                         f"board={board_rows} — one question, four readers")

    def test_a_report_with_no_doi_is_a_draft_and_not_finished(self):
        resp = self.client.get("/pipeline/overview/")
        self.assertEqual(self._stage(resp, "drafted"), 1)
        self.assertEqual(self._stage(resp, "reported"), 2)

    def test_the_chart_buckets_are_exclusive_and_account_for_everything(self):
        """The bars add up to the total *by construction*, not by luck.

        They did add up on the live data, which is what made the overlapping
        Feasibility bucket invisible: no target on file happens to have an IP,
        IF or FC session and no antibodies. One does here.
        """
        resp = self.client.get("/pipeline/overview/")
        drawn = (sum(s["count"] for s in resp.context["stages"])
                 + resp.context["on_hold_count"] + resp.context["cancelled_count"])
        self.assertEqual(drawn, Target.objects.using(DB).count())

    def test_a_target_with_a_non_wb_session_is_counted_once(self):
        resp = self.client.get("/pipeline/overview/")
        self.assertEqual(self._stage(resp, "experimenting"), 2)   # CCCC + IIII
        self.assertEqual(self._stage(resp, "feasibility"), 1)     # AAAA alone

    def test_a_sites_own_column_is_derived_too(self):
        resp = self.client.get("/pipeline/overview/")
        summaries = {s["site"].name: s for s in resp.context["site_summaries"]}
        self.assertEqual(summaries["Leicester"]["completed"], 0,
                         "no target is nominated at, or holds an antibody for, "
                         "a site that has finished one")

    def test_the_page_says_reported_rather_than_published(self):
        """A tile whose number changed and whose word did not is a tile a reader
        reconciles against the old meaning."""
        body = self.client.get("/pipeline/overview/").content.decode()
        self.assertIn("Reported", body)
        self.assertIn("Zenodo DOI or F1000", body)


class TheGenePageIsAWorkspaceTests(TestCase):
    """It gathered everything about a gene and let you change none of it.

    "Antibodies (0)" with a download button and no way to add one, so the answer
    to "record STMN2's antibodies" was always to leave — and the only thing you
    could actually *do* there was generate a report about work done elsewhere.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")

    def _page(self):
        resp = self.client.get(f"/pipeline/target/{self.target.pk}/")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_you_can_add_antibodies_and_cell_lines_without_leaving(self):
        body = self._page()
        self.assertIn("Add antibodies", body)
        self.assertIn("Add cell lines", body)
        self.assertIn('id="ab-add-panel"', body)
        self.assertIn('id="cl-add-panel"', body)

    def test_the_add_panels_post_to_the_same_endpoints_as_the_boards(self):
        """One write path per entity. A second one here would be free to drift
        from the board's, and the two previews would stop agreeing."""
        body = self._page()
        for name in ("bulk_antibodies_parse", "bulk_antibodies_commit",
                     "bulk_cell_lines_parse", "bulk_cell_lines_commit"):
            self.assertIn(reverse(f"pipeline:{name}"), body)

    def test_the_columns_come_from_the_same_constant_as_the_templates(self):
        from pipeline.views.imports import columns_and_example
        resp = self.client.get(f"/pipeline/target/{self.target.pk}/")
        self.assertEqual(resp.context["ab_columns"],
                         columns_and_example("antibodies")[0])
        self.assertEqual(resp.context["cl_columns"],
                         columns_and_example("cell-lines")[0])

    def test_recording_a_session_opens_the_boards_own_pop_out(self):
        """A session's header is a form of its own, so this links to the one
        implementation rather than copying it — pre-filled with the gene."""
        body = self._page()
        self.assertIn("/pipeline/sessions/board/?gene=STMN2&add=1", body)

    def test_a_board_arriving_with_add_opens_ready_to_type(self):
        for url, field in (("/pipeline/antibodies/board/", "ne-default-gene"),
                           ("/pipeline/cell-lines/board/", "ne-default-gene"),
                           ("/pipeline/sessions/board/", "ne-gene")):
            with self.subTest(url=url):
                body = self.client.get(url, {"gene": "STMN2", "add": "1"}).content.decode()
                self.assertIn("get('add')", body)
                self.assertIn(field, body)

    def test_every_section_can_reach_its_board_filtered_to_this_gene(self):
        body = self._page()
        for board in ("/pipeline/antibodies/board/?gene=STMN2",
                      "/pipeline/cell-lines/board/?gene=STMN2",
                      "/pipeline/sessions/board/?gene=STMN2"):
            self.assertIn(board, body)

    def test_the_downloads_are_this_genes_rows(self):
        """Offered only when there is something to download, and scoped to the
        gene — a whole-dataset file from a gene's page is the same trap the
        boards had."""
        self.assertNotIn("/pipeline/antibodies/export/", self._page())
        company = Company.objects.using(DB).create(name="Proteintech")
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="10586-1-AP", site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        body = self._page()
        self.assertIn("/pipeline/antibodies/export/?gene=STMN2", body)
        self.assertIn("/pipeline/cell-lines/export/?gene=STMN2", body)

    def test_the_bench_workbook_is_offered_here(self):
        """Its only entry point was the retired session list."""
        self.assertIn("/pipeline/session/template/?gene=STMN2", self._page())

    def test_generating_a_report_is_still_here(self):
        """The one thing this page could always do. It must survive the rebuild."""
        self.assertIn(
            reverse("pipeline:generate_report", args=[self.target.pk]), self._page())


class ChromeIsOnEveryPageTests(TestCase):
    """"A search box in the top bar on every page" was not true.

    The third field test tested every page reachable from Home and the Browse
    menu, and found one that had no top bar at all: the figure cropper. It does
    not extend base.html — it is a self-contained tool with its own head, kept
    that way on purpose — so "Publish figures" was a dead end you could only leave
    with the browser's Back button.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)

    # Everything reachable from Home or the Browse menu.
    PAGES = ("/pipeline/start/", "/pipeline/overview/", "/pipeline/targets/board/",
             "/pipeline/antibodies/board/", "/pipeline/cell-lines/board/",
             "/pipeline/sessions/board/", "/pipeline/feasibility/",
             "/pipeline/recommendations/",
             "/pipeline/data/", "/pipeline/cropper/", "/pipeline/guide/")

    def test_every_page_has_the_search_box(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                self.assertIn('action="/pipeline/find/"', body,
                              f"{path} has no search box in its chrome")

    def test_every_page_has_a_way_back_to_the_hub(self):
        """A page you can only leave with the Back button is a dead end."""
        for path in self.PAGES:
            with self.subTest(path=path):
                body = self.client.get(path).content.decode()
                self.assertIn('href="/pipeline/start/"', body,
                              f"{path} offers no way back into the app")


class AccountPagesReachedFromThePipelineWearItsChromeTests(TestCase):
    """Change password is linked from every pipeline page, and was not one.

    It is django-allauth's page, shared with the Academy, so it cannot extend
    base.html — and it drew the *public website's* header instead: Home, About
    Us, Publications, Roadmap, Partners, Contact. Somebody who pressed "Change
    password" on the sessions board got seven links to the marketing site and
    none back to their work. The owner reported it as confusing on sight.

    The signal is the ``?next=`` the pipeline nav already sends
    (``pipeline/context_processors.py::account_chrome``). Four things have to
    hold, and the third is the one that would go wrong quietly.
    """

    databases = {"pipeline_db", "academy_db"}

    PUBLIC_NAV = "/publications/"      # on the public header, nowhere else
    PIPELINE_NAV = "/pipeline/find/"   # the pipeline bar's search box

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)

    def test_reached_from_the_pipeline_it_is_a_pipeline_page(self):
        for path in ("/accounts/password/change/", "/accounts/email/"):
            with self.subTest(path=path):
                body = self.client.get(
                    path + "?next=/pipeline/sessions/board/").content.decode()
                self.assertIn(self.PIPELINE_NAV, body)
                self.assertIn("/pipeline/sessions/board/", body)  # the way back
                self.assertNotIn(self.PUBLIC_NAV, body)

    def test_reached_from_the_academy_it_is_an_academy_page(self):
        """The other direction. A visitor here may never have seen the pipeline."""
        body = self.client.get("/accounts/password/change/").content.decode()
        self.assertIn(self.PUBLIC_NAV, body)
        self.assertNotIn(self.PIPELINE_NAV, body)

    def test_a_failed_save_keeps_the_chrome(self):
        """allauth posts ``next`` as a hidden field, not in the query string.

        So a page that read ``request.GET.next`` alone answered correctly on the
        way in and changed shape on the error — the bar swapping to the public
        site's the moment somebody mistyped their current password.
        """
        body = self.client.post("/accounts/password/change/",
                                {"oldpassword": "wrong", "password1": "a",
                                 "password2": "b",
                                 "next": "/pipeline/sessions/board/"}).content.decode()
        self.assertIn(self.PIPELINE_NAV, body)
        self.assertNotIn(self.PUBLIC_NAV, body)

    def test_next_must_be_one_of_this_sites_pipeline_paths(self):
        """The chrome draws ``next`` in an href, so it is checked, not trusted.

        (allauth echoes whatever arrived into its own hidden field regardless;
        that is its business, and ``url_has_allowed_host_and_scheme`` is what
        stops it *redirecting* anywhere off-site. What is asserted here is only
        that none of these turns the page into a pipeline page or draws a way
        "back to the pipeline" that does not go there.)
        """
        for bad in ("//evil.example/pipeline/", "https://evil.example/pipeline/x",
                    "/academy/"):
            with self.subTest(next=bad):
                body = self.client.get(
                    "/accounts/password/change/?next=" + bad).content.decode()
                self.assertNotIn(self.PIPELINE_NAV, body)
                self.assertNotIn("Back to the pipeline", body)


class NoPageStyleBlockOverridesTheChromeTests(TestCase):
    """A page's own stylesheet must not redefine a class the top bar uses.

    ``ChromeIsOnEveryPageTests`` above asks whether the nav's markup is in the
    page, and on Set recommendations it was — every link, the search box, the
    user block, the sign-out form, all present and none of it visible. The page
    declared ``.hidden { display: none !important; }`` for its own image
    overlay, and the chrome shows its desktop half with Tailwind's
    ``hidden md:flex``: hidden on a phone, flex on a wide screen. An
    ``!important`` on a bare class beats ``md:flex`` whatever the specificity or
    the source order, so the whole bar emptied out and the only ways off the
    page were the wordmark and the Back button — there was no sign-out at all
    (twentieth field test).

    That is the shape this file exists for: the server answered correctly and
    the page did the wrong thing with it, invisible to every response-level
    test. A browser test proves the fix on the one page; this proves the *rule*
    across every page for the cost of reading nine files, and it is the cheapest
    thing that can fail here.

    A rule is flagged only when its selector is a bare class on its own —
    ``.hidden``, or ``.hidden, .foo``. A scoped one (``#image-preview.rec-hidden``,
    ``.rec-tool .thumb-zoom``) is how a page is *supposed* to name its own
    furniture and is exactly what the fix looks like, so flagging those would
    make the test unpassable.
    """
    databases = {"academy_db", "pipeline_db"}

    TEMPLATE_DIR = Path(settings.BASE_DIR) / "pipeline/templates/pipeline"

    @staticmethod
    def _strip_django(source):
        source = re.sub(r"\{%.*?%\}", "", source, flags=re.S)
        return re.sub(r"\{\{.*?\}\}", "x", source, flags=re.S)

    def _chrome_classes(self):
        """Every class name base.html hands to an element in its top bar."""
        source = self._strip_django((self.TEMPLATE_DIR / "base.html").read_text())
        names = set()
        for m in re.finditer(r'class="([^"]*)"', source):
            for token in m.group(1).split():
                # Tailwind's responsive/state variants (`md:flex`, `hover:bg-…`)
                # cannot be written as a bare CSS class selector, so only the
                # unprefixed half of a utility is reachable this way.
                if ":" not in token:
                    names.add(token)
        return names

    def _bare_class_selectors(self, source):
        """``{class name: selector}`` for every rule selected by a lone class."""
        found = {}
        for block in re.finditer(r"<style[^>]*>(.*?)</style>", source, re.S):
            css = re.sub(r"/\*.*?\*/", "", self._strip_django(block.group(1)),
                         flags=re.S)
            # Rule heads only: everything up to a `{`, minus at-rule bodies'
            # own braces, which `[^{}]*` already keeps us out of.
            for rule in re.finditer(r"([^{}]+)\{", css):
                head = rule.group(1)
                if head.lstrip().startswith("@"):
                    continue
                for selector in head.split(","):
                    selector = selector.strip()
                    m = re.fullmatch(r"\.([A-Za-z0-9_-]+)", selector)
                    if m:
                        found[m.group(1)] = selector
        return found

    def test_no_page_redefines_a_class_the_top_bar_relies_on(self):
        chrome = self._chrome_classes()
        self.assertIn("hidden", chrome, "base.html no longer uses `hidden` — "
                      "check this test still guards what it thinks it does")

        for path in sorted(self.TEMPLATE_DIR.glob("*.html")):
            if path.name == "base.html":
                continue
            with self.subTest(template=path.name):
                collisions = {
                    name: selector
                    for name, selector in
                    self._bare_class_selectors(path.read_text()).items()
                    if name in chrome
                }
                self.assertEqual(
                    collisions, {},
                    f"{path.name} styles {sorted(collisions.values())} as a bare "
                    "class, and the top bar uses that name too — scope it to the "
                    "element it is for (`#thing.page-hidden`) or the nav goes "
                    "with it")


class EveryPageIsInBrowseTests(TestCase):
    """Half the app could be reached only from the task hub.

    Browse listed the four boards, Overview, Add a gene and How it all works.
    People & access, Downloads & uploads, Set recommendations, Publish figures
    and the extension install page were linked from the hub and nowhere else,
    and the Portfolio only from the target board — so wanting recommendations
    while standing on a board meant going Home first, and a coordinator on the
    sessions board had no way to the grant-writing figures at all.

    The three board guides had been telling people *"Downloads & uploads (in
    Browse)"* for as long as they had existed, which is the app disagreeing with
    its own documentation about its own shape.

    Derived from the hub's own task list rather than a second hard-coded copy: a
    card added to the hub and not to Browse is precisely the failure, and a test
    that lists the destinations itself cannot see it.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def _superuser_client(self):
        from django.contrib.auth.models import User
        from django.test import Client
        for alias in ("academy_db", DB):
            u = User(username="root", is_superuser=True, is_staff=True)
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="root")
        Member.objects.create(user_id=pu.pk, site_id=self.site.pk,
                              role="admin", is_active=True)
        client = Client()
        self.assertTrue(client.login(username="root", password="pw"))
        return client

    def _menus(self, body):
        """The desktop dropdown and the responsive menu, sliced out separately.

        They are two lists in the DOM, and a link added to one is not in the
        other — which is how a phone once lost the gene a laptop kept. The
        desktop slice runs to the menu's own closing tag, which only works
        because its dividers are ``<hr>``; written as empty ``<div>``s they
        ended the slice early and hid everything below the first one.
        """
        desktop = body.split('id="browse-menu"', 1)[1].split("</div>", 1)[0]
        mobile = body.split('id="mobile-menu"', 1)[1].split("</nav>", 1)[0]
        return {"desktop dropdown": desktop, "responsive menu": mobile}

    def test_every_hub_task_is_also_in_browse(self):
        from pipeline.views.hub import TASK_DEFS
        body = self.client.get("/pipeline/sessions/board/").content.decode()
        for menu, chunk in self._menus(body).items():
            for _id, title, _desc, url_name, _icon in TASK_DEFS:
                with self.subTest(menu=menu, task=title):
                    self.assertIn(f'href="{reverse(url_name)}"', chunk,
                                  f'"{title}" is on the hub and not in the {menu}')

    def test_the_portfolio_is_in_browse(self):
        """It is on no hub card either — its only link in the whole app was a
        button on the target board, so it was unreachable from the other three."""
        body = self.client.get("/pipeline/sessions/board/").content.decode()
        for menu, chunk in self._menus(body).items():
            with self.subTest(menu=menu):
                self.assertIn('href="/pipeline/targets/portfolio/"', chunk)

    def test_a_superuser_reaches_people_and_access_from_any_page(self):
        client = self._superuser_client()
        body = client.get("/pipeline/cell-lines/board/").content.decode()
        for menu, chunk in self._menus(body).items():
            with self.subTest(menu=menu):
                self.assertIn('href="/pipeline/users/board/"', chunk)

    def test_an_ordinary_member_is_not_offered_it(self):
        """Superuser-only in the nav for the same reason it is superuser-only on
        the hub: it hands out access, and a link to a page that refuses you is
        worse than no link."""
        body = self.client.get("/pipeline/cell-lines/board/").content.decode()
        for menu, chunk in self._menus(body).items():
            with self.subTest(menu=menu):
                self.assertNotIn('href="/pipeline/users/board/"', chunk)

    def test_the_extension_is_offered_while_it_is_still_team_only(self):
        body = self.client.get("/pipeline/start/").content.decode()
        for menu, chunk in self._menus(body).items():
            with self.subTest(menu=menu):
                self.assertIn('href="/extension/"', chunk)

    def test_and_drops_out_of_browse_once_it_is_public(self):
        """Then it lives on the public Tools hub, and listing it twice would make
        the pipeline look like it owns a page it does not."""
        with self.settings(EXTENSION_PAGE_PUBLIC=True):
            body = self.client.get("/pipeline/start/").content.decode()
        for menu, chunk in self._menus(body).items():
            with self.subTest(menu=menu):
                self.assertNotIn('href="/extension/"', chunk)

    def test_the_two_menus_offer_the_same_destinations(self):
        """Not "both are non-empty": the desktop dropdown and the responsive menu
        are maintained by hand, so the thing worth pinning is that they agree."""
        client = self._superuser_client()
        body = client.get("/pipeline/start/").content.decode()
        menus = self._menus(body)
        hrefs = {name: set(re.findall(r'href="([^"]+)"', chunk))
                 for name, chunk in menus.items()}
        desktop, mobile = hrefs["desktop dropdown"], hrefs["responsive menu"]
        # The responsive menu also carries the account links the desktop bar
        # keeps on the right-hand side, so it is a superset by design.
        self.assertEqual(desktop - mobile, set(),
                         "the responsive menu is missing destinations")


class TheGeneFollowsYouEverywhereTests(TestCase):
    """It followed you from a board, and only on a desktop.

    Two gaps the third field test found. The gene's own page identifies its gene
    by the URL *path*, and the nav was reading the query *string* — so the one
    page most obviously about a single gene was the one that dropped it. And the
    responsive menu never carried it at all, so a phone lost the gene exactly
    where a laptop kept it.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.target = Target.objects.using(DB).create(gene_name="SOD1")

    BOARDS = ("/pipeline/targets/board/", "/pipeline/antibodies/board/",
              "/pipeline/cell-lines/board/", "/pipeline/sessions/board/")

    def test_the_genes_own_page_carries_its_gene_into_the_nav(self):
        resp = self.client.get(f"/pipeline/target/{self.target.pk}/")
        self.assertEqual(resp.context["nav_gene"], "SOD1")
        body = resp.content.decode()
        for board in self.BOARDS:
            self.assertIn(f'href="{board}?gene=SOD1"', body,
                          f"Browse dropped the gene on the way to {board}")

    def test_both_menus_carry_it_so_a_phone_does_not_lose_it(self):
        """The desktop dropdown and the responsive menu are two separate lists in
        the DOM. Only one of them had the gene."""
        body = self.client.get("/pipeline/antibodies/board/",
                               {"gene": "SOD1"}).content.decode()
        desktop = body.split('id="browse-menu"', 1)[1].split("</div>", 1)[0]
        mobile = body.split('id="mobile-menu"', 1)[1]
        for menu, name in ((desktop, "desktop dropdown"), (mobile, "mobile menu")):
            for board in ("/pipeline/cell-lines/board/", "/pipeline/sessions/board/"):
                with self.subTest(menu=name, board=board):
                    self.assertIn(f'href="{board}?gene=SOD1"', menu,
                                  f"the {name} dropped the gene")

    def test_with_no_gene_in_play_the_links_stay_plain(self):
        body = self.client.get("/pipeline/antibodies/board/").content.decode()
        self.assertIn('href="/pipeline/cell-lines/board/"', body)
        self.assertNotIn("?gene=", body)

    def test_set_recommendations_carries_it_too(self):
        """It joined Browse without the gene, correctly — the view did not read
        `?gene=` then, and a query string a view ignores only makes a link look
        filtered. The view reads it now, and this is a per-gene surface whose
        own first instruction is "pick a gene", so carrying it is the whole
        difference between landing on that gene's figures and landing on a
        160-item dropdown."""
        body = self.client.get("/pipeline/antibodies/board/",
                               {"gene": "SOD1"}).content.decode()
        desktop = body.split('id="browse-menu"', 1)[1].split("</div>", 1)[0]
        mobile = body.split('id="mobile-menu"', 1)[1]
        for menu, name in ((desktop, "desktop dropdown"), (mobile, "mobile menu")):
            with self.subTest(menu=name):
                self.assertIn('href="/pipeline/recommendations/?gene=SOD1"', menu,
                              f"the {name} dropped the gene")


class SearchSaysWhyItMatchedTests(TestCase):
    """Searching HAP1 returned UBQLN2 under "Genes" and said nothing about why.

    It was right: UBQLN2's alternative name is *Chap1*, and "HAP1" is a substring
    of it. Correct and unreadable — the same fault the paste preview had when it
    printed two different rows identically.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.ubqln2 = Target.objects.using(DB).create(
            gene_name="UBQLN2", protein_name="Ubiquilin-2",
            alternative_name="Chap1; DSK2; PLIC-2")

    def _rows(self, term):
        resp = self.client.get("/pipeline/find/", {"q": term})
        self.assertEqual(resp.status_code, 200)
        groups = {g["key"]: g for g in resp.context["results"]["groups"]}
        return groups, resp.content.decode()

    def test_a_match_on_another_name_says_so(self):
        groups, body = self._rows("HAP1")
        row = groups["targets"]["rows"][0]
        self.assertEqual(row["title"], "UBQLN2")
        self.assertIn("another name for it", row["why"])
        self.assertIn("Chap1", row["why"])
        # And it is on the page, not just in the payload.
        self.assertIn("another name for it", body)

    def test_an_obvious_match_stays_quiet(self):
        """No explanation when the reason is the title — the common case should
        not be cluttered."""
        groups, _ = self._rows("UBQLN")
        self.assertEqual(groups["targets"]["rows"][0]["why"], "")

    def test_every_row_shape_carries_the_field(self):
        """Present everywhere, so a template never has to guess whether it exists."""
        company = Company.objects.using(DB).create(name="Proteintech")
        Antibody.objects.using(DB).create(
            target_id=self.ubqln2.pk, company_id=company.pk,
            catalogue_number="10586-1-AP", clone_id="Chap1-42",
            site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk)
        groups, _ = self._rows("Chap1")
        for key, group in groups.items():
            for row in group["rows"]:
                with self.subTest(group=key):
                    self.assertIn("why", row)


class TheHubShowsTheOrderOfTheWorkTests(TestCase):
    """The front door did not say where the front door was.

    Cards were sorted by the signed-in member's role, so the three boards led
    and *Check feasibility* — step one of everything — sat fifth, in the middle
    of the second row. The sixth field test's annotated walkthrough had to draw
    a box round it captioned "1 · Everything starts here".

    Frequency ordering serves the person who already knows the app; a hub is for
    the one who does not. Both are served now, with one control each: the chips
    stay role-aware, the grid is the order of the work.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def test_the_first_thing_on_the_page_is_the_first_thing_you_do(self):
        from pipeline.views.hub import PHASES
        body = self.client.get("/pipeline/start/").content.decode()
        grid = body.split("A gene, start to finish", 1)[1]
        # Both ways a gene gets on a list, and looking it up comes first.
        self.assertEqual(PHASES[0][3], ["feasibility", "board"])
        self.assertLess(grid.index("/pipeline/feasibility/"),
                        grid.index("/pipeline/sessions/board/"),
                        "the boards still come before the way in")

    def test_every_task_is_in_exactly_one_phase(self):
        """A card that falls out of every phase disappears from the hub — the
        grid is built by slicing TASK_DEFS, so a new task with no phase is
        silently unreachable rather than merely misplaced."""
        from pipeline.views.hub import ACROSS, PHASES, TASK_DEFS
        placed = [i for _pid, _t, _b, ids, _n in PHASES for i in ids] + list(ACROSS[1])
        self.assertEqual(sorted(placed), sorted(d[0] for d in TASK_DEFS))
        self.assertEqual(len(placed), len(set(placed)), "a task is in two phases")

    def test_every_task_still_reaches_its_page(self):
        body = self.client.get("/pipeline/start/").content.decode()
        for url in ("/pipeline/feasibility/", "/pipeline/cell-lines/board/",
                    "/pipeline/antibodies/board/", "/pipeline/sessions/board/",
                    "/pipeline/cropper/", "/pipeline/recommendations/",
                    "/pipeline/targets/board/", "/pipeline/data/"):
            with self.subTest(url=url):
                self.assertIn(f'href="{url}"', body)

    def test_there_is_one_order_and_it_is_the_same_for_everybody(self):
        """The role-based "your usual" row was a second, competing answer to
        "what should I look at", and two orders on one page is no order."""
        body = self.client.get("/pipeline/start/").content.decode()
        self.assertNotIn("Your usual", body)
        src = (Path(settings.BASE_DIR) / "pipeline/views/hub.py").read_text()
        self.assertNotIn("ROLE_USUAL", src)

    def test_the_way_in_is_named_for_what_it_does(self):
        """"Check feasibility" named the assessment, not the act."""
        body = self.client.get("/pipeline/start/").content.decode()
        # Scoped to the grid: the nav's Browse menu links to the same page, and
        # checking the whole body would pass on that instead.
        grid = body.split("A gene, start to finish", 1)[1]
        card = grid.split("/pipeline/feasibility/", 1)[1][:1200]
        self.assertIn("add it", card)

    def test_the_page_does_not_claim_one_way_in(self):
        """It said adding a gene here was "the only way a target gets on your
        site's list". The target board's own Add panel posts to the same
        `bulk_targets.plan`/`apply` pair and writes the same target and the same
        nomination, so the claim was simply false — and a page a reader catches
        out once is a page they stop trusting."""
        body = self.client.get("/pipeline/start/").content.decode()
        grid = body.split("A gene, start to finish", 1)[1]
        step_one = grid.split("Set up what you will test", 1)[0]
        self.assertNotIn("only way", step_one)
        self.assertIn("/pipeline/feasibility/", step_one)
        self.assertIn("/pipeline/targets/board/", step_one)

    def test_the_page_does_not_promise_a_wizard(self):
        """Many genes never need all four applications, which is why
        gene_progress refuses a percentage. The hub must not imply otherwise."""
        body = self.client.get("/pipeline/start/").content.decode()
        self.assertIn("jump in anywhere", body)


class ASessionsBenchSheetIsWhereTheSessionIsTests(TestCase):
    """The bound sheet existed and was reachable only from the sessions board.

    So on a gene's page — where you are when you have just planned a session —
    the nearest thing was the per-gene bench *workbook*, which cannot fill a
    session and makes a new one. The sixth field test took it, and ended with a
    Planned session at 0 results beside a Complete one. Warning about that was
    half an answer.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        from datetime import date
        from django.contrib.auth.models import User
        from pipeline.models import ExperimentSession, Member
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        pu = User.objects.using(DB).get(username="carl")
        member = Member.objects.using(DB).get(user_id=pu.pk)
        self.target = Target.objects.using(DB).create(gene_name="ELP3")
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, procedure_type="WB",
            date=date(2026, 7, 31), experimenter_id=member.pk,
            status=ExperimentSession.SessionStatus.PLANNED)

    def test_a_planned_session_offers_its_own_sheet_on_the_gene_page(self):
        body = self.client.get(f"/pipeline/target/{self.target.pk}/").content.decode()
        self.assertIn(f"/pipeline/session/{self.session.pk}/download/bench-sheet/", body)
        self.assertIn("Bench sheet", body)

    def test_that_sheet_actually_downloads(self):
        resp = self.client.get(
            f"/pipeline/session/{self.session.pk}/download/bench-sheet/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheet", resp["Content-Type"])


class WorkArrivingOutOfOrderStillLandsSomewhereTests(TestCase):
    """A shipment turns up for a gene nobody added, or results are recorded
    before the target exists.

    Every write path can mint the target inline, which is right. But it minted
    one with no nomination — and a target's site lives on its nominations — so
    the gene existed and belonged to nobody: absent from every site filter,
    uncounted on Overview, and described by its own page as "not nominated by
    any site yet". Feasibility has recorded the nomination since the first field
    test; the four other routes into `resolve_or_create_target` had not.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        from django.contrib.auth.models import User
        from pipeline.models import Company, Member
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        pu = User.objects.using(DB).get(username="carl")
        self.member = Member.objects.using(DB).get(user_id=pu.pk)
        Company.objects.using(DB).create(name="abcam")

    def test_a_gene_minted_by_an_antibody_paste_is_on_your_sites_list(self):
        from pipeline.services import bulk_antibodies
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\n"
            "NEWGENE1\tAB-999\tabcam\n")
        with mock.patch("pipeline.services.targets.uniprot.lookup_gene",
                        return_value={"found": False}):
            bulk_antibodies.apply(rows, True, member=self.member)
        target = Target.objects.using(DB).get(gene_name__iexact="NEWGENE1")
        nom = TargetNomination.objects.using(DB).filter(target_id=target.pk).first()
        self.assertIsNotNone(nom, "the gene was created belonging to nobody")
        self.assertEqual(nom.site_id, self.site.pk)
        self.assertFalse(nom.funded, "unfunded is the honest starting state")

    def test_the_gene_page_then_names_the_site_rather_than_nobody(self):
        from pipeline.services import bulk_antibodies
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\nNEWGENE2\tAB-998\tabcam\n")
        with mock.patch("pipeline.services.targets.uniprot.lookup_gene",
                        return_value={"found": False}):
            bulk_antibodies.apply(rows, True, member=self.member)
        target = Target.objects.using(DB).get(gene_name__iexact="NEWGENE2")
        resp = self.client.get(f"/pipeline/target/{target.pk}/")
        self.assertEqual(resp.context["nominated_sites"], ["Leicester"])
        self.assertNotContains(resp, "not nominated by any site yet")

    def test_the_progress_strip_links_to_whatever_is_still_missing(self):
        """The answer to "can I do it in the wrong order" — every unfinished
        step on a gene's page is a link to the board that finishes it, so the
        page is a worklist rather than a report."""
        from pipeline.services import bulk_antibodies
        rows = bulk_antibodies.parse(
            "gene\tcatalogue\tcompany\nNEWGENE3\tAB-997\tabcam\n")
        with mock.patch("pipeline.services.targets.uniprot.lookup_gene",
                        return_value={"found": False}):
            bulk_antibodies.apply(rows, True, member=self.member)
        target = Target.objects.using(DB).get(gene_name__iexact="NEWGENE3")
        resp = self.client.get(f"/pipeline/target/{target.pk}/")
        steps = {s["key"]: s for s in resp.context["progress_steps"]}
        # Done out of order: antibodies before the knockout line.
        self.assertTrue(steps["antibodies"]["done"])
        self.assertFalse(steps["ko_line"]["done"])
        for key in ("ko_line", "ko_validated"):
            with self.subTest(key=key):
                self.assertIn("/pipeline/cell-lines/board/", steps[key]["url"])
                self.assertTrue(steps[key]["hint"], "an undone step must say what to do")
        self.assertEqual(resp.context["next_step"]["key"], "ko_line")


class TheWalkthroughIsWiredUpTests(TestCase):
    """The cross-cutting guide, and the two ways this kind of page dies.

    PLATFORM_ROADMAP #37 asked for one and named the trap: run 7 produced a
    walkthrough that was never wired to a URL, so it aged in a branch until it
    was wrong. **A routed endpoint is not a reachable one** — the same rule as
    an export with no importer, and as `import_template`, which was built
    correctly for three kinds and named by no template or script.

    So what is pinned is the wiring, not the prose. The prose is a record of a
    run and is replaced wholesale rather than edited; there is nothing here a
    future edit to the *text* can break.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)

    def test_it_is_reachable_from_the_front_door_and_from_browse(self):
        """Both menus, because there are two and only one has ever been
        updated at a time — `nav_gene` rode the desktop dropdown alone and a
        phone lost the gene exactly where a laptop kept it."""
        url = reverse("pipeline:walkthrough")
        hub = self.client.get("/pipeline/start/").content.decode()
        self.assertIn(url, hub, "the hub does not name the guide")
        # Twice on any page that has chrome: the desktop dropdown and the
        # responsive menu.
        board = self.client.get("/pipeline/targets/board/").content.decode()
        self.assertGreaterEqual(
            board.count(f'href="{url}"'), 2,
            "the guide is in one Browse menu but not the other")

    def test_every_screenshot_it_names_is_on_disk(self):
        """A walkthrough is its pictures. `{% static %}` does not fail loudly
        for a missing file — it builds the URL anyway — so a page of alt text
        and broken images returns a clean 200 and looks fine to every test that
        only reads the response.
        """
        import re
        from pathlib import Path

        body = self.client.get(reverse("pipeline:walkthrough")).content.decode()
        names = re.findall(r'walkthrough/([\w.-]+\.jpg)', body)
        self.assertEqual(len(names), 14, f"expected 14 screens, found {len(names)}")
        root = Path("pipeline/static/pipeline/walkthrough")
        for name in names:
            with self.subTest(image=name):
                self.assertTrue((root / name).is_file(), f"{name} is not on disk")

    def test_its_stylesheet_cannot_reach_the_rest_of_the_app(self):
        """It was authored as a standalone page, so it carries `body`, `:root`
        and `*` rules. Dropped into a template that shares a cascade with the
        chrome, those repaint the nav on every page — scoped under `.wt`.
        """
        from pathlib import Path
        css = Path("pipeline/templates/pipeline/walkthrough.html").read_text()
        css = css[css.index("<style>"):css.index("</style>")]
        for loose in (":root {", "\n  body {", "\n  * {", "\n  header {", "\n  main {"):
            with self.subTest(rule=loose.strip()):
                self.assertNotIn(loose, css,
                                 f"{loose.strip()} is not scoped to the page")


class TheStylesheetComesFromThisSiteTests(TestCase):
    """Which of the two sources a page names, and that the switch works.

    Cheap on purpose: this is server-rendered HTML, so it belongs here rather
    than in the browser suite at 170× the cost. What a browser is genuinely
    needed for — that a stylesheet which never arrives still leaves a usable
    page — is pinned in `tests_browser_board.py`.

    Both directions are patched explicitly. Whether the built file exists
    depends on whether `bin/build_css.sh` has been run in this checkout, and a
    test that reads ambient state is the shape that had four browser tests
    passing until the day CI ran somewhere with a working network.
    """

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")

    def setUp(self):
        self.client = _member_client(self, self.site)

    def _hub(self):
        return self.client.get(reverse("pipeline:hub")).content.decode()

    def test_the_built_stylesheet_is_used_and_the_cdn_is_not_named(self):
        with mock.patch("pipeline.styling.built_stylesheet_url",
                        return_value="/static/pipeline/tailwind.css"):
            html = self._hub()
        self.assertIn('href="/static/pipeline/tailwind.css"', html)
        self.assertNotIn("cdn.tailwindcss.com", html,
                         "a page with its own stylesheet still called the CDN")

    def test_without_a_build_the_cdn_stands_in(self):
        """The fallback that keeps a fresh clone working with no build step.

        Wrong in production, which is what `pipeline.W003` is for — but a
        checkout that renders unstyled until somebody finds a shell script is
        worse, and this is the half that makes the change safe to merge before
        the Build Command is changed.
        """
        with mock.patch("pipeline.styling.built_stylesheet_url",
                        return_value=None):
            html = self._hub()
        self.assertIn("cdn.tailwindcss.com", html)

    def test_the_fallback_is_on_the_page_either_way(self):
        """It is inline, because a stylesheet that failed to load cannot carry
        its own contingency — and `.hidden` is what keeps every dropdown,
        modal and scrim in this app shut.
        """
        for url in ("/static/pipeline/tailwind.css", None):
            with self.subTest(built=bool(url)):
                with mock.patch("pipeline.styling.built_stylesheet_url",
                                return_value=url):
                    html = self._hub()
                self.assertIn(".no-tailwind .hidden", html)
                self.assertIn(r".no-tailwind .md\:flex", html)
                self.assertIn("OGAStyling", html)

    def test_the_deploy_check_names_the_build_command(self):
        """W003 reaches the owner, who has to change one setting in Render's
        dashboard. A warning that says only "not found" is not actionable.
        """
        from pipeline.apps import _check_stylesheet

        with mock.patch("pipeline.styling.built_stylesheet_url",
                        return_value=None), self.settings(DEBUG=False):
            issues = _check_stylesheet(None)
        self.assertEqual([i.id for i in issues], ["pipeline.W003"])
        self.assertIn("build_css.sh", issues[0].hint)

        # Silent once it is built, and silent in DEBUG whatever the answer.
        with mock.patch("pipeline.styling.built_stylesheet_url",
                        return_value="/static/pipeline/tailwind.css"), \
                self.settings(DEBUG=False):
            self.assertEqual(_check_stylesheet(None), [])
        with mock.patch("pipeline.styling.built_stylesheet_url",
                        return_value=None), self.settings(DEBUG=True):
            self.assertEqual(_check_stylesheet(None), [])
