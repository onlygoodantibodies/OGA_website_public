"""The purge command: what it removes, and what it must never remove.

Written because deleting a target is the one operation in this codebase with no
undo short of a database restore, and because the obvious implementation — let
the FKs cascade — silently corrupts the cell-line table.
"""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from django.contrib.auth.models import User

from pipeline.models import (Antibody, CellLine, Company, ExperimentSession,
                             Member, Site, Target)

DB = "pipeline_db"
TAG = "[COWORK RUN4]"


class PurgeTargetTests(TestCase):
    databases = {"pipeline_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester",
                                                  short_code="LEI")
        user = User(username="hsv6")
        user.save(using=DB)
        self.member = Member.objects.using(DB).create(
            user_id=user.pk, site_id=self.site.pk, role="admin", is_active=True)
        self.company = Company.objects.using(DB).create(name="Proteintech")
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        self.other = Target.objects.using(DB).create(gene_name="SOD1")

        # A WT parental line is shared and carries no gene of its own.
        self.wt = CellLine.objects.using(DB).create(name="SH-SY5Y",
                                                    genotype="WT")
        self.ko = CellLine.objects.using(DB).create(
            name="SH-SY5Y STMN2 KO", genotype="KO", target_id=self.target.pk,
            parent_line_id=self.wt.pk)

        self.ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=self.company.pk,
            catalogue_number="10586-1-AP", site_id=self.site.pk)
        self.keep_ab = Antibody.objects.using(DB).create(
            target_id=self.other.pk, company_id=self.company.pk,
            catalogue_number="SOD1-1", site_id=self.site.pk)
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-07-30",
            site_id=self.site.pk, experimenter_id=self.member.pk)

    def _run(self, *args):
        out = StringIO()
        call_command("purge_target", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        out = self._run("STMN2")
        self.assertIn("DRY RUN", out)
        self.assertTrue(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.assertTrue(Antibody.objects.using(DB).filter(pk=self.ab.pk).exists())

    def test_dry_run_names_what_would_go(self):
        """The owner approving this is a scientist: catalogue numbers and line
        names, not row counts."""
        out = self._run("STMN2")
        self.assertIn("10586-1-AP", out)
        self.assertIn("SH-SY5Y STMN2 KO", out)
        self.assertIn("Proteintech", out)

    def test_apply_removes_the_target_and_its_records(self):
        self._run("STMN2", "--apply")
        self.assertFalse(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.assertFalse(Antibody.objects.using(DB).filter(pk=self.ab.pk).exists())
        self.assertFalse(
            ExperimentSession.objects.using(DB).filter(pk=self.session.pk).exists())

    def test_the_shared_wt_line_survives(self):
        """It belongs to every gene and to none. Losing it would take the
        parentage of every other KO derived from it."""
        self._run("STMN2", "--apply")
        self.assertTrue(CellLine.objects.using(DB).filter(pk=self.wt.pk).exists())

    def test_the_ko_line_is_deleted_not_orphaned(self):
        """CellLine.target is SET_NULL, so cascading would leave the KO behind
        with no gene — which is exactly what a WT parental line looks like."""
        self._run("STMN2", "--apply")
        self.assertFalse(CellLine.objects.using(DB).filter(pk=self.ko.pk).exists())
        survivors = CellLine.objects.using(DB).all()
        self.assertEqual([c.pk for c in survivors], [self.wt.pk])

    def test_another_gene_is_untouched(self):
        self._run("STMN2", "--apply")
        self.assertTrue(Target.objects.using(DB).filter(pk=self.other.pk).exists())
        self.assertTrue(Antibody.objects.using(DB).filter(pk=self.keep_ab.pk).exists())

    def test_an_unknown_gene_says_so_and_does_not_stop_the_rest(self):
        out = self._run("STMN2", "NOSUCHGENE", "--apply")
        self.assertIn("No target called 'NOSUCHGENE'", out)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.target.pk).exists())


class CheckTagTests(TestCase):
    """Proof that a repeat run starts from a clean slate.

    Purging by gene removes what the run *created*. What it cannot remove is free
    text the run typed onto records that were already there — a note on a shared
    cell line, a comment on somebody else's antibody. Those survive, and they are
    what makes a second run confusing: this run's `[COWORK]` note reads exactly
    like the last one's.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        # A shared WT line the run only *edited* — it existed before, so a purge
        # by gene leaves it, note and all.
        self.shared = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk,
            ko_validation_notes="[COWORK RUN2] not validated: no loss of signal")

    def _run(self, *args):
        out = StringIO()
        call_command("purge_target", "STMN2", *args, stdout=out)
        return out.getvalue()

    def test_it_finds_a_note_left_on_a_record_the_purge_does_not_touch(self):
        out = self._run("--apply", "--check-tag", "[COWORK RUN2]")
        self.assertIn("CellLine", out)
        self.assertIn(str(self.shared.pk), out)
        self.assertIn("ko_validation_notes", out)
        self.assertIn("still mention", out)

    def test_it_says_the_slate_is_clean_when_it_is(self):
        self.shared.ko_validation_notes = ""
        self.shared.save(using=DB, update_fields=["ko_validation_notes"])
        out = self._run("--apply", "--check-tag", "[COWORK RUN2]")
        self.assertIn("slate is clean", out)

    def test_the_check_is_read_only_and_works_on_a_dry_run(self):
        out = self._run("--check-tag", "[COWORK RUN2]")
        self.assertIn("DRY RUN", out)
        self.assertIn("still mention", out)
        # Nothing removed, note intact.
        self.assertTrue(Target.objects.using(DB).filter(pk=self.target.pk).exists())
        self.shared.refresh_from_db(using=DB)
        self.assertIn("[COWORK RUN2]", self.shared.ko_validation_notes)

    def test_without_the_flag_nothing_is_searched(self):
        self.assertNotIn("still carrying", self._run("--apply"))


class TheOrphanedWildTypeATestRunCreatedTests(TestCase):
    """"A WT line is shared and was already there" is true of the real ones and
    false of a run that created its own parent.

    Run 4 added ``SH-SY5Y WT [COWORK RUN4]``, its knockout went with the gene, and
    the parent stayed — an orphan carrying the run's tag in its name that a purge
    *by gene* can never reach, because a WT has no gene. The next run's records
    would have been indistinguishable from it.

    What must not happen is the obvious over-correction: deleting a WT because it
    looks like test data. A real HAP1 parents nine genes' knockouts, and purging
    one of them must leave it exactly where it is.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.stmn2 = Target.objects.using(DB).create(gene_name="STMN2")
        self.elp3 = Target.objects.using(DB).create(gene_name="ELP3")
        # A wild type this run created, parenting only this run's knockout.
        self.own_wt = CellLine.objects.using(DB).create(
            name="SH-SY5Y WT [COWORK RUN4]", genotype="WT", site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="SH-SY5Y STMN2 KO", genotype="KO", target_id=self.stmn2.pk,
            parent_line_id=self.own_wt.pk, site_id=self.site.pk)
        # A wild type the lab already had, parenting two genes' knockouts.
        self.shared_wt = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1 STMN2 KO", genotype="KO", target_id=self.stmn2.pk,
            parent_line_id=self.shared_wt.pk, site_id=self.site.pk)
        CellLine.objects.using(DB).create(
            name="HAP1 ELP3 KO", genotype="KO", target_id=self.elp3.pk,
            parent_line_id=self.shared_wt.pk, site_id=self.site.pk)

    def _run(self, *args):
        out = StringIO()
        call_command("purge_target", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_the_dry_run_names_the_wild_type_it_would_orphan(self):
        out = self._run("STMN2", "ELP3")
        self.assertIn("SH-SY5Y WT [COWORK RUN4]", out)
        self.assertIn("nothing pointing at", out)

    def test_the_dry_run_says_how_to_remove_it(self):
        self.assertIn("--orphan-wt", self._run("STMN2", "ELP3"))

    def test_without_the_flag_it_is_still_left_alone(self):
        """The default has to stay conservative — this is live data."""
        self._run("STMN2", "ELP3", "--apply")
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.own_wt.pk).exists())

    def test_the_flag_refuses_to_run_without_a_tag(self):
        """The tag is the only evidence of which wild type a run created. Without
        it the command has no way to tell, so it does not guess."""
        with self.assertRaises(CommandError) as caught:
            self._run("STMN2", "ELP3", "--apply", "--orphan-wt")
        self.assertIn("--check-tag", str(caught.exception))
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.own_wt.pk).exists())

    def test_with_the_flag_and_the_tag_the_run_s_own_parent_goes(self):
        self._run("STMN2", "ELP3", "--apply", "--orphan-wt",
                  "--check-tag", TAG)
        self.assertFalse(CellLine.objects.using(DB)
                         .filter(pk=self.own_wt.pk).exists())

    def test_a_shared_wild_type_survives_purging_one_of_its_genes(self):
        self._run("STMN2", "--apply", "--orphan-wt", "--check-tag", TAG)
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.shared_wt.pk).exists())

    def test_a_shared_wild_type_survives_even_when_every_gene_goes(self):
        """The case that shaped the design. Purging both genes leaves the lab's
        HAP1 parenting nothing, with nothing pointing at it — by relation alone it
        is indistinguishable from a wild type the run invented. It carries no tag,
        so it is not a candidate."""
        out = self._run("STMN2", "ELP3", "--apply", "--orphan-wt",
                        "--check-tag", TAG)
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.shared_wt.pk).exists(),
                        f"a shared parental line was deleted:\n{out}")

    def test_an_untagged_line_is_not_even_listed_as_a_candidate(self):
        out = self._run("STMN2", "ELP3", "--orphan-wt", "--check-tag", TAG)
        head = out.split("still carrying")[0]
        self.assertIn("SH-SY5Y WT", head)
        self.assertNotIn("- HAP1 (id", head)

    def test_left_alone_does_not_claim_a_line_the_orphan_pass_will_take(self):
        """Two answers to one question. The per-target report said "LEFT ALONE —
        parental lines are shared" about the very line the section below it
        announced it would remove."""
        out = self._run("STMN2", "ELP3", "--orphan-wt", "--check-tag", TAG)
        left_alone = [ln for ln in out.splitlines() if "LEFT ALONE" in ln]
        self.assertTrue(left_alone)
        for line in left_alone:
            self.assertNotIn("SH-SY5Y WT", line)
        # HAP1 is genuinely left alone and must still say so.
        self.assertTrue(any("HAP1" in ln for ln in left_alone))

    def test_without_the_flag_left_alone_still_names_every_parent(self):
        out = self._run("STMN2", "ELP3")
        left_alone = " ".join(ln for ln in out.splitlines() if "LEFT ALONE" in ln)
        self.assertIn("SH-SY5Y WT", left_alone)
        self.assertIn("HAP1", left_alone)

    def test_a_wild_type_with_a_vial_is_never_orphaned(self):
        from pipeline.models import CellLineVial
        CellLineVial.objects.using(DB).create(
            cell_line_id=self.own_wt.pk, c_number=901, site_id=self.site.pk)
        self._run("STMN2", "ELP3", "--apply", "--orphan-wt", "--check-tag", TAG)
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.own_wt.pk).exists())

    def test_a_wild_type_a_session_used_is_never_orphaned(self):
        from datetime import date
        from django.contrib.auth.models import User
        from pipeline.models import ExperimentSession, Member
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        member = Member.objects.using(DB).create(
            user_id=pu.pk, site_id=self.site.pk, is_active=True,
            display_name="Vera")
        # A session on a gene that is NOT being purged, so the session survives.
        other = Target.objects.using(DB).create(gene_name="SOD1")
        ExperimentSession.objects.using(DB).create(
            target_id=other.pk, procedure_type="WB", site_id=self.site.pk,
            experimenter_id=member.pk, date=date(2026, 7, 31),
            cell_line_wt_id=self.own_wt.pk)
        out = self._run("STMN2", "ELP3", "--apply", "--orphan-wt",
                        "--check-tag", TAG)
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.own_wt.pk).exists(), out)

    def test_the_tag_check_names_both_kinds_of_leftover(self):
        """It used to assert every remaining mention was an edit to a record the
        run did not create. For the orphaned wild type that was simply wrong."""
        self.own_wt.ko_validation_notes = f"{TAG} none yet"
        self.own_wt.save(using=DB)
        out = self._run("STMN2", "ELP3", "--apply", "--check-tag", TAG)
        self.assertIn("could not reach by gene", out)
        self.assertIn("--orphan-wt", out)


