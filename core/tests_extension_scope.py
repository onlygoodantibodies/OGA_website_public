"""The extension says what its verdicts cover, in the site's own words.

Two things here are silent when they go wrong, which is the whole reason either
is pinned.

**The caveat reaching the extension at all.** `card.js` draws it from
`index.json`, so if the builder stops sending it every card falls back to a
bundled constant and goes on rendering perfectly — with a sentence that is
frozen at whatever it said the day the fallback was written. Nothing on any
screen distinguishes that from working. The fallback exists for old installs and
must never become the live path.

**The words on the five listing screenshots.** One tuple feeds three places:
the caption the page prints, the `alt` a screen reader gets, and the banner
drawn into the store's copy of the same five. A sighted reader sees the picture;
a screen-reader user gets only the `alt`; and nothing compares them. So the
page is asserted to carry the caption AND the `alt`, and the two are asserted
to differ — an `alt` that repeats the caption reads it twice.

Deliberately not pinned: anything about the pixels, including the banner. Those
images are rendered outside this repo now, so asserting their contents here
would be asserting something this codebase does not produce.
"""
from __future__ import annotations

import re
import sys
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.utils.html import escape
from django.test import TestCase, override_settings
from django.urls import reverse

from django.contrib.auth.models import User

from core import recommendations as R
from core.extension_index import (EXTENSION_INDEX_CACHE_KEY, build_index,
                                  invalidate_snapshot)
from core.views import (HERO_SCREENSHOT, HERO_SCREENSHOT_CROP, LISTING_FRAME,
                        LISTING_SCREENSHOTS, _extension_hero,
                        _extension_identity, _extension_screenshots)
from pipeline.models import (Antibody, Company, Member, PublicationImage,
                             Site, Target)


