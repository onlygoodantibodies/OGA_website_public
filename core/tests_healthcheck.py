"""The health check answers, under the conditions Render actually probes it in.

The point of this endpoint is to close the ~40s window in which a deploy serves
502s (see OGA_website/health.py). The failure it must not have is the opposite
one: a check that never passes hangs the deploy instead of completing it, and
nothing on any screen says so — the site simply stays on the old code.

So the assertions are about the ways a perfectly good Django app answers
something other than 200 to an internal probe:

* a Host header that is not in ALLOWED_HOSTS, which is a fixed list here with no
  wildcard, and which Django answers 400 to;
* the bare path with no trailing slash, which APPEND_SLASH would answer 301 to;
* and needing a database, which would make a momentary pipeline_db wobble read
  as "this instance is broken" and roll a good deploy back.

The database one is asserted by running with no databases declared at all: this
is a SimpleTestCase, so any query raises rather than quietly succeeding against
a test database that a real booting instance would not have.
"""
from __future__ import annotations

from django.test import SimpleTestCase


class HealthCheckTests(SimpleTestCase):
    """No `databases` attribute on purpose — a query here fails the test."""

    def test_it_answers_ok(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"ok\n")

    def test_a_host_outside_allowed_hosts_still_gets_200(self):
        """Render probes over its own network. If that Host header is not in
        ALLOWED_HOSTS, ordinary Django answers 400 and the deploy never
        completes — which is worse than the 502 this exists to remove."""
        r = self.client.get("/healthz", HTTP_HOST="10.0.0.7")
        self.assertEqual(r.status_code, 200,
                         "the health check must not depend on ALLOWED_HOSTS")

    def test_the_host_check_is_otherwise_still_enforced(self):
        """The exemption is for this one path and must not widen: a bad Host on
        any other URL is still refused."""
        r = self.client.get("/", HTTP_HOST="10.0.0.7")
        self.assertEqual(r.status_code, 400)

    def test_both_spellings_answer_without_a_redirect(self):
        for path in ("/healthz", "/healthz/"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200,
                                 "a 301 fails a health check as surely as an "
                                 "error does")

    def test_it_is_never_cached(self):
        """Cloudflare is in front of this origin; a cached 'ok' is not a health
        check."""
        self.assertEqual(self.client.get("/healthz")["Cache-Control"],
                         "no-store")
