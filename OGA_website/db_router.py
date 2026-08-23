"""
OGA Database Router — OGA_website/db_router.py
===============================================

Routes Django ORM queries to the correct database:

    default (db_core.sqlite3)     — in git, deployed automatically
        Gene, Antibody, Description, Experiment, CellLine

    academy_db (db_academy.sqlite3) — on Render persistent disk, never in git
        APIConsumer (live writes from API queries)
        All academy models (users, progress, certificates)
        Auth, sessions, allauth, contenttypes, admin

    pipeline_db (PostgreSQL)      — Render PostgreSQL service
        All YCharOS pipeline models (targets, antibodies, sessions, etc.)
        Django auth/contenttypes/sessions tables (pipeline users are separate
        from OGA academy users — different user pool, different database)
        Falls back to local SQLite when PIPELINE_DATABASE_URL is not set.
"""

# These are the ONLY models that live in the git-deployed database.
# Everything else — including core.APIConsumer — goes to the persistent disk.
CORE_DATA_MODELS = {'gene', 'antibody', 'experiment', 'description', 'cellline'}

# The pipeline app gets its own PostgreSQL database.
PIPELINE_APP = 'pipeline'

# Aliases that carry the academy schema. `academy_db` is the one the app reads
# and writes; `academy_sqlite` exists only while the data is being moved off the
# Render disk, so `academy_db_copy` has somewhere to read the old rows from. Both
# take the same migrations — an academy database is an academy database — while
# `db_for_read`/`db_for_write` still name only `academy_db`, so nothing routes a
# live query at the source by accident.
ACADEMY_DB = 'academy_db'
ACADEMY_ALIASES = frozenset({ACADEMY_DB, 'academy_sqlite'})

# Django infrastructure apps that must exist in pipeline_db because
# pipeline models have ForeignKeys to auth.User (Member.user).
# These create a separate user pool from academy_db — pipeline users
# (YCharOS team) are not OGA academy users.
DJANGO_INFRA_APPS = {'auth', 'contenttypes', 'sessions', 'admin'}


def _is_core_data(app_label, model_name):
    """Returns True if this model belongs in the git-deployed database."""
    return app_label == 'core' and model_name in CORE_DATA_MODELS


def _is_pipeline(app_label):
    """Returns True if this model belongs in the pipeline database."""
    return app_label == PIPELINE_APP


class OGARouter:
    """
    Routes reads and writes:
      - core data models → 'default' (db_core.sqlite3)
      - pipeline models  → 'pipeline_db' (PostgreSQL)
      - everything else  → 'academy_db' (db_academy.sqlite3)
    """

    def db_for_read(self, model, **hints):
        if _is_pipeline(model._meta.app_label):
            return 'pipeline_db'
        if _is_core_data(model._meta.app_label, model._meta.model_name):
            return 'default'
        return 'academy_db'

    def db_for_write(self, model, **hints):
        if _is_pipeline(model._meta.app_label):
            return 'pipeline_db'
        if _is_core_data(model._meta.app_label, model._meta.model_name):
            return 'default'
        return 'academy_db'

    def allow_relation(self, obj1, obj2, **hints):
        # Allow relations within the same database.
        # Pipeline models only relate to other pipeline models (and auth.User).
        # Core/academy can relate to each other (existing behaviour).
        db1 = self._get_db(obj1)
        db2 = self._get_db(obj2)
        if db1 == db2:
            return True
        # Allow core ↔ academy relations (existing behaviour)
        if 'pipeline_db' not in (db1, db2):
            return True
        # Block pipeline ↔ core/academy cross-database relations
        return False

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Controls which migrations run on which database.

        pipeline_db gets:
          - pipeline app migrations
          - Django infrastructure (auth, contenttypes, sessions, admin)
            because pipeline.Member has FK to auth.User

        default gets:
          - core data models only

        academy_db gets:
          - everything else (academy, allauth, auth, admin, etc.)
        """
        # Pipeline app → only pipeline_db
        if _is_pipeline(app_label):
            return db == 'pipeline_db'

        # allauth lives ONLY on academy_db. Pin its migrations there — including
        # the app-level data migrations (RunPython) that otherwise return None and
        # get run on every database, where account_* tables don't exist (this bites
        # multi-database test-DB creation; harmless-but-wrong in production).
        if app_label in ('account', 'socialaccount'):
            return db in ACADEMY_ALIASES

        # For pipeline_db: allow Django infrastructure, block everything else
        if db == 'pipeline_db':
            return app_label in DJANGO_INFRA_APPS

        # Existing core/academy routing (unchanged)
        if model_name is not None:
            if _is_core_data(app_label, model_name):
                return db == 'default'
            else:
                return db in ACADEMY_ALIASES

        # No model name (app-level migration operations like RunPython/RunSQL).
        # Return None = no opinion, let Django decide.
        return None

    def _get_db(self, obj):
        """Helper: determine which database an object lives in."""
        if _is_pipeline(obj._meta.app_label):
            return 'pipeline_db'
        if _is_core_data(obj._meta.app_label, obj._meta.model_name):
            return 'default'
        return 'academy_db'