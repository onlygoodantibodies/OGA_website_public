"""The antibodies board: find an antibody, edit it, toggle what it's good for.

The rule with teeth here is that identity is not editable. ``(catalogue,
company, target, lot, site)`` is the antibody's key — retyping the catalogue
number in a grid cell would silently turn the row into a different antibody,
so the board refuses it. RRID is refused for the same class of reason: it is
written through ``rrid_utils`` so the bare AB_number and the registry link
cannot drift apart.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from pipeline.models import Antibody, Company, Member, Site, Target

DB = "pipeline_db"


class _BoardFixture:
    """One antibody on one bench, signed in — the fixture all three classes want.

    A mixin rather than a base class with tests in it. Subclassing
    ``AntibodyBoardTests`` to borrow its ``setUp`` also inherits its eleven
    tests, so every such subclass re-runs them under a new name: the module ran
    48 tests where it has 26, and a count that moves when nobody added coverage
    is the one signal this repo reads to catch an omission.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        self.site2 = Site.objects.create(name="McGill", short_code="MCG")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        Member.objects.create(user_id=pu.pk, site_id=self.site.pk,
                              role="experimenter", is_active=True,
                              display_name="Vera")
        self.target = Target.objects.create(protein_name="SOD", gene_name="SOD1")
        self.company = Company.objects.create(name="Proteintech")
        self.antibody = Antibody.objects.create(
            target=self.target, company=self.company, catalogue_number="12A8",
            clonality="polyclonal", host_species="rabbit", site=self.site)
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def _patch(self, query="", **body):
        return self.client.post(f"/pipeline/antibodies/board/patch/?{query}", body)


class AntibodyBoardTests(_BoardFixture, TestCase):

    def test_the_page_renders_on_the_shared_module(self):
        html = self.client.get("/pipeline/antibodies/board/").content.decode()
        self.assertIn("pipeline/board.js", html)
        self.assertIn("OGABoard.create(", html)

    def test_rows_carry_what_the_grid_draws(self):
        row = self.client.get("/pipeline/antibodies/board/rows/").json()["rows"][0]
        self.assertEqual(row["catalogue"], "12A8")
        self.assertEqual(row["company"], "Proteintech")
        self.assertEqual(row["gene"], "SOD1")
        self.assertEqual(row["recommended"], {"wb": False, "ip": False,
                                              "if": False, "fc": False})

    def test_filters_narrow_the_board(self):
        other_target = Target.objects.create(protein_name="Tau", gene_name="MAPT")
        Antibody.objects.create(target=other_target, company=self.company,
                                catalogue_number="99Z", wb_recommended=True)
        by_gene = self.client.get("/pipeline/antibodies/board/rows/?gene=MAPT").json()
        self.assertEqual(by_gene["count"], 1)
        by_app = self.client.get(
            "/pipeline/antibodies/board/rows/?application=wb").json()
        self.assertEqual(by_app["count"], 1)
        recommended = self.client.get(
            "/pipeline/antibodies/board/rows/?recommended=no").json()
        self.assertEqual(recommended["count"], 1)
        self.assertEqual(recommended["rows"][0]["catalogue"], "12A8")

    def test_editing_a_free_text_field_saves(self):
        data = self._patch(target_id=self.antibody.pk, field="lot_number",
                           value="B-4471").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["row"]["lot_number"], "B-4471")
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.lot_number, "B-4471")

    def test_toggling_a_recommendation(self):
        self._patch(target_id=self.antibody.pk, field="wb_recommended", value="yes")
        self.antibody.refresh_from_db()
        self.assertTrue(self.antibody.wb_recommended)

        self._patch(target_id=self.antibody.pk, field="wb_recommended", value="")
        self.antibody.refresh_from_db()
        self.assertFalse(self.antibody.wb_recommended)

    def test_site_resolves_by_name_and_a_bad_one_is_refused(self):
        self._patch(target_id=self.antibody.pk, field="site", value="McGill")
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.site_id, self.site2.pk)

        response = self._patch(target_id=self.antibody.pk, field="site",
                               value="Atlantis")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Atlantis", response.json()["error"])
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.site_id, self.site2.pk)

    def test_a_non_numeric_concentration_is_refused(self):
        response = self._patch(target_id=self.antibody.pk, field="concentration",
                               value="lots")
        self.assertEqual(response.status_code, 400)
        # "not a number" was the old wording, and it was wrong about the case
        # that matters: "1.0 mg/mL" *is* a number, with a unit. The refusal names
        # the units it takes, so it says what is right and not only what is
        # wrong — services/concentration.py.
        self.assertIn("not a concentration", response.json()["error"])
        self.assertIn("µg/mL", response.json()["error"])

    def test_identity_and_rrid_are_not_editable(self):
        for field in ("catalogue_number", "company", "target", "rrid", "rrid_link"):
            response = self._patch(target_id=self.antibody.pk, field=field,
                                   value="tampered")
            self.assertEqual(response.status_code, 400, field)
            self.assertIn("not editable", response.json()["error"])
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.catalogue_number, "12A8")
        self.assertEqual(self.antibody.rrid, "")

    def test_a_row_pushed_out_of_the_active_filter_reports_matches_false(self):
        data = self._patch(f"site={self.site.pk}", target_id=self.antibody.pk,
                           field="site", value="McGill").json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["matches"])
        self.assertIsNone(data["row"])

    def test_unknown_antibody_is_a_404(self):
        self.assertEqual(
            self._patch(target_id=999999, field="lot_number", value="x").status_code,
            404)

    def test_rows_query_count_does_not_grow_with_the_board(self):
        def queries():
            with CaptureQueriesContext(connections[DB]) as ctx:
                r = self.client.get("/pipeline/antibodies/board/rows/")
                self.assertEqual(r.status_code, 200)
            return len(ctx), r.json()["count"]

        small, small_n = queries()
        for i in range(30):
            Antibody.objects.create(target=self.target, company=self.company,
                                    catalogue_number=f"BULK-{i}")
        large, large_n = queries()
        self.assertEqual((small_n, large_n), (1, 31))
        self.assertEqual(small, large,
                         f"rows/ issued {small} queries for 1 antibody but {large} "
                         f"for 31 — that is an N+1")


