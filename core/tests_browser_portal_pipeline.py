"""The portal's Pipeline Data tab, opened in a real browser.

Everything this tab shows is **unpublished** and belongs to one manufacturer, so
the two ways it can go wrong are both silent. It can show nothing when there is
something — a partner concludes the feature does not work, or worse that OGA has
nothing of theirs, and the pre-release window quietly stops being used. Or the
thumbnails can fail to arrive, which reads the same way.

Neither is visible to a response test. The tab is drawn entirely in the browser
from two API calls, and the images cannot be `<img src>` at all: the key is a
header, so each one is fetched with the key and handed over as a blob. That is
four pieces of wiring between a correct server and a page that shows a partner
their data, and `tests_portal_script.py` — which parses the script and checks
every `onclick` names something — passes on all four being broken.

The scoping itself is asserted far more cheaply in
`core/tests_api_pipeline.py`, which is where a question about *which rows* is
answered. This is only about the press.
"""
import io
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.test import tag
from django.urls import reverse
from PIL import Image

# The browser and the skip condition are `pipeline/tests_browser_board.py`'s —
# a second chrome-finder here is exactly the copy that drifts.
from pipeline.tests_browser_board import CHROME, HAVE_PLAYWRIGHT

from core.models import APIConsumer
from pipeline.models import Antibody, Company, Target
from pipeline.services import review as review_svc

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (30, 90, 200)).save(buf, "PNG")
    return buf.getvalue()


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class PipelineDataTabInARealBrowserTests(StaticLiveServerTestCase):
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
        company = Company.objects.create(name="Abcam", display_name="Abcam")
        target = Target.objects.create(gene_name="SNCA", protein_name="Syn")
        antibody = Antibody.objects.create(
            target=target, company=company, catalogue_number="ab138501")
        review_svc.stage(antibody=antibody, application_type="WB",
                         content=_png(), filename="SNCA_ab138501_WB.png",
                         recommended=True)
        self.consumer = APIConsumer.objects.create(
            name="Abcam", consumer_type="manufacturer", supplier_filter="Abcam")
        self.page = self._browser.new_page()

    def tearDown(self):
        self.page.close()

    def _connect(self):
        self.page.goto(f"{self.live_server_url}{reverse('portal')}"
                       f"?key={self.consumer.api_key}")
        self.page.wait_for_selector("#dashboard:not(.hidden)")

    # Both are `commissioning` by the tag's own bar (see
    # `pipeline/tests_browser_board.py`): they prove the tab worked when it was
    # built, and if either breaks the partner *sees* it — a blank tab or a
    # missing image — and no record is written wrong. They are also two of the
    # more expensive tests in the suite, because connecting drives the portal's
    # whole sign-in. The release test one file over is the opposite case and
    # stays on the push tier: it writes to the public website.
    @tag("commissioning")
    def test_the_tab_shows_your_unreleased_figure_and_its_image(self):
        self._connect()
        self.page.click('[data-tab="pipeline"]')
        self.page.wait_for_selector("#pipeline-content .ab-card")

        body = self.page.text_content("#pipeline-content")
        self.assertIn("ab138501", body)
        self.assertIn("SNCA", body)
        # Said once, at the top — the row is provisional and the tab must say so
        # before anybody reads a recommendation off it.
        self.assertIn("NOT published", body)

        # The thumbnail is fetched with the key and swapped in as a blob. A
        # naked <img src> would 401 here, and the card would render perfectly
        # around a broken image.
        self.page.wait_for_function(
            "() => { const i = document.querySelector('#pipeline-content img');"
            " return i && i.src.startsWith('blob:') && i.naturalWidth > 0; }")

    @tag("commissioning")
    def test_it_reports_where_your_genes_have_got_to(self):
        self._connect()
        self.page.click('[data-tab="pipeline"]')
        self.page.wait_for_selector("#pipeline-content table")
        table = self.page.text_content("#pipeline-content table")
        self.assertIn("SNCA", table)
        self.assertIn("awaiting release", table)
