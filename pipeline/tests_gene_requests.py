"""Genes the public asks for: what must not go silently wrong.

Four things here can be wrong without anything on any screen contradicting
them, which is the bar for a test in this repo:

* a **real gene refused** by the modification filter — PHOSPHO1 and SUMO1 are
  human genes whose symbols contain modification vocabulary, and turning one
  away looks to the reader exactly like the app working;
* a request **written into the wrong table** — a public wish landing in
  ``TargetNomination`` would be counted by the Portfolio as consortium work,
  and nothing on that page would look unusual;
* a request **lost to a UniProt outage** — the one attempt a stranger makes to
  tell us what they need, refused by a service we do not run;
* the home page's no-match panel **not rendering server-side**, which is the
  bug this whole feature started as: ``/?search=ZZZ`` drew an empty grid and
  said nothing at all.

The outage is simulated by patching ``requests`` rather than ``lookup_gene``:
that function catches ``RequestException`` itself and returns, so a fixture
raising from it exercises a branch the real path cannot reach.
"""
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from requests.exceptions import RequestException

from pipeline.models import GeneRequest, Target, TargetNomination
from pipeline.services import gene_requests as GR

DB = "pipeline_db"


def _uniprot_found(gene="SNCA", accession="P37840"):
    """A minimal UniProt search payload the real parser reads."""
    return {
        "results": [{
            "primaryAccession": accession,
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
            "proteinDescription": {
                "recommendedName": {"fullName": {"value": "Alpha-synuclein"}}},
            "genes": [{"geneName": {"value": gene}}],
            "sequence": {"molWeight": 14460},
        }]
    }


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class ModificationFilterTests(TestCase):
    """The filter is checked before UniProt, so a false positive is a refusal."""
    databases = {"academy_db", "pipeline_db"}

    def test_real_genes_whose_symbols_read_like_modifications_are_not_refused(self):
        for symbol in ("PHOSPHO1", "SUMO1", "UBC", "METTL3", "SIRT1", "NEDD4"):
            with self.subTest(symbol=symbol):
                self.assertFalse(GR.looks_like_modification(symbol))

    def test_modification_requests_are_recognised(self):
        for text in ("phospho-tau", "acetylated histone H3", "anti-pS129 SNCA",
                     "phospho tau Ser396", "cleaved caspase 3"):
            with self.subTest(text=text):
                self.assertTrue(GR.looks_like_modification(text))

    def test_non_human_species_are_recognised(self):
        self.assertTrue(GR.looks_non_human("mouse Snca"))
        self.assertTrue(GR.looks_non_human("Rat TRPA1"))
        self.assertFalse(GR.looks_non_human("SNCA"))


class CheckTests(TestCase):
    databases = {DB, "academy_db"}

    def setUp(self):
        cache.clear()

    def test_a_modification_is_refused_with_the_reason_and_writes_nothing(self):
        verdict = GR.check("phospho-tau S396")
        self.assertEqual(verdict["status"], GR.MODIFICATION)
        self.assertIn("knockout", verdict["message"])
        request, _ = GR.record(typed_text="phospho-tau S396",
                               email="a@b.com", applications=["WB"])
        self.assertIsNone(request)
        self.assertEqual(GeneRequest.objects.count(), 0)

    def test_a_non_human_request_is_refused(self):
        verdict = GR.check("mouse Snca")
        self.assertEqual(verdict["status"], GR.NOT_HUMAN)
        self.assertIn("human", verdict["message"])

    def test_a_gene_already_in_the_pipeline_is_recordable_and_linked(self):
        target = Target.objects.using(DB).create(
            gene_name="STMN2", protein_name="Stathmin-2")
        verdict = GR.check("stmn2")
        self.assertEqual(verdict["status"], GR.IN_PIPELINE)
        self.assertEqual(verdict["target_id"], target.pk)

        request, _ = GR.record(typed_text="stmn2", email="a@b.com",
                               applications=["WB"], verdict=verdict)
        self.assertEqual(request.target_id, target.pk)

    def test_an_unreachable_uniprot_records_rather_than_refusing(self):
        with mock.patch("pipeline.services.uniprot.requests.get",
                        side_effect=RequestException("proxy down")):
            verdict = GR.check("TRPA1")
        self.assertEqual(verdict["status"], GR.UNCHECKED)
        self.assertIn(verdict["status"], GR.RECORDABLE)

        request, _ = GR.record(typed_text="TRPA1", email="a@b.com",
                               applications=["WB"], verdict=verdict)
        self.assertIsNotNone(request)
        self.assertEqual(request.checked, GeneRequest.CHECKED_UNCHECKED)
        # The symbol is not claimed as confirmed — the screen reads this.
        self.assertEqual(request.gene_symbol, "")
        self.assertEqual(request.typed_text, "TRPA1")

    def test_a_confirmed_human_gene_carries_its_accession_through(self):
        with mock.patch("pipeline.services.uniprot.requests.get",
                        return_value=_Response(_uniprot_found())):
            verdict = GR.check("SNCA")
        self.assertEqual(verdict["status"], GR.OK)
        self.assertEqual(verdict["gene"], "SNCA")
        self.assertEqual(verdict["uniprot_id"], "P37840")


