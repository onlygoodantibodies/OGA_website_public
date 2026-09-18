"""Checking and recording a gene the public asked us to characterise.

The public "Nominate a Target" button went to the contact form, so every request
ever made is prose in a Gmail inbox: unaggregated, unqueryable, and impossible
to answer *"which gene has the most people waiting for it?"* with. This module
is the one reader for the replacement — what a request is checked against before
it is accepted, and what gets written when it is.

**The check refuses two things and explains both, because a bare "no" reads as a
site that does not work.** A protein that is not human is out of scope by what
the consortium is (`organism_id:9606` is already in every UniProt query this
repo makes), and an antibody against a *post-translational modification* is out
of scope by the method: the whole verdict here rests on a knockout, and a
knockout removes the protein, so it cannot tell a phospho-specific antibody from
one that binds the unmodified form. Somebody asking for anti-pS129 α-synuclein
is not asking for something we have not got round to — they are asking for
something this design cannot answer, and saying so is more use than a queue
position.

**An outage is not a refusal.** ``uniprot.lookup_gene`` folds "no such gene" and
"could not reach UniProt" into ``found=False`` and tells them apart with
``unavailable`` — the distinction the bulk-add preview learned the hard way,
where a blocked proxy reported *"TRPA1 not in UniProt — check the spelling"*
about a real gene. Here the stakes are a stranger's only attempt to tell us what
they need, so an unreachable UniProt **accepts** the request and marks it
``unchecked`` rather than turning them away. A lost request cannot be recovered;
an unconfirmed symbol can be read by a human later.

**The check is not a permission slip.** ``record`` re-runs it, because the page
that previewed the answer and the press that writes it are separated by however
long somebody spent typing, and the gene could have been published in between.
"""
from __future__ import annotations

import re

# The four the rest of the app uses, spelled exactly as
# `PublicationImage.application_type` stores them — see services/review.py.
APPLICATION_CODES = ['WB', 'IP', 'ICC-IF', 'FC']

#: What a public reader sees. The labels spell the application out: "ICC-IF" is
#: the database's word, not a bench scientist's first guess at what to tick.
#:
#: **What somebody needs is a wider question than what OGA tests**, which is the
#: same distinction the antibody board draws between a supplier's six claimed
#: applications and the verdict's four (``antibody_board.SUPPLIER_APPLICATIONS``).
#: A request is demand, not a promise: IHC and a free-text OTHER are here so a
#: reader can say what they actually need, rather than picking the nearest of
#: four and leaving us to read a requirement that was never theirs.
APPLICATION_CHOICES = [
    ('WB', 'Western blot'),
    ('IP', 'Immunoprecipitation'),
    ('ICC-IF', 'Immunofluorescence / immunocytochemistry'),
    ('IHC', 'Immunohistochemistry'),
    ('FC', 'Flow cytometry'),
    ('OTHER', 'Something else'),
    # Not one of the four, and deliberately offered: forcing a guess produces a
    # tick that reads as a requirement and is not one. It is stored as typed.
    ('UNSURE', 'Not sure yet'),
]
_VALID_APPLICATIONS = {code for code, _ in APPLICATION_CHOICES}

#: The one code that carries free text beside it, in ``applications_other``.
OTHER = 'OTHER'

# Verdicts. Only OK and UNCHECKED are recordable; the rest are refusals with
# something to say.
OK = 'ok'
PUBLISHED = 'published'          # already on the public site — go and read it
IN_PIPELINE = 'in_pipeline'      # a Target here, nothing published yet
NOT_HUMAN = 'not_human'
MODIFICATION = 'modification'
UNKNOWN = 'unknown'              # UniProt answered, and knows no such protein
UNCHECKED = 'unchecked'          # UniProt could not be reached

#: Verdicts a request may be written under.
RECORDABLE = {OK, IN_PIPELINE, UNCHECKED}

