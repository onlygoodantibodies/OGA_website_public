"""The top bar dropped two entries, so the Tools hub is now the only route.

The Selection Tool and Connect your AI came off the bar (they were making it ten
items wide and wrapping it onto two lines on a phone). Both are cards on the
Tools hub, badged as early releases — which is fine right up until somebody
tidies the hub and one of them goes, at which point the tool is reachable from
no chrome anywhere and nothing fails. Same shape as the pipeline's "Browse holds
everything the hub holds": what a nav stops carrying, some other page has to.
"""
from django.test import TestCase
from django.urls import reverse


class TheToolsHubCarriesWhatTheBarDroppedTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def _bar(self, html):
        return html.split('<ul class="menu">')[1].split("</ul>")[0]

    def test_the_two_early_releases_are_not_in_the_top_bar(self):
        bar = self._bar(self.client.get(reverse("tools_hub")).content.decode())
        self.assertNotIn(reverse("selector:tool"), bar)
        self.assertNotIn(reverse("connect_your_ai"), bar)
        self.assertIn(reverse("tools_hub"), bar)

    def test_the_tools_hub_links_to_both_and_says_they_are_early_releases(self):
        html = self.client.get(reverse("tools_hub")).content.decode()
        self.assertIn(reverse("selector:tool"), html)
        self.assertIn(reverse("connect_your_ai"), html)
        # One badge per card. A card losing its badge is a claim about maturity
        # quietly withdrawn, which is the half a link check cannot see.
        self.assertEqual(html.count("Early release"), 2)