class ClearingUpAfterTheGenesHaveAlreadyGoneTests(TestCase):
    """The second half of the run-4 cleanup, which could not run.

    ``purge_target STMN2 ELP3 --check-tag …`` errored with "None of those genes are
    in the pipeline" — because they had been purged an hour earlier, which is
    exactly when you want the two jobs a tag unlocks: confirming a clean slate, and
    clearing what a purge *by gene* cannot reach. The orphaned wild type was
    unreachable at the only moment anyone would go looking for it.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        # The state after the run-4 purge: a tagged WT parent, already parentless.
        self.leftover = CellLine.objects.using(DB).create(
            name=f"SH-SY5Y WT {TAG}", genotype="WT", site_id=self.site.pk,
            ko_validation_notes=f"{TAG} no KO validation performed yet")
        self.lab_wt = CellLine.objects.using(DB).create(
            name="HAP1", genotype="WT", site_id=self.site.pk)

    def _run(self, *args):
        out = StringIO()
        call_command("purge_target", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_a_tag_check_still_runs_when_the_genes_are_gone(self):
        out = self._run("STMN2", "ELP3", "--check-tag", TAG)
        self.assertIn("None of those genes are in the pipeline", out)
        self.assertIn(f"SH-SY5Y WT {TAG}", out)

    def test_naming_no_genes_at_all_says_so_rather_than_gone(self):
        """"Those genes are already gone" about genes nobody named is the kind of
        small lie that makes a person doubt the rest of the output."""
        out = self._run("--check-tag", TAG)
        self.assertIn("No genes named", out)
        self.assertNotIn("already gone", out)

    def test_it_still_errors_when_there_is_nothing_to_do_at_all(self):
        with self.assertRaises(CommandError):
            self._run("STMN2", "ELP3")

    def test_the_leftover_wild_type_is_found_with_no_targets_left(self):
        out = self._run("STMN2", "ELP3", "--orphan-wt", "--check-tag", TAG)
        head = out.split("still carrying")[0]
        self.assertIn(f"SH-SY5Y WT {TAG}", head)
        self.assertNotIn("- HAP1 (id", head)

    def test_it_can_be_removed_with_no_targets_left(self):
        self._run("STMN2", "ELP3", "--apply", "--orphan-wt", "--check-tag", TAG)
        self.assertFalse(CellLine.objects.using(DB)
                         .filter(pk=self.leftover.pk).exists())
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.lab_wt.pk).exists())

    def test_the_slate_then_reports_clean(self):
        out = self._run("STMN2", "ELP3", "--apply", "--orphan-wt",
                        "--check-tag", TAG)
        self.assertIn("the slate is clean", out)

    def test_a_tagged_wild_type_something_still_uses_is_kept(self):
        from datetime import date
        from django.contrib.auth.models import User
        from pipeline.models import ExperimentSession, Member
        for alias in ("academy_db", DB):
            u = User(username="vera")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="vera")
        member = Member.objects.using(DB).create(
            user_id=pu.pk, site_id=self.site.pk, is_active=True,
            display_name="Vera")
        target = Target.objects.using(DB).create(gene_name="SOD1")
        ExperimentSession.objects.using(DB).create(
            target_id=target.pk, procedure_type="WB", site_id=self.site.pk,
            experimenter_id=member.pk, date=date(2026, 7, 31),
            cell_line_wt_id=self.leftover.pk)
        out = self._run("STMN2", "ELP3", "--apply", "--orphan-wt",
                        "--check-tag", TAG)
        self.assertTrue(CellLine.objects.using(DB)
                        .filter(pk=self.leftover.pk).exists(), out)

    def test_a_tagged_knockout_is_never_treated_as_an_orphan_wild_type(self):
        """Only WT lines. A tagged KO is the purge's own business."""
        target = Target.objects.using(DB).create(gene_name="STMN2")
        ko = CellLine.objects.using(DB).create(
            name=f"SH-SY5Y STMN2 KO {TAG}", genotype="KO",
            target_id=target.pk, site_id=self.site.pk)
        out = self._run("STMN2", "--orphan-wt", "--check-tag", TAG)
        head = out.split("still carrying")[0]
        self.assertNotIn(f"- SH-SY5Y STMN2 KO {TAG} (id {ko.pk})", head)


