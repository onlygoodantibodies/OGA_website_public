from django.apps import AppConfig
from django.core.checks import Warning as CheckWarning, register


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        register(_check_signed_firefox_build, "core")
        register(_check_api_throttle_cache, "core")
        register(_check_academy_db_is_durable, "core")
        register(_check_academy_db_url_parsed, "core")
        register(_check_target_confusion_files, "core")
        register(_check_confusion_release_is_served, "core")
        _watch_for_snapshot_changes()


def _watch_for_snapshot_changes():
    """Clear the extension snapshot whenever what it reports changes.

    A signal rather than a call in each write path, because the flags are
    written from more places than anyone will remember: the recommendations
    board, `review.py::release` and `withdraw`, the Access importers, the
    antibody board, and management commands. A write path that forgets to
    invalidate looks exactly like one that did — the save succeeds, the board
    redraws, and the stale answer is the one readers get. This cannot be
    forgotten by construction.

    **Deliberately broad.** It fires on every `Antibody` save, not only on a
    change to a recommendation flag, because `post_save` cannot tell you which
    fields moved without keeping a copy of the row. The cost is a rebuild on the
    next `/extension/index.json` request after any antibody write, and the cache
    is per-process LocMem, so the signal itself is a dict delete. Being wrong in
    this direction costs a query; being wrong in the other direction publishes a
    verdict nobody can correct for a day.

    `PublicationImage` matters as much as the flags: a figure is what makes an
    application *assessed*, so publishing or withdrawing one changes verdicts on
    antibodies whose own row never moved.
    """
    from django.db.models.signals import post_delete, post_save

    from .extension_index import invalidate_snapshot

    def _drop(sender, **kwargs):
        invalidate_snapshot()

    # Registered by label, so this module does not import pipeline models at
    # startup -- ready() runs before the app registry is fully populated for
    # every app, and an import here would tie core's readiness to pipeline's.
    for label in ('pipeline.Antibody', 'pipeline.PublicationImage'):
        post_save.connect(_drop, sender=label, weak=False,
                          dispatch_uid=f'oga_snapshot_{label}_save')
        post_delete.connect(_drop, sender=label, weak=False,
                            dispatch_uid=f'oga_snapshot_{label}_delete')


def _check_academy_db_url_parsed(app_configs, **kwargs):
    """Refuse a connection string that was not read as a connection string.

    An **error**, and the one place that is right — deliberately against this
    file's own convention, because the usual argument does not apply. The other
    checks here are warnings so that `manage.py check`, which runs inside the
    pre-deploy `migrate` *and* on every management command, cannot take the site
    down over an optional feature. This one can only fire when
    ACADEMY_DATABASE_URL is set *and* unparseable — a state no cron and no working
    deploy is ever in — so failing is free, and the alternative is Django's own
    message about a 150-character database name, which names neither the variable
    nor the slash that caused it.
    """
    from django.core.checks import Error as CheckError

    from OGA_website import academy_db

    why = academy_db.config_refusal()
    if not why:
        return []
    return [CheckError("ACADEMY_DATABASE_URL could not be read as a URL.",
                       hint=why, id="core.E001")]


def _check_academy_db_is_durable(app_configs, **kwargs):
    """Say out loud when academy_db points somewhere a deploy will wipe.

    The reasoning, the rule and the exemptions live in
    ``OGA_website/academy_db.py``; this is the half that reaches a deploy log.

    A **warning, never an error**, for a reason specific to this one: ``manage.py
    check`` runs on *every* management command, and the two cron services
    (``build_bulk_archive``, ``dataset_snapshot``) run this same code with **no
    disk attached**, so the fallback path is what they legitimately have. An
    error would abort both of them today, over a database neither one opens.

    The hard stop is ``academy_db.enforce()`` in ``wsgi.py``, which only a
    process about to serve HTTP ever reaches.
    """
    from OGA_website import academy_db

    why = academy_db.refusal()
    if not why:
        return []
    return [CheckWarning(
        "academy_db is not on durable storage.",
        hint=why,
        id="core.W004")]


