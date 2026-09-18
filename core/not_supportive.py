"""Antibodies whose every tested application came back *not supportive*.

A manufacturer asked the question this module answers: *which of my products
did OGA test and find nothing supportive for, in any application?* It is the
one slice of this dataset a supplier cannot assemble from the public gene
pages without walking every gene by hand, and it is the slice they most need,
because it is the list a product manager would act on.

**It is a view, not a new verdict.** Every value here comes from
``core/recommendations.py`` — the one reader for what OGA says about an
antibody in an application — and nothing in this file decides anything.
Re-deriving "did it pass" from the flags would be the fifth copy of a question
that already has one answer, and the first four did not agree
(``recommendations``'s own docstring is the record of that).

Three things shape it, and each is a way this could be wrong in the direction
that matters.

**Untested is not failed, and the gate that says so is the gene.**
``Antibody.wb_recommended`` and its three siblings are plain booleans, so
``False`` means *tested and not supportive* on a curated gene and *nobody has
run it* everywhere else. A list built on the boolean alone would tell a
manufacturer their product failed testing that was never performed on it —
which ``recommendations`` calls the one output of this dataset that must never
be wrong by accident. So an application counts here only where
``recommendation()`` returns ``NOT_RECOMMENDED``, which asks for a published
figure *and* a curated gene before it will say so.

**"Not supportive" is two rungs and a manufacturer needs both.** A negative
where the antibody was still seen to do the thing the application is for —
a western blot that detects and is not selective, an IP that enriches but not
significantly — reads as *Limited support*, and the site draws it amber rather
than red. Both are ``NOT_RECOMMENDED``, so both belong in this list; printing
them under one word would flatten a real distinction on the one screen whose
whole purpose is a supplier judging their own products. Every row carries the
words, the qualifier and the composed sentence the gene page prints, so the
supplier's row and the public page cannot say two different things.

**An antibody with no published figure at all is absent, not failing.** The
membership test is *at least one tested application, and not one of them
supportive*. The count of tested applications rides on every row, because
"failed all four" and "failed the only one anybody ran" are two different
statements about a product and a reader who cannot tell them apart has been
handed the stronger one.
"""
from __future__ import annotations

import csv
import io

from django.db.models import Prefetch, Q

from . import recommendations as R

DB = 'pipeline_db'

#: Said once on the envelope rather than repeated per row — ``api_pipeline``'s
#: rule, and ``recommendations.SCOPE_NOTE``'s before it. It states the two
#: things a supplier reading this list could otherwise get wrong, and it is
#: deliberately about what the list *is* rather than about what to do with it.
LIST_NOTE = (
    "Every antibody here was tested by OGA under the consensus protocols and "
    "no application it was tested in came back supportive. Applications nobody "
    "has run are not counted and are not listed: an untested application is "
    "absent from this list, never a failure in it. 'Limited support' rows are "
    "included and named apart — those are results where the antibody was still "
    "seen to do what the application is for, and they are not the same finding "
    "as 'Not supportive'."
)

#: What a reader is owed about scope, alongside the list. The dataset's own
#: caveat, not a second wording of it.
SCOPE_NOTE = R.SCOPE_NOTE

#: The CSV's columns, in order. One row per **application finding**, not per
#: antibody: the question is per-application and a wide row with four verdict
#: columns would leave a reader deciding which of the blanks meant *not tested*
#: and which meant *no such column*. `applications_tested` and
#: `applications_not_supportive` ride on every row so the antibody-level fact
#: survives the flattening.
CSV_COLUMNS = (
    'catalogue_number',
    'rrid',
    'supplier',
    'gene',
    'application',
    'finding',
    'qualifier',
    'sentence',
    'oga_support',
    'oga_recommendation',
    'applications_tested',
    'applications_not_supportive',
    'clone_id',
    'clonality',
    'host_species',
    'discontinued',
    'gene_page_url',
    'figure_url',
    'product_link',
    # The reference point: a supportive result for the SAME gene and the SAME
    # application, from whichever antibody has one — usually a different
    # product, sometimes a different supplier. Named `example_` rather than
    # `reference_` so no reader takes it for a reference standard; it is one
    # antibody that worked, not a benchmark.
    'example_supportive_catalogue',
    'example_supportive_supplier',
    'example_supportive_finding',
    'example_supportive_figure_url',
)


