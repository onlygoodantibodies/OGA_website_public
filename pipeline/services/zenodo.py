"""The Zenodo API, as much of it as one deposit needs.

**The InvenioRDM API, not the legacy deposit one.** Zenodo rebuilt on InvenioRDM
in Oct 2023; ``/api/deposit/depositions`` still answers, but its ``communities``
metadata field no longer creates a community submission — which is the half this
is for — and its per-file upload ceiling is 100 MB against the new API's 50 GB.
Both reasons point the same way.

Five calls, in this order, and the order is the design:

1. ``POST /api/records``                              — create a draft
2. ``POST /api/records/{id}/draft/pids/doi``          — reserve the DOI
3. ``POST/PUT/POST .../draft/files/…``                — three-step file upload
4. ``PUT  /api/records/{id}/draft/review``            — attach to the community
5. ``POST .../draft/actions/submit-review``           — submit for review

**The app never publishes.** A draft submitted for review is not public — not
even to somebody with the link — until a curator of the community accepts it,
and accepting is what publishes. That is Zenodo's own behaviour, not something
built here, and it is exactly the approval gate the work was asked for. Nothing
in this module can make a record public.

Step 2 is why the order matters: the DOI is reserved *before* the Data Note is
generated, so the document carries the real DOI in its Data availability section
instead of ``[DOI to be assigned upon deposit]``.

No client library. The three Python ones are thin wrappers over these calls and
none of them implements the community review flow — ``inveniordm-py`` has no
communities or requests support at all; only zen4R (R) does, and its source is
where the exact call shapes here were read from. A dependency that does the easy
half and skips the half this exists for is not worth its update cost.

Every call is bounded and **degrades rather than raises**, the rule
``tests_timeouts.py`` pins for UniProt and SciCrunch: an unreachable Zenodo must
be reported as unreachable, not surface as a 500 on a scientist's screen. And no
call happens inside an open transaction.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
# Files are the slow part and are streamed, so they get their own budget.
UPLOAD_TIMEOUT_SECONDS = 600


class ZenodoError(Exception):
    """A refusal worth showing somebody, with the detail already logged."""


def base_url() -> str:
    """Which Zenodo. Sandbox is a *different site* with its own logins and its
    own tokens, issuing test DOIs under ``10.5072`` — so pointing at it is how
    the whole flow gets driven end to end without one real record existing.
    """
    return (getattr(settings, "ZENODO_BASE_URL", "")
            or "https://zenodo.org").rstrip("/")


def community() -> str:
    return getattr(settings, "ZENODO_COMMUNITY", "") or "ycharos"


def token() -> str:
    return getattr(settings, "ZENODO_API_TOKEN", "") or ""


def configured() -> str:
    """Why a deposit cannot be attempted, or "".

    Answered before anything is built, so the page can grey the button with the
    reason rather than assembling a payload and failing at the last call. The
    message names the setting because this one reaches whoever runs the site,
    not a bench scientist — the deposit button is not on a bench surface.
    """
    if not token():
        return ("Zenodo is not connected yet: no API token is configured, so "
                "nothing can be submitted. Whoever looks after the site needs "
                "to add one (ZENODO_API_TOKEN).")
    return ""


def _headers(extra=None) -> dict:
    h = {"Authorization": f"Bearer {token()}"}
    h.update(extra or {})
    return h


def _request(method, path, **kwargs):
    """One HTTP call, bounded, with the failure turned into something sayable."""
    url = f"{base_url()}/api{path}"
    kwargs.setdefault("timeout", TIMEOUT_SECONDS)
    try:
        resp = requests.request(method, url, headers=_headers(kwargs.pop("headers", None)),
                                **kwargs)
    except RequestException as exc:
        logger.warning("zenodo %s %s failed: %s", method, path, exc)
        raise ZenodoError(
            "Zenodo could not be reached, so nothing was submitted. Nothing "
            "here has been changed — try again in a few minutes.") from exc
    if resp.status_code >= 400:
        # The body carries Invenio's field-level errors, which are what actually
        # says *why* a deposit was refused. To the log, never to the page.
        logger.warning("zenodo %s %s -> %s: %s", method, path,
                       resp.status_code, resp.text[:2000])
        raise ZenodoError(_refusal(resp))
    return resp.json() if resp.content else {}


def _refusal(resp) -> str:
    """What to tell somebody about a 4xx/5xx, in words they can act on."""
    if resp.status_code in (401, 403):
        return ("Zenodo refused the connection — the API token is missing, "
                "expired, or lacks permission to deposit. Nothing was "
                "submitted.")
    if resp.status_code == 404:
        return ("Zenodo could not find that record or community. Nothing was "
                "submitted.")
    detail = ""
    try:
        body = resp.json()
        parts = [f"{e.get('field', '')}: {e.get('messages', e.get('message', ''))}"
                 for e in (body.get("errors") or [])]
        detail = "; ".join(p for p in parts if p.strip(": "))
        detail = detail or (body.get("message") or "")
    except ValueError:
        pass
    return (f"Zenodo refused the deposit: {detail}" if detail
            else "Zenodo refused the deposit. Nothing was submitted.")


# ── the five calls ──────────────────────────────────────────────────────────

def create_draft(metadata: dict) -> dict:
    """A new unpublished record. Nothing is public at any point after this."""
    return _request("POST", "/records", json=metadata,
                    headers={"Content-Type": "application/json"})


def reserve_doi(record_id: str) -> str:
    """Mint the DOI on the draft, before publication.

    This is what lets the Data Note inside the deposit cite the deposit.
    """
    out = _request("POST", f"/records/{record_id}/draft/pids/doi")
    return ((out.get("pids") or {}).get("doi") or {}).get("identifier", "")


def upload_file(record_id: str, name: str, stream) -> dict:
    """The three-step upload: declare the key, send the bytes, commit it.

    Streamed rather than read into memory — a raw scan is the largest thing
    this app handles, and the point of the new API is that it takes them.
    """
    _request("POST", f"/records/{record_id}/draft/files", json=[{"key": name}],
             headers={"Content-Type": "application/json"})
    _request("PUT", f"/records/{record_id}/draft/files/{name}/content",
             data=stream, timeout=UPLOAD_TIMEOUT_SECONDS,
             headers={"Content-Type": "application/octet-stream"})
    return _request("POST", f"/records/{record_id}/draft/files/{name}/commit")


def attach_to_community(record_id: str, community_id: str) -> dict:
    """Make this draft a submission to the community.

    One community per review request — a limitation of Zenodo's, not a choice
    here.
    """
    return _request("PUT", f"/records/{record_id}/draft/review",
                    json={"receiver": {"community": community_id},
                          "type": "community-submission"},
                    headers={"Content-Type": "application/json"})


def submit_for_review(record_id: str, message: str = "") -> dict:
    """Hand it to the curators. **This does not publish it** — their acceptance
    does, which is the whole reason this route was chosen.
    """
    payload = ({"payload": {"content": message, "format": "html"}}
               if message else {})
    return _request("POST", f"/records/{record_id}/draft/actions/submit-review",
                    json=payload, headers={"Content-Type": "application/json"})


def community_id(slug: str = "") -> str:
    """Resolve a community slug to the id the review request wants."""
    out = _request("GET", f"/communities/{slug or community()}")
    return out.get("id", "")


def record_url(record_id: str) -> str:
    return f"{base_url()}/records/{record_id}"


def record_url_for_doi(doi: str) -> str:
    """``Report.zenodo_doi`` is a URLField and every row on file holds a link,
    so a bare ``10.5281/zenodo.123`` would be the one entry nobody could click.
    """
    doi = (doi or "").strip()
    if not doi:
        return ""
    if doi.startswith("http"):
        return doi
    return f"https://doi.org/{doi}"
