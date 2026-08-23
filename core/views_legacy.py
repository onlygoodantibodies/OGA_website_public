from django.shortcuts import render
from django.core.mail import send_mail, EmailMessage
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.shortcuts import redirect
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from .models import Gene, Antibody, Experiment, Description
from django.contrib.admin.views.decorators import staff_member_required
import random
import json
from django.views.decorators.clickjacking import xframe_options_exempt
from django.http import HttpResponse

def robots_txt(request):
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /embed/",
        "Disallow: /portal/",
        "Disallow: /admin/",
        "Disallow: /admin-tools/",
        "",
        "Sitemap: https://onlygoodantibodies.co.uk/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")

def home(request):
    # Get the search query from the request
    query = request.GET.get('search', '').strip()  # Retrieve the search query from the URL

    # Fetch all genes by default and order them alphabetically
    genes = Gene.objects.all().order_by('name')

    # Filter genes if a query is provided
    if query:
        genes = genes.filter(name__icontains=query).order_by('name')  # Case-insensitive partial match, ordered alphabetically

    # Check if there are no results
    no_results = not genes.exists() and bool(query)  # True if query exists but no genes match

    return render(request, "core/home.html", {
            "genes": genes,
            "search_query": query,
            "no_results": no_results,
            "gene_count": Gene.objects.count(),
            "antibody_count": Antibody.objects.count(),
            "experiment_count": Experiment.objects.count(),
        })


def about(request):
    return render(request, 'core/about.html', {
        "gene_count": Gene.objects.count(),
        "antibody_count": Antibody.objects.count(),
    })

def publications(request):
    """Redirect handled in urls.py — kept as fallback."""
    return redirect('news_publications', permanent=True)

def news_publications(request):
    """Render the combined News & Publications page."""
    return render(request, 'core/news_publications.html')

def partners(request):
    return render(request, 'core/partners.html')

