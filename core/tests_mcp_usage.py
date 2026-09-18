"""The MCP usage counter: the door, and the arithmetic behind it.

Three things here can be wrong *silently*, which is the bar for a test in this
repo.

**A second row instead of an increment.** The counter's whole value is that one
row per (day, tool, client) is incremented in place; a path that inserts a
second row splits a total across rows nobody would think to sum, and every
number on the impact page is quietly low.

**A door that is open when it should not be.** This is the only unauthenticated-
by-URL write path on the site. Unconfigured it must not exist; misconfigured it
must refuse. Neither failure shows on any screen.

**A reporter choosing its own date.** The row count is bounded because the date
is this server's. A payload that could set it could write any day it liked into
a figure somebody is going to quote in a grant.
"""
import json
from unittest import mock

from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import mcp_usage
from core.models import McpUsageDay

TOKEN = "shared-secret"
URL_NAME = "mcp_usage_report"


class RecordTests(TestCase):
    databases = {"academy_db"}

    def test_two_calls_increment_one_row(self):
        mcp_usage.record("search_antibodies", "claude-ai")
        mcp_usage.record("search_antibodies", "claude-ai")
        row = McpUsageDay.objects.get()
        self.assertEqual(row.count, 2)

    def test_a_different_client_is_a_different_row(self):
        """The split is the point: "who is calling" is half of what the page
        answers, and folding two clients into one row loses it."""
        mcp_usage.record("target_report", "claude-ai")
        mcp_usage.record("target_report", "chatgpt")
        self.assertEqual(McpUsageDay.objects.count(), 2)

    def test_an_unnamed_client_is_a_row_and_not_a_crash(self):
        mcp_usage.record("target_report")
        self.assertEqual(McpUsageDay.objects.get().client, "")

    def test_a_database_error_is_swallowed(self):
        """Telemetry never breaks the thing it measures — and these rows share a
        database with the site's logins, so a locked counter must not be able to
        become a failed sign-in."""
        with mock.patch.object(McpUsageDay.objects, "filter",
                               side_effect=DatabaseError("locked")):
            mcp_usage.record("search_antibodies")  # must not raise
        self.assertFalse(McpUsageDay.objects.exists())

    def test_an_over_long_name_is_truncated_not_refused(self):
        """A future tool with a long name must be counted, not dropped, and must
        not raise on the column width."""
        mcp_usage.record("t" * 200, "c" * 200)
        row = McpUsageDay.objects.get()
        self.assertEqual(len(row.tool), 64)
        self.assertEqual(len(row.client), 64)


