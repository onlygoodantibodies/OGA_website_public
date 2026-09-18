"""Abcam's lowercase ``ab`` prefix, and the 69 live rows that shout it.

Abcam writes ``ab302677``; 69 of the 697 Abcam rows on live (30 Aug 2026) hold
``AB302677``. The case is invisible to every lookup in this repo — ``__iexact``,
``__icontains``, and the extension's ``normaliseKey`` all fold it — so nothing
is *broken* by it. It is wrong on the public gene page, in exports, and on
anything a scientist copies into a supplier's search, and it is the string the
Antibody Registry lookup sends verbatim.

What this file mostly pins is the **shape of the refusal**, because the obvious
version of it is wrong in a way live data does not reveal until it has run.
``fix_proteintech_catalogue_case`` groups its candidates by supplier +
catalogue number, which is right for a suffix mis-transcription and wrong here:
an antibody is ``(catalogue, company, target)`` while ``lot`` and ``site`` say
which vial, so one Abcam product held at McGill and at Leicester is two
legitimate rows. Ten such groups exist among these 69 — ``ab124807`` on OGA,
``AB32071`` on PARP1 and eight more — and every one is two benches with
different lot numbers. Refusing them would leave one product drawn as
``AB32071`` and ``ab32071`` on a single gene page, which is worse than either
doing all of it or none of it.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from pipeline.models import Antibody, Company, Site, Target
from pipeline.tests_timeouts import DB


class AbcamCatalogueCaseCommandTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.abcam = Company.objects.using(DB).create(name="abcam")
        cls.other = Company.objects.using(DB).create(name="Thermo Fisher")
        cls.mcgill = Site.objects.using(DB).create(
            name="McGill", short_code="MCG", is_active=True)
        cls.leicester = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)

    def _antibody(self, catalogue, gene, *, company=None, lot="1", site=None):
        target = Target.objects.using(DB).filter(gene_name=gene).first()
        if target is None:
            target = Target.objects.using(DB).create(gene_name=gene)
        return Antibody.objects.using(DB).create(
            catalogue_number=catalogue,
            company=company or self.abcam,
            target=target, site=site or self.mcgill, lot_number=lot)

    def _run(self, *args):
        out = StringIO()
        call_command("fix_abcam_catalogue_case", *args, stdout=out, stderr=out)
        return out.getvalue()

    # --- the correction ---------------------------------------------------

    def test_an_uppercase_prefix_is_lowercased(self):
        ab = self._antibody("AB302677", "NR3C1")
        self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "ab302677")

    def test_a_dry_run_writes_nothing_and_names_the_change(self):
        ab = self._antibody("AB305050", "NR3C1")
        output = self._run()
        self.assertIn("AB305050", output)
        self.assertIn("ab305050", output)
        self.assertIn("DRY RUN", output)
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "AB305050")

    def test_a_row_already_lowercase_is_not_a_candidate(self):
        ab = self._antibody("ab307605", "NR3C1")
        output = self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "ab307605")
        self.assertIn("nothing to change", output)

    # --- what it must not touch -------------------------------------------

    def test_a_non_abcam_row_is_never_touched(self):
        """``ab`` is Abcam's prefix and nobody else's. A row filed under
        another supplier is not this command's business, and is not even
        reported — it is not a candidate at all."""
        ab = self._antibody("AB123456", "MAPT", company=self.other)
        self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "AB123456")

    def test_a_pack_size_suffix_is_left_alone_and_named(self):
        ab = self._antibody("AB243904-100ul", "VCP")
        output = self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "AB243904-100ul")
        self.assertIn("not a plain AB<digits>", output)
        self.assertIn(f"id {ab.pk}", output)

    def test_the_row_holding_both_casings_is_left_alone_and_named(self):
        """Live holds one cell reading ``AB32071\\nab32071``. Lowercasing it
        would produce a catalogue number that is still two numbers and a
        newline, so the only safe answer is to say it was seen."""
        ab = self._antibody("AB32071\nab32071", "PARP1")
        output = self._run("--apply")
        ab.refresh_from_db(using=DB)
        self.assertEqual(ab.catalogue_number, "AB32071\nab32071")
        self.assertIn("left alone", output.lower())

    # --- the grouping, which is the whole point ---------------------------

    def test_one_product_at_two_benches_is_corrected_on_both_rows(self):
        """The case a supplier+catalogue grouping gets wrong. McGill and
        Leicester holding different lots of ``AB32071`` are two vials of one
        product, not one product recorded twice — so both are corrected.
        Refusing them would leave the gene page drawing one product under two
        spellings, which is the state this command exists to end."""
        mcgill = self._antibody("AB32071", "PARP1", lot="GR3369201-3",
                                site=self.mcgill)
        leicester = self._antibody("AB32071", "PARP1", lot="1029023-6",
                                   site=self.leicester)

        self._run("--apply")

        mcgill.refresh_from_db(using=DB)
        leicester.refresh_from_db(using=DB)
        self.assertEqual([mcgill.catalogue_number, leicester.catalogue_number],
                         ["ab32071", "ab32071"])

    def test_the_same_vial_recorded_twice_is_refused_and_neither_moves(self):
        """Same supplier, gene, lot and site: correcting both puts one string
        on one identity twice, and the database would not stop it —
        ``unique_antibody_per_site_lot`` compares ``varchar``
        case-sensitively, so ``AB68159`` beside ``ab68159`` is accepted and
        every ``__iexact`` reader then matches both, with ``.first()`` picking
        one arbitrarily. That is a merge decision, with a human in front."""
        upper = self._antibody("AB68159", "TLR2", lot="GR3433872-4")
        lower = self._antibody("ab68159", "TLR2", lot="GR3433872-4")

        output = self._run("--apply")

        self.assertIn("Refused", output)
        self.assertIn("merge", output)
        upper.refresh_from_db(using=DB)
        lower.refresh_from_db(using=DB)
        self.assertEqual([upper.catalogue_number, lower.catalogue_number],
                         ["AB68159", "ab68159"])

    def test_two_candidates_landing_on_one_identity_are_both_refused(self):
        """Neither row exists in the corrected spelling yet, so an occupant
        check alone passes both and writes the collision it was meant to
        prevent."""
        first = self._antibody("AB316189", "TLR2", lot="1085759-3")
        second = self._antibody("Ab316189", "TLR2", lot="1085759-3")

        output = self._run("--apply")

        self.assertIn("Refused", output)
        first.refresh_from_db(using=DB)
        second.refresh_from_db(using=DB)
        self.assertEqual([first.catalogue_number, second.catalogue_number],
                         ["AB316189", "Ab316189"])

    # --- confining a run --------------------------------------------------

    def test_ids_confines_the_run(self):
        wanted = self._antibody("AB302677", "NR3C1")
        untouched = self._antibody("AB305050", "NR3C1")

        self._run("--ids", str(wanted.pk), "--apply")

        wanted.refresh_from_db(using=DB)
        untouched.refresh_from_db(using=DB)
        self.assertEqual(wanted.catalogue_number, "ab302677")
        self.assertEqual(untouched.catalogue_number, "AB305050")
