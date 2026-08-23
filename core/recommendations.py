"""What OGA recommends for one antibody in one application — the one reader.

OGA tests an antibody under the community consensus protocols, in one knockout
cell line, for one application, and recommends it or does not under those
conditions.

``SCOPE_NOTE`` is what a reader is owed about that, and it travels with the
data. It is stated once per surface rather than repeated on every value — a
caveat on every row is a caveat nobody reads.

The three values
----------------
``Antibody.wb_recommended`` and its three siblings are plain booleans, so
``False`` on its own answers two completely different questions with one value:

    * we tested this antibody for western blot and do not recommend it, and
    * nobody has tested it.

Only the first says anything. Reporting the second as if it were the first tells
a manufacturer their product failed testing that was never run on it, which is
the one output of this dataset that must never be wrong by accident.

Two signals disambiguate it, and **both are needed**:

``PublicationImage``
    A published figure for that application is the record that the application
    was tested at all.

The gene having any recommendation
    A gene nobody has curated yet can carry published figures and no flags, and
    that is the normal state of a gene mid-pipeline — figures go up as sessions
    are cropped, and the recommendations are set later, in one pass, on
    ``/pipeline/recommendations/``. Reading the figure alone turns every antibody
    on every uncurated gene into a documented failure the moment its first figure
    is published.

So::

    flag set                                     -> recommended
    no flag + figure for this application
                + the gene has been curated      -> not_recommended
    anything else                                -> not_tested

Three copies of this question exist in the repo and **they did not all agree**:

``mcp_servers/common/portal.py::_assessment``
    Three-valued with the gene gate. The reasoning above is its docstring's, and
    this module is that logic lifted out so the API can share it rather than
    import from an MCP server.

``core/extension_index.py::_verdicts``
    Lacked the gene gate until 5 Aug 2026, so on an uncurated gene it reported
    ``NOT_RECOMMENDED`` where this module reports ``NOT_TESTED``. It asks this
    module now.

``core/api_views.py::_serialise_antibody``
    Two-valued — the raw booleans, under ``recommendations``. Kept as it is,
    because the portal front end and every existing integration read that shape.
    New surfaces carry ``oga_recommendations`` beside it, not instead of it.
"""
from __future__ import annotations

# The four applications tested under the consensus protocols, in the order every
# OGA surface prints them.
APPLICATIONS = ('WB', 'IP', 'ICC-IF', 'FC')

RECOMMENDED = 'recommended'
NOT_RECOMMENDED = 'not_recommended'
NOT_TESTED = 'not_tested'

# Which boolean on Antibody carries the recommendation for each application.
_FLAG = {
    'WB': 'wb_recommended',
    'IP': 'ip_recommended',
    'ICC-IF': 'if_recommended',
    'FC': 'fc_recommended',
}

#: The consensus protocols the results come from: Ayoubi et al., 2024, *Nature
#: Protocols*, written with YCharOS, the industry–academic consortium. One copy,
#: because the public pages, the API and the framework page all cite it and a
#: second URL is the one that rots.
#:
#: **Not the Delphi study** (owner, 7 Aug 2026). That is a separate piece of
#: work — 32 experts rating interventions for funders, publishers and
#: institutions — and it is what the roadmap pages are built on. The protocols
#: were not written by that method, and the homepage card said they were.
CONSENSUS_PROTOCOL_URL = 'https://www.nature.com/articles/s41596-024-01095-8'

