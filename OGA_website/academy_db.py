"""Where the academy database is allowed to live, and what is in it.

Two facts, one module, because both move together during the SQLite →
PostgreSQL migration and both are the kind that go wrong silently.

**Which models live in academy_db** is derived from the router, never typed.
``db_router.OGARouter`` already answers it for every query the app makes; a
second list in a management command would be a second answer, and the one that
drifts is the one used once a quarter. ``models()`` asks the router.

**Where academy_db may live** is the guard this migration needs. Today the
alias is SQLite on a Render persistent disk, and the disk is the only reason
that disk exists — which is also why deploys are stop-then-start (a disk can be
mounted by one instance at a time, so Render must stop the old container before
the new one starts; that 40-second hole is the 502s readers hit, and no health
check can bridge it). Once the data is on PostgreSQL the disk comes off and the
gap closes.

The hazard is what the removal opens up. ``settings.ACADEMY_DB_PATH`` falls back
to ``BASE_DIR / 'db_academy.sqlite3'`` when ``/var/data/`` is absent, so with the
disk gone **and** ``ACADEMY_DATABASE_URL`` unset the app boots happily on a
brand-new empty SQLite file inside the container: nobody can sign in, a sign-up
appears to succeed, and it is gone at the next deploy with nothing on any screen
saying so. That is the silent-omission shape this project keeps paying for, and
it becomes reachable only *because* the disk was removed.

So the rule is the same one ``pipeline/services/attachments.py::storage_refusal``
applies to uploads — **a file the app knows it will lose is worse than no file**
— and it reads the same setting, ``PERSISTENT_MEDIA_ROOTS``. SQLite is allowed
only where the file survives a deploy.

It is deliberately expressed twice, at two strengths:

``core.W004`` is a **warning**, matching the convention every other check here
follows (``core.W001``-``W003``, ``pipeline.W002``-``W003``): ``manage.py check``
runs inside the pre-deploy ``migrate`` *and* on every management command, and the
two cron services run the same code with **no disk attached**. An error would
abort them today, over a database neither of them opens.

``enforce()`` is the **hard stop**, called from ``wsgi.py`` and therefore only by
a process that is about to serve HTTP — never by a cron, never by a management
command. A refusal there fails the health check, so with a rolling deploy the new
instance is never promoted and the old one keeps serving. Down with a named
reason beats up with an invented database.
"""
from __future__ import annotations

#: The alias the app reads and writes.
ALIAS = 'academy_db'

#: Aliases that hold the academy schema. The second exists only while the data
#: is being moved: it points at the SQLite file so ``academy_db_copy`` and
#: ``academy_db_compare`` can read the old and the new side by side. It is
#: registered only when ``ACADEMY_SQLITE_URL`` is set, so outside the migration
#: there is no second alias to point at a file by accident.
SOURCE_ALIAS = 'academy_sqlite'
ALIASES = frozenset({ALIAS, SOURCE_ALIAS})


def models():
    """Every model the router puts in ``academy_db``, in a stable order.

    ``include_auto_created=True`` matters: the automatic through tables behind
    ``User.groups`` and ``User.user_permissions`` hold real rows, and a copy that
    walked only the declared models would move every user and silently drop what
    each of them is allowed to do.
    """
    from django.apps import apps
    from django.db import router

    found = [m for m in apps.get_models(include_auto_created=True)
             if router.db_for_read(m) == ALIAS]
    return sorted(found, key=lambda m: (m._meta.app_label, m._meta.model_name))


