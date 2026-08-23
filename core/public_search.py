"""Finding a published antibody by the identifier a reader actually holds.

The site could only ever be searched by gene, which answers *"what should I buy
for my target?"*. The other question — *"I have this vial / this is the reagent
in the paper I am reviewing — is it any good?"* — had no door at all, and typing
``14060-1-AP`` into either search box answered **"No gene matches"** about an
antibody that is in the dataset with two published figures. A confident miss on a
record we hold is worse than no feature: it reads as *we have not tested this* on
the one site whose whole claim is that it has.

Three things this module exists to keep straight.

**One normaliser.** A catalogue number is typeset differently by every publisher
and every supplier prints its own house style, so ``14060-1-AP`` arrives as
``14,060–1-AP``, ``14 060-1-AP`` or ``#14060-1AP``. Those rules are already
written down twice, deliberately mirrored —
``mcp_servers/common/manuscript.py`` and
``browser-extension/src/matcher.js::normaliseIdentifier`` — and a third copy here
would be the list that drifts. This module imports ``manuscript`` and adds no
rules of its own. It is pure ``re`` and stdlib, so importing it costs nothing
(``core/tests_public_genes.py`` already imports its neighbour).

**Matching happens in Python, over a cached list, not in SQL.** The published set
is small (~1,600 antibodies), so the whole thing is held in the cache and scanned.
The alternative — the ``Replace()`` annotation chain in
``mcp_servers/common/portal.py::_typeset_tolerant`` — has to spell the punctuation
out in SQL, which is a second, weaker copy of ``collapse_identifier``'s rule and
cannot be kept in step by anything. Here ``collapse_identifier`` itself is called,
so there is nothing to keep in step.

**A match that is not unique is a question, not a coin toss.** A catalogue number
is not unique across suppliers, which is why the extension refuses to index one
without an RRID (``core/extension_index.py``): it matches blind page text and has
nobody to ask. This module is behind a dropdown, so it can show every hit with its
supplier and gene beside it and let the reader pick — which is also why it can
index the ~29% of records that carry no usable RRID, where the extension cannot.
Nothing here ever silently picks one of several.

Scope is ``pipeline/public.py``'s: an antibody with no published figure is not in
the index, because there is nothing to show anyone who finds it.
"""
from __future__ import annotations

from django.core.cache import cache

from pipeline.rrid_utils import normalize_rrid

# The identifier rules, imported rather than restated — see the module docstring.
from mcp_servers.common.manuscript import (
    collapse_identifier, resolution_variants,
)

CACHE_KEY = 'public_antibody_search_index_v1'
CACHE_SECONDS = 3600

#: Suggestions shown for one query. The dropdown is a way in, not a result page.
SUGGESTION_LIMIT = 8

#: `limit=NO_LIMIT` — every match, for a caller collecting rather than offering.
NO_LIMIT = None

# Ranks, low is better. Kept as names because the sort reads better for it.
_EXACT = 0
_PREFIX = 1
_CONTAINS = 2


def _records():
    """Every published antibody, flattened to what a search hit needs.

    Cached whole: the set only changes when a figure is published, and rebuilding
    it costs one query. ``select_related`` matters — without it this is 1,600
    company lookups.
    """
    records = cache.get(CACHE_KEY)
    if records is not None:
        return records

    from pipeline.models import Antibody

    records = []
    queryset = (
        Antibody.objects
        .select_related('target', 'company')
        .filter(publication_images__isnull=False)
        .filter(target__gene_name__isnull=False)
        .exclude(target__gene_name='')
        .distinct()
    )
    for antibody in queryset:
        catalogue = (antibody.catalogue_number or '').strip()
        clone = (antibody.clone_id or '').strip()
        # The stored RRID has held full registry URLs and placeholders ("?") at
        # various times; `normalize_rrid` is the one reader that knows which of
        # those is an identifier and which is a blank wearing one's clothes.
        rrid = normalize_rrid(antibody.rrid) or ''
        if not (catalogue or clone or rrid):
            continue

        company = antibody.company
        supplier = ''
        if company:
            supplier = (company.display_name or company.name or '').strip()

        records.append({
            'id': antibody.pk,
            'catalogue': catalogue,
            'rrid': rrid,
            'clone': clone,
            'supplier': supplier,
            'gene': antibody.target.gene_name,
            'target_id': antibody.target_id,
            # Every form this record can be found by, pre-computed once so a
            # keystroke is a dict lookup rather than 1,600 normalisations.
            '_keys': _keys_for(catalogue, rrid, clone),
        })

    records.sort(key=lambda r: (r['gene'], r['catalogue']))
    cache.set(CACHE_KEY, records, CACHE_SECONDS)
    return records


