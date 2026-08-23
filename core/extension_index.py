"""
Build the lookup snapshot the browser extension matches against.

The extension does all of its matching locally — no page text is ever sent to
us — so it needs a compact copy of the dataset in the browser: RRID and
catalogue number to per-application recommendation, plus the list of genes that have
knockout-controlled data at all.

Reads the live pipeline models (PostgreSQL), same as the public antibody pages.

Two things about the data are easy to get wrong and matter enormously:

1. ``Antibody.*_recommended`` are plain booleans, so ``False`` on its own is
   ambiguous — it means "tested and not recommended" OR "never tested".
   Two signals disambiguate it and **both are needed**: a published
   ``PublicationImage`` for that application, *and* the gene having been curated
   at all. Untested is **not** a statement about quality and must never be shown
   as one. ``core/recommendations.py`` owns the rule; this file only maps its
   answers to the small integer codes the snapshot ships.

2. These are recommendations from testing under the consensus protocols, in one
   knockout cell line — not verdicts on a product. Performance is protocol- and
   sample-dependent, so "not recommended" means "not in the conditions tested"
   and says nothing either way about another assay system or sample type.

3. Recommendations are per application. A pass in Western blot says nothing about
   immunofluorescence, so each application is stored separately and the
   extension colours each mention for the application the page used it in.
"""

from __future__ import annotations

import datetime

from django.db.models import Max

from pipeline.models import (Antibody as PipelineAntibody, PublicationImage,
                             Report)
from pipeline.public import public_gene_names

from .recommendations import (NOT_RECOMMENDED as _R_NOT_RECOMMENDED,
                              NOT_TESTED as _R_NOT_TESTED,
                              RECOMMENDED as _R_RECOMMENDED,
                              curated_gene_ids, recommendation)

BASE_URL = 'https://onlygoodantibodies.co.uk'

# Recommendation codes, kept as small ints because this file is downloaded by every
# install and re-downloaded on every refresh.
NOT_TESTED = 0
NOT_RECOMMENDED = 1
RECOMMENDED = 2

# The four applications assessed under the consensus protocols.
APPLICATIONS = ('WB', 'IP', 'IF', 'FC')

# PublicationImage stores immunofluorescence as 'ICC-IF'; the extension uses 'IF'.
_IMAGE_APP_TO_KEY = {'WB': 'WB', 'IP': 'IP', 'ICC-IF': 'IF', 'FC': 'FC'}

# This file says 'IF'; the database and core/recommendations.py say 'ICC-IF'.
_KEY_TO_APPLICATION = {'WB': 'WB', 'IP': 'IP', 'IF': 'ICC-IF', 'FC': 'FC'}

_CODE = {
    _R_NOT_TESTED: NOT_TESTED,
    _R_NOT_RECOMMENDED: NOT_RECOMMENDED,
    _R_RECOMMENDED: RECOMMENDED,
}


def _verdicts(antibody, tested_types, gene_is_curated):
    """Per-application code for one antibody.

    ``tested_types`` is the set of ``PublicationImage.application_type`` values
    published for it; ``gene_is_curated`` is whether anything against the same
    gene carries a recommendation.

    The gene gate is the part that was missing, and it decides what this file
    says about real commercial products. A published figure was taken as proof
    the application had been assessed, so on a gene that has been cropped but
    not yet been through ``/pipeline/recommendations/`` — the ordinary state of
    a gene mid-pipeline, since figures go up as sessions are cropped and the
    recommendations are set later in one pass — every antibody was badged
    **tested and not recommended**. Not a rendering quirk: the extension puts
    that on a publisher's page next to a named product, telling a reader it
    failed knockout-controlled testing that nobody has run on it.

    ``core/recommendations.py`` is the one reader for the rule now, so this file and
    the portal API cannot drift apart again — they disagreed on exactly this
    for as long as both existed. The codes stay small ints because every install
    downloads this and re-downloads it on every refresh.
    """
    return {
        app: _CODE[recommendation(antibody, _KEY_TO_APPLICATION[app],
                                  tested_types, gene_is_curated)]
        for app in APPLICATIONS
    }


