"""The Manuscript Validation Checker is gone, and its address is not.

Retired 21 Aug 2026. Over the 30 days of logs Render keeps, the page was fetched
about thirty times — almost all of it crawlers — and the submit and RRID-lookup
endpoints were **never called once**, so nobody completed a record in that
window. The browser extension and the MCP connector answer the same question
from a paper the reader already has open. The evidence is in `DECISIONS.md`.

Two things are pinned, and only the second is obvious.

The address keeps answering, because one of the two human arrivals came from
Google — it is in the index, and an indexed URL that starts 404ing is silent to
everybody except the person who followed the link. A later tidy-up that deletes
the "dead" route would look harmless in review.

And the route deliberately has **no url name**. That is what makes a link to the
retired tool fail loudly instead of quietly redirecting: with no name there is
nothing for `{% url %}` to resolve. Re-adding the name "to be safe" would undo
the protection without breaking anything visible, which is exactly the kind of
change nothing else here would catch.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse


class RetiredManuscriptValidationCheckerTests(SimpleTestCase):

    def test_the_address_still_answers_and_sends_you_to_the_tools_hub(self):
        response = self.client.get('/tools/validation-recorder/')
        self.assertEqual(response.status_code, 301,
                         "the Checker's URL is in Google's index — it must keep "
                         "redirecting rather than 404")
        self.assertEqual(response.headers['Location'], reverse('tools_hub'),
                         "the redirect must land where the working controls are: "
                         "the Tools hub carries both the extension and the MCP")

    def test_the_url_has_no_name_so_nothing_can_link_to_it(self):
        with self.assertRaises(NoReverseMatch):
            reverse('validation_recorder')

    def test_the_endpoints_that_took_submissions_are_withdrawn(self):
        for path in ('/tools/validation-recorder/submit/',
                     '/tools/validation-recorder/rrid-lookup/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.post('/tools/validation-recorder/submit/',
                             {'author': 'x'}).status_code, 404)

    def test_the_validation_record_is_a_different_tool_and_survives(self):
        """Retiring the Checker must not take the self-report tool with it — the
        funders and publishers roadmap pages both promise it."""
        self.assertEqual(reverse('validation_record'), '/tools/validation-record/')
