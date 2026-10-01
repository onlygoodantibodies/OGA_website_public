"""Manufacturer emails page: the three things that would go wrong silently.

A resend reaching people nobody ticked; a resend moving the weekly-update
clock (so the next update is late, or reports as "new" what it already
sent); and the page opening to somebody who is not a superuser — it emails
named people at partner companies.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from core import manufacturer_contacts as MC
from core.models import SupplierContact, SupplierMailing
from core.tests_supplier_mailing import _Fixture
from pipeline.models import Member, Site

DB = "pipeline_db"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   SUPPLIER_MAILING_CC=["owner@le.ac.uk", "carl@mcgill.ca"])
@mock.patch("core.manufacturer_contacts.configuration_gaps", return_value=[])
class ResendTests(_Fixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = SupplierContact.objects.create(
            consumer=cls.abcam, email="colleague@abcam.com", name="Sam Colleague")

    def test_goes_only_to_the_ticked_person_and_oga(self, _gaps):
        MC.resend(self.abcam.pk, [self.other.pk], ["colleague@abcam.com"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["colleague@abcam.com"])
        self.assertEqual(mail.outbox[0].cc, ["owner@le.ac.uk", "carl@mcgill.ca"])
        self.assertIn("Dear Sam,", mail.outbox[0].body)
        self.assertTrue(mail.outbox[0].attachments)

    def test_does_not_move_the_weekly_update_cursor(self, _gaps):
        launch = SupplierMailing.objects.create(
            consumer=self.abcam, kind="launch", subject="s", recipients="r",
            fingerprint={"results": {}, "pending": []},
            sent_at=timezone.now() - timedelta(days=8))
        MC.resend(self.abcam.pk, [self.other.pk], ["colleague@abcam.com"])
        self.assertEqual(SupplierMailing.objects.filter(kind="resend").count(), 1)
        self.assertEqual(SupplierMailing.cursor_for(self.abcam), launch,
                         "A resend became the cursor, delaying the next update.")

    def test_a_changed_selection_sends_nothing(self, _gaps):
        with self.assertRaises(MC.Refused):
            MC.resend(self.abcam.pk, [self.other.pk], ["contact@abcam.com"])
        self.assertEqual(mail.outbox, [])
        self.assertFalse(SupplierMailing.objects.exists())

    def test_a_stopped_contact_is_refused_by_name(self, _gaps):
        SupplierContact.objects.filter(pk=self.other.pk).update(is_active=False)
        with self.assertRaisesMessage(MC.Refused, "colleague@abcam.com is stopped"):
            MC.resend(self.abcam.pk, [self.other.pk], ["colleague@abcam.com"])
        self.assertEqual(mail.outbox, [])


class ContactTests(_Fixture):

    def test_a_duplicate_address_is_refused_whatever_its_case(self):
        with self.assertRaisesMessage(MC.Refused, "already on Abcam's list"):
            MC.add_contact(self.abcam.pk, "", "Contact@Abcam.com", "to")

    def test_an_address_off_the_key_domains_is_added_and_said(self):
        text = MC.add_contact(self.abcam.pk, "Pat", "pat@abcam.co.jp", "cc")
        self.assertIn("cannot request the portal key", text)
        self.assertTrue(SupplierContact.objects.filter(email="pat@abcam.co.jp",
                                                       role="cc").exists())

    def test_a_personal_mail_domain_is_refused(self):
        with self.assertRaises(MC.Refused):
            MC.set_domains(self.abcam.pk, "abcam.com, gmail.com")
        self.abcam.refresh_from_db()
        self.assertEqual(self.abcam.email_domains, "abcam.com")


class PageTests(_Fixture):

    def _client(self, username, superuser):
        site = Site.objects.using(DB).get_or_create(name="Leicester", short_code="LEI")[0]
        for alias in ("academy_db", DB):
            u = User(username=username, is_superuser=superuser, is_staff=superuser)
            u.set_password("pw")
            u.save(using=alias)
        Member.objects.create(user_id=User.objects.using(DB).get(username=username).pk,
                              site_id=site.pk, role="member", is_active=True)
        client = Client()
        self.assertTrue(client.login(username=username, password="pw"))
        return client

    def test_a_member_who_is_not_a_superuser_cannot_open_or_post(self):
        client = self._client("bench", False)
        self.assertNotEqual(client.get(reverse("pipeline:manufacturer_emails")).status_code, 200)
        client.post(reverse("pipeline:manufacturer_emails_action"),
                    {"action": "add", "consumer": self.abcam.pk,
                     "email": "x@abcam.com", "role": "to"})
        self.assertFalse(SupplierContact.objects.filter(email="x@abcam.com").exists())

    def test_adding_draws_the_receipt_inside_the_organisation(self):
        client = self._client("root", True)
        response = client.post(reverse("pipeline:manufacturer_emails_action"),
                               {"action": "add", "consumer": self.abcam.pk, "name": "Katie",
                                "email": "katie@abcam.com", "role": "to"}, follow=True)
        self.assertContains(response, 'id="receipt"')
        self.assertContains(response, "Added Katie to Abcam")
        self.assertContains(response, "katie@abcam.com")
