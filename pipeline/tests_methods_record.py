"""The Copy methods button and the report methods behind it (30 Sep 2026).

Pinned here is what would be silently wrong rather than loudly broken: an
amount printed under the wrong antibody, a button under a figure that has no
methods, a missing value written as a placeholder, and a backfill that
overwrites a lab value it had no business touching.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase

from pipeline.models import (Antibody, AntibodyMethod, Company, ExperimentSession,
                             Member, MethodsRecord, PublicationImage, Report, Site,
                             Target, WbResult)
from pipeline.services import methods_backfill as B
from pipeline.services import methods_text as M
from OGA_website.public_snapshot import REASON, withheld

DOI = "https://doi.org/10.5281/zenodo.1234567"
WB_CONDITIONS = {"lysis_buffer": "RIPA buffer", "protein_loading_ug": "40",
                 "membrane": "nitrocellulose", "blocking": "5% milk, 1 hr"}


class TheParagraphLeavesOutWhatWasNotStatedTests(SimpleTestCase):

    def test_a_missing_value_is_not_written_about(self):
        text = M.paragraph("WB", {"blocking": "5% milk"}, "Abcam ab1", "")
        self.assertEqual(
            text, "Membranes were blocked with 5% milk. The primary antibody was Abcam ab1.")

    def test_the_extractors_notes_to_itself_are_removed(self):
        self.assertEqual(M.clean("0.5 µg/mL (Table 3 working concentration)"), "0.5 µg/mL")
        self.assertEqual(M.clean("HRP anti-rabbit per Table 4"), "HRP anti-rabbit")
        self.assertEqual(M.clean("methods text says ~0.2 µg/ml"), "~0.2 µg/ml")
        self.assertEqual(M.clean("not stated in methods text"), "")

    def test_every_record_on_file_reads_without_a_placeholder(self):
        if withheld(B.DATA_FILE):
            self.skipTest(REASON)
        data = B.load()
        for run in data["run_conditions"]:
            app = B.FIGURE_APP[run["app"]]
            text = M.paragraph(app, M.clean_conditions(app, run["conditions"]),
                               "Supplier cat1 (RRID:AB_1)", "1/500")
            for bad in ("None", "[", "not stated", "methods text", "per Table", " ."):
                self.assertNotIn(bad, text, f"{run['record_id']}: {bad!r}")


class _Figures(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Abcam")
        cls.target = Target.objects.create(gene_name="TGM2", protein_name="TGM2")
        Report.objects.create(target=cls.target, zenodo_doi=DOI,
                              zenodo_date=date(2024, 3, 15))
        cls.ab1 = Antibody.objects.create(target=cls.target, company=cls.company,
                                          catalogue_number="ab111", rrid="AB_111",
                                          wb_recommended=True)
        cls.ab2 = Antibody.objects.create(target=cls.target, company=cls.company,
                                          catalogue_number="ab222", wb_recommended=True)
        cls.ab3 = Antibody.objects.create(target=cls.target, company=cls.company,
                                          catalogue_number="ab333")
        # Tested by western blot and not supported: its methods are on file,
        # and must not be offered until the result says otherwise.
        cls.ab4 = Antibody.objects.create(target=cls.target, company=cls.company,
                                          catalogue_number="ab444")
        for ab in (cls.ab1, cls.ab2, cls.ab4):
            PublicationImage.objects.create(antibody=ab, application_type="WB",
                                            image=f"publication_images/2026/{ab.catalogue_number}_WB.png")
        PublicationImage.objects.create(antibody=cls.ab3, application_type="IP",
                                        image="publication_images/2026/ab333_IP.png")
        cls.record = MethodsRecord.objects.create(target=cls.target, application="WB",
                                                  conditions=WB_CONDITIONS)
        AntibodyMethod.objects.create(record=cls.record, antibody=cls.ab1,
                                      amount="1/500", basis="report_named")
        AntibodyMethod.objects.create(record=cls.record, antibody=cls.ab4,
                                      amount="1/1000", basis="report_named")


class EachFigureCopiesItsOwnAntibodysMethodsTests(_Figures):

    def test_the_amount_is_on_its_own_antibody_and_nowhere_else(self):
        out = M.for_antibodies([self.ab1, self.ab2, self.ab3])
        one = out[self.ab1.pk]["WB"]["text"]
        self.assertIn("The primary antibody, Abcam ab111 (RRID:AB_111), was diluted 1/500.", one)
        self.assertIn(f"Method as published in {DOI}.", one)
        two = out[self.ab2.pk]["WB"]["text"]
        self.assertIn("The primary antibody was Abcam ab222.", two)
        self.assertNotIn("1/500", two)

    def test_no_methods_without_both_a_figure_and_a_record(self):
        out = M.for_antibodies([self.ab3])
        # ab333 has an IP figure but IP has no record; it has a WB record but no WB figure.
        self.assertEqual(out, {})

    def test_not_offered_under_a_negative_and_offered_once_regraded(self):
        self.assertNotIn(self.ab4.pk, M.for_antibodies([self.ab4]))
        Antibody.objects.filter(pk=self.ab4.pk).update(wb_recommended=True)
        ab4 = Antibody.objects.get(pk=self.ab4.pk)
        text = M.for_antibodies([ab4])[ab4.pk]["WB"]["text"]
        self.assertIn("Abcam ab444, was diluted 1/1000", text)

    def test_the_gene_page_draws_one_button_per_figure_that_has_methods(self):
        html = Client().get("/antibodies/TGM2/").content.decode()
        self.assertEqual(html.count('class="copy-methods"'), 2)
        self.assertIn('data-methods="Western blot', html)
        self.assertIn("copy_methods", html)

    def test_the_api_carries_the_same_paragraph(self):
        rows = Client().get("/api/v1/antibodies/").json()["antibodies"]
        by_name = {r["antibody_name"]: r for r in rows}
        wb = by_name["ab111"]["oga_methods"]["WB"]
        self.assertEqual((wb["amount"], wb["amount_basis"], wb["source"]),
                         ("1/500", "report_named", DOI))
        self.assertEqual(wb["text"], M.for_antibodies([self.ab1])[self.ab1.pk]["WB"]["text"])
        self.assertEqual(by_name["ab333"]["oga_methods"], {})


class TheBackfillTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        site = Site.objects.create(name="McGill", short_code="MCG")
        u = User(username="riham")
        u.save(using="pipeline_db")
        member = Member.objects.create(user_id=u.pk, site_id=site.pk, role="experimenter",
                                       is_active=True, display_name="Riham")
        company = Company.objects.create(name="Proteintech")
        self.target = Target.objects.create(gene_name="ACE", protein_name="ACE")
        self.abs = {}
        for cat in ("AA-1", "BB-2", "CC-3"):
            ab = Antibody.objects.create(target=self.target, company=company,
                                         catalogue_number=cat)
            PublicationImage.objects.create(antibody=ab, application_type="WB",
                                            image=f"publication_images/2026/{cat}_WB.png")
            self.abs[cat] = ab
        self.session = ExperimentSession.objects.create(
            procedure_type="WB", target=self.target, experimenter=member, site=site,
            date=date(2023, 3, 1), status="complete",
            session_conditions={"lysis_buffer": "kept as typed"})
        self.rows = {
            "AA-1": WbResult.objects.create(session=self.session, antibody=self.abs["AA-1"]),
            "BB-2": WbResult.objects.create(session=self.session, antibody=self.abs["BB-2"],
                                            dilution="1/5000", primary_ab_dilution="1/5000"),
            "CC-3": WbResult.objects.create(session=self.session, antibody=self.abs["CC-3"],
                                            dilution="1/200", primary_ab_dilution="1/200"),
        }

    def _data(self):
        def value(rid, cat, val, action, detail, evidence="Antibody named with value"):
            return {"record_id": rid, "gene": "ACE", "app": "WB", "catalogue": cat,
                    "report_value": val, "evidence_type": evidence, "where": "p.9",
                    "report_id": "r1", "action": action, "action_detail": detail,
                    "db_target": f"pipeline_wbresult #{self.rows[cat].pk}",
                    "db_field": "dilution + primary_ab_dilution",
                    "session_id": str(self.session.pk)}
        return {
            "run_column_targets": {"WB": {
                "lysis_buffer": "session_conditions.lysis_buffer",
                "blocking": "session_conditions.blocking",
                "membrane": "wbresult.membrane"}},
            "reports": {"r1": {"file": "ACE.pdf", "year": "2023"}},
            "run_conditions": [{
                "record_id": "RUN-1", "gene": "ACE", "app": "WB", "report_id": "r1",
                "report_file": "ACE.pdf", "report_year": "2023", "action": "FILL",
                "session_ids": str(self.session.pk),
                "conditions": {"lysis_buffer": "RIPA", "blocking": "5% milk, 1 hr",
                               "membrane": "nitrocellulose"}}],
            "antibody_values": [
                value("AB-1", "AA-1", "1/500", "UPDATE ROW", "#1: DB value empty"),
                value("AB-2", "BB-2", "1/500", "CHECK — DB DIFFERS", "DB has 1/5000"),
                value("AB-3", "CC-3", "1/1000", "CHECK — DB DIFFERS", "DB has 1/200",
                      evidence="General statement (antibody not named with value)"),
            ],
            "dilution_differences": {
                "AB-2": {"group": "DB error", "db_value": "1/5000", "category": "H7"},
                "AB-3": {"group": "Report error", "db_value": "1/200", "category": "H5"},
            },
        }

    def _run(self, *args):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(self._data(), fh)
        call_command("load_methods_record", "--file", fh.name, *args,
                     stdout=open("/dev/null", "w"))

    def _cell(self, cat):
        return WbResult.objects.get(pk=self.rows[cat].pk)

    def test_the_dry_run_writes_nothing(self):
        self._run()
        self.assertFalse(MethodsRecord.objects.exists())
        self.assertEqual(self._cell("AA-1").dilution, "")
        self.assertEqual(self._cell("BB-2").dilution, "1/5000")

    def test_the_accepted_hypotheses_are_applied_and_nothing_else(self):
        self._run("--apply")
        # Blank filled; a DB error corrected; a report error leaves the lab value.
        self.assertEqual((self._cell("AA-1").dilution, self._cell("AA-1").primary_ab_dilution),
                         ("1/500", "1/500"))
        self.assertEqual(self._cell("BB-2").dilution, "1/500")
        self.assertEqual(self._cell("CC-3").dilution, "1/200")
        # A condition somebody typed stays; a blank one is filled.
        conds = ExperimentSession.objects.get(pk=self.session.pk).session_conditions
        self.assertEqual(conds["lysis_buffer"], "kept as typed")
        self.assertEqual(conds["blocking"], "5% milk, 1 hr")
        self.assertEqual(self._cell("CC-3").membrane, "nitrocellulose")
        # What the button prints: the report's value, except where the report
        # was the one in error.
        printed = {m.antibody.catalogue_number: (m.amount, m.basis)
                   for m in AntibodyMethod.objects.select_related("antibody")}
        self.assertEqual(printed, {"AA-1": ("1/500", "report_named"),
                                   "BB-2": ("1/500", "report_named"),
                                   "CC-3": ("1/200", "lab_record")})

    def test_a_correction_is_refused_where_the_cell_has_changed_since(self):
        WbResult.objects.filter(pk=self.rows["BB-2"].pk).update(
            dilution="1/300", primary_ab_dilution="1/300")
        plan = B.plan(self._data())
        self.assertIn("changed since the extraction", plan.left)
        B.apply(plan)
        self.assertEqual(self._cell("BB-2").dilution, "1/300")

    def test_a_second_run_changes_nothing(self):
        self._run("--apply")
        plan = B.plan(self._data())
        self.assertEqual({r["status"] for r in plan.records}, {"same"})
        self.assertEqual({m["status"] for m in plan.antibody_methods}, {"same"})
        self.assertEqual(plan.row_fills, [])
        self.assertEqual(plan.session_fills, [])


class OnePrimaryOneSecondaryTests(SimpleTestCase):
    """A partner reads the paragraph as describing one antibody's blot."""

    CONDS = {"cell_lines": "SK-N-FI WT and ACE KO; concentrated culture medium; "
                           "Fig 1B HAP1 WT medium",
             "secondary_antibody": "peroxidase-conjugated goat anti-rabbit (Thermo 65-6120) "
                                   "and anti-mouse (62-6520)"}

    def test_the_secondary_matching_the_host_is_named_alone(self):
        mouse = M.paragraph("WB", M.clean_conditions("WB", self.CONDS), "Bio-Techne MAB9291",
                            "1/500", "mouse")
        self.assertIn("peroxidase-conjugated goat anti-mouse (Thermo 62-6520)", mouse)
        self.assertNotIn("anti-rabbit", mouse)
        rabbit = M.paragraph("WB", M.clean_conditions("WB", self.CONDS), "X", "1/500", "rabbit")
        self.assertIn("goat anti-rabbit (Thermo 65-6120)", rabbit)
        self.assertNotIn("anti-mouse", rabbit)

    def test_another_figures_samples_are_not_this_antibodys(self):
        text = M.paragraph("WB", M.clean_conditions("WB", self.CONDS), "X", "1/500", "mouse")
        self.assertIn("Samples of SK-N-FI WT and ACE KO, concentrated culture medium, were prepared", text)
        self.assertNotIn("Fig", text)

    def test_a_printed_order_the_catalogue_contradicts_is_corrected(self):
        sec = "Peroxidase-conjugated goat anti-mouse and anti-rabbit (Thermo Fisher 65-6120 and 62-6520)"
        self.assertEqual(M.choose_secondary(sec, "mouse"),
                         "Peroxidase-conjugated goat anti-mouse (Thermo Fisher 62-6520)")

    def test_a_host_the_report_does_not_name_keeps_the_report_s_list(self):
        sec = "HRP goat anti-rabbit (Thermo 65-6120) and anti-mouse (62-6520)"
        self.assertEqual(M.choose_secondary(sec, "rat"), sec)
        self.assertEqual(M.choose_secondary(sec, ""), sec)

    def test_a_concentration_per_species_follows_the_host(self):
        self.assertEqual(M.choose_dilution("0.05 µg/mL (anti-rabbit), 0.5 µg/mL (anti-mouse); "
                                           "1 hr at room temperature", "mouse"),
                         "0.5 µg/mL; 1 hr at room temperature")

    def test_no_composed_paragraph_carries_an_extraction_note(self):
        import re
        if withheld(B.DATA_FILE):
            self.skipTest(REASON)
        bad = re.compile(r"\bFig\b|Figure|\bTable\b|legend|as printed|printed '|working "
                         r"concentration|secondary table|listed (in|under)|\(methods\)|\(\)")
        for run in B.load()["run_conditions"]:
            app = B.FIGURE_APP[run["app"]]
            for host in ("rabbit", "mouse", ""):
                text = M.paragraph(app, M.clean_conditions(app, run["conditions"]),
                                   "S cat1", "1/500", host)
                self.assertIsNone(bad.search(text), f"{run['record_id']} {host}: {text}")
                plain = re.sub(r"(?:^|\s)[A-Z]\)", "", text)  # "probed with A) … or B) …"
                self.assertEqual(plain.count("("), plain.count(")"), run["record_id"])


