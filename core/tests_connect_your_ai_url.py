"""`/tools/ai-tutor/` was renamed to `/tools/connect-your-ai/` on 22 Aug 2026.

The page is the MCP connector — "connect your AI to the OGA database" — and was
never the Academy's conversational tutor at `academy:assistant`. Its own
`<title>` and `<h1>` say so, and every link on the site called it *Connect your
AI*: the address was the last place still carrying the wrong name.

Two things are pinned here, and only the first is obvious.

The old address must keep answering. It is in Google's index, it is the handoff
link inside every already-installed copy of the browser extension (which nobody
can be made to update), and it is in whatever anybody bookmarked. An indexed URL
that starts 404ing is silent to everyone except the person who followed it.

And the old route deliberately has **no url name**, so a template cannot link to
the old address by accident — one page, one name. Re-adding `ai_tutor` "so old
code keeps working" would undo that without breaking anything visible.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse


class ConnectYourAiUrlRenameTests(SimpleTestCase):

    def test_the_page_is_at_its_new_address(self):
        self.assertEqual(reverse('connect_your_ai'), '/tools/connect-your-ai/')

    def test_the_old_address_still_answers_and_sends_you_to_the_new_one(self):
        response = self.client.get('/tools/ai-tutor/')
        self.assertEqual(response.status_code, 301,
                         "the old URL is indexed and is baked into installed "
                         "copies of the browser extension — it must redirect, "
                         "not 404")
        self.assertEqual(response.headers['Location'],
                         reverse('connect_your_ai'))

    def test_the_old_url_has_no_name_so_nothing_can_link_to_it(self):
        with self.assertRaises(NoReverseMatch):
            reverse('ai_tutor')
