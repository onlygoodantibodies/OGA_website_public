"""Funder pages on the public site — one curated list, one reader.

A funder wants "the genes OGA characterised with our money", and the pipeline
can nearly answer it: every target and every nomination carries a granting
agency and a project, and the names are real in the live data (19 Sep 2026:
the ``MJFF`` label reaches 30 targets, 16 of them with a public page). What the
pipeline does **not** hold is which labels mean one funder — ``MJFF/GBA1
Canada`` is one Access value naming two funders, and the project names beside
the agency ("PD Proteins", "Dark PD targets") are Carl's, with no column saying
who paid. So a funder page is a curated entry in ``PAGES``: which labels mean
the funder. Adding one is adding an entry once its labels have been confirmed
with the person whose workbook the names came from. MJFF was the only page
from 19 Sep 2026, by the owner's decision (an NC3Rs page over Leicester's work
was built and withdrawn the same day); ALS-RAP joined it on 10 Oct 2026 for the
eLife paper.

**A programme whose gene set is published is a gene list, not a label.**
ALS-RAP is the 33 genes Ayoubi et al. name in eLife, funded jointly by three
charities. The ``ALS-RAP`` agency label is on those 33 in the Access export
*and* on 13 more (GRN and SPAST among them, both public), so reading the label
would put 35 genes under a paper that characterised 33. ``genes`` holds the
paper's list, spelled as the targets are stored (``C9orf72``).

Two rules, both inherited.

**Only public genes in the main list.** It is drawn from
``pipeline.public.public_targets``, so a gene listed there is one with a page to
go to.

**In progress is a second list, and it is names only** (29 Sep 2026, asked for
by MJFF — "so folks know what is coming down the pipeline"). A funded gene
whose work has started and that has no public page yet is listed by symbol and
protein name, with no link, no antibody count, no supplier and no result: the
supplier-scoped rule in ``core/api_pipeline.py`` still holds for everything
*about* the unpublished work. A gene is listed once at least one antibody is
logged against it (``in_progress``). The two lists cannot share a gene: one is
``public_targets``, the other excludes it.

**Both columns, like ``targets.sites_of``.** The funder and project live on the
``Target`` row (written once by the 2019 import and dead for writes since) *and*
on ``TargetNomination`` (the live record). Reading one gave 16 of the 25 genes
the first draft of this page listed. The membership test asks both.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Exists, OuterRef, Q

from pipeline.public import control_kinds, public_targets

from core import recommendations as R

# Drawn in this order on the page; the stored codes are the public figures'.
APPLICATION_ORDER = R.APPLICATIONS
APPLICATION_LABEL = {"WB": "Western blot", "IP": "Immunoprecipitation",
                     "ICC-IF": "Immunofluorescence", "FC": "Flow cytometry",
                     "IHC": "Immunohistochemistry"}


@dataclass(frozen=True)
class Funder:
    name: str                       # as the page names it; also the logo's alt text
    url: str
    logo: str = ""                  # a `core/static/...` path; blank draws the name
    # A tile behind a logo drawn in white, which is how ALS Canada shows its
    # own mark (white on its purple). Blank for an ordinary logo.
    background: str = ""


@dataclass(frozen=True)
class Paper:
    """The paper a programme's results were published in. The title is quoted as
    published, and ``reported`` is the paper's own count — a different question
    from the antibodies with figures on OGA, so the page says whose it is."""
    authors: str
    year: int
    title: str
    journal: str
    doi: str                        # the version the page cites, as the owner gave it
    reported: str
    journal_logo: str = ""          # a `core/static/...` path

    @property
    def url(self) -> str:
        return f"https://doi.org/{self.doi}"


@dataclass(frozen=True)
class FunderPage:
    slug: str
    name: str                       # the page's heading
    short: str                      # "MJFF", for a link or a title
    intro: str
    funders: tuple[Funder, ...]
    label: str = "Funder"           # the pill above the heading
    title: str = ""                 # <title>; blank is "<short>-funded genes"
    agencies: tuple[str, ...] = ()  # `GrantingAgency.name` values that mean this funder
    projects: tuple[str, ...] = ()  # `Project.name` values that mean this funder
    genes: tuple[str, ...] = ()     # a published gene set, matched by gene name
    paper: Paper | None = None

    @property
    def page_title(self) -> str:
        return self.title or f"{self.short}-funded genes"

    @property
    def funded_by(self) -> str:
        """The funders' names as a phrase: "A", "A and B", "A, B and C"."""
        names = [f.name for f in self.funders]
        return names[0] if len(names) == 1 else (
            f"{', '.join(names[:-1])} and {names[-1]}")