def scope(company_ids=None, allowed_genes=None):
    """The antibodies this caller may be shown, before the verdicts are read.

    ``company_ids=None`` is every supplier — what a registry key or an internal
    caller gets. ``allowed_genes`` is the demo-account gene list, applied the
    same way the feeds apply it.

    Only antibodies carrying at least one published figure, because a published
    figure is the record that an application was tested at all. That narrowing
    is what keeps this from walking all 3,225 rows to discard most of them.
    """
    from pipeline.models import Antibody, PublicationImage

    qs = (Antibody.objects.using(DB)
          .filter(publication_images__isnull=False)
          .exclude(target__isnull=True)
          .select_related('target', 'company')
          .prefetch_related(Prefetch(
              'publication_images',
              queryset=PublicationImage.objects.using(DB).order_by('application_type')))
          .distinct())
    if company_ids is not None:
        qs = qs.filter(company_id__in=list(company_ids))
    if allowed_genes:
        gene_q = Q()
        for gene in allowed_genes:
            gene_q |= Q(target__gene_name__iexact=gene)
        qs = qs.filter(gene_q)
    return qs.order_by('target__gene_name', 'catalogue_number')


def rows(company_ids=None, allowed_genes=None, base_url='',
         include_limited=False):
    """One dict per qualifying antibody, each carrying its per-application findings.

    ``include_limited`` widens the set from *nothing on-target was seen in any
    application* to *no application was supportive*. Off by default, because
    the harder-edged list is the one a product manager acts on first and the
    softer one reads as longer than the problem is.

    **The tick moves antibodies, never findings**, and that is the whole of why
    it is safe. Dropping *Limited support* rows out of an antibody that also
    has a flat negative would leave a card reading "1 of 2 applications came
    back without support" over one result — a count disagreeing with the list
    beneath it, which is what this codebase reads as data loss — and it would
    hide from a supplier a result about their own product. So a card always
    shows every one of that antibody's results; what the tick decides is which
    antibodies are on the page at all.

    Batched: the curated-gene set and the capability axes are resolved once for
    the whole scope, never per antibody. Resolving either in the loop is the
    N+1 that only shows as a slow feed, which is the shape
    ``TheFeedsDoNotQueryPerGeneTests`` exists to catch — and this walks the
    same scale of set.
    """
    antibodies = list(scope(company_ids, allowed_genes))
    if not antibodies:
        return []

    curated = R.curated_gene_ids({a.target_id for a in antibodies})
    axes = R.capability_axes([a.pk for a in antibodies])

    out = []
    for antibody in antibodies:
        tested = {img.application_type for img in antibody.publication_images.all()}
        figures = {img.application_type: img for img in antibody.publication_images.all()}
        gene_curated = antibody.target_id in curated

        findings, supportive_any = [], False
        for application in R.APPLICATIONS:
            if application not in tested:
                continue
            described = R.describe(antibody, application, tested, gene_curated,
                                   axes.get((antibody.pk, application)))
            if described['verdict'] == R.RECOMMENDED:
                supportive_any = True
                break
            if described['verdict'] != R.NOT_RECOMMENDED:
                # Tested in the sense that a figure exists, and the gene is not
                # curated yet, so OGA has not said anything about it. Not a
                # finding, and not a reason to leave the antibody out either —
                # that is what `applications_tested` counts.
                continue
            findings.append(_finding(application, described,
                                     figures.get(application), base_url))

        if supportive_any or not findings:
            continue
        if not include_limited and has_limited(findings):
            continue
        out.append(_row(antibody, findings, len(tested), base_url))
    return out


