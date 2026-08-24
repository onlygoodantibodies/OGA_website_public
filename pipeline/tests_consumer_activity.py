"""One API consumer's activity page: whose data it draws, and what it promises.

Four things here can be wrong *silently*, which is the whole bar for this file.

**Whose rows it draws.** Every number on the page is filtered to one consumer.
Attributing another organisation's traffic to a named partner is a mistake
nobody would see — the page looks exactly as convincing either way.

**Whether it and the partner's own portal agree.** ``/v1/status/`` tells a
consumer how many antibodies are waiting for them; this page tells the owner the
same thing. Two spellings of that count is the shape this repo keeps getting
bitten by, so both are pinned to the one function.

**Whether the plain-English mapping has kept up with the API.** A new endpoint
with no entry draws as a bare URL name in the middle of a sentence. The expected
keys are derived from ``core/api_urls.py`` rather than copied, so adding a route
fails here instead of shipping.

**Whether two organisations sharing a name still merge.** The impact page used
to group its busiest-organisations table on the name, which silently added a
trial key's traffic to a paid key's row. It groups on the id now — which is also
what the link needs.

The gate is loud rather than silent, and pinned anyway: it is the decision the
page rests on, and a gate that quietly opened would publish which partners are
pulling how hard to every bench member.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.db import connections
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pipeline.models import Member, Site

DB = "pipeline_db"


class ConsumerActivityPageTests(TestCase):
    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester",
                                                  short_code="LEI")

    # --- fixtures ------------------------------------------------------

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

    def _consumer(self, name="Bio-Techne", **kwargs):
        from core.models import APIConsumer

        kwargs.setdefault("consumer_type", "manufacturer")
        kwargs.setdefault("supplier_filter", "Bio-Techne")
        return APIConsumer.objects.create(name=name, **kwargs)

    def _usage(self, consumer, endpoint, count, date=None):
        from core.models import ApiUsageDay

        return ApiUsageDay.objects.create(
            consumer=consumer, endpoint=endpoint, count=count,
            date=date or timezone.localdate())

    # --- who may see it ------------------------------------------------

    def test_a_superuser_can_open_it(self):
        consumer = self._consumer()
        response = self._user("root", True).get(
            reverse("pipeline:api_consumer", args=[consumer.pk]))
        self.assertEqual(response.status_code, 200)

    def test_an_ordinary_member_cannot(self):
        """Same gate as the impact page it hangs off, for the same reason: how
        hard a named partner pulls is commercial, not bench science."""
        consumer = self._consumer()
        response = self._user("bench", False).get(
            reverse("pipeline:api_consumer", args=[consumer.pk]))
        self.assertNotEqual(
            response.status_code, 200,
            "A non-superuser opened an API consumer's activity page.")

    def test_the_whole_page_renders_with_every_section_populated(self):
        """The cheap guard against a template error, which renders as a 500 or
        — worse — as a silently-taken wrong branch. Every section is given
        something to draw, including the ones an empty consumer skips.
        """
        from core.models import ReviewedAntibody
        from pipeline.models import Antibody, Company, Target

        consumer = self._consumer(
            gene_filter="GBA1,GPNMB",
            portal_config={"image_format": "jpg", "url_pattern": ""},
            last_queried_at=timezone.now() - timedelta(days=2))
        self._usage(consumer, "api_status", 4)
        self._usage(consumer, "antibodies_feed", 2,
                    date=timezone.localdate() - timedelta(days=3))
        Antibody.objects.create(
            target=Target.objects.create(protein_name='Synuclein',
                                         gene_name='SNCA'),
            company=Company.objects.create(name='Bio-Techne'),
            catalogue_number='NBP1-88736')
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue='NBP1-88736')

        response = self._user("root", True).get(
            reverse("pipeline:api_consumer", args=[consumer.pk]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        self.assertIn("Bio-Techne", body)
        self.assertIn("opened the portal", body)       # the activity mapping
        self.assertIn("NBP1-88736", body)              # the ticked-off table
        self.assertIn("image_format", body)            # their settings
        self.assertIn("/api/v1/openapi.json", body)    # the caveat names paths
        # The gene is a way to its own page, wherever it is printed.
        self.assertIn(reverse("pipeline:target_detail",
                              args=[Target.objects.get(gene_name='SNCA').pk]),
                      body)

    def test_an_unknown_consumer_is_a_404(self):
        response = self._user("root", True).get(
            reverse("pipeline:api_consumer", args=[999999]))
        self.assertEqual(response.status_code, 404)

    # --- whose rows it draws -------------------------------------------

    def test_it_draws_only_this_consumers_requests(self):
        """The mistake nobody would see: the page is equally convincing when it
        is quietly totalling somebody else's traffic under this partner's name.
        """
        from pipeline.services import consumer_activity

        mine = self._consumer(name="Bio-Techne")
        theirs = self._consumer(name="Abcam", supplier_filter="Abcam")
        self._usage(mine, "api_status", 3)
        self._usage(theirs, "api_status", 40)

        data = consumer_activity.for_consumer(mine)
        self.assertEqual(data["requests"], 3)
        self.assertEqual(data["portal_requests"], 3)

    def test_a_keyless_row_is_nobodys(self):
        """`consumer=None` rows are the whole internet. Folded into a named
        organisation they would inflate exactly the partner being assessed."""
        from core.models import ApiUsageDay
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        self._usage(consumer, "genes_feed", 2)
        ApiUsageDay.objects.create(consumer=None, endpoint="genes_feed",
                                   count=99, date=timezone.localdate())

        self.assertEqual(consumer_activity.for_consumer(consumer)["requests"], 2)

    # --- one reader for "how many are waiting" --------------------------

    def test_the_page_asks_the_same_question_the_portal_answers(self):
        """`/v1/status/` and this page must not disagree about a partner's
        backlog. Both are pinned to `pending_antibodies_for`: a count written
        out a second time would pass every other test in this file."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        with mock.patch("core.api_views.pending_antibodies_for",
                        return_value=37) as reader:
            data = consumer_activity.for_consumer(consumer)
        self.assertEqual(data["pending"], 37)
        self.assertTrue(data["pending_known"])
        reader.assert_called_once_with(consumer)

    def test_the_status_endpoint_asks_it_too(self):
        consumer = self._consumer()
        with mock.patch("core.api_views.pending_antibodies_for",
                        return_value=37):
            response = Client().get(reverse("api:api_status"),
                                    HTTP_X_API_KEY=str(consumer.api_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_antibodies"], 37)

    def test_an_unreachable_pipeline_is_a_gap_and_not_a_zero(self):
        """"0 waiting" about a partner with a backlog is worse than saying the
        count could not be taken — the page draws a dash and says so.

        The consumer has a tick on purpose. Without one, the gene lookup is
        skipped and the test proves nothing about the outage it is named for:
        that is exactly how an unguarded second pipeline read sat here passing.
        """
        from core.models import ReviewedAntibody
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue="ab12345")

        with mock.patch("core.api_views.pending_antibodies_for",
                        side_effect=RuntimeError("pipeline down")), \
             mock.patch("pipeline.models.Antibody.objects.filter",
                        side_effect=RuntimeError("pipeline down")):
            data = consumer_activity.for_consumer(consumer)

        self.assertIsNone(data["pending"])
        self.assertFalse(data["pending_known"])
        # ...and the ticks are still theirs; only the gene column is missing.
        self.assertTrue(data["reviewed"]["unresolved"])
        self.assertEqual(len(data["reviewed"]["listed"]), 1)

    def test_the_reason_for_a_missing_count_is_on_the_page(self):
        """A dash with its reason nowhere is an accusation: "not counted" reads
        as "we do not count this", not "we could not reach the database". The
        sentence used to live inside the cursor-has-moved branch, so the
        commonest case carried no reason at all."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()  # last_queried_at is None
        with mock.patch("core.api_views.pending_antibodies_for",
                        side_effect=RuntimeError("pipeline down")):
            story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("did not answer", story)

    # --- the plain-English mapping --------------------------------------

    def test_every_api_endpoint_has_plain_english(self):
        """Derived from the URLconf, not copied from it.

        A route added with no entry here draws as a bare `gene_progress` in the
        middle of an English sentence — legible enough to ship and wrong enough
        to notice a year later.
        """
        from core import api_urls
        from pipeline.services.consumer_activity import ENDPOINT_ACTIVITY

        routed = {p.name for p in api_urls.urlpatterns if p.name}
        missing = sorted(routed - set(ENDPOINT_ACTIVITY))
        self.assertEqual(
            missing, [],
            f"API endpoints with no plain-English label: {missing}. "
            "Add them to ENDPOINT_ACTIVITY.")

    def test_an_unlabelled_endpoint_is_still_drawn(self):
        """Never dropped: a row that vanished for want of a translation makes
        the page understate the very thing it exists to show."""
        from pipeline.services.consumer_activity import activity_label

        self.assertIn("wat", activity_label("wat"))

    # --- what they ticked off --------------------------------------------

    def test_a_ticked_catalogue_is_resolved_to_its_gene(self):
        """The one genuinely cross-database read on the page.

        `ReviewedAntibody` holds a catalogue **string** in academy_db while the
        antibodies are pipeline rows, and the router refuses to relate the two —
        so this is matched in Python, and a match that quietly stopped working
        would draw a page of catalogue numbers with every gene blank, which is
        indistinguishable from a partner who ticked off retired products.
        """
        from core.models import ReviewedAntibody
        from pipeline.models import Antibody, Company, Target
        from pipeline.services import consumer_activity

        target = Target.objects.create(protein_name='Synuclein',
                                       gene_name='SNCA')
        company = Company.objects.create(name='Bio-Techne')
        Antibody.objects.create(target=target, company=company,
                                catalogue_number='NBP1-88736')

        consumer = self._consumer()
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue='NBP1-88736')
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue='GONE-999')

        listed = consumer_activity.for_consumer(consumer)['reviewed']['listed']
        by_catalogue = {r['catalogue']: r for r in listed}
        self.assertEqual(by_catalogue['NBP1-88736']['gene'], 'SNCA')
        # The gene links to its own page, so the row has to carry the id.
        self.assertEqual(by_catalogue['NBP1-88736']['target_id'], target.pk)
        # Kept and drawn without a gene rather than dropped: a tick against
        # something no longer on file is a fact worth seeing.
        self.assertIsNone(by_catalogue['GONE-999']['gene'])
        self.assertFalse(by_catalogue['GONE-999']['matched'])

    def test_a_catalogue_is_matched_whatever_its_case(self):
        """Both sides are typed by somebody else.

        The consumer POSTs whatever their own system calls the product, and 19
        live rows carry the wrong case already. An exact match draws a stocked
        product as "no longer on file" — a statement about the partner's ticks
        that is really a statement about capitalisation.
        """
        from core.models import ReviewedAntibody
        from pipeline.models import Antibody, Company, Target
        from pipeline.services import consumer_activity

        target = Target.objects.create(protein_name='Tau', gene_name='MAPT')
        company = Company.objects.create(name='Bio-Techne')
        Antibody.objects.create(target=target, company=company,
                                catalogue_number='67322-1-Ig')

        consumer = self._consumer()
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue='67322-1-IG')

        listed = consumer_activity.for_consumer(consumer)['reviewed']['listed']
        self.assertEqual(listed[0]['gene'], 'MAPT')

    def test_one_number_on_two_genes_is_not_resolved_to_either(self):
        """`catalogue_number` carries no unique constraint of its own, and the
        live data has one number on two targets. Picking whichever row sorted
        first labelled a partner's tick with another product's gene, silently —
        and the gene is the one column a reader would quote."""
        from core.models import ReviewedAntibody
        from pipeline.models import Antibody, Company, Target
        from pipeline.services import consumer_activity

        company = Company.objects.create(name='Bio-Techne')
        for gene in ('RAB32', 'SNCA'):
            Antibody.objects.create(
                target=Target.objects.create(protein_name=gene,
                                             gene_name=gene),
                company=company, catalogue_number='ab251764')

        consumer = self._consumer()
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue='ab251764')

        row = consumer_activity.for_consumer(consumer)['reviewed']['listed'][0]
        self.assertTrue(row['ambiguous'])
        self.assertIsNone(row['gene'])

    def test_a_matched_row_with_no_gene_is_not_drawn_as_missing(self):
        """An antibody whose target carries a blank gene name is on file — 8
        such rows live — so it must not read as the tick having gone missing."""
        from core.models import ReviewedAntibody
        from pipeline.models import Antibody, Company, Target
        from pipeline.services import consumer_activity

        Antibody.objects.create(
            target=Target.objects.create(protein_name='Unnamed', gene_name=''),
            company=Company.objects.create(name='Bio-Techne'),
            catalogue_number='NB-1')

        consumer = self._consumer()
        ReviewedAntibody.objects.create(consumer=consumer,
                                        antibody_catalogue='NB-1')

        row = consumer_activity.for_consumer(consumer)['reviewed']['listed'][0]
        self.assertTrue(row['matched'])
        self.assertIsNone(row['gene'])

    # --- the sentences ---------------------------------------------------

    def test_a_key_that_has_never_been_used_says_so(self):
        """The empty states are the interesting ones on this page, so none of
        them may render as a blank."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("never used this key", story)

    def test_a_switched_off_key_says_its_silence_is_not_evidence(self):
        from pipeline.services import consumer_activity

        consumer = self._consumer(is_active=False)
        story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("switched off", story)

    def test_machine_only_traffic_is_not_read_as_portal_use(self):
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        self._usage(consumer, "antibodies_feed", 5)
        story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("never opened the portal", story)
        self.assertIn("machine traffic", story)

    def test_no_sign_ins_is_not_no_portal_work(self):
        """The portal's actions are keyed HTTP endpoints a script can call
        without ever fetching /v1/status/. Testing the sign-in alone put "all
        of this is machine traffic against the feeds" directly above a table
        reading "marked everything as reviewed"."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        self._usage(consumer, "antibodies_feed", 5)
        self._usage(consumer, "mark_reviewed", 2)
        story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("something the portal does", story)
        self.assertNotIn("All of this is machine traffic", story)

    def test_a_subset_agrees_with_the_total_it_is_part_of(self):
        """"1 of those request" — the noun refers back to the previous
        sentence's total, so it agrees with that, never with the subset. A
        partner who opened the portal once and otherwise scripts is the ordinary
        shape of a new integration."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        self._usage(consumer, "api_status", 1)
        self._usage(consumer, "antibodies_feed", 5)
        story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("1 of those requests", story)

    def test_a_single_day_is_not_a_range(self):
        """"between 5 August 2026 and 5 August 2026" lands on the commonest
        non-empty state on this page — a key somebody tried once."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        self._usage(consumer, "genes_feed", 1)
        story = " ".join(consumer_activity.for_consumer(consumer)["story"])
        self.assertIn("on one day", story)
        self.assertNotIn("between", story)

    # --- cost -------------------------------------------------------------

    def test_the_page_does_not_get_more_expensive_as_they_use_it(self):
        """A reporting page is the most tempting place to write a loop, and it
        looks fine on the handful of rows a dev database has."""
        from pipeline.services import consumer_activity

        consumer = self._consumer()
        self._usage(consumer, "api_status", 1)

        def queries():
            with CaptureQueriesContext(connections["academy_db"]) as ctx:
                consumer_activity.for_consumer(consumer)
            return len(ctx)

        small = queries()
        for day in range(2, 40):
            self._usage(consumer, "antibodies_feed", day,
                        date=timezone.localdate() - timedelta(days=day))
        self.assertEqual(small, queries())


class ImpactPageLinksTests(TestCase):
    """The link, and the grouping bug that making it a link exposed."""

    databases = {"pipeline_db", "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester",
                                                  short_code="LEI")

    def _consumer(self, name, **kwargs):
        from core.models import APIConsumer

        kwargs.setdefault("consumer_type", "manufacturer")
        return APIConsumer.objects.create(name=name, **kwargs)

    def _usage(self, consumer, endpoint, count):
        from core.models import ApiUsageDay

        ApiUsageDay.objects.create(consumer=consumer, endpoint=endpoint,
                                   count=count, date=timezone.localdate())

    def test_two_organisations_with_one_name_stay_two_rows(self):
        """Grouped on the name, a manufacturer's trial key and paid key merged
        into a single row whose total belonged to neither — and the `gene_filter`
        help text describes that pair as the normal way an account starts."""
        from pipeline.services import impact

        trial = self._consumer("Abcam", gene_filter="GBA1")
        paid = self._consumer("Abcam")
        self._usage(trial, "api_status", 3)
        self._usage(paid, "antibodies_feed", 9)

        rows = impact.api_usage()["by_consumer"]
        self.assertEqual(len(rows), 2, f"Two keys merged into one row: {rows}")
        self.assertEqual(
            {r["consumer_id"] for r in rows}, {trial.pk, paid.pk})

    def test_an_organisation_name_is_a_way_to_its_page(self):
        """A name links only when the row carries the id it would link to, and
        the href has to be the one Django reverses."""
        consumer = self._consumer("Bio-Techne")
        self._usage(consumer, "api_status", 4)

        for alias in ("academy_db", DB):
            u = User(username="root", is_superuser=True, is_staff=True)
            u.set_password("pw")
            u.save(using=alias)
        pipeline_user = User.objects.using(DB).get(username="root")
        Member.objects.create(user_id=pipeline_user.pk, site_id=self.site.pk,
                              role="admin", is_active=True)
        client = Client()
        self.assertTrue(client.login(username="root", password="pw"))

        body = client.get(reverse("pipeline:impact")).content.decode()
        self.assertIn(
            reverse("pipeline:api_consumer", args=[consumer.pk]), body,
            "The impact page names an organisation and does not link to it.")
