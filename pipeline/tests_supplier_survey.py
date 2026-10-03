"""``import_supplier_survey`` writes what the survey found, and only where it still holds.

The silent shapes: a pk naming a different reagent, a later board edit
overwritten by a 3 Oct reading, and a catalogue-search miss taking a product off
its gene page. Each is pinned here, along with the link half, since a
``URLField`` validates nothing on save.
"""
import csv
import pathlib
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from pipeline.management.commands.import_supplier_survey import DEFAULT_CSV
from pipeline.models import Antibody, Company, Target

COLUMNS = ["id", "catalogue_number", "company", "gene", "out_of_market_now",
           "set_out_of_market", "current_supplier_url", "set_supplier_url",
           "clear_supplier_url", "evidence", "evidence_type", "note"]


class SupplierSurveyTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="GeneTex")
        target = Target.objects.create(protein_name="Epoxide hydrolase 2",
                                       gene_name="EPHX2")
        cls.ab = Antibody.objects.create(
            target=target, company=company, catalogue_number="GTX84570",
            supplier_url="https://old.example/GTX84570")

    def _run(self, *rows, apply=False):
        path = pathlib.Path(tempfile.mkdtemp()) / "survey.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            for row in rows:
                w.writerow({"id": self.ab.pk, "catalogue_number": "GTX84570",
                            "out_of_market_now": "FALSE",
                            "current_supplier_url": "https://old.example/GTX84570",
                            **row})
        out = StringIO()
        call_command("import_supplier_survey", str(path),
                     *(["--apply"] if apply else []), stdout=out)
        self.ab.refresh_from_db()
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        report = self._run({"set_out_of_market": "TRUE",
                            "evidence_type": "on_page_statement"})
        self.assertFalse(self.ab.out_of_market)
        self.assertIn("DRY RUN", report)

    def test_a_statement_withdraws_and_a_link_is_replaced(self):
        self._run({"set_out_of_market": "TRUE", "evidence_type": "on_page_statement",
                   "set_supplier_url": "https://www.genetex.com/GTX84570"},
                  apply=True)
        self.assertTrue(self.ab.out_of_market)
        self.assertEqual(self.ab.supplier_url, "https://www.genetex.com/GTX84570")

    def test_a_search_miss_is_held_but_its_link_still_clears(self):
        report = self._run({"set_out_of_market": "TRUE",
                            "evidence_type": "catalogue_search_miss",
                            "clear_supplier_url": "TRUE"}, apply=True)
        self.assertFalse(self.ab.out_of_market)
        self.assertEqual(self.ab.supplier_url, "")
        self.assertIn("not written", report)

    def test_a_catalogue_number_that_disagrees_is_refused(self):
        report = self._run({"catalogue_number": "GTX99999",
                            "clear_supplier_url": "TRUE"}, apply=True)
        self.assertEqual(self.ab.supplier_url, "https://old.example/GTX84570")
        self.assertIn("Refused", report)

    def test_a_link_edited_since_the_survey_is_left_alone(self):
        report = self._run({"current_supplier_url": "https://other.example/x",
                            "set_supplier_url": "https://www.genetex.com/GTX84570"},
                           apply=True)
        self.assertEqual(self.ab.supplier_url, "https://old.example/GTX84570")
        self.assertIn("changed since", report)

    def test_the_survey_read_links_without_their_query_string(self):
        # The first live dry run (3 Oct) refused 24 rows nobody had edited:
        # the survey dropped ?srsltid=… tracking codes and search terms, and
        # read a bare catalogue number in the link cell as empty.
        Antibody.objects.filter(pk=self.ab.pk).update(
            supplier_url="https://old.example/GTX84570?srsltid=AfmBOo")
        self._run({"set_supplier_url": "https://www.genetex.com/GTX84570"},
                  apply=True)
        self.assertEqual(self.ab.supplier_url, "https://www.genetex.com/GTX84570")

        Antibody.objects.filter(pk=self.ab.pk).update(supplier_url="GTX84570*")
        report = self._run({"current_supplier_url": "",
                            "set_supplier_url": "https://www.genetex.com/x"},
                           apply=True)
        self.assertEqual(self.ab.supplier_url, "https://www.genetex.com/x")
        self.assertIn("Link added (1)", report)

    def test_a_different_address_behind_the_query_is_still_refused(self):
        Antibody.objects.filter(pk=self.ab.pk).update(
            supplier_url="https://other.example/GTX84570?srsltid=AfmBOo")
        report = self._run({"set_supplier_url": "https://www.genetex.com/x"},
                           apply=True)
        self.assertIn("changed since", report)

    def test_a_link_that_is_not_one_is_refused(self):
        report = self._run({"set_supplier_url": "www.genetex.com/GTX84570"},
                           apply=True)
        self.assertEqual(self.ab.supplier_url, "https://old.example/GTX84570")
        self.assertIn("not a usable link", report)

    def test_the_committed_file_holds_back_exactly_the_three_search_misses(self):
        with open(DEFAULT_CSV, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 292)
        misses = sorted(r["id"] for r in rows
                        if r["evidence_type"] == "catalogue_search_miss")
        self.assertEqual(misses, ["415", "4560", "4561"])
        withdrawals = [r for r in rows if r["set_out_of_market"] == "TRUE"]
        self.assertEqual(len(withdrawals), 16)