def contact(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        message = request.POST.get('message')

        subject = f'Contact Form Submission from {name}'
        full_message = f'Name: {name}\nEmail: {email}\n\nMessage:\n{message}'

        send_mail(
            subject,
            full_message,
            email,
            [settings.EMAIL_HOST_USER],
            fail_silently=False,
        )
        return redirect('success')  # Redirect to the success page
    
    return render(request, 'core/contact.html')

def success(request):
    return render(request, 'core/success.html')

def news(request):
    """Redirect handled in urls.py — kept as fallback."""
    return redirect('news_publications', permanent=True)

def using_the_data(request):
    return render(request, 'core/using-the-data.html')


def gene_redirect(request, gene_id):
    """Redirect old numeric URLs (e.g. /124/) to new name-based URLs (e.g. /antibodies/ARHGDIA/)."""
    gene = get_object_or_404(Gene, id=gene_id)
    return redirect('antibody_table', gene_name=gene.name, permanent=True)


def antibody_table(request, gene_name):
    gene = get_object_or_404(Gene, name=gene_name)
    antibodies = gene.antibodies.all().select_related("description")

    # ---------------------------
    # GET FILTER PARAMETERS
    # ---------------------------
    host_filter = request.GET.get("host")
    clonality_filter = request.GET.get("clonality")
    recombinant_filter = request.GET.get("recombinant")
    discontinued = request.GET.get("discontinued")

    # NOTE: Application filter removed from server-side.
    # It is now handled entirely by client-side JS using
    # data fetched from /api/internal/recommendations/.

    # ---------------------------
    # APPLY FILTERS (server-side: host, clonality, recombinant, discontinued only)
    # ---------------------------
    if host_filter:
        antibodies = antibodies.filter(description__host=host_filter)

    if clonality_filter:
        antibodies = antibodies.filter(description__clonality=clonality_filter)

    if recombinant_filter:
        antibodies = antibodies.filter(description__recombinant=recombinant_filter)

    # discontinued filter
    if discontinued == "yes":
        antibodies = antibodies.filter(description__discontinued=True)
    elif discontinued == "no" or discontinued is None:
        antibodies = antibodies.filter(description__discontinued=False)

    # ---------------------------
    # PREPARE STRUCTURED OUTPUT
    # ---------------------------
    antibody_data = []
    for antibody in antibodies:
        experiments = {
            "WB": None,
            "IP": None,
            "ICC_IF": None,
            "FC": None,
        }

        for experiment in antibody.experiments.all():
            key = experiment.experiment_type.replace("-", "_")
            if experiment.file_path:
                experiments[key] = experiment.file_path.url

        # NOTE: Recommendations (selected_apps / applications) deliberately
        # excluded from server-rendered HTML to prevent scraping.
        # They are served via /api/internal/recommendations/ and
        # injected by client-side JS.

        description = {
            "rrid": d.rrid if (d := antibody.description) else None,
            "supplier": d.supplier if d else None,
            "host": d.host if d else None,
            "clonality": d.clonality if d else None,
            "clone_ID": d.clone_ID if d else None,
            "recombinant": d.recombinant if d else None,
            "product_link": d.product_link if d else None,
            "discontinued": d.discontinued if d else False,
        }

        antibody_data.append({
            "id": antibody.id,
            "name": antibody.name,
            "experiments": experiments,
            "description": description,
        })
    random.Random(gene.id).shuffle(antibody_data)
    # ---------------------------
    # FETCH FILTER OPTIONS
    # ---------------------------
    hosts = gene.antibodies.filter(description__host__isnull=False)\
        .values_list("description__host", flat=True).distinct()

    clonality_options = gene.antibodies.filter(description__clonality__isnull=False)\
        .values_list("description__clonality", flat=True).distinct()

    recombinant_options = gene.antibodies.filter(description__recombinant__isnull=False)\
        .values_list("description__recombinant", flat=True).distinct()

    discontinued_options = gene.antibodies.filter(description__discontinued__isnull=False)\
        .values_list("description__discontinued", flat=True).distinct()

    return render(request, "core/antibody_table.html", {
        "gene": gene,
        "f1000_report_link": gene.f1000_report_link,
        "antibody_data": antibody_data,
        "hosts": hosts,
        "clonality_options": clonality_options,
        "recombinant_options": recombinant_options,
        "discontinued": discontinued,
        "discontinued_options": discontinued_options,
    })


# ─────────────────────────────────────────────────────────
# AJAX endpoint: Recommendations (not in HTML source)
# ─────────────────────────────────────────────────────────

def gene_recommendations(request, gene_name):
    """
    Returns recommendation booleans per antibody for a gene.
    Called by client-side JS on page load — NOT rendered in HTML.

    This is the interpretation layer that scrapers cannot extract
    from the server-rendered page.

    Light protections:
    - Referer check (blocks direct URL hits from scripts)
    - Under /api/ path (blocked by robots.txt)
    - Not discoverable from HTML source without reading JS
    """
    # Referer check — allow the live site, localhost for dev
    referer = request.META.get('HTTP_REFERER', '')
    allowed_referers = ['onlygoodantibodies.co.uk', 'localhost', '127.0.0.1']
    if not any(host in referer for host in allowed_referers):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        gene = Gene.objects.get(name=gene_name)
    except Gene.DoesNotExist:
        return JsonResponse({'error': 'Gene not found'}, status=404)

    antibodies = gene.antibodies.select_related('description').all()
    recommendations = {}

    for ab in antibodies:
        desc = getattr(ab, 'description', None)
        if desc:
            recommendations[str(ab.id)] = {
                'wb': desc.wb_app,
                'ip': desc.ip_app,
                'icc_if': desc.icc_if_app,
                'fc': desc.fc_app,
            }

    return JsonResponse({
        'gene': gene.name,
        'recommendations': recommendations,
    })


def set_dark_mode(request):
    response = redirect('home')  # Redirect back to homepage
    response.set_cookie(
        'dark_mode',  # Cookie name
        'enabled',  # Cookie value
        max_age=60*60*24*30,  # 30 days expiration
        secure=True,  # Works only on HTTPS
        httponly=True,  # Cannot be accessed by JavaScript
        samesite='Lax'  # Restrict cookie sharing across sites
    )
    return response

def privacy_policy(request):
    return render(request, 'core/privacy_policy.html')

def roadmap(request):
    return render(request, 'core/roadmap.html')

def champions(request):
    return render(request, 'core/champions.html')

def roadmap_institutions(request):
    return render(request, 'core/roadmap_institutions.html')

def roadmap_funders(request):
    return render(request, 'core/roadmap_funders.html')

def roadmap_publishers(request):
    return render(request, 'core/roadmap_publishers.html')

def roadmap_manufacturers(request):
    return render(request, 'core/roadmap_manufacturers.html')

# ── Validation Tools ──────────────────────────────────────────

def validation_planner(request):
    """Render the validation planner page."""
    return render(request, 'core/validation_planner.html', {
        'genes': Gene.objects.all().order_by('name'),
    })


def validation_recorder(request):
    """Render the validation recorder page with OGA lookup data for evidence matching."""

    genes = Gene.objects.prefetch_related(
        'antibodies__description',
        'antibodies__experiments'
    ).all().order_by('name')

    # Build gene-keyed antibody lookup for client-side fuzzy matching
    gene_antibodies = {}
    for gene in genes:
        abs_list = []
        for ab in gene.antibodies.all():
            try:
                desc = ab.description
            except Description.DoesNotExist:
                continue
            images = {}
            for e in ab.experiments.all():
                if e.file_path:
                    images[e.experiment_type] = '/media/' + str(e.file_path)
            abs_list.append({
                'name': ab.name,                     # catalogue number
                'rrid': desc.rrid or '',
                'clone': desc.clone_ID or '',
                'supplier': desc.supplier or '',
                'host': desc.host or '',
                'clonality': desc.clonality or '',
                'recombinant': desc.recombinant or '',
                'discontinued': desc.discontinued,
                'rec': {
                    'wb': desc.wb_app,
                    'ip': desc.ip_app,
                    'icc_if': desc.icc_if_app,
                    'fc': desc.fc_app,
                },
                'images': images,
            })
        if abs_list:
            gene_antibodies[gene.name] = abs_list

    # Build alias → canonical gene name mapping for fuzzy matching
    gene_aliases = {}
    for gene in genes:
        if gene.aliases:
            for alias in gene.aliases.split(','):
                alias = alias.strip()
                if alias:
                    gene_aliases[alias] = gene.name

    return render(request, 'core/validation_recorder.html', {
        'genes': genes,
        'gene_antibodies_json': json.dumps(gene_antibodies),
        'gene_aliases_json': json.dumps(gene_aliases),
    })

def validation_framework(request):
    """Render the validation planning framework page."""
    return render(request, 'core/validation_framework.html')

def tools_hub(request):
    """Render the tools and resources hub page."""
    return render(request, 'core/tools.html')


def rrid_lookup(request):
    """
    Proxy endpoint for RRID lookups against the Antibody Registry API (n2t.net).
    Returns parsed antibody metadata for autofilling the validation recorder.
    """
    import urllib.request
    import urllib.error

    rrid = request.GET.get('rrid', '').strip()
    if not rrid or not rrid.startswith('AB_'):
        return JsonResponse({'success': False, 'error': 'Invalid RRID format'})

    url = f'https://n2t.net/RRID:{rrid}.json'

    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'OGA-ValidationRecorder/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        hits = data.get('hits', {}).get('hits', [])
        if not hits:
            return JsonResponse({'success': False, 'error': 'RRID not found'})

        source = hits[0].get('_source', {})
        item = source.get('item', {})
        antibodies = source.get('antibodies', {})
        primary = antibodies.get('primary', [{}])[0] if antibodies.get('primary') else {}
        organisms = source.get('organisms', {})
        vendors = source.get('vendors', [{}])
        vendor = vendors[0] if vendors else {}
        rrid_info = source.get('rrid', {})
        issues_data = source.get('issues', {})
        validation = source.get('validation', {})
        mentions = source.get('mentions', [{}])
        mention = mentions[0] if mentions else {}

        # Build host/clonality string
        source_org = organisms.get('source', [{}])
        host_species = source_org[0].get('species', {}).get('name', '') if source_org else ''
        clonality = primary.get('clonality', {}).get('name', '')
        host_clonality = ''
        if host_species and clonality:
            host_clonality = host_species.capitalize() + ' ' + clonality.lower()
        elif host_species:
            host_clonality = host_species.capitalize()

        # Target species
        target_orgs = organisms.get('target', [])
        target_species = [t.get('species', {}).get('name', '') for t in target_orgs if t.get('species', {}).get('name')]

        # Parse issues
        direct_issues = issues_data.get('direct', [])
        issue_texts = []
        for iss in direct_issues:
            comment = iss.get('comments', '').strip()
            if comment:
                issue_texts.append(comment)

        # Parse recommended applications from notes
        notes = item.get('notes', [])
        applications = ''
        for note in notes:
            desc = note.get('description', '')
            if 'Useful for' in desc or 'Western' in desc or 'Immuno' in desc:
                applications = desc

        result = {
            'success': True,
            'vendor': vendor.get('name', ''),
            'catalogNumber': vendor.get('catalogNumber', ''),
            'name': item.get('name', ''),
            'description': item.get('description', ''),
            'target': primary.get('targets', [{}])[0].get('name', '') if primary.get('targets') else '',
            'hostClonality': host_clonality,
            'clone': primary.get('clone', {}).get('identifier', ''),
            'isotype': primary.get('isotype', {}).get('name', ''),
            'properCitation': rrid_info.get('properCitation', ''),
            'discontinued': bool(item.get('discontinued', {}).get('comment', '')),
            'targetSpecies': target_species,
            'applications': applications,
            'isValidated': validation.get('isValidated', False),
            'totalMentions': mention.get('totalRRIDMentions', {}).get('count', 0),
            'issues': issue_texts,
            'hasIssues': len(issue_texts) > 0,
        }

        return JsonResponse(result)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return JsonResponse({'success': False, 'error': 'RRID not found in registry'})
        return JsonResponse({'success': False, 'error': f'Registry returned error {e.code}'})
    except urllib.error.URLError:
        return JsonResponse({'success': False, 'error': 'Could not reach antibody registry'})
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'RRID lookup failed: {e}')
        return JsonResponse({'success': False, 'error': 'Lookup failed'})

