from django.apps import AppConfig
from django.core.checks import Warning as CheckWarning, register


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        register(_check_signed_firefox_build, "core")
        register(_check_api_throttle_cache, "core")


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