MJFF = FunderPage(
    slug="mjff",
    name="The Michael J. Fox Foundation for Parkinson's Research",
    short="MJFF",
    intro=("Antibodies against proteins of interest to Parkinson's research, "
           "characterised by YCharOS to community consensus protocols under "
           "programmes funded by The Michael J. Fox Foundation."),
    funders=(Funder(
        name="The Michael J. Fox Foundation for Parkinson's Research",
        url="https://www.michaeljfox.org",
        logo="core/supporters/mjff.png"),),
    agencies=("MJFF", "MJFF/GBA1 Canada"),
)

ALS_RAP = FunderPage(
    slug="als-rap",
    # "ALS-RAP" stays out of the heading and the intro: a heading breaks at the
    # hyphen ("ALS-" / "RAP)"), so the short name rides in the label above it.
    name="ALS Reproducible Antibody Platform",
    short="ALS-RAP",
    title="ALS-RAP genes",
    label="ALS-RAP · Jointly funded",
    intro=("Antibodies against proteins encoded by ALS risk genes, characterised "
           "by YCharOS to community consensus protocols in a programme funded "
           "jointly by the MND Association, the ALS Association and ALS Canada."),
    funders=(
        Funder(name="MND Association", url="https://www.mndassociation.org",
               logo="core/supporters/mnd-association.png"),
        # The wordmark the owner supplied (10 Oct 2026), with a viewBox added
        # so it scales; als.org refuses a script and the copies online are
        # third-party, so a replacement comes from the owner too.
        Funder(name="ALS Association", url="https://www.als.org",
               logo="core/supporters/als-association.svg"),
        Funder(name="ALS Canada", url="https://als.ca",
               logo="core/supporters/als-canada.png", background="#682076"),
    ),
    # The paper's Results, "Genetic prioritization", in its order.
    genes=("ACSL5", "ALS2", "ANG", "ANXA11", "ATXN2", "C9orf72", "CAV1", "CCNF",
           "CHCHD10", "CHMP2B", "FIG4", "FUS", "HNRNPA1", "HNRNPA2B1", "KIF5A",
           "LGALS1", "MATR3", "NEK1", "OPTN", "PFN1", "SETX", "SIGMAR1", "SOD1",
           "SPG11", "SQSTM1", "TAF15", "TARDBP", "TBK1", "TIA1", "TUBA4A",
           "UBQLN2", "VAPB", "VCP"),
    paper=Paper(
        authors="Ayoubi R, MacDougall EJ, McDowell I, et al.",
        year=2026,
        title="A validated antibody toolbox for ALS research",
        journal="eLife",
        # Version 2, the revised Reviewed Preprint (9 Oct 2026), by the owner's
        # choice. The all-versions DOI is 10.7554/eLife.111349; change this
        # one when the version of record is out.
        doi="10.7554/eLife.111349.2",
        journal_logo="core/supporters/elife.png",
        reported=("303 antibodies against 33 ALS-associated proteins, tested in "
                  "western blot, immunoprecipitation and immunofluorescence"),
    ),
)

PAGES: dict[str, FunderPage] = {p.slug: p for p in (MJFF, ALS_RAP)}


