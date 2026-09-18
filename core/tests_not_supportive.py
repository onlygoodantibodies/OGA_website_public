"""The not-supportive review list: what it must never say.

This list names commercial products as having failed. There is exactly one way
it can be wrong that nobody would catch, and it is the one
``core/recommendations.py`` was written about: reporting an application nobody
ran as an application that failed. A supplier reading that has been told their
product failed a test that was never performed on it, and nothing on the page
would contradict it.

So the shape of this file is: the right rows are in it, the wrong rows are not,
and the two rungs of *not supportive* stay told apart.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from core import not_supportive as NS
from core import recommendations as R
from core.models import APIConsumer
from django.contrib.auth.models import User

from pipeline.models import (Antibody, Company, ExperimentSession, Member,
                             PublicationImage, Site, Target, WbResult)


def _png(colour=(30, 90, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (80, 40), colour).save(buf, "PNG")
    return buf.getvalue()


def _figure(antibody, application):
    from django.core.files.base import ContentFile

    image = PublicationImage(antibody=antibody, application_type=application)
    image.image.save(f"{antibody.catalogue_number}_{application}.png",
                     ContentFile(_png()), save=False)
    image.save()
    return image


class TheListNamesOnlyWhatWasTestedTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.abcam = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.other = Company.objects.create(name="Proteintech")

        # A curated gene: something on it carries a recommendation, which is
        # what makes a missing flag mean "tested and not supportive".
        cls.curated = Target.objects.create(gene_name="TRPA1")
        cls.passer = Antibody.objects.create(
            target=cls.curated, company=cls.abcam, catalogue_number="ab-good",
            wb_recommended=True)
        _figure(cls.passer, "WB")

        # The row this list is for: two applications tested, neither supportive.
        cls.failer = Antibody.objects.create(
            target=cls.curated, company=cls.abcam, catalogue_number="ab-bad")
        _figure(cls.failer, "WB")
        _figure(cls.failer, "IP")

        # Tested for WB and failed it, supportive for IP. Not on the list: the
        # question is "no supportive result anywhere", not "failed something".
        cls.mixed = Antibody.objects.create(
            target=cls.curated, company=cls.abcam, catalogue_number="ab-mixed",
            ip_recommended=True)
        _figure(cls.mixed, "WB")
        _figure(cls.mixed, "IP")

        # An uncurated gene. The flags are False on every antibody because
        # nobody has set them yet, NOT because anything failed.
        cls.uncurated = Target.objects.create(gene_name="SNCA")
        cls.untested = Antibody.objects.create(
            target=cls.uncurated, company=cls.abcam, catalogue_number="ab-uncurated")
        _figure(cls.untested, "WB")

        # Another supplier's failure, for the scoping assertions.
        cls.theirs = Antibody.objects.create(
            target=cls.curated, company=cls.other, catalogue_number="pt-bad")
        _figure(cls.theirs, "WB")

        # No figure at all — never tested, so absent rather than failing.
        cls.no_figure = Antibody.objects.create(
            target=cls.curated, company=cls.abcam, catalogue_number="ab-nofigure")

        cls.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")
        cls.registry = APIConsumer.objects.create(
            name="Antibody Registry", consumer_type="rrid")

    def setUp(self):
        cache.clear()

    def _catalogues(self, **kwargs):
        return {row["catalogue_number"] for row in NS.rows(**kwargs)}

    def test_an_antibody_that_failed_every_tested_application_is_listed(self):
        self.assertIn("ab-bad", self._catalogues())

    def test_an_antibody_with_any_supportive_application_is_not_listed(self):
        """The membership test is *no* supportive result, not *a* failure."""
        listed = self._catalogues()
        self.assertNotIn("ab-mixed", listed)
        self.assertNotIn("ab-good", listed)

    def test_an_uncurated_genes_antibody_is_not_reported_as_failing(self):
        """The defect this list exists not to have.

        Every flag on an uncurated gene is ``False`` because nobody has set it.
        Read as a verdict, that is a public claim that a named commercial
        product failed a test nobody ran on it.
        """
        self.assertNotIn("ab-uncurated", self._catalogues())

    def test_an_antibody_with_no_figure_at_all_is_absent(self):
        self.assertNotIn("ab-nofigure", self._catalogues())

    def test_the_row_counts_tested_and_failed_apart(self):
        """"Failed all four" and "failed the only one anybody ran" differ."""
        row = next(r for r in NS.rows() if r["catalogue_number"] == "ab-bad")
        self.assertEqual(row["applications_tested"], 2)
        self.assertEqual(row["applications_not_supportive"], 2)
        self.assertEqual({f["application"] for f in row["findings"]},
                         {"WB", "IP"})

    def test_a_scoped_key_sees_only_its_own_reagents(self):
        listed = self._catalogues(company_ids=[self.abcam.pk])
        self.assertIn("ab-bad", listed)
        self.assertNotIn("pt-bad", listed,
                         "A supplier was shown another supplier's failure.")

    def test_an_unscoped_reader_sees_every_supplier(self):
        self.assertEqual({"ab-bad", "pt-bad"}, self._catalogues())


class TheTwoRungsStayToldApartTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.target = Target.objects.create(gene_name="ATP2B1")
        Antibody.objects.create(target=cls.target, company=cls.company,
                                catalogue_number="ab-curates",
                                wb_recommended=True)
        site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", "pipeline_db"):
            user = User(username="vera")
            user.set_password("pw")
            user.save(using=alias)
        member = Member.objects.create(
            user_id=User.objects.using("pipeline_db").get(username="vera").pk,
            site_id=site.pk, role="experimenter", is_active=True,
            display_name="Vera")
        cls.session = ExperimentSession.objects.create(
            procedure_type="WB", target=cls.target, status="complete",
            experimenter=member, site=site, date=date(2026, 2, 18))

        # Detects the target and is not selective: a negative the data still
        # says something on-target about — *Limited support*, drawn amber.
        cls.limited = Antibody.objects.create(
            target=cls.target, company=cls.company, catalogue_number="ab-limited")
        _figure(cls.limited, "WB")
        WbResult.objects.create(session=cls.session, antibody=cls.limited,
                                signal="YES", rating="NO")

        # Nothing on-target seen at all — *Not supportive*, drawn red.
        cls.flat = Antibody.objects.create(
            target=cls.target, company=cls.company, catalogue_number="ab-flat")
        _figure(cls.flat, "WB")
        WbResult.objects.create(session=cls.session, antibody=cls.flat,
                                signal="NO", rating="NO")

    def _finding(self, catalogue):
        # `include_limited` because one of these two IS the limited-support
        # case, and the default list is the hard edge that excludes it.
        row = next(r for r in NS.rows(include_limited=True)
                   if r["catalogue_number"] == catalogue)
        return row["findings"][0]

    def test_limited_support_is_named_as_limited_support(self):
        finding = self._finding("ab-limited")
        self.assertEqual(finding["finding"], R.LIMITED_WORDS)
        self.assertEqual(finding["oga_recommendation"], R.NOT_RECOMMENDED)
        self.assertIn("detects the target", finding["sentence"])

    def test_nothing_on_target_is_named_not_supportive(self):
        self.assertEqual(self._finding("ab-flat")["finding"],
                         R.CELL_WORDS[R.NOT_RECOMMENDED])

    def test_the_summary_counts_the_two_rungs_apart(self):
        totals = NS.summary(NS.rows(include_limited=True))
        self.assertEqual(totals["limited_support"], 1)
        self.assertEqual(totals["not_supportive"], 1)
        self.assertEqual(totals["findings"], 2)

    def test_the_words_are_the_gene_pages_own(self):
        """A supplier's row and the public page must not word one fact twice."""
        self.assertEqual(
            {f["finding"] for row in NS.rows(include_limited=True)
             for f in row["findings"]},
            {R.LIMITED_WORDS, R.CELL_WORDS[R.NOT_RECOMMENDED]})

    def test_limited_support_is_off_by_default_and_the_tick_adds_it(self):
        """The default list is the hard edge; the tick widens it.

        And the tick moves whole ANTIBODIES, never individual findings — an
        antibody kept with its limited-support result dropped would draw a card
        reading "1 of 2 came back without support" over one result, a count
        disagreeing with the list under it.
        """
        default = {r["catalogue_number"] for r in NS.rows()}
        widened = {r["catalogue_number"] for r in NS.rows(include_limited=True)}
        self.assertEqual(default, {"ab-flat"})
        self.assertEqual(widened, {"ab-flat", "ab-limited"})

        # Nothing in the default list holds a limited-support result at all.
        for row in NS.rows():
            self.assertFalse(row["has_limited_support"])
            self.assertNotIn(R.LIMITED_WORDS,
                             {f["finding"] for f in row["findings"]})

    def test_a_widened_antibody_keeps_every_one_of_its_results(self):
        row = next(r for r in NS.rows(include_limited=True)
                   if r["catalogue_number"] == "ab-limited")
        self.assertEqual(row["applications_not_supportive"], len(row["findings"]))


class TheDownloadsAreTheSameSetTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.target = Target.objects.create(gene_name="TRPA1")
        Antibody.objects.create(target=cls.target, company=cls.company,
                                catalogue_number="ab-curates", wb_recommended=True)
        cls.failer = Antibody.objects.create(
            target=cls.target, company=cls.company, catalogue_number="ab-bad",
            rrid="AB_123")
        _figure(cls.failer, "WB")
        _figure(cls.failer, "IP")
        cls.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")

    def setUp(self):
        cache.clear()

    def _get(self, name):
        return self.client.get(reverse(name),
                               HTTP_X_API_KEY=str(self.consumer.api_key))

    def test_the_json_manifest_counts_the_rows_it_ships(self):
        """A count and the list it totals come from one queryset."""
        body = self._get("api:not_supportive").json()
        self.assertEqual(body["manifest"]["antibodies"], len(body["antibodies"]))
        self.assertEqual(
            body["manifest"]["findings"],
            sum(len(r["findings"]) for r in body["antibodies"]))

    def test_the_csv_is_one_row_per_application_finding(self):
        response = self._get("api:not_supportive_csv")
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["application"] for r in rows}, {"WB", "IP"})
        self.assertEqual({r["catalogue_number"] for r in rows}, {"ab-bad"})
        self.assertEqual(list(rows[0].keys()), list(NS.CSV_COLUMNS))

    def test_the_csv_carries_the_antibody_level_counts_on_every_row(self):
        text = self._get("api:not_supportive_csv").content.decode("utf-8-sig")
        for row in csv.DictReader(io.StringIO(text)):
            self.assertEqual(row["applications_tested"], "2")
            self.assertEqual(row["applications_not_supportive"], "2")

    def test_the_reply_carries_every_figure_the_page_will_draw(self):
        """A PDF carried these figures and was withdrawn (owner, 14 Sep 2026).

        Every figure in it was an object-storage read on a four-thread site, so
        the document had to be budgeted — and a budgeted document sometimes
        arrives short of the pictures it exists for. The page has no budget:
        the browser fetches the images itself. So the promise moved here, where
        it can actually be kept, and this is what keeps it.
        """
        body = self._get("api:not_supportive").json()
        drawn = [entry for row in body["antibodies"]
                 for entry in row["applications"] if entry["figure_url"]]
        self.assertEqual(len(drawn), body["manifest"]["figures"])
        self.assertEqual({entry["application"] for entry in drawn}, {"WB", "IP"})

    def test_both_endpoints_require_a_key(self):
        for name in ("api:not_supportive", "api:not_supportive_csv"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 401)

    def test_the_gene_filter_narrows_the_downloads_and_the_manifest_together(self):
        """The count on the button and the contents of the file are one set.

        The filter reaches the downloads so a supplier can get a *complete*
        PDF for one gene where the whole list would run past the time the
        server may spend reading figures. That is only safe while the manifest
        moves with it — a file quietly holding a different set from the count
        beside it is the disagreement this codebase treats as data loss.
        """
        other = Target.objects.create(gene_name="SNCA")
        Antibody.objects.create(target=other, company=self.company,
                                catalogue_number="ab-curates-2", wb_recommended=True)
        second = Antibody.objects.create(target=other, company=self.company,
                                         catalogue_number="ab-other")
        _figure(second, "WB")

        whole = self._get("api:not_supportive").json()
        self.assertEqual(whole["manifest"]["antibodies"], 2)

        narrowed = self.client.get(
            reverse("api:not_supportive"), {"gene": "TRPA1"},
            HTTP_X_API_KEY=str(self.consumer.api_key)).json()
        self.assertEqual(narrowed["manifest"]["antibodies"], 1)
        self.assertEqual(len(narrowed["antibodies"]), 1)
        self.assertEqual(narrowed["gene"], "TRPA1")

        text = self.client.get(
            reverse("api:not_supportive_csv"), {"gene": "TRPA1"},
            HTTP_X_API_KEY=str(self.consumer.api_key)
        ).content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual({r["gene"] for r in rows}, {"TRPA1"})
        self.assertEqual(len(rows), narrowed["manifest"]["findings"])

    def test_a_demo_keys_gene_list_is_a_ceiling_not_a_starting_point(self):
        """Asking for a gene outside the key's scope narrows, never widens."""
        limited = APIConsumer.objects.create(
            name="Demo", consumer_type="manufacturer",
            supplier_filter="Abcam", gene_filter="SNCA")
        body = self.client.get(
            reverse("api:not_supportive"), {"gene": "TRPA1"},
            HTTP_X_API_KEY=str(limited.api_key)).json()
        self.assertEqual(body["manifest"]["antibodies"], 0)

    def test_neither_endpoint_may_be_cached_by_a_shared_cache(self):
        """A supplier's own list under a URL with no key in it."""
        for name in ("api:not_supportive", "api:not_supportive_csv"):
            with self.subTest(name=name):
                response = self._get(name)
                # GZipMiddleware adds Accept-Encoding; the key is what matters.
                self.assertIn("X-API-Key", response["Vary"])
                self.assertIn("private", response["Cache-Control"])


