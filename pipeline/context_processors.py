"""Template context every pipeline page gets.

``nav_gene`` is the gene the page is about, which the Browse menu carries so
moving between boards does not mean retyping it. ``nav_extension_preview`` says
whether the browser-extension install page is still team-only, which decides
whether Browse offers it. ``pipeline_stylesheet`` is where the page fetches its
CSS from — this site's built file, or the CDN standing in for it.

Why a context processor rather than a template expression. The boards name their
gene in the query string, so ``request.GET.gene`` is right for them — but the
gene's own page names it in the URL *path*, and reading only the query string
meant the one page most obviously about a single gene was the one that dropped it.
Trying to express "the context variable, or else the query parameter" in the
template fails: ``{{ nav_gene|default:request.GET.gene }}`` resolves the fallback
as a *filter argument*, and a missing key on a QueryDict raises there rather than
falling back to empty.

So the default is computed here, and a view that knows better simply puts
``nav_gene`` in its own context — a view's context wins over a processor's.
"""
from __future__ import annotations

from django.conf import settings


def nav_gene(request):
    """The gene a board is filtered to, or empty. Views may override."""
    gene = ""
    if request.method == "GET":
        gene = (request.GET.get("gene") or "").strip()
    return {"nav_gene": gene}


def nav_chrome(request):
    """Flags the nav needs on every page.

    The extension install page is team-only until the extension is announced,
    and the hub has always shown it in that state. Browse needs the same flag
    for the same reason, and the same gate: once ``EXTENSION_PAGE_PUBLIC`` is
    set the page belongs on the public Tools hub and drops out of the pipeline's
    own menu rather than being listed twice.
    """
    return {"nav_extension_preview":
            not getattr(settings, "EXTENSION_PAGE_PUBLIC", False)}


def account_chrome(request):
    """Whether an account page is being visited from the pipeline.

    The account pages — change password, email addresses, connected accounts,
    sign-in — are django-allauth's and are shared with the Academy, so they
    cannot extend ``pipeline/base.html``. They drew the *public website's*
    header instead: Home, About Us, Publications, Roadmap, Partners, Contact,
    My Account, Log Out. So pressing "Change password" on the sessions board
    landed a bench scientist on a page whose every link went to the marketing
    site, with nothing on it belonging to the app they were working in.

    The pipeline nav sends its own path as ``?next=``, which is already how
    those pages know where to return you. That is the whole signal, and it is
    read from **POST as well as GET**: allauth renders ``next`` as a hidden
    field, so a password change that fails validation re-renders from the POST
    body — where reading the query string alone would silently change the page's
    chrome between the attempt and the error.

    The value is checked rather than trusted. It is drawn in an ``href``, so it
    must be this site's own path: a leading ``/pipeline/`` is same-origin by
    construction, and ``//evil.example/pipeline/`` does not match it.

    The password-reset chain deliberately does **not** get this. allauth's
    "Forgot your password?" link carries no ``next``, and adding one would not
    only restyle the page: ``NextRedirectMixin.get_success_url`` prefers
    ``next`` over the view's own success URL, so submitting the form would jump
    to the pipeline — past the "check your email" page — and land a signed-out
    person back on sign-in, which reads as the request having failed.
    """
    nxt = ""
    if request.method == "POST":
        nxt = (request.POST.get("next") or "").strip()
    if not nxt:
        nxt = (request.GET.get("next") or "").strip()
    if not nxt.startswith("/pipeline/"):
        nxt = ""
    return {"pipeline_chrome": bool(nxt), "pipeline_next": nxt}


def stylesheet(request):
    """Where ``_styling.html`` should fetch the stylesheet from.

    A processor rather than something the template works out, for the same
    reason as ``nav_gene``: the answer is a staticfiles lookup, and a template
    has no way to ask that question. ``pipeline/styling.py`` holds the decision
    and caches it.
    """
    from pipeline.styling import built_stylesheet_url

    return {"pipeline_stylesheet": built_stylesheet_url()}