class InKindOrPurchasedTests(_BoardFixture, TestCase):
    """Whether the supplier contributed the vial or the lab bought it.

    `Antibody.acquisition_method` has recorded this since the Access import and
    is filled on every live row — 3,034 in kind, 146 purchased, 81 unknown —
    and it was drawn on no screen and carried in no sheet. So uOttawa asked for
    the field believing the portal had never had it: *"I don't recall that ever
    being specifically mentioned anywhere except us knowing it internally"*
    (4 Sep 2026). Third time in one week for that shape, after the freezer box,
    the received date and how a cell line grows.

    A closed set of three, so the pin is both halves of enforcing one — the
    picker narrows what a person can send, and the writer decides what is
    stored. `clonality` is the reason that is two assertions and not one: it
    had the picker, had no writer-side check, and saved `mono` verbatim.
    """

    def test_the_cell_is_editable_and_stores_the_code(self):
        resp = self._patch(target_id=self.antibody.pk,
                           field="acquisition_method", value="purchased")
        self.assertEqual(resp.status_code, 200)
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.acquisition_method, "purchased")

    def test_the_label_off_the_board_is_accepted_back(self):
        """The grid shows *In Kind* and a person may well type that."""
        resp = self._patch(target_id=self.antibody.pk,
                           field="acquisition_method", value="In Kind")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.acquisition_method, "in_kind")

    def test_anything_else_is_refused_by_name_and_nothing_is_stored(self):
        self.antibody.acquisition_method = "in_kind"
        self.antibody.save(using=DB)
        resp = self._patch(target_id=self.antibody.pk,
                           field="acquisition_method", value="free sample")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()["error"]
        self.assertIn("free sample", body)
        self.assertIn("in_kind", body)
        self.assertIn("purchased", body)
        self.antibody.refresh_from_db()
        self.assertEqual(self.antibody.acquisition_method, "in_kind")

    def test_the_picker_offers_the_three_and_the_row_carries_the_code(self):
        from pipeline.services import antibody_board
        offered = antibody_board.cell_choices()["acquisition_method"]
        self.assertEqual({v["value"] for v in offered["values"] if v["value"]},
                         {"in_kind", "purchased", "unknown"})
        self.antibody.acquisition_method = "purchased"
        self.antibody.save(using=DB)
        row = antibody_board.row_for(self.antibody)
        self.assertEqual(row["acquisition_method"], "purchased")

    def test_the_sheet_carries_it_out_and_reads_it_back(self):
        """It is in the download under a heading the parser answers to, and the
        stored code is what the sheet emits — not the label, for the same
        reason clonality emits the enum: `dataset.py` diffs against the stored
        column, so a label would preview a change on every row."""
        from pipeline.services import board_columns
        from pipeline.services.cropper import metadata as meta

        self.assertIn("acquisition", board_columns.sheet_headers("antibodies"))
        self.antibody.acquisition_method = "in_kind"
        self.antibody.save(using=DB)
        col = next(c for c in board_columns.registry("antibodies")
                   if c.key == "acquisition_method")
        self.assertEqual(col.value(self.antibody), "in_kind")
        self.assertEqual(meta.header_field("Acquisition"), "acquisition_raw")

    def test_a_blank_cell_never_overwrites_what_is_recorded(self):
        """The column left empty on a sheet means *this file does not say*, not
        *unknown* — 3,034 rows would be wrong if those collapsed together."""
        from pipeline.services.cropper import metadata as meta
        self.assertEqual(meta.parse_acquisition(""), "")
        self.assertEqual(meta.parse_acquisition("   "), "")
        # …while somebody recording that nobody knows still reads as unknown.
        self.assertEqual(meta.parse_acquisition("unknown"), "unknown")