# Species words somebody types when they mean a non-human orthologue. Matched as
# whole words on the typed string, before any lookup: "mouse Snca" resolves
# perfectly well to human SNCA once uppercased, so without this the app would
# accept a request for a reagent it is not going to make and say nothing.
_SPECIES = re.compile(
    r'\b(mouse|murine|mus musculus|rat|rattus|zebrafish|danio|xenopus|'
    r'drosophila|fly|yeast|s\. ?cerevisiae|c\. ?elegans|worm|bovine|cow|'
    r'porcine|pig|canine|dog|feline|cat|chicken|rabbit|monkey|macaque|'
    r'non-human|nonhuman)\b', re.IGNORECASE)

# Modification words. Matched only when the string has **more than one token**,
# because PHOSPHO1, SUMO1, UBC and METTL3 are real human genes and a single-word
# query is a symbol until UniProt says otherwise. "phospho-tau" and
# "acetylated histone H3" are phrases; "PHOSPHO1" is not.
_MODIFICATION_WORD = re.compile(
    r'\b(phospho\w*|dephospho\w*|acetyl\w*|deacetyl\w*|methyl\w*|'
    r'dimethyl\w*|trimethyl\w*|ubiquit\w*|sumoylat\w*|neddylat\w*|'
    r'glycosylat\w*|o-glcnac\w*|palmitoyl\w*|myristoyl\w*|prenylat\w*|'
    r'nitrosylat\w*|nitrat\w*|citrullinat\w*|carbonylat\w*|adp-ribosyl\w*|'
    r'cleaved|uncleaved|oxidised|oxidized|sulfat\w*|sulphat\w*)\b',
    re.IGNORECASE)

# A residue-and-position token, which is unambiguous where a bare "S100" is not
# (S100B is a gene). Only the spelled-out residues and the lower-case ``p``
# prefix a phospho-site is written with: pS129, pY705, Ser129, Thr181.
_RESIDUE = re.compile(
    r'\b(p[STY]\d{2,4}|(?:Ser|Thr|Tyr|Lys|Arg)\s?\d{1,4})\b')


def looks_like_modification(text: str) -> bool:
    """True when the string asks for a modification-specific antibody.

    Conservative on purpose: it is checked *before* UniProt is asked, so a false
    positive turns a real gene away. A single word is never a modification —
    ``PHOSPHO1`` is a human gene — so the modification vocabulary only counts in
    a phrase, while a residue-and-position token counts on its own.
    """
    text = (text or '').strip()
    if not text:
        return False
    if _RESIDUE.search(text):
        return True
    if len(re.findall(r'[A-Za-z0-9]+', text)) < 2:
        return False
    return bool(_MODIFICATION_WORD.search(text))


def looks_non_human(text: str) -> bool:
    """True when the string names a species we do not work on."""
    return bool(_SPECIES.search(text or ''))


def parse_applications(values) -> str:
    """The stored form of a set of ticked applications.

    Unknown codes are dropped rather than stored: this arrives from a public
    form, and a value nothing in the app can read is worse in a column than
    absent from it. Order follows ``APPLICATION_CHOICES`` so two identical
    requests store an identical string and group together.
    """
    wanted = {str(v).strip().upper() for v in (values or [])}
    wanted &= _VALID_APPLICATIONS
    return ', '.join(code for code, _ in APPLICATION_CHOICES if code in wanted)


def application_labels(stored: str, other: str = '') -> list:
    """``"WB, FC"`` → the words a reader sees. The one reader for the reverse.

    ``other`` is what was typed beside "Something else", and replaces the
    generic label with it: a tally reading *"Something else × 7"* counts seven
    requests and says nothing about any of them, which is the count-with-no-list
    failure one level down.
    """
    labels = dict(APPLICATION_CHOICES)
    out = []
    for code in (stored or '').split(','):
        code = code.strip()
        if not code:
            continue
        if code == OTHER and (other or '').strip():
            out.append(other.strip())
        else:
            out.append(labels.get(code, code))
    return out