@require_POST
def submit_validation_record(request):
    """
    Receive validation record submission from the v6e recorder.
    Sends structured email to OGA with antibody data and any uploaded images.
    Nothing is saved to disk or database.

    mode=download: auto-sent on PDF download (no consent checkbox, includes control images)
    """
    try:
        author = request.POST.get('author', '').strip()
        email = request.POST.get('email', '').strip()
        institution = request.POST.get('institution', '').strip()
        manuscript = request.POST.get('manuscript', '').strip()
        antibodies_json = request.POST.get('antibodies_json', '[]')
        record_id = request.POST.get('record_id', '').strip()
        mode = request.POST.get('mode', 'download')

        try:
            antibodies = json.loads(antibodies_json)
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid data'})

        # ── Build email body ──
        body = []
        body.append('ANTIBODY VALIDATION RECORD — PDF DOWNLOAD')
        body.append('(Auto-submitted on download)')
        body.append('=' * 50)
        body.append('')
        body.append(f'Record ID: {record_id}')
        body.append(f'Author: {author}')
        body.append(f'Email: {email}')
        body.append(f'Institution: {institution}')
        body.append(f'Manuscript: {manuscript}')
        body.append(f'Antibodies: {len(antibodies)}')
        body.append(f'Submitted: {__import__("datetime").datetime.now().strftime("%d %B %Y, %H:%M")}')
        body.append('')

        has_images = False

        for i, ab in enumerate(antibodies):
            body.append(f'{"─" * 50}')
            body.append(f'{i + 1}. {ab.get("target", "Unnamed target").upper()}')
            body.append(f'{"─" * 50}')
            body.append(f'Application: {ab.get("application", "")}')
            body.append(f'Sample type: {ab.get("sample", "")}')

            figures = ab.get('figures', '')
            if figures:
                body.append(f'Figures: {figures}')
            body.append('')

            body.append('ANTIBODY IDENTITY')
            for field, label in [
                ('vendor', 'Vendor'),
                ('catNo', 'Catalogue #'),
                ('productUrl', 'Product URL'),
                ('rrid', 'RRID'),
                ('clone', 'Clone'),
                ('lot', 'Lot'),
                ('host', 'Host/clonality'),
                ('concentration', 'Concentration'),
            ]:
                val = ab.get(field, '')
                if val:
                    body.append(f'  {label}: {val}')
            body.append('')

            for field, label in [
                ('posControl', 'POSITIVE CONTROL'),
                ('negControl', 'NEGATIVE CONTROL'),
                ('orthogonal', 'ANTIBODY-INDEPENDENT READOUTS'),
            ]:
                val = ab.get(field, '')
                if val:
                    body.append(label)
                    body.append(val)
                    body.append('')

            control_figures = ab.get('controlFigures', '')
            if control_figures:
                body.append(f'  Control figures: {control_figures}')
                body.append('')

            independent = ab.get('independentEvidence', '')
            if independent:
                body.append('INDEPENDENT CHARACTERISATION EVIDENCE')
                body.append(independent)
                body.append('')

            body.append('')

        email_body = '\n'.join(body)

        # ── Build email ──
        subject = f'[Download] Validation Record: {author or "Anonymous"} — {len(antibodies)} antibod{"ies" if len(antibodies) != 1 else "y"} [{record_id}]'

        msg = EmailMessage(
            subject=subject,
            body=email_body,
            from_email=None,  # Uses DEFAULT_FROM_EMAIL from settings
            to=['onlygoodantibodies@gmail.com'],
            reply_to=[email] if email else [],
        )

        # ── Attach the generated PDF report ──
        pdf_file = request.FILES.get('pdf_file')
        if pdf_file:
            pdf_content = pdf_file.read()
            msg.attach(
                pdf_file.name or f'validation-record-{record_id}.pdf',
                pdf_content,
                'application/pdf'
            )

        # ── Attach the structured JSON record ──
        json_file = request.FILES.get('json_file')
        if json_file:
            json_content = json_file.read()
            msg.attach(
                json_file.name or f'validation-record-{record_id}.json',
                json_content,
                'application/json'
            )

        # ── Attach uploaded control images (processed in memory only) ──
        for key, uploaded_file in request.FILES.items():
            if key.startswith('image_'):
                content = uploaded_file.read()
                msg.attach(
                    uploaded_file.name,
                    content,
                    uploaded_file.content_type or 'application/octet-stream'
                )
                has_images = True

        # ── Flag for DOI follow-up ──
        if has_images:
            msg.body += '\n\n' + '=' * 50
            msg.body += '\nNOTE: This submission includes validation images.'
            msg.body += '\nConsider following up about creating a DOI record.'
            msg.body += '\n' + '=' * 50

        msg.send(fail_silently=False)

        return JsonResponse({'success': True})

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'Validation record submission failed: {e}')
        return JsonResponse({'success': False, 'error': 'Email send failed'})