#: Said once, with the data, and not repeated per value.
#:
#: This is the owner's wording, 7 Aug 2026, and it is deliberately the whole of
#: the scientific caveat — two facts a reader can act on. What it replaced ran
#: to a paragraph per surface: that a recommendation is "not a verdict", that
#: `not_tested` "is not a negative result — map it to no data, never to a
#: failure", that absence from the dataset means the same. Untested is untested,
#: and a caveat long enough to need its own section is one nobody finishes.
#:
#: The second fact was sharpened on 12 Aug 2026, on the owner's instruction. It
#: had read "may not apply to other protocols and samples", which says the
#: result might not carry over — true, and weaker than what OGA means. A reader
#: holding a *recommended* verdict and a different sample type was being left to
#: infer whether their own experiment was covered, and the honest answer is that
#: it is neither supported nor contradicted. Both directions are stated, because
#: the harmful reading runs both ways: a pass here is not a licence for another
#: assay system, and a fail here is not a mark against somebody's working IHC.
SCOPE_NOTE = (
    'Results are based on consensus protocols. Antibody performance is '
    'protocol and sample dependent, and these results do not validate or '
    'invalidate experiments in other assay systems or sample types.')

#: The licence the data is under, stated once for the same reason ``SCOPE_NOTE``
#: is: it has to travel with the data, and it is now on four surfaces (the gene
#: pages' JSON-LD, the gene pages themselves, ``/data-access/`` and ``API.md``).
#: A second copy is the one that ends up naming a different licence.
LICENCE_NAME = 'CC BY 4.0'
LICENCE_URL = 'https://creativecommons.org/licenses/by/4.0/'

#: **What must be cited is the report DOI on the gene's own page**, not this
#: site (owner, 9 Aug 2026). Every gene that has a report carries one, and it is
#: what makes a reuse traceable back to the experiments rather than to a URL
#: that can move. A gene with no report yet has nothing to cite, which is why
#: every reader of this has to cope with the DOI being absent.
CITATION_NOTE = (
    'Free to reuse under CC BY 4.0. Cite the DOI of the report for the gene '
    'you used, shown on that gene’s page.')

# What each value means. Short on purpose: the scope is stated once above, and
# repeating a caveat on every row makes readers skip all of them.
MEANINGS = {
    RECOMMENDED: 'Recommended for this application in the conditions tested.',
    NOT_RECOMMENDED: 'Tested and not recommended in the conditions tested.',
    NOT_TESTED: 'Not tested for this application.',
}


def recommendation(antibody, application, tested_applications, gene_is_curated):
    """What OGA recommends for one antibody in one application.

    ``tested_applications`` is the set of ``PublicationImage.application_type``
    values that exist for this antibody; ``gene_is_curated`` is whether any
    antibody against the same gene carries any recommendation.

    Both are passed in rather than looked up, because the callers resolve them
    in bulk — one query for the whole page, not one per antibody.
    """
    if getattr(antibody, _FLAG[application], False):
        return RECOMMENDED
    if application in tested_applications and gene_is_curated:
        return NOT_RECOMMENDED
    return NOT_TESTED


def recommendations_for(antibody, tested_applications, gene_is_curated):
    """All four recommendations for one antibody, keyed by application."""
    return {
        app: recommendation(antibody, app, tested_applications, gene_is_curated)
        for app in APPLICATIONS
    }


def curated_gene_ids(target_ids=None):
    """Which targets have had their recommendations set.

    One query for the whole batch. ``_target_has_recommendations`` in
    ``api_views`` answers the same question one target at a time, which is fine
    for a gene page and is an N+1 on a feed — this is the bulk form.

    ``target_ids=None`` means every target, for callers that build a snapshot of
    the whole dataset (the extension index) and would otherwise pass a few
    hundred ids into an ``IN`` clause to learn the same thing.
    """
    from pipeline.models import Antibody
    from django.db.models import Q

    any_flag = (Q(wb_recommended=True) | Q(ip_recommended=True)
                | Q(if_recommended=True) | Q(fc_recommended=True))

    qs = Antibody.objects.all()
    if target_ids is not None:
        ids = list(target_ids)
        if not ids:
            return set()
        qs = qs.filter(target_id__in=ids)

    return set(
        qs.filter(any_flag)
          .values_list('target_id', flat=True)
          .distinct()
    )