def _lookup(text: str) -> dict:
    """``uniprot.lookup_interactive`` — the shared reader for a public lookup.

    A public page must not turn one visitor's keystrokes into one call each to
    somebody else's API, and the answer for a gene symbol does not change from
    hour to hour. That cache lived here and the selection tool had none, so the
    tool took the site down on 2 Sep 2026 while this page was safe; the policy
    is one reader now, and it carries a deadline and a thread cap as well as the
    cache. Cached on the *failure* too: an unreachable UniProt is unreachable
    for everybody, and re-asking per visitor makes an outage worse.
    """
    from pipeline.services import uniprot
    return uniprot.lookup_interactive(text)


def check(text: str) -> dict:
    """What we can say about a string somebody wants characterised.

    Returns ``status`` (one of the module constants), the resolved ``gene``,
    ``uniprot_id``, ``protein_name``, a reader-facing ``message``, and — when
    the gene is already here — ``target_id`` and ``gene_url``.

    **The database is asked before UniProt**, both because the answer is better
    (a gene we have published needs a link, not a form) and because it costs
    nothing. The modification and species tests come first of all: they need no
    network at all, and they are the two answers a reader most needs a reason
    for.
    """
    typed = (text or '').strip()
    out = {
        'status': UNKNOWN, 'query': typed, 'gene': '', 'uniprot_id': '',
        'protein_name': '', 'message': '', 'target_id': None, 'gene_url': '',
    }
    if not typed:
        out['message'] = 'Type a gene symbol.'
        return out

    if looks_like_modification(typed):
        out['status'] = MODIFICATION
        out['message'] = (
            f'“{typed}” looks like a request for an antibody against a '
            'post-translational modification, which is outside what we can '
            'currently assess. Every verdict on this site rests on a knockout '
            'control, and a knockout removes the whole protein — so it cannot '
            'show whether an antibody is specific for the modified form. If '
            'the unmodified protein would still be useful to you, nominate the '
            'gene on its own.')
        return out

    if looks_non_human(typed):
        out['status'] = NOT_HUMAN
        out['message'] = (
            f'“{typed}” names a non-human protein. Everything the consortium '
            'characterises is human, so we cannot take this on — but if the '
            'human orthologue would be useful to you, nominate that instead.')
        return out

    known = _known_gene(typed)
    if known is not None:
        return {**out, **known}

    answer = _lookup(typed)
    if answer.get('unavailable'):
        out['status'] = UNCHECKED
        out['message'] = (
            'We could not reach UniProt just now, so we have not been able to '
            'confirm this symbol. Send the request anyway — it is recorded '
            'either way and we will check it ourselves.')
        return out

    if not answer.get('found'):
        out['status'] = UNKNOWN
        out['message'] = (
            f'UniProt has no human protein under “{typed}”. It may be a '
            'spelling, an alias UniProt does not carry, or a protein from '
            'another species — we only characterise human proteins. Check the '
            'symbol at uniprot.org and try again.')
        return out

    gene = (answer.get('gene_name') or typed).strip().upper()
    # A lookup by accession resolves to a symbol, and that symbol may already be
    # a gene we hold — ask again with what UniProt actually said.
    if gene != typed.upper():
        known = _known_gene(gene)
        if known is not None:
            return {**out, **known, 'gene': gene}

    out.update({
        'status': OK,
        'gene': gene,
        'uniprot_id': answer.get('uniprot_id', ''),
        'protein_name': answer.get('protein_name', ''),
    })
    out['message'] = (
        f'{gene} — {out["protein_name"] or "human protein"} '
        f'({out["uniprot_id"]}). Not characterised by us yet.')
    return out