# ── Embeddable Antibody Card ──────────────────────────────────
@xframe_options_exempt
def embed_antibody_card(request):
    """
    Public embeddable card for a single antibody.
    No authentication — this is public data rendered as HTML.

    Usage:
        /embed/?rrid=AB_2222383
        /embed/?catalogue=17987-1-AP
        /embed/?application=WB          (single application view)
        /embed/?compact=true            (smaller card)

    Iframe embedding:
        <iframe src="https://onlygoodantibodies.co.uk/embed/?rrid=AB_2222383"
                width="900" height="420" frameborder="0"></iframe>
    """
    from django.db.models import Q

    rrid = request.GET.get('rrid', '').strip()
    catalogue = request.GET.get('catalogue', '').strip()
    compact = request.GET.get('compact', '').lower() == 'true'
    application = request.GET.get('application', '').strip().upper()

    if not rrid and not catalogue:
        return render(request, 'core/embed_card.html', {
            'error': 'Please provide ?rrid= or ?catalogue= parameter.',
        })

    # Look up the antibody
    antibody = None
    if rrid:
        try:
            desc = Description.objects.select_related(
                'antibody__gene'
            ).get(rrid=rrid)
            antibody = desc.antibody
        except Description.DoesNotExist:
            pass
        except Description.MultipleObjectsReturned:
            desc = Description.objects.select_related(
                'antibody__gene'
            ).filter(rrid=rrid).first()
            if desc:
                antibody = desc.antibody
    elif catalogue:
        try:
            antibody = Antibody.objects.select_related(
                'gene', 'description'
            ).get(name=catalogue)
        except Antibody.DoesNotExist:
            pass
        except Antibody.MultipleObjectsReturned:
            antibody = Antibody.objects.select_related(
                'gene', 'description'
            ).filter(name=catalogue).first()

    if not antibody:
        lookup = rrid if rrid else catalogue
        return render(request, 'core/embed_card.html', {
            'error': f'Antibody not found: {lookup}',
        })

    # Get description
    desc = getattr(antibody, 'description', None)

    # Build experiments dict keyed by type
    exp_by_type = {}
    for exp in antibody.experiments.all():
        exp_by_type[exp.experiment_type] = exp

    # Build structured list for template iteration — only include experiments with images
    app_types = [
        ('WB', 'WB'),
        ('IP', 'IP'),
        ('ICC-IF', 'ICC-IF'),
        ('FC', 'FC'),
    ]

    # If application filter specified, restrict to that one
    valid_apps = {'WB', 'IP', 'ICC-IF', 'FC'}
    if application and application in valid_apps:
        app_types = [(application, application)]

    experiment_cards = []
    for code, label in app_types:
        exp = exp_by_type.get(code)
        if exp and exp.file_path:
            experiment_cards.append({
                'label': label,
                'has_data': True,
                'image_url': exp.file_path.url,
            })

    # Build recommended applications list
    applications = []
    if desc:
        if desc.wb_app:
            applications.append('Western Blot')
        if desc.ip_app:
            applications.append('IP')
        if desc.icc_if_app:
            applications.append('ICC-IF')
        if desc.fc_app:
            applications.append('FC')

    # Gene-level recommendation status
    gene_has_recommendations = Description.objects.filter(
        antibody__gene=antibody.gene
    ).filter(
        Q(wb_app=True) | Q(icc_if_app=True) | Q(ip_app=True) | Q(fc_app=True)
    ).exists()

    return render(request, 'core/embed_card.html', {
        'antibody': antibody,
        'desc': desc,
        'experiment_cards': experiment_cards,
        'applications': applications,
        'gene_has_recommendations': gene_has_recommendations,
        'compact': compact,
        'error': None,
    })
