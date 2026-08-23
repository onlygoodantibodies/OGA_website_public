"""Every POST the board makes must survive CSRF enforcement.

The board's JavaScript originally read the token from ``document.cookie``. That
can never work here: ``CSRF_COOKIE_HTTPONLY`` is on, so the cookie is invisible
to JavaScript and every POST — upload preview, upload commit, and every inline
cell edit — came back 403 on live.

It passed every check I ran because Django's test client does not enforce CSRF
unless asked, and the browser walkthrough only ever read pages. So these use
``enforce_csrf_checks=True`` and drive the POSTs the way the page does: with the
token the template rendered into it.
"""
from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.test import Client, TestCase

from pipeline.models import Member, Site

DB = "pipeline_db"


class BoardCsrfTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        site = Site.objects.create(name="McGill", short_code="MCG")
        for alias in ("academy_db", DB):
            u = User(username="carl")
            u.set_password("pw")
            u.save(using=alias)
        pu = User.objects.using(DB).get(username="carl")
        Member.objects.create(user_id=pu.pk, site_id=site.pk, role="admin",
                              is_active=True)
        self.client = Client(enforce_csrf_checks=True)
        self.assertTrue(self.client.login(username="carl", password="pw"))

    def _token_from_page(self):
        """The token exactly as the page hands it to fetch()."""
        html = self.client.get("/pipeline/targets/board/").content.decode()
        match = re.search(r'const csrf = "([^"]+)"', html)
        self.assertIsNotNone(match, "the board no longer renders a CSRF token")
        return match.group(1)

    def test_board_renders_a_usable_token(self):
        token = self._token_from_page()
        self.assertTrue(token and token != "NOTPROVIDED", token)

    def test_inline_edit_succeeds_with_the_page_token(self):
        from pipeline.models import Target
        target = Target.objects.create(protein_name="Synuclein", gene_name="SNCA")
        response = self.client.post(
            "/pipeline/targets/board/patch/",
            {"target_id": target.pk, "field": "essential_gene", "value": "NO"},
            HTTP_X_CSRFTOKEN=self._token_from_page())
        self.assertEqual(response.status_code, 200, response.content[:200])
        target.refresh_from_db()
        self.assertEqual(target.essential_gene, "NO")

    def test_upload_preview_succeeds_with_the_page_token(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from pipeline.services.tests.test_target_board import _carl_workbook

        data = _carl_workbook([
            ["NIH", "AMP-AD", "Funding available", "2020", "", "McGill", "",
             "SNCA", "", "", "", "", "", "", "", "", "", "", "", ""]])
        response = self.client.post(
            "/pipeline/targets/board/upload/preview/",
            {"file": SimpleUploadedFile("carl.xlsx", data), "default_site": "McGill"},
            HTTP_X_CSRFTOKEN=self._token_from_page())
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertTrue(response.json()["ok"])

    def test_a_post_without_the_token_is_still_rejected(self):
        """The protection must remain real, not be worked around."""
        response = self.client.post("/pipeline/targets/board/patch/",
                                    {"target_id": 1, "field": "essential_gene",
                                     "value": "NO"})
        self.assertEqual(response.status_code, 403)
