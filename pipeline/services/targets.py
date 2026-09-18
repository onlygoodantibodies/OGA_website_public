"""
Shared target resolve-or-create — the single spine for "add this gene if it's
not already in the pipeline".

Before this, targets were created three divergent ways: Feasibility made a full
record (UniProt protein name, accession, mass, synonyms), while the bulk-antibody
paste and the cropper created a bare stub (gene name only). This helper unifies
the inline paths so a target created while adding antibodies or cell lines is a
real record, not a stub — enriched from UniProt when it has to be created, with a
graceful fall back to a bare target when UniProt is unreachable.

Feasibility keeps its own richer flow (it already has the UniProt data the user
looked at in the browser); this is for the callers that only have a gene name.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple

from django.db.models import Q

from pipeline.models import Target, TargetNomination
from pipeline.services import gene_symbol, uniprot

logger = logging.getLogger(__name__)

DB = "pipeline_db"

# "There isn't one" — what a gene column says when the row has no gene, which is
# every wild type.
#
# The lab has always written `NA` there, and it is a perfectly good answer: a
# wild-type parental line is not a knockout *of* anything. The app had no such
# concept, so the value went through `resolve_or_create_target` and became a
# **Target called NA**, protein name "Not applicable" — and 96 of the 136 wild
# types in the live database now point at it. It appears in the gene list, in
# every gene filter and in the search box as though it were a gene, and the
# cell-line board prints it in the GENE column of rows whose whole point is that
# they have no gene.
#
# So the sentinel is recognised rather than stored: a gene cell that says NA
# means *no gene*, on the way in and on the way out. The existing rows are left
# alone deliberately — nothing needs deleting for the screens to be right, and a
# write against live data is a separate decision with a backup in front of it
# (see the production-data skill).
NOT_APPLICABLE = frozenset({"NA", "N/A", "N.A.", "NONE", "NOT APPLICABLE",
                            "NOT-APPLICABLE", "N/APPLICABLE", "-", "--", "—"})


def is_not_applicable(value) -> bool:
    """True when a gene cell says "there isn't one" rather than naming a gene.

    Takes what a person typed, a stored ``gene_name``, or a ``Target``.
    """
    if value is None:
        return False
    if hasattr(value, "gene_name"):
        value = value.gene_name
    return (str(value) or "").strip().upper() in NOT_APPLICABLE


def gene_terms(value) -> list:
    """The gene symbols a ``?gene=`` filter names — one, or a list of them.

    ``?gene=`` has always meant *an exact gene*, spelled the same way on all four
    boards so a link can carry one from board to board. It now also carries
    several, because adding a batch of genes has somewhere to land: a paste that
    creates six targets sends you to the board narrowed to exactly those six,
    rather than to 585 rows with the new ones somewhere in them.

    One gene is unchanged, which is the whole point — every existing link keeps
    meaning what it meant. Separators are commas or whitespace, because a person
    reading "STMN2, ELP3" in a URL will type it back with the space.

    Case-insensitive dedup, order preserved: the filter is built as an OR of
    ``iexact`` terms, so a repeated gene would only cost query size, and the
    order is what the reader typed.
    """
    if value is None:
        return []
    out, seen = [], set()
    for term in re.split(r"[,\s]+", str(value).strip()):
        if not term:
            continue
        key = term.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def gene_q(field: str, value) -> Optional[Q]:
    """``Q`` matching ``field`` against every gene ``value`` names, or ``None``.

    ``field`` is the path to the gene column from whatever is being filtered —
    ``gene_name`` on the targets board, ``target__gene_name`` on the other
    three. An OR of ``iexact`` terms rather than ``__in``, because ``__in`` is
    case-sensitive on PostgreSQL and the Access-era rows are not reliably
    upper-case.

    Returns ``None`` when the value names nothing, so a caller can tell "no gene
    filter" from "a gene filter that matches nothing" — the same distinction
    ``services/sites.py`` draws, and for the same reason: a filter that quietly
    widens to everything is worse than one that matches no rows.
    """
    terms = gene_terms(value)
    if not terms:
        return None
    q = Q()
    for term in terms:
        q |= Q(**{f"{field}__iexact": term})
    return q


def gene_q_with_na(relation: str, value) -> Optional[Q]:
    """``gene_q``, plus ``NA`` meaning *the rows with no gene at all*.

    ``relation`` is the path to the ``Target`` — ``"target"`` on the antibodies
    and cell-lines boards — because "no gene" has **three** shapes on file and
    only one of them is a gene name a filter could match:

    * no target at all (``CellLine.target`` is nullable; every wild type),
    * a target whose ``gene_name`` is blank (``Antibody.target`` is *not*
      nullable, so this is the only shape an antibody with no gene can take —
      and 8 of them are drawn with an empty GENE cell on the live board),
    * the Access-era placeholder target literally called ``NA``, which 96 of the
      wild types still point at.

    All three read as an empty gene cell, so all three have to answer the word
    the empty gene cell tells you to type.

    The cell-lines board has taken ``NA`` since the delete gate needed it (a wild
    type has no gene, so a gene filter can structurally never reach one, and every
    parental line would have been permanently undeletable). The antibodies board
    did not, and it has the same hole for the same reason: 8 antibodies on file
    have no target, so they answer no gene filter and there was no way to list
    them — while the board one tab over tells you to type exactly this. Two
    boards, one word, two behaviours is the shape of a search box that reads as
    broken from outside.

    Written once here rather than a third time in a view: the loop is what makes
    a *list* work (``?gene=NA,STMN2``), which ``gene_q`` alone cannot do, and it
    had already been copied once.
    """
    field = f"{relation}__gene_name"
    q, matched = Q(), False
    for term in gene_terms(value):
        matched = True
        if is_not_applicable(term):
            q |= (Q(**{f"{relation}__isnull": True})
                  | Q(**{f"{field}": ""})
                  | Q(**{f"{field}__isnull": True})
                  | Q(**{f"{field}__in": NOT_APPLICABLE}))
        else:
            q |= Q(**{f"{field}__iexact": term})
    return q if matched else None


def gene_of(target) -> str:
    """A target's gene symbol, or "" when the target is the NA placeholder.

    Every reader that prints a gene goes through this, so the 96 wild types
    attached to the NA row read as what they are — lines with no gene — rather
    than as knockouts of a gene called NA.
    """
    if target is None:
        return ""
    gene = getattr(target, "gene_name", "") or ""
    return "" if is_not_applicable(gene) else gene


def other_names(target) -> list:
    """Every other name recorded for a target, deduplicated, in one list.

    A gene's other names live in **two** columns and always have:
    ``alternative_name`` (Access's ``AlternProteinName``, and what
    ``resolve_or_create_target`` fills with UniProt's gene synonyms) and
    ``aliases`` (the gene symbols ``enrich_targets_from_uniprot`` writes). Only
    the first was drawn anywhere in the pipeline, so an alias added through the
    Downloads & uploads round trip — which is the route that page invites, "fill
    in missing values across many genes" — landed in a real, searchable field
    with **no home on any screen**: the gene page went on reading "Also known
    as: ANKTM1" and nothing anywhere said the save had worked.

    Which column a name is in is an artefact of which importer wrote it, not a
    distinction a reader can act on, so they are shown as one list rather than
    two labelled ones. The gene's own symbol is dropped — "also known as TRPA1"
    on TRPA1's own page is noise — as are the ``-`` placeholders the Access
    import left behind.
    """
    if target is None:
        return []
    gene = (getattr(target, "gene_name", "") or "").strip().upper()
    protein = (getattr(target, "protein_name", "") or "").strip().upper()
    out, seen = [], {gene, protein}
    for column in ("alternative_name", "aliases"):
        for name in re.split(r"[,;/]+", getattr(target, column, "") or ""):
            name = name.strip()
            key = name.upper()
            if not name or key in seen or key in NOT_APPLICABLE:
                continue
            seen.add(key)
            out.append(name)
    return out


def registry_names(target) -> list:
    """Every name that identifies this gene to an **external registry**.

    Not the same question as ``other_names`` above, which answers "what should
    the gene page print under *also known as*" — and deliberately drops the
    protein name, because repeating it there is noise. An outside database asks
    the opposite: the Antibody Registry names a target by its **protein**, so
    ``Podoplanin``, ``Alpha-1-antitrypsin``, ``Alpha-synuclein`` and
    ``Synaptophysin`` are precisely the strings a lookup has to accept for PDPN,
    SERPINA1, SNCA and SYP.

    Reusing the display list for matching cost 16 of 19 remaining REVIEW rows on
    the 30 Aug 2026 backfill — ten PDPN antibodies among them — because the one
    name the registry used was the one ``other_names`` exists to leave out. Two
    questions that look alike are two questions.

    Returns the gene symbol first, then the protein name, then the other names.
    """
    if target is None:
        return []
    out, seen = [], set()
    gene = (getattr(target, "gene_name", "") or "").strip()
    protein = (getattr(target, "protein_name", "") or "").strip()
    for name in [gene, protein] + list(other_names(target)):
        name = (name or "").strip()
        key = name.upper()
        if not name or key in seen or key in NOT_APPLICABLE:
            continue
        seen.add(key)
        out.append(name)
    return out


def sites_of(target, nominations=None) -> dict:
    """Which sites a target belongs to — ``{"sites": [Site, …], "from_import": bool}``.

    Two columns record this and **neither alone is honest**, which is exactly the
    shape of ``other_names`` above: which one a fact is in is an artefact of which
    importer wrote it, not a distinction a reader can act on.

    * ``TargetNomination.site`` is the live record. Everything added through the
      app since nominations existed is here, and it is what the boards, the
      Portfolio and the site filters read.
    * ``Target.site`` is the denormalised leftover of the Access import, and
      ``import_access_data`` writes it — the *same* site — on every one of the 508
      rows it creates, while creating **no nominations at all**. So for those
      targets it is the only record of whose they are.

    Reading only the second gave "Site —" for everything added since; reading only
    the first gave a **blank** for every imported target, which is where this came
    from. The thirteenth field test checked all 348 genes in Overview's Active
    list and found **192** showing a site there and nothing on the target board —
    Overview falling back to ``Target.site``, the board not. Two screens, one
    question, two answers, and the board's answer invites somebody to start
    filling in sites that are already recorded.

    So: the nominations when there are any, the import's site when there are not,
    and ``from_import`` says which — because a reader who cannot tell them apart
    will read the import's uniform "McGill" as a per-gene decision somebody made.
    Never squash the two into one list: a nomination is a record with a funder, a
    project and a date behind it, and this is a column on a spreadsheet from 2019.

    ``nominations`` may be passed when the caller already has them (the gene page
    fetches them with ``select_related`` for its own panel); otherwise the
    prefetch on ``target.nominations`` is used.
    """
    if target is None:
        return {"sites": [], "from_import": False}
    noms = target.nominations.all() if nominations is None else nominations
    sites = {n.site.pk: n.site for n in noms if n.site_id}
    if sites:
        return {"sites": sorted(sites.values(), key=lambda s: s.name or ""),
                "from_import": False}
    if getattr(target, "site_id", None) and target.site:
        return {"sites": [target.site], "from_import": True}
    return {"sites": [], "from_import": False}


def site_names(target, nominations=None) -> list:
    """Just the names from ``sites_of`` — for callers that only print them."""
    return [s.name for s in sites_of(target, nominations)["sites"]]


@dataclass
class GeneMatch:
    """What a typed gene name turned out to be.

    ``matched_via`` is ``"name"``, ``"alias"`` or ``""``, and the caller is
    expected to *say which* — a resolver that picks correctly still has to
    report what it picked, the rule ``services/cell_lines.py::preview`` already
    holds one board over. ``ambiguous`` is the third answer and the one that
    must never be collapsed into the others: the alias names more than one gene
    on file, so there is no answer, only a question.
    """
    typed: str = ""
    target: Optional[Target] = None
    matched_via: str = ""
    on_file_as: str = ""                            # gene_name, when matched by alias
    ambiguous: list = field(default_factory=list)   # gene names, when an alias names several

    @property
    def found(self) -> bool:
        return self.target is not None

    @property
    def by_alias(self) -> bool:
        return self.matched_via == "alias"

    @property
    def gene_name(self) -> str:
        """The spelling this gene is stored under — what a write should use."""
        if self.target is not None:
            return self.target.gene_name or self.typed
        return self.typed


def alias_owners(db: str = DB) -> dict:
    """``{OTHER_NAME_UPPER: [Target, ...]}`` — every name a target answers to
    besides its own symbol.

    Reads **both** columns through ``other_names``, which is the whole reason
    this exists rather than a third hand-written loop. A gene's other names live
    in ``alternative_name`` (Access's column, and UniProt's synonyms as written
    by ``resolve_or_create_target``) and in ``aliases`` (what
    ``enrich_targets_from_uniprot`` writes), and which one a name is in is an
    artefact of which importer ran, not a distinction anybody can act on. On the
    live data (14 Aug 2026) 471 of 583 targets carry the first and **448 carry
    the second**, so a reader that consults only one misses most of the symbols
    that matter: ``bulk_targets._existing_by_synonym`` read
    ``alternative_name`` alone, and every ``VEGFR1``/``LGP2``/``AMPK1`` sits in
    the column it was not reading.

    The whole table is walked rather than queried per name because there are 583
    rows of four short columns and the alternative is a `LIKE '%…%'` per gene,
    which matches substrings — ``RAB4`` inside ``RAB41`` — and is the wrong
    answer as well as the slower one.
    """
    owners: dict = {}
    for target in (Target.objects.using(db)
                   .only("pk", "gene_name", "protein_name",
                         "alternative_name", "aliases", "uniprot_id")):
        for name in other_names(target):
            owners.setdefault(name.upper(), []).append(target)
    return owners


def match_gene(gene: str, db: str = DB, owners: Optional[dict] = None) -> GeneMatch:
    """Resolve a typed gene to the target on file, by symbol **or by an old name**.

    Written because the figure cropper did not do this at all: it asked
    ``gene_name__iexact`` and, finding nothing, offered to create the gene. Type
    a symbol that has since been renamed — ``PARK8`` for LRRK2, ``CALM`` for
    CALM1 — and the tool that publishes figures would have made a *second*
    target for a gene already in the pipeline, with the antibodies and the
    published figures split across two rows that nothing joins.

    Two rules, and the second is why this returns a verdict rather than a Target:

    * **An exact name wins over every heuristic**, the rule
      ``cropper/db.py::resolve_company`` was rewritten for. It is load-bearing
      here: ``RAB41`` is a target of its own *and* is listed among ``RAB43``'s
      other names, so an alias-first reader would file RAB41's figures under
      RAB43.
    * **A name that matches several is a question, not a coin toss.** Thirteen
      other-names on the live data name two targets each — ``CALM`` is CALM1 and
      PICALM, ``SPARC`` is SPOCK2 and SPOCK3, ``RAB4`` is RAB4A and RAB4B — and
      guessing puts a western blot on the wrong gene's public page. The match
      comes back with ``ambiguous`` filled and no target, so the caller asks.

    ``owners`` is for callers resolving a batch: pass ``alias_owners()`` once
    rather than rebuilding the index per gene.
    """
    typed = (gene or "").strip()
    if not typed or is_not_applicable(typed):
        return GeneMatch(typed=typed)

    exact = Target.objects.using(db).filter(gene_name__iexact=typed).first()
    if exact is not None:
        return GeneMatch(typed=typed, target=exact, matched_via="name")

    if owners is None:
        owners = alias_owners(db=db)
    matches = owners.get(typed.upper()) or []
    # One target reached through two of its own names is still one answer.
    unique = {t.pk: t for t in matches}
    if len(unique) == 1:
        target = next(iter(unique.values()))
        return GeneMatch(typed=typed, target=target, matched_via="alias",
                         on_file_as=target.gene_name or "")
    if len(unique) > 1:
        return GeneMatch(typed=typed,
                         ambiguous=sorted(t.gene_name or "" for t in unique.values()))
    return GeneMatch(typed=typed)


def resolve_target(gene: str, db: str = DB) -> Optional[Target]:
    """Return the existing Target for a gene (case-insensitive), or None.

    ``NA`` and its spellings resolve to None: they say this row has no gene, and
    matching them against the placeholder target is how a wild type acquires one.
    """
    gene = (gene or "").strip()
    if not gene or is_not_applicable(gene):
        return None
    return Target.objects.using(db).filter(gene_name__iexact=gene).first()


def resolve_or_create_target(
    gene: str,
    *,
    enrich: bool = True,
    uniprot_data: Optional[dict] = None,
    member=None,
    db: str = DB,
) -> Tuple[Optional[Target], bool]:
    """Return ``(target, created)`` for a gene name.

    - Matches an existing target case-insensitively first (no write, no network).
    - When it must create, enriches from UniProt (protein name, accession, mass,
      synonyms) so the new record isn't a hollow stub. Pass ``uniprot_data`` to
      supply an already-fetched lookup (e.g. the cropper session) and skip the
      network call; pass ``enrich=False`` to create a bare target deliberately.
    - Dedup-safe: if UniProt's canonical gene symbol or accession already belongs
      to a target (i.e. the input was a synonym), returns that target instead of
      creating a duplicate. UniProt failures never raise — they fall back to a
      bare target so a paste still lands.

    Returns ``(None, False)`` for a blank gene, and for one that says ``NA`` —
    see ``NOT_APPLICABLE``. That one matters more than it looks: this function
    falls back to a bare target when UniProt cannot confirm a symbol, so before
    the sentinel existed a wild type whose gene column said NA created a target
    called NA and attached itself to it.
    """
    gene = (gene or "").strip()
    if not gene or is_not_applicable(gene):
        return None, False

    existing = Target.objects.using(db).filter(gene_name__iexact=gene).first()
    if existing:
        return existing, False

    up = uniprot_data
    if up is None and enrich:
        try:
            up = uniprot.lookup_gene(gene)
        except Exception as e:  # network / parsing — never fatal
            logger.warning("UniProt enrich failed for '%s': %s", gene, e)
            up = None
    up = up or {}
    found = bool(up.get("found"))

    # UniProt's primary symbol may differ from what was typed (synonym → canonical).
    #
    # Cased by `gene_symbol.canonical` rather than `.upper()`: uppercase is the
    # rule and `orf` is the exception, so a bare upper stored UniProt's own
    # `C9orf72` as `C9ORF72` — the write-path half of the eight mis-cased
    # symbols the twentieth field test found in the import.
    canonical = gene_symbol.canonical(up.get("gene_name") or gene) if found \
        else gene_symbol.canonical(gene)
    if canonical.upper() != gene.upper():
        existing = Target.objects.using(db).filter(gene_name__iexact=canonical).first()
        if existing:
            return existing, False

    # None (not "") when there's no accession — uniprot_id is UNIQUE, and an empty
    # string is not treated as NULL, so two blank accessions would collide.
    uniprot_id = ((up.get("uniprot_id") or "").strip() if found else "") or None
    if uniprot_id:
        taken = Target.objects.using(db).filter(uniprot_id=uniprot_id).first()
        if taken:
            # Another target already owns this accession — link to it, don't dup.
            return taken, False

    target = Target(
        gene_name=canonical,
        protein_name=(up.get("protein_name") or "") if found else "",
        uniprot_id=uniprot_id,
        theoretical_mass_kda=up.get("mass_kda") if found else None,
        alternative_name=", ".join(up.get("gene_synonyms") or []) if found else "",
        status=Target.Status.NOT_STARTED,
    )
    if member is not None and getattr(member, "pk", None):
        target.created_by = member
    target.save(using=db)

    # A gene created on the way past is still your site's gene.
    #
    # Work does not always arrive in order: a shipment turns up for a gene
    # nobody added, or somebody records results before the target exists. Every
    # write path can mint the target inline, which is right — but it minted one
    # with **no nomination**, and a target's site lives on its nominations. So
    # the gene existed and belonged to nobody: absent from every site filter,
    # uncounted on Overview, and reported by its own page as "not nominated by
    # any site yet". Feasibility's Add to Pipeline has recorded the nomination
    # since the first field test; the other four routes into this function had
    # not, and there is no reason for them to differ. Entering your lab's
    # antibodies for a gene says your lab is pursuing it just as plainly as
    # looking it up does.
    #
    # Unfunded, which is the honest starting state, and the gene page's progress
    # strip then prompts for whatever else is missing.
    site_id = getattr(member, "site_id", None) if member is not None else None
    if site_id:
        TargetNomination.objects.using(db).get_or_create(
            target_id=target.pk, site_id=site_id,
            defaults={"funded": False})
    return target, True


def published_gene_request(raw, application: Optional[str] = None,
                           db: str = DB) -> Tuple[str, str]:
    """What ``?gene=`` asks for on a figure-review page, resolved against the data.

    ``?gene=`` means the same thing on all four boards — an exact gene, so a link
    can carry one from board to board — and a page that reads the value without
    using it is a page whose picker sits on "Select a gene" while its own nav
    links point at the gene the reader asked for (twentieth field test). Two
    pages judge published figures a gene at a time (Set recommendations, Judge
    outcomes) and they must answer the same way, so the resolution is here rather
    than copied into each.

    Returns ``(gene, note)``. ``gene`` is the target's **own spelling**, resolved
    case-insensitively — the ``<option>`` values carry that spelling, and a
    ``<select>`` given a value no option has keeps its placeholder, which is
    indistinguishable from the parameter being ignored. It matters for the eight
    mis-cased symbols: a link written ``?gene=RAB44`` has to open ``Rab44``.

    ``note`` is what the page has to say when the request cannot be honoured, and
    there are two of those that look identical from outside: a gene that is not
    in the pipeline at all, and one that is but has **no published figures**,
    which is the only reason a real gene is absent from these pickers. A picker
    on its placeholder says neither.

    ``application`` narrows the figure test to one of ``PublicationImage``'s four
    values — ``'WB'`` for a page that judges western blots only, so a gene whose
    figures are all IP says that rather than opening empty.
    """
    from pipeline.models import Antibody

    text = (raw or "").strip()
    if not text:
        return "", ""

    # `?gene=` may name several — a bulk add lands on one — and these pages take
    # one gene at a time, so say which was taken rather than dropping the rest.
    terms = gene_terms(text) or [text]
    wanted, extra = terms[0], terms[1:]
    tail = (f" Showing {wanted} only — this page sets one gene at a time, and "
            f"{', '.join(extra)} {'were' if len(extra) > 1 else 'was'} not "
            "opened.") if extra else ""

    target = Target.objects.using(db).filter(gene_name__iexact=wanted).first()
    if target is None:
        return "", f"There is no gene called {wanted} in the pipeline." + tail

    gene = target.gene_name
    figures = Antibody.objects.using(db).filter(
        target_id=target.pk, publication_images__isnull=False)
    if application:
        figures = figures.filter(publication_images__application_type=application)
    if not figures.exists():
        kind = f"published {application} figures" if application \
            else "published characterisation figures"
        return "", (
            f"{gene} has no {kind} yet, so there is nothing to judge here. "
            "Crop one on Publish figures first — this page lists a gene once it "
            "has at least one." + tail)

    return gene, tail.strip()