def _keys_for(catalogue, rrid, clone):
    """Lower-cased lookup forms for one stored record.

    Both sides of a comparison go through this, so the publisher's typesetting is
    normalised away whether it landed in the paper or in our own database.
    """
    keys = set()
    for value in (catalogue, rrid, clone):
        for form in lookup_forms(value):
            keys.add(form)
    return keys


def lookup_forms(value):
    """Lower-cased forms ``value`` may be matched by, exactly.

    ``resolution_variants`` puts the string as given first and only ever adds
    forms, so a stored number that genuinely contains one of these characters
    still matches itself. ``collapse_identifier`` is last and returns ``""``
    below its own length floor, which is what stops short numbers matching each
    other across suppliers.
    """
    value = (value or '').strip()
    if not value:
        return []
    forms = []
    for form in resolution_variants(value):
        lowered = form.lower()
        if lowered and lowered not in forms:
            forms.append(lowered)
        collapsed = collapse_identifier(form).lower()
        if collapsed and collapsed not in forms:
            forms.append(collapsed)
    return forms


def _hit(record):
    """The public shape — the private key set never leaves this module."""
    return {k: v for k, v in record.items() if not k.startswith('_')}


def search(query, limit=SUGGESTION_LIMIT):
    """Published antibodies matching ``query``, best first.

    Three tiers, and the order is the whole of the ranking: an exact match on any
    typeset form, then a catalogue/RRID/clone the query starts, then one it
    appears in. Partial matching is there because the box is a dropdown somebody
    types into — ``1406`` should offer ``14060-1-AP`` before it has been finished.
    """
    query = (query or '').strip()
    if not query:
        return []

    exact = set(lookup_forms(query))
    lowered = query.lower()
    # Substring matching needs a floor: a single character appears in most
    # catalogue numbers on file, so `a` would "match" half the dataset and rank
    # it by nothing. An exact match is unaffected — that tier is above this one.
    partial = lowered if len(lowered) >= 2 else None

    scored = []
    for record in _records():
        rank = None
        if exact & record['_keys']:
            rank = _EXACT
        elif partial:
            for value in (record['catalogue'], record['rrid'], record['clone']):
                value = (value or '').lower()
                if not value:
                    continue
                if value.startswith(partial):
                    rank = _PREFIX
                    break
                if partial in value:
                    rank = _CONTAINS if rank is None else rank
        if rank is not None:
            scored.append((rank, record['gene'], record['catalogue'], record))

    scored.sort(key=lambda row: row[:3])
    if limit is NO_LIMIT:
        return [_hit(row[3]) for row in scored]
    return [_hit(row[3]) for row in scored[:limit]]


def resolve(query, records=None):
    """The one antibody ``query`` names, or ``None`` if it names none or several.

    Deliberately refuses a plural answer rather than taking the first: two
    products can share a catalogue number across suppliers, and picking one is how
    a reader is sent to a verdict about a reagent they do not own. Duplicate rows
    for the *same* product are not that — same gene and same catalogue is one
    answer recorded twice, so those resolve.
    """
    query = (query or '').strip()
    if not query:
        return None

    pool = records if records is not None else _records()
    forms = set(lookup_forms(query))
    matches = [r for r in pool if forms & r['_keys']]

    if not matches:
        # A row's own primary key, which is what the gene page puts in its links.
        if query.isdigit():
            wanted = int(query)
            matches = [r for r in pool if r['id'] == wanted]
        if not matches:
            return None

    if len({(r['target_id'], r['catalogue'].lower()) for r in matches}) > 1:
        return None
    return _hit(matches[0])


def resolve_on_target(target_id, query):
    """``resolve``, narrowed to one gene's own antibodies.

    The gene page's ``?ab=`` uses this rather than the site-wide resolver: a
    catalogue number shared with a different gene's antibody is unambiguous once
    you are already on a gene's page, and refusing it there would be the app
    declining to answer a question it can see the answer to.
    """
    mine = [r for r in _records() if r['target_id'] == target_id]
    return resolve(query, records=mine)


def target_ids(query):
    """Target ids whose published antibodies match ``query``.

    For the homepage's server-side ``?search=``, so pasting a catalogue number
    into the box and pressing Enter narrows the gene grid to the gene it belongs
    to instead of answering "no results".

    Unlimited on purpose: this narrows a page rather than filling a dropdown, so
    truncating it would drop genes whose antibodies genuinely match.
    """
    return {r['target_id'] for r in search(query, limit=NO_LIMIT)}


def clear_cache():
    cache.delete(CACHE_KEY)
