"""Where a session's raw files live, and why it is not the media bucket.

The media bucket is **public on purpose**: the public antibody pages serve
publication images straight off it, so ``AWS_QUERYSTRING_AUTH = False`` and a
Cloudflare custom domain turn every object into a plain, permanent URL. That is
right for a figure panel that is meant to be seen and wrong for an uncropped gel
scan, which is unpublished lab data.

Routing the download through ``views/attachments.py`` was half of it — the app
never prints the object key, so there is nothing to copy. It is only half,
because the object is still *served* by the public route to anyone who has or
guesses the key, and "we don't show the URL" is not the same as "there is no
URL". The other half is here: attachments get their own storage, which never
uses the public custom domain and signs the URLs it does produce.

**Set ``R2_ATTACHMENTS_BUCKET`` to a bucket with no public route.** Without it
this falls back to the media bucket, which keeps every file readable and
downloadable — nothing breaks — but leaves the object reachable on the public
domain by key. That fallback is deliberate rather than a hard failure: refusing
to boot over a missing optional variable takes the site down to fix a privacy
gap, which is the worse trade. ``manage.py check`` says so instead.
"""
from __future__ import annotations

from django.conf import settings
from django.core.files.storage import default_storage


def attachment_storage():
    """The storage ``FileAttachment.file`` uses.

    A callable, evaluated once when the models load, so a deploy that flips
    ``USE_R2`` picks the right backend without the field being redefined.
    """
    if not getattr(settings, "USE_R2", False):
        # Local development: the ordinary media root, which is the point of dev.
        # In production this is the case `services/attachments.py::
        # storage_refusal` refuses an upload for — Render's container filesystem
        # does not survive a deploy.
        return default_storage

    from storages.backends.s3 import S3Storage
    return S3Storage(
        bucket_name=(getattr(settings, "R2_ATTACHMENTS_BUCKET", "")
                     or settings.AWS_STORAGE_BUCKET_NAME),
        # Never the public route, whichever bucket this ends up on.
        custom_domain=None,
        # Signed and short-lived, so a URL that does escape stops working.
        querystring_auth=True,
        querystring_expire=getattr(settings, "R2_ATTACHMENT_URL_SECONDS", 300),
        default_acl=None,
        file_overwrite=False,
    )


def attachments_are_public() -> bool:
    """True when attachments would land in the bucket the public site serves.

    Read by the system check. Not a reason to refuse an upload — the file is
    stored, kept and downloadable either way — so this is a warning about who
    else could read it, not about whether it survives.
    """
    return bool(getattr(settings, "USE_R2", False)
                and not getattr(settings, "R2_ATTACHMENTS_BUCKET", ""))
