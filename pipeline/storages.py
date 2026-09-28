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

**Queued whole IHC figures use it too** (``PendingIhcFigure.image``, 26 Sep
2026): they show several suppliers' unreleased results, so they are private
until ``services/ihc_figures.py`` copies them to the public key at release.

**So do the cropper's own uploads** (``CropperImage.image``, via
``cropper_storage``, 26 Sep 2026). A whole IHC figure is copied byte for byte
*from* the upload, so while the upload sat in the public media bucket under its
own filename, "private until release" was only unlinked — the same bytes were a
guessable public URL from the moment they were added, through release and past
a withdrawal. The cropper draws them through ``views/cropper.py::
cropper_image``, a members-only view, never a storage URL.

**Set ``R2_ATTACHMENTS_BUCKET`` to a bucket with no public route.** Without it
this falls back to the media bucket, which keeps every file readable and
downloadable — nothing breaks — but leaves the object reachable on the public
domain by key. That fallback is deliberate rather than a hard failure: refusing
to boot over a missing optional variable takes the site down to fix a privacy
gap, which is the worse trade. ``manage.py check`` says so instead.
"""
from __future__ import annotations

from django.conf import settings
from django.core.files.storage import Storage, default_storage


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


class _PrivateFirst(Storage):
    """Writes to private storage; reads private first, then the public media
    bucket — where every cropper upload staged before ``cropper_storage``
    existed still sits, and a resumed session must go on finding it.

    Deletes from both, so removing a session's figures removes them wherever
    they are. ``url`` is never what the cropper shows (see
    ``views/cropper.py::cropper_image``)."""

    def __init__(self, primary, legacy):
        self.primary, self.legacy = primary, legacy

    def _holder(self, name):
        try:
            if self.primary.exists(name):
                return self.primary
        except Exception:
            pass
        return self.legacy

    def _open(self, name, mode="rb"):
        return self._holder(name).open(name, mode)

    def _save(self, name, content):
        return self.primary.save(name, content)

    def get_valid_name(self, name):
        return self.primary.get_valid_name(name)

    def get_available_name(self, name, max_length=None):
        return self.primary.get_available_name(name, max_length=max_length)

    def exists(self, name):
        return self.primary.exists(name) or self.legacy.exists(name)

    def delete(self, name):
        for storage in (self.primary, self.legacy):
            try:
                storage.delete(name)
            except Exception:
                pass

    def size(self, name):
        return self._holder(name).size(name)

    def url(self, name):
        return self._holder(name).url(name)


def cropper_storage():
    """The storage ``CropperImage.image`` uses — the attachments storage for
    new uploads, falling back to the media bucket for older ones."""
    primary = attachment_storage()
    if primary is default_storage:
        return default_storage
    return _PrivateFirst(primary, default_storage)