def has_limited(findings):
    """Does this antibody hold any *Limited support* result?

    The one reader for the question the tick asks, so the filter, the counts
    and the sentence beside the tick cannot come to three answers about one
    antibody.

    Asks the controlled value rather than the printed word (14 Sep 2026). The
    words are the half we reword — *Limited support* is itself a rewording of
    what the rung was first called — and a rewording that quietly stops
    matching here would empty the tick, change the default list, and say
    nothing on any screen.
    """
    return any(f['oga_support'] == R.LIMITED_SUPPORT for f in findings)


def _finding(application, described, figure, base_url):
    return {
        'application': application,
        # The rung a person is shown — *Not supportive* or *Limited support*.
        # Named `finding` rather than `verdict` because the controlled value is
        # the same word for both and this is the half that tells them apart.
        'finding': described['words'],
        'qualifier': described['qualifier'],
        'sentence': described['sentence'],
        # `finding` as a controlled value. This file has drawn the two rungs
        # apart since it was written — it is the one surface whose whole
        # purpose depends on the distinction — and published only the word for
        # it, so a supplier loading the CSV into their own system had to match
        # on prose. `oga_support` is the same fact in a value they can filter
        # on, and it is the same string the feed and the manifest now carry.
        'oga_support': described['support'],
        # The legacy controlled value, under the name the rest of this API
        # gives it. Kept: both negative rungs report `not_recommended` here,
        # which is exactly why the column above exists.
        # **Not** `verdict`: that word was removed from the public contract on
        # 7 Aug 2026 as the wrong one — these are recommendations from testing
        # under the consensus protocols, not settled judgements about a product
        # — and `core/tests_data_access.py` keeps it out of the reference.
        'oga_recommendation': described['verdict'],
        'tone': described['tone'],
        'figure_url': figure_url(figure, base_url),
    }


def _applications(findings):
    """All four applications in the order every OGA surface prints them.

    **The card is read as one picture, not as a list of separate results**, so
    it draws the whole strip the way the browser extension does — one chip per
    application, coloured by its rung — rather than only the ones that failed.
    A supplier comparing two of their products wants the shape of both at a
    glance, and a strip that omitted the applications nobody ran would make two
    very different products look identical.

    **The untested ones are the half that has to be there.** An application
    with no published figure is drawn grey and says *Not tested*, which is the
    one thing this list must never let a reader confuse with a failure. Leaving
    it off the strip would put that distinction nowhere on the card and make a
    one-application antibody look like a four-application one.

    Every application with a figure is necessarily a finding here: an antibody
    is in this list only because none of them is supportive, and an application
    with a figure on a curated gene can only be one or the other. So nothing in
    this strip is a fifth verdict — it is the findings, plus the gaps, in one
    row.
    """
    by_application = {f['application']: f for f in findings}
    out = []
    for application in R.APPLICATIONS:
        finding = by_application.get(application)
        if finding is not None:
            out.append(dict(finding, tested=True))
            continue
        out.append({
            'application': application,
            'finding': R.UNTESTED_WORDS,
            'qualifier': '',
            'sentence': '',
            # The one rung both vocabularies spell identically, so this is the
            # same string twice rather than a mapping.
            'oga_support': R.NOT_TESTED,
            'oga_recommendation': R.NOT_TESTED,
            'tone': '',
            'figure_url': '',
            'tested': False,
        })
    return out