class RecordTests(TestCase):
    """A public request is its own row, and never the consortium's list."""

    databases = {DB, "academy_db"}

    def setUp(self):
        cache.clear()

    def test_a_request_never_becomes_a_target_nomination(self):
        Target.objects.using(DB).create(gene_name="STMN2")
        GR.record(typed_text="STMN2", email="a@b.com", applications=["WB", "FC"])
        self.assertEqual(GeneRequest.objects.count(), 1)
        # The whole reason the model is separate: a stranger's wish must not be
        # counted by the Portfolio as work a site has committed to.
        self.assertEqual(TargetNomination.objects.using(DB).count(), 0)

    def test_applications_are_stored_in_one_canonical_order(self):
        self.assertEqual(GR.parse_applications(["FC", "WB"]), "WB, FC")
        self.assertEqual(GR.parse_applications(["IHC", "WB"]), "WB, IHC")
        # A value the app cannot read is worse in a column than absent from it.
        self.assertEqual(GR.parse_applications(["WB", "NONSENSE"]), "WB")

    def test_other_is_labelled_with_what_was_typed_not_the_generic_word(self):
        """A tally reading "Something else × 7" counts seven requests and says
        nothing about any of them — the count-with-no-list failure, in the one
        column that exists to say what people need."""
        self.assertEqual(
            GR.application_labels("WB, OTHER", "ELISA"),
            ["Western blot", "ELISA"])
        # With nothing typed it falls back rather than printing an empty chip.
        self.assertEqual(GR.application_labels("OTHER", ""), ["Something else"])

    def test_by_gene_totals_agree_with_the_list_beneath_them(self):
        Target.objects.using(DB).create(gene_name="STMN2")
        for email in ("a@b.com", "c@d.com"):
            GR.record(typed_text="STMN2", email=email, applications=["WB"])
        rows = GR.by_gene()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], len(rows[0]["requests"]))
        self.assertEqual(rows[0]["count"], 2)


