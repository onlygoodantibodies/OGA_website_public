"""The portal and the API had no way in from the public site.

Both existed and worked, and nothing anywhere on onlygoodantibodies.co.uk said
so — a manufacturer or a registry had to already know to ask. That is the same
shape as this repo's rule about an export with no importer, or a routed endpoint
nothing links to: a capability nobody can find is one people conclude does not
exist.

So these pin the *reachability*, not the prose. The wording of the page will
change and should; what must not silently break is that the homepage points at
it, that it points at the things it describes, and that the one sentence
protecting a named commercial product is on it.
"""
from django.test import TestCase
from django.urls import reverse


class TheHomepageOffersTheWayInTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def test_the_homepage_links_to_it(self):
        """A page reachable only by typing its address is not reachable."""
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("data_access"))

    def test_it_renders(self):
        response = self.client.get(reverse("data_access"))
        self.assertEqual(response.status_code, 200)

    def test_the_tools_hub_offers_it_too(self):
        """The homepage link is one sentence in a paragraph; the hub is where a
        reader goes when they are looking for a tool rather than reading."""
        response = self.client.get(reverse("tools_hub"))
        self.assertContains(response, reverse("data_access"))


class ItSaysEnoughToActOnTests(TestCase):
    """"Sufficient to enable them to use the portal and API, and contact us for
    a key" — so each of those three has to actually be on the page."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        import re

        raw = self.client.get(reverse("data_access")).content.decode()
        self.page = raw
        # Whitespace-collapsed, for assertions about a *sentence*: the template
        # wraps its prose, so a phrase that reads as one line on screen is split
        # by a newline and eight spaces in the source.
        self.prose = re.sub(r"\s+", " ", raw).lower()

    def test_it_says_how_to_ask_for_a_key(self):
        """Nothing else on the page works without one, so this is the first
        thing that must not go missing."""
        self.assertIn("mailto:onlygoodantibodies@gmail.com", self.page)
        self.assertIn(reverse("contact"), self.page)

    def test_it_points_at_the_portal(self):
        self.assertIn(reverse("portal"), self.page)

    def test_it_gives_a_working_api_call(self):
        """A reader must be able to copy something and get an answer."""
        self.assertIn("X-API-Key", self.page)
        self.assertIn("/api/v1/status/", self.page)

    def test_it_points_at_the_machine_readable_spec(self):
        """The contract, for anyone generating a client rather than reading."""
        self.assertIn("/api/v1/openapi.json", self.page)

    def test_it_explains_bulk_download(self):
        self.assertIn("manifest", self.page.lower())

    def test_it_carries_the_scientific_caveat_and_nothing_more(self):
        """Two facts, and the owner's own words for them (7 Aug 2026).

        This used to pin four claims, two of which the owner has since cut: that
        a recommendation is "not a verdict", and the paragraph explaining that
        `not_tested` "is not a negative result — map it to no data, never to a
        failure". Untested is untested. What survives is what a reader can act
        on — which protocols the results come from, that performance is protocol
        and sample dependent, and that a result here neither validates nor
        invalidates another assay system or sample type — and it is the same
        sentence everywhere, because `core/recommendations.py::SCOPE_NOTE` is
        the only writer.

        Asserted as claims rather than as sentences, except the caveat itself,
        which is asserted against the constant so the page and the API cannot
        drift apart. Tags are stripped before comparing: the caveat carries a
        link, so the sentence is not contiguous in the source even though it
        reads as one line.

        `core/tests_recommendation_caveat.py` asks this of every surface that
        draws a recommendation, and asks it a second way — that no page says it
        twice.
        """
        import re

        from core import recommendations as R

        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", self.page)).lower()
        self.assertIn("not_tested", self.prose)
        self.assertIn(re.sub(r"\s+", " ", R.SCOPE_NOTE).lower(), text)
        self.assertIn(R.CONSENSUS_PROTOCOL_URL, self.page)

    def test_the_word_verdict_is_nowhere_a_reader_can_see_it(self):
        """Owner's instruction, 7 Aug 2026: not on the public pages.

        Stripped of `<style>` and `<script>` first — a page-scoped class name or
        a note to whoever edits the file next is not something a reader reads,
        and this repo already draws that line for the board guides.
        """
        import re

        visible = re.sub(r"<(style|script)\b.*?</\1>", " ", self.page,
                         flags=re.S | re.I)
        self.assertNotIn("verdict", visible.lower())


class TheProtocolsAreNotTheDelphiStudyTests(TestCase):
    """Two separate pieces of work, and the homepage merged them.

    The consensus **protocols** are Ayoubi et al., 2024, *Nature Protocols*,
    written with YCharOS, the industry–academic consortium. The **Delphi study**
    is 32 experts rating interventions for funders, publishers and institutions,
    and it is what the roadmap pages are built on. It did not produce the
    protocols — the homepage card said "developed through Delphi consensus"
    under a button linking the Nature Protocols paper (owner, 7 Aug 2026).

    Every surface citing the protocols reads one constant, so this asks the
    pages rather than the templates.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_the_extension_page_carries_the_same_caveat(self):
        """It shows OGA results too, so it owes the reader the same sentence.

        `EXTENSION_PAGE_PUBLIC` is off by default and the page then sits behind
        the pipeline login, so the public version has to be asked for.
        """
        import re

        from django.test import override_settings

        from core import recommendations as R

        with override_settings(EXTENSION_PAGE_PUBLIC=True):
            page = self.client.get(reverse("extension")).content.decode()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", page)).lower()
        self.assertIn(re.sub(r"\s+", " ", R.SCOPE_NOTE).lower(), text)

    def test_no_page_attributes_the_protocols_to_delphi(self):
        import re

        from core import recommendations as R

        for name in ("home", "data_access", "validation_framework"):
            with self.subTest(page=name):
                page = self.client.get(reverse(name)).content.decode()
                self.assertIn(R.CONSENSUS_PROTOCOL_URL, page)
                # Delphi may be named on a page — the framework page cites the
                # study properly. What must not happen is the two being joined
                # in one sentence about protocols.
                joined = re.findall(
                    r"[^.]*protocols?[^.]*delphi[^.]*\.|[^.]*delphi[^.]*protocols?[^.]*\.",
                    re.sub(r"<[^>]+>", "", page), re.I)
                self.assertEqual(
                    [], joined,
                    f"{name} attributes the protocols to the Delphi study: {joined}")