class TheManifestCarriesTheMethodsTests(_Figures):

    def test_a_supportive_figure_s_row_carries_its_paragraph(self):
        from core import api_manifest
        from core.models import APIConsumer
        consumer = APIConsumer.objects.create(name="Test", consumer_type="manufacturer", tier="data")
        rows, _ = api_manifest._rows(PublicationImage.objects.select_related(
            "antibody__target", "antibody__company"), consumer, {self.target.pk}, True)
        by_cat = {r["catalogue_number"]: r for r in rows}
        self.assertEqual(by_cat["ab111"]["oga_methods_text"],
                         M.for_antibodies([self.ab1])[self.ab1.pk]["WB"]["text"])
        self.assertEqual(by_cat["ab444"]["oga_methods_text"], "")  # not supportive
        self.assertEqual(api_manifest.CSV_COLUMNS[-1], "oga_methods_text")


class SessionsFromTheReportsTests(TestCase):
    """`methods_sessions`: created, split, or filled — decided from the rows."""
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        from pipeline.models import Site
        self.site = Site.objects.create(name="McGill", short_code="MCG")
        u = User(username="vera")
        u.save(using="pipeline_db")
        self.member = Member.objects.create(user_id=u.pk, site_id=self.site.pk,
                                            role="experimenter", is_active=True)
        self.company = Company.objects.create(name="Abcam")

    def _gene(self, name, cats):
        target = Target.objects.create(gene_name=name, protein_name=name)
        abs_ = {}
        for cat in cats:
            ab = Antibody.objects.create(target=target, company=self.company,
                                         catalogue_number=cat, site=self.site)
            PublicationImage.objects.create(antibody=ab, application_type="WB",
                                            image=f"publication_images/2026/{name}_{cat}_WB.png")
            abs_[cat] = ab
        return target, abs_

    @staticmethod
    def _data(runs, values):
        return {"run_column_targets": {"WB": {
                    "lysis_buffer": "session_conditions.lysis_buffer",
                    "membrane": "wbresult.membrane"}},
                "reports": {}, "dilution_differences": {},
                "run_conditions": runs, "antibody_values": values}

    @staticmethod
    def _run(rid, gene, report, action="CREATE SESSION", year="2024", sid=""):
        return {"record_id": rid, "gene": gene, "app": "WB", "report_id": report,
                "report_file": f"{report}.pdf", "report_year": year, "site": "McGill",
                "action": action, "session_ids": sid,
                "conditions": {"lysis_buffer": f"RIPA ({report})", "membrane": "nitrocellulose",
                               "cell_lines": "HAP1 WT and KO"}}

    @staticmethod
    def _value(gene, report, cat, value):
        return {"record_id": f"{report}-{cat}", "gene": gene, "app": "WB", "catalogue": cat,
                "report_value": value, "evidence_type": "Antibody named with value",
                "where": "", "report_id": report, "action": "CREATE ROW",
                "action_detail": "", "db_target": "", "db_field": "", "session_id": ""}

    def test_a_run_no_session_holds_gets_a_session_that_says_how_it_was_made(self):
        from pipeline.services import methods_sessions as MS
        self._gene("RAB8A", ["A1", "B2"])
        data = self._data([self._run("R1", "RAB8A", "r1")],
                          [self._value("RAB8A", "r1", "A1", "1/500"),
                           self._value("RAB8A", "r1", "B2", "1/1000")])
        plan = MS.plan(data)
        self.assertFalse(ExperimentSession.objects.exists())  # a plan writes nothing
        MS.apply(plan)
        session = ExperimentSession.objects.get()
        self.assertEqual((session.date, session.site_id, session.status),
                         (date(2024, 1, 1), self.site.pk, "complete"))
        self.assertIn("Nobody entered this session at the bench", session.comments)
        self.assertEqual(str(session.experimenter), MS.PLACEHOLDER_NAME)
        self.assertFalse(session.experimenter.is_active)
        self.assertEqual(session.session_conditions["lysis_buffer"], "RIPA (r1)")
        rows = {r.antibody.catalogue_number: r for r in session.wb_results.all()}
        self.assertEqual((rows["A1"].dilution, rows["B2"].dilution), ("1/500", "1/1000"))
        self.assertEqual(rows["A1"].membrane, "nitrocellulose")
        self.assertIn("nobody entered this row at the bench", rows["A1"].comments)
        # Run again: the session now holds the run, so nothing more is made.
        again = MS.plan(data)
        self.assertEqual((again.creates, again.row_adds, again.splits), ([], [], []))

    def test_a_create_label_over_a_session_that_holds_the_run_fills_it_instead(self):
        from pipeline.services import methods_sessions as MS
        target, abs_ = self._gene("PLCG2", ["A1", "B2", "C3"])
        session = ExperimentSession.objects.create(
            procedure_type="WB", target=target, experimenter=self.member, site=self.site,
            date=date(2023, 1, 1), status="complete", session_conditions={})
        for i, cat in enumerate(["A1", "B2"]):
            WbResult.objects.create(session=session, antibody=abs_[cat], access_id=100 + i)
        data = self._data([self._run("R1", "PLCG2", "r1")],
                          [self._value("PLCG2", "r1", c, "1/500") for c in ("A1", "B2", "C3")])
        MS.apply(MS.plan(data))
        self.assertEqual(ExperimentSession.objects.count(), 1)
        rows = {r.antibody.catalogue_number: r.dilution for r in session.wb_results.all()}
        self.assertEqual(rows, {"A1": "1/500", "B2": "1/500", "C3": "1/500"})
        self.assertEqual(ExperimentSession.objects.get().session_conditions["lysis_buffer"],
                         "RIPA (r1)")

    def test_a_session_holding_two_runs_is_split_where_batch_and_report_agree(self):
        from pipeline.services import methods_sessions as MS
        target, abs_ = self._gene("CD44", ["A1", "B2"])
        session = ExperimentSession.objects.create(
            procedure_type="WB", target=target, experimenter=self.member, site=self.site,
            date=date(2021, 1, 1), status="complete", session_conditions={})
        old = [WbResult.objects.create(session=session, antibody=abs_[c], access_id=10 + i,
                                       dilution="1/500", rating="YES")
               for i, c in enumerate(["A1", "B2"])]
        new = [WbResult.objects.create(session=session, antibody=abs_[c], access_id=2300 + i,
                                       dilution="1/2000", rating="NO")
               for i, c in enumerate(["A1", "B2"])]
        data = self._data(
            [self._run("R1", "CD44", "r2021", "SPLIT SESSION", "2021", str(session.pk)),
             self._run("R2", "CD44", "r2025", "SPLIT SESSION", "2025", str(session.pk))],
            [self._value("CD44", "r2021", c, "1/500") for c in ("A1", "B2")]
            + [self._value("CD44", "r2025", c, "1/2000") for c in ("A1", "B2")])
        plan = MS.plan(data)
        self.assertEqual(len(plan.splits), 1)
        MS.apply(plan)
        self.assertEqual(set(session.wb_results.values_list("pk", flat=True)), {r.pk for r in old})
        split = ExperimentSession.objects.exclude(pk=session.pk).get()
        self.assertEqual(set(split.wb_results.values_list("pk", flat=True)), {r.pk for r in new})
        self.assertEqual(list(split.wb_results.values_list("rating", flat=True)), ["NO", "NO"])
        self.assertIn(f"Split", split.comments)
        self.assertEqual(split.date, date(2025, 1, 1))
        self.assertEqual(MS.plan(data).splits, [])

    def test_one_report_and_a_differing_value_is_not_a_second_run(self):
        from pipeline.services import methods_sessions as MS
        target, abs_ = self._gene("CTSB", ["A1", "B2", "C3"])
        session = ExperimentSession.objects.create(
            procedure_type="WB", target=target, experimenter=self.member, site=self.site,
            date=date(2021, 1, 1), status="complete", session_conditions={})
        WbResult.objects.create(session=session, antibody=abs_["A1"], access_id=1, dilution="1/500")
        WbResult.objects.create(session=session, antibody=abs_["B2"], access_id=2, dilution="1/500")
        WbResult.objects.create(session=session, antibody=abs_["C3"], access_id=900, dilution="20 µl")
        data = self._data([self._run("R1", "CTSB", "r1", "SPLIT SESSION", sid=str(session.pk))],
                          [self._value("CTSB", "r1", c, "1/500") for c in ("A1", "B2", "C3")])
        plan = MS.plan(data)
        self.assertEqual((plan.splits, plan.creates), ([], []))


class StoredConditionsAreCleanedOnReadTests(_Figures):
    """A rule added after a record was loaded reaches the button with no reload."""

    def test_a_figure_reference_stored_before_the_rule_is_not_printed(self):
        MethodsRecord.objects.filter(pk=self.record.pk).update(conditions={
            **WB_CONDITIONS,
            "cell_lines": "HeLa WT and TGM2 KO; Fig 1B compares HeLa, HEK293T, HAP1"})
        text = M.for_antibodies([self.ab1])[self.ab1.pk]["WB"]
        self.assertIn("Lysates of HeLa WT and TGM2 KO were prepared", text["text"])
        self.assertNotIn("Fig", text["text"])
        self.assertEqual(text["conditions"]["cell_lines"], "HeLa WT and TGM2 KO")
