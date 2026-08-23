"""Where a gene has got to, for somebody outside the lab.

``services/gene_progress.py`` answers the same question for a scientist
standing on the gene's own page, and answers it *one gene at a time* — six or
seven small queries per target, which is right for a page that renders one. A
supplier asking "where are the four genes I have reagents in?" needs the same
facts for a set, and looping ``steps_for`` over a set is how a page's cost comes
to grow with the number of rows on it — the failure ``tests_timeouts.py`` pins
everywhere else.

So this is the batched half, and it is deliberately **narrower**: it reports
what a reader outside the consortium can act on — has this gene got a public
page, which applications have published figures, which have figures waiting,
and has the report been published — and says nothing about knockout lines,
nominations or who is running what. Those are the lab's business and the strip
on the gene page is where they belong.

The report definition is not a second one: ``target_board.is_completed_report``
is the app's single reading of "completed" (a Zenodo DOI or a published F1000
date), and it is asked here rather than restated.
"""
from __future__ import annotations

from collections import defaultdict

from pipeline.models import Antibody, PendingPublicationImage, PublicationImage, Report
from pipeline.services import doi as doi_svc
from pipeline.services import target_board as board_svc
from pipeline.services.review import PENDING

DB = "pipeline_db"

APPLICATIONS = ["WB", "IP", "ICC-IF", "FC"]


def reporting_status(targets, *, pending_antibody_ids=None) -> dict:
    """``{target_id: {...}}`` for a set of targets, in a fixed number of queries.

    ``pending_antibody_ids`` narrows the "awaiting release" counts to a
    caller's own reagents — a supplier should be told about their own figures
    waiting, not about somebody else's, which is a fact about another company's
    product on an unpublished figure. Left out, it counts every waiting figure,
    which is what an internal screen wants.
    """
    targets = list(targets)
    ids = [t.pk for t in targets]
    if not ids:
        return {}

    from pipeline.public import public_targets
    public_ids = set(public_targets().using(DB)
                     .filter(pk__in=ids).values_list("pk", flat=True))

    published = defaultdict(set)
    for tid, app in (PublicationImage.objects.using(DB)
                     .filter(antibody__target_id__in=ids)
                     .values_list("antibody__target_id", "application_type")):
        published[tid].add(app)

    waiting_qs = (PendingPublicationImage.objects.using(DB)
                  .filter(status=PENDING, antibody__target_id__in=ids))
    if pending_antibody_ids is not None:
        waiting_qs = waiting_qs.filter(antibody_id__in=list(pending_antibody_ids))
    waiting = defaultdict(set)
    waiting_count = defaultdict(int)
    for tid, app in waiting_qs.values_list("antibody__target_id", "application_type"):
        waiting[tid].add(app)
        waiting_count[tid] += 1

    tested = defaultdict(set)
    for tid, ab_id in (Antibody.objects.using(DB)
                       .filter(target_id__in=ids, publication_images__isnull=False)
                       .values_list("target_id", "pk")):
        tested[tid].add(ab_id)

    coverage = board_svc.application_coverage(ids)

    # One report per gene at most on any screen — `target_board.report_showing`
    # is the rule that a reader and a writer must pick the *same* row, and the
    # completed one is what a reader outside the lab is asking about.
    best = {}
    for report in (Report.objects.using(DB).filter(target_id__in=ids)
                   .order_by("-f1000_date", "-zenodo_date")):
        if not board_svc.is_completed_report(report):
            continue
        best.setdefault(report.target_id, report)

    out = {}
    for target in targets:
        tid = target.pk
        report = best.get(tid)
        gene = target.gene_name or ""
        out[tid] = {
            "gene": gene,
            "has_public_page": tid in public_ids,
            # Absolute-able by the caller; a relative path here so nothing in
            # this module has to know the site's own hostname.
            "public_path": f"/antibodies/{gene}/" if (tid in public_ids and gene) else None,
            "tested_antibodies": len(tested.get(tid, ())),
            "awaiting_release": waiting_count.get(tid, 0),
            "applications": {
                app: {
                    "published": app in published.get(tid, ()),
                    "awaiting_release": app in waiting.get(tid, ()),
                    # Which sites have run the procedure. Evidence, not a status
                    # field — `application_coverage`'s own rule.
                    #
                    # Two vocabularies, and they differ on exactly one value: a
                    # figure's application is `ICC-IF` (`PublicationImage`'s
                    # enum) and a session's procedure is `IF`
                    # (`target_board.APPLICATIONS`). Reading one with the
                    # other's key returns an empty list rather than raising, so
                    # every IF session would silently report as never run.
                    "run_at_sites": coverage.get(tid, {}).get(
                        "IF" if app == "ICC-IF" else app, []),
                }
                for app in APPLICATIONS
            },
            "report": _report_block(report),
            "stage": _stage(tid, report, published, waiting),
        }
    return out


def _report_block(report) -> dict:
    if report is None:
        return {"status": "none", "doi": "", "url": "", "date": ""}
    doi = report.zenodo_doi or report.f1000_doi or ""
    date = report.f1000_date or report.zenodo_date
    return {
        "status": "published",
        # `services/doi.py` is the one reader for these two fields, and `link`
        # is empty unless the value is absolute — a relative href works, goes
        # nowhere, and reads as the record having been lost.
        "doi": doi_svc.display(doi) or doi,
        "url": doi_svc.link(doi),
        "date": date.isoformat() if date else "",
    }


def _stage(tid, report, published, waiting) -> str:
    """One word for where this gene is, for a reader who wants to sort a list.

    Deliberately coarse and deliberately derived. It is **not** a status field
    and nothing writes it: every value here is a fact about records that exist,
    the same way `gene_progress` refuses to be a percentage.
    """
    if report is not None:
        return "reported"
    if published.get(tid):
        return "published_figures"
    if waiting.get(tid):
        return "awaiting_release"
    return "in_progress"