def _row(antibody, findings, tested_count, base_url):
    from pipeline.services import clonality as clonality_svc

    company = antibody.company
    gene = antibody.target.gene_name or ''
    return {
        # The handle the PDF builder keys its figures on, and what the portal
        # keys a card on. Not an identifier anybody outside is asked to store.
        'antibody_id': antibody.pk,
        'catalogue_number': antibody.catalogue_number or '',
        'rrid': antibody.rrid or '',
        'supplier': ((company.display_name or company.name) if company else ''),
        'gene': gene,
        'gene_page_url': f'{base_url}/antibodies/{gene}/' if gene else '',
        'clone_id': antibody.clone_id or '',
        'clonality': clonality_svc.label(antibody) or '',
        'host_species': antibody.host_species or '',
        'discontinued': bool(antibody.out_of_market),
        'product_link': antibody.supplier_url or '',
        # Both counts, because "failed all four" and "failed the only one
        # anybody ran" are different statements about a product.
        'applications_tested': tested_count,
        'applications_not_supportive': len(findings),
        # Two lists, and they answer different questions. `findings` is what is
        # COUNTED — the results that came back without support, and the rows the
        # CSV writes. `applications` is what is DRAWN — all four, with the
        # untested ones present and grey. Keeping them apart is what stops the
        # strip's length from being read as a count of failures.
        'findings': findings,
        'applications': _applications(findings),
        # Whether the tick is what put this antibody on the page.
        'has_limited_support': has_limited(findings),
    }


def figure_url(figure, base_url=''):
    """The published figure's absolute URL, or ``''``.

    Same derivation as ``api_views._serialise_antibody``: local storage gives a
    ``/media/…`` path that needs the host in front of it, R2 gives an absolute
    URL already.
    """
    if figure is None or not figure.image:
        return ''
    url = figure.image.url
    return url if url.startswith('http') else f'{base_url}{url}'


def summary(row_list, would_add=None):
    """What the list holds, for a manifest a reader sees before they download.

    A download owes a manifest rather than a gate, and the numbers are the
    list's own — read off the same rows the file is built from, so the sentence
    on the screen and the file cannot disagree.

    ``would_add`` is the count of antibodies the *Limited support* tick would
    put on the page, passed in by a caller that has resolved the wider set. A
    tick whose effect is only discoverable by pressing it is one nobody presses
    — and on this page the unpressed state is a shorter list of somebody's
    failing products, which is exactly the number a reader must not take as the
    whole of it.
    """
    findings = [f for row in row_list for f in row['findings']]
    per_application = {}
    for finding in findings:
        per_application[finding['application']] = (
            per_application.get(finding['application'], 0) + 1)
    return {
        'antibodies': len(row_list),
        'findings': len(findings),
        'limited_support': sum(1 for f in findings
                               if f['oga_support'] == R.LIMITED_SUPPORT),
        'not_supportive': sum(1 for f in findings
                              if f['oga_support'] != R.LIMITED_SUPPORT),
        'figures': sum(1 for f in findings if f['figure_url']),
        'genes': len({row['gene'] for row in row_list if row['gene']}),
        'per_application': per_application,
        # What ticking *also include Limited support* would add. ``None`` when
        # the caller did not resolve the wider set, and 0 when it is already
        # showing — two different facts, so the page can say nothing rather
        # than say zero where it does not know.
        'limited_support_would_add': would_add,
    }


