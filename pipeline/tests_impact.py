"""The impact page: who may see it, and whether its numbers mean what they say.

Three things are worth pinning and the rest is presentation.

**Who may see it.** The page aggregates people who are not pipeline members —
academy learners, workshop attendees, scientists who built a plan — and shows
which organisations hold API keys and how hard each is pulling, which is
commercially sensitive in the way `/gene-detail/` is. It is superuser-only on
purpose, and a gate that quietly opened would be invisible.

**Whether a number means what its label says.** A reporting page is where a
figure gets copied into a grant, so the two places the count could be quietly
generous are pinned: a *started* lesson must not be counted as completed, and
the keyed/keyless split must add up to the total rather than double-count.

**Whether one broken app takes the page down.** It reads four apps it does not
own. A section that throws is drawn as a named gap; the alternative is a
reporting page that either 500s or, worse, silently omits a section and
understates the total it exists to give.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pipeline.models import Member, Site

DB = "pipeline_db"


class ImpactPageTests(TestCase):
    databases = {"default", "pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester",
                                                  short_code="LEI")

    def _user(self, username, superuser):
        for alias in ("academy_db", DB):
            u = User(username=username, is_superuser=superuser,
                     is_staff=superuser)
            u.set_password("pw")
            u.save(using=alias)
        pipeline_user = User.objects.using(DB).get(username=username)
        Member.objects.create(user_id=pipeline_user.pk, site_id=self.site.pk,
                              role="admin" if superuser else "member",
                              is_active=True)
        client = Client()
        self.assertTrue(client.login(username=username, password="pw"))
        return client

    # --- who may see it ------------------------------------------------

    def test_a_superuser_can_open_it(self):
        response = self._user("root", True).get(reverse("pipeline:impact"))
        self.assertEqual(response.status_code, 200)

    def test_an_ordinary_member_cannot(self):
        """The gate is the decision, and it would fail silently if it opened.

        A bench member needs none of this to do bench work, and the page holds
        other people's engagement plus which partners are pulling how hard.
        """
        response = self._user("bench", False).get(reverse("pipeline:impact"))
        self.assertNotEqual(
            response.status_code, 200,
            "A non-superuser opened the impact page.")

    # --- do the numbers mean what they say -----------------------------

    def test_a_started_lesson_is_not_a_completed_one(self):
        """The figure a grant would quote, and the easiest one to inflate."""
        from academy.models import Lesson, LessonProgress
        from pipeline.services import impact

        learner = User.objects.using("academy_db").create(username="learner")
        lesson = Lesson.objects.create(title="Controls", slug="controls",
                                       order=1)
        LessonProgress.objects.create(user=learner, lesson=lesson,
                                      completed=False)

        numbers = impact.academy()
        self.assertEqual(numbers['lessons_completed'], 0)
        self.assertEqual(numbers['learners'], 0)
        # ...and the person still shows as having started, so the page is not
        # simply blind to them.
        self.assertEqual(numbers['learners_started'], 1)

    def test_the_keyed_and_keyless_split_adds_up(self):
        """Two numbers beside a total that do not sum to it is the shape of a
        reporting page nobody trusts again."""
        from core.models import APIConsumer, ApiUsageDay
        from django.utils import timezone

        from pipeline.services import impact

        consumer = APIConsumer.objects.create(name="Abcam",
                                              consumer_type="manufacturer")
        today = timezone.now().date()
        ApiUsageDay.objects.create(consumer=consumer, date=today,
                                   endpoint="genes_feed", count=7)
        ApiUsageDay.objects.create(consumer=None, date=today,
                                   endpoint="genes_feed", count=5)

        n = impact.api_usage()
        self.assertEqual(n['total_requests'], 12)
        self.assertEqual(n['keyless_requests'], 5)
        self.assertEqual(n['keyed_requests'], 7)
        self.assertEqual(n['keyed_requests'] + n['keyless_requests'],
                         n['total_requests'])

    def test_an_unused_key_is_not_counted_as_an_active_organisation(self):
        """A partnership set up and not taken up is a different fact from a
        quiet month, and folding it in overstates reach."""
        from core.models import APIConsumer

        from pipeline.services import impact

        APIConsumer.objects.create(name="Never called",
                                   consumer_type="manufacturer")
        n = impact.api_usage()
        self.assertEqual(n['organisations'], 1)
        self.assertEqual(n['organisations_active'], 0)

    # --- portal sign-ins ------------------------------------------------

    def test_a_portal_sign_in_is_the_endpoint_the_portal_actually_calls(self):
        """The constant and the route are two spellings of one fact.

        Nothing raises if they drift: the section would report zero sign-ins
        for ever, which reads as a portal nobody opens rather than as a filter
        matching nothing.
        """
        from django.urls import resolve, reverse as url_reverse

        from pipeline.services.impact import PORTAL_CONNECT_ENDPOINT

        match = resolve(url_reverse('api:api_status'))
        self.assertEqual(match.url_name, PORTAL_CONNECT_ENDPOINT)

    def test_it_counts_days_apart_from_connects(self):
        """A reload is another connect and not another visit, so the two are
        drawn as different numbers rather than one standing in for the other."""
        from datetime import timedelta

        from core.models import APIConsumer, ApiUsageDay
        from django.utils import timezone

        from pipeline.services import impact

        consumer = APIConsumer.objects.create(name="Abcam",
                                              consumer_type="manufacturer")
        today = timezone.now().date()
        ApiUsageDay.objects.create(consumer=consumer, date=today,
                                   endpoint=impact.PORTAL_CONNECT_ENDPOINT,
                                   count=9)
        ApiUsageDay.objects.create(consumer=consumer,
                                   date=today - timedelta(days=1),
                                   endpoint=impact.PORTAL_CONNECT_ENDPOINT,
                                   count=3)
        # Another endpoint entirely: pulling the feed is not signing in.
        ApiUsageDay.objects.create(consumer=consumer, date=today,
                                   endpoint="genes_feed", count=400)

        n = impact.portal_sessions()
        self.assertEqual(n['connects'], 12)
        self.assertEqual(n['days'], 2)
        self.assertEqual(n['organisations'], 1)
        row, = n['by_consumer']
        self.assertEqual(row['days'], 2)
        self.assertEqual(row['connects'], 12)
        self.assertEqual(row['last_seen'], today)
        self.assertEqual(row['first_seen'], today - timedelta(days=1))

    def test_a_script_that_never_signs_in_is_not_a_portal_organisation(self):
        """The whole reason this is drawn apart from the endpoint list: a
        nightly sync is not somebody opening the portal."""
        from core.models import APIConsumer, ApiUsageDay
        from django.utils import timezone

        from pipeline.services import impact

        consumer = APIConsumer.objects.create(name="Registry",
                                              consumer_type="rrid")
        today = timezone.now().date()
        ApiUsageDay.objects.create(consumer=consumer, date=today,
                                   endpoint="antibodies_feed", count=50)

        n = impact.portal_sessions()
        self.assertEqual(n['organisations'], 0)
        self.assertEqual(n['connects'], 0)
        self.assertEqual(n['by_consumer'], [])
        # …and the API section still knows about them, which is the difference.
        self.assertEqual(impact.api_usage()['organisations_active'], 1)

    def test_a_refused_sign_in_is_counted_and_never_attributed(self):
        """A key that no longer works is a support question. It is also
        unattributable — the row is keyed on a caller the request failed to
        identify — so it must be a total and never a name."""
        from core.models import ApiUsageDay
        from django.utils import timezone

        from pipeline.services import impact

        ApiUsageDay.objects.create(consumer=None, date=timezone.now().date(),
                                   endpoint=impact.PORTAL_CONNECT_ENDPOINT,
                                   count=4)

        n = impact.portal_sessions()
        self.assertEqual(n['refused'], 4)
        self.assertEqual(n['connects'], 0, "A refused attempt is not a sign-in.")
        self.assertEqual(n['organisations'], 0)
        self.assertEqual(n['by_consumer'], [])

    def test_the_section_is_drawn(self):
        """A figure computed and then not drawn is the same bug as one that is
        wrong — and this section is the whole request."""
        from core.models import APIConsumer, ApiUsageDay
        from django.utils import timezone

        from pipeline.services import impact

        consumer = APIConsumer.objects.create(name="Proteintech",
                                              consumer_type="manufacturer")
        ApiUsageDay.objects.create(consumer=consumer,
                                   date=timezone.now().date(),
                                   endpoint=impact.PORTAL_CONNECT_ENDPOINT,
                                   count=2)

        response = self._user("root", True).get(reverse("pipeline:impact"))
        self.assertContains(response, "Portal sign-ins")
        self.assertContains(response, "Proteintech")

    def test_its_cost_does_not_grow_with_the_number_of_days(self):
        """The last-API-call column is a join done in Python, which is exactly
        where a per-row query hides. N+1 is how these pages die, and it dies on
        the real data while looking fine on a handful of dev rows."""
        from datetime import timedelta

        from core.models import APIConsumer, ApiUsageDay
        from django.utils import timezone

        from pipeline.services import impact

        today = timezone.now().date()
        consumers = [APIConsumer.objects.create(name=f"Org {i}",
                                                consumer_type="manufacturer")
                     for i in range(3)]
        for consumer in consumers:
            ApiUsageDay.objects.create(
                consumer=consumer, date=today,
                endpoint=impact.PORTAL_CONNECT_ENDPOINT, count=1)
        # Pinned as "does not grow", never as a fixed number: the figure a page
        # costs is allowed to change, and what must not is its shape.
        with CaptureQueriesContext(connections["academy_db"]) as few:
            impact.portal_sessions()

        for consumer in consumers:
            for day in range(1, 15):
                ApiUsageDay.objects.create(
                    consumer=consumer, date=today - timedelta(days=day),
                    endpoint=impact.PORTAL_CONNECT_ENDPOINT, count=day)
        with CaptureQueriesContext(connections["academy_db"]) as many:
            impact.portal_sessions()

        self.assertEqual(len(many), len(few),
                         "The query count grew with the number of rows.")

    # --- one broken app must not take the page down --------------------

    def test_a_failing_section_is_named_rather_than_dropped(self):
        from pipeline.services import impact

        with mock.patch.object(impact, 'academy',
                               side_effect=RuntimeError('academy is down')):
            data = impact.everything()
        self.assertIsNone(data['academy'])
        self.assertIn('academy', data['failed_sections'])
        # The others are unaffected — the point of catching per section.
        self.assertIsNotNone(data['api'])

    def test_the_page_still_renders_when_a_section_fails(self):
        with mock.patch('pipeline.services.impact.selection_tool',
                        side_effect=RuntimeError('selector is down')):
            response = self._user("root", True).get(reverse("pipeline:impact"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be read")

    # --- reachable ------------------------------------------------------

    def test_it_is_in_both_browse_menus(self):
        """Two menus, and a link added to one is not in the other — which is
        how a phone once lost the gene a laptop kept."""
        body = self._user("root", True).get(
            reverse("pipeline:target_board")).content.decode()
        self.assertEqual(
            body.count(reverse("pipeline:impact")), 2,
            "Impact should appear in the desktop dropdown and the responsive "
            "menu, once each.")