class TheIndexCarriesTheCaveatTests(TestCase):
    """The extension reads the caveat from the data, not from its own build."""

    databases = {"pipeline_db", "academy_db"}

    def test_the_index_ships_the_site_wording(self):
        index = build_index()
        self.assertEqual(index["scope"], R.SCOPE_NOTE)
        self.assertEqual(index["scope_short"], R.SCOPE_SHORT)
        self.assertEqual(index["protocols_url"], R.CONSENSUS_PROTOCOL_URL)

    def test_the_short_form_still_states_both_facts(self):
        """A compression, not a second opinion.

        `SCOPE_SHORT` exists because the full note is four lines on a 330px
        card. What it must not do is drop half of what it shortens — and the
        two facts are the whole of the caveat: these come from the consensus
        protocols, and performance depends on protocol and sample.

        **Both facts, across the PAIR of strings** since 3 Sep 2026. The
        protocols half moved into `CONDITIONS_QUALIFIER`, which the card draws
        inside every verdict — a stronger position than a sentence underneath,
        since a reader who takes only the headline takes it too. Asserting the
        pair is what keeps that a move rather than a loss: drop the naming from
        both and this fails, which is the whole reason the assertion exists.
        """
        short = (R.CONDITIONS_QUALIFIER + " " + R.SCOPE_SHORT).lower()
        self.assertIn("consensus protocols", short)
        self.assertIn("protocol and sample dependent", short)
        # And not in both, which is the same caveat twice on a 330px card.
        self.assertNotIn("consensus protocols", R.SCOPE_SHORT.lower())

    def test_the_index_ships_the_per_application_caveat(self):
        """Keyed the way the card keys its tabs, not the way the database does.

        `APPLICATION_SCOPE` is keyed 'ICC-IF' and the extension calls it 'IF'.
        A caveat under a key no tab uses is one that never draws, and nothing on
        the card would say so.
        """
        scope = build_index()["application_scope"]
        self.assertEqual(scope["IF"], R.APPLICATION_SCOPE["ICC-IF"])
        self.assertNotIn("ICC-IF", scope)

    def test_every_application_with_a_capability_ships_its_interim_warning(self):
        """The one thing that reaches an installed build before a store
        release. `q` is data the shipped code does not read and the colours are
        in its JavaScript, so until a release lands this sentence is all that
        stops 491 qualified negatives being read as outright failures.

        Keyed the way the card keys its tabs — a warning under a key no tab uses
        never draws, and nothing on the card would say so.
        """
        scope = build_index()["application_scope"]
        for key in ("WB", "IP", "IF"):
            self.assertIn(key, scope, key)
            self.assertIn("not supportive", scope[key], key)
            self.assertIn("may still mean", scope[key], key)
        # Nothing records a flow cytometry outcome, so a line there would
        # promise a nuance the data cannot deliver.
        self.assertNotIn("FC", scope)

    def test_the_client_can_read_every_code_the_index_ships(self):
        """The two halves of one table, in two languages.

        The index ships a two-letter code per qualified application and the
        client holds the wording — the file is a megabyte before gzip and is
        fetched twice per install per day, so the sentences cannot travel in it.
        That split is only safe while both sides know the same codes: a code the
        client cannot read draws **nothing**, silently, on a mark whose whole
        meaning is the clause.

        Same rule as the catalogue-number normalisation this extension and the
        MCP server share, and the same failure if it drifts — one side widens,
        the other goes quiet, and no screen says so.
        """
        for name in ("card.js", "content.js"):
            src = (Path(__file__).resolve().parent.parent / "browser-extension"
                   / "src" / name).read_text()
            table = re.search(
                r"const QUALIFIER(?:S|_WORDS) = \{(.*?)\};", src, re.S)
            self.assertIsNotNone(table, f"{name} has no qualifier table")
            codes = set(re.findall(r"^\s*(\w+):", table.group(1), re.M))
            self.assertEqual(
                codes, set(R.QUALIFIER_CODES),
                f"{name} and core/recommendations.py disagree about the codes")

    def test_the_client_prints_the_clause_the_server_would(self):
        """Not just the same keys — the same sentences. Two wordings for one
        finding is the drift `core/recommendations.py` exists to stop, and here
        it would show up as the gene page and the extension describing the same
        antibody differently."""
        src = (Path(__file__).resolve().parent.parent / "browser-extension"
               / "src" / "card.js").read_text()
        body = re.search(r"const QUALIFIERS = \{(.*?)\};", src, re.S).group(1)
        drawn = dict(re.findall(r'^\s*(\w+): "([^"]+)"', body, re.M))
        self.assertEqual(
            drawn, {code: clause
                    for code, (clause, _) in R.QUALIFIER_CODES.items()})

    def test_the_index_ships_the_wording_behind_the_codes(self):
        """Served, not baked, so the next rewrite is a deploy.

        This is the text that has moved most — three passes to land on
        `Limited support` — so it goes the way the scope note goes: through the
        index, reaching every install within a day, instead of an AMO
        submission and a review wait. The client keeps the same table as a
        fallback for a cached index that predates the key.
        """
        index = build_index()
        self.assertEqual(
            index["qualifier_words"],
            {code: clause for code, (clause, _) in R.QUALIFIER_CODES.items()})
        self.assertEqual(index["limited_support_note"],
                         R.LIMITED_SUPPORT_NOTE)

    def test_the_client_prefers_the_served_wording(self):
        """A served table nothing reads is the shape this repo keeps finding —
        the string is present, the wiring is not. So this asserts the resolver
        is what the drawing code calls, not merely that the key is parsed."""
        src = (Path(__file__).resolve().parent.parent / "browser-extension"
               / "src" / "card.js").read_text()
        self.assertIn("index.qualifierWords", src)
        # Exactly one place still reads the baked table directly: the fallback
        # inside the resolver.
        self.assertEqual(src.count("QUALIFIERS[code]"), 1)
        self.assertNotIn("QUALIFIERS[(status", src)

    def test_the_index_ships_the_conditions_qualifier(self):
        """The words that go INSIDE a verdict, not the sentence under it.

        Shipped for the same reason as the scope note: the owner can reword it
        without a store submission. If the builder stops sending it the card
        falls back to the same words and nothing looks wrong — which is why it
        is asserted rather than assumed.
        """
        self.assertEqual(build_index()["conditions_qualifier"],
                         R.CONDITIONS_QUALIFIER)

    def test_it_says_what_it_depends_on(self):
        note = R.APPLICATION_SCOPE["ICC-IF"].lower()
        self.assertIn("fixation", note)
        self.assertIn("permeabilisation", note)

    def test_adding_it_did_not_move_the_schema(self):
        """An older signed build in the field must go on reading this file.

        Both gates in `background.js` check `schema === 1`; bumping it here
        would strand every install on the 18-antibody dev fixture, with no
        symptom beyond the marks quietly going stale.
        """
        self.assertEqual(build_index()["schema"], 1)


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheScreenshotWordsAreOneCopyTests(TestCase):
    """What is drawn into the banner is what `alt` says."""

    databases = {"pipeline_db", "academy_db"}

    def test_every_figure_is_captioned_and_described(self):
        """The words are in the page now, not in the pixels.

        Which means two separate things have to be there: a caption saying what
        the figure demonstrates, and an `alt` describing the picture. Drop the
        caption and the page is seven screenshots with no explanation; drop the
        `alt` and a screen reader gets nothing at all. Neither failure is
        visible to somebody looking at the page.
        """
        page = self.client.get(reverse("extension")).content.decode()
        shots = _extension_screenshots()
        self.assertTrue(shots, "no listing figures deployed to check")
        for shot in shots:
            with self.subTest(shot["src"]):
                self.assertIn(shot["title"], page)
                self.assertIn(shot["note"], page)
                # Escaped, because an alt is an attribute and prose contains
                # quotes and ampersands. Comparing the raw string passes until
                # somebody writes one, then fails for a reason that looks like
                # a missing image.
                self.assertIn(f'alt="{escape(shot["alt"])}"', page)

    def test_the_alt_is_not_just_the_caption_again(self):
        """Two jobs, two strings — or a screen reader hears it twice."""
        for _name, title, note, alt in LISTING_SCREENSHOTS:
            with self.subTest(title=title):
                self.assertNotEqual(alt, note)
                self.assertNotIn(note, alt)

    def test_the_store_gets_the_same_five_with_the_words_burned_in(self):
        """One copy, two renderings.

        A listing has nowhere to put a caption, so the store's copy carries the
        headline, the sentence and the frame in a banner; the page serves the
        plain capture and prints the same words as HTML. Two directories of one
        set is exactly the shape that drifts, so the filenames are asserted to
        line up — a store image nobody replaced is one showing last month's
        vocabulary to every new installer.
        """
        store = (Path(__file__).resolve().parent.parent / "browser-extension"
                 / "store" / "listing")
        for name, _title, _note, _alt in LISTING_SCREENSHOTS:
            with self.subTest(name):
                self.assertTrue((store / name.replace(".webp", ".png")).exists())

    def test_the_banners_are_built_from_these_words_and_not_by_hand(self):
        """The test above found the gap and could not close it.

        It knew a banner was missing and had nothing to say about one that was
        *there* and out of date — which is the state the set sat in from the day
        the verdict wording changed: five banners reading "Data not supportive
        in the conditions tested" over captures whose own pixels said "a 'not
        recommended' result may still mean…". Both phrases retired, on the
        images every new installer sees first, with the website correct all
        along.

        Nothing built them, so nothing could be re-run.
        `bin/build_listing_banners.py` composes each one from the capture, this
        tuple's headline and sentence, and `LISTING_FRAME`, and records what it
        used in `store/listing/banners.json`; `--check` compares that manifest
        and is what this asserts. The words are derived now, so the two
        renderings cannot say different things.

        **Not mtimes, and not pixels.** Git preserves neither modification
        times nor an ordering of them, so an mtime comparison would pass or
        fail by accident in a fresh clone — a flaky guard is worse than none.
        Re-rendering and comparing bytes fails the other way: freetype draws a
        glyph differently across versions, so CI would disagree with a laptop
        about a file neither had touched. The manifest is the only one of the
        three that is a fact about the words.
        """
        import subprocess

        root = Path(__file__).resolve().parent.parent
        script = root / "bin" / "build_listing_banners.py"
        self.assertTrue(script.exists(), "the banner builder is gone")
        done = subprocess.run([sys.executable, str(script), "--check"],
                              capture_output=True, text=True, cwd=root)
        self.assertEqual(done.returncode, 0,
                         "a store banner is older than its capture — re-run "
                         f"bin/build_listing_banners.py\n{done.stderr}")

    def test_the_frame_is_said_on_the_page_as_well(self):
        """It is drawn into every store image; the page has to say it too, or
        the two surfaces frame the same data differently."""
        page = self.client.get(reverse("extension")).content.decode()
        for phrase in ("assay- and sample-dependent",
                       "context-specific validation"):
            self.assertIn(phrase, page)
        self.assertIn("consensus protocols", LISTING_FRAME)

    def test_the_page_carries_the_immunofluorescence_caveat(self):
        page = self.client.get(reverse("extension")).content.decode()
        self.assertIn(R.APPLICATION_SCOPE["ICC-IF"], page)

    def test_the_listing_set_still_has_its_files(self):
        """A missing file falls back to its unhashed name rather than raising,
        so it reaches the page a stranger lands on as a broken image."""
        self.assertEqual(len(_extension_screenshots()), len(LISTING_SCREENSHOTS))


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheHeroFigureIsAtTheTopTests(TestCase):
    """One of the five is the picture that sells the extension, so it is drawn
    at the top and cropped to what it is showing (owner, 31 Aug 2026).

    Both halves are silent when they break. Move it back below the fold and the
    page still renders perfectly; leave it in the grid as well and the same
    picture appears twice, which looks like a duplicate rather than a mistake.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_it_is_drawn_before_the_grid(self):
        """Ordering is the whole of the change — a hero further down the page
        is not a hero, and nothing about the markup says so."""
        page = self.client.get(reverse("extension")).content.decode()
        hero = page.index("bx-hero-shot")
        grid = page.index("What it looks like on a paper")
        self.assertLess(hero, grid)

    def test_it_is_not_also_in_the_grid(self):
        """The same picture twice on one page reads as a mistake in the data,
        and `_extension_hero` is the only thing preventing it."""
        hero, rest = _extension_hero(_extension_screenshots())
        self.assertIsNotNone(hero, "the hero capture is not deployed")
        self.assertNotIn(HERO_SCREENSHOT,
                         [shot["src"].rsplit("/", 1)[-1] for shot in rest])
        self.assertEqual(len(rest), len(LISTING_SCREENSHOTS) - 1)

    def test_the_crop_is_drawn_and_the_whole_capture_enlarges(self):
        """The crop is what makes it legible at this width; Enlarge is where
        the rest of the page it sits on went. A hero whose Enlarge opens the
        crop again is a control that does nothing."""
        hero, _rest = _extension_hero(_extension_screenshots())
        self.assertTrue(hero["src"].endswith(HERO_SCREENSHOT_CROP),
                        "the cropped file is not deployed")
        self.assertTrue(hero["full"].endswith(HERO_SCREENSHOT))
        self.assertNotEqual(hero["src"], hero["full"])


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheSettingsSectionNamesTheRealControlsTests(TestCase):
    """`/extension/` explains the settings, and must name them as they appear.

    A third printed string held in two places: the checkbox labels in
    `browser-extension/src/options.html` and the settings table on the install
    page. Somebody reads the page, opens the settings and looks for the words
    they just read — a paraphrase sends them somewhere the control is not, which
    is the softer form of a failure this repo already has a rule about.

    Derived from the options page rather than spelled here, so renaming a
    checkbox fails this instead of quietly making the website wrong. It is the
    same reason the title normaliser is pinned against shared vectors: the two
    sides cannot be allowed to drift apart on their own.
    """

    databases = {"pipeline_db", "academy_db"}

    def option_labels(self):
        options = (Path(__file__).resolve().parent.parent / "browser-extension"
                   / "src" / "options.html").read_text()
        labels = re.findall(r"<label for=\"[^\"]+\"><b>([^<]+)</b>", options)
        # If this ever finds nothing the test would pass by vacuum, which is the
        # shape of every silent omission in this repository.
        self.assertGreaterEqual(len(labels), 6, "no checkbox labels were parsed")
        return labels

    def test_every_setting_is_explained_on_the_page(self):
        page = self.client.get(reverse("extension")).content.decode()
        for label in self.option_labels():
            self.assertIn(escape(label), page,
                          f"the install page does not name the {label!r} setting")

    def test_it_makes_the_case_for_the_broad_permission(self):
        """The section exists for one setting; the rest is context.

        Without "Run on any site" a proxied or society-journal paper is silently
        unmarked, so the page has to say both what is lost and that granting it
        costs no privacy — the second is what a careful reader is weighing.
        """
        page = self.client.get(reverse("extension")).content.decode().lower()
        self.assertIn("proxy", page)
        self.assertIn("costs you no privacy", page)


class TheTeamCanStillReachTheBuildTests(TestCase):
    """`/extension/download/` is linked from a page that can actually show it.

    The zip is the artefact uploaded to a store and only the deployed server can
    build it, so the link has to exist somewhere — a routed endpoint nothing
    points at is the failure this repo keeps meeting.

    **It is on the pipeline hub, and the reason is the cache.** It was put in the
    footer of `/extension/` on 3 Sep 2026 and was invisible to the member it was
    for: that page is public, so `cache_headers` stamps an anonymous 200 with
    `s-maxage` and takes `Cookie` out of `Vary`, and Cloudflare then hands its one
    stored copy to everybody. A purge did not help, because what repopulated the
    edge was another anonymous copy. `/pipeline/` is in `NEVER_CACHED_PREFIXES`,
    so the hub is rendered per visitor.

    So this asserts the two halves apart: the public page must NOT carry the link
    (it cannot show it honestly, and a cached copy offering a zip to strangers is
    the thing removing the hero link was for), and the hub must.
    """

    databases = {"pipeline_db", "academy_db"}

    def _sign_in_as_member(self):
        """A working account is three rows in two databases (services/members).

        Written out rather than through `grant()` because what is under test is
        the *read* — that the page asks the same two questions the decorator
        does, in the same database.
        """
        User.objects.create_user(username="scientist", password="x")
        pipe_user = User.objects.db_manager("pipeline_db").create_user(
            username="scientist")
        site = Site.objects.create(name="Leicester")
        Member.objects.create(user_id=pipe_user.pk, site=site, is_active=True)
        self.client.login(username="scientist", password="x")

    @override_settings(EXTENSION_PAGE_PUBLIC=True)
    def test_the_public_page_does_not_carry_it(self):
        """Not an omission — the page cannot render a per-visitor control."""
        page = self.client.get(reverse("extension")).content.decode()
        self.assertNotIn(reverse("extension_download"), page)

    @override_settings(EXTENSION_PAGE_PUBLIC=True)
    def test_not_even_for_a_signed_in_member(self):
        """The half that makes the point: membership must change nothing here.

        A member-conditional branch on this template would pass every other test
        in this file and still be invisible in production, because the copy the
        edge serves was rendered for somebody else.
        """
        self._sign_in_as_member()
        page = self.client.get(reverse("extension")).content.decode()
        self.assertNotIn(reverse("extension_download"), page)

    def test_the_hub_carries_it(self):
        self._sign_in_as_member()
        page = self.client.get(reverse("pipeline:hub")).content.decode()
        self.assertIn(reverse("extension_download"), page)

    def test_the_hub_names_the_version_the_zip_would_be(self):
        """The one way to waste a submission is to upload a number a store has.

        `manifest_version` reads the same file `build_zip_bytes` names the
        artefact from, so the row cannot claim a version the zip would not be.
        """
        from core.extension_index import manifest_version

        self._sign_in_as_member()
        page = self.client.get(reverse("pipeline:hub")).content.decode()
        version = manifest_version()
        self.assertTrue(version, "the manifest carries no version")
        self.assertIn(version, page)

    def test_a_stranger_is_refused_the_route_itself(self):
        """The link is who SEES it; the decorator is who may HAVE it.

        Every assertion above reads a rendered page, so a view that had lost its
        `pipeline_member_required` would pass all of them while serving the build
        to anyone who typed the URL.
        """
        response = self.client.get(reverse("extension_download"))
        self.assertNotEqual(response.status_code, 200)


class ChangingAVerdictReachesTheSnapshotTests(TestCase):
    """A recommendation nobody can see is not a recommendation.

    `/extension/index.json` serves cached bytes, and nothing used to clear them
    on a write. So setting a flag on the recommendations board redrew the board
    and left the extension reporting the previous verdict for up to an hour —
    on top of a Cloudflare hour and a daily refresh per install. Roughly a day
    in total, with no screen anywhere saying so: the gene page and a hover card
    on top of it disagreed, and only knowing this number existed told you which
    was current (SERPINA1/GTX112707, 28 Aug 2026).

    The signal is what makes it unforgettable — these flags are written from the
    recommendations board, `review.release`, `review.withdraw`, both Access
    importers, the antibody board and management commands, and a path that
    forgot to invalidate would look exactly like one that did.

    Asserted on the CACHE KEY rather than by reading the view twice: the view
    rebuilds from the same test database either way, so a stale cache and a
    fresh one produce identical bytes here and the assertion would pass while
    doing nothing.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        company = Company.objects.create(name="GeneTex")
        target = Target.objects.create(gene_name="SERPINA1")
        self.antibody = Antibody.objects.create(
            target=target, company=company, catalogue_number="GTX112707",
            rrid="AB_11161999")
        self.image = PublicationImage.objects.create(
            antibody=self.antibody, application_type="WB", image="pubs/x.png")

    def _warm(self):
        from django.core.cache import cache
        cache.set(EXTENSION_INDEX_CACHE_KEY, b"stale")
        return cache

    def test_saving_a_recommendation_drops_the_snapshot(self):
        cache = self._warm()
        self.antibody.wb_recommended = True
        self.antibody.save()
        self.assertIsNone(cache.get(EXTENSION_INDEX_CACHE_KEY),
                          "the extension would serve the old verdict")

    def test_publishing_a_figure_drops_it_too(self):
        """A figure is what makes an application assessed.

        Publishing or withdrawing one changes verdicts on antibodies whose own
        row never moved, so watching the flags alone would miss it.
        """
        cache = self._warm()
        PublicationImage.objects.create(
            antibody=self.antibody, application_type="IP", image="pubs/y.png")
        self.assertIsNone(cache.get(EXTENSION_INDEX_CACHE_KEY))

    def test_withdrawing_a_figure_drops_it(self):
        cache = self._warm()
        self.image.delete()
        self.assertIsNone(cache.get(EXTENSION_INDEX_CACHE_KEY))

    def test_the_view_rebuilds_after_an_invalidation(self):
        """End to end: clear it, ask for it, get real bytes rather than none."""
        from django.core.cache import cache

        invalidate_snapshot()
        body = self.client.get(reverse("extension_index")).content
        self.assertIn(b'"antibodies"', body)
        self.assertEqual(cache.get(EXTENSION_INDEX_CACHE_KEY), body,
                         "the view did not repopulate the key it reads")


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheScaleTheHeroClaimsIsDerivedTests(TestCase):
    """The page led with a typed number, and a typed number is quoted for a year.

    It said "more than 29,000 published papers" as prose. The citation snapshot
    counts its own papers and `pipeline/public.py::headline_counts` is the one
    reader for what the site has published — the same *tests* the home page
    leads with — so the page can say both without either drifting.

    The direction that matters is the empty one. A sentence built around a count
    that came back nought would read "So far that is 0 application-level test
    results", on the page whose whole job is to say there is data worth
    installing for, and a deploy against an empty database is exactly when
    nobody is looking. So the paragraph is drawn only when there is something to
    draw, and `NothingPublishedClaimsNoScaleTests` below pins the other half.
    """

    databases = {"pipeline_db", "academy_db"}

    CLAIM = "application-level test results"

    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name="GeneTex")
        target = Target.objects.create(gene_name="SERPINA1")
        antibody = Antibody.objects.create(
            target=target, company=company, catalogue_number="GTX112707",
            rrid="AB_11161999")
        for app in ("WB", "IP"):
            PublicationImage.objects.create(
                antibody=antibody, application_type=app,
                image=f"pubs/{app}.png")

    def test_the_figure_count_is_the_one_the_home_page_prints(self):
        from pipeline.public import headline_counts

        self.assertEqual(headline_counts()["experiment_count"], 2)
        page = self.client.get(reverse("extension")).content.decode()
        self.assertIn("2 application-level test results", page)

    def test_the_paper_count_comes_from_the_snapshot_that_holds_it(self):
        """Not a rounded figure in the template: the number, or no clause.

        A snapshot that is not deployed is a legitimate state — the citation
        artefact is built by `manage.py build_citation_index` and served as its
        own resource precisely so a missing one degrades — so the papers half is
        a clause inside the sentence and not the sentence.
        """
        from core import citations

        summary = citations.summary()
        page = self.client.get(reverse("extension")).content.decode()
        papers = ((summary or {}).get("counts") or {}).get("papers")
        if not papers:
            self.assertIn(self.CLAIM, page)
            self.assertNotIn("published papers", page)
            return
        self.assertIn(f"{papers:,} published papers", page)
        self.assertIn(summary["source"]["citeab_data_through"], page)


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class NothingPublishedClaimsNoScaleTests(TestCase):
    """Its own class because the fixture is the absence of one."""

    databases = {"pipeline_db", "academy_db"}

    def test_an_empty_dataset_draws_no_claim_at_all(self):
        page = self.client.get(reverse("extension")).content.decode()
        self.assertNotIn("application-level test results", page,
                         "the page claims a scale it has no figures for")
        self.assertNotIn("0 application-level", page)


