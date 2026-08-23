"""Workshop credential — institutional gate + claim→issue→verify chain.

Self-contained on credentials_db (no auth_user). Run with:

    python manage.py test credentials
"""
import json
import uuid

from django.test import TestCase, override_settings

from .institutional import is_free_provider, is_institutional_email
from .models import Certificate, PendingClaim

CLAIM_SETTINGS = dict(
    EMAIL_HOST_PASSWORD="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)


class InstitutionalEmailTests(TestCase):
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
    databases = "__all__"

    def test_titles_and_code(self):
        c = Certificate.objects.create(
            recipient_name="Jane R", institutional_email="jane@leicester.ac.uk",
            target_gene="SNCA")
        self.assertIsNotNone(c.verification_code)
        self.assertEqual(c.display_title, "Antibody Validation Planning — SNCA")
        self.assertEqual(c.recipient_display, "Jane R")
        self.assertEqual(c.kind, Certificate.WORKSHOP)


class VerifyRouteTests(TestCase):
    databases = "__all__"

    def test_verify_genuine_hides_email(self):
        c = Certificate.objects.create(
            recipient_name="Jane R", institutional_email="jane@leicester.ac.uk",
            target_gene="SNCA")
        r = self.client.get(f"/workshop/verify/{c.verification_code}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Certificate verified")
        self.assertNotContains(r, "jane@leicester.ac.uk")

    def test_verify_unknown_code(self):
        r = self.client.get(f"/workshop/verify/{uuid.uuid4()}/")
        self.assertContains(r, "Certificate not found")


@override_settings(**CLAIM_SETTINGS)
class ClaimTests(TestCase):
    databases = "__all__"

    def test_free_provider_rejected_no_claim(self):
        r = self.client.post("/workshop/claim/",
                             {"name": "Jane", "email": "jane@gmail.com"})
        self.assertContains(r, "personal email")
        self.assertFalse(PendingClaim.objects.exists())

    def test_workshop_only_claim(self):
        email = f"jane_{uuid.uuid4().hex[:6]}@leicester.ac.uk"
        plan = {"target_gene": "SNCA", "application": "WB",
                "pack_version": "0.2.0-draft"}
        r = self.client.post("/workshop/claim/", {
            "name": "Jane Researcher", "email": email, "gene": "SNCA",
            "rrid": "AB_123", "plan": json.dumps(plan)})
        self.assertEqual(r.status_code, 200)
        claim = PendingClaim.objects.get(institutional_email=email)
        self.assertIsNone(claim.verified_at)

        r = self.client.get(f"/workshop/claim/verify/{claim.token}/")
        self.assertContains(r, "certificate")
        certs = list(claim.certificates.all())
        self.assertEqual(len(certs), 1)                 # workshop only, no capstone
        self.assertEqual(certs[0].kind, Certificate.WORKSHOP)
        self.assertEqual(certs[0].plan, plan)

        # idempotent
        self.client.get(f"/workshop/claim/verify/{claim.token}/")
        self.assertEqual(
            Certificate.objects.filter(institutional_email=email).count(), 1)

        # per-cert PDF by code, no login
        r = self.client.get(f"/workshop/cert/{certs[0].verification_code}/pdf/")
        self.assertEqual(r.status_code, 200)
        body = b"".join(r.streaming_content) if r.streaming else r.content
        self.assertEqual(body[:4], b"%PDF")

    def test_all_modules_plus_workshop_earns_capstone(self):
        email = f"pat_{uuid.uuid4().hex[:6]}@crick.ac.uk"
        plan = {"target_gene": "MAPT", "pack_version": "0.2.0-draft"}
        r = self.client.post("/workshop/claim/", {
            "name": "Pat Q", "email": email,
            "modules": ["framework", "controls", "acquiring"],
            "plan": json.dumps(plan)})
        self.assertEqual(r.status_code, 200)
        claim = PendingClaim.objects.get(institutional_email=email)
        self.client.get(f"/workshop/claim/verify/{claim.token}/")
        certs = list(claim.certificates.all())
        kinds = sorted(c.kind for c in certs)
        # 3 modules + workshop + capstone = 5
        self.assertEqual(len(certs), 5)
        self.assertEqual(kinds.count(Certificate.MODULE), 3)
        self.assertEqual(kinds.count(Certificate.WORKSHOP), 1)
        self.assertEqual(kinds.count(Certificate.CAPSTONE), 1)
        cap = claim.certificates.get(kind=Certificate.CAPSTONE)
        self.assertIn("Full Certification", cap.display_title)

    def test_partial_modules_no_capstone(self):
        email = f"sam_{uuid.uuid4().hex[:6]}@mit.edu"
        r = self.client.post("/workshop/claim/", {
            "name": "Sam", "email": email, "modules": ["framework", "controls"]})
        claim = PendingClaim.objects.get(institutional_email=email)
        self.client.get(f"/workshop/claim/verify/{claim.token}/")
        certs = list(claim.certificates.all())
        self.assertEqual(len(certs), 2)                 # two module certs, no capstone
        self.assertFalse(any(c.kind == Certificate.CAPSTONE for c in certs))

    def test_claim_needs_something_to_issue(self):
        r = self.client.post("/workshop/claim/",
                             {"name": "X", "email": "x@leicester.ac.uk"})
        self.assertContains(r, "at least one module")
        self.assertFalse(PendingClaim.objects.exists())

    def test_bad_token(self):
        r = self.client.get(f"/workshop/claim/verify/{uuid.uuid4()}/")
        self.assertContains(r, "not recognised")