class RenumberEndpointTests(_BoardFixture, TestCase):
    """Assign or rearrange A-numbers over the **whole filtered set**.

    Two things here are silent if they go wrong, and neither shows on a screen.

    *The set is the filters, not the drawn page.* `page` is board state, and a
    renumbering that quietly covered fifty of ninety rows would leave the rest
    holding numbers from the old order — worse than not doing it at all. The
    same rule a download follows.

    *The filter that makes the feature usable has to be on the form.*
    `board.js` rebuilds its query from the filter form on every rows fetch, so
    `?numbered=no` — the way a bench finds the rows it has just logged — is
    dropped by the first fetch unless the form carries a control for it.
    """

    def _rows(self, **kw):
        from pipeline.services import lab_numbers
        made = []
        for i in range(kw.get("n", 3)):
            ab = Antibody(target=self.target, company=self.company,
                          catalogue_number=f"RN-{i}", site=self.site)
            lab_numbers.withhold(ab)
            ab.save()
            made.append(ab)
        return made

    def _post(self, path, query="", **body):
        return self.client.post(f"/pipeline/antibodies/board/{path}?{query}", body)

    def test_the_numbered_filter_is_a_control_on_the_form(self):
        html = self.client.get("/pipeline/antibodies/board/").content.decode()
        form = html.split('id="filters"', 1)[1].split("</form>", 1)[0]
        self.assertIn('name="numbered"', form)
        self.assertIn('value="no"', form)

    def test_the_plan_covers_every_filtered_row_not_the_drawn_page(self):
        self._rows(n=3)
        d = self._post("renumber/", "gene=SOD1&numbered=no&per_page=1&page=1").json()
        self.assertTrue(d["ok"], d)
        # One row per page, three rows in the filter: all three are planned.
        self.assertEqual(d["moving"], 3)

    def test_the_plan_writes_nothing(self):
        made = self._rows(n=2)
        self._post("renumber/", "gene=SOD1&numbered=no", start="50")
        self.assertEqual([Antibody.objects.get(pk=a.pk).ab_number for a in made],
                         [None, None])

    def test_applying_writes_the_numbers_the_preview_showed(self):
        made = self._rows(n=2)
        shown = self._post("renumber/", "gene=SOD1&numbered=no", start="50").json()
        done = self._post("renumber/apply/", "gene=SOD1&numbered=no", start="50",
                          consented_count=shown["moving"],
                          stamp=shown["stamp"]).json()
        self.assertTrue(done["ok"], done)
        numbers = sorted(Antibody.objects.filter(pk__in=[a.pk for a in made])
                         .values_list("ab_number", flat=True))
        self.assertEqual(numbers, [50, 51])

    def test_a_consent_count_that_no_longer_matches_is_refused(self):
        made = self._rows(n=2)
        r = self._post("renumber/apply/", "gene=SOD1&numbered=no", start="50",
                       consented_count=1)
        self.assertEqual(r.status_code, 400)
        self.assertIn("not the 1", r.json()["error"])
        self.assertEqual([Antibody.objects.get(pk=a.pk).ab_number for a in made],
                         [None, None])

    def test_a_first_number_that_is_not_a_number_says_what_is_accepted(self):
        self._rows(n=1)
        r = self._post("renumber/", "gene=SOD1&numbered=no", start="A-RUN11-01")
        self.assertEqual(r.status_code, 400)
        self.assertIn("A-RUN11-01", r.json()["error"])

    def test_another_benchs_antibodies_are_not_yours_to_renumber(self):
        """Vera is at Leicester; these are McGill's. The same rule as deleting —
        these numbers are written on somebody else's freezer boxes."""
        from pipeline.services import lab_numbers
        theirs = Antibody(target=self.target, company=self.company,
                          catalogue_number="RN-theirs", site=self.site2)
        lab_numbers.withhold(theirs)
        theirs.save()

        d = self._post("renumber/", f"site={self.site2.pk}", start="1").json()
        self.assertIn("McGill", d["refusal"])
        r = self._post("renumber/apply/", f"site={self.site2.pk}", start="1",
                       consented_count=1)
        self.assertEqual(r.status_code, 400)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.ab_number)

    def test_both_endpoints_refuse_a_signed_out_reader(self):
        self.client.logout()
        for path in ("renumber/", "renumber/apply/"):
            r = self.client.post(f"/pipeline/antibodies/board/{path}")
            self.assertIn(r.status_code, (302, 403), path)

    def test_planning_does_not_issue_a_query_per_row(self):
        """This runs over the whole filtered set, and an unfiltered press is the
        whole board. `_held_back` asks seven relations, and asking them a row at
        a time is seven queries an antibody — twenty thousand on live, with
        nothing on the page to say why it had stopped responding."""
        def queries(n):
            self._rows(n=n)
            with CaptureQueriesContext(connections[DB]) as ctx:
                r = self._post("renumber/", "gene=SOD1&numbered=no", start="500")
                self.assertTrue(r.json()["ok"])
            return len(ctx)

        Antibody.objects.filter(catalogue_number__startswith="RN-").delete()
        small = queries(2)
        Antibody.objects.filter(catalogue_number__startswith="RN-").delete()
        large = queries(20)
        self.assertEqual(small, large,
                         f"planning issued {small} queries for 2 antibodies but "
                         f"{large} for 20 — that is a query per row")