def portal(request):
    return render(request, 'core/portal.html')

# ─────────────────────────────────────────────────────────
# VIEW: Serve the recommendation manager page
# ─────────────────────────────────────────────────────────

@staff_member_required
def admin_recommendations(request):
    return render(request, 'core/admin_recommendations.html')


# ─────────────────────────────────────────────────────────
# API: Gene list with counts
# ─────────────────────────────────────────────────────────

@staff_member_required
def admin_rec_genes(request):
    """Returns all genes with antibody and recommendation counts."""
    from django.db.models import Q

    genes = Gene.objects.all().order_by('name')
    result = []

    total_antibodies = 0
    total_recommended = 0

    for gene in genes:
        abs_qs = gene.antibodies.all()
        ab_count = abs_qs.count()
        total_antibodies += ab_count

        rec_count = Description.objects.filter(
            antibody__gene=gene
        ).filter(
            Q(wb_app=True) | Q(icc_if_app=True) | Q(ip_app=True) | Q(fc_app=True)
        ).count()
        total_recommended += rec_count

        result.append({
            'name': gene.name,
            'antibody_count': ab_count,
            'rec_count': rec_count,
        })

    return JsonResponse({
        'genes': result,
        'total_antibodies': total_antibodies,
        'total_recommended': total_recommended,
    })


