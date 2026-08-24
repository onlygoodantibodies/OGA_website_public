"""Every public surface that shows a recommendation says what one covers.

Owner's instruction, 12 Aug 2026: wherever a recommendation is shown, a caveat
has to explain that antibody performance is protocol and sample dependent, and
that OGA's results neither validate nor invalidate experiments in other assay
systems or sample types — **once per page**, not more.

Both halves are asserted, and the second is the one that needs a test. A missing
caveat is silent: the page renders, the verdicts are drawn, and nothing on
screen says the reader was owed a sentence they did not get. So is a duplicated
one — it reads as clutter rather than as a defect, and a reader who meets the
same caveat three times on one screen reads none of them.

The wording itself is asserted against `core/recommendations.py::SCOPE_NOTE`
rather than spelled here, because the constant is the one writer and a test that
spells its own copy is a fifth copy. Four of these pages had hand-typed the
sentence and one of them (the gene page) had drifted into saying something else;
they all include `core/templates/core/_recommendation_caveat.html` now.

`/data-access/api/` renders `API.md`, which is prose written by hand and cannot
include a template — so it is asked the same question here, which is the only
thing standing between it and the drift its neighbour just came out of.
"""
import re

from django.test import TestCase, override_settings
from django.urls import reverse

from core import recommendations as R
from pipeline.models import Antibody, Company, PublicationImage, Target


def visible_text(html):
    """What a reader reads: tags stripped, whitespace flattened, lower-cased.

    The caveat carries a link, so the sentence is not contiguous in the source
    even though it reads as one line — and on `/data-access/api/` the link is in
    the *middle* of it, because that page is rendered from `API.md` and markdown
    has nowhere else to put one. So a tag becomes **nothing**, not a space: the
    space version reads `consensus protocols .` there and matches no page,
    which is the same convention `tests_data_access.py` already uses.
    """
    without_code = re.sub(r"<(style|script)\b.*?</\1>", " ", html,
                          flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", without_code)).lower()


#: The sentence as it reads on a page, for `assertIn` against `visible_text`.
CAVEAT = re.sub(r"\s+", " ", R.SCOPE_NOTE).lower()


class EverySurfaceThatShowsARecommendationCarriesTheCaveatTests(TestCase):
    """One fixture, every public surface, asked the same two questions."""

    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="Abcam", display_name="Abcam")
        cls.target = Target.objects.create(gene_name="SNCA", protein_name="Syn")
        cls.antibody = Antibody.objects.create(
            target=cls.target, company=company, catalogue_number="ab138501",
            rrid="AB_2687467", wb_recommended=True)
        # A published figure is what makes an antibody public — the gene pages,
        # the embed card and the extension index all require one.
        PublicationImage.objects.create(
            antibody=cls.antibody, application_type="WB", image="pubs/x.png")

    def pages(self):
        """Every public surface that draws an OGA recommendation.

        The extension page is behind the pipeline login until
        `EXTENSION_PAGE_PUBLIC` is set, so the public version is asked for.
        """
        yield "gene page", self.client.get(
            reverse("antibody_table", args=["SNCA"])).content.decode()
        yield "portal", self.client.get(reverse("portal")).content.decode()
        yield "data access", self.client.get(
            reverse("data_access")).content.decode()
        yield "api reference", self.client.get(
            reverse("api_reference")).content.decode()
        yield "embed card", self.client.get(
            reverse("embed_antibody_card"),
            {"catalogue": "ab138501"}).content.decode()
        yield "connect your AI", self.client.get(reverse("connect_your_ai")).content.decode()
        yield "selection tool", self.client.get(
            reverse("selector:tool")).content.decode()
        with override_settings(EXTENSION_PAGE_PUBLIC=True):
            yield "extension", self.client.get(
                reverse("extension")).content.decode()

    def test_each_one_says_what_a_recommendation_covers(self):
        for name, page in self.pages():
            with self.subTest(page=name):
                self.assertIn(CAVEAT, visible_text(page))

    def test_no_page_says_it_twice(self):
        """Owner's instruction: not more than once per page.

        Counted on the visible text rather than on the includes, because two
        includes and one include drawn inside a loop fail the same way and only
        the rendered page can tell.
        """
        for name, page in self.pages():
            with self.subTest(page=name):
                self.assertEqual(1, visible_text(page).count(CAVEAT))

    def test_the_caveat_states_both_facts(self):
        """The constant is the one writer, so this is the only place they live.

        Asserted as claims rather than as a sentence — the owner may reword it,
        and a test that pins the prose would fail on an improvement. What must
        survive a rewording is that both facts are still in there, and that the
        second is stated in **both** directions: a pass here is not a licence
        for another assay system, and a fail here is not a mark against
        somebody's working IHC.
        """
        note = R.SCOPE_NOTE.lower()
        self.assertIn("protocol and sample dependent", note)
        self.assertIn("validate or invalidate", note)
        self.assertIn("assay systems or sample types", note)