def _absolute(url):
    if not url:
        return None
    return url if url.startswith('http') else f'{BASE_URL}{url}'


def _one_to_one(lookup, antibodies):
    """Collapse identifier -> [rrid, ...] to identifier -> rrid.

    An identifier can map to several RRIDs for two very different reasons.
    Duplicate rows for the same product agree on the target and the verdicts, so
    there is nothing to resolve, and dropping them would silently make the
    antibody unmatchable by the identifier papers actually cite. Genuinely
    different products sharing an identifier cannot be told apart from page
    text, and those we do refuse to guess at.

    Returns (resolved, ambiguous).
    """
    resolved = {}
    ambiguous = set()
    for key, rrids in lookup.items():
        distinct = list(dict.fromkeys(rrids))
        if len(distinct) == 1:
            resolved[key] = distinct[0]
            continue
        verdicts = {(antibodies[r]['g'], tuple(sorted(antibodies[r]['a'].items()))) for r in distinct}
        if len(verdicts) == 1:
            resolved[key] = distinct[0]   # same product recorded twice
        else:
            ambiguous.add(key)            # different products, genuinely unresolvable
    return resolved, ambiguous


# What `generated` says when there is no published figure anywhere. A constant
# rather than today's date, so that even an empty snapshot is byte-identical
# from one build to the next -- which is the property the whole of _dataset_stamp
# exists to protect, and the one a test can pin. Only reachable in dev and in
# tests; live has thousands of figures.
EMPTY_DATASET_STAMP = '1970-01-01T00:00:00Z'


def _dataset_stamp():
    """When the data in this snapshot last changed -- NOT when it was serialised.

    A build-time clock reading (`timezone.now()`, which this was) makes the bytes
    different every hour on a dataset nobody has touched, and that defeats every
    cache between here and the reader: the ETag never matches, so Cloudflare
    re-fetches a megabyte it already holds and each install downloads one to be
    told what it already knew. Antibody records change monthly at most.

    A published figure is what makes an antibody public at all, and this file
    carries only antibodies that have one, so the newest figure dates the
    snapshot. A recommendation flipped without a new figure does not move this
    string -- and does not need to, because the ETag is a hash of the whole body,
    so the *bytes* still change and every cache still notices. This is the
    human-facing half: the extension's options page prints it as "data <date>".
    """
    newest = PublicationImage.objects.aggregate(newest=Max('created_at'))['newest']
    if newest is None:
        return EMPTY_DATASET_STAMP
    return (newest.astimezone(datetime.timezone.utc)
            .strftime('%Y-%m-%dT%H:%M:%SZ'))