# ─────────────────────────────────────────────────────────
# API: Antibodies for a gene with experiments and recs
# ─────────────────────────────────────────────────────────

@staff_member_required
def admin_rec_antibodies(request):
    """Returns antibodies for a specific gene with full detail."""
    gene_name = request.GET.get('gene', '').strip()
    if not gene_name:
        return JsonResponse({'error': 'Missing gene parameter'}, status=400)

    try:
        gene = Gene.objects.get(name__iexact=gene_name)
    except Gene.DoesNotExist:
        return JsonResponse({'error': f'Gene not found: {gene_name}'}, status=404)

    antibodies = gene.antibodies.select_related('description').prefetch_related('experiments').all()

    results = []
    for ab in antibodies:
        desc = getattr(ab, 'description', None)

        experiments = []
        for exp in ab.experiments.all():
            if exp.file_path:
                experiments.append({
                    'app': exp.experiment_type,
                    'app_display': exp.get_experiment_type_display(),
                    'image_url': f'https://onlygoodantibodies.co.uk/media/{exp.file_path}',
                })

        recs = {}
        if desc:
            recs = {
                'WB': desc.wb_app,
                'IP': desc.ip_app,
                'ICC-IF': desc.icc_if_app,
                'FC': desc.fc_app,
            }

        results.append({
            'id': ab.id,
            'name': ab.name,
            'gene': gene.name,
            'rrid': desc.rrid if desc else None,
            'supplier': desc.supplier if desc else None,
            'host': desc.host if desc else None,
            'clonality': desc.clonality if desc else None,
            'recommendations': recs,
            'experiments': experiments,
        })

    return JsonResponse({'antibodies': results})


# ─────────────────────────────────────────────────────────
# API: Toggle a recommendation
# ─────────────────────────────────────────────────────────

@staff_member_required
@require_POST
def admin_rec_toggle(request):
    """
    Toggle a recommendation for a specific antibody + application.
    Expects JSON body: { "antibody_id": 123, "application": "WB", "value": true }
    Writes to db_core.sqlite3 (Description model).
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    antibody_id = data.get('antibody_id')
    application = data.get('application', '').strip()
    value = data.get('value', False)

    app_field_map = {
        'WB': 'wb_app',
        'IP': 'ip_app',
        'ICC-IF': 'icc_if_app',
        'FC': 'fc_app',
    }

    if application not in app_field_map:
        return JsonResponse({'error': f'Invalid application: {application}'}, status=400)

    try:
        desc = Description.objects.get(antibody_id=antibody_id)
    except Description.DoesNotExist:
        return JsonResponse({'error': f'No description for antibody {antibody_id}'}, status=404)

    field = app_field_map[application]
    setattr(desc, field, bool(value))
    desc.save(update_fields=[field])

    return JsonResponse({
        'status': 'ok',
        'antibody_id': antibody_id,
        'antibody_name': desc.antibody.name,
        'application': application,
        'value': bool(value),
    })