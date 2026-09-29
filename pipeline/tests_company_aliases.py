"""A supplier typed under another name it is known by is that supplier.

`resolve_company` matched on `name` alone, so "Santa Cruz Biotechnology" (the
public name of the record called `Santa-Cruz`), "Structural Genomics Consortium"
(SGC's), and "Developmental Studies Hybridoma Bank" (`… (DSHB)` without its
bracket) each created a second supplier. DSHB ended up as three records and IPI
as two, and a supplier split in two can make each half look like the only
source for an antibody.
"""
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from pipeline.models import Antibody, Company, Target
from pipeline.services.cropper import db as cdb

DB = "pipeline_db"


class ASupplierIsFoundByTheNamesItIsKnownByTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.dshb = Company.objects.using(DB).create(
            name="Developmental Studies Hybridoma Bank (DSHB)", display_name="DSHB")
        cls.scbt = Company.objects.using(DB).create(
            name="Santa-Cruz", display_name="Santa Cruz Biotechnology")
        cls.novus = Company.objects.using(DB).create(
            name="Bio-Techne (Novus Biologicals)", display_name="Bio-Techne")
        cls.rnd = Company.objects.using(DB).create(
            name="Bio-Techne (R&D Systems)", display_name="Bio-Techne")

    def _resolve(self, vendor, catalogue=""):
        return cdb.resolve_company(vendor, catalogue, create=False, db=DB)

    def test_the_public_name_and_both_halves_of_a_bracket_resolve(self):
        for typed in ("Developmental Studies Hybridoma Bank", "DSHB", "dshb"):
            with self.subTest(typed=typed):
                self.assertEqual(self._resolve(typed).pk, self.dshb.pk)
        self.assertEqual(self._resolve("Santa Cruz Biotechnology").pk, self.scbt.pk)

    def test_creating_does_not_mint_a_second_record(self):
        before = Company.objects.using(DB).count()
        got = cdb.resolve_company("Santa Cruz Biotechnology", "sc-1", create=True, db=DB)
        self.assertEqual(got.pk, self.scbt.pk)
        self.assertEqual(Company.objects.using(DB).count(), before)

    def test_the_preview_names_the_record_it_will_be_stored_under(self):
        self.assertEqual(cdb.resolved_company_name("DSHB", db=DB), self.dshb.name)

    def test_the_display_name_wins_over_a_second_row_of_that_name(self):
        """A supplier is its display name (owner, 29 Sep 2026): an empty
        `BD Biosciences` row beat `BD Bioscience`, whose display name it is,
        and every paste of that spelling landed under the empty one."""
        Company.objects.using(DB).create(name="DSHB")
        self.assertEqual(self._resolve("DSHB").pk, self.dshb.pk)

    def test_a_brand_typed_in_full_is_that_brand(self):
        self.assertEqual(self._resolve("Bio-Techne (Novus Biologicals)", "AF1").pk,
                         self.novus.pk)

    def test_a_shared_public_name_is_still_split_by_catalogue(self):
        self.assertEqual(self._resolve("Bio-Techne", "NB100-1").pk, self.novus.pk)
        self.assertEqual(self._resolve("Bio-Techne", "AF3130").pk, self.rnd.pk)

    def test_an_unknown_supplier_is_still_unknown(self):
        self.assertIsNone(self._resolve("Hybridoma Bank"))


class AssignAntibodyCompanyTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.sgc = Company.objects.using(DB).create(name="SGC")
        cls.other = Company.objects.using(DB).create(name="abcam")
        target = Target.objects.using(DB).create(gene_name="PFN1")
        cls.blank = Antibody.objects.using(DB).create(
            target_id=target.pk, catalogue_number="AC-PFN1-4")
        cls.taken = Antibody.objects.using(DB).create(
            target_id=target.pk, catalogue_number="ab1", company_id=cls.other.pk)

    def _run(self, *pairs, apply=False):
        args = [*pairs, "--company", str(self.sgc.pk)] + (["--apply"] if apply else [])
        call_command("assign_antibody_company", *args, stdout=StringIO())

    def test_a_dry_run_writes_nothing_and_apply_writes(self):
        self._run(f"{self.blank.pk}=AC-PFN1-4")
        self.blank.refresh_from_db()
        self.assertIsNone(self.blank.company_id)
        self._run(f"{self.blank.pk}=AC-PFN1-4", apply=True)
        self.blank.refresh_from_db()
        self.assertEqual(self.blank.company_id, self.sgc.pk)

    def test_a_refusal_anywhere_writes_nothing(self):
        """A supplier already on file is never overwritten, and a pk whose
        catalogue does not match is refused — and either stops the whole run."""
        for bad in (f"{self.taken.pk}=ab1", f"{self.blank.pk}=AC-PFN1-5"):
            with self.subTest(bad=bad), self.assertRaises(CommandError):
                self._run(f"{self.blank.pk}=AC-PFN1-4", bad, apply=True)
        self.blank.refresh_from_db()
        self.taken.refresh_from_db()
        self.assertIsNone(self.blank.company_id)
        self.assertEqual(self.taken.company_id, self.other.pk)


class SuppliersAreDeduplicatedByDisplayNameTests(TestCase):
    """Live, 29 Sep 2026: `Thermo Fisher`, `Cell Signaling`, `Millipore` and
    `BD Biosciences` each sat as an empty second supplier beside the real one,
    and a vial filed there fell out of that manufacturer's portal key."""

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.thermo = Company.objects.using(DB).create(name="Thermo Fisher Scientific")
        cls.cst = Company.objects.using(DB).create(name="Cell Signaling Technology")
        cls.sciences = Company.objects.using(DB).create(name="Cell Sciences")
        cls.bd = Company.objects.using(DB).create(name="BD Bioscience",
                                                  display_name="BD Biosciences")
        cls.retired = Company.objects.using(DB).create(name="Thermo Fisher",
                                                       is_active=False)

    def _resolve(self, vendor, create=False):
        return cdb.resolve_company(vendor, "", create=create, db=DB)

    def test_a_short_form_lands_on_the_one_supplier_it_names(self):
        self.assertEqual(self._resolve("Thermo Fisher").pk, self.thermo.pk)
        self.assertEqual(self._resolve("Cell Signaling").pk, self.cst.pk)
        self.assertEqual(self._resolve("BD Bioscience").pk, self.bd.pk)
        self.assertEqual(self._resolve("bd biosciences").pk, self.bd.pk)

    def test_a_prefix_two_suppliers_share_is_not_guessed(self):
        self.assertIsNone(self._resolve("Cell"))

    def test_a_new_supplier_is_a_request_not_a_list_entry(self):
        acme = self._resolve("Acme Antibodies", create=True)
        self.assertFalse(acme.is_active)
        self.assertEqual(self._resolve("Acme Antibodies", create=True).pk, acme.pk,
                         "a second paste joins the first request")
        self.assertNotIn("Acme Antibodies", cdb.canonical_suppliers(db=DB))
        self.assertEqual(cdb.pending_suppliers(db=DB), [],
                         "nothing filed under it yet, so nothing to ask about")
        target = Target.objects.using(DB).create(gene_name="ACME1")
        Antibody.objects.using(DB).create(target=target, company=acme,
                                          catalogue_number="AC-1")
        self.assertEqual([(c.pk, n) for c, n in cdb.pending_suppliers(db=DB)],
                         [(acme.pk, 1)])

    def test_the_picker_offers_one_entry_per_display_name(self):
        offered = cdb.canonical_suppliers(db=DB)
        self.assertIn("BD Biosciences", offered)
        self.assertNotIn("BD Bioscience", offered)
        self.assertNotIn("Thermo Fisher", offered)

    def test_a_portal_key_sees_every_row_of_its_supplier_whatever_the_case(self):
        from core.api_views import _get_supplier_company_ids
        self.assertEqual(set(_get_supplier_company_ids("thermo fisher scientific")),
                         {self.thermo.pk})
        self.assertEqual(set(_get_supplier_company_ids("BD Biosciences")),
                         {self.bd.pk})
