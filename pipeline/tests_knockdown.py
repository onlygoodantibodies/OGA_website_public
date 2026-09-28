"""Knockdown reaches the public site, and only from a figure that says so.

The cropper has always burned "knockout" or "knockdown" into a figure's legend
from its session's genotype, and the word reached the pixels and nothing else.
`PublicationImage.control_genotype` carries it now: staged with the crop,
copied at release, carried back at withdrawal, and read by the gene page, the
public API and the backfill that fills the blanks on older figures.
"""
from __future__ import annotations

from io import StringIO

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from pipeline.models import (Antibody, CellLine, Company, CropperSession,
                             ExperimentSession, PendingPublicationImage,
                             PublicationImage, Site, Target)
from pipeline.services import review as svc

DB = "pipeline_db"


def _png() -> bytes:
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


class TheFigureCarriesItsControlKindTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="GPNMB")
        self.ab = Antibody.objects.using(DB).create(
            target=self.target, company=self.company, catalogue_number="ab1")

    def _stage(self, kind):
        return svc.stage(antibody=self.ab, application_type="WB", content=_png(),
                         filename="GPNMB_ab1_WB.png", staged_by="m",
                         control_genotype=kind)

    def test_stage_release_and_withdraw_all_carry_it(self):
        item = self._stage("KD")
        self.assertEqual(item.control_genotype, "KD")
        svc.release([item], actor="m")
        live = PublicationImage.objects.using(DB).get()
        self.assertEqual(live.control_genotype, "KD")
        svc.withdraw([live], actor="m")
        self.assertEqual(PendingPublicationImage.objects.using(DB).get().control_genotype, "KD")

    def test_a_caller_that_does_not_know_does_not_erase_it(self):
        self._stage("KD")
        item = svc.stage(antibody=self.ab, application_type="WB", content=_png(),
                         filename="GPNMB_ab1_WB.png", staged_by="m")
        self.assertEqual(item.control_genotype, "KD")

    def test_the_gene_page_and_the_api_say_knockdown_only_when_the_figure_does(self):
        svc.release([self._stage("KO")], actor="m")
        page = self.client.get("/antibodies/GPNMB/")
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn("Knockout-controlled characterisation data", body)
        # The mark under a figure, not the stylesheet comment describing it.
        self.assertNotIn("knockdown control</p>", body)

        svc.release([self._stage("KD")], actor="m")
        body = self.client.get("/antibodies/GPNMB/").content.decode()
        self.assertIn("Knockdown-controlled characterisation data", body)
        self.assertIn("<title>GPNMB antibody characterisation — knockdown-controlled", body)
        self.assertIn("knockdown control</p>", body)

    def test_the_public_api_names_the_control_per_figure(self):
        svc.release([self._stage("KD")], actor="m")
        from core.api_views import _serialise_antibody
        ab = Antibody.objects.using(DB).prefetch_related("publication_images").get(pk=self.ab.pk)
        d = _serialise_antibody(ab, False, include_recs=False)
        self.assertEqual([e["control"] for e in d["experiments"]], ["knockdown"])


