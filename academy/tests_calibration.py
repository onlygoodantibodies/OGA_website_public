"""The western blot calibration study (academy/calibration_views.py)."""
from collections import Counter
from unittest import skipIf

from django.test import TestCase
from django.urls import reverse

from academy import calibration as C
from academy.models import CalibrationRater, CalibrationRating
from OGA_website.public_snapshot import REASON, withheld

SIGNUP = {'name': 'Ada Rater', 'email': 'ada@le.ac.uk', 'role': 'postdoc',
          'wb_experience': 'regular', 'wb_years': '4', 'ko_experience': 'yes',
          'consent': 'yes'}


# The study's answer key is left out of the public snapshot; every page counts it.
@skipIf(withheld(C.MANIFEST), REASON)
class CalibrationStudyTests(TestCase):
    databases = {'academy_db'}

    def test_a_personal_email_is_refused_by_name(self):
        resp = self.client.post(reverse('academy:calibrate'),
                                dict(SIGNUP, email='ada@gmail.com'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'gmail.com')
        self.assertFalse(CalibrationRater.objects.exists())

    def test_a_rating_is_saved_for_the_blot_shown_and_the_next_one_follows(self):
        self.client.post(reverse('academy:calibrate'), SIGNUP)
        rater = CalibrationRater.objects.get(email='ada@le.ac.uk')
        page = self.client.get(reverse('academy:calibrate_rate'))
        first = page.context['item_id']
        self.assertEqual(first, C.order_for(rater.pk)[0])
        # The expected answer and how the blot was made never reach the page.
        it = C.item(first)
        html = page.content.decode()
        self.assertNotIn(it['params'].get('recipe') or it['params'].get('catalogue'), html)
        self.client.post(reverse('academy:calibrate_rate'),
                         {'item_id': first, 'answer': 'other', 'seconds': '12'})
        saved = CalibrationRating.objects.get(rater=rater)
        self.assertEqual((saved.item_id, saved.answer, saved.seconds), (first, 'other', 12))
        nxt = self.client.get(reverse('academy:calibrate_rate')).context['item_id']
        self.assertNotEqual(nxt, first)

    def test_the_results_are_not_open_to_raters(self):
        self.client.post(reverse('academy:calibrate'), SIGNUP)
        resp = self.client.get(reverse('academy:calibrate_results'))
        self.assertEqual(resp.status_code, 302)

    def test_kappa_is_one_for_perfect_agreement_and_counts_no_unsure(self):
        table = {'a': Counter(main=3), 'b': Counter(none=3), 'c': Counter(other=3)}
        self.assertAlmostEqual(C.fleiss_kappa(table), 1.0)

    def test_the_results_page_draws_for_a_superuser(self):
        from django.contrib.auth.models import User
        self.client.post(reverse('academy:calibrate'), SIGNUP)
        rater = CalibrationRater.objects.get()
        for it, ans in zip(C.items()[:5], ('main', 'other', 'none', 'unsure', 'main')):
            CalibrationRating.objects.create(rater=rater, item_id=it['id'], answer=ans)
        boss = User.objects.db_manager('academy_db').create_superuser('boss', 'b@x.org', 'pw')
        self.client.force_login(boss)
        resp = self.client.get(reverse('academy:calibrate_results'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ada Rater')
        csv_resp = self.client.get(reverse('academy:calibrate_results_csv'))
        self.assertEqual(len(csv_resp.content.decode().strip().splitlines()), 6)