def refusal(alias=ALIAS, config=None) -> str:
    """Why ``alias`` must not be served from where it points, or ``""``.

    Empty for anything that is not SQLite, for ``DEBUG`` (where a local file is
    the point), and for a SQLite file on a root ``PERSISTENT_MEDIA_ROOTS`` names.

    That last exemption is what keeps the rollback open: for as long as the
    Render disk is attached, clearing ``ACADEMY_DATABASE_URL`` puts the site back
    on ``/var/data/db_academy.sqlite3`` and this stays quiet. It starts speaking
    the moment that file is somewhere a deploy rebuilds.

    ``config`` defaults to the alias's entry in ``DATABASES``; passing one makes
    this a plain function of a database configuration, which is how the tests
    exercise every branch without overriding ``DATABASES`` and inviting Django's
    (correct) warning about doing that.
    """
    from django.conf import settings

    if config is None:
        config = settings.DATABASES.get(alias) or {}
    if not str(config.get('ENGINE', '')).endswith('sqlite3'):
        return ''
    if settings.DEBUG:
        return ''

    name = str(config.get('NAME', '') or '')
    persistent = tuple(str(p) for p in
                       getattr(settings, 'PERSISTENT_MEDIA_ROOTS', ()) or ())
    if persistent and name.startswith(persistent):
        return ''

    return (
        f"{alias} is a SQLite file at {name or '(unset)'}, which is not on a "
        "persistent disk. Every login, academy record and API key would be read "
        "from and written to a file this container rebuilds from git on the next "
        "deploy, so a sign-up would appear to work and be gone by morning. Set "
        "ACADEMY_DATABASE_URL to the PostgreSQL database that holds them, or "
        "re-attach the Render disk mounted at "
        f"{persistent[0] if persistent else '/var/data'}."
    )


#: PostgreSQL's own identifier limit. A NAME longer than this is not a long name,
#: it is the whole connection string in the wrong field.
MAX_PG_NAME = 63


def config_refusal(alias=ALIAS, config=None) -> str:
    """Why the configured connection cannot work, or ``""``.

    One failure mode, worth catching by name because Django's own message for it
    is unrecognisable. ``urlparse`` only treats what follows a scheme as a host if
    the separator is exactly ``://``; with ``:``, ``:/`` or ``:///`` it puts the
    entire rest of the string in ``path``, and ``dj_database_url`` hands that to
    ``NAME``. The connection then fails with::

        The database name '<the whole URL, password and all>' (150 characters) is
        longer than PostgreSQL's limit of 63 characters. Supply a shorter NAME in
        settings.DATABASES.

    which reads as a problem with the database's *name* — a thing nobody chose and
    nobody can shorten — rather than a slash. It cost a cutover attempt on
    23 Aug 2026.

    Empty ``HOST`` is the precise tell, and is independently always wrong here: a
    PostgreSQL connection with no host is a local socket, which no Render service
    has. Checking that as well as the length catches the same slip on a short URL,
    where the length alone would sail through and connect to nothing.
    """
    from django.conf import settings

    if config is None:
        config = settings.DATABASES.get(alias) or {}
    if 'postgresql' not in str(config.get('ENGINE', '')):
        return ''

    name = str(config.get('NAME', '') or '')
    host = str(config.get('HOST', '') or '')
    if len(name) <= MAX_PG_NAME and host:
        return ''

    symptom = (f"its database name came out {len(name)} characters long"
               if len(name) > MAX_PG_NAME else "it has no host")
    return (
        f"ACADEMY_DATABASE_URL is set but was not read as a URL — {symptom}, "
        "which happens when the separator after the scheme is not exactly '://'. "
        "A single slash, or three, puts the whole connection string into the "
        "database name. Copy the Internal Database URL from the Render dashboard "
        "with its copy button rather than retyping it. It should look like:\n"
        "  postgresql://USER:PASSWORD@dpg-xxxxxxxxxxxx-a/oga_academy_db\n"
        f"What was parsed: host={host or '(empty)'}, "
        f"name={name[:40]!r}{'...' if len(name) > 40 else ''}"
    )


def enforce(alias=ALIAS, config=None) -> None:
    """Raise rather than serve requests from a database about to be invented.

    Called from ``wsgi.py``, so it runs for gunicorn and for nothing else.
    """
    from django.core.exceptions import ImproperlyConfigured

    why = refusal(alias, config)
    if why:
        raise ImproperlyConfigured(why)
