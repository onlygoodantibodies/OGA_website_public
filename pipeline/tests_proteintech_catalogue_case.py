"""Proteintech's ``-Ig`` suffix, and the 19 live rows that spell it wrong.

``-Ig`` is Proteintech's mouse-monoclonal class, capital ``I`` for
immunoglobulin. On live ``pipeline_db`` 11 rows carry ``-lg`` (lowercase L) and
8 carry ``-ig`` (lowercase i), and they came in that way — both spellings are in
``access_csvs/Antibodies.csv``, so it is the 2019 Access transcription rather
than anything this app wrote. In a sans-serif face ``I`` and ``l`` are one
glyph, which is why reading it never caught it.

The two classes are not equally harmful and the command must not present them as
though they were. ``l`` is a different **letter** from ``i``, so the 11 miss
every lookup in this repo. The 8 are matched correctly today by all of them —
``__iexact``, ``__icontains``, and the extension's ``normaliseKey`` — and are
merely wrong on screen and in exports.

What this file mostly pins is the refusal. ``67322-1-lg`` and ``67322-1-ig`` are
one EPHX2 product recorded twice, and the trap is that the database would accept
correcting both: ``unique_antibody_per_site_lot`` compares ``varchar``
case-sensitively, so ``-Ig`` beside ``-ig`` raises nothing, and what is left is
two rows that every case-insensitive reader matches at once — a ``.first()``
coin toss, silently. A collision check written against the constraint would pass
this and ship the defect.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from pipeline.models import Antibody, Company, Site, Target
from pipeline.tests_timeouts import DB


class ProteintechCatalogueCaseCommandTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.proteintech = Company.objects.using(DB).create(name="Proteintech")
        cls.site = Site.objects.using(DB).create(
            name="McGill", short_code="MCG", is_active=True)

    def _antibody(self, catalogue, gene, *, company=None, lot="1"):
        target = Target.objects.using(DB).filter(gene_name=gene).first()
        if target is None:
            target = Target.objects.using(DB).create(gene_name=gene)
        return Antibody.objects.using(DB).create(
            catalogue_number=catalogue,
            company=company or self.proteintech,
            target=target, site=self.site, lot_number=lot)

    def _run(self, *args):
        out = StringIO()
        call_command("fix_proteintech_catalogue_case", *args,
                     stdout=out, stderr=out)
        return out.getvalue()

    # --- the 11 -----------------------------------------------------------

    def test_a_lowercase_L_is_corrected_and_named_as_breaking(self):
        ab = self._antibody("67499-1-lg", "PRDX6")
        output = self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "67499-1-Ig")
        self.assertIn("Breaks matching", output)

    def test_a_dry_run_writes_nothing_and_names_the_change(self):
        ab = self._antibody("60316-1-lg", "VCP")
        output = self._run()
        self.assertIn("60316-1-lg", output)
        self.assertIn("60316-1-Ig", output)
        self.assertIn("DRY RUN", output)
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "60316-1-lg")

    # --- the 8 ------------------------------------------------------------

    def test_a_lowercase_i_is_corrected_but_reported_as_cosmetic(self):
        """It matches correctly today — every reader is case-insensitive — so
        presenting it beside the 11 would overstate what is broken."""
        ab = self._antibody("66499-1-ig", "MAPT")
        output = self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "66499-1-Ig")
        self.assertIn("Cosmetic", output)

    def test_breaking_only_leaves_the_cosmetic_rows_alone(self):
        breaking = self._antibody("60333-1-lg", "TMEM106B")
        cosmetic = self._antibody("66645-1-ig", "TLR2")

        self._run("--breaking-only", "--apply")

        breaking.refresh_from_db(using=DB)
        cosmetic.refresh_from_db(using=DB)
        self.assertEqual(breaking.catalogue_number, "60333-1-Ig")
        self.assertEqual(cosmetic.catalogue_number, "66645-1-ig")

    # --- the refusal ------------------------------------------------------

    def test_the_two_spellings_of_one_product_are_refused_and_neither_moves(self):
        """The EPHX2 pair. Correcting both puts one string on two rows, and the
        database would not stop it: `unique_antibody_per_site_lot` compares
        case-sensitively, so `-Ig` beside `-ig` is accepted and every
        `__iexact` reader then matches both. That is a merge decision, with a
        human in front of it."""
        lg = self._antibody("67322-1-lg", "EPHX2", lot="10012475")
        ig = self._antibody("67322-1-ig", "EPHX2", lot="10012475")

        output = self._run("--apply")

        self.assertIn("Left alone", output)
        self.assertIn("merge", output)
        self.assertIn("67322-1-Ig", output)
        lg.refresh_from_db(using=DB)
        ig.refresh_from_db(using=DB)
        self.assertEqual([lg.catalogue_number, ig.catalogue_number],
                         ["67322-1-lg", "67322-1-ig"])

    def test_a_row_already_holding_the_corrected_number_blocks_the_change(self):
        wrong = self._antibody("66820-1-lg", "PRDX1", lot="a")
        right = self._antibody("66820-1-Ig", "PRDX1", lot="b")

        output = self._run("--apply")

        self.assertIn("Left alone", output)
        self.assertIn(f"id {right.pk}", output)
        wrong.refresh_from_db(using=DB)
        self.assertEqual(wrong.catalogue_number, "66820-1-lg")

    # --- what it must never do -------------------------------------------

    def test_an_already_correct_row_is_not_a_candidate(self):
        ab = self._antibody("67501-1-Ig", "RAB2A")
        output = self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "67501-1-Ig")
        self.assertIn("nothing to change", output)

    def test_a_non_proteintech_row_is_left_alone_by_name(self):
        """`-Ig` is this vendor's convention and nobody else's, so a candidate
        filed elsewhere is refused rather than assumed."""
        other = Company.objects.using(DB).create(name="Abcam")
        ab = self._antibody("12345-1-lg", "SETX", company=other)

        output = self._run("--apply")

        self.assertIn("Left alone", output)
        self.assertIn("Abcam", output)
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "12345-1-lg")

    def test_it_changes_the_suffix_and_never_the_digits(self):
        """The digits are the product. A transform that could also renumber one
        is not a plan anybody can check by eye against live data."""
        from pipeline.management.commands import fix_proteintech_catalogue_case as cmd

        for stored in ("60316-1-lg", "66242-1-lg", "68514-1-ig", "81180-2-ig",
                       "67499-1-lg"):
            with self.subTest(stored=stored):
                self.assertEqual(cmd.corrected(stored)[:-2], stored[:-2])
                self.assertTrue(cmd.corrected(stored).endswith("-Ig"))

    def test_other_proteintech_classes_are_untouched(self):
        """`-AP`, `-RR`, `-AG` and the CoraLite conjugates are real product
        classes, and 237 rows on file carry one."""
        for catalogue in ("67002-1-AP", "80001-1-RR", "16555-1-AG",
                          "CL488-66184", "Biotin-60293"):
            with self.subTest(catalogue=catalogue):
                ab = self._antibody(catalogue, "TARDBP", lot=catalogue)
                self._run("--apply")
                ab.refresh_from_db(using=DB)
                self.assertEqual(ab.catalogue_number, catalogue)
