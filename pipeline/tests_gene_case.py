"""Human gene symbols are uppercase, and ``orf`` is the exception.

Eight of the 584 targets on file are typed in the wrong case — ``DnaJC18``,
``Kif5a``, ``PTK2b``, ``Rab3C``, ``Rab40AL``, ``Rab44``, ``Rab45``, ``Rab46``
(twentieth field test, 5 Aug 2026). Human symbols are not written that way; the
*mouse* ones are, so anybody matching our export against a supplier list or a
UniProt query reads them as another organism's genes.

Two halves, and the second is why this is a module rather than one ``.upper()``
inline. The eight rows are corrected by ``manage.py fix_gene_case``, dry-run by
default. And the **write path** was uppercasing blindly in five places, so
``C9orf72`` — which three targets on file spell correctly, because lowercase
``orf`` is the HGNC convention — is stored as ``C9ORF72`` when it arrives
through UniProt today. The obvious fix to the eight *creates* that defect, which
is what most of this file is about.
"""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from pipeline.models import Target
from pipeline.services import gene_symbol, targets as target_svc
from pipeline.tests_timeouts import DB


class GeneSymbolCaseTests(TestCase):
    """What ``gene_symbol.canonical`` promises."""

    def test_the_eight_miscased_symbols_on_file_are_uppercased(self):
        found = gene_symbol.miscased(
            ["DnaJC18", "Kif5a", "PTK2b", "Rab3C", "Rab40AL",
             "Rab44", "Rab45", "Rab46"])
        self.assertEqual(
            found,
            [("DnaJC18", "DNAJC18"), ("Kif5a", "KIF5A"), ("PTK2b", "PTK2B"),
             ("Rab3C", "RAB3C"), ("Rab40AL", "RAB40AL"), ("Rab44", "RAB44"),
             ("Rab45", "RAB45"), ("Rab46", "RAB46")])

    def test_orf_symbols_are_left_alone(self):
        """The three on file are correct as they are — and a bare `.upper()`,
        which is what the app was doing, would break every one of them."""
        for symbol in ("C9orf72", "C9orf16", "C14orf119"):
            with self.subTest(symbol=symbol):
                self.assertEqual(gene_symbol.canonical(symbol), symbol)
                self.assertTrue(gene_symbol.is_canonical(symbol))
        self.assertEqual(gene_symbol.miscased(["C9orf72", "C14orf119"]), [])

    def test_an_orf_symbol_typed_in_any_case_comes_back_correct(self):
        for typed in ("C9ORF72", "c9orf72", "C9Orf72"):
            with self.subTest(typed=typed):
                self.assertEqual(gene_symbol.canonical(typed), "C9orf72")

    def test_it_changes_case_and_never_letters(self):
        """The whole safety argument for running this over live data: a plan a
        reader can check by eye. A tidy-up helper that can also change which
        letters are there is one nobody can review."""
        for symbol in ("Rab44", "C9orf72", "STMN2", "NKX2-1", "HLA-DRB5",
                       "TRPA1", "c14ORF119", "p65", "MT-CO1"):
            with self.subTest(symbol=symbol):
                self.assertEqual(gene_symbol.canonical(symbol).upper(),
                                 symbol.upper())

    def test_blank_and_NA_are_handed_back_untouched(self):
        """A row with no gene is a legitimate record, not a spelling to fix, and
        `targets.is_not_applicable` reads NA case-insensitively already."""
        self.assertEqual(gene_symbol.canonical(""), "")
        self.assertEqual(gene_symbol.canonical(None), "")
        self.assertEqual(gene_symbol.canonical("  "), "")
        self.assertEqual(gene_symbol.miscased(["", None, "  "]), [])


