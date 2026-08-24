"""
OGA Database Router — OGA_website/db_router.py
===============================================

Routes Django ORM queries to one of the two live databases. The split is by
subject, and it is the thing to keep straight:

    pipeline_db (PostgreSQL)      — antibody and scientific data
        All YCharOS pipeline models (targets, antibodies, cell lines, sessions,
        results, figures, reports)
        Django auth/contenttypes/sessions tables, because pipeline.Member has a
        ForeignKey to auth.User — a separate user pool from academy_db
        Falls back to local SQLite when PIPELINE_DATABASE_URL is not set.

    academy_db (PostgreSQL)       — people and organisations
        Users, auth, sessions, allauth, admin
        All academy models (progress, certificates), credentials, selector
        core.APIConsumer, core.ReviewedAntibody, core.ApiUsageDay — who
        consumes the API and what they have read

There is no third database. `default` is `{}` — deliberately empty, so an
unrouted model raises ImproperlyConfigured instead of quietly landing
somewhere. It used to be `db_core.sqlite3`, a SQLite file committed to git
holding the retired `core` models (Gene, Antibody, Description, Experiment,
CellLine); both the file and the models were removed on 23 Aug 2026.
"""

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


def _is_pipeline(app_label):
    """Returns True if this model belongs in the pipeline database."""
    return app_label == PIPELINE_APP


class OGARouter:
    """
    Routes reads and writes:
      - pipeline models  → 'pipeline_db' (the science)
      - everything else  → 'academy_db'   (the people)
    """

    def db_for_read(self, model, **hints):
        if _is_pipeline(model._meta.app_label):
            return 'pipeline_db'
        return 'academy_db'

    def db_for_write(self, model, **hints):
        if _is_pipeline(model._meta.app_label):
            return 'pipeline_db'
        return 'academy_db'

    def allow_relation(self, obj1, obj2, **hints):
        # Allow relations within the same database. Pipeline models only
        # relate to other pipeline models (and pipeline_db's own auth.User).
        db1 = self._get_db(obj1)
        db2 = self._get_db(obj2)
        if db1 == db2:
            return True
        # Block pipeline ↔ academy cross-database relations
        return False

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Controls which migrations run on which database.

        pipeline_db gets:
          - pipeline app migrations
          - Django infrastructure (auth, contenttypes, sessions, admin)
            because pipeline.Member has FK to auth.User

        academy_db gets:
          - everything else (core, academy, allauth, auth, admin, etc.)

        `default` gets nothing, and is not a database — see the module
        docstring.
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

        # Everything that is not pipeline is academy.
        if model_name is not None:
            return db in ACADEMY_ALIASES

        # No model name (app-level migration operations like RunPython/RunSQL).
        # Return None = no opinion, let Django decide.
        return None

    def _get_db(self, obj):
        """Helper: determine which database an object lives in."""
        if _is_pipeline(obj._meta.app_label):
            return 'pipeline_db'
        return 'academy_db'