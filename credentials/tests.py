"""The institutional-email gate, and the workshop credential's model.

The claim and verify pages were retired on 14 Oct 2026 (none was ever issued),
so their tests went with them. ``institutional.py`` is still read by the key
requests, the manufacturer contacts and ``core``'s models and views.
"""
from django.test import TestCase

from .institutional import is_free_provider, is_institutional_email
from .models import Certificate


class InstitutionalEmailTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    def test_free_providers_rejected(self):
        for bad in ("a@gmail.com", "a@outlook.com", "a@yahoo.co.uk", "a@icloud.com"):
            self.assertTrue(is_free_provider(bad))
            self.assertFalse(is_institutional_email(bad))

    def test_institutional_accepted(self):
        for good in ("a@leicester.ac.uk", "a@mit.edu", "a@crick.ac.uk"):
            self.assertFalse(is_free_provider(good))
            self.assertTrue(is_institutional_email(good))

    def test_malformed_rejected(self):
        self.assertFalse(is_institutional_email("notanemail"))
        self.assertFalse(is_institutional_email(""))


class CertificateModelTests(TestCase):
    databases = {"academy_db", "pipeline_db"}

    def test_titles_and_code(self):
        c = Certificate.objects.create(
            recipient_name="Jane R", institutional_email="jane@leicester.ac.uk",
            target_gene="SNCA")
        self.assertIsNotNone(c.verification_code)
        self.assertEqual(c.display_title, "Antibody Validation Planning — SNCA")
        self.assertEqual(c.recipient_display, "Jane R")
        self.assertEqual(c.kind, Certificate.WORKSHOP)
