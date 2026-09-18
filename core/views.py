"""
OGA Public Views — core/views.py
=================================

Phase 3 rewrite: all data reads come from pipeline PostgreSQL
(Target, Antibody, PublicationImage, Report, Company) instead
of core SQLite (Gene, Antibody, Description, Experiment). The legacy layer
and its `default` SQLite database were retired on 23 Aug 2026; the models
that remain in `core` — APIConsumer, ReviewedAntibody, ApiUsageDay — all
route to academy_db.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.core.mail import send_mail, get_connection, EmailMessage
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.db.models import Count, F, Q
from django.utils.http import urlencode

from pathlib import Path

import random
import json
import logging

logger = logging.getLogger(__name__)

# Pipeline models — primary data source
from pipeline.models import (
    Target, Antibody as PipelineAntibody, Company as PipelineCompany,
    PublicationImage, Report,
)
# One definition of "a gene the public site will show" — see pipeline/public.py.
from pipeline.public import (headline_counts, public_targets,
                             published_antibodies)
# What a vial's clonality is, from the enum and `is_recombinant` together —
# neither column answers it alone. See pipeline/services/clonality.py.
from pipeline.services import clonality as clonality_svc
# Catalogue number / RRID / clone lookup — see core/public_search.py.
from . import public_search
# Frozen id → name map behind the legacy numeric gene URLs.
from .legacy_gene_ids import LEGACY_GENE_NAMES

# The three-valued recommendation reader, the scope caveat and the licence —
# one definition, shared with the API, the extension index and the MCP server.
from . import recommendations as R
from . import target_confusions


# Browser-extension snapshot: rebuilt at most once an hour. The extension only
# refreshes daily, so this mostly protects against crawlers hitting the endpoint.
# _v2 because what is stored under it changed from a dict to the serialised
# bytes. LocMem forgets on every deploy so it would never notice, but a shared
# Redis cache (CACHE_URL) keeps its contents across one -- and the old value
# handed to the new code is a dict where bytes are expected.
# Owned by core/extension_index.py, which also clears the key on a write. Kept
# importable from here because that is where they have always been read from.
from .extension_index import (                                  # noqa: E402
    EXTENSION_INDEX_CACHE_KEY, EXTENSION_INDEX_CACHE_SECONDS)

# The citation snapshot is a static file, not a database read, so it is cached for
# a day rather than an hour -- and it is served SEPARATELY from index.json on
# purpose. Folding it in would add ~1.3 MB to a payload every installed extension
# already downloads daily, including the ones too old to use it, and `schema` on
# index.json is checked twice in background.js so it cannot be bumped without
# stranding every extension in the field on its bundled dev fixture.
EXTENSION_CITATIONS_CACHE_SECONDS = 86400

# Gene name/alias list behind the header search box. Small and rarely changing
# (a new gene appears only when a figure is published), so a long cache is fine.
GENE_SEARCH_INDEX_CACHE_KEY = 'gene_search_index_v1'
GENE_SEARCH_INDEX_CACHE_SECONDS = 3600


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _get_report_link(target):
    """
    Resolve the best available report link for a target.
    Priority: (1) Report.f1000_doi, (2) Report.zenodo_doi.
    Returns (url, label) tuple.

    There used to be a third source — the retired ``core.Gene.f1000_report_link``
    column in the git-deployed SQLite. It was measured before removal on
    23 Aug 2026: of the 155 public targets, 154 carry a pipeline ``Report`` with
    a DOI and never reached the fallback, and the one that does not (RAB5B) had
    no row in the legacy table either. So the fallback returned nothing for
    every public gene, and dropping it changes no page.
    """
    report = target.reports.filter(
        Q(f1000_doi__gt='') | Q(zenodo_doi__gt='')
    ).order_by('-f1000_date', '-zenodo_date').first()

    if report:
        if report.f1000_doi:
            return report.f1000_doi, 'F1000 Report'
        if report.zenodo_doi:
            return report.zenodo_doi, 'Zenodo Report'

    return None, 'Full Report'


def _gene_jsonld(request, target, published_total, report_link):
    """schema.org ``Dataset`` for one gene's page, as a JSON string.

    The page states its results in a table built for a person to read. A machine
    arriving at it — Google Dataset Search, a crawler assembling a corpus, an
    assistant asked whether a reagent is any good — has to infer the whole thing
    from markup, and mostly does not. This says it outright: what the dataset
    is, who made it, what licence it carries and **which DOI to cite**.

    The DOI is the point of it. Reuse is free under CC BY 4.0 and what is owed
    in return is a citation of the report for the gene, so the one field a
    reuser most needs is the one hardest to find by reading a table. A gene with
    no report published yet simply omits it rather than citing the site: there
    is nothing to cite, and inventing an identifier is worse than having none.

    ``published_total`` is the gene's published antibodies, not the rows drawn:
    a Dataset description is a claim about the dataset, and this one was built
    from the filtered list, so ``?discontinued=show`` and ``?host=Rabbit``
    described two different datasets under one ``url``. Same reasoning as the
    ``<title>`` two lines away in the template.
    """
    aliases = [a.strip() for a in (target.aliases or '').split(',') if a.strip()]

    doc = {
        '@context': 'https://schema.org',
        '@type': 'Dataset',
        'name': f'{target.gene_name} antibody characterisation — '
                f'knockout-controlled results',
        'description': (
            f'Knockout-controlled characterisation of '
            f'{published_total} commercially available {target.gene_name} '
            f'antibodies, tested by YCharOS for western blot, '
            f'immunoprecipitation, immunocytochemistry/immunofluorescence and '
            f'flow cytometry under community consensus protocols. '
            f'{R.SCOPE_NOTE}'),
        'url': request.build_absolute_uri(),
        'license': R.LICENCE_URL,
        'isAccessibleForFree': True,
        'creator': {
            '@type': 'Organization',
            'name': 'YCharOS',
            'url': 'https://onlygoodantibodies.co.uk/about/',
        },
        'publisher': {
            '@type': 'Organization',
            'name': 'Only Good Antibodies',
            'url': 'https://onlygoodantibodies.co.uk/',
        },
        'keywords': [target.gene_name] + aliases + [
            'antibody validation', 'antibody characterisation',
            'knockout control', 'research reagent',
        ],
        'measurementTechnique': [_APP_TECHNIQUE[app] for app in R.APPLICATIONS],
        # The value domain, once per variable and stated as the three values
        # rather than as three sentences — R.MEANINGS spells them out for a
        # person, and repeating that paragraph four times in one block is
        # padding a machine has to read past.
        'variableMeasured': [
            {'@type': 'PropertyValue',
             'name': f'{_APP_TECHNIQUE[app]} recommendation',
             'description': 'One of: ' + ', '.join(
                 (R.RECOMMENDED, R.NOT_RECOMMENDED, R.NOT_TESTED))}
            for app in R.APPLICATIONS
        ],
    }

    if target.protein_name:
        doc['about'] = {'@type': 'Thing', 'name': target.protein_name}

    # The DOI is both how the dataset is identified and what a reuser must cite,
    # so it goes in both fields rather than one — a reader looking for either
    # finds it.
    if report_link:
        doc['identifier'] = report_link
        doc['citation'] = report_link

    # `</script>` inside the block ends it early, so every `<` becomes its JSON
    # escape — that is what stops a supplier name or a free-text field from
    # closing the block. A `<` can only ever appear inside a string literal in
    # JSON, so this cannot touch the structure.
    return json.dumps(doc, ensure_ascii=False).replace('<', '\\u003c')


# The four applications as the gene-page template spells them. `experiments` and
# `recommended` are drawn in the same four cells, so they are keyed alike — a
# green wash that had to be looked up under a different name than the image it
# sits behind is how the two drift apart.
_TEMPLATE_APP = {'WB': 'WB', 'IP': 'IP', 'ICC-IF': 'ICC_IF', 'FC': 'FC'}

# The same four as the client-side application filter's `<option>` values.
_FILTER_APP = {'WB': 'wb', 'IP': 'ip', 'ICC-IF': 'icc_if', 'FC': 'fc'}

# What each is called in the "Recommended Applications:" line. These were in the
# page's JavaScript; they are here now because the line is rendered on the
# server, and R.APPLICATIONS decides the order — the order every OGA surface
# prints them in, which this page was the odd one out on.
_APP_LABEL = {'WB': 'Western Blot', 'IP': 'IP',
              'ICC-IF': 'ICC / IF', 'FC': 'Flow Cytometry'}

# What the four applications are, spelled out for a machine reading the JSON-LD
# rather than for somebody who already knows the abbreviations.
_APP_TECHNIQUE = {
    'WB': 'Western blot',
    'IP': 'Immunoprecipitation',
    'ICC-IF': 'Immunocytochemistry / immunofluorescence',
    'FC': 'Flow cytometry',
}


def _supplier_name(antibody):
    """Return the OGA canonical supplier name for a pipeline Antibody."""
    if antibody.company:
        return antibody.company.display_name or antibody.company.name
    return None


def _target_has_recommendations(target):
    """True if any antibody for this target has any recommendation set."""
    return PipelineAntibody.objects.filter(target=target).filter(
        Q(wb_recommended=True) | Q(ip_recommended=True)
        | Q(if_recommended=True) | Q(fc_recommended=True)
    ).exists()


# ─────────────────────────────────────────────────────────
# Static / info pages (unchanged)
# ─────────────────────────────────────────────────────────

# Crawlers that take bandwidth and send nobody back. These are backlink and
# SEO-metrics services selling reports about other people's sites; a scientist
# looking for an antibody does not arrive from any of them. Sampled over a
# single hour on 20 Aug 2026, roughly nine in ten requests to this site were
# automated, and this list was the freeloading half of them.
#
# Deliberately NOT here: Googlebot, Bingbot and DuckDuckBot, which are how
# people find a gene page; and the AI crawlers (ClaudeBot, GPTBot,
# ChatGPT-User, PerplexityBot, OAI-SearchBot), because a model answering an
# antibody question from this data is the point of the project -- it is what
# `mcp_servers/` exists to do, and blocking the same data at the front door
# would work against it.
#
# This is a request, not a control. Well-run crawlers honour it; some of the
# names below are documented to ignore robots.txt entirely, and the only answer
# to those is a rule at the edge (a CDN or Render's firewall), not a text file.
FREELOADING_CRAWLERS = [
    "AhrefsBot",
    "Amazonbot",
    "Barkrowler",
    "BLEXBot",
    "Bytespider",
    "DataForSeoBot",
    "DotBot",
    "MJ12bot",
    "MegaIndex",
    "PetalBot",
    "SemrushBot",
    "serpstatbot",
    "ZoominfoBot",
]

# Paths worth no crawler's time, and expensive when walked. /static/pipeline/
# is the lab tool's own assets -- 18 MB of it is the Tesseract OCR bundle the
# figure cropper loads in the browser -- and /pipeline/ itself answers every
# anonymous request with a redirect to sign in.
CRAWLER_DISALLOWED_PATHS = [
    "/api/",
    "/embed/",
    "/portal/",
    "/admin/",
    "/admin-tools/",
    "/accounts/",
    "/pipeline/",
    "/static/pipeline/",
]


def robots_txt(request):
    """What crawlers may take, and how fast.

    Bandwidth here is almost entirely crawler traffic, so this file is the
    cheapest lever on the bill -- but it only ever asks. See the two lists
    above for who is turned away and, more importantly, who is not.
    """
    lines = []
    for agent in FREELOADING_CRAWLERS:
        lines += [f"User-agent: {agent}", "Disallow: /", ""]
    lines += ["User-agent: *", "Allow: /"]
    lines += [f"Disallow: {path}" for path in CRAWLER_DISALLOWED_PATHS]
    lines += [
        # Seconds between requests, for the crawlers that read it. The site is
        # one small instance and nothing on it changes minute to minute.
        "Crawl-delay: 10",
        "",
        "Sitemap: https://onlygoodantibodies.co.uk/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def about(request):
    # The same reader the home page uses. This was a hand copy of the counting
    # expression, so the two hero numbers on About were a second implementation
    # of the home page's headline — right until one of them was edited.
    return render(request, 'core/about.html', get_live_stats())


def publications(request):
    return redirect('news_publications', permanent=True)


def news_publications(request):
    return render(request, 'core/news_publications.html')


def partners(request):
    return render(request, 'core/partners.html')


def contact(request):
    """General enquiries. **Not a way to nominate a gene any more.**

    It used to be: an enquiry type of "Suggest a Target" with a gene, an
    application and a details box, all of which became an email and nothing
    else. So every gene anybody has ever asked for is prose in an inbox, and
    the one question worth asking of it — which gene are the most people
    waiting for? — cannot be answered at all.

    ``nominate_gene`` replaced it, and this route is *removed* rather than
    quietly deprioritised. Two doors to one thing means half the requests keep
    arriving in the form that cannot be counted, and nothing on either page
    would say so.

    The old field names are still read, deliberately: a bookmarked form or a
    tab left open overnight posts them, and dropping them on the floor would
    turn a submission somebody watched succeed into a blank message.
    """
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        message = request.POST.get('message', '').strip()
        # Only ever set by a stale copy of the old form — see the docstring.
        stale = [(label, request.POST.get(field, '').strip()) for label, field in
                 (('Gene', 'gene'), ('Antibody', 'antibody'),
                  ('Target details', 'target-details'))]

        subject = f'Contact Form Submission from {name}'

        lines = [f'Name: {name}', f'Email: {email}']
        for label, value in stale:
            if value:
                lines.append(f'{label}: {value}')
        lines.append('')
        lines.append('Message:')
        lines.append(message)
        full_message = '\n'.join(lines)

        # Send FROM the authenticated account (Gmail rejects an arbitrary From),
        # and set Reply-To to the visitor so replies reach them.
        mail = EmailMessage(
            subject=subject,
            body=full_message,
            from_email=settings.EMAIL_HOST_USER,
            to=[settings.EMAIL_HOST_USER],
            reply_to=[email] if email else None,
        )
        try:
            mail.send(fail_silently=False)
        except Exception:
            # Don't 500 on a mail-server hiccup or missing credentials — log it
            # and show the same success page so the visitor isn't dead-ended.
            logger.exception('Contact form email failed to send')
        return redirect('success')

    return render(request, 'core/contact.html')


def success(request):
    return render(request, 'core/success.html')


def news(request):
    return redirect('news_publications', permanent=True)


def using_the_data(request):
    return render(request, 'core/using-the-data.html')


def data_access(request):
    """How manufacturers, registries and data partners use the portal and API.

    The portal and the API existed with nothing on the public site pointing at
    them: a partner had to already know to ask. This is the page that answers
    "how do we get at this" without a conversation first -- and it asks for one
    only at the step that genuinely needs it, which is the key.
    """
    return render(request, 'core/data_access.html', {
        'consensus_protocol_url': R.CONSENSUS_PROTOCOL_URL,
        'licence_name': R.LICENCE_NAME,
        'licence_url': R.LICENCE_URL,
    })


def api_reference(request):
    """`API.md`, rendered, at a URL a partner can be sent.

    The full reference — every endpoint, every parameter, and a working sync
    client — existed only as a file in the git repository. `/data-access/`
    introduces the API and `openapi.json` describes it to a machine, but the
    document written for a person to read was reachable by nobody: the same
    export-with-no-importer failure this project keeps meeting, applied to
    documentation. A partner asking "how do I use this" got a page that told
    them the API exists and a JSON schema.

    Rendered from the file on every request rather than converted once into a
    template, so the document and the page cannot drift — the reason CLAUDE.md
    gives for never restating what the code states, one layer out. It is a few
    milliseconds of Markdown on a page nobody hits in a loop.
    """
    import markdown

    path = Path(settings.BASE_DIR, 'API.md')
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        # A missing file must not 500 the page: say so and keep the routes that
        # do work in front of the reader.
        return render(request, 'core/api_reference.html', {
            'body': None,
            'error': 'The reference could not be loaded. The machine-readable '
                     'specification at /api/v1/openapi.json is unaffected.',
        }, status=503)

    return render(request, 'core/api_reference.html', {
        'body': markdown.markdown(
            text, extensions=['fenced_code', 'tables', 'toc']),
        'error': None,
    })


def set_dark_mode(request):
    response = redirect('home')
    response.set_cookie(
        'dark_mode', 'enabled',
        max_age=60 * 60 * 24 * 30,
        secure=True, httponly=True, samesite='Lax',
    )
    return response


def privacy_policy(request):
    return render(request, 'core/privacy_policy.html')


def roadmap(request):
    return render(request, 'core/roadmap.html', get_live_stats())


def champions(request):
    return render(request, 'core/champions.html')


def connect_your_ai(request):
    # The public "connect your AI to the OGA database" page (MCP connector).
    return render(request, 'core/connect_your_ai.html')


def roadmap_institutions(request):
    return render(request, 'core/roadmap_institutions.html')


def roadmap_funders(request):
    return render(request, 'core/roadmap_funders.html')


def roadmap_publishers(request):
    return render(request, 'core/roadmap_publishers.html')


def roadmap_manufacturers(request):
    return render(request, 'core/roadmap_manufacturers.html')


def portal(request):
    """The supplier-facing view of their own reagents.

    Presents results the way a public gene page does (owner, 29 Aug 2026): a
    manufacturer comparing their row here with the page it links to must not
    find two ways of saying one thing. The label comes from the same constant
    the gene page uses rather than being typed into the template, which is the
    whole of why they cannot drift.
    """
    from .recommendations import CONSENSUS_PROTOCOL_URL

    return render(request, 'core/portal.html', {
        'consensus_protocol_url': CONSENSUS_PROTOCOL_URL,
        'supported_applications_label': R.SUPPORTED_APPLICATIONS_LABEL,
    })


# ─────────────────────────────────────────────────────────
# HOME — gene listing (reads from pipeline Target)
# ─────────────────────────────────────────────────────────

def get_live_stats():
    """The genes/antibodies/tests tally, from ``pipeline/public.py``.

    Kept as a name because the home page, About and the roadmap all call it, but
    it holds no arithmetic of its own any more: the three counts are defined
    once, beside the gene rule they belong with, so the API manifest and the
    impact page cannot answer differently. See ``headline_counts`` for why an
    antibody is counted as a record rather than as a distinct catalogue number.
    """
    return headline_counts()


def home(request):
    query = request.GET.get('search', '').strip()

    # Annotate gene_name as 'name' so home.html template's {{ gene.name }} works.
    #
    # `published_antibody_count` is the number on each gene's card, and it is
    # annotated rather than left to the template because `{{ gene.antibodies.count }}`
    # counted **every** antibody on the target, published or not. The cards
    # therefore added up to 1,920 under a headline reading 1,645 — 88 of the 159
    # genes overstated, SOD1 worst at 29 on the card against 11 published (and 9
    # drawn, once the discontinued tick has had its say, which is a separate and
    # deliberate difference). A reader who adds up the front page and gets a
    # bigger number than the front page's own total has been shown the
    # pipeline's private backlog, which `pipeline/public.py` exists to keep off
    # this site.
    #
    # `distinct=True` because the filter joins the figures: three figures on one
    # antibody would otherwise count it three times. It also takes the count off
    # the template, where it was one query per gene.
    targets = (
        public_targets()
        .annotate(name=F('gene_name'))
        .annotate(published_antibody_count=Count(
            'antibodies',
            filter=Q(antibodies__publication_images__isnull=False),
            distinct=True))
        .order_by('name')
    )

    # **The card grid is never filtered, and that is the point of it.** It is a
    # link surface for crawlers and a decorative index of what the site holds —
    # every gene on it has its own sitemap entry and its own page — and it
    # begins ~1,200px below the search box. So narrowing it answered a search
    # somewhere no reader could see: on a 900px screen, pressing Enter moved
    # nothing at all. Worse, a filtered home page is a thinner copy of the home
    # page at a second URL, which is the wrong thing to hand a crawler.
    #
    # The search is answered under the box instead, by `matches` below.
    matches = []
    if query:
        # A catalogue number or an RRID is a thing people paste into this box,
        # and matching gene names alone answered "no results" for reagents we
        # hold. When it names exactly one antibody, go straight to it — that is a
        # resolution, not a guess, and the gene page is where the verdict is.
        single = public_search.resolve(query)
        if single:
            gene_page = reverse(
                'antibody_table', kwargs={'gene_name': single['gene']})
            focus = single['catalogue'] or single['rrid']
            return redirect(f"{gene_page}?{urlencode({'ab': focus})}")
        # Anything less certain is listed as links, so the answer is a thing to
        # click rather than an instruction to go and look. Capped, because this
        # is a line under a search box and not a results page; the count above
        # it is the whole set, so a truncated list never reads as the total.
        matches = list(
            targets.filter(
                Q(gene_name__icontains=query)
                | Q(pk__in=public_search.target_ids(query))
            ).values_list('gene_name', flat=True)
        )

    no_results = bool(query) and not matches

    from .recommendations import CONSENSUS_PROTOCOL_URL

    return render(request, "core/home.html", {
        "genes": targets,
        "search_query": query,
        # The full match list and how many there are — one queryset, so the
        # count and the links under it can never disagree.
        "matches": matches[:12],
        "match_count": len(matches),
        "no_results": no_results,
        "consensus_protocol_url": CONSENSUS_PROTOCOL_URL,
        **get_live_stats(),
    })


# ─────────────────────────────────────────────────────────
# GENE REDIRECT — legacy numeric URLs (/124/ → /antibodies/ARHGDIA/)
# The numeric ids were core.Gene primary keys, and that model is retired. The
# mapping is frozen in core/legacy_gene_ids.py — see its docstring for why the
# addresses are kept and what the 155 of them resolve to.
# ─────────────────────────────────────────────────────────

def gene_redirect(request, gene_id):
    gene_name = LEGACY_GENE_NAMES.get(gene_id)
    if gene_name is None:
        raise Http404('No gene with that legacy id')
    return redirect('antibody_table', gene_name=gene_name, permanent=True)


# ─────────────────────────────────────────────────────────
# ANTIBODY TABLE — main gene page
# ─────────────────────────────────────────────────────────

def antibody_table(request, gene_name):
    # A target with nothing published is not a public gene — serving it produced
    # a page reading "characterisation data for 0 antibodies", which looks like a
    # verdict on the gene rather than an absence of work. 404 instead.
    target = get_object_or_404(public_targets(), gene_name=gene_name)
    antibodies = target.antibodies.select_related('company').prefetch_related('publication_images').filter(publication_images__isnull=False).distinct()

    # ---------------------------
    # GET FILTER PARAMETERS
    # ---------------------------
    host_filter = request.GET.get("host")
    clonality_filter = request.GET.get("clonality")
    recombinant_filter = request.GET.get("recombinant")
    # A tick box, unticked by default. The everyday question this page answers
    # is "what should I buy", and 182 of the 1,645 published reagents can no
    # longer be bought — so they are hidden to begin with. They are never
    # *dropped*: the characterisation stands whatever the supplier has since
    # done with the product, and somebody reviewing a paper that used one needs
    # to see it. So the number hidden is always stated beside the tick, and a
    # row the reader has named by catalogue number is shown regardless (below).
    # `yes` is honoured as well as `show` because it is what the retired filter
    # page used, and a bookmark should not come back emptier than it went.
    show_discontinued = request.GET.get("discontinued", "") in ("show", "yes")

    # ---------------------------
    # APPLY FILTERS
    # ---------------------------
    if host_filter:
        antibodies = antibodies.filter(host_species=host_filter)

    # Both filters ask `services/clonality.py`, because both questions are
    # answered by the enum and `is_recombinant` together and by neither alone —
    # `filter(is_recombinant=True)` missed McGill's 342 rows that carry the fact
    # in the enum instead. `label_q` still honours a bare enum value, so a
    # bookmarked `?clonality=monoclonal` keeps meaning what it meant.
    if clonality_filter:
        antibodies = antibodies.filter(clonality_svc.label_q(clonality_filter))

    if recombinant_filter in ("Yes", "No"):
        antibodies = antibodies.filter(
            clonality_svc.recombinant_q(recombinant_filter == "Yes"))

    # ?ab= is resolved *before* the discontinued filter rather than after it,
    # because the reader has named this row: the home page's search box resolves
    # a catalogue number and redirects straight here, so hiding the row and then
    # reporting "no antibody here matches" would be the site denying a record it
    # holds and has published figures for — which is the one thing it must never
    # say. A named row is exempt from the hiding; nothing else is, and the row
    # itself carries the Discontinued line so the exemption is visible.
    focus_query = request.GET.get("ab", "").strip()
    focus = (public_search.resolve_on_target(target.pk, focus_query)
             if focus_query else None)

    # Discontinued → out_of_market. Counted before it is applied, because a
    # filter that removes rows without saying how many is indistinguishable
    # from a gene that never had them.
    hidden_discontinued = shown_discontinued = 0
    if show_discontinued:
        shown_discontinued = antibodies.filter(out_of_market=True).count()
    else:
        hidden = Q(out_of_market=True)
        if focus:
            hidden &= ~Q(pk=focus["id"])
        hidden_discontinued = antibodies.filter(hidden).count()
        antibodies = antibodies.exclude(hidden)

    # ---------------------------
    # PREPARE STRUCTURED OUTPUT
    # ---------------------------
    # Whether this gene has been curated at all is what separates "tested and
    # not recommended" from "nobody has tested it" — see core/recommendations.py.
    # One query for the page, not one per antibody.
    gene_is_curated = _target_has_recommendations(target)
    # The capability behind each negative, for the whole page in one pass —
    # two queries per application, not two per antibody.
    gene_axes = R.capability_axes([ab.pk for ab in antibodies])

    antibody_data = []
    for ab in antibodies:
        # Build publication image lookup
        pub_images = {}
        for img in ab.publication_images.all():
            pub_images[img.application_type] = img

        experiments = {"WB": None, "IP": None, "ICC_IF": None, "FC": None}
        for app_type, img in pub_images.items():
            key = app_type.replace("-", "_")
            if img.image:
                experiments[key] = img.image.url

        description = {
            "rrid": ab.rrid or None,
            "supplier": _supplier_name(ab),
            "host": ab.host_species or None,
            # The label, not the stored enum: two sites recorded the same
            # reagents under two conventions, and the enum alone contradicts
            # itself across them. services/clonality.py.
            "clonality": clonality_svc.label(ab),
            "clone_ID": ab.clone_id or None,
            "recombinant": "Yes" if clonality_svc.is_recombinant(ab) else None,
            "product_link": ab.supplier_url or None,
            "discontinued": ab.out_of_market,
        }

        # The verdicts, on the server. They used to be fetched by page JS from a
        # referer-gated endpoint, which meant the one field this site exists to
        # publish was the only one absent from the HTML: invisible to search
        # engines, to any client that does not run JavaScript, and to a reader
        # whose browser strips `Referer`. It stopped nobody — the same verdicts
        # are keyless on the MCP connector and in the extension index — while
        # costing every well-behaved reader.
        # One state and one caption per application, from the one definition.
        # The verdict carries the ICC-IF veto (a ratio below the floor cannot be
        # supportive) and the caption carries the qualifier on either side of
        # it — 36% of supportive western blots are not selective, and until now
        # they were drawn identically to the ones that are.
        states, captions, tabs = {}, {}, {}
        verdicts = {}
        for app in R.APPLICATIONS:
            key = _TEMPLATE_APP[app]
            axes = gene_axes.get((ab.pk, app))
            value, clause, tempers = R.verdict_with_qualifier(
                ab, app, set(pub_images), gene_is_curated, axes)
            verdicts[app] = value
            # One colour per cell: the verdict and its qualifier together pick
            # green, amber or red, so amber never sits inside a red border.
            states[key] = R.tone(value, tempers)
            captions[key] = R.cell_caption(app, value, axes)
            # A supportive verdict the data fell short of keeps its green and
            # carries a small yellow tab; on a negative the same fact is the
            # cell's own colour.
            tabs[key] = R.caveat_tab(value, tempers)
        recommended = {
            _TEMPLATE_APP[app]: verdicts[app] == R.RECOMMENDED
            for app in R.APPLICATIONS
        }
        antibody_data.append({
            "id": ab.id,
            "name": ab.catalogue_number,
            "experiments": experiments,
            "description": description,
            # Drawn in the four experiment cells, keyed as `experiments` is.
            "recommended": recommended,
            # Same keys again: the colour family (which carries the qualifier
            # on a negative), the yellow tab (which carries it on a supportive
            # one), and the line that says it in words on either.
            "states": states,
            "tabs": tabs,
            "caveats": captions,
            # The "Recommended Applications:" line, in R.APPLICATIONS order.
            "recommended_apps": [_APP_LABEL[app] for app in R.APPLICATIONS
                                 if verdicts[app] == R.RECOMMENDED],
            # What the client-side application filter matches on. It read the
            # AJAX payload; with nothing fetched it reads this off the row.
            "filter_keys": ' '.join(_FILTER_APP[app] for app in R.APPLICATIONS
                                    if verdicts[app] == R.RECOMMENDED),
            # Three-valued, for the JSON-LD block only — the page itself draws
            # `recommended` and says nothing where an application is untested,
            # which is the distinction core/recommendations.py exists for.
            "verdicts": verdicts,
        })

    # Deliberately shuffled, seeded on the target so the order is stable between
    # visits: no supplier gets top billing on a page of verdicts about them.
    random.Random(target.pk).shuffle(antibody_data)

    # ---------------------------
    # ?ab= — ONE ANTIBODY, IN CONTEXT
    # ---------------------------
    # Arriving from a catalogue-number search, the reader has named the row they
    # want, and the shuffle above means it could be anywhere in a list of twenty.
    # It is lifted to the top and highlighted rather than shown alone: the useful
    # answer to "is this one any good?" is rarely yes or no, it is "not for WB,
    # and these two against the same target are" — which is only on screen if the
    # siblings are. The chip above the table is what says the page is focused, and
    # is the way back to the unfiltered order.
    if focus:
        wanted = focus["id"]
        antibody_data.sort(key=lambda row: row["id"] != wanted)
        # A row can still be filtered out by the dropdowns above, in which case
        # it is genuinely not on this page and saying otherwise would be a chip
        # pointing at nothing. The discontinued tick is not one of those — the
        # row was exempted from it before the query ran.
        if not any(row["id"] == wanted for row in antibody_data):
            focus = None

    # ---------------------------
    # FETCH FILTER OPTIONS
    # ---------------------------
    # NB: .order_by(<field>) is required before .distinct(). Antibody has a
    # default Meta.ordering (target, company, catalogue_number); without resetting
    # it, those columns get folded into SELECT DISTINCT and every row is unique,
    # so the dropdowns showed one option per antibody instead of per distinct value.
    hosts = (
        target.antibodies
        .filter(host_species__isnull=False)
        .exclude(host_species='')
        .order_by("host_species")
        .values_list("host_species", flat=True).distinct()
    )

    # Derived from the rows, not from the enum: the options are the labels the
    # page actually prints, so picking one cannot come back empty. `Recombinant
    # monoclonal` is a real option here and is not a value the column holds.
    clonality_options = clonality_svc.options_for(target.antibodies)

    # Recombinant is now boolean — present as Yes/No dropdown
    recombinant_options = ["Yes", "No"]

    # Report link
    report_link, report_label = _get_report_link(target)

    # What the gene HAS, before any of the filtering above. The <title>, the
    # meta description and the JSON-LD are where this number is read with none
    # of the page's own context beside it — in a search result, in a link
    # preview, in a row of tabs — so a filtered count there is a claim about the
    # dataset rather than about the reader's filters, and `?host=Rabbit` would
    # silently rewrite it. This is the per-gene half of the site-wide headline,
    # and summed across the 159 genes it is exactly that headline.
    published_total = published_antibodies().filter(target=target).count()

    return render(request, "core/antibody_table.html", {
        # Individual context vars replace the old gene model object
        "gene_name": target.gene_name,
        "report_link": report_link,
        "report_label": report_label,
        # schema.org Dataset, in the <head> — see _gene_jsonld above.
        "jsonld": _gene_jsonld(request, target, published_total, report_link),
        # Said on the page as well as in the JSON-LD: a reader who copies a
        # figure into a slide deck is the one who needs it and the least likely
        # to read markup.
        "licence_name": R.LICENCE_NAME,
        "licence_url": R.LICENCE_URL,
        "citation": target.citation,
        "aliases": target.aliases,
        "cell_line_link": target.cell_line_link,
        # Data
        "antibody_data": antibody_data,
        # From `core/recommendations.py`, not spelled in the template: this
        # heading and the captions in the cells under it are the same claim, and
        # two copies on one screen is the drift that module exists to stop.
        "supported_applications_label": R.SUPPORTED_APPLICATIONS_LABEL,
        # What is drawn: after the host/clonality/recombinant dropdowns and
        # after the discontinued tick. Right for everything on the page, because
        # the line stating how many are hidden sits directly under it.
        "antibody_count": len(antibody_data),
        "published_total": published_total,
        # ?ab= focus — the id to highlight, and what to call it in the chip.
        "focus_id": focus["id"] if focus else None,
        "focus_label": (focus["catalogue"] or focus["rrid"]) if focus else "",
        "focus_supplier": focus["supplier"] if focus else "",
        # Asked for a row this gene does not have. Said plainly, without turning
        # it into a claim about the antibody: the page cannot tell a typo from a
        # reagent nobody has tested.
        "focus_missing": focus_query if focus_query and not focus else "",
        # Filters
        "hosts": hosts,
        "clonality_options": clonality_options,
        "recombinant_options": recombinant_options,
        # The tick, and what it is currently costing the reader either way.
        "show_discontinued": show_discontinued,
        "hidden_discontinued": hidden_discontinued,
        "shown_discontinued": shown_discontinued,
    })


# ─────────────────────────────────────────────────────────
# AJAX: Recommendations (not in HTML source)
# ─────────────────────────────────────────────────────────

def gene_recommendations(request, gene_name):
    """Recommendation booleans per antibody for a gene.

    The gene page renders these itself now, so nothing on this site calls it —
    it is kept because the values were served here for long enough that
    something may be reading them, and because ``antibody_table_legacy.html``
    still does.

    **The `Referer` check is gone.** It was there to keep the verdicts off
    machine clients, and it never did: it is one header to spell, and the same
    values are keyless on the MCP connector and in the extension index. What it
    achieved was refusing the well-behaved — `curl`, a server-side script, a
    browser configured not to leak where its reader came from. The gene page
    publishes exactly this now, so the reasoning ``gene_search_index`` and
    ``antibody_search`` next door already give applies unchanged: same data,
    nothing new exposed, hence no check.
    """
    try:
        target = Target.objects.get(gene_name=gene_name)
    except Target.DoesNotExist:
        return JsonResponse({'error': 'Gene not found'}, status=404)

    antibodies = target.antibodies.all().filter(publication_images__isnull=False).distinct()
    recommendations = {}

    for ab in antibodies:
        recommendations[str(ab.id)] = {
            'wb': ab.wb_recommended,
            'ip': ab.ip_recommended,
            'icc_if': ab.if_recommended,
            'fc': ab.fc_recommended,
        }

    return JsonResponse({
        'gene': target.gene_name,
        'recommendations': recommendations,
    })


# ─────────────────────────────────────────────────────────
# AJAX: Gene index for the site-wide search box
# ─────────────────────────────────────────────────────────

def gene_search_index(request):
    """Gene names + aliases for the search box in the header bar.

    The homepage search filters gene boxes already in the DOM; every other page
    has no such list, so the shared search box fetches this instead (lazily, on
    first use). Same data the homepage already publishes in its HTML, so nothing
    new is exposed — hence no referer check.
    """
    from django.core.cache import cache

    genes = cache.get(GENE_SEARCH_INDEX_CACHE_KEY)
    if genes is None:
        genes = [
            {'name': name, 'aliases': aliases or ''}
            for name, aliases in (
                public_targets()
                .order_by('gene_name')
                .values_list('gene_name', 'aliases')
            )
        ]
        cache.set(GENE_SEARCH_INDEX_CACHE_KEY, genes,
                  GENE_SEARCH_INDEX_CACHE_SECONDS)

    response = JsonResponse({'genes': genes})
    response['Cache-Control'] = f'public, max-age={GENE_SEARCH_INDEX_CACHE_SECONDS}'
    return response


def antibody_search(request):
    """Published antibodies matching ``?q=`` — catalogue number, RRID or clone.

    Answered on the server rather than by shipping an index the way the gene list
    is, for one reason: the typesetting rules that make ``14,060–1-AP`` find
    ``14060-1-AP`` live in ``mcp_servers/common/manuscript.py``, and a copy of
    them in page JavaScript would be the third, and the one that drifts. The
    dataset is small and cached, so this is a dict lookup, not a query.

    Same data the gene pages already publish, so no referer check — the gene
    index next door made the same call for the same reason.
    """
    query = request.GET.get('q', '')
    hits = public_search.search(query) if query.strip() else []

    for hit in hits:
        gene_page = reverse('antibody_table', kwargs={'gene_name': hit['gene']})
        focus = hit['catalogue'] or hit['rrid'] or str(hit['id'])
        hit['url'] = f"{gene_page}?{urlencode({'ab': focus})}"
        hit.pop('target_id', None)

    return JsonResponse({'antibodies': hits})


# ── Validation Tools ──────────────────────────────────────────

def validation_planner(request):
    targets = (
        Target.objects
        .filter(gene_name__isnull=False)
        .exclude(gene_name='')
        .annotate(name=F('gene_name'))
        .order_by('name')
    )
    return render(request, 'core/validation_planner.html', {
        'genes': targets,
    })


def validation_record(request):
    """Lightweight self-report: what the antibody was, what it was used for,
    which controls were run, and why. The same spine as the Planner, filled in
    after the experiment. Client-side only — nothing is stored server-side."""
    targets = (
        Target.objects
        .filter(gene_name__isnull=False)
        .exclude(gene_name='')
        .annotate(name=F('gene_name'))
        .order_by('name')
    )
    return render(request, 'core/validation_record.html', {
        'genes': targets,
    })


def validation_framework(request):
    from .recommendations import CONSENSUS_PROTOCOL_URL

    return render(request, 'core/validation_framework.html', {
        'consensus_protocol_url': CONSENSUS_PROTOCOL_URL,
    })


def tools_hub(request):
    return render(request, 'core/tools.html', {
        'extension_public': getattr(settings, 'EXTENSION_PAGE_PUBLIC', False),
    })


# ─────────────────────────────────────────────────────────
# Browser extension
# ─────────────────────────────────────────────────────────

# The five figures on `/extension/` and on the Chrome Web Store listing.
#
# THIS IS THE COPY. The images are captured by hand on a real publisher page
# and rendered in two forms — plain for this site, banner-captioned for the
# store — and the words below are what goes in both, plus the `alt` the page
# serves. A wording change reaching one and not the other is invisible to a
# sighted reader and is the whole of what a screen-reader user gets.
#
# They are listed here rather than in the template because the page must not
# name a file that is not there. A missing static file falls back to its
# unhashed name rather than raising (`OGA_website/storages.py`) — deliberately,
# so an absent asset cannot 500 a page — and the cost of that trade is a broken
# image on the page a stranger lands on. Same existence check as `signed_xpi`.
#
# On the framing (owner, 29 Aug 2026): this is independent characterisation
# data about the REAGENTS A MANUSCRIPT RELIES ON, offered as context rather
# than as a verdict on the paper, and worth most where the manuscript shows no
# context-specific validation of its own. The four rungs — supportive, limited
# support, not supportive, not tested — are what the pictures show.
#: Said on every listing image, under a rule below the sentence. The frame the
#: whole set is read through (owner, 29 Aug 2026): this is characterisation data
#: about the REAGENTS A MANUSCRIPT RELIES ON, offered as context rather than as a
#: verdict on the paper — and it is worth most where the manuscript shows no
#: context-specific validation of its own.
#:
#: It is not `SCOPE_NOTE`, and the difference is deliberate. `SCOPE_NOTE` is the
#: caveat that travels with the DATA, on gene pages and in the JSON-LD; this is
#: the frame for a store listing, which has to say what the thing is for before
#: it says what the data does not cover. Owner kept "protocol and sample
#: dependent" in the first and wrote "assay- and sample-dependent" here.
LISTING_FRAME = (
    'Independent, knockout-controlled antibody characterisation data generated '
    'to community consensus protocols. Antibody performance is assay- and '
    'sample-dependent; this adds context, particularly where a manuscript '
    'provides no context-specific validation of its own.'
)

#: The five listing figures: file, headline, sentence, and what the picture is.
#:
#: **One copy, two renderings.** `browser-extension/store/listing/` holds the
#: same five with the headline, the sentence and `LISTING_FRAME` drawn into a
#: banner, because a Chrome Web Store listing has nowhere to put a caption. The
#: `.webp` named here is the *plain* capture, and `/extension/` prints the same
#: words underneath it as HTML — selectable, translatable, reachable by a screen
#: reader, and rewordable without regenerating an image.
#:
#: That split is what the seven card crops were introduced for in August and
#: what this keeps. What it drops is the crops themselves: they showed the
#: pre-0.3.2 vocabulary, and a screenshot ages the moment the thing it shows
#: changes, with nothing on the page saying which version you are looking at.
#:
#: `alt` describes the PICTURE; the caption says what it demonstrates. They are
#: different jobs, and an `alt` repeating the caption reads it twice to somebody
#: using a screen reader.
LISTING_SCREENSHOTS = (
    (
        '01_supportive_IF_RAB8A_6975.webp',
        'Supportive data \u2014 and the image behind it',
        'Cell Signaling 6975: the data supports western blot, '
        'immunoprecipitation and immunofluorescence under the consensus '
        'protocols, shown with the wild-type and RAB8A-knockout '
        'immunofluorescence the result came from.',
        'A hover card over the catalogue number 6975 in a methods section. All '
        'three tested applications read supportive and flow cytometry reads not '
        'tested, above an immunofluorescence image of wild-type and '
        'RAB8A-knockout HeLa cells.',
    ),
    (
        '02_limited_support_ab237703.webp',
        'The result is per application, not per antibody',
        'ab237703 gives limited support by western blot \u2014 it detects the '
        'target \u2014 and supportive results for immunoprecipitation and '
        'immunofluorescence.',
        'A hover card whose western blot tab reads limited support while '
        'immunoprecipitation and immunofluorescence read supportive, above the '
        'wild-type and knockout blot.',
    ),
    (
        '03_not_supportive_A7131.webp',
        'Data not supportive under the consensus protocols',
        'A7131 was tested by western blot against a PINK1 knockout and gave no '
        'selective signal \u2014 characterisation context this paper does not '
        'provide for itself. The page does not say which application it was '
        'used for, so every result is shown.',
        'A hover card reading \u201cTested; data not supportive for WB\u201d '
        'over a marked catalogue number in a methods section, above a blot in '
        'which the wild-type and knockout lanes look the same.',
    ),
    (
        '05_page_summary_panel.webp',
        'Every antibody on the page, added up',
        'The toolbar panel counts the marks \u2014 two supportive, two limited '
        'support, none not supportive, three with characterised alternatives '
        '\u2014 and lists one line per reagent worth a look.',
        'The extension panel counting the marks on one paper: two supportive, '
        'two limited support, none not supportive, no split verdicts, three '
        'with alternatives and none application-untested.',
    ),
    (
        '06_no_catalogue_number_A2164.webp',
        'Not tested? The alternatives are',
        'A2164 is not in the dataset, so the card leads with what is: NR3C1 has '
        'knockout-controlled antibodies to use instead, and the target was read '
        'from the text beside the catalogue number.',
        'A card over the catalogue number A2164 offering characterised NR3C1 '
        'antibodies instead, and saying this exact antibody has not been '
        'tested.',
    ),
)


#: The one figure that goes at the top of the page, and the crop it is drawn as.
#:
#: **This is the picture that sells the thing** (owner, 31 Aug 2026): a marked
#: catalogue number in a real methods section, the card it opens, and the
#: knockout blot the verdict rests on — the whole claim in one image. Buried
#: fifth in a 2-up grid at a third of life size it was legible as a shape and
#: not as words, which is the same complaint the grid's Enlarge button answers.
#:
#: Two renderings of one capture, for the same reason `LISTING_SCREENSHOTS`
#: has two: the `_top` file is cropped to the card and the text it sits on, so
#: it reads at the size a hero can afford; the full 1280x800 capture is what
#: Enlarge opens, so nothing is lost, only deferred. The words come from the
#: tuple entry, so the hero and the store listing cannot drift apart.
#:
#: It is still *in* `LISTING_SCREENSHOTS` — the store gets all five, and
#: `_extension_screenshots` still checks all five are deployed. What changes is
#: only where the page draws this one.
#: The declared-target card on a real paper (oncotarget 17778, the Sholto David
#: beta-galactosidase review). Drawn inside the Coming-in block and nowhere
#: else — see `_extension_page`. Its `alt` is the whole of what a screen-reader
#: user gets from it, so it carries the card's own two claims and the source.
CONFUSION_SCREENSHOT = '04_identity_flag_lacZ_ab9361.webp'
CONFUSION_SCREENSHOT_ALT = (
    'A hover card over the catalogue number ab9361 in a methods section, '
    'headed \u201cAntibody to lacZ (E. coli beta-galactosidase), not to '
    'mammalian beta-galactosidase\u201d, saying this paper is on a published '
    'list of papers that used it as a mammalian beta-galactosidase antibody, '
    'and citing the review by Sholto David for For Better Science, 21 July '
    '2026. No application results and no image: OGA has not tested it.'
)

HERO_SCREENSHOT = '03_not_supportive_A7131.webp'

#: Falls back to the full capture when the crop is not deployed: a hero drawn
#: from the uncropped file is small, not broken.
HERO_SCREENSHOT_CROP = '03_not_supportive_A7131_top.webp'


def _extension_identity():
    """The facts an IT team needs to allow the extension through a policy.

    A managed browser is allowed or blocked by *identifier*, so a page that
    explains how to get approval and does not carry the identifiers sends the
    reader back to us for the one thing they came for. All of it is derived
    rather than typed:

    - the **Chrome/Edge id** is the last path segment of the store URL, which is
      what a Chrome extension id is. Derived so it cannot drift from the button
      above it, and so no second environment variable has to be remembered on
      the day a listing goes live. An id is 32 letters a-p; anything else means
      the URL is not a store link and the page says to read the id off the
      listing instead of printing a wrong one.
    - the **Firefox id**, the **update URL** and the **site count** come from
      the shipped manifest, which is the thing being installed. A count typed
      here would be wrong the first time a publisher was added.

    **The site count is domains, and `matches` is not that number.** Every site
    is listed twice in the manifest -- `https://*.nature.com/*` and
    `https://nature.com/*` -- so `len(matches)` was 196 where the list covers
    **100** publisher domains, and the page said so twice, in the section an IT
    team reads to decide whether to allow it. Derived and doubled is still the
    wrong noun: the same count-versus-the-thing-it-counts shape as every other
    one in CLAUDE.md, arrived at by deriving the wrong quantity rather than by
    typing a number. Hosts are folded to their registrable domain as well, or
    PubMed and PMC count as two sites and `onlinelibrary.wiley.com` is a
    publisher Wiley does not already own a row for (106 hosts, 100 domains).
    """
    import json
    import re

    identity = {'chrome_id': '', 'gecko_id': '', 'update_url': '',
                'site_count': 0}
    chrome_url = getattr(settings, 'EXTENSION_CHROME_URL', '') or ''
    tail = chrome_url.rstrip('/').rsplit('/', 1)[-1]
    if re.fullmatch(r'[a-p]{32}', tail):
        identity['chrome_id'] = tail

    manifest_path = (Path(settings.BASE_DIR) / 'browser-extension'
                     / 'manifest.json')
    try:
        with open(manifest_path, encoding='utf-8') as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        # The page is still worth drawing without them; every other line in the
        # section stands on its own.
        return identity

    gecko = (manifest.get('browser_specific_settings', {})
             .get('gecko', {}))
    identity['gecko_id'] = gecko.get('id', '')
    identity['update_url'] = gecko.get('update_url', '')
    scripts = manifest.get('content_scripts') or [{}]
    identity['site_count'] = len(
        _manifest_domains(scripts[0].get('matches') or []))
    return identity


# Two-part public suffixes that appear in the manifest's own list. Not a
# public-suffix library: the list is `browser-extension/manifest.json`, it is
# read here, and `tests_extension_scope` walks it, so an entry this does not
# know about is a test failure rather than a silently wrong number on a page.
_TWO_PART_TLDS = ('co', 'ac', 'org', 'com', 'net', 'gov', 'edu', 'go', 'or')


def _manifest_domains(matches):
    """The publisher domains a list of match patterns covers.

    `https://*.nature.com/*` and `https://nature.com/*` are one site, and
    `pmc.ncbi.nlm.nih.gov`, `pubmed.ncbi.nlm.nih.gov` and `ncbi.nlm.nih.gov`
    are one domain. Returns the set so a caller can count it or print it.
    """
    domains = set()
    for pattern in matches:
        if '://' not in pattern:
            continue
        host = pattern.split('://', 1)[1].split('/', 1)[0]
        host = host.lstrip('*').lstrip('.')
        if not host:
            continue
        parts = host.split('.')
        if len(parts) >= 3 and parts[-2] in _TWO_PART_TLDS:
            domains.add('.'.join(parts[-3:]))
        else:
            domains.add('.'.join(parts[-2:]))
    return domains


def _extension_screenshots():
    """The listing screenshots whose files are actually deployed.

    **These are the page's figures again, replacing the seven card crops on
    29 Aug 2026** (owner). The crops were introduced in August precisely
    because the listing set had gone stale — it showed wording that had just
    been replaced — and the same argument now runs the other way: the listing
    set is the current one, captured on 0.3.2 with the four rungs and the new
    colours, and the crops are the stale pair.

    It gives up the thing the crops were also for: **the words are in the
    pixels here**, so they cannot be selected, translated or reworded without
    regenerating an image. `alt` carries the headline and the sentence for that
    reason — it is the whole of what a screen-reader user gets, and
    `LISTING_SCREENSHOTS` is the one copy both are drawn from.
    """
    here = Path(settings.BASE_DIR) / 'core' / 'static' / 'core' / 'extension'
    return [
        {'src': f'core/extension/{name}', 'title': title, 'note': note,
         'alt': alt}
        for name, title, note, alt in LISTING_SCREENSHOTS
        if (here / name).exists()
    ]


def _extension_hero(shots):
    """Pull the hero figure out of the grid, so it is drawn once.

    Returns ``(hero, rest)``. The hero keeps the tuple's own title, note and
    ``alt`` — one copy of the words — and gains ``full``, the uncropped capture
    Enlarge opens. Drawing it in both places would be the same picture twice on
    one page; leaving it in the grid as well is the failure this splits.

    A missing crop falls back to the full capture, and a missing capture leaves
    the grid exactly as it was: no hero rather than a broken image.
    """
    here = Path(settings.BASE_DIR) / 'core' / 'static' / 'core' / 'extension'
    hero = None
    rest = []
    for shot in shots:
        if shot['src'].rsplit('/', 1)[-1] != HERO_SCREENSHOT:
            rest.append(shot)
            continue
        hero = dict(shot, full=shot['src'])
        if (here / HERO_SCREENSHOT_CROP).exists():
            hero['src'] = f'core/extension/{HERO_SCREENSHOT_CROP}'
    return hero, rest


def _extension_page(request, is_public):
    """Install page for the browser extension.

    Store URLs come from settings so the buttons can go live the moment a
    listing is approved, with no code change or redeploy. Until then the page
    shows them as pending rather than linking nowhere.
    """
    # A signed Firefox build turns the side-load instructions into a one-click
    # install, so only offer it when the file is actually deployed.
    xpi_version = getattr(settings, 'EXTENSION_XPI_VERSION', '')
    signed_xpi = bool(xpi_version) and (
        Path(settings.BASE_DIR) / 'browser-extension' / 'signed'
        / f'oga-extension-{xpi_version}.xpi'
    ).exists()

    from .recommendations import CONSENSUS_PROTOCOL_URL

    # The two numbers the page leads with, both derived. Typed, they are the
    # kind of figure that is right on the day it is written and quoted for a
    # year afterwards -- the page already carried "more than 29,000 published
    # papers" as prose. `headline_counts` is the one reader for what the site
    # has published (the same `tests` the home page prints), and the citation
    # snapshot counts its own papers. `citation_papers` is None when no
    # snapshot is deployed, which the template draws rather than printing a
    # nought into a sentence about scale.
    from pipeline.public import headline_counts

    from . import citations

    snapshot = citations.summary() or {}
    citation_papers = (snapshot.get('counts') or {}).get('papers') or None

    # The listing set, not the card crops: see `_extension_screenshots`. One
    # of the five is drawn at the top instead of in the grid; `_extension_hero`
    # is the only place that decides which.
    hero_shot, screenshots = _extension_hero(_extension_screenshots())

    # The declared-target card, captured on a real paper. It is NOT in the
    # listing grid and must not be: the grid is what a reader can install
    # today, and this is the one figure on the page showing a build the stores
    # have not approved. It sits inside the Coming-in block, which says so.
    #
    # Replaces an HTML mock of the same card. The mock was right while no
    # capture existed — a screenshot of an uninstallable build, drawn from the
    # card's own stylesheet — and a real one is better the moment there is one:
    # the mock was a drawing of what the card *should* look like, which is a
    # claim nobody had checked.
    confusion_shot = None
    _shot = (Path(settings.BASE_DIR) / 'core' / 'static' / 'core'
             / 'extension' / CONFUSION_SCREENSHOT)
    if _shot.exists():
        confusion_shot = {
            'src': f'core/extension/{CONFUSION_SCREENSHOT}',
            'alt': CONFUSION_SCREENSHOT_ALT,
        }

    return render(request, 'core/extension.html', {
        'chrome_url': getattr(settings, 'EXTENSION_CHROME_URL', ''),
        'hero_shot': hero_shot,
        'screenshots': screenshots,
        'firefox_url': getattr(settings, 'EXTENSION_FIREFOX_URL', ''),
        'edge_url': getattr(settings, 'EXTENSION_EDGE_URL', ''),
        'is_public': is_public,
        'signed_xpi': signed_xpi,
        'xpi_version': xpi_version,
        # The release that first draws a declared-target notice. Passed in rather
        # than written into the template, so the page and `core.W006` — which
        # warns once this version is actually being served and the page's
        # "coming soon" has become false — read one constant.
        'confusion_release': target_confusions.EXTENSION_RELEASE,
        # Counted, never typed: the block below quotes how many product codes
        # and how many reviews are on file, and both moved the week they were
        # written. `public_totals` is the one reader.
        'confusion_totals': target_confusions.public_totals(),
        'consensus_protocol_url': CONSENSUS_PROTOCOL_URL,
        'confusion_shot': confusion_shot,
        'citation_papers': citation_papers,
        'citeab_data_through': (snapshot.get('source') or {}).get(
            'citeab_data_through', ''),
        **headline_counts(),
        # NOTHING member-conditional belongs in this context, and the team's
        # build link is the one that tried. `/extension/` is not in
        # `cache_headers.NEVER_CACHED_PREFIXES`, so an anonymous 200 goes to the
        # edge with `s-maxage` and `Cookie` stripped from `Vary` — Cloudflare
        # then serves that one copy to everybody, and a signed-in member gets
        # the stranger's page. A footer panel gated on membership was added on
        # 3 Sep 2026 and was invisible to the member it was for, with a cache
        # purge changing nothing.
        #
        # It also broke the argument `cache_headers` rests on: that no public
        # template renders anything belonging to a person, so the worst a
        # mis-served copy can do is show a wrong hyperlink. The download lives
        # on the pipeline hub instead, which IS in that prefix list.
        **_extension_identity(),
    })


def extension(request):
    """Public once announced; pipeline-team only before then.

    Until EXTENSION_PAGE_PUBLIC is set, this sits behind the same login as the
    rest of the pipeline, so the code can be deployed and the team can install
    and test the extension without anything being publicly reachable. It is
    linked from the pipeline hub while in that state.
    """
    if getattr(settings, 'EXTENSION_PAGE_PUBLIC', False):
        return _extension_page(request, is_public=True)

    from pipeline.decorators import pipeline_member_required
    gated = pipeline_member_required(lambda r: _extension_page(r, is_public=False))
    return gated(request)


def extension_firefox_xpi(request):
    """Serve the Mozilla-signed Firefox build so a click installs it.

    The MIME type is the whole point: served as anything other than
    application/x-xpinstall, Firefox downloads the file instead of offering to
    install it, which looks exactly like the extension being broken.

    Drop the signed file from the AMO Developer Hub at
    browser-extension/signed/oga-extension-<version>.xpi and set
    EXTENSION_XPI_VERSION to match.
    """
    version = getattr(settings, 'EXTENSION_XPI_VERSION', '')
    if not version:
        raise Http404('No signed Firefox build is configured.')

    path = (Path(settings.BASE_DIR) / 'browser-extension' / 'signed'
            / f'oga-extension-{version}.xpi')
    if not path.exists():
        raise Http404('Signed Firefox build not found on this deploy.')

    response = FileResponse(open(path, 'rb'), content_type='application/x-xpinstall')
    # inline, not attachment: an attachment disposition makes Firefox save it.
    response['Content-Disposition'] = f'inline; filename="oga-extension-{version}.xpi"'
    response['Access-Control-Allow-Origin'] = '*'
    return response


def extension_updates(request):
    """Firefox's update manifest for the self-distributed (unlisted) build.

    Firefox polls this to find newer versions of a side-loaded add-on, so the
    team gets fixes without anyone re-sending an XPI. Public and unauthenticated
    by necessity — Firefox fetches it with no credentials.

    Returns an empty addons list until EXTENSION_XPI_URL and EXTENSION_XPI_VERSION
    are set, which is a valid "no updates available" answer rather than an error.
    """
    xpi_url = getattr(settings, 'EXTENSION_XPI_URL', '')
    xpi_version = getattr(settings, 'EXTENSION_XPI_VERSION', '')

    updates = []
    if xpi_url and xpi_version:
        updates.append({'version': xpi_version, 'update_link': xpi_url})

    response = JsonResponse({
        'addons': {
            'extension@onlygoodantibodies.co.uk': {'updates': updates},
        },
    })
    response['Access-Control-Allow-Origin'] = '*'
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def extension_download(request):
    """The packaged extension, for the team and for store submission.

    Always behind the pipeline login — this is a build artefact, not something
    the public should be side-loading. Everyone else installs from a store.

    The zip carries a snapshot built from the live database, so it works on real
    papers immediately rather than shipping the 18-record seed the tests use.
    **That is also why it cannot be produced from a checkout, a CI job or a web
    session**: deploy first, then download from here, or the packaged snapshot is
    the previous code's and nothing on the page says so.
    """
    from pipeline.decorators import pipeline_member_required

    def _serve(_request):
        from .extension_index import build_zip_bytes

        payload, version = build_zip_bytes()
        response = HttpResponse(payload, content_type='application/zip')
        response['Content-Disposition'] = (
            f'attachment; filename="oga-extension-{version}.zip"'
        )
        response['Content-Length'] = str(len(payload))
        return response

    return pipeline_member_required(_serve)(request)


def extension_citations(request):
    """Which published papers used which of our antibodies, and for what.

    A static artefact built by ``manage.py build_citation_index`` from the OGA x
    CiteAb join database. It lets the extension REPLACE its application guess
    (measured bad away from western blot) with a lookup on papers the citation
    record covers.

    Served as its own resource so an extension that does not understand it never
    pays for it, and so a missing artefact degrades to *the extension works
    exactly as it did before* rather than taking index.json down with it. A 404
    here is a fact the extension reports as "could not be checked" -- which is
    NOT the same as "this paper is not in the record", and the card says so.
    """
    from . import citations

    payload = citations.raw_bytes()
    if payload is None:
        # Deliberately not an empty JSON object. An empty table is indistinguishable
        # from a table in which nothing matched, and the extension would then tell
        # readers no paper cites anything.
        response = JsonResponse(
            {'error': 'no citation snapshot is deployed'}, status=404)
    else:
        response = HttpResponse(payload, content_type='application/json')
        response['Cache-Control'] = (
            f'public, max-age={EXTENSION_CITATIONS_CACHE_SECONDS}')
    response['Access-Control-Allow-Origin'] = '*'
    return response


def extension_index(request):
    """The snapshot the extension downloads and then matches against locally.

    Built from the live pipeline data and cached, so it tracks the database
    without needing a build step or a redeploy. Public, read-only data, open to
    any origin — other tools are welcome to use it.
    """
    import json

    from django.core.cache import cache
    from django.core.serializers.json import DjangoJSONEncoder

    from .extension_index import build_index, load_aliases

    # The BYTES are cached, not the dict. Cached as a dict, every single request
    # re-serialised about a megabyte of JSON to produce the identical answer --
    # and now that an ETag is computed from the body, that work happened even on
    # requests that were about to be told "you already have this one".
    payload = cache.get(EXTENSION_INDEX_CACHE_KEY)
    if payload is None:
        payload = json.dumps(
            build_index(aliases=load_aliases()),
            cls=DjangoJSONEncoder, separators=(',', ':')).encode()
        cache.set(EXTENSION_INDEX_CACHE_KEY, payload, EXTENSION_INDEX_CACHE_SECONDS)

    response = HttpResponse(payload, content_type='application/json')
    response['Access-Control-Allow-Origin'] = '*'
    response['Cache-Control'] = f'public, max-age={EXTENSION_INDEX_CACHE_SECONDS}'
    return response


# ── Embeddable Antibody Card ──────────────────────────────────

@xframe_options_exempt
def embed_antibody_card(request):
    """
    Public embeddable card for a single antibody.
    Now reads from pipeline models.

    NOTE: embed_card.html template may reference antibody.name, antibody.gene.name,
    desc.rrid, etc. A compatibility dict is built to match the old template interface.
    If the template is updated to use pipeline field names, this compat layer can be removed.
    """
    from types import SimpleNamespace

    rrid = request.GET.get('rrid', '').strip()
    catalogue = request.GET.get('catalogue', '').strip()
    compact = request.GET.get('compact', '').lower() == 'true'
    application = request.GET.get('application', '').strip().upper()

    if not rrid and not catalogue:
        return render(request, 'core/embed_card.html', {
            'error': 'Please provide ?rrid= or ?catalogue= parameter.',
        })

    # Only antibodies with a published figure, the same rule the gene pages and
    # the extension use (pipeline/public.py). Without it this card served rows
    # that have nothing behind them — the exact failure the gene pages 404 on,
    # rendered instead as an assessment with every application blank, on the one
    # surface designed to be embedded in somebody else's page.
    published = PipelineAntibody.objects.select_related(
        'target', 'company'
    ).filter(publication_images__isnull=False).distinct()

    antibody = None
    if rrid:
        # Accept the RRID in either the bare 'AB_1234' form or the full
        # antibodyregistry URL, regardless of which form is stored, so existing
        # published embed links keep working after RRID standardisation.
        from pipeline.rrid_utils import rrid_match_candidates
        antibody = published.filter(rrid__in=rrid_match_candidates(rrid)).first()
    elif catalogue:
        antibody = published.filter(catalogue_number=catalogue).first()

    if not antibody:
        lookup = rrid if rrid else catalogue
        return render(request, 'core/embed_card.html', {
            'error': f'Antibody not found: {lookup}',
        })

    # Build compatibility wrapper for embed_card.html template
    # Template expects: antibody.name, antibody.gene.name, desc.rrid, etc.
    desc = SimpleNamespace(
        rrid=antibody.rrid or None,
        supplier=_supplier_name(antibody),
        host=antibody.host_species or None,
        clonality=antibody.clonality or None,
        clone_ID=antibody.clone_id or None,
        recombinant='Yes' if antibody.is_recombinant else None,
        product_link=antibody.supplier_url or None,
        discontinued=antibody.out_of_market,
        wb_app=antibody.wb_recommended,
        ip_app=antibody.ip_recommended,
        icc_if_app=antibody.if_recommended,
        fc_app=antibody.fc_recommended,
    )

    # Wrap the antibody to provide .name and .gene.name attributes
    antibody_compat = SimpleNamespace(
        id=antibody.id,
        name=antibody.catalogue_number,
        gene=SimpleNamespace(name=antibody.target.gene_name),
    )

    # Build publication image lookup
    pub_images = {}
    for img in antibody.publication_images.all():
        pub_images[img.application_type] = img

    app_types = [('WB', 'WB'), ('IP', 'IP'), ('ICC-IF', 'ICC-IF'), ('FC', 'FC')]
    valid_apps = {'WB', 'IP', 'ICC-IF', 'FC'}
    if application and application in valid_apps:
        app_types = [(application, application)]

    experiment_cards = []
    for code, label in app_types:
        img = pub_images.get(code)
        if img and img.image:
            experiment_cards.append({
                'label': label,
                'has_data': True,
                'image_url': img.image.url,
            })

    applications = []
    if antibody.wb_recommended:
        applications.append('Western Blot')
    if antibody.ip_recommended:
        applications.append('IP')
    if antibody.if_recommended:
        applications.append('ICC-IF')
    if antibody.fc_recommended:
        applications.append('FC')

    gene_has_recommendations = _target_has_recommendations(antibody.target)

    # The card is the one surface where this data leaves the site and renders
    # inside somebody else's page, so it is the surface attribution matters most
    # on — and it linked to the website, which /data-access/ now says explicitly
    # is *not* what attribution means. Cite the gene's report DOI. Nothing is
    # said where a gene has no report yet: the site link below is the honest
    # attribution then, and inventing a citation is worse than omitting one.
    report_link, _ = _get_report_link(antibody.target)

    return render(request, 'core/embed_card.html', {
        'antibody': antibody_compat,
        'desc': desc,
        'experiment_cards': experiment_cards,
        'applications': applications,
        # Same heading as the gene page's, from the same constant.
        'supported_applications_label': R.SUPPORTED_APPLICATIONS_LABEL,
        'gene_has_recommendations': gene_has_recommendations,
        'compact': compact,
        'report_link': report_link,
        'licence_name': R.LICENCE_NAME,
        'licence_url': R.LICENCE_URL,
        'error': None,
    })


# Recommendation manager moved to the pipeline app
# (pipeline/views/recommendations.py, gated by @pipeline_member_required).
# The old /admin-tools/recommendations/ URL now redirects there.


# ─────────────────────────────────────────────────────────
# NOMINATE A GENE — the public "we don't have this, ask for it" door
# ─────────────────────────────────────────────────────────

#: A press this often from one address is somebody testing the form, not a lab
#: with 20 genes. It refuses politely and says what to do instead. Counted in
#: the process cache like `core/api_throttle.py`'s ceiling and approximate for
#: the same reason — this is an abuse guard, not a quota.
NOMINATION_LIMIT_PER_HOUR = 10


def _nomination_throttle_key(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = (forwarded.split(',')[0].strip()
          or request.META.get('REMOTE_ADDR', '') or 'unknown')
    return f'gene_nomination_count:{ip}'


def nominate_gene(request):
    """Ask us to characterise a gene we do not have.

    Until now this button went to the contact form, so a request became an email
    and nothing else: unaggregated, unqueryable, and no use at all for answering
    *"which gene is the most people waiting for?"* — which is the one question a
    prioritisation meeting actually asks. It writes a ``GeneRequest`` now, and
    still sends the email, because somebody is watching that inbox today and a
    change that silently stops a notification is the worst kind.

    Deliberately **not** a ``TargetNomination``: that is the consortium's own
    list, with a site, a funder and a bench behind each row, and dropping the
    public's wishes into it would put unfunded strangers into every "who is
    doing what" total the Portfolio draws.

    The check is ``pipeline/services/gene_requests.py``, and it runs on the way
    in as well as at the press: arriving from a failed search with ``?gene=``,
    the reader is told what we know about their gene before they type anything —
    including the two answers that are refusals ("not human", "that is a
    modification"), which are more use than a queue position.
    """
    from pipeline.services import gene_requests as GR
    from django.core.cache import cache

    typed = (request.GET.get('gene') or '').strip()
    source = (request.GET.get('from') or '').strip()[:20]
    form = {
        'gene': typed, 'email': '', 'name': '', 'organisation': '',
        'applications': [], 'applications_other': '', 'note': '',
        'has_funding': False, 'funding_note': '',
    }
    errors = {}
    verdict = None

    if request.method == 'POST':
        typed = (request.POST.get('gene') or '').strip()
        source = (request.POST.get('source') or '').strip()[:20]
        form = {
            'gene': typed,
            'email': (request.POST.get('email') or '').strip(),
            'name': (request.POST.get('name') or '').strip(),
            'organisation': (request.POST.get('organisation') or '').strip(),
            'applications': request.POST.getlist('applications'),
            'applications_other': (request.POST.get('applications_other') or '').strip(),
            'note': (request.POST.get('note') or '').strip(),
            'has_funding': bool(request.POST.get('has_funding')),
            'funding_note': (request.POST.get('funding_note') or '').strip(),
        }

        # A field no human sees and every naive bot fills in. Answered with the
        # ordinary success page rather than an error: telling a bot it was
        # spotted is how it learns to stop filling the field in.
        if (request.POST.get('website') or '').strip():
            return render(request, 'core/nominate_done.html',
                          {'gene': typed, 'applications': []})

        # Everything is checked at once and answered beside its own box — a
        # refusal that names one field at a time is a form somebody submits
        # three times.
        if not typed:
            errors['gene'] = 'Which gene? Enter its symbol, e.g. SNCA.'
        if not form['email']:
            errors['email'] = ('We need an email address — it is how we tell '
                               'you if this gene gets funded.')
        elif '@' not in form['email']:
            errors['email'] = 'That does not look like an email address.'
        # Typing in the box is choosing the option: refusing somebody who said
        # what they need because they did not also tick a box beside it would
        # be the form arguing with an answer it already has.
        if form['applications_other'] and GR.OTHER not in form['applications']:
            form['applications'] = list(form['applications']) + [GR.OTHER]

        if not form['applications']:
            errors['applications'] = (
                'Tick at least one application, or "Not sure yet" — which '
                'application you need changes what we would have to run.')
        elif GR.OTHER in form['applications'] and not form['applications_other']:
            # "Something else" on its own records that they need *something*
            # and not what, which is the one answer this question cannot use.
            errors['applications'] = (
                'You ticked "Something else" — say which, in the box beside '
                'it, or the request cannot tell us what you need.')

        count = cache.get(_nomination_throttle_key(request), 0)
        if count >= NOMINATION_LIMIT_PER_HOUR:
            errors['gene'] = (
                'That is a lot of nominations from one place in an hour. Email '
                'onlygoodantibodies@gmail.com with the list instead — a batch '
                'of genes is a conversation we would rather have properly.')

        if not errors:
            verdict = GR.check(typed)
            if verdict['status'] in GR.RECORDABLE:
                gene_request, verdict = GR.record(
                    typed_text=typed,
                    email=form['email'],
                    applications=form['applications'],
                    applications_other=form['applications_other'],
                    requester_name=form['name'],
                    organisation=form['organisation'],
                    has_funding=form['has_funding'],
                    funding_note=form['funding_note'],
                    note=form['note'],
                    source=source,
                    verdict=verdict,
                )
                cache.set(_nomination_throttle_key(request), count + 1, 3600)
                # The row is written before the email is attempted: a mail
                # server hiccup must not lose a request we have already
                # accepted. Same reason the contact form swallows the error.
                _email_gene_request(gene_request, form)
                return render(request, 'core/nominate_done.html', {
                    'gene': gene_request.gene_symbol or gene_request.typed_text,
                    'already_here': verdict['status'] == GR.IN_PIPELINE,
                    'unchecked': verdict['status'] == GR.UNCHECKED,
                    'has_funding': form['has_funding'],
                    'applications': GR.application_labels(
                        gene_request.applications,
                        gene_request.applications_other),
                    # `recorded` is what the analytics event is gated on, and it
                    # is set here and nowhere else: the honeypot above renders
                    # this same page for a bot, and a conversion count that
                    # includes them is worse than no count. `source` rides
                    # along so the funnel splits by where they came in, the
                    # same value that goes on the row.
                    'recorded': True,
                    'source': source,
                })
            # A refusal: fall through and draw the verdict's own message.

    elif typed:
        verdict = GR.check(typed)
        if verdict['status'] == GR.PUBLISHED and verdict['gene_url']:
            # We have it. A form asking them to request what they can already
            # read is the site failing to answer a question it can answer.
            return redirect(verdict['gene_url'])
        if verdict.get('gene'):
            form['gene'] = verdict['gene']

    return render(request, 'core/nominate.html', {
        'form': form,
        'errors': errors,
        'verdict': verdict,
        'source': source,
        'application_choices': GR.APPLICATION_CHOICES,
        'refused': bool(verdict and verdict['status'] not in GR.RECORDABLE),
    })


def _email_gene_request(gene_request, form):
    """Tell the inbox, as the contact form always has.

    The row is the record; this is the notification. It never raises: the
    request is already written, and a mail failure that 500s would show the
    person an error page about something that worked.
    """
    if gene_request is None:
        return
    lines = [
        f'Gene: {gene_request.gene_symbol or gene_request.typed_text}',
        f'Typed: {gene_request.typed_text}',
        f'UniProt: {gene_request.uniprot_id or "not confirmed"}',
        f'Protein: {gene_request.protein_name or "-"}',
        f'Applications: {gene_request.applications or "-"}',
        f'Other application: {gene_request.applications_other or "-"}',
        f'From: {form["name"] or "-"} <{gene_request.email}>',
        f'Organisation: {form["organisation"] or "-"}',
        f'Possible funding: {"YES" if gene_request.has_funding else "no"}',
    ]
    if gene_request.funding_note:
        lines += ['', 'Funding note:', gene_request.funding_note]
    if gene_request.note:
        lines += ['', 'Why they need it:', gene_request.note]
    lines += ['', 'Recorded in the pipeline — see Gene requests.']
    # A bounded connection, unlike the contact form's. The row is already
    # written, so this send is the *optional* half — and the request thread that
    # is waiting on it belongs to a member of the public looking at a spinner,
    # on a service running four threads on one instance. An unreachable SMTP
    # host blocks for the OS default otherwise, which is minutes.
    try:
        connection = get_connection(timeout=10)
        EmailMessage(
            subject=(f'[Gene nomination] '
                     f'{gene_request.gene_symbol or gene_request.typed_text}'),
            body='\n'.join(lines),
            from_email=settings.EMAIL_HOST_USER,
            to=[settings.EMAIL_HOST_USER],
            reply_to=[gene_request.email] if gene_request.email else None,
            connection=connection,
        ).send(fail_silently=False)
    except Exception:
        logger.exception('Gene nomination notification failed to send')