def to_csv(row_list):
    """The list as CSV bytes — one row per application finding.

    UTF-8 with a BOM, because Excel on Windows reads a BOM-less UTF-8 file as
    the local code page and a supplier's first act with this file is to open it
    in Excel.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(CSV_COLUMNS)
    references = {}
    for row in row_list:
        for entry in row.get('applications') or []:
            if entry.get('reference'):
                references[(row['antibody_id'], entry['application'])] = entry['reference']
    for row in row_list:
        for finding in row['findings']:
            merged = dict(row, **finding)
            example = references.get((row['antibody_id'], finding['application']))
            merged.update({
                'example_supportive_catalogue': (example or {}).get('catalogue_number', ''),
                'example_supportive_supplier': (example or {}).get('supplier', ''),
                'example_supportive_finding': (example or {}).get('sentence', ''),
                'example_supportive_figure_url': (example or {}).get('figure_url', ''),
            })
            writer.writerow([_cell(merged.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue().encode('utf-8-sig')


def _cell(value):
    if value is None:
        return ''
    if value is True:
        return 'yes'
    if value is False:
        return 'no'
    return value


def reference_figures(gene_names, base_url=''):
    """A supportive example per gene per application — the reference point.

    A negative result is only legible next to what a good one looks like. A
    supplier reading *not supportive for WB on RAB8A* cannot tell from the
    figure alone whether the blot is bad or whether RAB8A is simply hard, and
    the answer is on the same gene page: some other antibody's supportive blot,
    under the same protocol, in the same knockout line (owner, 14 Sep 2026).

    **The preferred example is supportive with no caveat.** ``describe`` sets
    ``tab`` on a supportive verdict the data fell short of — a western blot that
    detects the target and is not selective is still supportive, and still the
    wrong thing to hold up as the reference. So a clean one wins, and a
    caveated one is used only where the gene has nothing better; the caveat
    travels with it either way, because a reference whose own limitation is
    hidden is worse than none.

    **It is deliberately not scoped to the caller.** The question is *what does
    a good result on this gene look like*, and the answer is whichever antibody
    it is — often somebody else's. All of it is already on the public gene page
    the card links to, so nothing here is disclosed that a reader could not
    read off the website; what the surface must do is say plainly that the
    reference is a different product, which is the one way this could mislead.

    Batched like everything else here: three queries and one axes resolution
    for the whole set of genes, never one per gene.
    """
    from pipeline.models import Antibody, PublicationImage

    genes = [g for g in dict.fromkeys(gene_names) if g]
    if not genes:
        return {}

    gene_q = Q()
    for gene in genes:
        gene_q |= Q(target__gene_name__iexact=gene)
    candidates = list(
        Antibody.objects.using(DB)
        .filter(gene_q)
        .filter(publication_images__isnull=False)
        .select_related('target', 'company')
        .prefetch_related(Prefetch(
            'publication_images',
            queryset=PublicationImage.objects.using(DB).order_by('application_type')))
        .distinct()
        .order_by('catalogue_number'))
    if not candidates:
        return {}

    curated = R.curated_gene_ids({a.target_id for a in candidates})
    axes = R.capability_axes([a.pk for a in candidates])

    best = {}
    for antibody in candidates:
        gene = (antibody.target.gene_name or '') if antibody.target_id else ''
        if not gene:
            continue
        figures = {img.application_type: img
                   for img in antibody.publication_images.all()}
        tested = set(figures)
        gene_curated = antibody.target_id in curated
        for application in tested:
            described = R.describe(antibody, application, tested, gene_curated,
                                   axes.get((antibody.pk, application)))
            if described['verdict'] != R.RECOMMENDED:
                continue
            # `tab` is exactly "supportive, and the data fell short on one
            # axis". A clean example beats a caveated one; nothing else ranks.
            caveated = bool(described['tab'])
            key = (gene.upper(), application)
            held = best.get(key)
            # Take it if nothing is held, or if what is held is caveated and
            # this one is not. Ties keep the first, which the catalogue-number
            # ordering above makes stable rather than arbitrary.
            if held is not None and not (held['caveated'] and not caveated):
                continue
            best[key] = {
                'application': application,
                'catalogue_number': antibody.catalogue_number or '',
                'rrid': antibody.rrid or '',
                'supplier': ((antibody.company.display_name or antibody.company.name)
                             if antibody.company_id else ''),
                'finding': described['words'],
                'qualifier': described['qualifier'],
                'sentence': described['sentence'],
                'caveated': caveated,
                'figure_url': figure_url(figures.get(application), base_url),
            }

    out = {}
    for (gene_upper, application), row in best.items():
        out.setdefault(gene_upper, {})[application] = row
    return out


def attach_references(row_list, references):
    """Hang each row's per-application reference on its ``applications`` strip.

    Done here rather than in the browser so the CSV, the PDF and the page all
    name the same example — three surfaces picking their own "best supportive
    one" is the drift this module exists to avoid.
    """
    for row in row_list:
        per_application = references.get((row['gene'] or '').upper(), {})
        for entry in row['applications']:
            reference = per_application.get(entry['application'])
            # Never point a reference at the row it is a reference for.
            if reference and reference['catalogue_number'] == row['catalogue_number']:
                reference = None
            entry['reference'] = reference
    return row_list