def _check_api_throttle_cache(app_configs, **kwargs):
    """Say out loud that the API throttle is counting per worker.

    ``core/api_throttle.py`` counts requests in Django's cache. With no
    ``CACHES`` setting Django falls back to local memory, which every gunicorn
    worker has its own copy of — so the real ceiling is the configured one times
    the worker count, and it moves when the dyno is resized. That is an
    acceptable abuse guard and a poor promise, and the difference is invisible
    from the code, which is the only reason this is worth a line at deploy.

    A **warning, never an error**: ``manage.py check`` runs inside the pre-deploy
    ``migrate``, and refusing to start over a cache backend would take the whole
    site down to protect a rate limit.
    """
    from . import api_throttle

    if api_throttle.using_shared_cache():
        return []
    return [CheckWarning(
        "The API rate limit is counted in a per-process cache.",
        hint="Each gunicorn worker keeps its own counters, so a consumer can "
             f"reach roughly {api_throttle.BURST_LIMIT}/minute per worker "
             "rather than in total. Fine as an abuse guard; set CACHES to a "
             "shared backend (Redis) if the limit needs to be exact.",
        id="core.W003")]


def _check_target_confusion_files(app_configs, **kwargs):
    """Say out loud which rows of a reviewer's list could not be read.

    ``core/target_confusions.py`` drops a row with no DOI, no product code, or a
    verdict word it does not recognise, rather than guessing at it — a verdict
    nobody recognised must not become the strongest one by default. Dropping is
    right and silence is not: a paper the review documented would then read as a
    paper nobody had looked at, on the surface whose whole job is to say the
    opposite.

    A **warning, never an error**: ``manage.py check`` runs inside the pre-deploy
    ``migrate``, and a malformed reference file must not be able to take the site
    down. The notice degrades to nothing, which is what both tools did before it
    existed.
    """
    from . import target_confusions

    found = target_confusions.problems()
    if not found:
        return []
    return [CheckWarning(
        f"{len(found)} row(s) of the target-confusion lists could not be read.",
        hint="Dropped, not guessed at: " + "; ".join(found[:6])
             + ("; …" if len(found) > 6 else "")
             + ". Fix the file in core/data/target_confusions/ — a dropped row "
               "means the extension and the connector will treat that paper as "
               "one nobody has reviewed.",
        id="core.W005")]


def _check_confusion_release_is_served(app_configs, **kwargs):
    """``/extension/`` states the declared-target notices are live. Say when that
    stops being true.

    The declared-target table reaches every install on a site deploy, but the
    code that DRAWS a mark from it only reaches a reader once a store approves
    ``target_confusions.EXTENSION_RELEASE``. So the page's claim about them is
    a claim about which version is being served, and it has been wrong in both
    directions on one day.

    IT RAN THE OTHER WAY FIRST and that is worth keeping, because the reversal
    is the whole lesson. The page carried a *Coming in N* block and a *from N*
    chip while the feature was in review, and this check warned once N became
    the version being served — a promise about something readers already had.
    It fired exactly as designed on 12 Sep 2026, the copy went present-tense,
    and the check then had nothing left to guard in that direction.

    So it is inverted rather than deleted. The page now says Firefox installs
    draw these marks; that sentence becomes false the moment
    ``EXTENSION_XPI_VERSION`` names a build OLDER than ``EXTENSION_RELEASE``, or
    is unset — a rollback, or somebody clearing the variable — and the failure is
    silent in the way this file records over and over: the page renders, the
    words are grammatical, and the only reader who can tell is one who installed
    the extension and found the feature missing.

    WHAT IT CANNOT SEE is the Chrome half, and that is deliberate rather than an
    omission. A store approval leaves no trace in this repository or its
    environment — no file, no variable — so "Chrome in review" on that block is a
    sentence only a human can retire. When Chrome ships it, drop the
    *Chrome in review* half of the chip and of the block's tag by hand.

    UNSET IS SILENT, the same as ``_check_signed_firefox_build`` above and for
    the same reason: no variable means no signed build is offered and the page
    shows side-load instructions, which is a valid state and the one every dev
    checkout and CI run is in. Warning on it would put a permanent line in the
    output of every local ``check``, and a check that is always firing is one
    nobody reads — which costs more than the case it would catch, since clearing
    the variable in production already drops the Firefox button in a way a
    person looking at the page can see.

    A **warning**, never an error: ``manage.py check`` runs inside the pre-deploy
    ``migrate``, and stale copy must not be able to take the site down.
    """
    from django.conf import settings

    from . import target_confusions

    served = (getattr(settings, 'EXTENSION_XPI_VERSION', '') or '').strip()
    needed = target_confusions.EXTENSION_RELEASE
    if not served or _version_key(served) >= _version_key(needed):
        return []
    return [CheckWarning(
        "/extension/ says the declared-target notices are live in Firefox, but "
        f"the version being served is {served}, older than the {needed} that "
        "first draws them.",
        hint="The 'In Firefox now' tag on the wrong-target block and the chip "
             "beside the Wrong target swatch are now false — a reader is being "
             "told to expect a mark the build they can install does not draw. "
             "Either serve a build at or above "
             "core/target_confusions.py::EXTENSION_RELEASE, or put that block "
             "back to a promise.",
        id="core.W006")]