class TheStripShowsAllFourApplicationsTests(TestCase):
    """The card is read as one picture, so it draws the whole strip.

    Two things it must never get wrong, and both are silent. An application
    nobody ran must not be drawn as a failure — that is the defect this whole
    list is built around, arriving through the strip instead of through the
    count. And nothing here may ever be supportive: an antibody is on this page
    only because none of its applications was, so a green chip would mean the
    membership rule had broken, and a supplier would be looking at a product
    listed as failing beside evidence that it works.
    """
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.target = Target.objects.create(gene_name="TRPA1")
        Antibody.objects.create(target=cls.target, company=cls.company,
                                catalogue_number="ab-curates", wb_recommended=True)
        cls.failer = Antibody.objects.create(
            target=cls.target, company=cls.company, catalogue_number="ab-bad")
        _figure(cls.failer, "WB")
        _figure(cls.failer, "IP")

    def _strip(self):
        row = next(r for r in NS.rows() if r["catalogue_number"] == "ab-bad")
        return {a["application"]: a for a in row["applications"]}

    def test_all_four_applications_are_present_in_the_printed_order(self):
        row = next(r for r in NS.rows() if r["catalogue_number"] == "ab-bad")
        self.assertEqual([a["application"] for a in row["applications"]],
                         list(R.APPLICATIONS))

    def test_an_application_nobody_ran_says_not_tested_and_carries_no_figure(self):
        strip = self._strip()
        for application in ("ICC-IF", "FC"):
            with self.subTest(application=application):
                entry = strip[application]
                self.assertFalse(entry["tested"])
                self.assertEqual(entry["finding"], R.UNTESTED_WORDS)
                self.assertEqual(entry["oga_recommendation"], R.NOT_TESTED)
                self.assertEqual(entry["figure_url"], "")
                # No tone means no colour: it is drawn grey, never red.
                self.assertEqual(entry["tone"], "")

    def test_a_tested_application_is_a_finding_with_its_figure(self):
        strip = self._strip()
        for application in ("WB", "IP"):
            with self.subTest(application=application):
                entry = strip[application]
                self.assertTrue(entry["tested"])
                self.assertEqual(entry["oga_recommendation"], R.NOT_RECOMMENDED)
                self.assertTrue(entry["figure_url"])

    def test_no_row_in_this_list_ever_carries_a_supportive_application(self):
        """The membership rule, asserted where it would be seen to break."""
        for include in (False, True):
            for row in NS.rows(include_limited=include):
                for entry in row["applications"]:
                    self.assertNotEqual(entry["oga_recommendation"], R.RECOMMENDED)

    def test_every_published_figure_the_antibody_has_is_on_the_strip(self):
        """What the withdrawn PDF could not promise, the page can.

        Every tested application of a listed antibody is a finding — it is here
        only because none of its applications is supportive — so every figure
        it has is drawn. The only absences are applications with no figure at
        all, and those say *Not tested* rather than going quiet.
        """
        from pipeline.models import PublicationImage

        row = next(r for r in NS.rows() if r["catalogue_number"] == "ab-bad")
        stored = set(PublicationImage.objects.using(NS.DB)
                     .filter(antibody=self.failer)
                     .values_list("application_type", flat=True))
        drawn = {e["application"] for e in row["applications"] if e["figure_url"]}
        self.assertEqual(drawn, stored)
        for entry in row["applications"]:
            if entry["application"] not in stored:
                self.assertEqual(entry["finding"], R.UNTESTED_WORDS)
                self.assertEqual(entry["figure_url"], "")

    def test_the_strip_is_drawn_and_the_findings_are_counted(self):
        """Two lists, two questions — the strip's length is not a count."""
        row = next(r for r in NS.rows() if r["catalogue_number"] == "ab-bad")
        self.assertEqual(len(row["applications"]), 4)
        self.assertEqual(len(row["findings"]), 2)
        self.assertEqual(row["applications_not_supportive"], 2)
        self.assertEqual(row["applications_tested"], 2)


