"""The discontinued tick, pressed in a real browser.

The filter bar has no submit button — every control submits the form from a
``change`` listener, and that listener selected ``select[name]``. A tick box is
not a ``select``, so a checkbox added to that bar renders perfectly, is
clickable, reports itself checked, and changes nothing: the exact shape this
repo keeps meeting, where the page is right, the control is there, pressing it
does nothing and nothing on screen says why. ``manage.py check`` passes on it,
``node --check`` passes on it, and a response test passes on it too — the markup
is all present. Only a press can tell.

The round trip is what is asserted, not the tick's own state: pressing it must
reach the server and come back with the hidden rows in the table, and pressing
it again must put them away. Everything else about this feature — the count, the
``?ab=`` exemption, the refusals — is server-rendered and pinned far more
cheaply in ``core/tests_availability.py``, which is the rule
``tests_feasibility_add.py`` earns its keep by.

Both tests stay on the push tier. A tick that silently does nothing is a
discontinued reagent the reader cannot reach at all, on the one page where the
whole dataset is published; and the label under the count is the only route to
the tick that a reader who has just read "Not shown: 12" is actually looking at.
"""
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.urls import reverse

# The browser and the skip condition are `pipeline/tests_browser_board.py`'s —
# a second chrome-finder here is exactly the copy that drifts. It also sets
# DJANGO_ALLOW_ASYNC_UNSAFE on import, which the sync API needs before the ORM
# will answer.
from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

from pipeline.models import Antibody, Company, PublicationImage, Target

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class DiscontinuedTickInARealBrowserTests(StaticLiveServerTestCase):
    databases = {"pipeline_db", "academy_db"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        company = Company.objects.create(name="Proteintech")
        target = Target.objects.create(protein_name="Synuclein", gene_name="SNCA")
        for catalogue, gone in (("10842-1-AP", False), ("60009-1-Ig", True)):
            ab = Antibody.objects.create(
                target=target, company=company,
                catalogue_number=catalogue, out_of_market=gone)
            PublicationImage.objects.create(
                antibody=ab, application_type="WB", image=f"pubs/{catalogue}.png")
        self.page = self._browser.new_page()
        self.page.goto(
            self.live_server_url
            + reverse("antibody_table", kwargs={"gene_name": "SNCA"}))

    def tearDown(self):
        self.page.close()

    def _rows(self):
        return self.page.eval_on_selector_all(
            "#antibody-table .row-box[data-antibody-id] h3",
            "els => els.map(e => e.textContent.trim())")

    def test_ticking_the_box_brings_the_discontinued_row_back(self):
        self.assertEqual(self._rows(), ["10842-1-AP"])

        self.page.check("#discontinued")
        # The form has no submit button: if the change listener does not fire,
        # this waits out its timeout on a page that looks perfectly correct.
        self.page.wait_for_url("**discontinued=show**")
        self.assertIn("60009-1-Ig", self._rows())

        self.page.uncheck("#discontinued")
        self.page.wait_for_function(
            "() => !location.search.includes('discontinued')")
        self.assertEqual(self._rows(), ["10842-1-AP"])

    def test_the_label_under_the_count_works_the_tick(self):
        # It is a <label for=...> outside the form, which is the one thing that
        # makes it a route to the control rather than a second copy of it.
        self.page.click(".oga-discontinued-toggle")
        self.page.wait_for_url("**discontinued=show**")
        self.assertIn("60009-1-Ig", self._rows())
        self.assertTrue(self.page.is_checked("#discontinued"))