def _check_signed_firefox_build(app_configs, **kwargs):
    """``EXTENSION_XPI_VERSION`` must name a file that is actually in ``signed/``.

    The variable decides which Mozilla-signed build ``/extension/firefox.xpi``
    serves and which version ``/extension/updates.json`` offers to installed
    copies. It lives in the Render dashboard and the ``.xpi`` files live in git,
    so the two drift the moment one moves without the other — the same shape as
    the Build Command and ``.python-version``: configuration invisible from the
    code.

    Both directions fail quietly, which is the whole reason for this.

    Point it at a version with no file and ``extension_firefox_xpi`` 404s while
    ``signed_xpi`` goes False, so the install page **drops its one-click Firefox
    button** and says nothing — under copy that still reads "version N is
    current". The twelfth field test reported a version mismatch it could not
    explain, and nothing in the repo could answer it.

    Leave it behind after committing a newer signed build and the team keeps
    installing the old one, with the update manifest confirming they are current.
    That one is genuinely invisible: everything works, it is simply the wrong
    build. So a *newer* file sitting unserved in ``signed/`` is worth saying too.

    A **warning, never an error**: ``manage.py check`` runs inside the pre-deploy
    ``migrate``, so an error here would take the whole site down over a browser
    extension. Unset is silent — a valid state meaning no signed build is
    offered and the page shows side-load instructions instead.
    """
    from pathlib import Path

    from django.conf import settings

    version = getattr(settings, 'EXTENSION_XPI_VERSION', '')
    if not version:
        return []

    signed = Path(settings.BASE_DIR) / 'browser-extension' / 'signed'
    present = sorted(p.name[len('oga-extension-'):-len('.xpi')]
                     for p in signed.glob('oga-extension-*.xpi'))

    if version not in present:
        return [CheckWarning(
            f"EXTENSION_XPI_VERSION is {version!r}, but no signed build of that "
            "version is deployed.",
            hint="The Firefox install button is hidden and /extension/firefox.xpi "
                 "returns 404, while the page still says that version is current. "
                 "Either commit browser-extension/signed/oga-extension-"
                 f"{version}.xpi or set the variable to one that is there. "
                 f"Present: {', '.join(present) or 'none'}.",
            id="core.W001")]

    newest = max(present, key=_version_key)
    if _version_key(newest) > _version_key(version):
        return [CheckWarning(
            f"A newer signed build ({newest}) is deployed than "
            f"EXTENSION_XPI_VERSION ({version}) names.",
            hint="Firefox users stay on the older build and the update manifest "
                 "tells them they are current. Set EXTENSION_XPI_VERSION to "
                 f"{newest} once it is ready to serve, or delete the file if it "
                 "was committed by mistake.",
            id="core.W002")]
    return []


def _version_key(version):
    """`0.2.10` sorts above `0.2.9`, which a string compare gets backwards."""
    return tuple(int(p) if p.isdigit() else 0 for p in str(version).split('.'))
