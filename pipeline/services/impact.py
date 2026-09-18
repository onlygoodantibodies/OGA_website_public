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

**Two of the sections are about things that are not this application.** The MCP
connector runs as its own Render service and reports each tool call back here
(`core/mcp_usage.py`), because its own log is discarded after seven days; gene
requests are strangers asking for work rather than work done. Both are counted
the same way as everything else — a row the application wrote because somebody
did something — and both say on the page what they are.

**What is still not counted, and cannot honestly be.** Browser-extension
installs: the extension fetches two JSON files a day, which was the closest
thing to an install count until Cloudflare began caching them at the edge on
30 Aug 2026, and the origin now sees whatever the edge did not absorb. Drawing
that as a series would draw the *fix* as a collapse in usage. Cloudflare's own
analytics is where that question goes.

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

from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncMonth
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


def _daily_series(rows, days=RECENT_DAYS):
    """A day counter's last ``days`` days, oldest first, gaps filled with zero.

    Every counter on this page is one row per thing per day, which means a quiet
    day has **no row** — and a chart drawn straight from the rows closes the gap
    up, so a fortnight of silence and a fortnight of steady use draw the same
    shape. The zeros are the point: they are what makes "over time" a series
    rather than a list of the days something happened.

    Aggregated in SQL and filled in Python over a fixed ``days``-long window, so
    the cost is one query and one bounded loop however much traffic there is.
    """
    start = timezone.localdate() - timedelta(days=days - 1)
    totals = {row['date']: row['n'] for row in
              rows.filter(date__gte=start)
                  .values('date')
                  .annotate(n=Sum('count'))}
    return [{'date': start + timedelta(days=i),
             'n': totals.get(start + timedelta(days=i), 0)}
            for i in range(days)]


