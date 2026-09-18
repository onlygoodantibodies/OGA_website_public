"""The declared-target lists, and the two tools reading one copy of them.

What is defended here is mostly ARITHMETIC AND PAIRING, because that is where
this can go wrong without anything looking wrong. A dropped CSV row makes a
documented paper read as one nobody reviewed. A payload key the client cannot
read draws no mark and reports no error. A verdict attached to the wrong (paper,
product) pair puts an accusation on the wrong reagent, in a reply that looks
exactly like a correct one.

The pairing tests are the same arrangement as
``core/data/title_normalisation_vectors.json``: the extension's shipped fixture
and this builder must agree, and neither side can move alone.
"""
from django.core.checks import Warning as CheckWarning
from django.test import TestCase

import json
from pathlib import Path
from unittest import mock

from django.conf import settings

from core import apps as core_apps
from core import target_confusions as tc


class TheListsOnFileTests(TestCase):
    """The two reviews as committed, read end to end."""

    databases = {"pipeline_db", "academy_db"}

    def test_nothing_was_dropped(self):
        # A row this cannot use is left out AND counted, never guessed at. The
        # count being zero is what says the committed files are all readable;
        # `core.W005` is what says so at deploy when they are not.
        self.assertEqual(tc.problems(), ())

    def test_every_review_is_loaded_with_its_source(self):
        # The exact set, so adding a list is a deliberate edit here rather than
        # a silent widening — and so a file that stopped loading is a failure
        # and not a quietly shorter answer.
        by_id = {n["id"]: n for n in tc.load()}
        self.assertEqual(set(by_id), {"p16-ink4a", "beta-galactosidase",
                                      "perk-for-p-erk", "p-erk-for-perk"})
        for notice in by_id.values():
            # The link is the reader's whole way to check this, so a notice
            # without one is refused at load rather than served unbacked.
            self.assertTrue(notice["source"]["url"].startswith("https://"))
            self.assertTrue(notice["antibodies"])

    def test_the_counts_are_what_the_reviews_say(self):
        p16 = tc.paper_counts(next(n for n in tc.load() if n["id"] == "p16-ink4a"))
        self.assertEqual(p16["papers"], 406)
        self.assertEqual(p16[tc.MISTAKEN], 317)
        self.assertEqual(p16[tc.AS_DECLARED], 17)
        self.assertEqual(p16[tc.NOT_CHECKED], 72)

    def test_the_correct_uses_have_not_vanished(self):
        """If this ever reaches zero, stop and look at the data.

        `as_declared` is the state that suppresses a mark entirely, and it is the
        only guard against telling 17 sets of authors they used the wrong
        antibody. A list that lost its verdict column would load cleanly, count
        every row as `mistaken`, and take that guard with it — with no other test
        in either suite noticing.
        """
        declared = [(doi, code)
                    for n in tc.load()
                    for doi, codes in n["papers"].items()
                    for code, verdict in codes.items()
                    if verdict == tc.AS_DECLARED]
        self.assertGreater(len(declared), 0)

    def test_a_verdict_belongs_to_one_paper_and_one_product(self):
        # This DOI is on the p16 list for sc-166760. Answering for ab51243 would
        # be the citation layer's wrong-paper failure one field along, and it is
        # invisible: both codes are on the same list.
        doi = "10.1016/j.exger.2019.110805"
        self.assertEqual(tc.verdict_for(doi, "sc-166760"), tc.NOT_CHECKED)
        self.assertIsNone(tc.verdict_for(doi, "ab51243"))

    def test_a_doi_is_keyed_however_it_is_printed(self):
        # Through `citations.normalise_doi`, which the extension mirrors — so the
        # key a page computes is the key stored here.
        for printed in ("10.1038/s41586-019-0885-0",
                        "10.1038/S41586-019-0885-0",
                        "https://doi.org/10.1038/s41586-019-0885-0",
                        "doi:10.1038/s41586-019-0885-0",
                        "  10.1038/s41586-019-0885-0 "):
            self.assertEqual(tc.verdict_for(printed, "ab51243"), tc.MISTAKEN,
                             printed)

    def test_a_product_code_is_matched_case_folded_and_depunctuated(self):
        for printed in ("ab51243", "AB51243", "Ab51243"):
            notice, antibody = tc.for_identifier(printed)
            self.assertEqual(antibody["identifier"], "ab51243", printed)
        # Alphanumerics-only, for a publisher that moved the separator.
        self.assertEqual(tc.for_identifier("1467 7381")[1]["identifier"],
                         "14-6773-81")
        self.assertEqual(tc.for_identifier("not-a-code"), (None, None))

    def test_a_short_collapsed_form_cannot_match_anything(self):
        # MIN_COLLAPSED, the same number and the same reason as both tools'
        # catalogue lookups: below it, an alphanumerics-only form is too easily
        # another supplier's number.
        self.assertEqual(tc._collapsed("ab98"), "")

    def test_a_paper_on_two_lists_reports_both(self):
        found = tc.for_paper("10.1139/bcb-2018-0126")
        self.assertEqual({r["identifier"] for r in found}, {"ab51243", "ab9361"})
        # The supplier's spelling, not the reviewer's: the two sheets disagree
        # about the case of one Abcam product, and what a reader will search for
        # is the one on the datasheet.
        self.assertNotIn("AB9361", {r["identifier"] for r in found})


