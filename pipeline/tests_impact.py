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
    databases = {"pipeline_db", "academy_db"}

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

    # --- the page draws what the sections return ------------------------

    def test_the_page_draws_the_new_sections_with_rows_in_them(self):
        """An empty page renders differently from a full one, and a chart is the
        classic place that shows up: `widthratio` divides, `|last` walks the
        list, and neither runs at all when the series is empty. The other tests
        render this page with nothing in it."""
        from django.utils import timezone

        from core.models import McpUsageDay
        from pipeline.models import GeneRequest

        McpUsageDay.objects.create(date=timezone.localdate(),
                                   tool="search_antibodies",
                                   client="claude-ai", count=3)
        GeneRequest.objects.create(typed_text="MAPT", gene_symbol="MAPT",
                                   email="a@example.com")

        response = self._user("root", True).get(reverse("pipeline:impact"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "search_antibodies")
        self.assertContains(response, "MAPT")

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


class McpSectionTests(TestCase):
    """The MCP connector's numbers.

    Two things can be quietly wrong. A **zero day dropped from the series**
    turns "used twice in a month" into a chart that looks like steady use, since
    a chart drawn only from the days something happened closes the gaps up. And
    a **zero that means "nobody is counting"** drawn as a zero that means
    "nobody called" is the failure this whole section exists to end — it is the
    misreading the WorkOS user list produced on 31 Aug 2026.
    """

    databases = {"pipeline_db", "academy_db"}

    def _rows(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.models import McpUsageDay

        today = timezone.localdate()
        McpUsageDay.objects.create(date=today, tool="search_antibodies",
                                   client="claude-ai", count=3)
        McpUsageDay.objects.create(date=today - timedelta(days=2),
                                   tool="target_report", client="chatgpt",
                                   count=1)

    def test_a_quiet_day_is_a_zero_and_not_a_gap(self):
        from pipeline.services import impact

        self._rows()
        series = impact.mcp_server()['series']
        self.assertEqual(len(series), impact.RECENT_DAYS)
        self.assertEqual(series[-1]['n'], 3)
        self.assertEqual(series[-2]['n'], 0, "The quiet day was dropped.")
        self.assertEqual(series[-3]['n'], 1)

    def test_the_series_is_oldest_first(self):
        """The chart draws it left to right; reversed, every trend reads
        backwards and nothing on the page says so."""
        from pipeline.services import impact

        self._rows()
        series = impact.mcp_server()['series']
        self.assertLess(series[0]['date'], series[-1]['date'])

    def test_the_totals_and_the_lists_agree(self):
        from pipeline.services import impact

        self._rows()
        data = impact.mcp_server()
        self.assertEqual(data['calls'], 4)
        self.assertEqual(sum(r['calls'] for r in data['by_tool']), 4)
        self.assertEqual(sum(r['calls'] for r in data['by_client']), 4)
        self.assertEqual(data['days'], 2)

    def test_the_page_says_when_nothing_is_being_counted(self):
        """No token means no reporting, which means an empty table is not a fact
        about usage. The page must say which of the two it is showing."""
        from django.test import override_settings

        from pipeline.services import impact

        with override_settings(MCP_USAGE_TOKEN=''):
            self.assertFalse(impact.mcp_server()['reporting'])
        with override_settings(MCP_USAGE_TOKEN='set'):
            self.assertTrue(impact.mcp_server()['reporting'])


class GeneDemandSectionTests(TestCase):
    """Requests and genes are two numbers, and the difference is the signal.

    One gene asked for three times is three requests and one gene. Folding them
    would throw away the repetition, which is the only reason these rows are
    kept one-per-press in the first place.
    """

    databases = {"pipeline_db", "academy_db"}

    def test_repeated_requests_for_one_gene_are_counted_both_ways(self):
        from pipeline.models import GeneRequest
        from pipeline.services import impact

        for _ in range(3):
            GeneRequest.objects.create(typed_text="MAPT", gene_symbol="MAPT",
                                       email="a@example.com")
        GeneRequest.objects.create(typed_text="SOD1", gene_symbol="SOD1",
                                   email="b@example.com", has_funding=True)

        data = impact.gene_demand()
        self.assertEqual(data['requests'], 4)
        self.assertEqual(data['genes'], 2)
        self.assertEqual(data['with_funding'], 1)
        self.assertEqual(data['top'][0]['count'], 3)

    def test_the_current_month_is_marked_partial(self):
        """A month one day old is short of days, not short of demand. Drawn
        unlabelled beside complete months its bar reads as a collapse — which is
        how it was read on 1 Sep 2026, when 27 August requests sat next to a
        September that was a few hours old."""
        from django.utils import timezone

        from pipeline.models import GeneRequest
        from pipeline.services import impact

        GeneRequest.objects.create(typed_text="MAPT", gene_symbol="MAPT",
                                   email="a@example.com")
        series = impact.gene_demand()['series']
        self.assertTrue(series[-1]['partial'])
        self.assertEqual(series[-1]['month'],
                         timezone.localdate().replace(day=1))
        self.assertFalse(any(row['partial'] for row in series[:-1]))

    def test_no_requests_draws_no_months(self):
        """An empty series rather than a made-up one: with nothing recorded there
        is no first month to count from."""
        from pipeline.services import impact

        self.assertEqual(impact.gene_demand()['series'], [])


class InternalUsageIsExcludedButShownTests(TestCase):
    """Our own testing must not inflate an impact figure — and must not vanish.

    Two things can be silently wrong. The **keyless** API rows have no consumer
    at all, and `exclude(consumer__is_internal=True)` drops them too, because
    `NOT (consumer_id IN (…))` is NULL for a NULL id and SQL keeps only rows a
    WHERE clause says TRUE about. That would delete a headline number while
    looking like it only removed our keys. And an exclusion the page does not
    print is a total nobody can check: the count that was held back has to be
    on the screen beside the one it was held back from.
    """

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        from django.utils import timezone

        from core.models import APIConsumer, ApiUsageDay, McpUsageDay
        from pipeline.services import impact

        today = timezone.localdate()
        self.customer = APIConsumer.objects.create(
            name="Real Supplier", consumer_type="manufacturer")
        self.ours = APIConsumer.objects.create(
            name="OGA test key", consumer_type="rrid", is_internal=True)

        ApiUsageDay.objects.create(consumer=self.customer, date=today,
                                   endpoint="antibodies_feed", count=5)
        ApiUsageDay.objects.create(consumer=self.ours, date=today,
                                   endpoint="antibodies_feed", count=90)
        ApiUsageDay.objects.create(consumer=None, date=today,
                                   endpoint="antibodies_feed", count=7)
        ApiUsageDay.objects.create(consumer=self.customer, date=today,
                                   endpoint=impact.PORTAL_CONNECT_ENDPOINT,
                                   count=3)
        ApiUsageDay.objects.create(consumer=self.ours, date=today,
                                   endpoint=impact.PORTAL_CONNECT_ENDPOINT,
                                   count=40)

        McpUsageDay.objects.create(date=today, tool="list_targets",
                                   client="claude-ai", count=2)
        McpUsageDay.objects.create(date=today, tool="list_targets",
                                   client="claude-code", internal=True,
                                   count=30)

    def test_a_keyless_request_survives_excluding_our_keys(self):
        """The trap. This section totals every endpoint, so the customer's are
        5 on the feed plus 3 portal connects, and 7 are keyless: 15. Our 130
        are gone. If the keyless rows had been swept out with our keys this
        would read 8, which looks like a plausible number and is not one."""
        from pipeline.services import impact

        data = impact.api_usage()
        self.assertEqual(data['total_requests'], 15)
        self.assertEqual(data['keyless_requests'], 7)
        self.assertEqual(data['keyed_requests'], 8)

    def test_the_api_says_what_it_held_back(self):
        from pipeline.services import impact

        data = impact.api_usage()
        self.assertEqual(data['internal_requests'], 130)
        self.assertEqual(data['internal_keys'], 1)
        # And our key is not counted as an organisation reached.
        self.assertEqual(data['organisations'], 1)

    def test_the_portal_excludes_our_key_and_counts_it(self):
        from pipeline.services import impact

        data = impact.portal_sessions()
        self.assertEqual(data['connects'], 3)
        self.assertEqual(data['organisations'], 1)
        self.assertEqual(data['internal_connects'], 40)
        self.assertNotIn("OGA test key",
                         [r['consumer__name'] for r in data['by_consumer']])

    def test_the_mcp_excludes_our_calls_and_counts_them(self):
        from pipeline.services import impact

        data = impact.mcp_server()
        self.assertEqual(data['calls'], 2)
        self.assertEqual(data['internal_calls'], 30)
        self.assertEqual(sum(r['calls'] for r in data['by_tool']), 2)
        self.assertEqual(data['series'][-1]['n'], 2)