class ItsOwnLinksResolveTests(TestCase):
    """A page whose links 404 is worse than no page: it was written to be
    followed, and this repo has been bitten by pointing readers at dead pages
    before."""

    databases = {"pipeline_db", "academy_db"}

    def test_every_internal_link_answers(self):
        """Anchors only.

        Not `<link>` or `<img>`: a stylesheet is not a destination, and
        `staticfiles/` is a build artefact that only exists after
        `collectstatic`, so including assets here would fail on a fresh
        checkout for a reason that has nothing to do with this page.

        A redirect counts as answering. `/news/` and `/publications/` are
        deliberate permanent redirects to the combined page, and a test that
        called those broken would be wrong about the site rather than about the
        link.
        """
        import re

        page = self.client.get(reverse("data_access")).content.decode()
        hrefs = {h for h in re.findall(r'<a\s[^>]*href="(/[^"#]*)"', page)}
        self.assertTrue(hrefs, "no internal links found — has the page changed?")
        for href in sorted(hrefs):
            with self.subTest(href=href):
                status = self.client.get(href).status_code
                self.assertIn(
                    status, (200, 301, 302),
                    f"{href} is linked from the partner page and answers {status}.")


class NoPageStyleBlockOverridesTheChromeTests(TestCase):
    """A page stylesheet must not redefine a class the site chrome uses.

    `.hidden { display: none !important }`, declared on one pipeline page for an
    image overlay, beat the top bar's own `hidden md:flex` and emptied the whole
    navigation — every link present in the HTML and none of them drawn. Scope a
    page's rules to its own elements.
    """
    databases = {"academy_db", "pipeline_db"}

    def test_its_rules_are_prefixed_and_carry_no_important(self):
        import re
        from pathlib import Path

        from django.conf import settings

        source = Path(settings.BASE_DIR,
                      "core/templates/core/data_access.html").read_text()
        block = re.search(r"<style>(.*?)</style>", source, re.S)
        self.assertIsNotNone(block, "the page's <style> block has moved")
        css = block.group(1)

        self.assertNotIn("!important", css,
                         "!important in a page stylesheet is what turns a local "
                         "rule global.")
        selectors = re.findall(r"^\s*(\.[A-Za-z][\w-]*)", css, re.M)
        for selector in selectors:
            with self.subTest(selector=selector):
                self.assertTrue(
                    selector.startswith(".da-"),
                    f"{selector} is not scoped to this page; prefix it `da-`.")


