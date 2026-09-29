"""A members-only image, served so a browser that already has it is not sent it
again.

Three views send stored figure bytes through this origin rather than a bucket
URL — `cropper_image`, `review_image` and `review_ihc_image` — and every byte
they send is on Render's bandwidth bill (5 GB a month on the plan). None of
them said anything a browser could cache on: no validator, no `Cache-Control`.
`ConditionalGetMiddleware` cannot help, because a `FileResponse` streams and it
only fingerprints a body it holds. So the review queue re-fetched every
thumbnail on every redraw, and a cropper session re-sent every 1–7 MB figure on
every reload. Measured on Render's access log, 28 Sep 2026 19:00: 103 MB of a
126 MB hour was `/pipeline/cropper/image/`; 26–29 Sep, `review/ihc-image/1/`
(3.2 MB) and `/2/` (7.0 MB) went out whole each time the queue was opened.

**The answer is a 304, not a longer cache life.** Every reply carries an ETag
and `private, no-cache`: the browser keeps the bytes and asks each time, and a
copy that still matches is answered with no body at all. `no-cache` rather than
a `max-age` because one of the three is not immutable — re-cropping *revises* a
queued crop at the same key (`services/review.py::stage`) — and a reviewer
judging yesterday's crop off their own cache is a silent wrong answer. Asking
costs one small request; being wrong costs a verdict. `private` keeps it out of
any shared cache, and these paths are under `/pipeline/`, which the Cloudflare
rule never stores anyway.

**The ETag is what the stored bytes are, read from the database** — the object
key plus the row's `updated_at` where it has one — so answering a 304 opens
nothing in storage. The key alone is not enough for a revised crop, which keeps
its key; the row is saved in the same call that writes the new bytes, so its
`updated_at` moves with them.
"""
import hashlib

from django.http import FileResponse
from django.utils.cache import get_conditional_response
from django.utils.http import quote_etag

CACHE_CONTROL = "private, no-cache"


def etag_for(field_file, version=""):
    """A strong ETag naming these stored bytes, derived without opening them."""
    digest = hashlib.sha256(f"{field_file.name}|{version}".encode()).hexdigest()
    return quote_etag(digest[:32])


def not_modified(request, field_file, version=""):
    """A bodiless 304 if the browser already holds these bytes, else None."""
    etag = etag_for(field_file, version)
    response = get_conditional_response(request, etag=etag)
    if response is not None:
        response["Cache-Control"] = CACHE_CONTROL
    return response


def serve(handle, field_file, version=""):
    """The bytes, stamped so the next request for them can be a 304."""
    response = FileResponse(handle, filename=field_file.name.rsplit("/", 1)[-1])
    response["ETag"] = etag_for(field_file, version)
    response["Cache-Control"] = CACHE_CONTROL
    return response


def version_of(row):
    """What moves when a row's stored bytes are rewritten under the same key."""
    stamp = getattr(row, "updated_at", None)
    return stamp.isoformat() if stamp else ""