class PublicPageTests(TestCase):
    """The door, and the panel that points at it."""

    databases = {DB, "academy_db"}

    def setUp(self):
        cache.clear()

    def test_a_search_that_matches_nothing_offers_the_nomination_server_side(self):
        """``/?search=ZZZZZZ`` drew an empty grid and said nothing at all.

        The panel existed but was ``display:none`` and revealed only by the
        page's own JS, so the one route the header search box uses for
        everything it cannot resolve was the route with no message on it.
        """
        body = self.client.get("/?search=ZZZZZZ").content.decode()
        self.assertIn('class="search-answer"', body)
        self.assertIn(reverse("nominate_gene") + "?gene=ZZZZZZ", body)

    def test_the_card_grid_is_never_narrowed_by_a_search(self):
        """The grid is a crawler surface, not a result list.

        Every card links to a page that has its own sitemap entry, and the grid
        begins ~1,200px below the search box — so filtering it answered a search
        somewhere no reader could see, and served a crawler a thinner copy of
        the home page at a second URL. The answer goes under the box instead.

        Pinned because re-adding the filter is a one-line change that nothing
        else would contradict: the page would still look right to whoever made
        it, and the loss is invisible.
        """
        target = Target.objects.using(DB).create(gene_name="SNCA")
        other = Target.objects.using(DB).create(gene_name="ELP3")
        from pipeline.models import Antibody, Company, PublicationImage
        company = Company.objects.using(DB).create(name="Proteintech")
        for t, cat in ((target, "10842-1-AP"), (other, "24523-1-AP")):
            antibody = Antibody.objects.using(DB).create(
                target_id=t.pk, company_id=company.pk, catalogue_number=cat)
            PublicationImage.objects.using(DB).create(
                antibody_id=antibody.pk, application_type="WB")

        body = self.client.get("/?search=SNCA").content.decode()
        # Both genes still drawn, though only one matches.
        self.assertIn('data-name="ELP3"', body)
        self.assertIn('data-name="SNCA"', body)
        # ...and the answer under the box names the one that does. Collapsed
        # whitespace, because the count and its noun are on separate template
        # lines and this test is about the sentence, not the indentation.
        squashed = " ".join(body.split())
        self.assertIn('class="search-answer"', body)
        self.assertIn("1 gene matches <strong>SNCA</strong>", squashed)

    def test_the_contact_form_no_longer_offers_a_way_to_nominate(self):
        """Two doors to one thing means half the requests keep arriving in the
        form that cannot be counted, and nothing on either page would say so."""
        body = self.client.get(reverse("contact")).content.decode()
        self.assertNotIn("suggest-target", body)
        self.assertNotIn("Suggest a Target", body)
        # ...and it points at the one that is left.
        self.assertIn(reverse("nominate_gene"), body)

    def test_the_form_refuses_a_modification_and_writes_nothing(self):
        response = self.client.post(reverse("nominate_gene"), {
            "gene": "phospho-tau", "email": "a@b.com", "applications": ["WB"]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "out of scope")
        self.assertEqual(GeneRequest.objects.count(), 0)

    def test_a_missing_application_is_refused_beside_its_own_box(self):
        response = self.client.post(reverse("nominate_gene"), {
            "gene": "SNCA", "email": "a@b.com"})
        self.assertContains(response, "Tick at least one application")
        self.assertEqual(GeneRequest.objects.count(), 0)

    def test_something_else_with_no_text_is_refused_by_name(self):
        """It records that they need *something* and not what, which is the one
        answer this question cannot use."""
        response = self.client.post(reverse("nominate_gene"), {
            "gene": "SNCA", "email": "a@b.com", "applications": ["OTHER"]})
        self.assertContains(response, "say which")
        self.assertEqual(GeneRequest.objects.count(), 0)

    def test_typing_in_the_other_box_selects_the_option(self):
        """Refusing somebody who said what they need because they did not also
        tick the box beside it is the form arguing with an answer it has."""
        with mock.patch("pipeline.services.uniprot.requests.get",
                        return_value=_Response(_uniprot_found())):
            self.client.post(reverse("nominate_gene"), {
                "gene": "SNCA", "email": "a@b.com",
                "applications_other": "ELISA"})
        request = GeneRequest.objects.get()
        self.assertEqual(request.applications, "OTHER")
        self.assertEqual(request.applications_other, "ELISA")

    def test_a_good_nomination_is_written_and_the_receipt_names_it(self):
        with mock.patch("pipeline.services.uniprot.requests.get",
                        return_value=_Response(_uniprot_found())):
            response = self.client.post(reverse("nominate_gene"), {
                "gene": "SNCA", "email": "a@b.com",
                "applications": ["WB", "ICC-IF"],
                "has_funding": "on", "funding_note": "Wellcome, 2027"})
        self.assertEqual(response.status_code, 200)
        request = GeneRequest.objects.get()
        self.assertEqual(request.gene_symbol, "SNCA")
        self.assertEqual(request.applications, "WB, ICC-IF")
        self.assertTrue(request.has_funding)
        # The receipt repeats what was recorded, or a wrong tick is invisible.
        self.assertContains(response, "Western blot")

    def test_a_published_gene_sends_the_reader_to_it_rather_than_to_a_form(self):
        from pipeline.models import Antibody, Company, PublicationImage
        target = Target.objects.using(DB).create(gene_name="SNCA")
        company = Company.objects.using(DB).create(name="Proteintech")
        antibody = Antibody.objects.using(DB).create(
            target_id=target.pk, company_id=company.pk, catalogue_number="10842-1-AP")
        PublicationImage.objects.using(DB).create(
            antibody_id=antibody.pk, application_type="WB")

        response = self.client.get(reverse("nominate_gene") + "?gene=SNCA")
        self.assertEqual(response.status_code, 302)
        self.assertIn("SNCA", response["Location"])