class APaperIsNamedThreeWaysTests(TestCase):
    """A list row is reachable by whichever identifier the page in front of the
    reader happens to declare.

    Until 0.4.2 it was the DOI and nothing else, which is exactly as far as a
    publisher's own page gets you and no further. Four of the PERK papers are
    e-Century titles (Am J Transl Res, Am J Cancer Res) and e-Century registers
    no DOI with Crossref at all — so a paper the reviewer HAD established was
    reachable by neither tool, and because both PERK lists are `papers_only` the
    result was not a soft hedge but silence: a documented misuse handed back as
    an open question, which is the direction that costs something.

    The three keys, their order and their normalisers are `paper.js`'s, which
    the citation layer has used since it shipped. "Which paper is this" is one
    question and must not get two answers.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_a_doi_row_still_keys_exactly_as_it_did(self):
        # The existing keys must not move: they are in a snapshot every install
        # re-downloads, and a shipped client looks up `papers[doi]` with no
        # prefix. Everything else here is additive.
        self.assertEqual(tc.paper_keys(doi="10.1038/s41586-019-0885-0"),
                         ["10.1038/s41586-019-0885-0"])

    def test_a_pmid_row_is_reachable_by_its_pmid(self):
        self.assertEqual(tc.verdict_for(None, "#3179", pmid="36915767"),
                         tc.MISTAKEN)

    def test_the_four_papers_with_no_doi_are_no_longer_lost(self):
        # The whole reason for the release. Each is the reviewer's own verdict,
        # against the product code that paper actually names.
        for pmid, code in (("36915767", "#3179"), ("32194908", "#3192"),
                           ("36105027", "ab229912"), ("34873488", "ab229912")):
            self.assertEqual(tc.verdict_for(None, code, pmid=pmid), tc.MISTAKEN,
                             f"PMID {pmid} / {code}")

    def test_a_pmid_says_nothing_about_a_product_it_does_not_name(self):
        # A paper key is not a blanket verdict: the row names one product.
        self.assertIsNone(tc.verdict_for(None, "ab65142", pmid="36915767"))

    def test_a_pmid_is_read_however_it_is_printed(self):
        for printed in ("36915767", "PMID: 36915767", " 36915767 ", 36915767):
            self.assertEqual(tc.verdict_for(None, "#3179", pmid=printed),
                             tc.MISTAKEN, repr(printed))

    def test_a_title_needs_its_year_before_it_is_believed(self):
        # A title is a string two papers can share; the year is what confirms
        # it. `paper.js::confirmPaper` enforces the same rule for the citation
        # layer and this is not a second opinion about it.
        self.assertEqual(tc.paper_keys(title="Some Paper"), [])
        self.assertEqual(len(tc.paper_keys(title="Some Paper", year=2020)), 1)

    def test_a_page_is_looked_up_across_the_year_slack(self):
        # Online-first and print dates routinely differ by one, so the PAGE is
        # tried at three years while the ROW is stored under the one the
        # reviewer recorded. Storing three would put a tolerance into the data.
        keys = tc._lookup_keys(title="Some Paper", year=2020)
        self.assertEqual(len(keys), 2 * tc.YEAR_SLACK + 1)
        self.assertEqual(keys, sorted(keys, key=lambda k: int(k.rsplit(":", 1)[1])))

    def test_the_key_kinds_cannot_collide(self):
        # A DOI is stored bare so shipped clients keep working, so the prefixed
        # kinds must be unmistakable. Every DOI begins "10.".
        for notice in tc.load():
            for key in notice["papers"]:
                self.assertTrue(
                    key.startswith("10.") or key.startswith("pmid:")
                    or key.startswith("title:"), key)

    def test_a_row_naming_no_paper_at_all_is_counted_and_named(self):
        problems = []
        import tempfile
        rows = "doi,identifier,use\n,ab51243,wrong\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            path.write_text(rows, encoding="utf-8")
            papers, tally = tc._read_papers(path, "test", problems)
        self.assertEqual(papers, {})
        self.assertEqual(tally["papers"], 0)
        self.assertEqual(len(problems), 1)
        # It must name all three ways, or the fix is a guess.
        self.assertIn("DOI, PubMed id, or title and year", problems[0])


class TheTallyCountsPapersAndNotRowsTests(TestCase):
    """The number on the card is "N papers", so it counts papers.

    Two things make that stop being the same as counting the lookup map. A row
    keyed by both a DOI and a PMID occupies two slots in it; and a paper naming
    two of the listed product codes contributes two verdicts. Counting either
    prints a figure larger than the review's own, underneath a link to it.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_every_list_agrees_with_its_own_published_figure(self):
        counts = {n["id"]: tc.paper_counts(n) for n in tc.load()}
        self.assertEqual(counts["p16-ink4a"][tc.MISTAKEN], 317)
        self.assertEqual(counts["p16-ink4a"]["papers"], 406)
        self.assertEqual(counts["beta-galactosidase"][tc.MISTAKEN], 54)
        # 187 is the figure in the article. The rows hold 191 (paper, code)
        # pairs, because four papers name two antibodies each.
        self.assertEqual(counts["perk-for-p-erk"][tc.MISTAKEN], 187)
        self.assertEqual(counts["perk-for-p-erk"]["papers"], 187)

    def test_a_paper_under_two_keys_is_one_paper(self):
        import tempfile
        problems = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            path.write_text("doi,identifier,use,pmid\n"
                            "10.1/x,ab1,wrong,12345678\n", encoding="utf-8")
            papers, tally = tc._read_papers(path, "test", problems)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(papers), ["10.1/x", "pmid:12345678"])
        self.assertEqual(tally["papers"], 1)
        self.assertEqual(tally[tc.MISTAKEN], 1)

    def test_a_paper_naming_two_products_is_one_paper(self):
        import tempfile
        problems = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            path.write_text("doi,identifier,use\n"
                            "10.1/x,ab1,wrong\n10.1/x,ab2,wrong\n",
                            encoding="utf-8")
            _papers, tally = tc._read_papers(path, "test", problems)
        self.assertEqual(tally["papers"], 1)
        self.assertEqual(tally[tc.MISTAKEN], 1)


