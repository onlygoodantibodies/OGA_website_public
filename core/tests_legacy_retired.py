"""What the retirement of the legacy `core` layer has to keep true.

The five legacy models (Gene, Antibody, Experiment, Description, CellLine), the
SQLite file they lived in (`db_core.sqlite3`, tracked in git), and the `default`
alias that pointed at it were all removed on 23 Aug 2026. `default` is now `{}`
— chosen because Django treats an empty alias as unconfigured, so anything that
reaches it raises `ImproperlyConfigured` at the query instead of quietly reading
or writing somewhere plausible.

Three of the four classes here guard the silent half of that.

* `NothingRoutesToDefaultTests` — the whole point. A model routed to `default`
  does not fail at import or at deploy; it fails the first time somebody opens
  the page that reads it, in production. Asking the router directly about every
  installed model is cheap and answers before then.
* `LegacyNumericGeneUrlsTests` — `/124/` → `/antibodies/ARHGDIA/` used to be a
  `core.Gene` primary-key lookup. The map is frozen in `core/legacy_gene_ids.py`
  and nothing regenerates it, so a typo in it is invisible: the redirect still
  answers 301, just to the wrong gene.
* `NoLegacyModelSurvivesTests` — the retired names must not come back by
  accident, and the three that stayed must still be here.

`AdminStillOpensTests` is the odd one out and is tagged `commissioning`: the
admin is where a routing mistake shows up first and most bluntly, and this
proved it worked at the point of the change. It fails loudly, so it does not
need to run on every edit.
"""

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from OGA_website.db_router import OGARouter


class NothingRoutesToDefaultTests(SimpleTestCase):
    """`default` is empty, so any model that lands there is a 500 waiting.

    Nothing here runs a query — it asks the router and the settings — so this
    is a `SimpleTestCase` and costs no test database.
    """

    def test_default_is_the_dummy_backend(self):
        """`{}` in settings.py, but not `{}` by the time Django has booted.

        `ConnectionHandler` fills every alias in with its defaults, and for one
        with no ENGINE the default is `django.db.backends.dummy` — the backend
        whose every operation raises `ImproperlyConfigured`. So asserting the
        literal empty dict tests the wrong object; the dummy engine is the
        thing that makes an unrouted access loud, and it is what to pin.
        """
        self.assertEqual(settings.DATABASES['default']['ENGINE'],
                         'django.db.backends.dummy')
        self.assertEqual(settings.DATABASES['default']['NAME'], '')

    def test_no_installed_model_reads_or_writes_default(self):
        router = OGARouter()
        stray = []
        for model in apps.get_models(include_auto_created=True):
            for verb, alias in (('read', router.db_for_read(model)),
                                ('write', router.db_for_write(model))):
                if alias in (None, 'default'):
                    stray.append(f'{model._meta.label} ({verb} -> {alias})')
        self.assertEqual(stray, [], f'models routed to default: {stray}')

    def test_every_alias_the_router_names_is_a_real_database(self):
        """A router may only answer with an alias that has an ENGINE.

        A typo'd alias name is the same failure as `default` and reads the same
        way from the outside — "improperly configured" naming no model.
        """
        router = OGARouter()
        for model in apps.get_models(include_auto_created=True):
            for alias in (router.db_for_read(model), router.db_for_write(model)):
                engine = settings.DATABASES.get(alias, {}).get('ENGINE')
                self.assertTrue(
                    engine and engine != 'django.db.backends.dummy',
                    f'{model._meta.label} routed to unusable alias {alias!r}')

    def test_default_takes_no_migrations(self):
        router = OGARouter()
        for app_label in ('core', 'academy', 'pipeline', 'auth', 'account'):
            self.assertNotEqual(
                router.allow_migrate('default', app_label, model_name='x'), True,
                f'{app_label} would migrate into the empty default database')


class NoLegacyModelSurvivesTests(SimpleTestCase):
    """The five are gone; the three that route to academy_db are not."""

    def test_the_five_retired_models_are_not_installed(self):
        labels = {m._meta.label for m in apps.get_models()}
        for retired in ('core.Gene', 'core.Antibody', 'core.Experiment',
                        'core.Description', 'core.CellLine'):
            self.assertNotIn(retired, labels)

    def test_the_three_live_core_models_are_installed(self):
        labels = {m._meta.label for m in apps.get_models()}
        for kept in ('core.APIConsumer', 'core.ReviewedAntibody',
                     'core.ApiUsageDay'):
            self.assertIn(kept, labels)


class LegacyNumericGeneUrlsTests(TestCase):
    """The frozen id -> name map still answers, and answers correctly."""

    databases = {"academy_db", "pipeline_db"}

    def test_a_known_id_redirects_permanently_to_its_gene_page(self):
        from core.legacy_gene_ids import LEGACY_GENE_NAMES
        # 124 is the id the module docstring uses as its worked example.
        self.assertEqual(LEGACY_GENE_NAMES[124], 'ARHGDIA')
        response = self.client.get('/124/')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], '/antibodies/ARHGDIA/')

    def test_the_map_covers_the_ids_that_were_issued(self):
        """155 rows, ids 1..157 with two gaps. A truncated copy is silent."""
        from core.legacy_gene_ids import LEGACY_GENE_NAMES
        self.assertEqual(len(LEGACY_GENE_NAMES), 155)
        self.assertEqual(min(LEGACY_GENE_NAMES), 1)
        self.assertEqual(max(LEGACY_GENE_NAMES), 157)
        self.assertTrue(all(isinstance(v, str) and v
                            for v in LEGACY_GENE_NAMES.values()))

    def test_an_id_that_was_never_issued_is_a_404(self):
        self.assertEqual(self.client.get('/99999/').status_code, 404)


@tag("commissioning")
class AdminStillOpensTests(TestCase):
    """Every registered model's admin pages open with no `default` behind them.

    The admin touches routing more broadly than any single page: it builds a
    changelist and an add form for all 31 registered models across both
    databases, and writes a `LogEntry` for anything it changes.
    """

    databases = {"academy_db", "pipeline_db"}

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.db_manager('academy_db').create_superuser(
            username='boss', email='boss@example.com', password='pw')

    def setUp(self):
        self.client.force_login(self.boss)

    def test_the_index_and_every_changelist_and_add_form_open(self):
        self.assertEqual(self.client.get(reverse('admin:index')).status_code, 200)
        broken = []
        for model in admin.site._registry:
            slug = f'{model._meta.app_label}_{model._meta.model_name}'
            for view in ('changelist', 'add'):
                response = self.client.get(reverse(f'admin:{slug}_{view}'))
                # 403 on an add form is a deliberate `has_add_permission =
                # False` — ApiUsageDay is counted, not typed. Anything else is
                # the routing breaking.
                if response.status_code not in (200, 403):
                    broken.append(f'{slug}_{view} -> {response.status_code}')
        self.assertEqual(broken, [], f'admin pages not opening: {broken}')

    def test_the_admin_can_still_write_and_log_it(self):
        from django.contrib.admin.models import LogEntry
        from core.models import APIConsumer
        self.client.post(reverse('admin:core_apiconsumer_add'), {
            'name': 'Smoke Co', 'consumer_type': 'rrid', 'tier': 'free',
            'is_active': 'on',
        })
        self.assertTrue(APIConsumer.objects.filter(name='Smoke Co').exists())
        self.assertTrue(LogEntry.objects.exists())
