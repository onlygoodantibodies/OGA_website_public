"""
The target board — Carl's master spreadsheet, but cross-site and self-updating.

His sheet is one site's view (column F says McGill on the rows where it says
anything at all), so work at Leicester is invisible in it: MMP7 and SERPINA1 are
published and PKN2, GNAQ and NR3C1 submitted, and none of them appear in the
file. That blind spot is the thing worth fixing — his own stated wish was "a
spreadsheet that indicates the targets being pursued elsewhere… one obvious
advantage is to quickly catch any duplicated targets".

Three jobs, matching what he actually does with the file:

  * ``board_rows``       — the list itself, all sites, filterable (weekly triage).
  * ``nomination_check`` — before adding a gene: is anyone already doing it, has
    anyone done its family, is a KO line obtainable (grant writing / new targets).
  * ``portfolio``        — counts by protein class, agency, site, completion
    ("we've done N GPCRs"), which the spreadsheet cannot answer at all.

Nothing here writes. ``services/target_list_io.py`` owns the write path.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from django.db.models import (BooleanField, Count, Exists, OuterRef, Prefetch, Q,
                              Value)

from pipeline.models import (Antibody, ExperimentSession, GrantingAgency,
                             HorizonKoLine, Project, Report, Site, Target,
                             TargetAssignment, TargetClassification,
                             TargetNomination)
from pipeline.services import board_page as board_page_svc
from pipeline.services import doi as doi_svc
from pipeline.services import find
from pipeline.services import sites as site_svc
from pipeline.services import targets as target_svc
from pipeline.services.protein_class import gene_family

DB = "pipeline_db"

APPLICATIONS = ["WB", "IP", "IF", "FC"]


# ---------------------------------------------------------------------------
# "Completed" — Carl's definition: a Zenodo report exists, or F1000 is published
# ---------------------------------------------------------------------------

def completed_report_q() -> Q:
    return Q(zenodo_doi__gt="") | Q(f1000_date__isnull=False)


def is_completed_report(report) -> bool:
    """The same sentence as ``completed_report_q``, asked of a row in memory.

    Two callers need it without a second query — ``row_for`` splits a target's
    prefetched reports into the published record and the rest, and the gene page
    draws that split — and a hand-written ``if r.zenodo_doi or r.f1000_date`` in
    either of them is a fourth answer to "how many have we finished" waiting to
    happen. ``tests_board_patch.py`` pins that the two agree.

    Note which half is which: an F1000 **DOI** does not make a gene completed
    (a preprint has one before it is accepted); the F1000 **date** does.
    """
    return bool(report.zenodo_doi) or report.f1000_date is not None


def completed_subquery():
    """``Exists`` of a qualifying report — the one definition of "done".

    Public because Overview asked the question three other ways and got three
    other answers: its headline tile counted ``Target.status == 'published'``
    (134), its stage chart counted that *and* "any Report row" as two separate
    bars (34 + 134), and this board and the Portfolio counted a Zenodo DOI or a
    published F1000 date (164). All three were internally consistent, none of
    them agreed, and "how many have we finished" depended on which page the
    reader was standing on.

    This is the owner's definition and the only derived one; ``Target.status`` is
    the Access-era column CLAUDE.md already records as dead. Checked against the
    import before switching Overview over: of the 508 rows in
    ``access_csvs/Proteins.csv``, all 134 with a "Report complete" status also
    carry a DOI or an F1000 date, so ``status='published'`` is a strict **subset**
    of this and nothing is orphaned by preferring it — which is the check
    ``views/dashboard.py::_active`` had to *fail* in the other direction, where
    the typed statuses are the only record for 152 of McGill's targets.
    """
    return Exists(
        Report.objects.using(DB).filter(completed_report_q(), target_id=OuterRef("pk"))
    )


def report_showing(reports, field):
    """Which of a target's ``Report`` rows the board is *drawing* in a column.

    A target may carry more than one. ``row_for`` drew the first row that had a
    Zenodo DOI, while the patch endpoint edited the first row by ``pk`` — so on a
    gene with two reports the board showed one row's DOI and saved over another's,
    and clearing the cell wrote a blank to a field that was already blank while
    the value stayed on screen. "I deleted it and it came back" is the worst
    possible answer on the one field a person is trying to correct.

    So the reader and the writer ask this. ``None`` means no row is showing that
    column yet, and the caller decides whether to reach for the first row on file
    or create one.
    """
    if field.startswith("zenodo"):
        return next((r for r in reports if r.zenodo_doi), None)
    if field.startswith("f1000"):
        return next((r for r in reports if r.f1000_doi or r.f1000_date), None)
    return None


# Everything a ``Report`` row can actually record. Named rather than derived
# from ``_meta`` so that adding a field is a decision about this rule too: a new
# column nobody lists here would make a row carrying it look empty, and this is
# the test a delete is made on.
_REPORT_CONTENT = ("zenodo_doi", "f1000_doi", "zenodo_date", "f1000_date",
                   "f1000_priority", "f1000_note", "introduction_text",
                   "generated_at", "generated_by_id")


def records_nothing(report) -> bool:
    """Whether this ``Report`` row states no fact at all.

    A row like this is not a record of anything — it is the residue of a cell
    edit. Typing a Zenodo DOI onto a gene with no report creates the row, and
    *clearing* that DOI again used to leave it behind: the gene page then showed
    **REPORTS (1) · Draft — No DOI linked yet** for a report nobody generated,
    directly under the panel's own promise that "a draft never appears here".
    Overview's *Report drafted* bar counted it too, so a corrected typo left a
    permanent phantom deposit in the consortium's figures.

    ``status`` is deliberately not content: it is derived from the fields above
    (``views/target_board.py::_save_report_field``), so a row whose every field
    is empty and whose status reads ``draft`` is stating a default, not a fact.
    """
    return not any(getattr(report, f, None) for f in _REPORT_CONTENT)


def _funded_subquery():
    return Exists(
        TargetNomination.objects.using(DB).filter(target_id=OuterRef("pk"), funded=True)
    )


def _has_nominated_site():
    return Exists(
        TargetNomination.objects.using(DB)
        .filter(target_id=OuterRef("pk"), site__isnull=False)
    )


def _filter_site(qs, value):
    """`?site=` on the targets board, where the answer lives in two columns.

    ``services/targets.py::sites_of`` is the reader; this is the same rule as a
    queryset. A target belongs to the sites that nominated it, or — when none did
    — to the site the Access import stamped on it. Filtering on the nominations
    alone hid 192 imported targets from every site filter on this board while
    Overview listed them under that very site, and sent ``?site=none`` 210 rows
    whose site is recorded, just not where the filter was looking.
    """
    kind, pk = site_svc.filter_kind(value)
    if kind == site_svc.ALL:
        return qs
    if kind == site_svc.NOTHING:
        return qs.none()
    if kind == site_svc.NO_SITE:
        # No nomination naming a site *and* nothing from the import either —
        # genuinely nobody's, which is what the Portfolio's caveat now links to.
        return qs.filter(~_has_nominated_site(), site__isnull=True)
    return qs.filter(Q(nominations__site_id=pk)
                     | (~_has_nominated_site() & Q(site_id=pk)))


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

def board_queryset():
    """Targets annotated with everything the board renders."""
    return (
        Target.objects.using(DB)
        # `Target.site` is the Access import's record of whose a target is, and
        # `targets.sites_of` reads it whenever no nomination names one. Joined
        # here rather than fetched per row: `portfolio()` walks all 585 and the
        # board draws 50, so without this it is an N+1 on both — the cost pattern
        # `tests_timeouts.py` pins as "query count does not grow with row count".
        .select_related("site")
        .annotate(
            is_completed=completed_subquery(),
            is_funded=_funded_subquery(),
            antibody_count=Count("antibodies", distinct=True),
        )
        .prefetch_related(
            Prefetch("nominations", queryset=TargetNomination.objects.using(DB)
                     .select_related("site", "project", "granting_agency")),
            Prefetch("reports", queryset=Report.objects.using(DB)),
            Prefetch("classifications",
                     queryset=TargetClassification.objects.using(DB)),
        )
    )


def apply_filters(qs, *, q="", gene="", site="", project="", agency="", funded="",
                  completed="", protein_class="", status=""):
    """Filters mirror the columns Carl sorts and filters on in Excel.

    ``status`` is the legacy ``Target.status`` field. The board doesn't show it —
    Carl's answer was that funded + completed is all the workflow state needed —
    but Overview still drills down by it (``?status=in_progress``), so it is
    honoured here rather than silently ignored, which would turn "45 in progress"
    into a link showing everything.
    """
    if q:
        # One list, in `services/find.py`, shared with the nav search box.
        qs = qs.filter(find.target_q(q))
    # Exact genes, so ?gene=STMN2 means the same thing on all four boards and a
    # gene can be carried from one to the next. `q` stays the fuzzy box people
    # type into; this is the one a link sets. A comma-separated list narrows to
    # exactly those genes — `services/targets.py::gene_q`, and the reason it
    # exists is that adding six genes at once now lands here, on the six.
    if gene:
        gene_filter = target_svc.gene_q("gene_name", gene)
        if gene_filter is not None:
            qs = qs.filter(gene_filter)
    if site:
        qs = _filter_site(qs, site)
    if project:
        qs = qs.filter(nominations__project_id=project)
    if agency:
        qs = qs.filter(nominations__granting_agency_id=agency)
    if protein_class:
        qs = qs.filter(classifications__label=protein_class)
    if status:
        qs = qs.filter(status=status)
    if funded in ("yes", "no"):
        qs = qs.filter(is_funded=(funded == "yes"))
    if completed in ("yes", "no"):
        qs = qs.filter(is_completed=(completed == "yes"))
    return qs.distinct()


def application_coverage(target_ids) -> dict:
    """{target_id: {"WB": ["Leicester"], ...}} — who has actually run what.

    Harvinder's point that "many don't have all 4 applications, and may never
    have them all" is why completion is reported per application per site rather
    than as one done/not-done flag. Evidence is a real session or an assignment
    marked complete — not somebody's typed status.
    """
    cover = defaultdict(lambda: defaultdict(set))
    sessions = (ExperimentSession.objects.using(DB)
                .filter(target_id__in=target_ids)
                .select_related("site")
                .values_list("target_id", "procedure_type", "site__name"))
    for tid, proc, site_name in sessions:
        if proc in APPLICATIONS:
            cover[tid][proc].add(site_name or "—")
    assignments = (TargetAssignment.objects.using(DB)
                   .filter(target_id__in=target_ids,
                           status=TargetAssignment.AssignmentStatus.COMPLETE)
                   .values_list("target_id", "task_type", "site__name"))
    for tid, task, site_name in assignments:
        if task in APPLICATIONS:
            cover[tid][task].add(site_name or "—")
    return {tid: {k: sorted(v) for k, v in procs.items()}
            for tid, procs in cover.items()}


def _report_row(report) -> dict:
    """One published report, as JSON — every value a string, per the board rule.

    Dates go out as ISO and are what the gene page's date cells edit, because
    ``target_list_io.parse_date`` reads that spelling back: the same rule
    ``doi.display`` holds one column over — what a surface prints must be a value
    its own parser accepts.
    """
    return {
        "status": report.status,
        "status_label": report.get_status_display(),
        "zenodo_text": doi_svc.display(report.zenodo_doi),
        "zenodo_link": doi_svc.link(report.zenodo_doi),
        "zenodo_date": report.zenodo_date.isoformat() if report.zenodo_date else "",
        "f1000_text": doi_svc.display(report.f1000_doi),
        "f1000_link": doi_svc.link(report.f1000_doi),
        "f1000_date": report.f1000_date.isoformat() if report.f1000_date else "",
    }


def _class_rows(target) -> list[dict]:
    """Each protein-class label once, with whether it may be taken off by hand.

    A label can legitimately sit under two sources — the unique constraint is
    ``(target, label, source)``, so UniProt and a curator can both say ``GPCR`` —
    and that is one finding to a reader, so they are folded into one entry.
    ``removable`` is whether any of them is the hand-added row, which is the only
    one ``target_board_patch``'s ``remove_class`` will delete.
    """
    by_label: dict[str, set] = {}
    for c in target.classifications.all():
        by_label.setdefault(c.label, set()).add(c.source)
    Source = TargetClassification.Source
    return [{
        "label": label,
        "sources": sorted(sources),
        "removable": Source.MANUAL in sources,
        "from": "added by hand" if sources == {Source.MANUAL}
                else "derived from " + ", ".join(sorted(
                    s for s in sources if s != Source.MANUAL)),
    } for label, sources in sorted(by_label.items())]


def row_for(target, coverage=None) -> dict:
    """One board row — the A–T columns, plus what the sheet could never hold."""
    noms = list(target.nominations.all())
    reports = list(target.reports.all())
    zen = report_showing(reports, "zenodo_doi")
    f10 = report_showing(reports, "f1000_doi")
    # Whose target this is, from both places it can be recorded — see
    # `services/targets.py::sites_of`. The board read the nominations alone and
    # printed an empty cell offering "set site" for every one of the 508 targets
    # the Access import created, while Overview showed the site it stamped on
    # them. Anybody tidying up would have started filling in sites that are
    # already on file.
    whose = target_svc.sites_of(target, noms)
    sites = [s.name for s in whose["sites"]]
    return {
        "id": target.pk,
        "gene": target.gene_name or "",
        "protein": target.protein_name or "",
        "alternative": target.alternative_name or "",
        # Both alias columns as one list (services/targets.py::other_names).
        # `alternative` above has been in this payload since the board was
        # written and is drawn nowhere, which is how the second alias column —
        # the one the Downloads & uploads round trip writes — reached the
        # database with no screen in the app showing it.
        "other_names": target_svc.other_names(target),
        "uniprot": target.uniprot_id or "",
        "mass_kda": (float(target.theoretical_mass_kda)
                     if target.theoretical_mass_kda is not None else None),
        "sites": sites,
        # …and where that came from. A nomination is a record with a funder, a
        # project and a date behind it; the import's site is a column on a 2019
        # spreadsheet, written identically on every row it created. Drawn apart,
        # so a reader does not take the second for the first — and so the cell
        # can say that typing here *records* the nomination rather than moving
        # one that does not exist.
        "sites_from_import": whose["from_import"],
        "agencies": sorted({n.granting_agency.name for n in noms if n.granting_agency}),
        "projects": sorted({n.project.name for n in noms if n.project}),
        "funded": bool(getattr(target, "is_funded", False)),
        # "No nomination on file" is not the same claim as "we looked and it
        # isn't funded". Before a target list is imported every target is the
        # former, and rendering all of them as "not funded" asserts something we
        # do not know.
        "has_nomination": bool(noms),
        "completed": bool(getattr(target, "is_completed", False)),
        "nominations": [{
            "id": n.pk,
            "site": n.site.name if n.site else "",
            "agency": n.granting_agency.name if n.granting_agency else "",
            "project": n.project.name if n.project else "",
            "funded": n.funded,
            "date": n.date_of_nomination.isoformat() if n.date_of_nomination else "",
            "date_text": n.date_text,
            "comments": n.comments,
            "status_note": n.status_note,
            "conclusion_note": n.conclusion_note,
            "antibodies_requested": n.antibodies_requested,
        } for n in noms],
        "zenodo_doi": zen.zenodo_doi if zen else "",
        "zenodo_date": (zen.zenodo_date.isoformat() if zen and zen.zenodo_date else ""),
        "f1000_doi": f10.f1000_doi if f10 else "",
        "f1000_date": (f10.f1000_date.isoformat() if f10 and f10.f1000_date else ""),
        # What the two DOI cells print, and where each one may point. Decided
        # here rather than in the template because ``services/doi.py`` is the one
        # reader for all three questions — and because a value on file may be
        # something no browser can follow, in which case the link is empty and
        # the cell draws text. Both are plain strings: every value in a board row
        # must be JSON, or one bad row 500s the whole response.
        "zenodo_text": doi_svc.display(zen.zenodo_doi) if zen else "",
        "zenodo_link": doi_svc.link(zen.zenodo_doi) if zen else "",
        "f1000_text": doi_svc.display(f10.f1000_doi) if f10 else "",
        "f1000_link": doi_svc.link(f10.f1000_doi) if f10 else "",
        "f1000_priority": next((r.f1000_priority for r in reports if r.f1000_priority), ""),
        "f1000_note": next((r.f1000_note for r in reports if r.f1000_note), ""),
        # Carl's column L. `_TARGET_FIELDS` has accepted a write to it since the
        # board was built and no screen drew it, so the one way to fill it in was
        # a spreadsheet — the mirror of the rule that a field a write path fills
        # must be drawn somewhere.
        "essential_gene": target.essential_gene or "",
        # The published record and what it left out, split here rather than by a
        # second query on the gene page — and split by `is_completed_report`, so
        # this list and the `completed` pill above it cannot come to disagree.
        # The board ignores both keys; they are what the gene page's Reports
        # panel is drawn from, and drawing that panel from the same payload the
        # cells are drawn from is what stops a save leaving the list beneath it
        # saying "nothing published for this target yet".
        "reports": {
            "published": [_report_row(r) for r in reports if is_completed_report(r)],
            "unpublished": sum(1 for r in reports if not is_completed_report(r)),
        },
        "classes": sorted({c.label for c in target.classifications.all()}),
        # The same labels with the evidence behind them. `classes` is a bare list
        # of strings and the board draws it as pills; the gene page offers to
        # *remove* one, and the patch endpoint only ever deletes a hand-added row
        # — so a × on a UniProt-derived label would be a control that does
        # nothing, silently, which is the shape of half the defects in this repo.
        "class_rows": _class_rows(target),
        "family": gene_family(target.gene_name or ""),
        "antibody_count": getattr(target, "antibody_count", 0),
        "coverage": (coverage or {}).get(target.pk, {}),
    }


def board_rows(**filters) -> list[dict]:
    """Every matching row. Kept for callers that genuinely want all of them —
    exports, and the tests that assert on the whole set."""
    qs = apply_filters(board_queryset(), **filters).order_by("gene_name")
    targets = list(qs)
    coverage = application_coverage([t.pk for t in targets])
    return [row_for(t, coverage) for t in targets]


def board_page(*, page=1, per_page=board_page_svc.DEFAULT_PER_PAGE, locate=None,
               **filters) -> dict:
    """One page of rows plus the whole filtered count — see
    ``services/board_page.py``.

    ``application_coverage`` is asked for the page's targets, not the whole
    queryset: it is the batch query that keeps this page off an N+1, and running
    it over 585 targets to draw 50 of them is the same waste one layer down.
    """
    qs = apply_filters(board_queryset(), **filters).order_by("gene_name")
    count = qs.count()
    pages = max(1, -(-count // per_page))
    located = board_page_svc.page_of(qs, locate, per_page) if locate else None
    if located:
        page = located
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    targets = list(qs[start:start + per_page])
    coverage = application_coverage([t.pk for t in targets])
    return {"rows": [row_for(t, coverage) for t in targets], "count": count,
            "page": page, "pages": pages, "per_page": per_page,
            "located": (bool(located) if locate else None)}


def filter_options() -> dict:
    labels = (TargetClassification.objects.using(DB)
              .values_list("label", flat=True).distinct().order_by("label"))
    return {
        "sites": list(Site.objects.using(DB).filter(is_active=True).order_by("name")),
        "projects": list(Project.objects.using(DB).order_by("name")),
        "agencies": list(GrantingAgency.objects.using(DB).order_by("name")),
        "classes": sorted(set(labels)),
    }


# ---------------------------------------------------------------------------
# Cross-site overlap — the thing the spreadsheet structurally cannot see
# ---------------------------------------------------------------------------

def duplicate_targets() -> list[dict]:
    """Genes nominated at more than one site — the same target funded twice."""
    rows = (TargetNomination.objects.using(DB)
            .exclude(site__isnull=True)
            .select_related("target", "site", "project", "granting_agency"))
    by_gene = defaultdict(list)
    for n in rows:
        by_gene[n.target.gene_name or f"#{n.target_id}"].append(n)
    out = []
    for gene, noms in sorted(by_gene.items()):
        sites = {n.site.name for n in noms}
        if len(sites) > 1:
            out.append({
                "gene": gene,
                "target_id": noms[0].target_id,
                "sites": sorted(sites),
                "detail": [{"site": n.site.name,
                            "project": n.project.name if n.project else "",
                            "agency": (n.granting_agency.name
                                       if n.granting_agency else ""),
                            "funded": n.funded} for n in noms],
            })
    return out


def duplicate_sites_for(gene: str) -> list[str]:
    """The sites one gene is nominated at, when there is more than one.

    Same rule as ``duplicate_targets``, scoped to a single gene so an inline edit
    can refresh one row's badge without rescanning every nomination in the
    consortium. Editing a nomination's site can only change that gene's own
    badge — the board is one row per gene — so this is the whole blast radius.
    """
    if not gene:
        return []
    sites = {n.site.name for n in
             TargetNomination.objects.using(DB)
             .filter(target__gene_name__iexact=gene)
             .exclude(site__isnull=True)
             .select_related("site")}
    return sorted(sites) if len(sites) > 1 else []


def family_expertise() -> dict:
    """{family: {site: {"total": n, "completed": n}}} — who knows this family.

    This is the Rab case: McGill has run most Rabs, so a Rab nominated elsewhere
    should probably be routed to McGill rather than started from scratch.
    """
    noms = (TargetNomination.objects.using(DB)
            .exclude(site__isnull=True)
            .select_related("target", "site"))
    completed_ids = set(
        Report.objects.using(DB).filter(completed_report_q())
        .values_list("target_id", flat=True))
    table = defaultdict(lambda: defaultdict(lambda: {"total": 0, "completed": 0}))
    seen = set()
    for n in noms:
        fam = gene_family(n.target.gene_name or "")
        if not fam:
            continue
        key = (fam, n.site.name, n.target_id)
        if key in seen:
            continue
        seen.add(key)
        cell = table[fam][n.site.name]
        cell["total"] += 1
        if n.target_id in completed_ids:
            cell["completed"] += 1
    return {fam: dict(sites) for fam, sites in table.items()}


def nomination_check(gene: str) -> dict:
    """Everything worth knowing before adding a gene to the list.

    Answers, in one call: are we already doing it (and where), has another site
    done its family, is there an off-the-shelf KO line, and is it published
    already. Runs offline-safe — the KO lookup is a local catalogue table, the
    family heuristic is pure string work.
    """
    gene = (gene or "").strip()
    out = {"gene": gene, "warnings": [], "notes": []}
    if not gene:
        return {**out, "ok": False, "error": "gene is required"}

    target = Target.objects.using(DB).filter(gene_name__iexact=gene).first()
    out["exists"] = target is not None
    out["target_id"] = target.pk if target else None

    if target:
        noms = list(TargetNomination.objects.using(DB)
                    .filter(target_id=target.pk)
                    .select_related("site", "project", "granting_agency"))
        sites = sorted({n.site.name for n in noms if n.site})
        out["pursued_at"] = sites
        out["completed"] = Report.objects.using(DB).filter(
            completed_report_q(), target_id=target.pk).exists()
        if sites:
            out["warnings"].append(
                f"{gene} is already on the list at {', '.join(sites)} — "
                "adding it again duplicates funded work.")
        elif noms:
            out["warnings"].append(
                f"{gene} is already on the list but no site is assigned.")
        else:
            out["notes"].append(f"{gene} is already a target, with no nomination yet.")
        if out["completed"]:
            out["warnings"].append(f"{gene} already has a published report.")
    else:
        out["pursued_at"] = []
        out["completed"] = False

    fam = gene_family(gene)
    out["family"] = fam
    out["family_expertise"] = {}
    if fam:
        expertise = family_expertise().get(fam, {})
        out["family_expertise"] = expertise
        best = sorted(expertise.items(),
                      key=lambda kv: (-kv[1]["completed"], -kv[1]["total"]))
        if best and best[0][1]["total"] > 1:
            site_name, stats = best[0]
            out["notes"].append(
                f"{site_name} has {stats['total']} {fam} targets "
                f"({stats['completed']} published) — they may be the right site "
                f"to run this one.")

    ko = list(HorizonKoLine.objects.using(DB).filter(gene_name__iexact=gene)
              .values("item_number", "product_name", "background")[:5])
    out["horizon_ko"] = ko
    if ko:
        out["notes"].append(
            f"An off-the-shelf {ko[0]['background']} KO line is catalogued "
            f"({ko[0]['item_number']}).")

    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# Portfolio — the grant-writing view
# ---------------------------------------------------------------------------

def portfolio() -> dict:
    """Counts Carl currently reconstructs from memory while writing grants."""
    targets = list(board_queryset())
    completed_ids = {t.pk for t in targets if t.is_completed}

    # Two lists, because they answer two questions and one of them buried the
    # other. A **class** is the curated rubric in `services/protein_class.py`
    # (GPCR, Kinase, Nuclear, Synaptic — 22 of them, from UniProt keywords and GO
    # terms) and is what a grant quotes. A **family** is the letters at the front
    # of the gene symbol: RAB11A, RAB5C and RAB7A all become "RAB family". That is
    # a text prefix rather than biology, `classify` emits one for every symbol
    # that has one, and there are 296 of them — 241 containing a single gene. So
    # the 22 rows worth reading sat inside 318, sorted by size, on the page
    # somebody opens to write a grant.
    #
    # The families are kept rather than dropped: they are what powers "McGill has
    # 60 RAB targets (14 published)" on Add Targets (`family_expertise`). They are
    # simply not the same question, so the page draws them apart.
    #
    # Counted as distinct *targets*, not rows. A hand-added label duplicates a
    # derived one — the unique constraint is per (target, label, source), so both
    # rows are legitimate — and counting rows inflated exactly the figure that
    # gets typed into a grant.
    class_targets = defaultdict(set)
    family_targets = defaultdict(set)
    for c in (TargetClassification.objects.using(DB)
              .values("target_id", "label", "source")):
        bucket = (family_targets
                  if c["source"] == TargetClassification.Source.FAMILY
                  else class_targets)
        bucket[c["label"]].add(c["target_id"])

    def _tally(buckets):
        return {label: {"total": len(ids),
                        "completed": len(ids & completed_ids)}
                for label, ids in buckets.items()}

    by_class = _tally(class_targets)
    by_family = _tally(family_targets)

    # Count targets, not nomination rows: a gene nominated twice at one site is
    # one target there, and it counts as funded if *any* of its nominations is.
    funded_at = defaultdict(bool)
    for n in (TargetNomination.objects.using(DB).exclude(site__isnull=True)
              .select_related("site")):
        key = (n.site.name, n.target_id)
        funded_at[key] = funded_at[key] or n.funded
    # …plus the targets whose site is recorded where nominations are not.
    #
    # This table said 373 under a header reading 583 and called the gap "210 with
    # no nomination naming a site", which was true and read as *210 genes nobody
    # has assigned*. They are not: 192 of them carry a site from the Access
    # import, shown on Overview all along. So these totals were **wrong rather
    # than incomplete** — the number a grant gets written from, short by the size
    # of the founding site's list. `services/targets.py::sites_of` is the reader;
    # `from_import` is counted separately so the page can say how much of a
    # column rests on the import rather than on a nomination somebody made.
    from_import_at = {}
    for t in targets:
        whose = target_svc.sites_of(t)
        if whose["from_import"]:
            for s in whose["sites"]:
                from_import_at[(s.name, t.pk)] = True
    by_site = defaultdict(lambda: {"total": 0, "completed": 0, "funded": 0,
                                   "from_import": 0})
    for (site_name, target_id), is_funded in funded_at.items():
        cell = by_site[site_name]
        cell["total"] += 1
        if target_id in completed_ids:
            cell["completed"] += 1
        if is_funded:
            cell["funded"] += 1
    for (site_name, target_id) in from_import_at:
        cell = by_site[site_name]
        cell["total"] += 1
        cell["from_import"] += 1
        if target_id in completed_ids:
            cell["completed"] += 1

    by_agency = defaultdict(lambda: {"total": 0, "completed": 0})
    seen_agency = set()
    for n in (TargetNomination.objects.using(DB).exclude(granting_agency__isnull=True)
              .select_related("granting_agency")):
        key = (n.granting_agency.name, n.target_id)
        if key in seen_agency:
            continue
        seen_agency.add(key)
        cell = by_agency[n.granting_agency.name]
        cell["total"] += 1
        if n.target_id in completed_ids:
            cell["completed"] += 1

    # Each table answers a different question and is right about its own: the
    # header counts *targets*, "By site" counts (site, target) pairs and "By
    # funder" counts (funder, target) pairs. Two of the three said so nowhere, so
    # a reader adding up the site column and getting 373 under a header reading
    # 583 had no way to tell a deliberate scope from records having gone missing
    # — on the page a grant gets written from, where that reading is expensive.
    # The class table already carried its caveat; this is the other two.
    #
    # Both directions matter and they pull opposite ways. A target nobody has
    # nominated at a site is *absent* from the site table, and a target two sites
    # are pursuing appears *twice*, so the column can be short of the header and
    # over-count at the same time. Naming one and not the other would make the
    # arithmetic look broken in a different way.
    target_ids = {t.pk for t in targets}
    funded_ids = {t.pk for t in targets if t.is_funded}
    site_target_ids = ({tid for (_s, tid) in funded_at}
                       | {tid for (_s, tid) in from_import_at}) & target_ids
    site_funded_ids = {tid for (_s, tid), f in funded_at.items() if f} & target_ids
    agency_target_ids = {tid for (_a, tid) in seen_agency} & target_ids
    site_rows = sum(c["total"] for c in by_site.values())
    agency_rows = sum(c["total"] for c in by_agency.values())

    return {
        "totals": {
            "targets": len(targets),
            "completed": len(completed_ids),
            "funded": sum(1 for t in targets if t.is_funded),
            # The watch list is genes somebody nominated and nobody is paying
            # for — not every target that happens to have no nomination record.
            "unfunded_watchlist": TargetNomination.objects.using(DB)
                .filter(funded=False)
                .exclude(target_id__in=completed_ids)
                .values("target_id").distinct().count(),
            "no_nomination": sum(1 for t in targets if not t.nominations.all()),
        },
        # What each of the two lower tables can and cannot see, so the page can
        # say it rather than leaving a reader to subtract.
        "coverage": {
            "site_rows": site_rows,
            "site_targets": len(site_target_ids),
            # How much of the column above rests on the import's site column
            # rather than on a nomination somebody made. Said out loud because
            # the two are not the same evidence, and because a reader who has
            # just been told the totals grew deserves to know what grew them.
            "site_from_import": len(from_import_at),
            "site_missing": len(target_ids) - len(site_target_ids),
            "site_shared": site_rows - len(site_target_ids),
            "site_funded_missing": len(funded_ids) - len(site_funded_ids),
            "site_completed_missing": len(completed_ids - site_target_ids),
            "agency_rows": agency_rows,
            "agency_targets": len(agency_target_ids),
            "agency_missing": len(target_ids) - len(agency_target_ids),
            "agency_shared": agency_rows - len(agency_target_ids),
            "agency_completed_missing": len(completed_ids - agency_target_ids),
        },
        "by_class": dict(sorted(by_class.items(),
                                key=lambda kv: -kv[1]["total"])),
        "by_family": dict(sorted(by_family.items(),
                                 key=lambda kv: (-kv[1]["total"], kv[0]))),
        "by_site": dict(sorted(by_site.items(), key=lambda kv: -kv[1]["total"])),
        "by_agency": dict(sorted(by_agency.items(), key=lambda kv: -kv[1]["total"])),
        "duplicates": duplicate_targets(),
    }
