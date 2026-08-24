"""Merging two Target rows that are the same gene.

RAB32 is on file twice because a paste put the protein name in the gene column,
so ``resolve_or_create_target`` — which matches on ``gene_name`` — made a second
record. Eight antibodies and a HAP1 knockout went onto it. PTK2B is the same
shape.

``Target.gene_name`` is UNIQUE, so the second row cannot simply be renamed: that
collides with the first, which is exactly what tells you this is a merge.

This is a live-data command, so what is pinned is the shape of its safety: a dry
run unless told otherwise, foreign keys found rather than listed, records never
silently combined, published figures never moved by accident, and — the one that
would do real damage — a loser that still holds a cell line never deleted, since
``CellLine.target`` is SET_NULL and a blanked knockout reads as a wild type.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from pipeline.models import (Antibody, CellLine, Company, PublicationImage,
                             Site, Target, TargetClassification)
from pipeline.tests_timeouts import DB


def _run(*args, **opts):
    out = StringIO()
    call_command("merge_targets", *args, stdout=out, stderr=out, **opts)
    return out.getvalue()


class MergeTargetsTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        self.company = Company.objects.using(DB).create(name="Proteintech")
        # The gene, and the record that was created under its protein name and
        # has since had that name emptied.
        self.gene = Target.objects.using(DB).create(
            gene_name="RAB32", uniprot_id="Q13637")
        self.stray = Target.objects.using(DB).create(gene_name=None)
        self.ab = Antibody.objects.using(DB).create(
            target_id=self.stray.pk, company_id=self.company.pk,
            catalogue_number="10999-1-AP", lot_number="00086609",
            site_id=self.site.pk)
        self.line = CellLine.objects.using(DB).create(
            name="HAP1", genotype="KO", target_id=self.stray.pk,
            site_id=self.site.pk)

    # -- addressing --------------------------------------------------------
    def test_a_target_with_no_gene_name_can_be_named_by_id(self):
        """The record this command exists for has no gene name, so there is no
        other way to say which one you mean."""
        out = _run("--from", f"id:{self.stray.pk}", "--into", "RAB32")
        self.assertIn("DRY RUN", out)
        self.assertIn(f"unnamed target id {self.stray.pk}", out)

    def test_an_unknown_gene_name_says_how_to_reach_a_nameless_row(self):
        with self.assertRaises(CommandError) as caught:
            _run("--from", "Ras-related protein Rab-32", "--into", "RAB32")
        message = str(caught.exception)
        self.assertIn("no target with the gene name", message)
        self.assertIn("id:<pk>", message)

    def test_it_refuses_to_merge_into_a_target_with_no_gene_name(self):
        """Merging the gene into the broken row would leave everything
        unreachable from every gene page, filter and search box."""
        with self.assertRaises(CommandError) as caught:
            _run("--from", "RAB32", "--into", f"id:{self.stray.pk}")
        self.assertIn("has no gene name", str(caught.exception))

    def test_it_refuses_to_merge_a_target_into_itself(self):
        with self.assertRaises(CommandError) as caught:
            _run("--from", "RAB32", "--into", "RAB32")
        self.assertIn("same target", str(caught.exception))

    # -- the dry run is the default ---------------------------------------
    def test_a_dry_run_moves_nothing(self):
        _run("--from", f"id:{self.stray.pk}", "--into", "RAB32")
        self.ab.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.ab.target_id, self.stray.pk)
        self.assertEqual(self.line.target_id, self.stray.pk)
        self.assertTrue(Target.objects.using(DB).filter(pk=self.stray.pk).exists())

    def test_apply_moves_the_records_and_deletes_the_empty_target(self):
        _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")
        self.ab.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.ab.target_id, self.gene.pk)
        self.assertEqual(self.line.target_id, self.gene.pk)
        self.assertFalse(Target.objects.using(DB).filter(pk=self.stray.pk).exists())

    # -- the foreign keys are found, not listed ---------------------------
    def test_every_target_foreign_key_is_found_by_introspection(self):
        """A model added later must not be left pointing at a deleted target."""
        from pipeline.management.commands.merge_targets import target_fks
        found = {model.__name__ for model, _field in target_fks()}
        for expected in ("Antibody", "CellLine", "ExperimentSession", "Report",
                         "TargetAssignment", "TargetClassification",
                         "TargetNomination", "ReagentRequest"):
            self.assertIn(expected, found)

    # -- records are never silently combined ------------------------------
    def test_a_row_whose_twin_is_already_on_the_survivor_is_held_back(self):
        """The identity key includes the target, so moving a vial onto a target
        that already holds the same vial would break the constraint."""
        Antibody.objects.using(DB).create(
            target_id=self.gene.pk, company_id=self.company.pk,
            catalogue_number="10999-1-AP", lot_number="00086609",
            site_id=self.site.pk)
        out = _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.target_id, self.stray.pk, "the twin moved anyway")
        self.assertIn("held back", out)
        self.assertIn("merge_duplicate_antibodies", out)

    def test_a_different_lot_is_a_different_vial_and_moves(self):
        """Two lots of one product are two vials, not a duplicate — the whole
        reason the RAB32 split is a re-pointing rather than a dedup."""
        Antibody.objects.using(DB).create(
            target_id=self.gene.pk, company_id=self.company.pk,
            catalogue_number="10999-1-AP", lot_number="00140343",
            site_id=self.site.pk)
        _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.target_id, self.gene.pk)

    def test_a_classification_already_on_the_survivor_is_held_back(self):
        TargetClassification.objects.using(DB).create(
            target_id=self.stray.pk, label="GPCR", source="uniprot")
        TargetClassification.objects.using(DB).create(
            target_id=self.gene.pk, label="GPCR", source="uniprot")
        out = _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")
        self.assertIn("held back", out)

    # -- the one that would do real damage --------------------------------
    def test_a_loser_still_holding_a_cell_line_is_never_deleted(self):
        """``CellLine.target`` is SET_NULL while everything else cascades, so
        deleting a target that still holds a line does not remove the line — it
        blanks its gene, and a cell line with no gene is what a wild type is.
        The knockout then reads as a parental and collides by name with the real
        knockout when the gene is put back."""
        clash = Antibody.objects.using(DB).create(
            target_id=self.gene.pk, company_id=self.company.pk,
            catalogue_number="10999-1-AP", lot_number="00086609",
            site_id=self.site.pk)
        self.assertTrue(clash.pk)
        out = _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")

        self.assertTrue(Target.objects.using(DB).filter(pk=self.stray.pk).exists(),
                        "the loser was deleted while a held-back row still pointed at it")
        self.assertIn("left in place", out)
        self.line.refresh_from_db()
        self.assertEqual(self.line.target_id, self.gene.pk)
        self.assertIsNotNone(self.line.target_id, "a knockout was blanked into a wild type")

    def test_keep_empty_target_leaves_the_row_behind(self):
        out = _run("--from", f"id:{self.stray.pk}", "--into", "RAB32",
                   "--apply", "--keep-empty-target")
        self.assertTrue(Target.objects.using(DB).filter(pk=self.stray.pk).exists())
        self.assertIn("was kept", out)

    # -- published figures -------------------------------------------------
    def test_it_refuses_when_a_published_figure_would_move(self):
        """A PublicationImage is what makes a figure public, so moving one is a
        change to what a gene page says about somebody's product."""
        PublicationImage.objects.using(DB).create(
            antibody_id=self.ab.pk, application_type="WB", image="x/y.png")
        with self.assertRaises(CommandError) as caught:
            _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")
        message = str(caught.exception)
        self.assertIn("published figure", message)
        self.assertIn("--allow-published-figures", message)
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.target_id, self.stray.pk)

    def test_the_published_refusal_can_be_overridden_deliberately(self):
        PublicationImage.objects.using(DB).create(
            antibody_id=self.ab.pk, application_type="WB", image="x/y.png")
        out = _run("--from", f"id:{self.stray.pk}", "--into", "RAB32",
                   "--apply", "--allow-published-figures")
        self.assertIn("published figure", out)
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.target_id, self.gene.pk)

    # -- nothing is copied onto the survivor ------------------------------
    def test_the_survivor_keeps_its_own_identity(self):
        """``uniprot_id`` is UNIQUE and which accession a gene should carry is a
        curation question, so the merge moves records and touches no field."""
        self.stray.uniprot_id = "P00000"
        self.stray.protein_name = "Ras-related protein Rab-32"
        self.stray.save(using=DB)
        _run("--from", f"id:{self.stray.pk}", "--into", "RAB32", "--apply")
        self.gene.refresh_from_db()
        self.assertEqual(self.gene.uniprot_id, "Q13637")
        self.assertEqual(self.gene.gene_name, "RAB32")