class TheBackfillReadsTheAccessExportTests(TestCase):
    """The figures already published predate the cropper, so the Access export
    is the evidence: a figure whose every Access row is marked knockdown is
    one; a figure with some rows marked and some not is a question the
    command asks rather than answers; the cropper session is the fallback."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        import tempfile
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="GPNMB")
        self.ab = Antibody.objects.using(DB).create(
            target=self.target, company=self.company, catalogue_number="ab1", access_id=101)
        self.ab2 = Antibody.objects.using(DB).create(
            target=self.target, company=self.company, catalogue_number="ab2", access_id=102)
        self.ab3 = Antibody.objects.using(DB).create(
            target=self.target, company=self.company, catalogue_number="ab3", access_id=103)
        session = CropperSession.objects.using(DB).create(
            owner_username="m", gene="GPNMB", genotype="KD")
        for ab, sess in ((self.ab, None), (self.ab2, None), (self.ab3, session)):
            item = svc.stage(antibody=ab, application_type="WB", content=_png(),
                             filename=f"GPNMB_{ab.catalogue_number}_WB.png",
                             staged_by="m", session=sess)
            svc.release([item], actor="m")
        # Older figures: released with no kind recorded.
        PublicationImage.objects.using(DB).update(control_genotype="")
        PendingPublicationImage.objects.using(DB).update(control_genotype="")
        # An Access export: ab1's two blots both say knockdown (one by the
        # tick, one by the comment); ab2's disagree; ab3 has no rows at all.
        self.dir = tempfile.mkdtemp()
        with open(f"{self.dir}/Wb.csv", "w", newline="") as fh:
            fh.write("ID,AntibodiesID,Comments,KnockDown\n")
            fh.write("1,101,,1\n")
            fh.write("2,101,Lane 2 is a KD,0\n")
            fh.write("3,102,siRNA KD in U87,0\n")
            fh.write("4,102,,0\n")

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_control_genotype", "--dir", self.dir, *args, stdout=out)
        return out.getvalue()

    def _kind(self, ab):
        return PublicationImage.objects.using(DB).get(antibody=ab).control_genotype

    def test_dry_run_by_default_then_access_settles_what_it_can(self):
        text = self._run()
        self.assertIn("would be written               2", text)
        self.assertIn("Access: all 2 row(s) say knockdown", text)
        self.assertIn("cropper session", text)
        self.assertIn("Access rows disagree            1", text)
        self.assertIn("ab2", text)
        self.assertEqual(self._kind(self.ab), "")
        self._run("--apply")
        self.assertEqual(self._kind(self.ab), "KD")
        self.assertEqual(self._kind(self.ab3), "KD", "the cropper session is the fallback")
        self.assertEqual(self._kind(self.ab2), "", "a disagreement is never written")
        self.assertEqual(PendingPublicationImage.objects.using(DB)
                         .get(antibody=self.ab).control_genotype, "KD")

    def test_set_needs_a_gene_and_writes_what_a_person_read_off_the_legend(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._run("--set", "KD")
        self._run("--gene", "GPNMB", "--set", "KO", "--apply")
        self.assertEqual(self._kind(self.ab2), "KO")

    def test_set_can_be_narrowed_to_one_antibody_and_one_application(self):
        """RAB13's IPs were knockdowns and its IF plates knockouts, so a gene-wide
        answer is the wrong shape there."""
        from django.core.management.base import CommandError
        self._run("--gene", "GPNMB", "--catalogue", "ab2", "--application", "WB",
                  "--set", "KD", "--apply")
        self.assertEqual(self._kind(self.ab2), "KD")
        self.assertEqual(self._kind(self.ab), "", "the other antibody was not touched")
        with self.assertRaises(CommandError):
            self._run("--gene", "GPNMB", "--application", "FC", "--set", "KD")


class TheKnockdownLineBackfillTests(TestCase):
    """The Access import pointed a knockdown session's control slot at the plain
    wild type and put no knockdown line on file. The backfill makes the line
    from the wild type in the lane and repoints only the sessions whose every
    Access-linked row is a knockdown."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        import tempfile
        from datetime import date
        from django.contrib.auth.models import User
        from pipeline.models import Member, WbResult
        site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        u = User(username="m"); u.set_password("pw"); u.save(using=DB)
        member = Member.objects.using(DB).create(user_id=u.pk, site_id=site.pk,
                                                 role="experimenter", is_active=True)
        self.company = Company.objects.using(DB).create(name="Abcam")
        self.target = Target.objects.using(DB).create(gene_name="GPNMB")
        self.wt = CellLine.objects.using(DB).create(name="U-87 MG", genotype="WT", site=site)
        ab = Antibody.objects.using(DB).create(target=self.target, company=self.company,
                                               catalogue_number="ab1", access_id=101)
        # The import's shape: control slot = the wild type in lane 2.
        self.kd_session = ExperimentSession.objects.using(DB).create(
            procedure_type="WB", target=self.target, experimenter=member, site=site,
            date=date(2024, 6, 12), status="complete",
            cell_line_wt=self.wt, cell_line_ko=self.wt)
        WbResult.objects.using(DB).create(session=self.kd_session, antibody=ab, access_id=1)
        WbResult.objects.using(DB).create(session=self.kd_session, antibody=ab, access_id=2)
        self.mixed = ExperimentSession.objects.using(DB).create(
            procedure_type="IF", target=self.target, experimenter=member, site=site,
            date=date(2024, 6, 12), status="complete", cell_line_wt=self.wt)
        from pipeline.models import IfResult
        IfResult.objects.using(DB).create(session=self.mixed, antibody=ab, access_id=10)
        IfResult.objects.using(DB).create(session=self.mixed, antibody=ab, access_id=11)
        # The knockout experiment with a stray tick: lanes name a KO line.
        from pipeline.models import CellLineVial
        hap1 = CellLine.objects.using(DB).create(name="HAP1", genotype="WT", site=site)
        hap1_ko = CellLine.objects.using(DB).create(name="HAP1", genotype="KO", site=site,
                                                    target=self.target, parent_line=hap1)
        CellLineVial.objects.using(DB).create(cell_line=self.wt, access_id=590, c_number=1)
        CellLineVial.objects.using(DB).create(cell_line=hap1, access_id=48, c_number=2)
        CellLineVial.objects.using(DB).create(cell_line=hap1_ko, access_id=365, c_number=3)
        self.stray = ExperimentSession.objects.using(DB).create(
            procedure_type="IP", target=self.target, experimenter=member, site=site,
            date=date(2023, 11, 17), status="complete", cell_line_wt=hap1, cell_line_ko=hap1_ko)
        from pipeline.models import IpResult
        IpResult.objects.using(DB).create(session=self.stray, antibody=ab, access_id=20)
        self.dir = tempfile.mkdtemp()
        with open(f"{self.dir}/CellLines.csv", "w", newline="") as fh:
            fh.write("ID,CellLine,WTorKO\n590,U-87 MG,WT\n48,HAP1,WT\n365,HAP1,KO\n")
        with open(f"{self.dir}/Wb.csv", "w", newline="") as fh:
            fh.write("ID,AntibodiesID,Comments,KnockDown,lane1CellLineID,lane2CellLineID\n"
                     "1,101,,1,590,590\n2,101,Lane 2 is a KD,0,590,590\n")
        with open(f"{self.dir}/IF.csv", "w", newline="") as fh:
            fh.write("ID,AntibodiesID,Comments,CellLine1ID,CellLine2ID\n"
                     "10,101,siRNA KD,590,\n11,101,,48,365\n")
        with open(f"{self.dir}/IP.csv", "w", newline="") as fh:
            fh.write("ID,AntibodiesID,Comments,KnockDown,CellLineID\n20,101,KD,1,365\n")
        with open(f"{self.dir}/Proteins.csv", "w", newline="") as fh:
            fh.write("ID,Gene,CommentsCustomKO\n1,GPNMB,siRNA stock\n")

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_knockdown_lines", "--dir", self.dir, *args, stdout=out)
        return out.getvalue()

    def test_dry_run_then_the_line_is_made_and_only_the_pure_session_repointed(self):
        text = self._run()
        self.assertIn("1 knockdown line(s) needed", text)
        self.assertIn("U-87 MG KD · siRNA", text)
        self.assertIn("1 session(s) would have their control slot set", text)
        self.assertIn("1 session(s) mix knockdown and other rows", text)
        self.assertIn("1 session(s) whose only knockdown-marked rows name a knockout", text)
        self.assertNotIn("HAP1 KD", text, "the stray tick on the knockout run made a line")
        self.assertFalse(CellLine.objects.using(DB).filter(genotype="KD").exists())

        self._run("--apply")
        kd = CellLine.objects.using(DB).get(genotype="KD")
        self.assertEqual((kd.name, kd.target_id, kd.parent_line_id, kd.knockdown_method),
                         ("U-87 MG", self.target.pk, self.wt.pk, "siRNA"))
        self.kd_session.refresh_from_db(); self.mixed.refresh_from_db(); self.stray.refresh_from_db()
        self.assertEqual(self.kd_session.cell_line_ko_id, kd.pk)
        self.assertIsNone(self.mixed.cell_line_ko_id, "a mixed session keeps its slot")
        self.assertEqual(self.stray.cell_line_ko.genotype, "KO", "the knockout run keeps its knockout")
        # Running again creates nothing twice.
        self.assertIn("(already on file)", self._run().replace("1 already on file", "(already on file)"))
        self.assertEqual(CellLine.objects.using(DB).filter(genotype="KD").count(), 1)

    def test_the_gene_page_says_what_the_public_figures_say(self):
        from pipeline.tests_timeouts import _member_client
        site = Site.objects.using(DB).get(short_code="MCG")
        client = _member_client(self, site)
        ab = Antibody.objects.using(DB).get(catalogue_number="ab1")
        svc.release([svc.stage(antibody=ab, application_type="WB", content=_png(),
                               filename="GPNMB_ab1_WB.png", staged_by="m",
                               control_genotype="KD")], actor="m")
        body = client.get(f"/pipeline/target/{self.target.pk}/").content.decode()
        self.assertIn("Knockdown-controlled", body)
        self.assertIn("no knockdown line is recorded under Cell Lines yet", body)
        self._run("--apply")
        body = client.get(f"/pipeline/target/{self.target.pk}/").content.decode()
        self.assertNotIn("no knockdown line is recorded", body)
        self.assertIn("U-87 MG", body)