def _busiest(series):
    """The tallest bar, for ``widthratio``. Never zero — a chart whose maximum
    is zero divides by it, and every bar on an empty chart is empty anyway."""
    return max([row['n'] for row in series] or [0]) or 1


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

    # Ours is not reach. Internal keys — our own testing, and the ones set up to
    # run a demo — come out of every figure below and are counted apart, never
    # dropped: a headline that silently swallowed them could not tell a quiet
    # month from a broken counter.
    everything = ApiUsageDay.objects.all()
    internal = everything.filter(consumer__is_internal=True)
    # Spelled as an explicit OR, and NOT because `exclude(consumer__is_internal=
    # True)` is broken here — it was checked both ways and both keep the keyless
    # rows, since Django adds an `IS NULL` guard when it excludes across a
    # *forward* FK. The trap this file already documents is the reverse-relation
    # one (`?site=none` needing `exclude(field__isnull=False)`), and the two look
    # identical in a diff. The OR says which of the two this is without the
    # reader having to know how Django builds the join — and `keyless_requests`
    # is a headline number, so it is worth one line of clarity. `tests_impact.py`
    # pins the behaviour rather than the spelling.
    rows = everything.filter(Q(consumer__isnull=True)
                             | Q(consumer__is_internal=False))
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

    # The same rows read as a shape rather than a total. The page had four
    # all-time numbers and no way to see whether any of them was still moving,
    # which is the question an impact case actually asks.
    series = _daily_series(rows)

    return {
        'total_requests': total,
        'series': series,
        'busiest_day': _busiest(series),
        'keyless_requests': keyless,
        'keyed_requests': total - keyless,
        'recent_requests': recent.aggregate(n=Sum('count'))['n'] or 0,
        'by_endpoint': by_endpoint,
        'by_consumer': by_consumer,
        'organisations': APIConsumer.objects.filter(
            is_active=True, is_internal=False).count(),
        'organisations_ever': APIConsumer.objects.filter(
            is_internal=False).count(),
        # What was held back, so the reader can see the size of the exclusion
        # rather than take the headline on trust.
        'internal_requests': internal.aggregate(n=Sum('count'))['n'] or 0,
        'internal_keys': APIConsumer.objects.filter(is_internal=True).count(),
        # A consumer with a key who has never called is a partnership that was
        # set up and not taken up, which is a different fact from a quiet month
        # and the one an impact case should not quietly fold in.
        'organisations_active': (
            APIConsumer.objects.filter(is_active=True, is_internal=False,
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
    internal = rows.filter(consumer__is_internal=True)
    keyed = rows.filter(consumer__isnull=False).exclude(
        consumer__is_internal=True)
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
        .exclude(consumer__is_internal=True)
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
        'internal_connects': internal.aggregate(n=Sum('count'))['n'] or 0,
    }


def mcp_server():
    """Tool calls the hosted MCP connector served, and by which client.

    **This is the only durable record that it is used at all.** The connector is
    its own Render service, so nothing the website writes ever sees a call; its
    log is the only other evidence and Render keeps seven days of it. The WorkOS
    user list looks like an answer and is not one — it counts *accounts*, so
    somebody who signed in once and never asked a question sits in it beside a
    partner using it daily, and anybody connecting with a bearer token instead
    of the hosted login is not in it at all.

    **Zero is not the same as nothing, and the page must not print one for the
    other.** These rows exist only because the connector reports them, so when
    the reporting token is unset there is no counting happening and an empty
    table means *nobody is writing this down*. ``reporting`` carries that
    distinction to the template; the flag is the setting the website checks,
    which is one half of a pair — the connector needs the same token set on its
    own service, and nothing here can see whether it is.

    **A call is a question asked, not an answer served.** The reporter counts
    before the tool runs, so a call that then failed is in here. That is the
    honest direction for a usage number and the wrong one for a reliability
    number, which this is not.

    **A client is software, never a person.** ``claude-ai``, ``claude-code``,
    ``chatgpt`` — what the client calls itself when it connects. The connector
    knows an OAuth subject and deliberately never sends it, so this counts reach
    and holds no identities.
    """
    from django.conf import settings

    from core.models import McpUsageDay

    # Same rule as the API's internal keys: our own calls come out of the
    # headline and are printed underneath. The connector decides which are ours
    # by comparing the caller's OAuth subject against a configured list and
    # sending a boolean, so this reads a flag and never an identity.
    everything = McpUsageDay.objects.all()
    ours = everything.filter(internal=True)
    rows = everything.filter(internal=False)
    recent = rows.filter(date__gte=_recent_cutoff().date())
    series = _daily_series(rows)

    return {
        'calls': rows.aggregate(n=Sum('count'))['n'] or 0,
        'calls_recent': recent.aggregate(n=Sum('count'))['n'] or 0,
        'days': rows.values('date').distinct().count(),
        'series': series,
        'busiest_day': _busiest(series),
        'first_seen': rows.aggregate(d=Min('date'))['d'],
        'last_seen': rows.aggregate(d=Max('date'))['d'],
        'by_tool': list(rows.values('tool')
                            .annotate(calls=Sum('count'))
                            .order_by('-calls')[:12]),
        # A client that did not name itself is counted and shown as that, not
        # folded into a named one and not dropped: "we do not know" is a fact
        # about the connector's callers worth seeing beside the ones we do.
        'by_client': list(rows.values('client')
                              .annotate(calls=Sum('count'))
                              .order_by('-calls')[:10]),
        'clients': rows.exclude(client='').values('client').distinct().count(),
        'reporting': bool(settings.MCP_USAGE_TOKEN),
        'internal_calls': ours.aggregate(n=Sum('count'))['n'] or 0,
    }


def gene_demand():
    """Genes strangers asked us to characterise — demand, not commitment.

    A ``GeneRequest`` is somebody outside the consortium saying they could not
    find their gene, and its model docstring is emphatic that this is never a
    ``TargetNomination``: a nomination is funded internal work, this is a wish.
    They are counted here for the reason the requests exist at all — twelve
    people asking for one gene is the single most useful prioritisation signal
    this project collects, and until it was drawn somewhere it could be quoted
    it was a table nobody opened.

    Grouped through ``gene_requests.by_gene()`` rather than a second
    ``values().annotate()``, because that is the one reader for what a group of
    requests is, and two groupers is how the staff page and this one come to
    print different numbers for one gene.

    **By month, not by day.** These arrive in ones and twos; a daily chart of a
    handful of requests is a row of empty tracks that says nothing.
    """
    from pipeline.models import GeneRequest
    from pipeline.services import gene_requests as GR

    rows = GeneRequest.objects.all()
    total = rows.count()
    groups = GR.by_gene()

    months = {row['m'].date() if hasattr(row['m'], 'date') else row['m']: row['n']
              for row in rows.annotate(m=TruncMonth('created_at'))
                             .values('m')
                             .annotate(n=Count('id'))}
    series = _fill_months(months)

    return {
        'requests': total,
        'requests_recent': rows.filter(
            created_at__gte=_recent_cutoff()).count(),
        # The distinct thing asked for, which is what a priority list is made
        # of — and never the same number as the requests, since the whole value
        # of these rows is that one gene can be asked for many times.
        'genes': len(groups),
        'top': groups[:10],
        # A request offering to bring money is a different conversation from a
        # wish, which is why the public form asks. Counted apart for that reason.
        'with_funding': rows.filter(has_funding=True).count(),
        # Already on the target list: the request was acted on, or the gene was
        # here before the asking. Either way it is not outstanding demand.
        'already_here': rows.filter(target__isnull=False).count(),
        'series': series,
        'busiest_month': max([r['n'] for r in series] or [0]) or 1,
    }


def _fill_months(totals):
    """Every month from the first with a request to this one, zeros included.

    Same reasoning as ``_daily_series``: months with nothing in them are what
    turn a list of events into a trend, and closing the gaps up draws a steady
    trickle exactly like a burst.

    **The last month is marked partial, because it always is.** A month one day
    old draws a bar next to complete ones and reads as a collapse in demand —
    which is not a hypothetical: on 1 Sep 2026 this chart showed 27 for August
    and 0 for September, and the first person to look at it read that as a drop
    worth investigating. The month is not short of requests, it is short of
    days. Same family as the browser-extension series this page refuses to draw:
    a number whose denominator changed is not a trend.
    """
    if not totals:
        return []
    this_month = timezone.localdate().replace(day=1)
    first = min(min(totals), this_month)
    out, cursor = [], first
    while cursor <= this_month:
        out.append({'month': cursor, 'n': totals.get(cursor, 0),
                    'partial': cursor == this_month})
        year, month = divmod(cursor.month, 12)
        cursor = cursor.replace(year=cursor.year + year, month=month + 1)
    return out


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
        'mcp': mcp_server,
        'demand': gene_demand,
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
