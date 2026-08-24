"""The home page's supporters strip: its files exist, and it sits where it does
for a reason.

Two things are pinned, both of which fail silently everywhere else.

**The logo files.** Production serves static through a manifest storage that
hashes every name, so a logo deleted or renamed is not a missing image — it is a
**500 on the home page**, the site's front door. The test suite does not use that
storage, so a response test renders 200 over a file that is not there and says
nothing. Checking the paths on disk is what catches it before a deploy does.

**The order.** The strip is deliberately *above* ``gene-list``: below it, it sat
behind a card for every published gene, which on live is several thousand pixels,
and acknowledgement a funder asks to be visible was on the wrong side of the
page. That decision is one template move away from being undone by someone
tidying, and nothing would look broken afterwards.

What is NOT pinned here is which organisation appears, or under which of the
three labels — that is a question about who funded what, it changes when the
grants do, and a test asserting it would be a second place to keep that fact
right. The labels themselves mirror ``partners.html``'s own sections; the note
in ``home.html`` says to move a logo on both pages together.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

LOGO_DIR = Path(settings.BASE_DIR) / "core" / "static" / "core" / "supporters"


class SupportersStripTests(SimpleTestCase):
    databases = {"academy_db", "pipeline_db"}

    def setUp(self):
        self.html = self.client.get(reverse("home")).content.decode()

    def test_every_logo_the_strip_names_is_on_disk(self):
        named = set(re.findall(r'core/supporters/([\w.-]+\.png)', self.html))
        self.assertTrue(named, "the strip named no logos at all")
        for name in sorted(named):
            with self.subTest(logo=name):
                self.assertTrue(
                    (LOGO_DIR / name).is_file(),
                    f"home.html names core/supporters/{name} and the file is not "
                    f"there — under the hashed static storage production uses, "
                    f"that is a 500 on the home page, not a broken image")

    def test_the_strip_comes_before_the_gene_list(self):
        strip = self.html.find('class="oga-supporters"')
        genes = self.html.find('class="gene-list"')
        self.assertNotEqual(strip, -1, "the supporters strip is gone")
        self.assertNotEqual(genes, -1, "the gene list is gone")
        self.assertLess(strip, genes,
                        "the supporters strip belongs above the gene list — "
                        "below it nobody scrolls that far")

    def test_each_logo_carries_alt_text(self):
        tags = re.findall(r'<img[^>]*core/supporters/[^>]*>', self.html)
        self.assertTrue(tags, "the strip drew no logos at all")
        for tag in tags:
            with self.subTest(tag=tag[:70]):
                self.assertRegex(tag, r'alt="[^"]+"',
                                 "a logo with no alt text is a name a screen "
                                 "reader cannot read out")
