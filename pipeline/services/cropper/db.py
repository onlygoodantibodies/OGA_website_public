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

from dataclasses import dataclass, field
from typing import Optional

from pipeline.models import Target, Antibody, Company, PublicationImage
from pipeline.services import targets as target_svc


APP_TYPES = ["WB", "IP", "ICC-IF", "FC"]


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
        return ""

    @property
    def banner(self) -> dict:
        """A small structured status for the UI to render a banner + set the
        overwrite gate."""
        if self.blocked:
            return {"level": "ambiguous", "requires_overwrite_ack": False,
                    "blocked": True, "text": self.blocked}
        if not self.exists:
            return {"level": "new", "requires_overwrite_ack": False,
                    "blocked": False,
                    "text": f"New gene — “{self.gene}” will be created."}
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
                "text": (f"{alias}⚠ “{named}” already has published images ({imgs}). "
                         f"Committing will OVERWRITE matching images — tick to allow.")}


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
    """Resolve a pasted vendor to the correct Company, following the dedup
    write rules in CLAUDE.md:

    - Match case/punctuation-insensitively via `Company.canonical_key` so we never
      re-create `abcam`/`Abcam`-style variants.
    - **Bio-Techne is split into two brands** that both display "Bio-Techne":
      catalogue starting `NB`/`BC` → Novus, everything else → R&D Systems. Pick the
      brand by catalogue prefix, never by display name.
    - `create=False` does a non-creating lookup (for the dry-run "will create
      supplier" notice); `create=True` uses `Company.resolve` to get-or-create a
      canonical row at commit time.
    """
    vendor = (vendor or "").strip()
    if not vendor:
        return None
    key = Company.canonical_key(vendor)
    companies = list(Company.objects.using(db).all())

    # An exact name wins over every heuristic below.
    #
    # The Bio-Techne rule exists because a pasted vendor of just "Bio-Techne" is
    # ambiguous and the catalogue prefix is the only thing that disambiguates it.
    # It was applied to *unambiguous* input too: someone who typed
    # "Bio-Techne (Novus Biologicals)" in full had their row saved against R&D
    # Systems, because their catalogue number did not begin NB or BC. The third
    # field test caught it — the preview showed one supplier and the save wrote
    # another, which is the one thing a preview must never do.
    #
    # So: if the name they typed *is* a company already on file, that is the
    # company. Guessing is for when there is nothing to go on.
    for c in companies:
        if Company.canonical_key(c.name) == key:
            return c

    if _is_biotechne(key):
        want_novus = _wants_novus(catalogue)
        for c in companies:
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

    for c in companies:
        if Company.canonical_key(c.name) == key:
            return c
    if not create:
        return None
    return Company.resolve(vendor, db=db)[0]


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