def build_index(aliases=None):
    """Return the snapshot as a plain dict, ready to serialise."""
    antibodies = {}
    catalogue = {}
    clones = {}
    ambiguous = set()
    genes_with_records = set()
    # Published antibodies this index cannot carry, because it is keyed on RRID
    # and they have none. Counted so `counts.antibodies` is never read as the
    # size of the dataset: 1,611 against the site's 1,645 is a matching limit,
    # not a smaller set, and the CHANGELOG and the roadmap both quote the 1,611
    # as though it were the whole thing.
    no_rrid = 0

    report_doi_by_target = {}
    for report in Report.objects.all().only('target_id', 'f1000_doi', 'zenodo_doi'):
        if report.target_id in report_doi_by_target:
            continue
        doi = report.f1000_doi or report.zenodo_doi
        if doi:
            report_doi_by_target[report.target_id] = doi

    # Which genes anybody has curated, in one query for the whole snapshot
    # rather than one per antibody. See _verdicts for why it decides the answer.
    curated_targets = curated_gene_ids()

    queryset = (
        PipelineAntibody.objects
        .select_related('target', 'company')
        .prefetch_related('publication_images')
        .filter(target__gene_name__isnull=False)
        .exclude(target__gene_name='')
    )

    for antibody in queryset:
        gene = antibody.target.gene_name
        rrid = (antibody.rrid or '').strip()
        cat = (antibody.catalogue_number or '').strip()

        images = {}
        tested_types = set()
        for img in antibody.publication_images.all():
            key = _IMAGE_APP_TO_KEY.get(img.application_type)
            if key and img.image:
                images[key] = _absolute(img.image.url)
                tested_types.add(img.application_type)

        if not images:
            # Nothing has been published for this antibody, so we have no
            # assessment to report. Leaving it out means the extension treats it
            # as untested rather than inventing a recommendation.
            continue

        if not rrid:
            # Catalogue number alone is not unique across suppliers, so without
            # an RRID there is no key we could match on safely.
            no_rrid += 1
            continue

        genes_with_records.add(gene)

        supplier = ''
        if antibody.company:
            supplier = antibody.company.display_name or antibody.company.name or ''

        record = {
            'n': cat,
            'g': gene,
            's': supplier,
            'a': _verdicts(antibody, tested_types,
                           antibody.target_id in curated_targets),
            # Provenance is per record, so supplier-contributed knockout
            # validations can sit alongside YCharOS's own testing without the
            # two being merged into one verdict.
            'src': 'YCharOS',
            'ind': True,
        }
        if antibody.supplier_url:
            record['p'] = antibody.supplier_url
        if antibody.clonality:
            record['c'] = antibody.clonality
        if antibody.clone_id:
            # Papers cite a monoclonal by its clone at least as often as by its
            # catalogue number -- "anti-CaMKII (pan) (D11A10) antibody" names no
            # catalogue number at all. Carried on the record as well as in the
            # lookup so the card can show which clone it matched, or hovering
            # 'D11A10' would produce a card headed '4436' with nothing tying the
            # two together.
            record['cl'] = antibody.clone_id.strip()
        if antibody.host_species:
            record['h'] = antibody.host_species
        if getattr(antibody, 'out_of_market', False):
            record['d'] = True
        if images:
            record['img'] = images
        doi = report_doi_by_target.get(antibody.target_id)
        if doi:
            record['doi'] = doi

        antibodies[rrid] = record

        if cat:
            catalogue.setdefault(cat.lower(), []).append(rrid)
        if record.get('cl'):
            clones.setdefault(record['cl'].lower(), []).append(rrid)

    catalogue, ambiguous = _one_to_one(catalogue, antibodies)
    # Clones collide more often than catalogue numbers do -- the same clone is
    # routinely sold by several suppliers under their own numbers -- so the
    # ambiguity check earns its keep here rather than being purely defensive.
    clones, ambiguous_clones = _one_to_one(clones, antibodies)

    # Every gene with a public page, so the extension can tell "we have data on
    # this target, just not this antibody" (amber) from "we have nothing" (grey).
    # This must be the *public* set, not every Target: an internal target the lab
    # has not characterised has no page to send anyone to, so claiming it turns a
    # correct grey into an amber pointing at a 404.
    genes = public_gene_names()

    return {
        'schema': 1,
        'generated': _dataset_stamp(),
        'source': BASE_URL,
        'genes': genes,
        'aliases': aliases or {},
        'antibodies': antibodies,
        'catalogue': catalogue,
        'clones': clones,
        'ambiguous_catalogue': sorted(ambiguous),
        'ambiguous_clones': sorted(ambiguous_clones),
        'counts': {
            'genes': len(genes),
            'genes_with_antibody_records': len(genes_with_records),
            'antibodies': len(antibodies),
            # The gap between `antibodies` and the site's published total. A new
            # key rather than a rename: the index is a published artefact and a
            # signed extension in the field reads it.
            'antibodies_without_rrid': no_rrid,
            'clones': len(clones),
        },
    }


