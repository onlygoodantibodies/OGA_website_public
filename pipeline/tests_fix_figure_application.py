"""Relabelling a figure filed under the wrong application.

Written for SLC2A6, 29 Aug 2026: two crops that are plainly WT/KO western blots
across HAP1 and HCT116 were filed as immunoprecipitation, and ``ab119272``
carried an *IP* recommendation because of it. The public consequence is the one
that matters — the extension, the MCP and the gene page all reported a
commercial product as recommended for an assay nobody ran on it.

What is pinned here is what would be **silently** wrong:

* **The recommendation left behind.** Moving the figure and not the flag leaves
  a public verdict about the old application, with no figure under it to explain
  where the verdict came from.
* **A collision written as an IntegrityError.** One antibody has at most one
  figure per application, so a move onto a taken slot has to be refused by name
  before the write, not discovered at it.
* **The rows keyed the same way going stale** — a queued crop and a Judge
  outcomes judgement both hang off (antibody, application) and would otherwise
  point at an application the antibody no longer has a figure for.
"""
from __future__ import annotations

from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import TestCase

from pipeline.models import (Antibody, AntibodyOutcome, Company,
                             PendingPublicationImage, PublicationImage, Site,
                             Target)
from pipeline.tests_timeouts import DB


class RelabellingAFigureTests(TestCase):
    databases = {"pipeline_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="SLC2A6")
        self.ab = self._antibody("ab119272", ip_recommended=True)
        self.img = self._figure(self.ab, "IP")

    def _antibody(self, catalogue, **flags):
        return Antibody.objects.using(DB).create(
            catalogue_number=catalogue, target=self.target,
            company=self.company, site=self.site, **flags)

    def _figure(self, ab, app, name="Glut-IP-ab119272.png"):
        return PublicationImage.objects.using(DB).create(
            antibody=ab, application_type=app,
            image=SimpleUploadedFile(name, b"not-a-png"))

    def _run(self, **kw):
        out = StringIO()
        call_command("fix_figure_application", stdout=out, stderr=out, **kw)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        """Every command that writes is dry-run by default until the owner has
        seen the output."""
        body = self._run(gene="SLC2A6", from_app="IP", to_app="WB")
        self.assertIn("Dry run", body)
        self.img.refresh_from_db()
        self.ab.refresh_from_db()
        self.assertEqual(self.img.application_type, "IP")
        self.assertTrue(self.ab.ip_recommended)

    def test_the_recommendation_moves_with_the_figure(self):
        """Left behind, it is a public verdict about an assay nobody ran, with
        no figure under it to say where it came from."""
        self._run(gene="SLC2A6", from_app="IP", to_app="WB", apply=True)
        self.img.refresh_from_db()
        self.ab.refresh_from_db()
        self.assertEqual(self.img.application_type, "WB")
        self.assertTrue(self.ab.wb_recommended)
        self.assertFalse(self.ab.ip_recommended)

    def test_an_antibody_with_no_recommendation_keeps_none(self):
        """The second SLC2A6 figure. Moving it must not invent a verdict."""
        plain = self._antibody("MA5-24979")
        self._figure(plain, "IP", name="Glut-IP-MA524979.png")
        self._run(gene="SLC2A6", from_app="IP", to_app="WB", apply=True)
        plain.refresh_from_db()
        self.assertFalse(plain.wb_recommended)
        self.assertFalse(plain.ip_recommended)

    def test_a_taken_destination_is_refused_by_name_not_by_integrityerror(self):
        """One antibody has at most one figure per application. A move onto a
        taken slot is a question for a person — which of the two is the western
        blot — not an exception at the write."""
        self._figure(self.ab, "WB", name="already-wb.png")
        body = self._run(gene="SLC2A6", from_app="IP", to_app="WB", apply=True)
        self.assertIn("REFUSED", body)
        self.assertIn("ab119272", body)
        self.img.refresh_from_db()
        self.ab.refresh_from_db()
        self.assertEqual(self.img.application_type, "IP")
        self.assertTrue(self.ab.ip_recommended, "a refused move writes nothing")

    def test_the_queued_crop_and_the_judgement_follow(self):
        """Both are keyed (antibody, application) like the figure, so both
        would otherwise point at an application with no figure under it."""
        PendingPublicationImage.objects.using(DB).create(
            antibody=self.ab, application_type="IP",
            image=SimpleUploadedFile("q.png", b"x"), status="released")
        AntibodyOutcome.objects.using(DB).create(
            antibody=self.ab, application_type="IP", enriches="yes")
        self._run(gene="SLC2A6", from_app="IP", to_app="WB", apply=True)
        self.assertEqual(
            PendingPublicationImage.objects.using(DB).get(antibody=self.ab)
            .application_type, "WB")
        self.assertEqual(
            AntibodyOutcome.objects.using(DB).get(antibody=self.ab)
            .application_type, "WB")

    def test_the_bytes_are_not_moved_or_renamed(self):
        """A key is not read for its application anywhere, a rename is a
        storage move against a live public URL, and the older figures are named
        from a convention that predates the current one. Said out loud, because
        a corrected figure keeping a filename that names the old application is
        the kind of thing somebody discovers later."""
        before = self.img.image.name
        body = self._run(gene="SLC2A6", from_app="IP", to_app="WB", apply=True)
        self.img.refresh_from_db()
        self.assertEqual(self.img.image.name, before)
        self.assertIn("the bytes are not moved or renamed", body)

    def test_the_readings_are_reported_as_the_evidence(self):
        """SLC2A6 has no IpResult row at all and every reading belongs to a WB
        session — which is what says the figure, not the readings, is wrong."""
        body = self._run(gene="SLC2A6", from_app="IP", to_app="WB")
        self.assertIn("readings on file", body)

    def test_a_misspelled_application_is_refused_with_the_casing(self):
        """`ICC-IF` in the database and `IF` in a crop's filename are not
        interchangeable, and neither is lowercase."""
        with self.assertRaises(CommandError) as caught:
            self._run(gene="SLC2A6", from_app="IP", to_app="IF")
        self.assertIn("ICC-IF", str(caught.exception))

    def test_naming_nothing_is_refused(self):
        with self.assertRaises(CommandError):
            self._run(from_app="IP", to_app="WB")

    def test_nothing_matching_says_so_rather_than_reporting_success(self):
        body = self._run(gene="ZZZZZZ", from_app="IP", to_app="WB")
        self.assertIn("No IP figures found", body)

    def test_ids_narrow_to_some_of_a_genes_figures(self):
        """Only some of a gene's figures may be wrong."""
        other = self._antibody("MA5-24979")
        other_img = self._figure(other, "IP", name="other.png")
        self._run(ids=str(other_img.pk), from_app="IP", to_app="WB", apply=True)
        self.img.refresh_from_db()
        other_img.refresh_from_db()
        self.assertEqual(self.img.application_type, "IP")
        self.assertEqual(other_img.application_type, "WB")
