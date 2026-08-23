"""What the site has recorded people doing — the numbers for an impact case.

Everything here was reachable only through Django admin, one model at a time,
which means it existed and nobody could quote it. A grant application or a
funder report wants "the API served N requests to M organisations, we issued K
workshop credentials across J institutions" — and assembling that meant knowing
which of four apps to open and holding the arithmetic in your head.

**What this cannot tell you, and says so on the page.** There is no page-view
analytics anywhere in Django: visits, unique visitors and referrers live in
Cloudflare, which is a different account and a different question. Everything
below is something the *application* wrote down because a person did it — a
certificate issued, a plan recorded, a request served. That is a narrower and
harder number than a page view, and for an impact case it is the better one,
but a reader who takes it for "traffic" will read it as far too small.

Three things about the shape.

**Aggregated in SQL, never in Python.** Every figure here is a ``Count`` or a
``Sum`` over a whole table, so the page costs a fixed handful of queries
whatever the data does. The rule this repo keeps meeting is that a page dies on
the full dataset while looking fine on a handful of dev rows, and a reporting
page is the most tempting place to write a loop.

**Counts and their lists do not disagree, because each count names what it
counted.** A bare number on a reporting page is the shape somebody quotes in a
grant, so each one carries the noun it is counting and, where it can, the
breakdown underneath — the Portfolio's rule about three correct answers to three
different questions.

**Nothing personal is drawn.** `credentials.Certificate` holds names and
institutional emails and `SelectionRecord` holds both plus an affiliation; this
module reads *counts* and email **domains**, never a person. That keeps the page
quotable in a report without deciding anybody else's privacy question.

The cross-database part is free: the router sends everything except the pipeline
models and the five legacy `core` tables to ``academy_db``, so plain ORM calls
land in the right place with no ``using()`` anywhere.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Max, Min, Sum
from django.utils import timezone

#: The window the "recently" figures use. A month is long enough that a quiet
#: fortnight does not read as a dead service, and short enough to show a change.
RECENT_DAYS = 30

#: What a portal sign-in looks like in the counter. The portal's front end calls
#: ``GET /api/v1/status/`` on connect and nowhere else (`core/templates/core/
#: portal.html::connect`), so the day's count of this endpoint *is* that
#: organisation's connects. It is the URL **name**, because that is what
#: `core/api_usage.py::endpoint_of` writes down.
#:
#: Spelled here and routed in `core/api_urls.py`, which is two places for one
#: fact — so `tests_impact.py` resolves the endpoint and asserts they agree. A
#: route renamed with this left behind would not raise: it would report zero
#: sign-ins, for ever, which reads as nobody using the portal.
PORTAL_CONNECT_ENDPOINT = 'api_status'


def _recent_cutoff():
    return timezone.now() - timedelta(days=RECENT_DAYS)


def api_usage():
    """Requests served, split by who asked and what they asked for.

    ``ApiUsageDay`` counts requests that were **allowed** — a throttled caller
    is refused in the cache and never reaches the counter — so these understate
    a runaway client and are honest about everybody else. Its own docstring has
    the reasoning.

    The keyless split is the number worth watching now. Before 12 Aug 2026 a
    keyless request was a 401 being counted as "somebody knocking"; since the
    read feeds opened it is somebody being served, and the same rows count both,
    which is exactly why the counter was built before the door was opened.
    """
    from core.models import APIConsumer, ApiUsageDay

    rows = ApiUsageDay.objects.all()
    recent = rows.filter(date__gte=_recent_cutoff().date())

    total = rows.aggregate(n=Sum('count'))['n'] or 0
    keyless = rows.filter(consumer__isnull=True).aggregate(
        n=Sum('count'))['n'] or 0

    by_endpoint = list(
        rows.values('endpoint')
            .annotate(requests=Sum('count'))
            .order_by('-requests')[:10])

    # Named callers only: a keyless row has no consumer to name, and putting an
    # "— no key —" entry in a *organisations* list would count nobody as one.
    #
    # Grouped on `consumer_id` and not on the name. `APIConsumer.name` carries no
    # unique constraint and the realistic duplicate is the one the `gene_filter`
    # help text describes — a trial key and a paid key for one manufacturer —
    # which grouped by name merged into a single row whose total belonged to
    # neither. The id is also what the row links to, and a name is only a way to
    # a page when the row carries the id it would link to.
    by_consumer = list(
        rows.filter(consumer__isnull=False)
            .values('consumer_id', 'consumer__name', 'consumer__consumer_type')
            .annotate(requests=Sum('count'))
            .order_by('-requests')[:10])

    return {
        'total_requests': total,
        'keyless_requests': keyless,
        'keyed_requests': total - keyless,
        'recent_requests': recent.aggregate(n=Sum('count'))['n'] or 0,
        'by_endpoint': by_endpoint,
        'by_consumer': by_consumer,
        'organisations': APIConsumer.objects.filter(is_active=True).count(),
        'organisations_ever': APIConsumer.objects.count(),
        # A consumer with a key who has never called is a partnership that was
        # set up and not taken up, which is a different fact from a quiet month
        # and the one an impact case should not quietly fold in.
        'organisations_active': (
            APIConsumer.objects.filter(is_active=True,
                                       usage_days__isnull=False)
            .distinct().count()),
    }


def portal_sessions():
    """Who signed in to the supplier portal with their key, and when.

    The rows for this were already being written — a portal connect is a keyed
    request to one endpoint, and `ApiUsageDay` has counted every one since it
    was built. What was missing is that nothing read them *as sign-ins*: the
    section above draws `api_status` as one line in a list of endpoints, beside
    a bulk feed nobody logs in to fetch, so "has this manufacturer ever opened
    the portal we built them?" was a question the numbers could answer and the
    page could not.

    **A connect is a connect, not a person and not a session.** The portal
    reconnects from `sessionStorage` on every page load, so a reload counts
    again; two people at one manufacturer sharing a key count as one
    organisation. The distinct-**days** column is the steadier number and is
    what a report should quote — "in on 14 separate days" survives a reader
    refreshing, where "112 sign-ins" does not.

    **Day granularity, deliberately.** The counter keeps one row per consumer
    per endpoint per day precisely so its size is bounded by who holds a key
    rather than by how hard anybody pulls, and that trade buys the dates and
    gives up the times. There is no "signed in at 14:03" here and there is not
    going to be one without a per-request log.

    **A refused sign-in cannot be attributed.** A wrong or inactive key is
    counted keyless, because the row is keyed on a consumer the request failed
    to identify — so failed attempts are a total and never a name. That total is
    worth drawing anyway: it separates "nobody has tried" from "somebody is
    trying with a key that no longer works", which is a support question.
    """
    from core.models import ApiUsageDay

    rows = ApiUsageDay.objects.filter(endpoint=PORTAL_CONNECT_ENDPOINT)
    keyed = rows.filter(consumer__isnull=False)
    recent = keyed.filter(date__gte=_recent_cutoff().date())

    by_consumer = list(
        keyed.values('consumer_id', 'consumer__name',
                     'consumer__consumer_type', 'consumer__tier')
             .annotate(connects=Sum('count'),
                       days=Count('date', distinct=True),
                       first_seen=Min('date'),
                       last_seen=Max('date'))
             .order_by('-days', '-connects')[:20])

    # When each of them last called the API *at all*. One aggregate over the
    # whole table, joined to the rows above by id in Python — a dict lookup per
    # organisation, which is bounded by who holds a key and not by traffic.
    # It is the column that says which kind of partner this is: a last_seen far
    # behind last_call is somebody whose script still syncs and whose people
    # stopped opening the portal, and that is not visible from either number
    # alone.
    last_call = dict(
        ApiUsageDay.objects.filter(consumer__isnull=False)
        .values_list('consumer_id')
        .annotate(last=Max('date'))
        .values_list('consumer_id', 'last'))
    for row in by_consumer:
        row['last_call'] = last_call.get(row['consumer_id'])

    return {
        'connects': keyed.aggregate(n=Sum('count'))['n'] or 0,
        'connects_recent': recent.aggregate(n=Sum('count'))['n'] or 0,
        'organisations': keyed.values('consumer_id').distinct().count(),
        'organisations_recent': recent.values('consumer_id').distinct().count(),
        'days': keyed.values('date').distinct().count(),
        'by_consumer': by_consumer,
        # Sign-in attempts carrying no usable key: no key at all, a key that is
        # not on file, or one whose consumer has been deactivated. All three
        # land here because none of them identifies a caller.
        'refused': (rows.filter(consumer__isnull=True)
                        .aggregate(n=Sum('count'))['n'] or 0),
    }


def academy():
    """Learning: who finished something, and how much.

    ``LessonProgress`` rows exist for lessons merely *started*, so `completed`
    is filtered explicitly — a started lesson is engagement and a finished one
    is the impact claim, and reporting the first as the second is the shape of
    every overstated number in this repo.
    """
    from academy.models import AIChatUsage, Certificate, LessonProgress

    completed = LessonProgress.objects.filter(completed=True)
    return {
        'certificates': Certificate.objects.count(),
        'certificates_recent': Certificate.objects.filter(
            issued_at__gte=_recent_cutoff()).count(),
        'lessons_completed': completed.count(),
        'learners': completed.values('user').distinct().count(),
        'learners_started': LessonProgress.objects.values(
            'user').distinct().count(),
        'by_lesson': list(
            completed.values('lesson__title')
                     .annotate(finished=Count('id'))
                     .order_by('-finished')[:10]),
        'ai_messages': AIChatUsage.objects.aggregate(
            n=Sum('message_count'))['n'] or 0,
    }


def workshop_credentials():
    """Credentials issued, and how many institutions they reached.

    Institutions are counted by **email domain**, which is the only affiliation
    a credential carries. It is an approximation and named as one on the page:
    two people at one university with different domains count twice, and a
    department that uses a shared address counts once.
    """
    from credentials.models import Certificate, PendingClaim

    certs = Certificate.objects.all()
    domains = (certs.exclude(institutional_email='')
                    .values_list('institutional_email', flat=True))
    institutions = {e.split('@')[-1].lower() for e in domains if '@' in e}

    return {
        'issued': certs.count(),
        'issued_recent': certs.filter(issued_at__gte=_recent_cutoff()).count(),
        'by_kind': list(certs.values('kind')
                             .annotate(n=Count('id'))
                             .order_by('-n')),
        'institutions': len(institutions),
        # Claims started and never verified: people who began and dropped out,
        # which is a number worth seeing beside the successes rather than
        # buried.
        'claims_unverified': PendingClaim.objects.filter(
            verified_at__isnull=True).count(),
    }


def selection_tool():
    """Validation plans people built, and how many are funder-reportable.

    ``is_compliance`` is the distinction that matters for an impact case: a
    personal plan is somebody helping themselves, a compliance record is
    attributable to a named person at a named institution and can be handed to
    a funder as evidence. They are counted apart for that reason.
    """
    from selector.models import SelectionRecord

    records = SelectionRecord.objects.all()
    institutions = {i.strip().lower() for i in
                    records.exclude(institution='')
                           .values_list('institution', flat=True) if i.strip()}

    return {
        'plans': records.count(),
        'plans_recent': records.filter(
            created_at__gte=_recent_cutoff()).count(),
        'compliance': records.filter(is_compliance=True).count(),
        'completed': records.filter(completed=True).count(),
        'institutions': len(institutions),
        'by_application': list(
            records.exclude(application='')
                   .values('application')
                   .annotate(n=Count('id'))
                   .order_by('-n')[:10]),
        'by_gene': list(
            records.exclude(target_gene='')
                   .values('target_gene')
                   .annotate(n=Count('id'))
                   .order_by('-n')[:10]),
    }


def dataset():
    """What there is to have an impact with — the published set.

    Counted the way the public pages count it (``pipeline/public.py``), so this
    page and the home page cannot report different totals: an antibody is public
    when it carries a published figure, and a gene is public when it has one of
    those antibodies.
    """
    from pipeline.public import headline_counts

    # The home page's own reader, not a copy of its arithmetic — the copy had
    # already drifted from it in one of the three numbers.
    counts = headline_counts()
    return {
        'genes': counts['gene_count'],
        'antibodies': counts['antibody_count'],
        'figures': counts['experiment_count'],
    }


def everything():
    """Every section, for the one page that draws them.

    Each block is its own function so a section that throws — a model moved, an
    app removed — can be caught per section rather than taking the page down.
    That matters here more than usual: this reads four apps it does not own, and
    a reporting page nobody can open is worse than one with a gap in it.
    """
    sections = {
        'api': api_usage,
        'portal': portal_sessions,
        'academy': academy,
        'credentials': workshop_credentials,
        'selection': selection_tool,
        'dataset': dataset,
    }
    out, failed = {}, []
    for name, fn in sections.items():
        try:
            out[name] = fn()
        except Exception:  # noqa: BLE001 — a broken section must not 500 the page
            out[name] = None
            failed.append(name)
    out['failed_sections'] = failed
    out['recent_days'] = RECENT_DAYS
    return out
