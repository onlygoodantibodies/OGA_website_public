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

from . import target_confusions

from .recommendations import (APPLICATION_SCOPE as _R_APPLICATION_SCOPE,
                              CONDITIONS_QUALIFIER as _R_CONDITIONS,
                              CONSENSUS_PROTOCOL_URL as _R_PROTOCOLS_URL,
                              NOT_RECOMMENDED as _R_NOT_RECOMMENDED,
                              NOT_TESTED as _R_NOT_TESTED,
                              capability_axes as _R_capability_axes,
                              QUALIFIER_CODES as _R_QUALIFIER_CODES,
                              LIMITED_SUPPORT_NOTE as _R_LIMITED_NOTE,
                              qualifier_code as _R_qualifier_code,
                              verdict as _R_verdict,
                              RECOMMENDED as _R_RECOMMENDED,
                              SCOPE_NOTE as _R_SCOPE_NOTE,
                              SCOPE_SHORT as _R_SCOPE_SHORT,
                              curated_gene_ids, recommendation)

BASE_URL = 'https://onlygoodantibodies.co.uk'

#: Where the built snapshot is cached, and for how long.
#:
#: Defined here rather than in ``core/views.py`` because this module owns the
#: snapshot: the view reads the key, and ``invalidate_snapshot`` below clears
#: it. Two modules holding the string is how one of them ends up clearing a key
#: nobody serves.
EXTENSION_INDEX_CACHE_KEY = 'extension_index_v2'
EXTENSION_INDEX_CACHE_SECONDS = 3600


def invalidate_snapshot():
    """Drop the cached snapshot, so the next request rebuilds it.

    **A recommendation nobody can see is not a recommendation.** Setting a flag
    on ``/pipeline/recommendations/`` writes to PostgreSQL immediately and the
    board redraws — but ``/extension/index.json`` went on serving the previous
    bytes for up to an hour, with a Cloudflare edge hour behind that and a daily
    refresh in each install behind that. Up to about 26 hours from the save to a
    reader seeing it, and no screen anywhere said so: the board showed the new
    verdict, the extension showed the old one, and the only way to tell which
    was current was to know this number existed.

    Found on 28 Aug 2026, on SERPINA1/GTX112707 — the gene page said recommended
    for WB and a hover card on the same screen said not recommended.

    Clearing on write removes the first hour entirely. **The other two remain**:
    the response still carries ``max-age=3600`` so Cloudflare holds a copy (and
    lowering that trades a real bandwidth bill for it — see the root CLAUDE.md
    on what leaves the origin), and an install still refreshes daily. One hour
    is a different thing from a day, which is the whole of what this buys.

    Cheap by construction: the cache is per-process LocMem, so this is a dict
    delete. The cost is a rebuild on the next request after any write, which is
    why it is worth knowing it fires on **every** antibody save and not only on
    the flags — see ``core/apps.py``.
    """
    from django.core.cache import cache

    cache.delete(EXTENSION_INDEX_CACHE_KEY)

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