class PTK2BStubTests(TestCase):
    """The other half of the same defect, and the cheaper one.

    ``PTK2b`` (mouse casing) sits beside ``PTK2B`` holding nothing but
    classifications. ``fix_gene_case`` cannot correct the case, because
    ``gene_name`` is UNIQUE and correcting it is a merge — so the two jobs are
    one job, and this command is what closes it.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_a_stub_holding_only_classifications_merges_and_goes(self):
        real = Target.objects.using(DB).create(gene_name="PTK2B", uniprot_id="Q14289")
        stub = Target.objects.using(DB).create(gene_name="PTK2b")
        TargetClassification.objects.using(DB).create(
            target_id=stub.pk, label="Kinase", source="manual")

        out = _run("--from", "PTK2b", "--into", "PTK2B", "--apply")

        self.assertFalse(Target.objects.using(DB).filter(pk=stub.pk).exists())
        self.assertEqual(
            TargetClassification.objects.using(DB).filter(target_id=real.pk).count(), 1)
        self.assertIn("deleted", out)

    def test_an_empty_stub_says_there_is_nothing_to_move(self):
        Target.objects.using(DB).create(gene_name="PTK2B", uniprot_id="Q14289")
        Target.objects.using(DB).create(gene_name="PTK2b")
        out = _run("--from", "PTK2b", "--into", "PTK2B")
        self.assertIn("Nothing is recorded against", out)
        self.assertIn("could be deleted", out)

    def test_the_stubs_one_duplicate_label_can_be_discarded_so_the_row_can_go(self):
        """PTK2b's only record is a `PTK family` tag PTK2B already carries, so
        holding it back keeps the stub alive forever. The tag is derived —
        `backfill_protein_classes` rebuilds it — and its evidence names the
        mis-cased spelling that is being deleted, so dropping it loses nothing."""
        real = Target.objects.using(DB).create(gene_name="PTK2B", uniprot_id="Q14289")
        stub = Target.objects.using(DB).create(gene_name="PTK2b")
        TargetClassification.objects.using(DB).create(
            target_id=real.pk, label="PTK family", source="family",
            evidence="gene symbol prefix of PTK2B")
        TargetClassification.objects.using(DB).create(
            target_id=stub.pk, label="PTK family", source="family",
            evidence="gene symbol prefix of PTK2b")

        held = _run("--from", "PTK2b", "--into", "PTK2B", "--apply")
        self.assertIn("held back", held)
        self.assertTrue(Target.objects.using(DB).filter(pk=stub.pk).exists())
        self.assertIn("--discard-duplicate-labels", held)

        out = _run("--from", "PTK2b", "--into", "PTK2B", "--apply",
                   "--discard-duplicate-labels")
        self.assertIn("gene symbol prefix of PTK2b", out,
                      "the evidence being discarded was not shown")
        self.assertFalse(Target.objects.using(DB).filter(pk=stub.pk).exists())
        self.assertEqual(
            TargetClassification.objects.using(DB).filter(label="PTK family").count(), 1)

    def test_discarding_labels_never_touches_an_antibody(self):
        """The flag is scoped to derived tags. A held-back vial is a record and
        stays a record."""
        site = Site.objects.using(DB).create(name="McGill", short_code="MCG")
        company = Company.objects.using(DB).create(name="Proteintech")
        real = Target.objects.using(DB).create(gene_name="PTK2B", uniprot_id="Q14289")
        stub = Target.objects.using(DB).create(gene_name="PTK2b")
        for tid in (real.pk, stub.pk):
            Antibody.objects.using(DB).create(
                target_id=tid, company_id=company.pk, catalogue_number="1-AP",
                lot_number="L1", site_id=site.pk)

        out = _run("--from", "PTK2b", "--into", "PTK2B", "--apply",
                   "--discard-duplicate-labels")
        self.assertEqual(Antibody.objects.using(DB).filter(target_id=stub.pk).count(), 1)
        self.assertIn("merge_duplicate_antibodies", out)
        self.assertTrue(Target.objects.using(DB).filter(pk=stub.pk).exists())

    def test_a_pair_differing_only_by_case_resolves_to_two_rows(self):
        """The lookup has to be exact first. Case-insensitively, 'PTK2b' and
        'PTK2B' are one string — so an iexact lookup hands back the same row for
        both sides and the command refuses itself as "the same target", which is
        precisely the pair it exists to fix."""
        real = Target.objects.using(DB).create(gene_name="PTK2B", uniprot_id="Q14289")
        stub = Target.objects.using(DB).create(gene_name="PTK2b")
        from pipeline.management.commands.merge_targets import Command
        resolve = Command()._target
        self.assertEqual(resolve("PTK2B").pk, real.pk)
        self.assertEqual(resolve("PTK2b").pk, stub.pk)

    def test_a_spelling_that_matches_both_by_case_is_refused_by_name(self):
        """'ptk2b' is neither row's spelling and matches both — a coin toss the
        caller has to settle, so the refusal offers both ids."""
        Target.objects.using(DB).create(gene_name="PTK2B", uniprot_id="Q14289")
        Target.objects.using(DB).create(gene_name="PTK2b")
        with self.assertRaises(CommandError) as caught:
            _run("--from", "ptk2b", "--into", "PTK2B")
        message = str(caught.exception)
        self.assertIn("differing only by case", message)
        self.assertIn("id:", message)

    def test_a_case_insensitive_spelling_still_works_when_it_is_unambiguous(self):
        Target.objects.using(DB).create(gene_name="RAB32", uniprot_id="Q13637")
        from pipeline.management.commands.merge_targets import Command
        self.assertEqual(Command()._target("rab32").gene_name, "RAB32")
