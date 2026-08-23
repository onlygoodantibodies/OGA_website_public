"""Deleting a record, and — mostly — refusing to.

Carl Laflamme: *"we created a mock gene USP8 that could be removed (not sure how
to remove a target entry)"*. He was not missing a button: nothing in the app
could delete anything, so an honest mistake needed a developer and a shell.

The reason it was left out is in the schema, and it is a good one. **Every
foreign key into these models is CASCADE or SET_NULL**, so `target.delete()` on
a gene with work behind it removes every antibody, session and reading in one
statement — and the SET_NULL ones leave a session that still exists and no
longer knows which cell line it was run against.

So what is pinned here is who may delete what, and that nothing is destroyed
without the panel first naming every casualty — including the ones that survive
with a hole in them, which no count of deleted rows can see.
"""
from __future__ import annotations

from django.test import TestCase

from pipeline.models import (Antibody, CellLine, CellLineVial, Company,
                             ExperimentSession, Member, PublicationImage, Site,
                             Target, TargetNomination, WbResult)
from pipeline.services import deletion
from pipeline.tests_timeouts import DB, _member_client


class AMockGeneCanBeRemovedTests(TestCase):
    """The case Carl actually had: a gene tried out and not pursued."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="uOttawa", short_code="UOT")
        self.client = _member_client(self, self.site)
        self.mock = Target.objects.using(DB).create(gene_name="USP8")
        TargetNomination.objects.using(DB).create(
            target_id=self.mock.pk, site_id=self.site.pk, funded=False)

    def _preview(self, kind, pk):
        return self.client.post("/pipeline/records/delete/preview/",
                                {"kind": kind, "id": pk}).json()

    def _delete(self, kind, pk, confirm):
        return self.client.post("/pipeline/records/delete/",
                                {"kind": kind, "id": pk, "confirm": confirm})

    def test_its_own_nomination_does_not_block_it(self):
        """Every target created through either door has one, so refusing on it
        would mean no target could ever be deleted."""
        plan = self._preview("target", self.mock.pk)["plan"]
        self.assertTrue(plan["allowed"])
        self.assertIn(("site nominations", 1),
                      [(o["noun"], o["count"]) for o in plan["owned"]])

    def test_typing_the_gene_removes_it(self):
        resp = self._delete("target", self.mock.pk, "USP8")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.mock.pk).exists())
        self.assertFalse(TargetNomination.objects.using(DB).exists())

    def test_the_confirmation_cannot_be_given_by_reflex(self):
        """Not a checkbox and not an "are you sure" — both are one click.

        Case matters: `usp8` is not what the dialog showed. So does saying the
        record's own name rather than a magic word — "DELETE" typed into the
        wrong dialog would delete the wrong thing, and a gene symbol cannot.
        """
        for wrong in ("", "usp8", "yes", "DELETE", "SNCA"):
            with self.subTest(typed=wrong):
                resp = self._delete("target", self.mock.pk, wrong)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("exactly", resp.json()["error"])
                self.assertTrue(
                    Target.objects.using(DB).filter(pk=self.mock.pk).exists())

    def test_surrounding_whitespace_is_forgiven(self):
        """A trailing space off a copy-paste is not an accidental deletion — the
        person typed the gene. Trimmed deliberately, and only trimmed."""
        resp = self._delete("target", self.mock.pk, "  USP8 ")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.mock.pk).exists())

    def test_a_get_never_deletes(self):
        """A link a prefetch can follow is not a delete button."""
        self.assertEqual(
            self.client.get("/pipeline/records/delete/",
                            {"kind": "target", "id": self.mock.pk,
                             "confirm": "USP8"}).status_code, 405)
        self.assertTrue(Target.objects.using(DB).filter(pk=self.mock.pk).exists())


class WorkBehindARecordIsNamedBeforeItGoesTests(TestCase):
    """The first design refused outright. The owner's decision on 3 Aug turned
    the wall into a manifest — for a superuser, and then for a member on their
    own bench's records — so what these pin is that the panel *says* what is
    behind a record, in the counts a scientist would recognise."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        # Nominated at my site: a target belongs to whoever nominated it, and
        # one with no nomination belongs to nobody (there is a test for that).
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab1", site_id=self.site.pk)

    def _preview(self, kind, pk):
        return self.client.post("/pipeline/records/delete/preview/",
                                {"kind": kind, "id": pk}).json()["plan"]

    def test_a_gene_with_an_antibody_is_a_warning_not_a_plain_delete(self):
        """It is Leicester's gene and the antibody is Leicester's, so a
        Leicester member may remove it — behind the red panel, never behind the
        ordinary "nothing else points at this" one."""
        plan = self._preview("target", self.target.pk)
        self.assertTrue(plan["allowed"])
        self.assertTrue(plan["overriding"])
        # "1 antibody", not "1 antibodies" — the noun agrees with its count on
        # the one panel a person reads immediately before destroying something.
        self.assertIn("antibody",
                      " ".join(c["noun"] for c in plan["cascade"]).lower())
        self.assertIn("cannot be undone", plan["why"])

    def test_an_antibody_with_a_reading_names_the_reading(self):
        session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=self.member.pk)
        WbResult.objects.using(DB).create(session_id=session.pk,
                                          antibody_id=self.ab.pk)
        plan = self._preview("antibody", self.ab.pk)
        self.assertTrue(plan["overriding"])
        self.assertIn("result", " ".join(c["noun"] for c in plan["cascade"]).lower())

    def test_an_antibody_with_a_published_figure_names_the_figure(self):
        PublicationImage.objects.using(DB).update_or_create(
            antibody_id=self.ab.pk, application_type="WB", defaults={})
        plan = self._preview("antibody", self.ab.pk)
        self.assertTrue(plan["overriding"])
        self.assertTrue(plan["cascade_total"] >= 1)

    def test_a_wild_type_says_the_knockout_would_lose_its_parent(self):
        """**The case the manifest alone gets wrong.**

        `parent_line` is SET_NULL, so nothing cascades off a wild type — a panel
        built from the delete count says "will also delete nothing else" while
        the knockout survives with no parent recorded and no trace that one was
        ever there. The orphan list is the other half of the sentence.
        """
        wt = CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                               site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk,
            parent_line_id=wt.pk, site_id=self.site.pk)
        plan = self._preview("cell-line", wt.pk)
        self.assertTrue(plan["overriding"])
        self.assertEqual(plan["cascade_total"], 0, "nothing cascades off a WT")
        self.assertEqual(plan["orphaned_total"], 1)
        self.assertIn("parent line",
                      " ".join(o["noun"] for o in plan["orphaned"]))
        self.assertIn("leave", plan["why"])

    def test_every_noun_on_the_panel_agrees_with_its_count(self):
        """**Next to a number is where a grammar slip costs most**, and this is
        the panel a person studies immediately before destroying something — so
        a reader who distrusts the wording distrusts the number, which is the
        one thing on the screen they have to trust.

        Both halves, because they are printed side by side and were written
        apart: the cascade took `verbose_name_plural` whatever the count (*"1
        cell lines"*, on the day a gene started taking its knockout with it),
        and the orphan wordings were fixed plural phrases. Fixing one and not
        the other puts a correct and an incorrect count in one list.
        """
        wt = CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                               site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk,
            parent_line_id=wt.pk, site_id=self.site.pk)
        ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=self.member.pk,
            cell_line_wt_id=wt.pk)

        for kind, pk in (("target", self.target.pk), ("cell-line", wt.pk)):
            plan = self._preview(kind, pk)
            for entry in plan["cascade"] + plan["orphaned"]:
                with self.subTest(kind=kind, noun=entry["noun"]):
                    first = entry["noun"].split()[0]
                    self.assertEqual(
                        first.endswith("s"), entry["count"] != 1,
                        f"{entry['count']} {entry['noun']}")

    def test_a_cell_line_a_session_used_says_the_session_would_forget_it(self):
        line = CellLine.objects.using(DB).create(name="HeLa", genotype="WT",
                                                 site_id=self.site.pk)
        ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=self.member.pk,
            cell_line_wt_id=line.pk)
        plan = self._preview("cell-line", line.pk)
        self.assertTrue(plan["overriding"])
        self.assertEqual(plan["orphaned_total"], 1)
        self.assertIn("wild type",
                      " ".join(o["noun"] for o in plan["orphaned"]))

    def test_a_gene_takes_its_knockouts_with_it_rather_than_orphaning_them(self):
        """**The one nobody would guess from the schema, and saying it was not
        enough.**

        `CellLine.target` is SET_NULL, so deleting a gene did not delete its
        knockouts — it blanked their gene. The panel said so, in those words,
        and TRPA1 was deleted after a field test anyway, because what the
        sentence describes does not sound like it leaves a broken row. It does:
        a cell line with no gene *is* a wild type everywhere else in this app,
        so the survivor could not be found by the gene it was made against, read
        as a parental line while still flagged genotype KO, and collided by name
        with the knockout added when the gene went back in — producing a refusal
        naming two options spelled identically.

        So a knockout is part of its gene and goes with it, and the manifest
        counts it under what will be *deleted* rather than under what will be
        left behind.
        """
        CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.target.pk,
            site_id=self.site.pk)
        plan = self._preview("target", self.target.pk)
        self.assertTrue(plan["overriding"])
        self.assertIn("cell line", [c["noun"] for c in plan["cascade"]],
                      "a knockout must be listed as deleted, not as orphaned")
        self.assertNotIn(
            "no gene recorded", " ".join(o["noun"] for o in plan["orphaned"]),
            "a row that is being deleted is not a row left behind broken")

    def test_a_gene_never_takes_a_wild_type_with_it(self):
        """The other half, and the one that makes the rule safe to have.

        A parental is shared between every knockout made from it, is recorded
        once with no gene, and **96 wild types point at the Access-era
        placeholder target called `NA`** — so cascading off a target would
        delete most of the consortium's parental lines in one press. Only lines
        that are not wild types go.
        """
        wt = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", target_id=self.target.pk,
            site_id=self.site.pk)
        plan = self._preview("target", self.target.pk)
        self.assertIn("wild type",
                      " ".join(o["noun"] for o in plan["orphaned"]),
                      "a wild type stays, and the panel says its gene goes")
        self.assertTrue(CellLine.objects.using(DB).filter(pk=wt.pk).exists())

    def test_every_set_null_into_a_deletable_model_is_covered(self):
        """A SET_NULL added to the schema later must not fire behind the
        *ordinary* dialog.

        The orphan list is only drawn when the plan is an override, and what
        makes it one is the blocker count — so a SET_NULL nothing counts is one
        whose damage happens behind a panel reading "nothing else points at
        this". Django's own metadata is the list, and `_ORPHAN_WORDING` is
        checked against it here rather than trusted, because a relation added in
        a migration would otherwise print the derived fallback (`… would lose
        their target`) on the one screen where the words matter most.
        """
        from django.db.models.deletion import SET_NULL

        from pipeline.services.deletion import _ORPHAN_WORDING, _PLANNERS
        found = set()
        for _kind, (model, _planner) in _PLANNERS.items():
            for rel in model._meta.related_objects:
                if rel.on_delete is SET_NULL:
                    found.add((rel.related_model._meta.model_name,
                               rel.field.name))
        self.assertTrue(found)
        self.assertEqual(
            found - set(_ORPHAN_WORDING), set(),
            "a SET_NULL relation with no wording — the panel would print the "
            "derived fallback for it")

    def test_the_na_placeholder_is_not_printed_as_a_gene(self):
        """96 wild types point at a placeholder Target called `NA`, so a dialog
        naming the row by `target.gene_name` reads "HAP1 NA WT" — on the one
        screen whose job is to say what is about to be destroyed. Every reader
        that prints a gene goes through `services/targets.py::gene_of`."""
        na = Target.objects.using(DB).create(gene_name="NA",
                                             protein_name="Not applicable")
        line = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", target_id=na.pk, site_id=self.site.pk)
        self.assertEqual(self._preview("cell-line", line.pk)["label"], "HAP1")

    def test_a_record_nothing_points_at_gets_no_warning_box_at_all(self):
        """The red panel has to mean something, so it must not appear on the
        mock gene that is the ordinary case."""
        plain = Target.objects.using(DB).create(gene_name="USP8")
        TargetNomination.objects.using(DB).create(
            target_id=plain.pk, site_id=self.site.pk, funded=False)
        plan = self._preview("target", plain.pk)
        self.assertTrue(plan["allowed"])
        self.assertFalse(plan["overriding"])
        self.assertEqual(plan["cascade"], [])
        self.assertEqual(plan["orphaned"], [])

    def test_a_cell_lines_own_batches_go_with_it(self):
        line = CellLine.objects.using(DB).create(name="U2OS", genotype="WT",
                                                 site_id=self.site.pk)
        CellLineVial.objects.using(DB).create(cell_line_id=line.pk, c_number=7)
        plan = self._preview("cell-line", line.pk)
        self.assertTrue(plan["allowed"])
        self.assertIn(("freeze-down batches", 1),
                      [(o["noun"], o["count"]) for o in plan["owned"]])