def _funded_q(page: FunderPage) -> Q:
    """A matching label on the ``Target`` row or on any of its nominations, or
    a gene on the page's published list. ``Exists`` rather than a join keeps it
    one row per target."""
    from pipeline.models import TargetNomination

    labelled = (Q(project__name__in=page.projects)
                | Q(granting_agency__name__in=page.agencies))
    nominated = TargetNomination.objects.filter(
        target_id=OuterRef("pk")).filter(
        Q(project__name__in=page.projects)
        | Q(granting_agency__name__in=page.agencies))
    return labelled | Q(Exists(nominated)) | Q(gene_name__in=page.genes)


def funder_targets(page: FunderPage):
    """The public genes on a funder's page — one row per target, by symbol.

    Membership is a matching label on the ``Target`` row or on any of its
    nominations, or a gene on the page's list (``_funded_q``). ``Exists``
    rather than a join on nominations keeps it one row per target, so
    ``.count()`` and the drawn list cannot disagree.
    """
    return (public_targets()
            .filter(_funded_q(page))
            .annotate(published_antibody_count=Count(
                "antibodies",
                filter=Q(antibodies__publication_images__isnull=False),
                distinct=True))
            .order_by("gene_name"))


def in_progress(page: FunderPage) -> list[tuple[str, str]]:
    """``(gene, protein)`` for the funder's genes under way and not yet public.

    Under way means **at least one antibody logged** (owner, 29 Sep 2026) —
    so a gene with only a preliminary Zenodo report (ATP13A2) is here,
    and a nomination nothing has arrived for is not. Cancelled and on-hold are
    left out: those are decisions somebody took, and this list is a promise.
    Names only, by design — see the module docstring. The count on the page is
    ``len()`` of this list.
    """
    from pipeline.models import Antibody
    from pipeline.public import unpublished_targets

    logged = Antibody.objects.filter(target_id=OuterRef("pk"))
    return list(unpublished_targets()
                .filter(_funded_q(page))
                .filter(Exists(logged))
                .exclude(status__in=("cancelled", "on_hold"))
                .order_by("gene_name")
                .values_list("gene_name", "protein_name"))


@dataclass
class Row:
    gene: str
    protein: str
    antibodies: int
    applications: list[tuple[str, str]] = field(default_factory=list)  # (code, label)


def rows_for(page: FunderPage) -> list[Row]:
    """What the page draws: every public gene on it with its published antibody
    count and the applications with a figure. The count on the page is
    ``len()`` of this list."""
    from pipeline.models import PublicationImage

    targets = list(funder_targets(page))
    if not targets:
        return []

    apps_by_target: dict[int, set[str]] = {}
    for target_id, app in (PublicationImage.objects
                           .filter(antibody__target_id__in=[t.pk for t in targets])
                           .values_list("antibody__target_id", "application_type")
                           .distinct()):
        apps_by_target.setdefault(target_id, set()).add(app)

    rows = []
    for t in targets:
        apps = apps_by_target.get(t.pk, set())
        rows.append(Row(
            gene=t.gene_name,
            protein=t.protein_name or "",
            antibodies=t.published_antibody_count,
            applications=[(a, APPLICATION_LABEL[a])
                          for a in APPLICATION_ORDER if a in apps],
        ))
    return rows


def application_key(rows: list[Row]) -> list[tuple[str, str]]:
    """``(code, label)`` for each application a row on the page draws, in the
    page's order — the key under the table names only codes it uses."""
    drawn = {code for r in rows for code, _ in r.applications}
    return [(a, APPLICATION_LABEL[a]) for a in APPLICATION_ORDER if a in drawn]


def control_kinds_for(page: FunderPage) -> set:
    """KO / KD over the page's published figures, for `control_noun`."""
    from pipeline.public import published_antibodies

    target_ids = list(funder_targets(page).values_list("pk", flat=True))
    ids = published_antibodies().filter(
        target_id__in=target_ids).values_list("id", flat=True)
    return control_kinds(antibody_ids=list(ids))