class RefusingWhenTheTagAndTheGenesNameDifferentRunsTests(TestCase):
    """The near-miss that produced this guard.

    The run-4 cleanup was attempted *after* run 5 had already been done on STMN2
    and ELP3. `purge_target STMN2 ELP3 --orphan-wt --check-tag "[COWORK RUN4]"`
    listed ten of run 5's antibodies and five of its sessions as "would be
    removed", under a command whose tag said RUN4 — and `--apply` would have
    destroyed the records another branch was triaging run 5 against.

    A tag says *which run* you mean. The purge goes by gene, and every run reuses
    the same genes, so the two can disagree and the output reads as though it is
    doing what you asked.

    The trigger is narrow on purpose: a gene carrying a **different tag of the same
    family**. Carrying no tag at all is no evidence — a run may tag only some of
    what it touches, and the read-only sweep is why `--check-tag` exists.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.company = Company.objects.using(DB).create(name="Proteintech")
        # STMN2 as run 5 left it.
        self.stmn2 = Target.objects.using(DB).create(gene_name="STMN2")
        Antibody.objects.using(DB).create(
            target_id=self.stmn2.pk, company_id=self.company.pk,
            catalogue_number="CAT-R5-S1 [COWORK RUN5]", site_id=self.site.pk)
        # Run 4's leftover wild type, which is what the operator meant to clear.
        self.leftover = CellLine.objects.using(DB).create(
            name=f"SH-SY5Y WT {TAG}", genotype="WT", site_id=self.site.pk)

    def _run(self, *args):
        out = StringIO()
        call_command("purge_target", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_it_refuses_and_names_the_run_that_is_actually_there(self):
        with self.assertRaises(CommandError) as caught:
            self._run("STMN2", "--check-tag", TAG)
        msg = str(caught.exception)
        self.assertIn("STMN2", msg)
        self.assertIn("[COWORK RUN5]", msg)
        self.assertIn(TAG, msg)

    def test_the_refusal_says_what_to_do_instead(self):
        with self.assertRaises(CommandError) as caught:
            self._run("STMN2", "--check-tag", TAG)
        msg = str(caught.exception)
        self.assertIn("drop the gene names", msg)
        self.assertIn("omit --check-tag", msg)

    def test_it_refuses_on_a_dry_run_too_not_only_on_apply(self):
        """The dry run is what a person reads before approving. If it prints run
        5's records under a RUN4 heading, the approval is uninformed."""
        with self.assertRaises(CommandError):
            self._run("STMN2", "--check-tag", TAG)
        with self.assertRaises(CommandError):
            self._run("STMN2", "--apply", "--check-tag", TAG)
        self.assertTrue(Antibody.objects.using(DB)
                        .filter(catalogue_number__contains="RUN5").exists())

    def test_dropping_the_gene_names_clears_the_leftover_and_nothing_else(self):
        """The command the operator actually wanted."""
        self._run("--apply", "--orphan-wt", "--check-tag", TAG)
        self.assertFalse(CellLine.objects.using(DB)
                         .filter(pk=self.leftover.pk).exists())
        self.assertTrue(Target.objects.using(DB)
                        .filter(pk=self.stmn2.pk).exists())
        self.assertEqual(Antibody.objects.using(DB)
                         .filter(target_id=self.stmn2.pk).count(), 1)

    def test_purging_those_genes_deliberately_still_works_with_their_own_tag(self):
        self._run("STMN2", "--apply", "--check-tag", "[COWORK RUN5]")
        self.assertFalse(Target.objects.using(DB)
                         .filter(pk=self.stmn2.pk).exists())

    def test_purging_those_genes_deliberately_still_works_with_no_tag(self):
        self._run("STMN2", "--apply")
        self.assertFalse(Target.objects.using(DB)
                         .filter(pk=self.stmn2.pk).exists())

    def test_an_untagged_gene_is_not_a_conflict(self):
        """Absence of the tag proves nothing — a run may have tagged only some of
        what it touched, and sweeping for a tag while purging a gene is the
        original point of --check-tag."""
        plain = Target.objects.using(DB).create(gene_name="ELP3")
        Antibody.objects.using(DB).create(
            target_id=plain.pk, company_id=self.company.pk,
            catalogue_number="NO-TAG-HERE", site_id=self.site.pk)
        out = self._run("ELP3", "--apply", "--check-tag", TAG)
        self.assertFalse(Target.objects.using(DB).filter(pk=plain.pk).exists())
        self.assertIn(TAG, out)

    def test_bracketed_prose_in_a_comment_is_not_a_conflict(self):
        """This function's output refuses a command, so a false positive blocks
        real work — and scientists write square brackets."""
        plain = Target.objects.using(DB).create(gene_name="SOD1")
        Antibody.objects.using(DB).create(
            target_id=plain.pk, company_id=self.company.pk,
            catalogue_number="12A8", site_id=self.site.pk,
            comments="clean band [see fig 2], faint above 50 kDa [n=3]")
        self._run("SOD1", "--apply", "--check-tag", TAG)
        self.assertFalse(Target.objects.using(DB).filter(pk=plain.pk).exists())


