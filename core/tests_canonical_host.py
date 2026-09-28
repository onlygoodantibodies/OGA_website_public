"""The site has one public hostname, and two things must survive saying so.

Context, 19 Sep 2026: ``oga-website.onrender.com`` — this service's own Render
hostname, which the apex is a CNAME to — turned up in Google's index. It is the
one door neither Cloudflare rule reaches, because both are rules on our zone and
that hostname is in Render's. ``OGA_website/canonical_host.py`` now redirects it.

Only the failures that would be **silent or catastrophic** are pinned here.

``/healthz`` is the catastrophic one. Render promotes a new instance when that
path answers 200; answered with a 301 it never promotes, the deploy hangs, and
the site stays on the old code with nothing on any screen saying why. Today the
health check middleware runs above this one so the probe never reaches it — a
test that asks the *endpoint* rather than reading the middleware order is what
keeps that true after somebody reorders the list in a merge.

The POST case is the silent one. A 301 turns a POST into a GET and drops the
body, so a redirect applied to the wrong method loses a write and returns 200
while doing it.

The rest are one line each and cost about 5 ms.
"""
from django.test import TestCase

ORIGIN_HOSTS = ('oga-website.onrender.com', 'only-good-antibodies.onrender.com')


class TheOriginHostnameRedirectsTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    def test_a_public_page_is_sent_to_the_canonical_hostname(self):
        page = self.client.get(
            '/privacy-policy/', headers={'host': 'oga-website.onrender.com'})
        self.assertEqual(page.status_code, 301)
        self.assertEqual(
            page['Location'],
            'https://onlygoodantibodies.co.uk/privacy-policy/')

    def test_the_path_and_the_query_string_both_survive(self):
        """A redirect that drops `?ab=` lands the reader on the gene page with
        the row they were sent to hidden — `out_of_market` rows are exempt only
        when named by that parameter. Nothing would say the link had been
        altered."""
        page = self.client.get(
            '/antibodies/APOE/?ab=12345',
            headers={'host': 'oga-website.onrender.com'})
        self.assertEqual(page.status_code, 301)
        self.assertEqual(
            page['Location'],
            'https://onlygoodantibodies.co.uk/antibodies/APOE/?ab=12345')

    def test_both_render_hostnames_are_covered(self):
        for host in ORIGIN_HOSTS:
            with self.subTest(host=host):
                page = self.client.get('/privacy-policy/', headers={'host': host})
                self.assertEqual(page.status_code, 301)

    def test_the_canonical_hostname_is_not_redirected(self):
        """The loop check. A rule written the other way round — redirect
        anything that is not on a list — sends the live site to itself."""
        page = self.client.get(
            '/privacy-policy/', headers={'host': 'onlygoodantibodies.co.uk'})
        self.assertEqual(page.status_code, 200)


class WhatMustStillAnswerOnTheOriginTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    def test_the_health_check_still_answers_two_hundred(self):
        """The whole deploy depends on this one. Asked of the endpoint, not of
        the middleware list, so reordering the list cannot pass it."""
        for path in ('/healthz', '/healthz/'):
            for host in ORIGIN_HOSTS:
                with self.subTest(path=path, host=host):
                    probe = self.client.get(path, headers={'host': host})
                    self.assertEqual(probe.status_code, 200)
                    self.assertEqual(probe.content, b'ok\n')

    def test_a_post_is_never_redirected(self):
        """The MCP connector posts its usage to the origin hostname because the
        same request to the apex is bounced 403 at the edge. A 301 here would
        turn that POST into a GET, drop the body, and report success."""
        sent = self.client.post(
            '/internal/mcp-usage/', data='{}', content_type='application/json',
            headers={'host': 'oga-website.onrender.com'})
        self.assertNotEqual(sent.status_code, 301)

    def test_the_internal_prefix_is_exempt_for_reads_too(self):
        """Belt and braces: the endpoint is POST-only today, so the method test
        above already spares it. This is what keeps a future GET under
        `/internal/` from being redirected in silence."""
        read = self.client.get(
            '/internal/mcp-usage/', headers={'host': 'oga-website.onrender.com'})
        self.assertNotEqual(read.status_code, 301)


class TheDotComFoldsIntoTheDotCoUkTests(TestCase):
    """Owner's decision, 27 Sep 2026: the .com is the same site, so it
    redirects rather than serving a second copy of every page."""
    databases = {"academy_db", "pipeline_db"}

    def test_a_dot_com_page_lands_on_the_same_page_at_the_dot_co_uk(self):
        for host in ('onlygoodantibodies.com', 'www.onlygoodantibodies.com'):
            with self.subTest(host=host):
                page = self.client.get(
                    '/antibodies/APOE/?ab=12345', headers={'host': host})
                self.assertEqual(page.status_code, 301)
                self.assertEqual(
                    page['Location'],
                    'https://onlygoodantibodies.co.uk/antibodies/APOE/?ab=12345')

    def test_a_post_to_the_dot_com_is_not_redirected(self):
        sent = self.client.post(
            '/internal/mcp-usage/', data='{}', content_type='application/json',
            headers={'host': 'onlygoodantibodies.com'})
        self.assertNotEqual(sent.status_code, 301)