class TheReferenceExampleIsPerGeneTests(TestCase):
    """A negative is only legible beside a supportive result on the same gene.

    Three things worth pinning, each one a way the reference could mislead: it
    must prefer a clean supportive result over a caveated one, it must carry
    that caveat when it has to fall back, and it must never point at the very
    antibody it is a reference for.
    """
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.abcam = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.other = Company.objects.create(name="Proteintech")
        cls.target = Target.objects.create(gene_name="ATP2B1")

        site = Site.objects.create(name="Leicester", short_code="LEI")
        for alias in ("academy_db", "pipeline_db"):
            user = User(username="rex")
            user.set_password("pw")
            user.save(using=alias)
        member = Member.objects.create(
            user_id=User.objects.using("pipeline_db").get(username="rex").pk,
            site_id=site.pk, role="experimenter", is_active=True,
            display_name="Rex")
        cls.session = ExperimentSession.objects.create(
            procedure_type="WB", target=cls.target, status="complete",
            experimenter=member, site=site, date=date(2026, 2, 18))

        # Supportive, and the data fell short on selectivity — a caveated
        # example, which is the fallback and not the preference.
        cls.caveated = Antibody.objects.create(
            target=cls.target, company=cls.other, catalogue_number="ab-caveated",
            wb_recommended=True)
        _figure(cls.caveated, "WB")
        WbResult.objects.create(session=cls.session, antibody=cls.caveated,
                                signal="YES", rating="NO")

        # The one this should pick: supportive, nothing held back.
        cls.clean = Antibody.objects.create(
            target=cls.target, company=cls.other, catalogue_number="ab-clean",
            wb_recommended=True)
        _figure(cls.clean, "WB")
        WbResult.objects.create(session=cls.session, antibody=cls.clean,
                                signal="YES", rating="YES")

        cls.failer = Antibody.objects.create(
            target=cls.target, company=cls.abcam, catalogue_number="ab-bad")
        _figure(cls.failer, "WB")
        WbResult.objects.create(session=cls.session, antibody=cls.failer,
                                signal="NO", rating="NO")

    def test_a_clean_supportive_example_beats_a_caveated_one(self):
        references = NS.reference_figures(["ATP2B1"])
        example = references["ATP2B1"]["WB"]
        self.assertEqual(example["catalogue_number"], "ab-clean")
        self.assertFalse(example["caveated"])
        self.assertTrue(example["figure_url"])

    def test_a_caveated_example_is_used_when_it_is_all_there_is_and_says_so(self):
        self.clean.wb_recommended = False
        self.clean.save()
        references = NS.reference_figures(["ATP2B1"])
        example = references["ATP2B1"]["WB"]
        self.assertEqual(example["catalogue_number"], "ab-caveated")
        self.assertTrue(example["caveated"])
        # The limitation travels with it: a reference whose own caveat is
        # hidden is worse than no reference.
        self.assertIn("not selective", example["sentence"])

    def test_a_gene_with_nothing_supportive_gets_no_example(self):
        self.clean.wb_recommended = False
        self.clean.save()
        self.caveated.wb_recommended = False
        self.caveated.save()
        self.assertEqual(NS.reference_figures(["ATP2B1"]), {})

    def test_the_example_never_points_at_the_row_it_is_an_example_for(self):
        rows = NS.rows(company_ids=[self.other.pk], include_limited=True)
        NS.attach_references(rows, NS.reference_figures(["ATP2B1"]))
        for row in rows:
            for entry in row["applications"]:
                reference = entry.get("reference")
                if reference:
                    self.assertNotEqual(reference["catalogue_number"],
                                        row["catalogue_number"])

    def test_each_application_picks_its_own_example_antibody(self):
        """One gene, four applications, up to four different products.

        The best western blot on a gene and the best immunoprecipitation on it
        are routinely different antibodies, so the example is chosen per
        (gene, application) and never once per gene. Collapsing it would put
        one product's name against a figure another product produced.
        """
        # `ab-caveated` is the only IP on this gene, so IP must land on it
        # while WB stays on `ab-clean`.
        _figure(self.caveated, "IP")
        self.caveated.ip_recommended = True
        self.caveated.save()

        references = NS.reference_figures(["ATP2B1"])["ATP2B1"]
        self.assertEqual(references["WB"]["catalogue_number"], "ab-clean")
        self.assertEqual(references["IP"]["catalogue_number"], "ab-caveated")
        self.assertNotEqual(references["WB"]["catalogue_number"],
                            references["IP"]["catalogue_number"])

    def test_the_example_is_not_scoped_to_the_caller(self):
        """The question is what a good result on this gene looks like."""
        rows = NS.rows(company_ids=[self.abcam.pk])
        NS.attach_references(rows, NS.reference_figures(["ATP2B1"]))
        strip = {a["application"]: a for a in rows[0]["applications"]}
        self.assertEqual(strip["WB"]["reference"]["catalogue_number"], "ab-clean")
        self.assertEqual(strip["WB"]["reference"]["supplier"], "Proteintech")


class TheListIsOrderedByGeneTests(TestCase):
    """The page groups by gene, so the rows have to arrive grouped.

    Sorting in the browser would be a second answer to which gene comes first,
    and the CSV and the PDF would then disagree with the page.
    """
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Abcam", display_name="Abcam")
        for gene in ("SNCA", "ATP2B1", "TRPA1"):
            target = Target.objects.create(gene_name=gene)
            Antibody.objects.create(target=target, company=company,
                                    catalogue_number=f"ab-curates-{gene}",
                                    wb_recommended=True)
            for n in (1, 2):
                failer = Antibody.objects.create(
                    target=target, company=company,
                    catalogue_number=f"ab-{gene}-{n}")
                _figure(failer, "WB")

    def test_rows_arrive_grouped_by_gene(self):
        genes = [row["gene"] for row in NS.rows()]
        self.assertEqual(genes, sorted(genes))
        # And each gene's rows are contiguous, which is what lets the page
        # break sections by walking the list once.
        seen = []
        for gene in genes:
            if not seen or seen[-1] != gene:
                seen.append(gene)
        self.assertEqual(len(seen), len(set(seen)))