class TheSnapshotCarriesThemTests(TestCase):
    """What the extension downloads, and the pairing that stops it drifting."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.payload = tc.index_payload()

    def test_the_snapshot_carries_the_notices(self):
        from core.extension_index import build_index

        index = build_index()
        self.assertEqual(index["target_confusions"], self.payload)
        # The key is ADDITIVE. Every install re-downloads this file daily and
        # would receive it long before a build that knows what to do with it, so
        # the outer schema must not move — a bump strands the field on the
        # 18-record dev fixture (`background.js` gates on `schema === 1`).
        self.assertEqual(index["schema"], 1)

    def test_the_shipped_client_fixture_is_the_same_table(self):
        """The extension's committed fixture must be what this builder emits.

        The fixture is otherwise rebuilt from live data, but these notices come
        from committed files, so a rebuild produces exactly this. Pinned because
        the failure is silent in the direction that matters: a fixture carrying
        an older table makes every notice test in the node suite pass against
        data the server no longer serves.
        """
        path = Path(settings.BASE_DIR) / "browser-extension" / "data" / "index.json"
        shipped = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(shipped.get("target_confusions"), self.payload)

    def test_every_product_code_resolves_to_wording_the_client_can_draw(self):
        # A payload whose two halves disagree draws a red mark with an empty
        # card, which is worse than no mark. `matcher.js::confusionFor` refuses
        # that pair; this is the half that stops it being built.
        for key, entry in self.payload["antibodies"].items():
            self.assertIn(entry["kind"], self.payload["kinds"], key)
            # A key is either the supplier's own spelling, case-folded, or the
            # bare-token alias a page can actually produce for a code whose
            # spelling carries punctuation the tokeniser will not emit — and the
            # entry keeps the supplier's spelling either way, so the card prints
            # `#3179` however the page wrote it.
            bare = "".join(c for c in entry["id"].lower() if c.isalnum())
            self.assertIn(key, {entry["id"].lower(), bare}, key)
            if key != entry["id"].lower():
                self.assertTrue(entry.get("supplier_required"),
                                f"{key}: a bare alias without the supplier gate")
            kind = self.payload["kinds"][entry["kind"]]
            for field in ("declared", "mistaken", "mistaken_protein", "short",
                          "url", "source"):
                self.assertTrue(kind.get(field), f"{key}: {field}")

    def test_a_code_needing_its_supplier_ships_cues_a_paper_would_print(self):
        # `Invitrogen` and `Thermo` both mean Thermo Fisher Scientific, and a
        # gate testing the stored supplier string would recognise neither — so
        # the gate would never open and the notice would never fire.
        gated = {k: e for k, e in self.payload["antibodies"].items()
                 if e.get("supplier_required")}
        # Millipore's `AB986` and Thermo's `A-11132` are typeset like an Abcam
        # number; Cell Signaling's three are BARE FOUR-DIGIT NUMBERS, and
        # `TOKEN_RE` matches any alphanumeric run, so `3192` on any page is a
        # candidate identifier.
        #
        # Since 0.4.2 those three are ALSO keyed under the bare token, because
        # the tokeniser never emits the `#` and they were reachable by nothing
        # at all. That makes the gate load-bearing again rather than
        # belt-and-braces: it is the only thing standing between a four-digit
        # number and a red mark, which is why the alias is written only for a
        # code that has one.
        self.assertEqual(set(gated),
                         {"ab986", "a-11132", "#3192", "#3179", "#5683",
                          "3192", "3179", "5683"})
        for key, entry in gated.items():
            self.assertTrue(entry["cues"], key)
            self.assertTrue(all(c == c.lower() for c in entry["cues"]), key)

    def test_only_a_code_the_collapsed_map_cannot_reach_gets_a_bare_alias(self):
        # Thermo's `A-11132` needs nothing: the token keeps its dash and its
        # collapsed form is seven characters, so the punctuation-insensitive map
        # already resolves `A11132`. Writing an alias for it too would be a
        # second exact key doing a job something else already does.
        alias = {k for k, e in self.payload["antibodies"].items()
                 if k != e["id"].lower()}
        self.assertEqual(alias, {"3179", "3192", "5683"})
        for key in alias:
            self.assertLess(len(key), tc.MIN_COLLAPSED, key)

    def test_every_verdict_in_the_payload_is_one_the_client_knows(self):
        seen = {v for codes in self.payload["papers"].values()
                for v in codes.values()}
        self.assertTrue(seen <= set(tc.VERDICTS), seen)

    def test_the_payload_is_byte_identical_between_builds(self):
        # The property `extension_index._dataset_stamp` exists to protect: a
        # snapshot that differs on unchanged data defeats every cache between
        # here and the reader, and each install re-downloads a megabyte to be
        # told what it already knew.
        first = json.dumps(tc.index_payload(), separators=(",", ":"))
        tc.index_payload.cache_clear()
        tc._loaded.cache_clear()
        tc._by_identifier.cache_clear()
        self.assertEqual(json.dumps(tc.index_payload(), separators=(",", ":")),
                         first)


class ABadRowIsSaidOutLoudTests(TestCase):
    """`core.W005`: dropped is right, silent is not."""

    databases = {"pipeline_db", "academy_db"}

    def test_a_clean_checkout_warns_about_nothing(self):
        self.assertEqual(core_apps._check_target_confusion_files(None), [])

    def test_an_unreadable_row_is_named_at_deploy(self):
        with mock.patch.object(tc, "problems",
                               return_value=("p16-ink4a: line 4 has no DOI",)):
            found = core_apps._check_target_confusion_files(None)
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], CheckWarning)
        self.assertEqual(found[0].id, "core.W005")
        self.assertIn("line 4 has no DOI", found[0].hint)

    def test_it_is_a_warning_and_never_an_error(self):
        # `manage.py check` runs inside the pre-deploy `migrate`, so an error
        # here would let a malformed reference file take the site down.
        with mock.patch.object(tc, "problems", return_value=("x",)):
            found = core_apps._check_target_confusion_files(None)
        self.assertFalse(any(getattr(f, "is_serious", lambda: False)()
                             for f in found))


class ThePageSaysWhatIsActuallyServedTests(TestCase):
    """`/extension/` describes the notices as live, and must stop if they are not.

    The data reaches every install on a site deploy; the code that draws a mark
    from it reaches a reader only when a store approves it. So the page's claim
    about these marks is a claim about which version is being served, and this
    class has now pinned it in **both** directions — which is the point worth
    keeping.

    It was written the other way round. The page carried a *Coming in N* block
    while the feature was in review, `core.W006` warned once N became the served
    version, it fired on the deploy of 12 Sep 2026, and the copy went
    present-tense in answer. The check was inverted rather than deleted, because
    the new sentence has its own silent failure: a rollback of
    `EXTENSION_XPI_VERSION` leaves the page promising a mark the installable
    build does not draw, and the page renders perfectly either way.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.page = (Path(settings.BASE_DIR) / "core" / "templates" / "core"
                     / "extension.html").read_text(encoding="utf-8")

    def test_the_page_has_stopped_promising_it(self):
        # The two surfaces the old copy lived on: the block's tag and the chip
        # beside the Wrong target swatch. Asserted on the RENDERED strings, not
        # on the bare phrase — the comments above both surfaces quote the words
        # they replaced, which is worth keeping and is not a promise to anybody.
        self.assertNotIn('bx-soon-tag">Coming in', self.page)
        self.assertNotIn('bx-key-soon">from', self.page)

    def test_the_colour_key_states_it_in_the_present(self):
        self.assertIn("Red also means the antibody is for a different protein",
                      self.page)
        self.assertNotIn("Red will also mean", self.page)

    def test_both_surfaces_carry_the_chrome_caveat(self):
        # Firefox has it and Chrome does not, so a reader on Chrome must not be
        # told to expect a mark that cannot appear. Two places say so, and the
        # check cannot see either — a store approval leaves no trace here.
        self.assertIn("In Firefox now &middot; Chrome in review", self.page)
        self.assertIn("Firefox now &middot; Chrome in review", self.page)
        self.assertIn("until the Web Store approves", self.page)

    def test_the_block_says_it_is_not_a_verdict(self):
        # The same rule as every other surface: OGA has characterised none of
        # them, and a page advertising a red mark has to say so. The count is
        # derived, so this asserts the sentence and not the number.
        self.assertIn("not a verdict on the antibody", self.page)
        self.assertIn("characterised none of these {{ confusion_totals.codes }}",
                      self.page)

    def test_the_counts_are_derived_and_not_typed(self):
        # "Eleven product codes across the two reviews" was wrong within three
        # days of being written, and nothing on the page said so.
        self.assertIn("{{ confusion_totals.codes }}", self.page)
        self.assertIn("{{ confusion_totals.reviews }}", self.page)
        self.assertNotIn("Eleven product codes", self.page)
        self.assertNotIn("The two reviews are", self.page)

    def test_the_totals_count_sources_rather_than_notices(self):
        # A collision that runs both ways is two notices out of one article, so
        # a reader told "four reviews" would go looking for a fourth.
        totals = tc.public_totals()
        self.assertEqual(totals["codes"],
                         sum(len(n["antibodies"]) for n in tc.load()))
        self.assertLessEqual(totals["reviews"], totals["lists"])

    def test_the_page_says_an_unlisted_paper_is_not_always_marked(self):
        # `papers_only` is user-visible: on the PERK lists a paper the review
        # never saw draws nothing at all, and a page claiming otherwise
        # over-promises for most of the codes on file.
        self.assertIn("Whether an unlisted paper is marked at all",
                      self.page)
        self.assertIn("only the papers the review actually names", self.page)

    def test_the_page_says_how_a_paper_is_matched(self):
        # The limit is real and named: a PubMed id reaches PubMed and Europe PMC
        # and rarely a publisher's own page, which is where a reader usually is.
        self.assertIn("matched by whatever identifier its page declares",
                      self.page)
        self.assertNotIn("A few listed papers cannot be matched at all",
                         self.page)

    def test_a_correct_use_is_named_on_the_page_too(self):
        # The 17 are the design's whole safety argument and the page makes the
        # same promise the card does.
        self.assertIn("found correct gets no mark", self.page)

    def test_the_warning_is_silent_while_that_version_is_served(self):
        # Unset is silent for the same reason `core.W001` is: no signed build
        # offered is a valid state, and it is the one every dev checkout is in.
        for served in ("", tc.EXTENSION_RELEASE, "0.4.1", "0.10.0"):
            with self.settings(EXTENSION_XPI_VERSION=served):
                self.assertEqual(
                    core_apps._check_confusion_release_is_served(None), [],
                    f"fired while {served!r} was being served")

    def test_the_warning_fires_if_an_older_build_is_served(self):
        # The rollback case, which is the one the present-tense copy created.
        for served in ("0.3.3", "0.3.9"):
            with self.settings(EXTENSION_XPI_VERSION=served):
                found = core_apps._check_confusion_release_is_served(None)
            self.assertEqual(len(found), 1, served)
            self.assertEqual(found[0].id, "core.W006")
            self.assertIn(tc.EXTENSION_RELEASE, found[0].msg)
            self.assertIn(served, found[0].msg)
            # It has to say what to change, not only that something is wrong.
            self.assertIn("Wrong target", found[0].hint)