def _qualified(antibody, tested_types, gene_is_curated, axes_by_app):
    """This antibody's qualifiers — ``{application: code}``, sparse.

    A negative that *did* the thing its application is for is a different answer
    from one that showed nothing, and a supportive verdict the data fell short
    of is a different answer from a clean one. On live data that is 491 of the
    1,833 negatives and 312 of the 879 supportive western blots.

    **Carried beside the codes in ``a``, never inside them.** ``a`` stays the
    same three small ints it has always been, because every install
    re-downloads this file daily and would receive a fourth value long before a
    build that knew what to do with it — Chrome updates an extension silently
    within hours, but the data does not wait for the update at all. A build that
    has never heard of ``q`` ignores it and behaves exactly as it does today.

    **Codes rather than sentences.** This file is a megabyte before gzip and the
    wording belongs on the client, next to the other strings it draws;
    ``recommendations.QUALIFIER_CODES`` owns both, and a test pins that the
    client's table carries the same codes. Two letters also keeps the *grade*
    on a supportive immunofluorescence verdict, which a bare list of
    applications could not: `xs` and `sl` say how strongly, and the client needs
    that to tell "Supportive — strongly selective" from a shortfall.

    It was a list of application keys until 29 Aug 2026, read by no shipped
    build, so widening it costs nothing.
    """
    out = {}
    for app in APPLICATIONS:
        db_app = _KEY_TO_APPLICATION[app]
        axes = axes_by_app.get(db_app)
        value = _R_verdict(antibody, db_app, tested_types, gene_is_curated, axes)
        code = _R_qualifier_code(db_app, value, axes)
        if code:
            out[app] = code
    return out


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

    # The capability behind each negative, for the whole snapshot in one pass —
    # two queries per application, not two per antibody. This file walks every
    # published antibody, so a per-row lookup here is the N+1 that would show up
    # as a slow feed rather than as a wrong answer.
    axes = _R_capability_axes(
        list(queryset.values_list('pk', flat=True)))

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
        # Sparse: omitted entirely when nothing is qualified, which is most
        # records. A key present on every row would cost the whole install base
        # bytes for a fact about a minority of them.
        qualified = _qualified(
            antibody, tested_types, antibody.target_id in curated_targets,
            {app: axes.get((antibody.pk, app)) for app in _KEY_TO_APPLICATION.values()})
        if qualified:
            record['q'] = qualified
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

    # Antibodies whose DECLARED target is not the one people buy them for, plus
    # the published lists of papers that used them against the other protein.
    # Read from committed files, so it moves on a deploy and not on a write --
    # `_dataset_stamp` therefore still describes the antibody data, and the
    # ETag over the body is what tells a cache this changed.
    #
    # Additive, and `schema` does NOT move: every install re-downloads this file
    # daily and would receive the key long before a build that knows what to do
    # with it. A build that has never heard of `target_confusions` ignores it.
    # Absent entirely rather than null when nothing is on file, so a reader of
    # the JSON can tell "no notices deployed" from "deployed and nothing matched".
    confusions = target_confusions.index_payload()

    index = {
        'schema': 1,
        'generated': _dataset_stamp(),
        'source': BASE_URL,
        # What the verdicts do and do not cover, shipped WITH the data rather
        # than written into the extension.
        #
        # `recommendations.py::SCOPE_NOTE` is the one reader for this sentence
        # on every other surface, and the extension was the one place it could
        # not reach: a card is drawn from bundled JS, so a copy there would be
        # a second wording that drifts on the day the owner sharpens the first
        # — which has already happened twice (7 and 12 Aug 2026). Sending it in
        # the index makes the Python constant the source for the card too, and
        # a reworded caveat lands on every install at the next daily refresh
        # with no store review.
        #
        # `card.js` carries a fallback for an install whose cached index
        # predates this key; both are additive, so `schema` does not move.
        'scope': _R_SCOPE_NOTE,
        'scope_short': _R_SCOPE_SHORT,
        # Appended INSIDE the verdict, so the strong words never stand
        # alone. See CONDITIONS_QUALIFIER for why that is not the same
        # job as the scope note underneath.
        'conditions_qualifier': _R_CONDITIONS,
        'protocols_url': _R_PROTOCOLS_URL,
        # Keyed the way THIS FILE keys applications ('IF', not 'ICC-IF'), so the
        # extension can look one up with the same key it draws the tab under.
        # Built through `_KEY_TO_APPLICATION` rather than spelled again, or this
        # becomes the fifth place the two spellings have to agree.
        'application_scope': {
            key: _R_APPLICATION_SCOPE[app]
            for key, app in _KEY_TO_APPLICATION.items()
            if app in _R_APPLICATION_SCOPE
        },
        # The wording behind the qualifier codes, and the sentence that says
        # what the middle rung is worth. Shipped for the same reason as the
        # scope note: this is the text most likely to be reworded again — it
        # took three passes to land — and going through the index makes each
        # rewrite a deploy that reaches every install within a day, rather than
        # an AMO submission and a review wait.
        #
        # `card.js` and `content.js` keep the same table hardcoded as a
        # fallback, so an install whose cached index predates this key still
        # draws a sentence. The two can differ for a day during a rollout; that
        # is already true of the scope note and has never bitten.
        'qualifier_words': {code: clause
                            for code, (clause, _) in _R_QUALIFIER_CODES.items()},
        'limited_support_note': _R_LIMITED_NOTE,
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
    if confusions:
        index['target_confusions'] = confusions
    return index


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


def manifest_version():
    """The version `build_zip_bytes` would stamp on the artefact, or ``None``.

    Read rather than passed around because the manifest is the one place the
    version lives — `build_zip_bytes` names the zip from it, and both stores
    refuse a number they have already seen. A surface offering the download can
    therefore say which version it is about to hand over, which is the check
    against submitting a version that is already published.

    Answers ``None`` rather than raising: this is drawn on the pipeline hub, and
    a missing or malformed manifest must cost a line on a page, never the page.
    """
    import json
    import os

    from django.conf import settings

    path = os.path.join(settings.BASE_DIR, 'browser-extension', 'manifest.json')
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh).get('version') or None
    except (OSError, ValueError):
        return None


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
    # 'store' holds the listing screenshots and promo tiles — about 2 MB of
    # marketing assets for a page a reviewer reads, not code the extension
    # runs. It went in the day the assets landed and the allowlist test
    # caught it, which is exactly the shape that test exists for.
    exclude_dirs = {'test', 'node_modules', '__pycache__', 'signed', 'store'}
    # package.json defines `npm test` and nothing the extension runs. Shipping
    # it would hand a store reviewer a devDependency on Playwright to explain.
    #
    # CLAUDE.md is this directory's working notes. It shipped in the 0.2.5
    # package — caught by reading the artefact before submitting it, which is
    # the only thing that could have caught it: nothing failed, the extension
    # works perfectly with it inside, and the file simply was not on this list
    # because it was written after the list was. It is internal engineering
    # prose (measured accuracy figures, the release procedure, why particular
    # things are wrong) sitting in every user's install directory and in front
    # of a store reviewer.
    #
    # Note the shape: an EXCLUDE list fails open, so a new file in this
    # directory ships by default and no test says so. That is the opposite
    # trade-off to Render's ignored-paths filter (root CLAUDE.md, Deploy),
    # where failing open is what makes it safe — there an unlisted path
    # rebuilds a backup nobody was watching, here an unlisted file is published.
    # The allowlist that closes it lives in the test, not here, so adding a file
    # the extension really needs stays a one-line change with a test telling you
    # to make it.
    exclude_names = {
        '.DS_Store', 'make_icons.py', 'README.md', 'CLAUDE.md', 'aliases.json',
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
