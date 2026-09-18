"""Every public surface that shows a recommendation says what one covers.

Owner's instruction, 12 Aug 2026: wherever a recommendation is shown, a caveat
has to explain what that result does and does not cover — **once per page**, not
more. The words are `recommendations.SCOPE_NOTE`'s and have been reworded three
times since; asserting the constant rather than a copy of it is what lets them
be.

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

    def test_the_gene_page_says_what_immunofluorescence_depends_on(self):
        """The four-column table is where a reader meets an ICC-IF verdict.

        A different claim from `SCOPE_NOTE` and drawn from a different constant
        (`APPLICATION_FACT`), so the once-per-page rule above does not cover it
        — but it fails the same silent way: the loop renders nothing at all if
        `application_facts` never reaches the context, and a page with a caveat
        missing looks exactly like a page that was never owed one.

        The **fact**, not the whole of `APPLICATION_SCOPE`: its other half ends
        "the gene page says which", which on the gene page points at itself.
        Asserted as absent too, or dropping it would be undone by the next
        person who tidied the include back in.
        """
        page = self.client.get(
            reverse("antibody_table", args=["SNCA"])).content.decode()
        self.assertIn(R.APPLICATION_FACT["ICC-IF"], page)
        self.assertNotIn("the gene page says which", page)

    def test_the_caveat_states_both_facts(self):
        """The constant is the one writer, so this is the only place they live.

        Asserted as claims rather than as a sentence — the owner may reword it,
        and a test that pins the prose would fail on an improvement. What must
        survive a rewording is that both facts are still in there, and that the
        second is stated in **both** directions: a pass here is not a licence
        for another assay system, and a fail here is not a mark against
        somebody's working IHC.

        The owner did reword it, on 11 Sep 2026, and this is what moved: the
        dependence names the **assay** as well as the protocol and the sample,
        and "do not validate or invalidate experiments in other assay systems
        or sample types" became "may differ and needs its own controls". Both
        directions survive in *may differ*, so what is pinned now is that the
        sentence still says performance may be different in the reader's own
        context and still tells them what to do about it — the half the old
        wording never had.
        """
        note = R.SCOPE_NOTE.lower()
        self.assertIn("assay, protocol and sample dependent", note)
        self.assertIn("your own experimental context", note)
        self.assertIn("may differ", note)
        self.assertIn("its own controls", note)

    def test_the_clause_about_the_readers_own_experiment_is_emphasised(self):
        """Owner, 11 Sep 2026, highlighting exactly those words on the page.

        Two halves, and the first is the one that needs a test. `SCOPE_EMPHASIS`
        is a *fragment* of `SCOPE_NOTE`, so a rewording of the note that no
        longer contains it makes `_emphasised` fall through and print the
        sentence whole — correct, complete, and silently no longer emphasised.
        Nothing on the page could tell you that had happened.

        The second half is that the bold actually reaches a rendered page, which
        a constant on its own does not prove: the partial has to read
        `scope_note_html` and not `scope_note`.
        """
        self.assertIn(R.SCOPE_EMPHASIS, R.SCOPE_NOTE)
        emphasised = f"<strong>{R.SCOPE_EMPHASIS}</strong>"
        for name, page in self.pages():
            if name == "api reference":
                continue
            with self.subTest(page=name):
                self.assertIn(emphasised, page)

    def test_every_page_says_where_to_plan_those_controls(self):
        """"Needs its own controls" with nowhere to click is half a message.

        The Framework is OGA's own answer to the sentence, and the link lives
        in `_recommendation_caveat.html` beside it so every surface drawing the
        caveat draws the way there. A page whose link went missing would read
        exactly like one that never offered it.

        `/data-access/api/` is the exception, for the reason it is an exception
        to everything here: it is rendered from `API.md`, which is hand-written
        prose read on GitHub as often as on the site, so its link is absolute
        and this assertion would be about the hostname rather than the page.
        It carries one, and the sentence itself is asked of it above.
        """
        framework = reverse("validation_framework")
        for name, page in self.pages():
            if name == "api reference":
                continue
            with self.subTest(page=name):
                self.assertIn(f'href="{framework}"', page)