class ANoticeIsNotAVerdictTests(TestCase):
    """The one thing every surface has to keep saying.

    OGA has characterised none of the listed products. The whole risk in the
    feature is a reader — or a model — taking a red mark and a strong sentence
    for a performance result, so the disclaimer is asserted rather than trusted
    to survive an edit.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_the_connector_block_says_so(self):
        from mcp_servers.common import confusions

        self.assertIn("NOT a performance verdict", confusions.NOT_A_VERDICT)
        self.assertIn("reagent identity", confusions.NOT_A_VERDICT)

    def test_the_card_wording_says_so(self):
        # The client's copy. Sent in the payload for the scope note's reasons —
        # so the sentence can be sharpened without a store submission — but this
        # half is the client's own and has to carry it.
        path = Path(settings.BASE_DIR) / "browser-extension" / "src" / "card.js"
        source = path.read_text(encoding="utf-8")
        self.assertIn("OGA has not tested this antibody", source)

    def test_no_notice_names_an_application(self):
        # An application on a notice would draw a chip, and a chip is a verdict.
        for notice in tc.load():
            blob = json.dumps(notice)
            for app in ("\"WB\"", "\"IP\"", "\"FC\"", "recommend"):
                self.assertNotIn(app, blob, notice["id"])


class APapersOnlyListSpeaksOnlyAboutItsOwnPapersTests(TestCase):
    """The unlisted-paper caution is a bet on the base rate.

    A notice is found by the product code, so by default a page naming one is
    marked whatever paper it is, and the card leads *"This paper MAY have used
    the wrong antibody"* with the review's tally behind it. That earns its place
    where the product is mostly misused — 317 of the 406 p16 papers used
    ``ab51243`` as a p16-INK4a antibody.

    PERK is the other way round. ``ab65142`` is a perfectly good PERK antibody
    and nearly every paper citing it used it correctly for the unfolded protein
    response, so the same caution would be a false accusation against a correct
    paper — the failure ``as_declared`` exists to prevent, arriving through a
    different door. 187 wrong papers is a lot in absolute terms and a small
    fraction of that product's use.

    ``papers_only`` is therefore per notice, and both sides are pinned here: a
    flag that turned the caution off everywhere would be a regression on the two
    lists that shipped with it.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_the_flag_reaches_the_extension_payload_per_notice(self):
        kinds = tc.index_payload()["kinds"]
        for notice_id in ("perk-for-p-erk", "p-erk-for-perk"):
            self.assertTrue(kinds[notice_id].get("papers_only"),
                            f"{notice_id} would mark an unlisted paper")
        for notice_id in ("p16-ink4a", "beta-galactosidase"):
            self.assertNotIn("papers_only", kinds[notice_id],
                             f"{notice_id} lost the caution it shipped with")

    def test_the_perk_lists_read_without_losing_rows(self):
        """A dropped row makes a documented paper read as one nobody reviewed,
        which is why `problems()` counts them rather than staying quiet."""
        self.assertEqual(tc.problems(), ())
        counts = {n["id"]: tc.paper_counts(n)
                  for n in tc.load()}
        self.assertEqual(counts["perk-for-p-erk"][tc.MISTAKEN], 187)
        self.assertEqual(counts["p-erk-for-perk"][tc.MISTAKEN], 2)

    def test_both_directions_of_the_collision_are_on_file(self):
        """One declared target per notice, so the reverse is a second list."""
        forward = dict(zip(("notice", "antibody"),
                           tc.for_identifier("ab65142")))
        reverse = dict(zip(("notice", "antibody"),
                           tc.for_identifier("sc-7383")))
        self.assertEqual(forward["notice"]["declared_target"]["gene"], "EIF2AK3")
        self.assertEqual(reverse["notice"]["mistaken_for"]["gene"], "EIF2AK3")
        self.assertEqual(reverse["notice"]["declared_target"]["gene"],
                         "MAPK1/MAPK3")

    def test_a_verdict_is_still_per_paper_and_product(self):
        listed = "10.18632/oncotarget.10087"
        self.assertEqual(
            tc.verdict_for(listed, "ab65142"),
            tc.MISTAKEN)
        # Same paper, a code the list does not name against it.
        self.assertIsNone(tc.verdict_for(listed, "ab192591"))
        self.assertIsNone(
            tc.verdict_for("10.1038/nature12345", "ab65142"))

    def test_the_cell_signaling_numbers_are_gated_on_their_supplier(self):
        """`#3192` collapses to a bare four-digit number, and `TOKEN_RE` matches
        any alphanumeric run — so without the gate any `3192` on a page is a
        candidate identifier. `papers_only` makes this belt-and-braces rather
        than load-bearing, since an unlisted page cannot mark at all, but the
        cheap gate stays."""
        payload = tc.index_payload()["antibodies"]
        for code in ("#3192", "#3179", "#5683"):
            entry = payload[code.lower()]
            self.assertTrue(entry.get("supplier_required"), code)
            self.assertIn("cell signaling", entry["cues"])