def _manifest_problems(manifest, ext_root):
    """Things a store will reject, or that will quietly break an install.

    Checked at package time rather than left to a reviewer to find. These are
    all static properties of the manifest, so once it is clean it stays clean.
    """
    import os

    problems = []

    for field in ('name', 'version', 'description', 'icons'):
        if not manifest.get(field):
            problems.append(f'manifest is missing {field!r}')

    description = manifest.get('description', '')
    if len(description) > 132:
        problems.append(f'description is {len(description)} characters; stores cap it at 132')

    for size in ('16', '48', '128'):
        rel = (manifest.get('icons') or {}).get(size)
        if not rel or not os.path.exists(os.path.join(ext_root, rel)):
            problems.append(f'missing the {size}px icon')

    if not manifest.get('browser_specific_settings', {}).get('gecko', {}).get('id'):
        problems.append('no gecko id, which Firefox (AMO) requires')

    # A broad pattern in host_permissions is granted at install: it moves Chrome
    # review onto the in-depth path and makes the prompt claim access the user
    # never agreed to. In optional_host_permissions it is granted only when the
    # user ticks it, which is how institutional proxies are covered at all --
    # those rewrite every journal hostname, so no domain list can reach them.
    # Opt-in broad access is a deliberate design choice, not a defect.
    broad = {'*://*/*', '<all_urls>', 'https://*/*', 'http://*/*'}
    for pattern in manifest.get('host_permissions', []):
        if pattern in broad:
            problems.append(f'host_permissions contains the broad pattern {pattern!r}')

    return problems


def build_zip_bytes():
    """Package the extension as a zip, carrying the *live* data snapshot.

    Returns (bytes, version). Used for the team download before a store listing
    exists, and as the artefact uploaded to the stores.

    The bundled ``data/index.json`` in the repo is a small seed for tests, so it
    is replaced here with data built from the live database. That means whoever
    downloads this gets an extension that works properly on real papers straight
    away, with no build step.
    """
    import io
    import json
    import os
    import zipfile

    from django.conf import settings

    ext_root = os.path.join(settings.BASE_DIR, 'browser-extension')

    # Development-only files that must not ship to users or to a store.
    # 'signed' holds the Mozilla-signed .xpi we host. Packaging it inside the
    # next build would ship a 150 kB copy of the previous release to every user
    # and hand a store reviewer a signed binary with no explanation.
    exclude_dirs = {'test', 'node_modules', '__pycache__', 'signed'}
    # package.json defines `npm test` and nothing the extension runs. Shipping
    # it would hand a store reviewer a devDependency on Playwright to explain.
    exclude_names = {
        '.DS_Store', 'make_icons.py', 'README.md', 'aliases.json',
        'package.json', 'package-lock.json',
    }

    with open(os.path.join(ext_root, 'manifest.json'), encoding='utf-8') as fh:
        manifest = json.load(fh)

    problems = _manifest_problems(manifest, ext_root)
    if problems:
        raise ValueError(
            'The extension manifest would be rejected or would misbehave:\n  - '
            + '\n  - '.join(problems)
        )

    version = manifest.get('version', '0.0.0')

    index = json.dumps(build_index(aliases=load_aliases()), separators=(',', ':'), sort_keys=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ext_root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            for name in filenames:
                if name in exclude_names or name.endswith(('.pyc', '.zip')):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, ext_root)
                if rel.replace(os.sep, '/') == 'data/index.json':
                    continue  # written from live data below
                zf.write(full, rel)
        zf.writestr('data/index.json', index)

    return buf.getvalue(), version


def load_aliases():
    """Common target names papers use, mapped to official gene symbols.

    Hand-maintained alongside the extension; missing entries only cost amber
    recall on antibodies named without a catalogue number.
    """
    import json
    from django.conf import settings

    path = settings.BASE_DIR / 'browser-extension' / 'data' / 'aliases.json'
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}
