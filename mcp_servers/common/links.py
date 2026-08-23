"""Build verification URLs into the pipeline web app.

Helper for handing back a link to a record so a logged-in pipeline user can one-
click through to eyeball it. (Originally used by the retired write server; kept
as a small utility.) This resolves the canonical pipeline URL via Django's
``reverse`` (so it always matches ``pipeline/urls.py``) and prefixes it with the
site's base URL.

Base URL comes from ``PIPELINE_BASE_URL`` (default the production site). Set it
to ``http://localhost:8000`` for local dev.
"""
from __future__ import annotations

import os

# kind -> pipeline URL name (each takes a single pk arg)
_URL_NAMES = {
    "antibody": "pipeline:antibody_detail",
    "cell_line": "pipeline:cell_line_detail",
    "target": "pipeline:target_detail",
    "session": "pipeline:session_detail",
    "batch": "pipeline:batch_detail",
    "generate_report": "pipeline:generate_report",
}

# name -> pipeline URL name (no args) — landing pages the LLM can deep-link.
_PAGE_NAMES = {
    "cropper": "pipeline:cropper",
    "feasibility": "pipeline:feasibility",
    "sessions": "pipeline:session_list",
}

_DEFAULT_BASE = "https://onlygoodantibodies.co.uk"


def base_url() -> str:
    return (os.environ.get("PIPELINE_BASE_URL") or _DEFAULT_BASE).rstrip("/")


def _reverse(name, args):
    try:
        from django.urls import reverse, NoReverseMatch
        try:
            return base_url() + reverse(name, args=args)
        except NoReverseMatch:
            return None
    except Exception:
        return None


def url_for(kind: str, pk) -> str | None:
    """Absolute pipeline URL for ``kind`` (antibody/cell_line/target/session/
    batch/generate_report) and primary key, or None if it can't be built."""
    if pk is None or kind not in _URL_NAMES:
        return None
    return _reverse(_URL_NAMES[kind], [pk])


def page_url(name: str, query: dict | None = None) -> str | None:
    """Absolute URL for a no-arg landing page (cropper/feasibility/sessions),
    with an optional ``query`` dict appended as a querystring (e.g. the cropper
    page deep-linked to a gene: ``page_url('cropper', {'gene': 'SNCA'})``)."""
    if name not in _PAGE_NAMES:
        return None
    url = _reverse(_PAGE_NAMES[name], [])
    if url and query:
        from urllib.parse import urlencode
        clean = {k: v for k, v in query.items() if v not in (None, "")}
        if clean:
            url += "?" + urlencode(clean)
    return url
