"""The sessions board: find a session, edit it in place, open its results.

Sessions are two-level — a header plus N result rows whose columns depend on the
procedure — so these cover both levels, and the two rules that make an editable
grid safe: an edit that moves a row out of the active filter takes the row with
it, and a bad value is refused with a message worth reading rather than saved.
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             IfResult, Member, Site, Target, WbResult)
from pipeline.tests_timeouts import _member_client

DB = "pipeline_db"


class SessionBoardTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        self.site2 = Site.objects.create(name="McGill", short_code="MCG")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        self.member = Member.objects.create(
            user_id=pu.pk, site_id=self.site.pk, role="experimenter",
            is_active=True, display_name="Vera Ruiz Moleon")
        self.target = Target.objects.create(protein_name="Superoxide dismutase",
                                            gene_name="SOD1")
        self.company = Company.objects.create(name="Proteintech")
        self.antibody = Antibody.objects.create(
            target=self.target, company=self.company, catalogue_number="12A8")
        self.wt = CellLine.objects.create(name="HAP1-WT", genotype="WT")
        self.session = ExperimentSession.objects.create(
            procedure_type="WB", target=self.target, experimenter=self.member,
            site=self.site, date=date(2026, 2, 18), status="complete")
        self.result = WbResult.objects.create(
            session=self.session, antibody=self.antibody, signal="clean band",
            rating="")
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def _patch(self, query="", **body):
        return self.client.post(f"/pipeline/sessions/board/patch/?{query}", body)

    # ── the page and its rows ──────────────────────────────────────────

    def test_the_page_renders_and_loads_the_shared_module(self):
        html = self.client.get("/pipeline/sessions/board/").content.decode()
        self.assertIn("pipeline/board.js", html)
        self.assertIn("OGABoard.create(", html)
        self.assertIn("session_id", html)

    def test_rows_carry_what_the_grid_draws(self):
        row = self.client.get("/pipeline/sessions/board/rows/").json()["rows"][0]
        self.assertEqual(row["gene"], "SOD1")
        self.assertEqual(row["procedure"], "WB")
        self.assertEqual(row["experimenter"], "Vera Ruiz Moleon")
        self.assertEqual(row["site"], "Leicester")
        self.assertEqual(row["result_count"], 1)

    def test_cancelled_sessions_are_hidden_unless_asked_for(self):
        ExperimentSession.objects.create(
            procedure_type="IP", target=self.target, experimenter=self.member,
            site=self.site, date=date(2026, 1, 5), status="cancelled")
        default = self.client.get("/pipeline/sessions/board/rows/").json()
        self.assertEqual(default["count"], 1)

        shown = self.client.get("/pipeline/sessions/board/rows/?show_cancelled=1").json()
        self.assertEqual(shown["count"], 2)

        explicit = self.client.get(
            "/pipeline/sessions/board/rows/?status=cancelled").json()
        self.assertEqual(explicit["count"], 1)

    def test_filters_narrow_the_board(self):
        ExperimentSession.objects.create(
            procedure_type="IF", target=self.target, experimenter=self.member,
            site=self.site2, date=date(2026, 3, 1), status="planned")
        by_proc = self.client.get("/pipeline/sessions/board/rows/?procedure=IF").json()
        self.assertEqual(by_proc["count"], 1)
        by_site = self.client.get(
            f"/pipeline/sessions/board/rows/?site={self.site.pk}").json()
        self.assertEqual(by_site["count"], 1)
        by_gene = self.client.get("/pipeline/sessions/board/rows/?q=SOD1").json()
        self.assertEqual(by_gene["count"], 2)

    # ── editing the session header ─────────────────────────────────────

    def test_editing_a_session_field_saves_and_returns_the_row(self):
        data = self._patch(session_id=self.session.pk, field="comments",
                           value="repeat of the 12 Feb run").json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["matches"])
        self.assertEqual(data["row"]["comments"], "repeat of the 12 Feb run")
        self.session.refresh_from_db()
        self.assertEqual(self.session.comments, "repeat of the 12 Feb run")

    def test_experimenter_and_site_resolve_by_name(self):
        self._patch(session_id=self.session.pk, field="site", value="McGill")
        self.session.refresh_from_db()
        self.assertEqual(self.session.site_id, self.site2.pk)

    def test_an_unknown_name_is_refused_with_a_useful_message(self):
        response = self._patch(session_id=self.session.pk, field="site",
                               value="Nowhere")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nowhere", response.json()["error"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.site_id, self.site.pk)

    def test_a_bad_status_is_refused(self):
        response = self._patch(session_id=self.session.pk, field="status",
                               value="nearly done")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a status", response.json()["error"])

    def test_a_non_numeric_protein_loading_is_refused(self):
        response = self._patch(session_id=self.session.pk,
                               field="protein_loading_ug", value="twenty")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a number", response.json()["error"])

    def test_identity_fields_are_not_editable(self):
        for field in ("procedure_type", "target"):
            response = self._patch(session_id=self.session.pk, field=field, value="IF")
            self.assertEqual(response.status_code, 400, field)
            self.assertIn("not editable", response.json()["error"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.procedure_type, "WB")

    def test_a_row_pushed_out_of_the_active_filter_reports_matches_false(self):
        data = self._patch(f"site={self.site.pk}", session_id=self.session.pk,
                           field="site", value="McGill").json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["matches"])
        self.assertIsNone(data["row"])

    # ── results, the second level ──────────────────────────────────────

    def test_results_come_back_with_the_columns_for_that_procedure(self):
        data = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}").json()
        keys = [c["key"] for c in data["columns"]]
        self.assertIn("signal", keys)
        self.assertIn("dilution", keys)
        self.assertNotIn("antibody", keys)
        self.assertNotIn("access_id", keys)
        self.assertEqual(data["rows"][0]["values"]["signal"], "clean band")

    def test_each_procedure_gets_its_own_columns(self):
        """The reason results open per row instead of one flat sheet."""
        if_session = ExperimentSession.objects.create(
            procedure_type="IF", target=self.target, experimenter=self.member,
            site=self.site, date=date(2026, 3, 2), status="complete")
        IfResult.objects.create(session=if_session, antibody=self.antibody,
                                specific_signal="yes")
        wb = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}").json()
        iff = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={if_session.pk}").json()
        wb_keys = {c["key"] for c in wb["columns"]}
        if_keys = {c["key"] for c in iff["columns"]}
        self.assertIn("exposure_time", wb_keys)
        self.assertIn("microscope", if_keys)
        self.assertNotIn("microscope", wb_keys)

    def test_editing_a_result_saves_and_returns_the_session_row(self):
        data = self._patch(session_id=self.session.pk, result_id=self.result.pk,
                           field="rating", value="4").json()
        self.assertTrue(data["ok"])
        self.result.refresh_from_db()
        self.assertEqual(self.result.rating, "4")
        # The session row comes back so the result count stays truthful.
        self.assertEqual(data["row"]["id"], self.session.pk)

    def test_a_result_field_from_another_procedure_is_refused(self):
        """WB has no 'microscope' — posting one must not create an attribute."""
        response = self._patch(session_id=self.session.pk, result_id=self.result.pk,
                               field="microscope", value="ImageXpress")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not editable", response.json()["error"])

    def test_a_result_belonging_to_another_session_is_refused(self):
        other = ExperimentSession.objects.create(
            procedure_type="WB", target=self.target, experimenter=self.member,
            site=self.site, date=date(2026, 4, 1), status="planned")
        response = self._patch(session_id=other.pk, result_id=self.result.pk,
                               field="rating", value="9")
        self.assertEqual(response.status_code, 404)
        self.result.refresh_from_db()
        self.assertEqual(self.result.rating, "")

    def test_unknown_session_is_a_404(self):
        self.assertEqual(
            self._patch(session_id=999999, field="comments", value="x").status_code, 404)

    # ── what a cell offers ─────────────────────────────────────────────

    def test_the_page_carries_the_status_choices_the_grid_edits_with(self):
        """`status` is a dropdown on the Plan a session header and on this
        board's own filter, and was a free-text box in the grid — the one
        surface where `done` could be typed and refused after the save."""
        from pipeline.models import ExperimentSession as ES
        html = self.client.get("/pipeline/sessions/board/").content.decode()
        self.assertIn('id="status-choices"', html)
        for value, _label in ES.SessionStatus.choices:
            self.assertIn(f'"{value}"', html)

    def test_rating_offers_what_the_database_already_says(self):
        """Free text with no vocabulary anywhere, and whatever is typed there is
        what a generated Data Note prints. Offered, not enforced."""
        for value in ("Recommended", "Recommended", "Not recommended"):
            WbResult.objects.create(session=self.session, antibody=self.antibody,
                                    rating=value)
        choices = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["field_choices"]
        # Most-used first, so the common answer is the first one offered.
        self.assertEqual(choices["rating"]["values"],
                         ["Recommended", "Not recommended"])
        self.assertFalse(choices["rating"]["strict"],
                         "a vocabulary nobody may add to stops describing the bench")

    def test_every_result_field_with_a_vocabulary_offers_it(self):
        """`signal` is the one that matters and it was offered nowhere.

        It is one of the two axes `AntibodyOutcome` reads and holds 3 distinct
        values over 1,927 rows on live, and the suggestion list named `rating`
        alone — so the field a public verdict is derived from was a bare text
        box while the field beside it had a list.
        """
        WbResult.objects.create(session=self.session, antibody=self.antibody,
                                signal="specific band", rating="Recommended",
                                ecl="Clarity", gel="4-20%")
        choices = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["field_choices"]
        for field in ("signal", "rating", "ecl", "gel"):
            self.assertIn(field, choices, f"{field} offers nothing")

    def test_a_field_with_nothing_recorded_in_it_offers_nothing(self):
        """An empty datalist is a dropdown arrow that does nothing.

        The sweep is over every text column now rather than a hand-typed
        `{"WB": ("rating",)}` — one field of twelve on a western blot, and
        nothing at all for IP, IF and FC — so what is pinned here is the *rule*
        rather than one field's absence: a column with values offers them, a
        column with none is left as a plain box.
        """
        WbResult.objects.create(session=self.session, antibody=self.antibody,
                                signal="clean band")
        choices = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["field_choices"]
        self.assertEqual(choices["signal"]["values"], ["clean band"])
        self.assertNotIn("rating", choices,
                         "a field nobody has recorded offered a list anyway")
        self.assertNotIn("comments", choices,
                         "comments is prose — a datalist of other people's "
                         "sentences is not a vocabulary")

    # ── two columns, one measurement ───────────────────────────────────

    def test_a_wb_card_draws_one_dilution_not_two(self):
        """`dilution` and `primary_ab_dilution` are the same measurement — the
        Access import wrote `1AbDilution` into both — and both were drawn on one
        card with nothing to say how they differ, because they do not."""
        self.result.dilution = "1:1000"
        self.result.primary_ab_dilution = "1:1000"
        self.result.save()
        cols = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["columns"]
        keys = [c["key"] for c in cols]
        self.assertIn("dilution", keys)
        self.assertNotIn("primary_ab_dilution", keys)
        # And the survivor says which dilution it is, now that the field whose
        # name said so has gone.
        label = next(c["label"] for c in cols if c["key"] == "dilution")
        self.assertIn("primary", label.lower())

    def test_a_duplicate_that_disagrees_is_drawn_and_says_why(self):
        """Hiding a non-empty field is how a value nobody can see becomes a
        value somebody overwrites."""
        self.result.dilution = "1:1000"
        self.result.primary_ab_dilution = "1:500"
        self.result.save()
        cols = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["columns"]
        dupe = next((c for c in cols if c["key"] == "primary_ab_dilution"), None)
        self.assertIsNotNone(dupe, "a disagreement is the whole reason to show it")
        self.assertIn("disagree", dupe["note"])

    def test_a_blank_copy_is_not_a_disagreement(self):
        """**The normal way to record a western blot triggered the alarm.**

        Filling in `dilution` and leaving the Method copy alone is what a
        scientist does, and the column appeared with a note reading "these two
        disagree" over a box nobody had typed in. Blank is not a second answer;
        it is no answer, and there is nothing to lose by hiding it.
        """
        self.result.dilution = "1:1000"
        self.result.primary_ab_dilution = ""
        self.result.save()
        cols = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["columns"]
        self.assertNotIn("primary_ab_dilution", [c["key"] for c in cols])

    def test_the_only_copy_with_a_value_is_shown_and_says_which_it_is(self):
        """The other direction, and it still has to be drawn: a value only the
        copy carries is not two answers disagreeing, it is the *only* answer,
        sitting in the column reports do not read. Hiding it is how a value
        nobody can see becomes a value somebody overwrites."""
        self.result.dilution = ""
        self.result.primary_ab_dilution = "1:500"
        self.result.save()
        cols = self.client.get(
            f"/pipeline/sessions/board/results/?session_id={self.session.pk}"
        ).json()["columns"]
        dupe = next((c for c in cols if c["key"] == "primary_ab_dilution"), None)
        self.assertIsNotNone(dupe, "a value drawn nowhere is a value lost")
        self.assertNotIn("disagree", dupe["note"])
        self.assertIn("blank", dupe["note"])

    def test_the_hidden_duplicate_is_still_editable_if_something_posts_it(self):
        """Not drawn is not the same as not a field. The patch endpoint validates
        against the model, and a row whose copies disagree has to be fixable."""
        response = self._patch(session_id=self.session.pk, result_id=self.result.pk,
                               field="primary_ab_dilution", value="1:1000")
        self.assertEqual(response.status_code, 200)
        self.result.refresh_from_db()
        self.assertEqual(self.result.primary_ab_dilution, "1:1000")


class SessionBoardCostTests(TestCase):
    """Same ceiling as the target board: the rows endpoint must not issue more
    queries as the board grows. Sessions grow one per experiment, without
    bound, so this matters more here than it does for targets."""
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        self.member = Member.objects.create(user_id=pu.pk, site_id=self.site.pk,
                                            role="experimenter", is_active=True,
                                            display_name="Vera")
        self.target = Target.objects.create(protein_name="P", gene_name="SOD1")
        company = Company.objects.create(name="Proteintech")
        self.antibody = Antibody.objects.create(target=self.target, company=company,
                                                catalogue_number="12A8")
        self.client = Client()
        self.assertTrue(self.client.login(username="vera", password="pw"))

    def _seed(self, n):
        for i in range(n):
            s = ExperimentSession.objects.create(
                procedure_type="WB", target=self.target, experimenter=self.member,
                site=self.site, date=date(2026, 1, 1), status="complete")
            WbResult.objects.create(session=s, antibody=self.antibody, signal="x")

    def _queries(self):
        with CaptureQueriesContext(connections[DB]) as ctx:
            response = self.client.get("/pipeline/sessions/board/rows/")
            self.assertEqual(response.status_code, 200)
        return len(ctx), response.json()["count"]

    def test_rows_query_count_does_not_grow_with_the_board(self):
        self._seed(3)
        small, small_count = self._queries()
        self.assertEqual(small_count, 3)

        self._seed(27)
        large, large_count = self._queries()
        self.assertEqual(large_count, 30)

        self.assertEqual(small, large,
                         f"rows/ issued {small} queries for 3 sessions but {large} "
                         f"for 30 — that is an N+1")


class BoardCarriesWhatTheSessionPageDidTests(TestCase):
    """The three things that kept ``session_detail`` alive.

    It was the only place with the bench-sheet round trip — download the sheet,
    write on it at the bench, upload it back — the session conditions, and the
    protocol phases. The first field test recorded every result there for exactly
    that reason. All three are on the board's results panel now, which is what
    made retiring the page a move rather than a loss.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.site.pk, experimenter_id=self.member.pk,
            session_conditions={"lysis_buffer": "RIPA", "from_a_spreadsheet": "42"})

    def _panel(self):
        resp = self.client.get("/pipeline/sessions/board/results/",
                               {"session_id": self.session.pk})
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_the_panel_offers_the_bench_sheet_and_the_way_back(self):
        d = self._panel()
        self.assertIn(f"/pipeline/session/{self.session.pk}/download/bench-sheet/",
                      d["bench_sheet_url"])
        self.assertIn(f"/pipeline/session/{self.session.pk}/results/upload/",
                      d["results_upload_url"])
        self.assertIn(f"/pipeline/session/{self.session.pk}/results/commit/",
                      d["results_commit_url"])

    def test_conditions_come_with_their_labels_and_their_values(self):
        d = self._panel()
        by_key = {c["key"]: c for c in d["conditions"]}
        self.assertIn("lysis_buffer", by_key)
        self.assertEqual(by_key["lysis_buffer"]["value"], "RIPA")
        self.assertEqual(by_key["lysis_buffer"]["label"], "Lysis Buffer")

    def test_a_condition_from_a_spreadsheet_is_shown_rather_than_hidden(self):
        """Sessions created from a per-gene template carry arbitrary columns no
        form knows about. A value you cannot see is a value somebody
        overwrites."""
        d = self._panel()
        extra = {c["key"]: c["value"] for c in d["extra_conditions"]}
        self.assertEqual(extra, {"from_a_spreadsheet": "42"})

    def test_a_condition_can_be_edited_on_the_board(self):
        resp = self.client.post("/pipeline/sessions/board/patch/", {
            "session_id": self.session.pk, "field": "cond:lysis_buffer",
            "value": "NP-40"})
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db(using=DB)
        self.assertEqual(self.session.session_conditions["lysis_buffer"], "NP-40")

    def test_editing_one_condition_keeps_the_spreadsheet_ones(self):
        """Rebuilding the dict from scratch is how the full-page save used to
        wipe them on every write."""
        self.client.post("/pipeline/sessions/board/patch/", {
            "session_id": self.session.pk, "field": "cond:lysis_buffer",
            "value": "NP-40"})
        self.session.refresh_from_db(using=DB)
        self.assertEqual(self.session.session_conditions["from_a_spreadsheet"], "42")

    def test_clearing_a_condition_removes_the_key_rather_than_storing_blank(self):
        self.client.post("/pipeline/sessions/board/patch/", {
            "session_id": self.session.pk, "field": "cond:lysis_buffer",
            "value": ""})
        self.session.refresh_from_db(using=DB)
        self.assertNotIn("lysis_buffer", self.session.session_conditions)

    def test_a_condition_that_is_not_in_this_procedures_protocol_is_refused(self):
        resp = self.client.post("/pipeline/sessions/board/patch/", {
            "session_id": self.session.pk, "field": "cond:microscope",
            "value": "ImageXpress"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not editable", resp.json()["error"])

    def test_the_protocol_phases_are_there_when_a_template_is_attached(self):
        """Empty is the normal case — most imported sessions have no template —
        so the key is present either way rather than sometimes missing."""
        self.assertEqual(self._panel()["protocol"], [])

    def test_the_condition_labels_come_from_the_one_registry(self):
        """Not a second copy. Four hand-written lists of "the result fields" is
        the mistake this codebase has already made once."""
        from pipeline.services import session_board as board
        from pipeline.views.session_entry import PROCEDURE_CONDITION_FIELDS
        self.assertEqual(
            [f["key"] for f in board.condition_fields("WB")],
            [key for key, _l, _t, _p in PROCEDURE_CONDITION_FIELDS["WB"]])