@override_settings(MCP_USAGE_TOKEN=TOKEN)
class EndpointTests(TestCase):
    databases = {"academy_db"}

    def setUp(self):
        self.client = Client()
        self.url = reverse(URL_NAME)

    def _post(self, payload, token=TOKEN):
        return self.client.post(
            self.url, data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_report_is_counted(self):
        self.assertEqual(self._post({"tool": "list_targets",
                                     "client": "claude-code"}).status_code, 204)
        row = McpUsageDay.objects.get()
        self.assertEqual((row.tool, row.client, row.count),
                         ("list_targets", "claude-code", 1))

    def test_a_wrong_token_writes_nothing(self):
        self.assertEqual(self._post({"tool": "x"}, token="wrong").status_code, 401)
        self.assertFalse(McpUsageDay.objects.exists())

    def test_no_token_at_all_writes_nothing(self):
        self.assertEqual(
            self.client.post(self.url, data="{}",
                             content_type="application/json").status_code, 401)
        self.assertFalse(McpUsageDay.objects.exists())

    def test_a_get_writes_nothing(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_report_cannot_choose_its_own_day(self):
        """The date is the server's. A reporter that could set it could write
        into any month it liked, and the row count is bounded by the fact that
        it cannot."""
        self._post({"tool": "x", "date": "2019-01-01"})
        self.assertEqual(McpUsageDay.objects.get().date, timezone.localdate())

    def test_a_count_is_clamped(self):
        self._post({"tool": "x", "count": 10 ** 9})
        self.assertEqual(McpUsageDay.objects.get().count, mcp_usage.MAX_REPORT)

    def test_a_report_with_no_tool_is_refused(self):
        self.assertEqual(self._post({"client": "claude-ai"}).status_code, 400)
        self.assertFalse(McpUsageDay.objects.exists())

    def test_junk_is_refused_rather_than_500(self):
        response = self.client.post(self.url, data="not json",
                                    content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, 400)


class UnconfiguredTests(TestCase):
    databases = {"academy_db"}

    @override_settings(MCP_USAGE_TOKEN="")
    def test_the_endpoint_does_not_exist_until_a_token_is_set(self):
        """404, not 401 and not an open door. A deployment that has not been
        given the token has not asked for a write path."""
        response = Client().post(reverse(URL_NAME), data="{}",
                                 content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(McpUsageDay.objects.exists())


@override_settings(MCP_USAGE_TOKEN=TOKEN)
class InternalFlagTests(TestCase):
    """Our own calls are a separate row, not a separate meaning of one row.

    `internal` is part of the unique key. If it were not, our call and a
    reader's call to one tool on one day would collide: the count would be
    right and the attribution would be whichever report arrived first — a
    number that looks perfect and credits the wrong people.
    """

    databases = {"academy_db"}

    def _post(self, payload):
        return Client().post(
            reverse(URL_NAME), data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def test_ours_and_theirs_are_two_rows(self):
        self._post({"tool": "list_targets", "client": "claude-ai"})
        self._post({"tool": "list_targets", "client": "claude-ai",
                    "internal": True})
        self.assertEqual(McpUsageDay.objects.count(), 2)
        self.assertEqual(
            McpUsageDay.objects.get(internal=True).count, 1)
        self.assertEqual(
            McpUsageDay.objects.get(internal=False).count, 1)

    def test_a_report_that_says_nothing_is_external(self):
        """A reporter too old to send the flag, or one that could not attribute
        its caller, must not have its calls filed as ours. Over-reporting our
        own testing is honest; inventing somebody else's reach is not."""
        self._post({"tool": "list_targets"})
        self.assertFalse(McpUsageDay.objects.get().internal)

    def test_two_internal_calls_increment_one_row(self):
        self._post({"tool": "x", "internal": True})
        self._post({"tool": "x", "internal": True})
        row = McpUsageDay.objects.get()
        self.assertEqual((row.internal, row.count), (True, 2))


class AdminScreenTests(TestCase):
    """There has to be somewhere to correct `internal`.

    Every other column here is derived from a request and is read-only. This
    one is a judgement the connector can only make once it knows whose sign-ins
    are ours, so the rows written before that are external by default and wrong
    — and until this screen existed there was no way to say so. A registration
    that quietly went missing would leave the first weeks of counting
    permanently miscredited with nothing on any screen to fix it.
    """

    databases = {"academy_db"}

    def test_the_rows_are_reachable_and_the_flag_is_editable(self):
        from django.contrib import admin

        from core.models import McpUsageDay

        self.assertIn(McpUsageDay, admin.site._registry,
                      "MCP usage rows have no admin screen.")
        options = admin.site._registry[McpUsageDay]
        self.assertIn('internal', options.list_editable)
        # And the counted columns stay read-only: they are facts about a
        # request, not opinions about it.
        for field in ('date', 'tool', 'client', 'count'):
            self.assertIn(field, options.readonly_fields)


class AdminMergeTests(TestCase):
    """Ticking `internal` on a row whose bucket is taken must not 500.

    This is not hypothetical. The first time the screen was used — 1 Sep 2026,
    ticking all four rows at once — one of them was a `list_targets` call that
    already had a sibling row in the internal bucket, and the unique key
    refused it: `UNIQUE constraint failed: date, tool, client, internal`,
    surfaced as an unhandled 500 with no clue what to do about it.

    The person means "count these as ours". Both rows count real calls of the
    same tool on the same day, so the honest result is one row holding all of
    them, and a message saying so.
    """

    databases = {"academy_db"}

    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from django.contrib.auth.models import User
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from core.admin import McpUsageDayAdmin

        self.admin = McpUsageDayAdmin(McpUsageDay, AdminSite())
        self.request = RequestFactory().post("/admin/core/mcpusageday/")
        self.request.user = User(username="root", is_superuser=True)
        self.request.session = {}
        self.request._messages = FallbackStorage(self.request)
        self.today = timezone.localdate()

    def _row(self, internal, count):
        return McpUsageDay.objects.create(
            date=self.today, tool="list_targets", client="claude-code",
            internal=internal, count=count)

    def test_the_two_rows_become_one_and_the_calls_all_survive(self):
        ours = self._row(True, 1)
        theirs = self._row(False, 2)

        theirs.internal = True
        self.admin.save_model(self.request, theirs, form=None, change=True)

        self.assertEqual(McpUsageDay.objects.count(), 1)
        survivor = McpUsageDay.objects.get()
        self.assertEqual(survivor.pk, ours.pk)
        self.assertTrue(survivor.internal)
        # 1 + 2. The count is the whole point: a merge that replaced instead of
        # added would look tidy and quietly lose two calls.
        self.assertEqual(survivor.count, 3)

    def test_the_merge_says_what_it_did(self):
        """A screen that silently absorbed a row would leave somebody wondering
        where it went — the same rule as every receipt in the pipeline."""
        self._row(True, 1)
        theirs = self._row(False, 2)
        theirs.internal = True
        self.admin.save_model(self.request, theirs, form=None, change=True)

        said = [str(m) for m in self.request._messages]
        self.assertTrue(said, "The merge happened with nothing said about it.")
        self.assertIn("merged", said[0])
        self.assertIn("3", said[0])
        self.assertIn("list_targets", said[0])

    def test_the_message_does_not_open_with_the_tool_name(self):
        """Django's admin template renders `{{ message|capfirst }}`, so a
        message starting with `list_targets` prints as `List_targets` — an
        identifier nobody can search for. Same rule as the µg/mL heading CSS
        uppercased into MG/ML: presentation must not rewrite a value."""
        from django.template.defaultfilters import capfirst

        self._row(True, 1)
        theirs = self._row(False, 2)
        theirs.internal = True
        self.admin.save_model(self.request, theirs, form=None, change=True)

        shown = capfirst(str(list(self.request._messages)[0]))
        self.assertIn("list_targets", shown)
        self.assertNotIn("List_targets", shown)

    def test_an_ordinary_tick_still_just_saves(self):
        """The merge path must not fire when the target bucket is free."""
        row = self._row(False, 2)
        row.internal = True
        self.admin.save_model(self.request, row, form=None, change=True)

        self.assertEqual(McpUsageDay.objects.count(), 1)
        self.assertTrue(McpUsageDay.objects.get().internal)
        self.assertEqual(McpUsageDay.objects.get().count, 2)
        self.assertFalse([str(m) for m in self.request._messages])