class TheApiReferenceIsOnTheWebsiteTests(TestCase):
    """`API.md` existed only in the git repository.

    `/data-access/` introduced the API and `openapi.json` described it to a
    machine, but the document written for a *person* — every endpoint, every
    parameter, a working sync client — was reachable by nobody. That is the
    export-with-no-importer failure this repo keeps meeting, applied to
    documentation: a partner asking "how do I use this" got an overview page and
    a JSON schema.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_it_renders_the_markdown(self):
        response = self.client.get(reverse("api_reference"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        # Rendered, not printed: a `<h2>` means the Markdown was converted
        # rather than dumped between <pre> tags.
        self.assertIn("<h2", page)
        self.assertIn("<code>", page)

    def test_the_partner_page_links_to_it(self):
        """A routed page nothing points at is one nobody finds."""
        page = self.client.get(reverse("data_access")).content.decode()
        self.assertIn(reverse("api_reference"), page)

    def test_it_carries_the_sync_client_a_reader_came_for(self):
        page = self.client.get(reverse("api_reference")).content.decode()
        self.assertIn("sync", page.lower())
        self.assertIn("If-None-Match", page)
        # The rule that makes "only fetch what I do not have" actually work.
        self.assertIn("url", page)

    def test_the_csv_header_it_documents_is_the_one_the_api_writes(self):
        """A documented column that never arrives is a parser somebody wrote
        for nothing.

        `API.md` listed `verdict` between `oga_recommendation` and
        `product_link`, and showed it in the JSON example too. The manifest has
        not emitted it for as long as `CSV_COLUMNS` has existed — the document
        was describing an older shape, and nothing compared the two (7 Aug
        2026). The OpenAPI description is generated from `CSV_COLUMNS` and was
        right the whole time, so the two public descriptions of one file
        disagreed.
        """
        import re

        from pathlib import Path

        from django.conf import settings

        from core.api_manifest import CSV_COLUMNS

        source = Path(settings.BASE_DIR, "API.md").read_text(encoding="utf-8")
        headers = [line for line in source.splitlines()
                   if line.startswith("url,filename,")]
        self.assertTrue(headers, "the CSV example has moved or lost its header")
        for header in headers:
            self.assertEqual(header.split(","), CSV_COLUMNS)

        # The JSON example is the same claim in the other format.
        self.assertNotIn('"verdict"', source)

    def test_it_names_no_placeholder_key_as_a_real_one(self):
        import re

        page = self.client.get(reverse("api_reference")).content.decode()
        self.assertIn("YOUR_API_KEY_HERE", page)
        leaked = re.findall(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            page)
        self.assertEqual(leaked, [], f"A real-looking key is on the page: {leaked}")

    def test_a_missing_file_says_so_rather_than_500ing(self):
        """The document is read from disk, so it can be absent."""
        from unittest import mock

        with mock.patch("pathlib.Path.read_text", side_effect=OSError("gone")):
            response = self.client.get(reverse("api_reference"))
        self.assertEqual(response.status_code, 503)
        self.assertIn("openapi.json", response.content.decode())

    def test_it_carries_the_site_chrome(self):
        """It is a standalone document like its siblings, not a bare page."""
        page = self.client.get(reverse("api_reference")).content.decode()
        self.assertIn("<title>", page)
        self.assertIn("footer", page)

    def test_its_style_block_is_scoped_and_carries_no_important(self):
        import re
        from pathlib import Path

        from django.conf import settings

        source = Path(settings.BASE_DIR,
                      "core/templates/core/api_reference.html").read_text()
        css = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
        self.assertNotIn("!important", css)
        selectors = re.findall(r"^\s*([^@{\n][^{\n]*)\{", css, re.M)
        for selector in selectors:
            with self.subTest(selector=selector.strip()):
                self.assertIn("#api-doc", selector,
                              "A rule here is not scoped to this page and can "
                              "reach the site chrome.")