class TheWritePathStoresTheCanonicalSpellingTests(TestCase):
    """The half that would have re-created the defect.

    `resolve_or_create_target` and `bulk_targets` both did
    `(uniprot_symbol or typed).upper()`, so UniProt's own `C9orf72` was stored
    as `C9ORF72`. Fixing the eight rows without this would have left the door
    that made them open.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    def test_a_typed_symbol_is_stored_uppercased(self):
        with mock.patch("pipeline.services.uniprot.lookup_gene",
                        return_value={"found": False, "unavailable": False}):
            target, created = target_svc.resolve_or_create_target("Rab44")
        self.assertTrue(created)
        self.assertEqual(target.gene_name, "RAB44")

    def test_uniprots_own_orf_spelling_survives_the_write(self):
        with mock.patch("pipeline.services.uniprot.lookup_gene", return_value={
                "found": True, "unavailable": False, "gene_name": "C9orf72",
                "uniprot_id": "Q96LT7", "protein_name": "Guanine nucleotide "
                "exchange factor C9orf72", "mass_kda": 54.3,
                "gene_synonyms": ["ALSFTD"]}):
            target, created = target_svc.resolve_or_create_target("c9orf72")
        self.assertTrue(created)
        self.assertEqual(target.gene_name, "C9orf72")

    def test_an_existing_row_is_matched_however_it_is_cased(self):
        """Nothing here may create a second target for a gene already on file —
        the lookup was and stays case-insensitive."""
        Target.objects.using(DB).create(gene_name="C9orf72")
        with mock.patch("pipeline.services.uniprot.lookup_gene") as lookup:
            target, created = target_svc.resolve_or_create_target("C9ORF72")
        self.assertFalse(created)
        self.assertEqual(target.gene_name, "C9orf72")
        lookup.assert_not_called()


class FixGeneCaseCommandTests(TestCase):
    """The command that corrects the eight live rows."""

    databases = {"default", "pipeline_db", "academy_db"}

    def _run(self, *args):
        out = StringIO()
        call_command("fix_gene_case", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing_and_names_the_change(self):
        Target.objects.using(DB).create(gene_name="Rab44")
        output = self._run()
        self.assertIn("Rab44", output)
        self.assertIn("RAB44", output)
        self.assertIn("DRY RUN", output)
        self.assertEqual(
            Target.objects.using(DB).get(gene_name__iexact="rab44").gene_name,
            "Rab44")

    def test_apply_corrects_the_case(self):
        kif = Target.objects.using(DB).create(gene_name="Kif5a")
        ptk = Target.objects.using(DB).create(gene_name="PTK2b")
        self._run("--apply")
        kif.refresh_from_db(using=DB)
        ptk.refresh_from_db(using=DB)
        self.assertEqual([kif.gene_name, ptk.gene_name], ["KIF5A", "PTK2B"])

    def test_an_orf_target_is_not_a_candidate(self):
        target = Target.objects.using(DB).create(gene_name="C9orf72")
        output = self._run("--apply")
        self.assertIn("nothing to change", output)
        target.refresh_from_db(using=DB)
        self.assertEqual(target.gene_name, "C9orf72")

    def test_a_collision_is_refused_by_name_and_neither_row_is_touched(self):
        """`Target.gene_name` is UNIQUE, so correcting `Rab44` when `RAB44`
        already exists is a merge — two records for one gene, with antibodies
        and sessions on both — and that is a separate decision with a backup in
        front of it. Checked before the write, because on PostgreSQL a failed
        statement poisons the transaction and the handler that would explain
        the refusal cannot then run the query it needs."""
        miscased = Target.objects.using(DB).create(gene_name="Rab44")
        real = Target.objects.using(DB).create(gene_name="RAB44")

        output = self._run("--apply")

        self.assertIn("Left alone", output)
        self.assertIn(f"target {real.pk}", output)
        self.assertIn("merge", output)
        miscased.refresh_from_db(using=DB)
        real.refresh_from_db(using=DB)
        self.assertEqual([miscased.gene_name, real.gene_name],
                         ["Rab44", "RAB44"])

    def test_a_target_with_no_gene_is_ignored(self):
        Target.objects.using(DB).create(gene_name=None)
        self.assertIn("nothing to change", self._run())


class EveryDoorToAGeneStoresTheSameSpellingTests(TestCase):
    """Three doors create a target, and each had its own `.upper()`.

    The recurring shape in this repo is a rule enforced at one door and not
    another, and gene-symbol case was enforced at none of them: the paste box
    (`bulk_targets`), the feasibility page's single-gene Add, and every inline
    path (`resolve_or_create_target`, covered above). `C9orf72` is the case that
    tells them apart, because it is the one symbol a bare `.upper()` gets wrong
    — and it is what UniProt itself answers.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    UNIPROT_C9ORF72 = {
        "found": True, "unavailable": False, "gene_name": "C9orf72",
        "uniprot_id": "Q96LT7", "protein_name": "Guanine nucleotide exchange "
        "factor C9orf72", "mass_kda": 54.3, "gene_synonyms": ["ALSFTD"],
    }

    def setUp(self):
        from pipeline.models import Site
        from pipeline.tests_timeouts import _member_client
        self.site = Site.objects.using(DB).create(
            name="Leicester", short_code="LEI", is_active=True)
        self.client = _member_client(self, self.site)

    def test_the_paste_box_stores_uniprots_own_spelling(self):
        from pipeline.services import bulk_targets
        with mock.patch("pipeline.services.uniprot.lookup_gene",
                        return_value=self.UNIPROT_C9ORF72):
            preview = bulk_targets.plan(["C9ORF72"], site=self.site.pk)
        self.assertTrue(preview.get("ok"), preview)
        bulk_targets.apply(preview["rows"], site=self.site.pk)
        self.assertEqual(
            list(Target.objects.using(DB)
                 .filter(gene_name__iexact="C9ORF72")
                 .values_list("gene_name", flat=True)),
            ["C9orf72"])

    def test_the_feasibility_single_gene_add_stores_it_too(self):
        with mock.patch("pipeline.services.uniprot.lookup_gene",
                        return_value=self.UNIPROT_C9ORF72):
            resp = self.client.post(
                "/pipeline/feasibility/add/",
                data={"gene_name": "c9orf72"},
                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            list(Target.objects.using(DB)
                 .filter(gene_name__iexact="C9ORF72")
                 .values_list("gene_name", flat=True)),
            ["C9orf72"])

    def test_the_same_gene_typed_upper_is_not_added_twice(self):
        """The half that would break if only the spelling changed: `C9ORF72`
        typed at one door must still find the `C9orf72` another door stored,
        rather than reporting it as a rename or minting a second row."""
        Target.objects.using(DB).create(gene_name="C9orf72")
        with mock.patch("pipeline.services.uniprot.lookup_gene",
                        return_value=self.UNIPROT_C9ORF72):
            resp = self.client.post(
                "/pipeline/feasibility/add/",
                data={"gene_name": "C9ORF72"},
                content_type="application/json")
        body = resp.json()
        self.assertFalse(body.get("created"))
        self.assertIn("already exists", body.get("message", ""))
        self.assertEqual(
            Target.objects.using(DB).filter(gene_name__iexact="C9ORF72").count(), 1)
