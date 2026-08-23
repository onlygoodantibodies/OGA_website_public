"""The pages the boards replaced are gone, and everything left still renders.

A page that still answers its URL is a page people still land on, and both field
tests did exactly that — the first recorded every session result on the legacy
session page, and the hub itself was still linking to three of them. Hiding them
from the nav was not enough.

Two things are pinned. The retired URLs are really gone, so nobody can bookmark
their way back in. And every page that remains still renders, because deleting a
URL name breaks every ``{% url %}`` that pointed at it — and that is a
render-time explosion no import check and no `manage.py check` will catch.
"""
from __future__ import annotations

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             Member, Site, Target)
from pipeline.tests_timeouts import DB, _member_client

# Retired 31 Jul 2026. Each was a page the boards replaced.
RETIRED = [
    "antibody_search",   # -> antibodies board
    "antibody_add",      # -> the board's Add pop-out
    "bulk_antibodies",   # -> the same pop-out
    "cell_line_list",    # -> cell lines board
    "bulk_cell_lines",   # -> the board's Add pop-out
    "session_list",      # -> sessions board
    "session_data",      # -> the board's own download / upload
    "session_plan",      # -> the board's Add pop-out
    "target_add",        # -> the target board's Add pop-out
    # Retired 31 Jul 2026, once the boards grew an identity dialog — the one
    # thing these four could do that a board would not.
    "antibody_detail",   # -> antibodies board
    "antibody_edit",     # -> the board's identity dialog
    "cell_line_detail",  # -> cell lines board
    "cell_line_edit",    # -> the board's identity dialog
    # Reagent batches, retired 31 Jul 2026 — not replaced by anything, withdrawn.
    # The workflow ended in the app **emailing a manufacturer contact directly**
    # from onlygoodantibodies@gmail.com and marking the items sent. Ordering
    # reagents from a partner is a conversation, and automating it before they
    # expect it is how you spend goodwill you cannot get back. The rows are
    # untouched and still reachable through the whole-dataset sheet.
    "batch_list",
    "batch_create",
    "batch_detail",
    "mark_not_sent",
    "receiving_queue",
    "receive_item",
    "manufacturer_contact_list",
]

# session_detail is gone as a *page* — its results grid, conditions, protocol
# phases and bench-sheet round trip are all on the sessions board now — but its
# URL survives as a redirect, and deliberately so: the first field test recorded
# every result there, so the bookmarks and pasted links exist and a 404 would
# strand them. Same treatment as `dashboard`.
REDIRECTED = {
    "session_detail": "/pipeline/sessions/board/",
    "dashboard": "/pipeline/targets/board/",
}


class RetiredPagesAreGoneTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def test_none_of_the_retired_names_still_resolve(self):
        for name in RETIRED:
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    # Some took a pk; reverse fails either way once gone.
                    reverse(f"pipeline:{name}")

    def test_a_retired_url_that_people_bookmarked_redirects_rather_than_404s(self):
        """Deleting a page is not the same as stranding the links to it. The
        session page in particular was where the first field test recorded every
        result, so those URLs are out in the world."""
        site = Site.objects.using(DB).create(name="Cornell", short_code="COR")
        client = _member_client(self, site)
        resp = client.get("/pipeline/session/7/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(REDIRECTED["session_detail"]))
        # …and it says which session, so you land on it rather than on the top.
        self.assertIn("open=7", resp["Location"])

    def test_the_session_page_template_is_gone(self):
        from django.template.loader import get_template
        from django.template import TemplateDoesNotExist
        with self.assertRaises(TemplateDoesNotExist):
            get_template("pipeline/session_detail.html")

    def test_their_urls_no_longer_answer(self):
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        client = _member_client(self, site)
        for path in ("/pipeline/antibodies/", "/pipeline/cell-lines/",
                     "/pipeline/sessions/", "/pipeline/session/data/",
                     "/pipeline/session/plan/", "/pipeline/target/add/",
                     "/pipeline/antibody/add/", "/pipeline/antibodies/bulk/",
                     "/pipeline/cell-lines/bulk/",
                     "/pipeline/antibody/1/", "/pipeline/antibody/1/edit/",
                     "/pipeline/cell-lines/1/", "/pipeline/cell-lines/1/edit/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_endpoints_the_boards_post_to_survived(self):
        """The pages went; their parse/commit endpoints did not. The boards'
        pop-outs post to them, so taking these out with the pages would have
        broken creating anything on three boards at once."""
        for name in ("bulk_antibodies_parse", "bulk_antibodies_commit",
                     "bulk_cell_lines_parse", "bulk_cell_lines_commit",
                     "session_plan_parse", "session_plan_commit"):
            with self.subTest(name=name):
                self.assertTrue(reverse(f"pipeline:{name}"))

    def test_what_only_the_retired_pages_offered_has_a_home(self):
        """Two things were reachable only from the session list: the
        step-by-step create form, and the per-gene bench workbook. Deleting the
        list without rehoming them would have stranded both."""
        site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        client = _member_client(self, site)
        body = client.get("/pipeline/sessions/board/").content.decode()
        self.assertIn(reverse("pipeline:session_create"), body)
        # The workbook needs a gene to mean anything, so it appears with one.
        Target.objects.using(DB).create(gene_name="STMN2")
        body = client.get("/pipeline/sessions/board/",
                          {"gene": "STMN2"}).content.decode()
        self.assertIn(reverse("pipeline:session_template_export"), body)


class EveryRemainingPageStillRendersTests(TestCase):
    """Deleting a URL name breaks every {% url %} that pointed at it, and that
    only shows when the template is rendered. Nothing else catches it."""

    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(
            gene_name="STMN2", protein_name="Stathmin 2", status="in_progress")
        company = Company.objects.using(DB).create(name="Proteintech")
        self.antibody = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="10586-1-AP", site_id=self.site.pk)
        self.line = CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.site.pk, experimenter_id=self.member.pk)

    def _paths(self):
        return [
            "/pipeline/start/", "/pipeline/overview/", "/pipeline/find/?q=STMN2x",
            "/pipeline/targets/board/", "/pipeline/antibodies/board/",
            "/pipeline/cell-lines/board/", "/pipeline/sessions/board/",
            "/pipeline/targets/guide/", "/pipeline/antibodies/guide/",
            "/pipeline/cell-lines/guide/", "/pipeline/sessions/guide/",
            "/pipeline/targets/portfolio/", "/pipeline/feasibility/",
            "/pipeline/data/", "/pipeline/recommendations/",
            "/pipeline/cropper/", "/pipeline/session/new/",
            f"/pipeline/target/{self.target.pk}/",
        ]

    def test_every_page_left_in_the_app_renders(self):
        for path in self._paths():
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertIn(
                    resp.status_code, (200, 302),
                    f"{path} returned {resp.status_code} — a template probably "
                    f"still reverses a retired URL name")

    def test_the_root_redirect_still_works(self):
        """`pipeline:dashboard` keeps its name and its redirect: bookmarks point
        at it and templates reverse it, even though the old view is gone."""
        resp = self.client.get("/pipeline/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"].split("?")[0], "/pipeline/targets/board/")