class TheRunsOwnSessionsDoNotProtectTheRunsOwnParentTests(TestCase):
    """``--orphan-wt`` was added so a test run's own wild-type parent stops
    outliving its own cleanup. It did not, and run 5 hit it again.

    A session records **which WT it was run against**. ``_would_orphan``
    discounted the knockouts the purge was about to delete, but not the sessions —
    so the two STMN2 sessions going with the gene still read as "2 session(s) used
    it", the parent was dropped from the candidate list before the dry run could
    name it, and ``--apply`` never deletes a line that was not a candidate.

    Two failures in one. The dry run printed no orphan section at all, under a
    command whose own message says *"Read the names first — that is the whole
    check"*; and the leftover the flag exists to prevent happened anyway, silently.
    It took a second pass naming no genes to clear it.

    The exclusions are passed into ``_what_still_points_at`` rather than filtered
    out of its result, because the old code stripped the parent hold by matching
    on the wording of the message — which is exactly why the session hold was
    missed.
    """

    databases = {"pipeline_db", "academy_db"}
    TAG = "[COWORK RUN5]"

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        user = User(username="hsv6")
        user.save(using=DB)
        self.member = Member.objects.using(DB).create(
            user_id=user.pk, site_id=self.site.pk, role="admin", is_active=True)
        self.target = Target.objects.using(DB).create(gene_name="STMN2")
        self.wt = CellLine.objects.using(DB).create(
            name=f"SH-SY5Y WT {self.TAG}", genotype="WT", site_id=self.site.pk)
        self.ko = CellLine.objects.using(DB).create(
            name=f"SH-SY5Y STMN2 KO {self.TAG}", genotype="KO",
            target_id=self.target.pk, parent_line_id=self.wt.pk,
            site_id=self.site.pk)
        # The bit that mattered: the session names the WT it was run against.
        self.session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", site_id=self.site.pk,
            experimenter_id=self.member.pk, date="2026-07-31",
            cell_line_wt_id=self.wt.pk, cell_line_ko_id=self.ko.pk)

    def _run(self, *args):
        out = StringIO()
        call_command("purge_target", "STMN2", "--orphan-wt",
                     "--check-tag", self.TAG, *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_the_dry_run_names_the_wild_type_it_will_remove(self):
        """In the section that says it is going — not in the "LEFT ALONE" line,
        which is where the name appeared while the line was quietly being kept.
        The two messages say opposite things about the same row, so asserting on
        the name alone passes either way."""
        before = self._run().split("DRY RUN")[0]
        self.assertIn("WT parental lines carrying", before)
        section = before.split("WT parental lines carrying")[1]
        self.assertIn(f"SH-SY5Y WT {self.TAG}", section)
        self.assertIn("will be REMOVED", section)

    def test_and_does_not_also_claim_it_is_being_left_alone(self):
        """Two answers to one question is the bug this command keeps having."""
        before = self._run().split("DRY RUN")[0]
        left = [ln for ln in before.splitlines() if "LEFT ALONE" in ln]
        self.assertFalse([ln for ln in left if f"SH-SY5Y WT {self.TAG}" in ln],
                         f"named as left alone and as removed: {left}")

    def test_one_pass_removes_it(self):
        self._run("--apply")
        self.assertFalse(
            CellLine.objects.using(DB).filter(pk=self.wt.pk).exists(),
            "the run's own parent outlived its own cleanup again")

    def test_and_says_the_slate_is_clean(self):
        """A second pass used to be needed, and nothing said so."""
        self.assertIn("the slate is clean", self._run("--apply"))

    def test_a_parent_used_by_a_gene_that_is_not_going_is_kept(self):
        """The safety direction. A session on *another* gene is real use, and no
        amount of tag matching may override it."""
        other = Target.objects.using(DB).create(gene_name="SOD1")
        ExperimentSession.objects.using(DB).create(
            target_id=other.pk, procedure_type="WB", site_id=self.site.pk,
            experimenter_id=self.member.pk, date="2026-07-31",
            cell_line_wt_id=self.wt.pk)
        self._run("--apply")
        self.assertTrue(CellLine.objects.using(DB).filter(pk=self.wt.pk).exists())

    def test_a_parent_with_a_knockout_for_another_gene_is_kept(self):
        other = Target.objects.using(DB).create(gene_name="SOD1")
        CellLine.objects.using(DB).create(
            name="SH-SY5Y SOD1 KO", genotype="KO", target_id=other.pk,
            parent_line_id=self.wt.pk, site_id=self.site.pk)
        self._run("--apply")
        self.assertTrue(CellLine.objects.using(DB).filter(pk=self.wt.pk).exists())

    def test_an_untagged_parent_is_never_a_candidate(self):
        """The tag is the evidence. Relations alone cannot tell a run's invented
        parent from the lab's own."""
        self.wt.name = "SH-SY5Y"
        self.wt.save(using=DB)
        self._run("--apply")
        self.assertTrue(CellLine.objects.using(DB).filter(pk=self.wt.pk).exists())

    def test_a_vial_still_keeps_it(self):
        from pipeline.models import CellLineVial
        CellLineVial.objects.using(DB).create(cell_line_id=self.wt.pk)
        self._run("--apply")
        self.assertTrue(CellLine.objects.using(DB).filter(pk=self.wt.pk).exists())