@override_settings(EXTENSION_PAGE_PUBLIC=True)
class TheApprovalSectionCarriesRealIdentifiersTests(TestCase):
    """A managed browser allows an extension by identifier, so a wrong one is
    worse than none: the page reads as the answer, IT pastes it, and the install
    still fails with nothing pointing back here.

    All of them are derived, so what is pinned is the derivation and the
    fallback — not the strings, which change when a listing moves.
    """

    databases = {"pipeline_db", "academy_db"}

    CHROME = ("https://chromewebstore.google.com/detail/oga/"
              "pkmfholpagoaiigfpbkalnpipkiiidlp")

    def test_the_chrome_id_is_taken_from_the_store_url(self):
        with override_settings(EXTENSION_CHROME_URL=self.CHROME):
            self.assertEqual(_extension_identity()["chrome_id"],
                             "pkmfholpagoaiigfpbkalnpipkiiidlp")

    def test_a_url_that_is_not_a_listing_yields_no_id(self):
        """Better to say where to find it than to print the wrong thing.

        An extension id is 32 letters a-p. Anything else means the setting is
        not a store link — a placeholder, a shortened link, a typo — and the
        page falls back to telling the reader where to read it off.
        """
        for url in ("", "https://example.org/", "https://example.org/detail/oga",
                    "https://chromewebstore.google.com/detail/oga/TOOSHORT"):
            with self.subTest(url=url), override_settings(EXTENSION_CHROME_URL=url):
                self.assertEqual(_extension_identity()["chrome_id"], "")

    def test_the_firefox_identity_comes_from_the_shipped_manifest(self):
        import json
        from pathlib import Path

        from django.conf import settings

        with open(Path(settings.BASE_DIR) / "browser-extension" / "manifest.json",
                  encoding="utf-8") as fh:
            gecko = json.load(fh)["browser_specific_settings"]["gecko"]

        identity = _extension_identity()
        self.assertEqual(identity["gecko_id"], gecko["id"])
        self.assertEqual(identity["update_url"], gecko["update_url"])

    def test_the_site_count_is_domains_and_not_match_patterns(self):
        """Derived from the wrong quantity is still the wrong number.

        A count typed here is wrong the first time a publisher is added, which
        is why it is read off the manifest. But it was read off ``matches``,
        and every site is listed twice there — ``https://*.nature.com/*`` and
        ``https://nature.com/*`` — so the page said **196** where the list
        covers **100** domains, in the section an IT team reads to decide
        whether to allow the extension. Nothing contradicted it: the number was
        derived, it moved when the list moved, and it was double.

        So this asserts the two are NOT equal, which is the only direction that
        can fail if somebody puts ``len(matches)`` back.
        """
        import json
        from pathlib import Path

        from django.conf import settings

        from core.views import _manifest_domains

        with open(Path(settings.BASE_DIR) / "browser-extension" / "manifest.json",
                  encoding="utf-8") as fh:
            matches = json.load(fh)["content_scripts"][0]["matches"]

        count = _extension_identity()["site_count"]
        self.assertEqual(count, len(_manifest_domains(matches)))
        self.assertGreater(len(matches), count)
        self.assertGreater(count, 1)

    def test_every_manifest_host_folds_to_a_domain_with_a_real_suffix(self):
        """A two-part suffix this does not know about invents a domain.

        ``_manifest_domains`` keeps the last two labels, or three where the
        second-to-last is a two-part suffix it recognises. An unrecognised one
        — ``jst.go.jp`` was the live example — would fold to ``go.jp`` and be
        counted as a site alongside every other Japanese publisher, quietly
        merging rows rather than failing. Cheapest check that would notice: no
        derived domain may be a bare public suffix.
        """
        import json
        from pathlib import Path

        from django.conf import settings

        from core.views import _TWO_PART_TLDS, _manifest_domains

        with open(Path(settings.BASE_DIR) / "browser-extension" / "manifest.json",
                  encoding="utf-8") as fh:
            matches = json.load(fh)["content_scripts"][0]["matches"]

        for domain in _manifest_domains(matches):
            head = domain.split(".")[0]
            self.assertNotIn(head, _TWO_PART_TLDS,
                             f"{domain} is a public suffix, not a site")
            self.assertGreaterEqual(len(domain.split(".")), 2, domain)

    def test_the_page_draws_the_identifiers_it_tells_it_to_use(self):
        with override_settings(EXTENSION_CHROME_URL=self.CHROME):
            page = self.client.get(reverse("extension")).content.decode()
        identity = _extension_identity()
        self.assertIn("If your institution blocks extensions", page)
        self.assertIn(identity["gecko_id"], page)
        self.assertIn("pkmfholpagoaiigfpbkalnpipkiiidlp", page)
        self.assertIn("ExtensionInstallAllowlist", page)
        self.assertIn("ExtensionSettings", page)
        self.assertIn(reverse("extension_firefox_xpi"), page)