def _known_gene(symbol: str):
    """This gene's status here, or ``None`` when we hold no such target.

    Published and unpublished are different answers to a reader: one is a page
    to go and read, the other is *we are already on it* — which is worth saying,
    and worth recording a request against, because a request is what tells a
    prioritisation meeting which of the several hundred unpublished targets
    anybody outside is waiting for.
    """
    from django.urls import reverse
    from pipeline.models import Target
    from pipeline.public import public_targets

    symbol = (symbol or '').strip()
    target = (Target.objects.filter(gene_name__iexact=symbol)
              .only('pk', 'gene_name', 'uniprot_id', 'protein_name').first())
    if target is None:
        return None

    common = {
        'gene': target.gene_name or symbol.upper(),
        'uniprot_id': getattr(target, 'uniprot_id', '') or '',
        'protein_name': getattr(target, 'protein_name', '') or '',
        'target_id': target.pk,
    }
    if public_targets().filter(pk=target.pk).exists():
        return {
            **common,
            'status': PUBLISHED,
            'gene_url': reverse('antibody_table',
                                kwargs={'gene_name': common['gene']}),
            'message': (f'{common["gene"]} is already on the site — '
                        'there is characterisation data for it now.'),
        }
    return {
        **common,
        'status': IN_PIPELINE,
        'message': (f'{common["gene"]} is already on our list and being worked '
                    'on, with nothing published yet. Tell us you are waiting '
                    'for it and which application you need — that is what '
                    'decides the order things get run in.'),
    }


def record(*, typed_text, email, applications=(), applications_other='',
           requester_name='', organisation='', has_funding=False,
           funding_note='', note='', source='', verdict=None):
    """Write one request, re-checking first. Returns ``(request, verdict)``.

    ``request`` is ``None`` when the verdict refuses — the caller draws the
    verdict's own message rather than inventing a second refusal. Passing
    ``verdict`` skips the re-check and is for tests and for a caller that has
    just done it; every real write path lets this function ask again, because
    the gene may have been published while the form was being filled in.
    """
    from pipeline.models import GeneRequest

    verdict = verdict or check(typed_text)
    if verdict['status'] not in RECORDABLE:
        return None, verdict

    request = GeneRequest.objects.create(
        typed_text=(typed_text or '').strip()[:120],
        gene_symbol=(verdict.get('gene') or '')[:40],
        uniprot_id=(verdict.get('uniprot_id') or '')[:20],
        protein_name=(verdict.get('protein_name') or '')[:255],
        applications=parse_applications(applications),
        applications_other=(applications_other or '').strip()[:120],
        email=(email or '').strip()[:254],
        requester_name=(requester_name or '').strip()[:120],
        organisation=(organisation or '').strip()[:160],
        has_funding=bool(has_funding),
        funding_note=(funding_note or '').strip(),
        note=(note or '').strip(),
        target_id=verdict.get('target_id'),
        checked=(GeneRequest.CHECKED_UNCHECKED
                 if verdict['status'] == UNCHECKED
                 else GeneRequest.CHECKED_HUMAN),
        source=(source or '')[:20],
    )
    return request, verdict


def by_gene():
    """Every request, grouped by gene, most-asked first — for the staff page.

    Grouped in Python over the whole table rather than in SQL: the aggregate a
    reader wants is not one a ``values().annotate()`` gives without a second
    query (the application tally and the funding flag come from the same rows),
    and the table is a public form's output — hundreds, not millions. Revisit
    when it stops being.

    The key is the confirmed symbol where there is one and the typed string
    where there is not, so an unchecked row is never silently folded into a
    confirmed gene's count.
    """
    from pipeline.models import GeneRequest

    groups = {}
    for req in GeneRequest.objects.select_related('target').all():
        key = req.gene_symbol or req.typed_text
        group = groups.setdefault(key, {
            'gene': req.gene_symbol, 'typed': req.typed_text, 'count': 0,
            'requests': [], 'applications': {}, 'funded_offers': 0,
            'target_id': req.target_id, 'unchecked': 0, 'latest': None,
        })
        group['count'] += 1
        group['requests'].append(req)
        for label in application_labels(req.applications, req.applications_other):
            group['applications'][label] = group['applications'].get(label, 0) + 1
        if req.has_funding:
            group['funded_offers'] += 1
        if req.checked == GeneRequest.CHECKED_UNCHECKED:
            group['unchecked'] += 1
        if req.target_id and not group['target_id']:
            group['target_id'] = req.target_id
        if group['latest'] is None or req.created_at > group['latest']:
            group['latest'] = req.created_at

    rows = list(groups.values())
    # Most-asked first; ties broken by recency so a new request is visible.
    rows.sort(key=lambda r: (-r['count'], -(r['latest'].timestamp() if r['latest'] else 0)))
    return rows