class ASessionCountsItsReadingsRatherThanRefusingTests(TestCase):
    """A session's results *are* the session, so refusing on them would leave
    only empty sessions deletable — and one recorded against the wrong gene is
    exactly the case this exists for. Counted loudly instead."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)
        company = Company.objects.using(DB).create(name="Abcam")
        self.ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab1", site_id=self.site.pk)
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=self.member.pk)
        WbResult.objects.using(DB).create(session_id=self.session.pk,
                                          antibody_id=self.ab.pk)

    def test_the_readings_are_named_in_the_confirmation(self):
        plan = self.client.post("/pipeline/records/delete/preview/",
                                {"kind": "session", "id": self.session.pk}).json()["plan"]
        self.assertTrue(plan["allowed"])
        self.assertIn(("result rows", 1),
                      [(o["noun"], o["count"]) for o in plan["owned"]])
        # And it is the session number you have to type, not "yes".
        self.assertEqual(plan["confirm_with"], str(self.session.pk))

    def test_deleting_it_does_not_touch_the_antibody(self):
        self.client.post("/pipeline/records/delete/",
                         {"kind": "session", "id": self.session.pk,
                          "confirm": str(self.session.pk)})
        self.assertFalse(
            ExperimentSession.objects.using(DB).filter(pk=self.session.pk).exists())
        self.assertTrue(Antibody.objects.using(DB).filter(pk=self.ab.pk).exists())


class TheCheckIsAskedTwiceTests(TestCase):
    """A preview is not a permission slip.

    Two different things can change while the dialog sits open, and they need
    two different checks. **Who it belongs to** — another site's antibody
    arriving under the gene — is caught by re-asking the whole plan. **How much
    is behind it** is not: that answer stays *yes, you may* while the number
    grows, so somebody who agreed to "delete it and 3 other records" would
    silently destroy thirty. The number the panel showed comes back with the
    commit and has to still be true.
    """

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.other = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.client = _member_client(self, self.site)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)

    def _commit(self, **extra):
        return self.client.post(
            "/pipeline/records/delete/",
            {"kind": "target", "id": self.target.pk, "confirm": "SNCA", **extra})

    def test_another_sites_record_arriving_after_the_preview_stops_the_commit(self):
        plan = self.client.post("/pipeline/records/delete/preview/",
                                {"kind": "target", "id": self.target.pk}).json()["plan"]
        self.assertTrue(plan["allowed"])

        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-late", site_id=self.other.pk)

        resp = self._commit()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("McGill", resp.json()["error"])
        self.assertTrue(Target.objects.using(DB).filter(pk=self.target.pk).exists())

    def test_a_number_that_grew_since_the_panel_drew_it_stops_the_commit(self):
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-1", site_id=self.site.pk)
        plan = self.client.post("/pipeline/records/delete/preview/",
                                {"kind": "target", "id": self.target.pk}).json()["plan"]
        agreed = plan["cascade_total"] + plan["orphaned_total"]
        self.assertTrue(plan["overriding"])

        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-2", site_id=self.site.pk)

        resp = self._commit(agreed=agreed)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("changed while that panel was open", resp.json()["error"])
        self.assertTrue(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.assertEqual(Antibody.objects.using(DB).count(), 2)

    def test_an_override_with_no_number_at_all_is_refused(self):
        """A POST with no dialog behind it cannot have agreed to anything."""
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-1", site_id=self.site.pk)
        resp = self._commit()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("how much", resp.json()["error"])
        self.assertTrue(Target.objects.using(DB).filter(pk=self.target.pk).exists())

    def test_the_right_number_goes_through(self):
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-1", site_id=self.site.pk)
        plan = self.client.post("/pipeline/records/delete/preview/",
                                {"kind": "target", "id": self.target.pk}).json()["plan"]
        resp = self._commit(agreed=plan["cascade_total"] + plan["orphaned_total"])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.assertFalse(Antibody.objects.using(DB).exists())

    def test_a_plain_delete_needs_no_number(self):
        """The mock gene nobody has worked on stays a two-click job."""
        self.assertEqual(self._commit().status_code, 200)

    def test_an_unknown_kind_is_refused(self):
        resp = self.client.post("/pipeline/records/delete/",
                                {"kind": "member", "id": 1, "confirm": "x"})
        self.assertEqual(resp.status_code, 400)


class TheDeleteIsReachableFromThePagesTests(TestCase):
    """A routed endpoint is not a reachable one — the rule that cost this repo
    five field tests with a download-only workbook whose importer sat written,
    routed and tested with nothing posting to it."""

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def test_a_gene_is_deleted_from_its_own_page_and_not_from_the_list(self):
        """Owner's call after using it: *"one should probably only delete a
        target from its own target page (not the board)"*.

        The warning box was right; the place was not. The board is 585 rows
        scrolled past at speed, so a control on each row is a control beside
        every gene nobody was thinking about. A gene's own page is about that
        gene alone, so having arrived is already the deliberate act.
        """
        target = Target.objects.using(DB).create(gene_name="USP8")
        page = self.client.get(f"/pipeline/target/{target.pk}/").content.decode()
        self.assertIn("OGABoard.deleteDialog", page)
        self.assertIn("/pipeline/records/delete/preview/", page)
        self.assertIn("target-delete-btn", page)

        board = self.client.get("/pipeline/targets/board/").content.decode()
        self.assertNotIn("OGABoard.deleteDialog", board)
        self.assertNotIn('class="delete-row', board,
                         "the target board still deletes from the list")

    def test_the_child_boards_only_offer_it_with_one_gene_in_front_of_you(self):
        """*"and delete should probably only be an option for antibodies or cell
        lines or sessions if you are filtered to just one gene"*.

        "Impossible to trigger accidentally" is not a property of the
        confirmation alone: a Delete on every row of a three-thousand-row board
        is a Delete beside every record you were not thinking about.

        The gate is read from the **form on every draw**, not from a
        server-rendered flag, because the Gene box can be cleared without
        pressing Apply and the grid redraws from the form after an add or a
        delete — a page rendered narrowed would otherwise repaint the whole
        dataset with a Delete on all of it.
        """
        for url in ("/pipeline/antibodies/board/", "/pipeline/cell-lines/board/",
                    "/pipeline/sessions/board/"):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn("OGABoard.deleteDialog", body)
                self.assertIn('class="delete-row', body)
                # The control is inside the gate, not merely present.
                self.assertIn("OGABoard.oneGene(filtersForm) ?", body)
                gated = body.split("OGABoard.oneGene(filtersForm) ?", 1)[1][:400]
                self.assertIn('class="delete-row', gated,
                              "the Delete sits outside the gene gate")
                # And the absence explains itself rather than reading as a
                # missing feature.
                self.assertIn('id="delete-gate"', body)
                self.assertIn("OGABoard.geneGate(", body)

    def test_a_wild_type_stays_reachable_because_na_narrows_to_them(self):
        """**The gate would have made every wild type undeletable.**

        A wild type has no gene, so `filter(target__gene_name=…)` can never
        return one — the rule this codebase has been bitten by four times — and
        deleting on a child board is gated on exactly that filter. Every
        parental line would have been permanently out of reach, including the
        stray `SH-SY5Y WT [COWORK RUN4]` a purge could not touch either.

        `NA` is already the app's word for "there isn't one" in a gene column
        (`services/targets.py::NOT_APPLICABLE`), so it means the wild types on
        this filter too: the existing convention reaching one more place, not a
        special case invented for the gate.
        """
        from pipeline.models import CellLine
        from pipeline.services import cell_line_board
        gene = Target.objects.using(DB).create(gene_name="SNCA")
        na = Target.objects.using(DB).create(gene_name="NA",
                                             protein_name="Not applicable")
        homeless = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk)
        placeheld = CellLine.objects.using(DB).create(
            name="U2OS", genotype="WT", target_id=na.pk, site_id=self.site.pk)
        ko = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=gene.pk, site_id=self.site.pk)

        found = set(cell_line_board.apply_filters(
            CellLine.objects.using(DB).all(), gene="NA")
            .values_list("pk", flat=True))
        # Both shapes of "no gene": none at all, and the Access-era placeholder
        # target that 96 of the 136 real wild types still point at.
        self.assertEqual(found, {homeless.pk, placeheld.pk})
        self.assertNotIn(ko.pk, found)

        # And a real gene still means that gene.
        self.assertEqual(
            set(cell_line_board.apply_filters(
                CellLine.objects.using(DB).all(), gene="SNCA")
                .values_list("pk", flat=True)),
            {ko.pk})

    def test_the_board_says_na_finds_the_lines_with_no_gene(self):
        """A convention stated only in a placeholder is not stated — it is
        hidden the moment the cell has a value.

        And it says what the filter *does*. It used to say `NA` showed "the wild
        types", which is what the filter is for and not what it matches: any row
        with no target. The eleventh field test found a knockout stranded there
        by a deleted gene, read the hint, and concluded the row was unreachable
        from the board — a message that sends you somewhere has to send you
        where the record is.
        """
        body = self.client.get("/pipeline/cell-lines/board/").content.decode()
        self.assertIn("NA", body)
        self.assertIn("no gene recorded", body)

    def test_the_button_is_dead_until_the_box_is_ticked(self):
        """A warning box with a second, explicit press — the owner's call after
        using it. The manifest sits between the two presses."""
        from pathlib import Path
        js = Path("pipeline/static/pipeline/board.js").read_text()
        block = js[js.index("function deleteDialog("):]
        block = block[:block.index("\n  function identityDialog(")]
        self.assertIn("el('ack').checked", block)
        self.assertIn("el('go').disabled = true", block)
        # And the commit refuses if the tick was somehow bypassed client-side.
        self.assertIn("if (!el('ack').checked) return;", block)

    def test_the_shared_file_asks_before_it_offers(self):
        from pathlib import Path
        js = Path("pipeline/static/pipeline/board.js").read_text()
        # The preview is fetched when the panel opens, and the typed
        # confirmation only appears once the server has allowed it.
        block = js[js.index("function deleteDialog("):]
        block = block[:block.index("\n  function identityDialog(")]
        self.assertIn("cfg.urls.preview", block)
        self.assertIn("p.allowed", block)
        self.assertIn("cfg.urls.commit", block)
        # The typed box appears only after the server has allowed it.
        self.assertLess(block.index("p.allowed"), block.index("el('word')"))
        # No confirm() anywhere: one reflexive click is what this replaces.
        self.assertNotIn("confirm(", block)


class YouMayDeleteYourOwnBenchsRecordsTests(TestCase):
    """Own site, or superuser.

    Nothing pointing at a row makes it *safe* to remove; it does not make it
    yours. With five sites and uOttawa arriving, "who noticed it first" is the
    wrong answer to "whose record is this" — the boards already treat site as
    half of what makes a row the row it is.
    """

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.mine = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.theirs = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.client = _member_client(self, self.mine)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="SNCA")

    def _ab(self, site, cat):
        return Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number=cat, site_id=site.pk)

    def _preview(self, kind, pk, client=None):
        return (client or self.client).post(
            "/pipeline/records/delete/preview/",
            {"kind": kind, "id": pk}).json()["plan"]

    def test_your_own_sites_record_can_go(self):
        ab = self._ab(self.mine, "ab-mine")
        self.assertTrue(self._preview("antibody", ab.pk)["allowed"])

    def test_another_sites_record_is_refused_by_name(self):
        ab = self._ab(self.theirs, "ab-theirs")
        plan = self._preview("antibody", ab.pk)
        self.assertFalse(plan["allowed"])
        self.assertIn("McGill", plan["why"])
        resp = self.client.post("/pipeline/records/delete/",
                                {"kind": "antibody", "id": ab.pk,
                                 "confirm": "ab-theirs"})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(Antibody.objects.using(DB).filter(pk=ab.pk).exists())

    def test_a_target_belongs_to_whoever_nominated_it(self):
        """`Target.site` is a dead Access-era column that no write path sets, so
        whose target it is lives on its nominations."""
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.theirs.pk, funded=False)
        plan = self._preview("target", self.target.pk)
        self.assertFalse(plan["allowed"])
        self.assertIn("McGill", plan["why"])

    def test_a_gene_two_sites_are_pursuing_is_not_yours_alone(self):
        for site in (self.mine, self.theirs):
            TargetNomination.objects.using(DB).create(
                target_id=self.target.pk, site_id=site.pk, funded=False)
        self.assertFalse(self._preview("target", self.target.pk)["allowed"])

    def test_a_record_with_no_site_is_nobodys(self):
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-homeless")
        plan = self._preview("antibody", ab.pk)
        self.assertFalse(plan["allowed"])
        self.assertIn("superuser", plan["why"])

    def test_a_superuser_may_remove_any_of_them(self):
        """Which is what keeps a mistake at a site with nobody senior on it from
        needing a developer and a shell."""
        from django.test import Client

        from pipeline.tests_user_board import _person
        _person("boss", self.mine, role="admin", superuser=True)
        boss = Client()
        self.assertTrue(boss.login(username="boss", password="pw"))

        theirs = self._ab(self.theirs, "ab-theirs")
        homeless = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-homeless")
        for ab in (theirs, homeless):
            with self.subTest(catalogue=ab.catalogue_number):
                self.assertTrue(self._preview("antibody", ab.pk, boss)["allowed"])


class ASuperuserMayDeleteThroughTheRefusalsTests(TestCase):
    """Owner's decision, 3 Aug: superusers can delete things even with data
    attached.

    That changes the risk, so it changes the design. The blockers stop being a
    wall and become a **manifest** — and the counts come from Django's own
    `Collector` rather than the hand-written blocker list, because deleting a
    target cascades to its antibodies and each of those cascades to its results
    and its published figures. "22 antibodies" understates it by two levels, and
    nobody can agree to what they have not been shown.
    """

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        from django.test import Client

        from pipeline.tests_user_board import _person
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        _person("boss", self.site, role="admin", superuser=True)
        self.boss = Client()
        self.assertTrue(self.boss.login(username="boss", password="pw"))

        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=False)
        company = Company.objects.using(DB).create(name="Abcam")
        self.ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab1", site_id=self.site.pk)
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=self.member.pk)
        WbResult.objects.using(DB).create(session_id=self.session.pk,
                                          antibody_id=self.ab.pk)

    def _preview(self, client, kind, pk):
        return client.post("/pipeline/records/delete/preview/",
                           {"kind": kind, "id": pk}).json()["plan"]

    def test_a_member_gets_the_same_warning_on_their_own_benchs_work(self):
        """*"and site members to be able to delete their own data similarly"* —
        every record in this fixture is Leicester's, so the member sees exactly
        what the superuser sees."""
        mine = self._preview(self.client, "target", self.target.pk)
        boss = self._preview(self.boss, "target", self.target.pk)
        self.assertTrue(mine["allowed"])
        self.assertTrue(mine["overriding"])
        self.assertEqual(mine["cascade"], boss["cascade"])

    def test_a_superuser_is_allowed_and_told_exactly_what_goes(self):
        plan = self._preview(self.boss, "target", self.target.pk)
        self.assertTrue(plan["allowed"])
        self.assertTrue(plan["overriding"])
        nouns = {c["noun"].lower() for c in plan["cascade"]}
        # Two levels down: the result row belongs to the session and the
        # antibody, neither of which is a direct child of the target.
        self.assertTrue(any("antibod" in n for n in nouns), nouns)
        self.assertTrue(any("session" in n for n in nouns), nouns)
        self.assertTrue(any("result" in n for n in nouns), nouns)
        # `collector.data` alone misses this: Django removes a nomination with a
        # single query, so it lives in `fast_deletes` and never appears there.
        self.assertTrue(any("nomination" in n for n in nouns), nouns)
        self.assertGreaterEqual(plan["cascade_total"], 4)

    def test_the_count_is_transitive_not_the_direct_children(self):
        """The hand-written blocker list says "1 antibodies, 1 sessions". The
        cascade says that *and* the reading and the nomination."""
        plan = self._preview(self.boss, "target", self.target.pk)
        direct = sum(b["count"] for b in plan["blockers"])
        self.assertGreater(plan["cascade_total"], direct)

    def test_it_still_takes_the_records_own_name(self):
        resp = self.boss.post("/pipeline/records/delete/",
                              {"kind": "target", "id": self.target.pk,
                               "confirm": "yes"})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(Target.objects.using(DB).filter(pk=self.target.pk).exists())

    def test_and_then_it_takes_everything(self):
        plan = self._preview(self.boss, "target", self.target.pk)
        resp = self.boss.post(
            "/pipeline/records/delete/",
            {"kind": "target", "id": self.target.pk, "confirm": "SNCA",
             "agreed": plan["cascade_total"] + plan["orphaned_total"]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.assertFalse(Antibody.objects.using(DB).filter(pk=self.ab.pk).exists())
        self.assertFalse(
            ExperimentSession.objects.using(DB).filter(pk=self.session.pk).exists())
        self.assertFalse(WbResult.objects.using(DB).exists())

    def test_the_cascade_is_read_only_until_it_is_confirmed(self):
        """`collect()` walks; `delete()` is never called by the preview."""
        before = Antibody.objects.using(DB).count()
        self._preview(self.boss, "target", self.target.pk)
        self.assertEqual(Antibody.objects.using(DB).count(), before)

    def test_the_preview_walks_the_collector_once_not_once_per_question(self):
        """The panel asks three things — what is deleted, what is nulled, and
        whether either reaches another site — and each was its own
        `Collector.collect()`. That walk reaches every reading and every
        published figure behind a gene, so it was three times the work on the
        one endpoint a person waits in front of before a destructive act.

        Counted directly. The flat-cost test below does **not** cover this: the
        collector's query count scales with the number of *relations*, not rows,
        so three walks are three times as expensive and still flat — it passes
        either way. A guard that cannot fail is worse than none.
        """
        from pipeline.services import deletion as svc
        walks = []
        real = svc._walk
        try:
            svc._walk = lambda obj, extra=(): (walks.append(obj),
                                               real(obj, extra))[1]
            plan = self._preview(self.boss, "target", self.target.pk)
        finally:
            svc._walk = real
        self.assertTrue(plan["overriding"], "the manifest path did not run")
        self.assertEqual(len(walks), 1, f"{len(walks)} collector walks per preview")

    def test_the_preview_costs_the_same_on_a_big_gene_as_a_small_one(self):
        """N+1, pinned the way this codebase pins every other one: *does not
        grow with row count*, rather than as a fixed number."""
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        from pipeline.models import Company
        company = Company.objects.using(DB).get(name="Abcam")

        def cost():
            with CaptureQueriesContext(connections[DB]) as ctx:
                self._preview(self.boss, "target", self.target.pk)
            return len(ctx)

        small = cost()
        for i in range(25):
            ab = Antibody.objects.using(DB).create(
                target_id=self.target.pk, company_id=company.pk,
                catalogue_number=f"ab-bulk-{i}", site_id=self.site.pk)
            WbResult.objects.using(DB).create(
                session_id=self.session.pk, antibody_id=ab.pk)
        big = cost()
        self.assertEqual(small, big,
                         f"the preview costs more on a bigger gene "
                         f"({small} → {big} queries)")

    def test_the_dialog_shows_the_manifest_rather_than_the_ordinary_line(self):
        from pathlib import Path
        js = Path("pipeline/static/pipeline/board.js").read_text()
        block = js[js.index("function deleteDialog("):]
        block = block[:block.index("\n  function identityDialog(")]
        self.assertIn("p.overriding", block)
        self.assertIn("cascade_total", block)
        # It must not fall through to "Nothing else points at …", which would be
        # a flat lie on a record with work behind it.
        over = block.index("p.overriding")
        ordinary = block.index("Nothing else points at")
        self.assertLess(over, ordinary)
        self.assertIn("return;", block[over:ordinary])

    def test_the_dialog_draws_the_orphans_as_well_as_the_deletions(self):
        """A wild type cascades to nothing, so a panel that reads only
        `cascade_total` prints "nothing else would be deleted" over three
        sessions about to lose their control line — true, and the wrong half of
        the truth."""
        from pathlib import Path
        js = Path("pipeline/static/pipeline/board.js").read_text()
        block = js[js.index("function deleteDialog("):]
        block = block[:block.index("\n  function identityDialog(")]
        self.assertIn("orphaned_total", block)
        self.assertIn("p.orphaned", block)

    def test_the_commit_sends_back_the_number_it_showed(self):
        """Or the server's changed-number check has nothing to compare against
        and every override is refused — the wiring, not the string."""
        from pathlib import Path
        js = Path("pipeline/static/pipeline/board.js").read_text()
        block = js[js.index("function deleteDialog("):]
        block = block[:block.index("\n  function identityDialog(")]
        self.assertIn("body.agreed = agreed", block)
        # It is set where the manifest is drawn, and cleared on every open, so a
        # second record cannot inherit the first one's number.
        self.assertIn("agreed = goes + left", block)
        self.assertLess(block.index("agreed = null"), block.index("agreed = goes"))


class WhatAMemberMayNotReachTests(TestCase):
    """Your own data is yours; the cascade decides whether it still is.

    A target Leicester nominated can carry McGill's antibodies two levels down,
    and destroying another lab's readings is not "your own data" by any reading
    of it. `_cascade_trespass` asks the collector rather than the blocker list,
    because at two levels down nothing about the target *looks* like it touches
    them.
    """

    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.mine = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.theirs = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.client = _member_client(self, self.mine)
        self.member = Member.objects.using(DB).get(site_id=self.mine.pk)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.mine.pk, funded=False)

    def _preview(self, kind, pk, client=None):
        return (client or self.client).post(
            "/pipeline/records/delete/preview/",
            {"kind": kind, "id": pk}).json()["plan"]

    def test_a_gene_carrying_another_sites_antibody_is_refused_by_name(self):
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-theirs", site_id=self.theirs.pk)
        plan = self._preview("target", self.target.pk)
        self.assertFalse(plan["allowed"])
        self.assertFalse(plan["overriding"])
        self.assertIn("McGill", plan["why"])

    def test_the_same_gene_carrying_only_your_own_is_allowed(self):
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-mine", site_id=self.mine.pk)
        plan = self._preview("target", self.target.pk)
        self.assertTrue(plan["allowed"])
        self.assertTrue(plan["overriding"])

    def test_a_nulled_row_counts_as_reached(self):
        """**Setting McGill's session's `cell_line_wt` to null damages McGill's
        record just as surely as deleting it would** — more quietly, since the
        row is still there afterwards looking complete. Leaving `field_updates`
        out of the trespass check would be the same mistake as leaving
        `fast_deletes` out of the manifest.
        """
        line = CellLine.objects.using(DB).create(name="HAP1", genotype="WT",
                                                 site_id=self.mine.pk)
        from pipeline.tests_user_board import _person
        their_member = _person("riham", self.theirs, login=False)
        ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.theirs.pk, experimenter_id=their_member.pk,
            cell_line_wt_id=line.pk)
        plan = self._preview("cell-line", line.pk)
        self.assertFalse(plan["allowed"])
        self.assertIn("McGill", plan["why"])

    def test_a_reading_inside_another_sites_session_counts_as_reached(self):
        """**A reading has no site of its own — its session does.**

        `WbResult`, the other three result models and `FileAttachment` all carry
        `session` and no `site`, so an antibody of mine used in McGill's session
        cascades to McGill's readings and nothing on the row says so. The check
        looked only for `site_id` and passed it: Leicester could have destroyed
        another lab's data through a row that was genuinely its own.
        """
        from pipeline.models import ExperimentSession, WbResult

        from pipeline.tests_user_board import _person
        mine = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-mine", site_id=self.mine.pk)
        their_member = _person("riham", self.theirs, login=False)
        their_session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.theirs.pk, experimenter_id=their_member.pk)
        WbResult.objects.using(DB).create(session_id=their_session.pk,
                                          antibody_id=mine.pk)

        plan = self._preview("antibody", mine.pk)
        self.assertFalse(plan["allowed"])
        self.assertIn("McGill", plan["why"])

    def test_a_reading_in_your_own_session_is_still_yours(self):
        """The other half — or the fix above would refuse every ordinary
        delete, which is the same wall this feature replaced."""
        from pipeline.models import ExperimentSession, WbResult
        my_member = Member.objects.using(DB).get(site_id=self.mine.pk)
        mine = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-mine", site_id=self.mine.pk)
        my_session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.mine.pk, experimenter_id=my_member.pk)
        WbResult.objects.using(DB).create(session_id=my_session.pk,
                                          antibody_id=mine.pk)
        plan = self._preview("antibody", mine.pk)
        self.assertTrue(plan["allowed"])
        self.assertTrue(plan["overriding"])

    def test_a_superuser_reaches_it_anyway(self):
        from django.test import Client

        from pipeline.tests_user_board import _person
        Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="ab-theirs", site_id=self.theirs.pk)
        _person("boss", self.mine, role="admin", superuser=True)
        boss = Client()
        self.assertTrue(boss.login(username="boss", password="pw"))
        plan = self._preview("target", self.target.pk, boss)
        self.assertTrue(plan["allowed"])
        self.assertTrue(plan["overriding"])
