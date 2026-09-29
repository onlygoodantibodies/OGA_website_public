"""
Database reconciliation for the figure cropper (spec §6a, §11).

Wires the gene field to the live pipeline DB and drives the overwrite-safety
the owner asked for:

- gene_status(gene): what exists for this gene today — the Target, its existing
  antibodies, and which already have published images. Powers the front-of-tool
  banner and the "you would overwrite images" gate.
- candidate_catalogues(gene, pasted): the OCR match set = pasted list ∪ the
  gene's existing DB antibodies (so an existing gene maps cells to existing rows).
- match_company(name): resolve a pasted vendor to an EXISTING Company row where
  possible, so commit never spawns duplicate Company records (AUDIT §3).

Read-only helpers here; the transactional write lives in the commit step.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from pipeline.models import Target, Antibody, Company, PublicationImage
from pipeline.services import targets as target_svc


APP_TYPES = ["WB", "IP", "ICC-IF", "FC"]


def not_on_file_sentence(gene: str) -> str:
    """The refusal for a gene that is not in the pipeline — one writer for the
    banner and the save, so the sentence read before cropping is the one the
    save gives back.

    ``NA`` is not a gene that is missing, it is the placeholder for "no gene"
    (``targets.NOT_APPLICABLE``, which ``match_gene`` never resolves), so it
    must not send the reader to add a gene called NA."""
    if target_svc.is_not_applicable(gene):
        return (f"“{gene}” means no gene, and a figure is filed under the gene "
                f"it shows. Type that gene's symbol in the Gene box (box 2), "
                f"e.g. SOD1, then save again.")
    return (f"“{gene}” is not in the pipeline yet, and the cropper does not add "
            f"genes — a gene is added on the targets board, which checks the "
            f"symbol first. Add it there, then come back and save these crops.")


def add_target_url(gene: str) -> str:
    """The targets board with this gene filled in and the Add panel open — the
    same link the antibody paste gives (`bulk_antibodies._add_target_url`).
    Nothing for ``NA``, which is not a gene anybody should add."""
    if target_svc.is_not_applicable(gene):
        return ""
    from pipeline.services.bulk_antibodies import _add_target_url
    return _add_target_url(gene)


@dataclass
class AntibodyStatus:
    id: int
    catalogue_number: str
    company: str
    apps_with_images: list           # e.g. ["WB", "ICC-IF"]


@dataclass
class GeneStatus:
    gene: str
    exists: bool
    target_id: Optional[int] = None
    protein_name: str = ""
    uniprot_id: str = ""
    antibodies: list = field(default_factory=list)     # list[AntibodyStatus]
    images_by_app: dict = field(default_factory=dict)  # {"WB": 12, ...}
    # How the typed gene was found, and what it is filed under. A crop that
    # lands on LRRK2 because somebody typed PARK8 is the right outcome and a
    # silent one is not: this is what the banner says out loud, before any
    # cropping is done.
    matched_via: str = ""            # "name" | "alias" | ""
    resolved_gene: str = ""          # the spelling the crops will be filed under
    ambiguous: list = field(default_factory=list)

    @property
    def antibody_count(self) -> int:
        return len(self.antibodies)

    @property
    def has_images(self) -> bool:
        return any(self.images_by_app.values())

    @property
    def by_alias(self) -> bool:
        return self.matched_via == "alias"

    @property
    def blocked(self) -> str:
        """Why this gene cannot be committed, or "".

        One writer for the banner and the commit's refusal, so the sentence the
        reader is shown before they crop is the sentence the save gives back.
        """
        if self.ambiguous:
            names = ", ".join(self.ambiguous)
            return (f"“{self.gene}” is recorded as another name for more than one "
                    f"gene on file ({names}). Type the gene symbol you mean — "
                    f"filing these figures under the wrong one is not something "
                    f"the tool can undo for you.")
        if not self.exists and self.gene:
            # **A target is added on the targets doors and nowhere else**
            # (CLAUDE.md, "Adding a gene"). The cropper said "New gene — will be
            # created" here and then minted one with no check on the symbol, so
            # a typo in this box became a permanent gene with public figures
            # hanging off it. `commit.gene_refusal` says the same sentence at
            # the save.
            return not_on_file_sentence(self.gene)
        return ""

    @property
    def add_target_url(self) -> str:
        return add_target_url(self.gene) if (not self.exists and self.gene
                                             and not self.ambiguous) else ""

    @property
    def banner(self) -> dict:
        """A small structured status for the UI to render a banner + set the
        overwrite gate."""
        if self.blocked:
            return {"level": "ambiguous" if self.ambiguous else "not_on_file",
                    "requires_overwrite_ack": False,
                    "blocked": True, "text": self.blocked,
                    "add_target_url": self.add_target_url}
        # Said first and in every branch below, because it changes which gene's
        # public page these figures land on.
        alias = (f"“{self.gene}” is an older name for {self.resolved_gene}, which is "
                 f"already in the pipeline — these crops will be filed under "
                 f"{self.resolved_gene}. " if self.by_alias else "")
        named = self.resolved_gene or self.gene
        if not self.has_images:
            return {"level": "exists_no_images", "requires_overwrite_ack": False,
                    "blocked": False,
                    "text": (f"{alias}“{named}” exists with {self.antibody_count} "
                             f"antibodies but no published images. Crops will map "
                             f"to existing antibodies where the catalogue matches.")}
        imgs = ", ".join(f"{k}×{v}" for k, v in self.images_by_app.items() if v)
        return {"level": "exists_with_images", "requires_overwrite_ack": True,
                "blocked": False,
                "text": (f"{alias}⚠ “{named}” already has published figures ({imgs}). "
                         f"A crop for an antibody and application that already has "
                         f"one replaces it, and it is written to the file the "
                         f"public gene page shows — so that page can change as "
                         f"soon as you save, before anybody reviews it. Tick the "
                         f"box below to allow it.")}


def gene_status(gene: str) -> GeneStatus:
    """What is already on file for this gene — by symbol or by an older name.

    It asked ``gene_name__iexact`` alone, so a renamed symbol came back as a new
    gene and the banner offered to create one: the cropper was a press away from
    splitting a gene's antibodies and published figures across two target rows.
    ``targets.match_gene`` is the one reader for both, and this reports which of
    the two answered.
    """
    gene = (gene or "").strip()
    match = target_svc.match_gene(gene)
    if match.ambiguous:
        return GeneStatus(gene=gene, exists=False, ambiguous=match.ambiguous)
    target = match.target
    if target is None:
        return GeneStatus(gene=gene, exists=False, resolved_gene=gene)

    abs_status, images_by_app = [], {a: 0 for a in APP_TYPES}
    antibodies = (target.antibodies.using('pipeline_db')
                  .select_related("company")
                  .prefetch_related("publication_images"))
    for ab in antibodies:
        apps = sorted({img.application_type for img in ab.publication_images.all()})
        for a in apps:
            images_by_app[a] = images_by_app.get(a, 0) + 1
        abs_status.append(AntibodyStatus(
            id=ab.id,
            catalogue_number=ab.catalogue_number,
            company=company_label(ab.company) if ab.company else "",
            apps_with_images=apps,
        ))
    return GeneStatus(
        gene=gene, exists=True, target_id=target.id,
        protein_name=target.protein_name or "",
        uniprot_id=target.uniprot_id or "",
        antibodies=abs_status,
        images_by_app={k: v for k, v in images_by_app.items() if v},
        matched_via=match.matched_via,
        resolved_gene=match.gene_name,
    )


def candidate_catalogues(gene: str, pasted: list) -> list:
    """OCR match set: the confirmed paste list UNION the gene's existing DB
    antibodies (deduped, order-preserving)."""
    seen, out = set(), []
    for c in list(pasted or []) + [a.catalogue_number for a in gene_status(gene).antibodies]:
        c = (c or "").strip()
        key = c.upper()
        if c and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def resolve_company(vendor: str, catalogue: str = "", create: bool = True,
                    db: str = "pipeline_db") -> Optional[Company]:
    """Resolve a typed supplier to the supplier it is — **by display name first**.

    **A supplier is its display name** (owner, 29 Sep 2026): the name the public
    site and a manufacturer's portal key know it by, `display_name` or else
    `name`. The resolver used to let an exact `name` win over everything, so an
    empty second row called `BD Biosciences` beat the real `BD Bioscience` whose
    display name that is, and `Thermo Fisher` matched nothing and minted a row of
    its own — and every antibody filed there fell out of the Thermo Fisher
    Scientific key's feed. The order now:

    1. **An active supplier whose display name is what was typed.** Two sharing
       it — Bio-Techne's Novus and R&D brands — is the one genuine ambiguity, and
       the catalogue prefix settles it (`NB`/`BC` → Novus, else R&D).
    2. **An active supplier whose `name` is what was typed**, so typing a brand
       in full (`Bio-Techne (Novus Biologicals)`) is never overridden.
    3. **A name it is otherwise known by** — either half of a bracketed name —
       when exactly one active supplier carries it.
    4. **One active supplier whose display name starts with what was typed, or
       the other way round** (`Thermo Fisher` → Thermo Fisher Scientific), at
       five letters or more and only when exactly one answers. The preview says
       `(you typed "…")`, so the substitution is visible before anything saves.
    5. **A supplier waiting for approval** (`is_active=False`) by its exact name,
       so a second paste of the same new vendor joins the first request.
    6. Otherwise, with ``create``, **a new supplier waiting for approval**: the
       vial is still recorded, under a row nothing offers and no key sees, and a
       superuser is told (the hub; `pending_suppliers`) to make it canonical or
       merge it into the one it really is.
    """
    vendor = (vendor or "").strip()
    if not vendor:
        return None
    key = Company.canonical_key(vendor)
    companies = list(Company.objects.using(db).all())
    active = [c for c in companies if c.is_active]

    # A display name somebody *set* — not a row whose name stands in for one,
    # or a second row called `BD Biosciences` ties with the supplier whose
    # display name that is and the tie goes to the empty row.
    by_display = [c for c in active
                  if c.display_name and Company.canonical_key(c.display_name) == key]
    if len(by_display) == 1:
        return by_display[0]
    if len(by_display) > 1 or _is_biotechne(key):
        by_name = [c for c in active if Company.canonical_key(c.name) == key]
        if by_name:
            return by_name[0]
        want_novus = _wants_novus(catalogue)
        for c in active:
            k = Company.canonical_key(c.name)
            if "biotechne" not in k:
                continue
            if want_novus and "novus" in k:
                return c
            if not want_novus and "rdsystems" in k:
                return c
        if not create:
            return None
        return Company.resolve(_biotechne_brand(catalogue), db=db)[0]

    for c in active:
        if Company.canonical_key(c.name) == key:
            return c

    match = _by_alias(active).get(key)
    if match is not None:
        return match

    if len(key) >= _MIN_PREFIX:
        started = {display_key(c): c for c in active
                   if len(display_key(c)) >= _MIN_PREFIX
                   and (display_key(c).startswith(key) or key.startswith(display_key(c)))}
        if len(started) == 1:
            return next(iter(started.values()))

    for c in companies:
        if not c.is_active and Company.canonical_key(c.name) == key:
            return c
    if not create:
        return None
    company, created = Company.resolve(vendor, db=db)
    if created:
        company.is_active = False
        company.save(using=db, update_fields=["is_active"])
    return company


#: The shortest spelling a prefix match is trusted on: `Cell` would answer for
#: Cell Signaling and Cell Sciences at once, and is refused as ambiguous anyway.
_MIN_PREFIX = 5


def display_key(company) -> str:
    """The supplier a row is, as a canonical key: its display name, or its name."""
    return Company.canonical_key(company.display_name or company.name)


def canonical_suppliers(db: str = "pipeline_db") -> list:
    """The display names a supplier picker offers — one per supplier.

    Active rows only, deduplicated by display name, so Bio-Techne's two brands
    are one entry and nothing waiting for approval is offered as if it were on
    the list.
    """
    seen = {}
    for c in Company.objects.using(db).filter(is_active=True).order_by("name"):
        label = (c.display_name or c.name).strip()
        seen.setdefault(Company.canonical_key(label), label)
    return sorted(seen.values(), key=str.lower)


def pending_suppliers(db: str = "pipeline_db") -> list:
    """Suppliers typed in and waiting for a superuser: ``[(company, n_records)]``.

    Only rows something points at — an empty inactive row is a retired
    duplicate, not a request.
    """
    from django.db.models import Count
    rows = (Company.objects.using(db).filter(is_active=False)
            .annotate(n_ab=Count("antibodies", distinct=True),
                      n_cl=Count("cell_lines", distinct=True)))
    return [(c, c.n_ab + c.n_cl) for c in rows if c.n_ab + c.n_cl]


_BRACKETED = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def alias_keys(company) -> set:
    """The other spellings a supplier is known by, as canonical keys.

    Its public ``display_name``, and for a name ending in a bracket both halves:
    ``Developmental Studies Hybridoma Bank (DSHB)`` is also the full name alone
    and ``DSHB``. Never the ``name`` itself — that is the exact match, which
    wins over all of these.
    """
    spellings = [company.display_name or ""]
    m = _BRACKETED.match(company.name or "")
    if m:
        spellings += [m.group(1), m.group(2)]
    own = Company.canonical_key(company.name)
    return {k for k in map(Company.canonical_key, spellings) if k and k != own}


def _by_alias(companies) -> dict:
    """alias key → the one company carrying it; an alias two carry is dropped."""
    seen: dict = {}
    for c in companies:
        for k in alias_keys(c):
            seen.setdefault(k, []).append(c)
    return {k: cs[0] for k, cs in seen.items() if len(cs) == 1}


_BIOTECHNE_KEYS = {"novus", "novusbiologicals", "rdsystems", "randdsystems", "rd"}


def _is_biotechne(key: str) -> bool:
    return ("biotechne" in key) or key in _BIOTECHNE_KEYS


def _wants_novus(catalogue: str) -> bool:
    return (catalogue or "").upper().strip().startswith(("NB", "BC"))


def _biotechne_brand(catalogue: str) -> str:
    return ("Bio-Techne (Novus Biologicals)" if _wants_novus(catalogue)
            else "Bio-Techne (R&D Systems)")


_UNRESOLVED = object()


def company_label(company) -> str:
    """The supplier spelling a *record* is filed under.

    `.name`, never `.display_name`. display_name is the public website's
    preferred spelling; every screen that reads a pipeline record shows `.name`,
    and the paste preview goes out of its way to say which of the two your typing
    resolved to. So a clash banner announcing "already a row … from Abcam" named
    a supplier the board does not show and the write does not store — the exact
    substitution the preview had just corrected you away from. Sits beside
    `resolved_company_name` because they are the two halves of one question:
    that one is the name a *write* will store, this is the name a *record* is
    already stored under.
    """
    if company is None:
        return "(no supplier)"
    return company.name or company.display_name or "(no supplier)"


def resolved_company_name(vendor: str, catalogue: str = "",
                          db: str = "pipeline_db", existing=_UNRESOLVED) -> str:
    """The supplier name this row will be **stored** under. What a preview shows.

    `resolve_company(create=False)` answers a different question — *which
    supplier row exists right now* — and a preview built on it was wrong in both
    directions at once (run 5):

      * it returns `None` when the write is going to create the row, so a bare
        "Bio-Techne" against an `NB` catalogue previewed as typed and saved as
        `Bio-Techne (Novus Biologicals)`. Silence on the one path that can file a
        vial under the wrong vendor;
      * and callers read `display_name` off what it did return, which is the
        public-facing spelling, not the stored one. `abcam` previewed as
        `Abcam (you typed "abcam")` — a substitution announced for a row nothing
        was going to change.

    So this returns the `name` of the company `resolve_company(create=True)` would
    settle on, without creating anything. Compare it against what was typed and
    you get an annotation that is true.

    `existing` is for callers that have already done the non-creating lookup — the
    two bulk `plan`s need it for `company_status` anyway, and this reads every
    Company row, so asking twice per pasted line is a cost that grows with the
    paste. Pass `None` to say "looked, found nothing".
    """
    vendor = (vendor or "").strip()
    if not vendor:
        return ""
    if existing is _UNRESOLVED:
        existing = resolve_company(vendor, catalogue, create=False, db=db)
    if existing is not None:
        return existing.name
    if _is_biotechne(Company.canonical_key(vendor)):
        return _biotechne_brand(catalogue)
    return vendor


# Backwards-compatible alias (non-creating lookup for the dry-run summary).
def match_company(vendor: str, catalogue: str = "") -> Optional[Company]:
    return resolve_company(vendor, catalogue, create=False)


def _product_qs(target, vendor: str, catalogue: str, db: str):
    """Rows for one product: `(catalogue, company, target)`. Every vial of it."""
    catalogue = (catalogue or "").strip()
    company = resolve_company(vendor, catalogue, create=False, db=db)
    qs = (Antibody.objects.using(db)
          .filter(target=target, catalogue_number__iexact=catalogue))
    if company is not None:
        qs = qs.filter(company=company)
    return qs


def find_antibody(target, vendor: str, catalogue: str,
                  db: str = "pipeline_db", site_id=None) -> Optional[Antibody]:
    """Find the Antibody row *the product* refers to — a figure in a paper, an
    antibody named on a session sheet. Resolve the company first, then match on
    catalogue + target, case-tolerant. RRID is a secondary check, never the sole
    key (one RRID can map to >1 row).

    Product-level on purpose: a published figure names a catalogue number, not a
    vial. But one product can now hold a row per vial, so ``site_id`` says whose
    to prefer — without it, a Leicester session could attach to Montreal's vial
    purely because that row was created first. Falls back to any row, since the
    antibody being named is a stronger signal than the site not matching.

    To resolve a vial rather than a product — which is what a paste is doing —
    use ``find_vial``.
    """
    qs = _product_qs(target, vendor, catalogue, db)
    if site_id is not None:
        own = qs.filter(site_id=site_id).order_by("id").first()
        if own is not None:
            return own
    return qs.order_by("id").first()


def find_vial(target, vendor: str, catalogue: str, lot: str, site_id,
              db: str = "pipeline_db") -> Optional[Antibody]:
    """Find the row for one *vial*: the product, plus which lot and whose bench.

    An antibody is `(catalogue, company, target)` — the product you order. A row
    is one vial of it, which is why the table's constraint is those three plus lot
    and site. Leicester and Montreal can hold the same catalogue number from the
    same lot and still have physically different vials, tested separately and
    written up separately, so site always separates them and lot separates them
    again within a site.

    A blank lot means "nobody wrote the lot down", not "a different vial". An
    incoming lot therefore fills that blank in rather than forking a second row —
    otherwise pasting the same vial twice, once before the lot was known, leaves
    two records where the newer has the lot and the older has all the results.
    Symmetrically, a paste with no lot updates the row already on file rather than
    creating a lotless twin of it.

    Returns None when this site has no such vial — the caller creates one.
    """
    lot = (lot or "").strip()
    product = _product_qs(target, vendor, catalogue, db)
    candidates = list(product.filter(site_id=site_id).order_by("id"))
    if not candidates and site_id is not None:
        # Rows that predate anyone recording a site are unclaimed, not another
        # site's. Adopt one rather than creating a parallel row beside it: most
        # of the table was entered before site was filled in, and without this
        # the first paste after this rule changed would duplicate all of it.
        candidates = list(product.filter(site_id__isnull=True).order_by("id"))
    if not candidates:
        return None
    exact = next((a for a in candidates
                  if (a.lot_number or "").strip().lower() == lot.lower()), None)
    if exact is not None:
        return exact
    if lot:
        # A row that never recorded a lot is this vial with a gap, not another one.
        return next((a for a in candidates
                     if not (a.lot_number or "").strip()), None)
    # No lot given: nothing here can tell the vials apart, so don't invent one.
    return candidates[0]
