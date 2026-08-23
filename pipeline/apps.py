from django.apps import AppConfig
from django.core.checks import Warning as CheckWarning, register


class PipelineConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pipeline'
    verbose_name = 'YCharOS Pipeline'

    def ready(self):
        # Issuing an antibody's A-number and a cell line's C-number, on
        # `pre_save`, so every door that creates a record gets it — see
        # `pipeline/signals.py` for why that is one receiver rather than a call
        # in each of the eight write paths.
        from pipeline import signals  # noqa: F401

        register(_check_attachment_storage, "pipeline")
        register(_check_stylesheet, "pipeline")


def _check_attachment_storage(app_configs, **kwargs):
    """Tell the owner about raw-file storage at deploy, not via a scientist.

    Both of these are **warnings, never errors**: an error fails
    ``manage.py check``, which the pre-deploy ``migrate`` runs, so it would take
    the whole site down over a file-storage setting. That is a gate nobody asked
    for, and the runtime refusal already protects the person at the bench. What
    this buys is that the owner finds out first.
    """
    from django.conf import settings
    from pipeline.storages import attachments_are_public

    issues = []
    if not settings.DEBUG and not getattr(settings, "USE_R2", False):
        issues.append(CheckWarning(
            "Uploaded session files would not survive a deploy.",
            hint="USE_R2 is not set, so files land on the container filesystem, "
                 "which Render rebuilds from git on every deploy. Attaching is "
                 "refused while this is true — see "
                 "pipeline/services/attachments.py::storage_refusal.",
            id="pipeline.W001"))
    if attachments_are_public():
        issues.append(CheckWarning(
            "Raw session files are stored in the public media bucket.",
            hint="Set R2_ATTACHMENTS_BUCKET to a bucket with no public route. "
                 "Files store and download correctly either way; without it an "
                 "uncropped gel scan is reachable on the public domain by "
                 "anyone who knows its key.",
            id="pipeline.W002"))
    return issues


def _check_stylesheet(app_configs, **kwargs):
    """Say when production is rendering off the CDN rather than its own file.

    The fallback in ``pipeline/styling.py`` is there so a fresh clone has
    styling with no build step, which is right for a laptop and wrong for the
    deploy — and it fails *softly*, so a build step that quietly did nothing
    looks exactly like a working site until the day somebody's network blocks
    the CDN. This is the thing that makes it visible.

    A warning, never an error, for the same reason as W001 and W002: an error
    fails ``manage.py check``, which the pre-deploy ``migrate`` runs, and taking
    the site down over a stylesheet is worse than serving it from a CDN.
    """
    from django.conf import settings
    from pipeline.styling import built_stylesheet_url

    if settings.DEBUG or built_stylesheet_url() is not None:
        return []
    return [CheckWarning(
        "The pipeline is styled from cdn.tailwindcss.com, not from this site.",
        hint="pipeline/static/pipeline/tailwind.css was not found, so pages fall "
             "back to Tailwind's browser build. Run ./bin/build_css.sh before "
             "collectstatic — the Build Command should be `pip install -r "
             "requirements.txt && ./bin/build_css.sh && python manage.py "
             "collectstatic --noinput`.",
        id="pipeline.W003")]
