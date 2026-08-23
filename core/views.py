"""
OGA Public Views — core/views.py
=================================

Phase 3 rewrite: all data reads come from pipeline PostgreSQL
(Target, Antibody, PublicationImage, Report, Company) instead
of core SQLite (Gene, Antibody, Description, Experiment).

Legacy backup: core/views_legacy.py (identical to pre-Phase-3 version).
Rollback: swap import in core/urls.py → `from . import views_legacy as views`

Models that stay in core/academy_db: APIConsumer, ReviewedAntibody.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.core.mail import send_mail, EmailMessage
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

# Core Gene — legacy fallback only (gene_redirect, report link fallback)
from .models import Gene

# The three-valued recommendation reader, the scope caveat and the licence —
# one definition, shared with the API, the extension index and the MCP server.
from . import recommendations as R


# Browser-extension snapshot: rebuilt at most once an hour. The extension only
# refreshes daily, so this mostly protects against crawlers hitting the endpoint.
# _v2 because what is stored under it changed from a dict to the serialised
# bytes. LocMem forgets on every deploy so it would never notice, but a shared
# Redis cache (CACHE_URL) keeps its contents across one -- and the old value
# handed to the new code is a dict where bytes are expected.
EXTENSION_INDEX_CACHE_KEY = 'extension_index_v2'
EXTENSION_INDEX_CACHE_SECONDS = 3600

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
    Priority: (1) Report.f1000_doi, (2) Report.zenodo_doi,
    (3) core Gene.f1000_report_link as fallback.
    Returns (url, label) tuple.
    """
    report = target.reports.filter(
        Q(f1000_doi__gt='') | Q(zenodo_doi__gt='')
    ).order_by('-f1000_date', '-zenodo_date').first()

    if report:
        if report.f1000_doi:
            return report.f1000_doi, 'F1000 Report'
        if report.zenodo_doi:
            return report.zenodo_doi, 'Zenodo Report'

    # Fallback: core Gene (still in git-deployed SQLite)
    try:
        gene = Gene.objects.get(name=target.gene_name)
        if gene.f1000_report_link:
            return gene.f1000_report_link, 'Full Report'
    except Gene.DoesNotExist:
        pass

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
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        message = request.POST.get('message', '').strip()
        enquiry_type = request.POST.get('enquiry-type', '').strip()
        gene = request.POST.get('gene', '').strip()
        antibody = request.POST.get('antibody', '').strip()
        target_details = request.POST.get('target-details', '').strip()

        subject = f'Contact Form Submission from {name}'
        if enquiry_type:
            subject = f'[{enquiry_type}] {subject}'

        lines = [f'Name: {name}', f'Email: {email}']
        if enquiry_type:
            lines.append(f'Enquiry Type: {enquiry_type}')
        if gene:
            lines.append(f'Gene: {gene}')
        if antibody:
            lines.append(f'Antibody: {antibody}')
        if target_details:
            lines.append(f'Target details: {target_details}')
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
    from .recommendations import CONSENSUS_PROTOCOL_URL

    return render(request, 'core/portal.html', {
        'consensus_protocol_url': CONSENSUS_PROTOCOL_URL,
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

    if query:
        # A catalogue number or an RRID is a thing people paste into this box,
        # and matching gene names alone answered "no results" for reagents we
        # hold. When it names exactly one antibody, go straight to it — that is a
        # resolution, not a guess, and the gene page is where the verdict is.
        # Anything less certain narrows the grid instead of picking for them.
        single = public_search.resolve(query)
        if single:
            gene_page = reverse(
                'antibody_table', kwargs={'gene_name': single['gene']})
            focus = single['catalogue'] or single['rrid']
            return redirect(f"{gene_page}?{urlencode({'ab': focus})}")
        targets = targets.filter(
            Q(gene_name__icontains=query)
            | Q(pk__in=public_search.target_ids(query))
        )

    no_results = not targets.exists() and bool(query)

    from .recommendations import CONSENSUS_PROTOCOL_URL

    return render(request, "core/home.html", {
        "genes": targets,
        "search_query": query,
        "no_results": no_results,
        "consensus_protocol_url": CONSENSUS_PROTOCOL_URL,
        **get_live_stats(),
    })


# ─────────────────────────────────────────────────────────
# GENE REDIRECT — legacy numeric URLs (/124/ → /antibodies/ARHGDIA/)
# Still reads core Gene because old numeric IDs don't exist on Target
# ─────────────────────────────────────────────────────────

def gene_redirect(request, gene_id):
    gene = get_object_or_404(Gene, id=gene_id)
    return redirect('antibody_table', gene_name=gene.name, permanent=True)


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
        verdicts = R.recommendations_for(ab, set(pub_images), gene_is_curated)
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

# The Chrome Web Store listing's own screenshots, shown on the install page so
# a reader sees the thing working before installing it. Each PNG carries its
# headline and sentence drawn into the image, which is why `alt` repeats them
# and nothing is printed underneath: the words are in the picture already.
#
# They are listed here rather than in the template because the page must not
# name a file that is not there. A missing static file falls back to its
# unhashed name rather than raising (`OGA_website/storages.py`) — deliberately,
# so an absent asset cannot 500 a page — and the cost of that trade is a broken
# image on the page a stranger lands on. Same existence check as `signed_xpi`.
EXTENSION_SCREENSHOTS = (
    (
        '01_recommended_IF_RAB8A.webp',
        'Recommended — and the knockout image that proves it',
        'Cell Signaling 6975 on a Europe PMC methods section: recommended for '
        'WB, IP and IF, with the wild-type / RAB8A-knockout immunofluorescence '
        'behind the verdict.',
    ),
    (
        '02_recommended_WB_LRRK2.webp',
        'Right antibody, right application',
        'ab133474 is recommended for western blot — but not for IP or IF. The '
        'extension reads which application the paper actually ran.',
    ),
    (
        '03_not_recommended_RAB10.webp',
        'Tested against a knockout. It failed.',
        'ab104859 is not recommended for WB, IP or IF. Two antibodies in one '
        'sentence are flagged before you order either.',
    ),
    (
        '04_no_catalogue_number.webp',
        'No catalogue number? Still useful.',
        'When a paper names only the target, the extension points to the '
        'antibodies that do have knockout-controlled data — and says plainly '
        'that this one has not been tested.',
    ),
)


def _extension_screenshots():
    """The listing screenshots whose files are actually deployed."""
    here = Path(settings.BASE_DIR) / 'core' / 'static' / 'core' / 'extension'
    return [
        {'src': f'core/extension/{name}', 'title': title, 'note': note}
        for name, title, note in EXTENSION_SCREENSHOTS
        if (here / name).exists()
    ]


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

    return render(request, 'core/extension.html', {
        'chrome_url': getattr(settings, 'EXTENSION_CHROME_URL', ''),
        'screenshots': _extension_screenshots(),
        'firefox_url': getattr(settings, 'EXTENSION_FIREFOX_URL', ''),
        'edge_url': getattr(settings, 'EXTENSION_EDGE_URL', ''),
        'is_public': is_public,
        'signed_xpi': signed_xpi,
        'xpi_version': xpi_version,
        'consensus_protocol_url': CONSENSUS_PROTOCOL_URL,
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
    the public should be side-loading. Once the store listings exist, everyone
    else installs from there.

    The zip carries a snapshot built from the live database, so it works on real
    papers immediately rather than shipping the small seed used by the tests.
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
