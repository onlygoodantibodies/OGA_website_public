"""Judge outcomes — the two-axis verdict for a gene's published figures.

Set recommendations asks one question of each figure: should the field buy this
antibody. That is one bit, and the reviewers' feedback is that the bench
recorded two — *does it detect the target* and *is it selective for it* — with
the middle case (detects, not selective) carrying a third of the published WB
dataset and no way to say so.

This page is that question, in the shape Set recommendations already proved:
pick a gene, see its figures side by side, judge them quickly. The differences
are deliberate.

* **Clicking a figure enlarges it, it does not judge it.** With two axes there
  is no single toggle a click could mean, and selectivity is exactly the call
  nobody should be making from a 120px thumbnail — the complaint CLAUDE.md
  already records about this family of page.
* **What a session recorded is shown and cannot be typed over here.** The bench
  record wins; this page fills its blanks. ``services/outcomes.py`` is the one
  reader that reconciles the two.
* **Gaps first.** The point is the 227 published WB antibodies with nothing
  recorded, so the gene picker is ordered by how many are outstanding and the
  grid opens filtered to them.

Nothing here touches ``Antibody.wb_recommended`` — the current recommendations
stay in play until the browser extension, the MCP and the public pages are built
and approved to carry the nuance (owner, 28 Aug 2026).
"""
from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Antibody, AntibodyOutcome, PublicationImage, Target
from pipeline.services import clonality as clonality_svc
from pipeline.services import outcomes as outcome_svc
from pipeline.services import targets as target_svc

logger = logging.getLogger(__name__)

DB = "pipeline_db"

#: The applications this page judges, in the order it offers them. FC is absent
#: on purpose: `histogram_shift` is blank on all 22 live rows, so a tab for it
#: would be an empty box with no source behind it. `services/outcomes.py` is the
#: one reader for which axes each of these answers — a column the page calls
#: judgeable must be one the reader reads, so widen both or neither.
APPLICATIONS = ("WB", "ICC-IF", "IP")
DEFAULT_APPLICATION = "WB"

#: How each is written where a person reads it. `ICC-IF` is the database's exact
#: value and must stay that in a URL or a payload; this is only the wording.
APPLICATION_LABELS = {"WB": "Western blot", "ICC-IF": "Immunofluorescence",
                      "IP": "Immunoprecipitation"}


def _application(request, body=None):
    """Which application is being judged. Unknown falls back, never 500s.

    A write says so in its **body**, a read in its query string. Reading only
    the query string was a live defect the browser test caught: the page posted
    a save with no application at all, so an immunofluorescence judgement was
    checked against the antibody's *western blot* figures, refused because there
    were none, and the card silently kept the old answer. Nothing on the screen
    named the application, so the refusal read as the click not registering.
    """
    if body is not None:
        asked = str(body.get("application") or "").strip()
        if asked in APPLICATIONS:
            return asked
    asked = (request.GET.get("app") or "").strip()
    return asked if asked in APPLICATIONS else DEFAULT_APPLICATION


@pipeline_member_required
def outcomes(request):
    """The page: gene picker, then that gene's figures with both axes."""
    application = _application(request)
    gene, note = target_svc.published_gene_request(
        request.GET.get("gene"), application=application)
    return render(request, "pipeline/outcomes.html", {
        "requested_gene": gene,
        "gene_note": note,
        "application": application,
        "applications": [{"code": a, "label": APPLICATION_LABELS[a]}
                         for a in APPLICATIONS],
    })


@pipeline_member_required
def outcome_genes(request):
    """Genes with at least one published WB figure, most outstanding first.

    The count that orders the list is what is *left to do*, not what exists —
    the page is for closing gaps, and a picker ordered alphabetically over 154
    genes makes the reader find the work by hand.
    """
    # Three queries for the whole picker, not three per gene: `by_gene` batches
    # the figures, the session rows and the stored judgements in one pass each.
    application = _application(request)
    genes = []
    total_figures = total_gaps = 0
    for gene, by_ab in outcome_svc.by_gene(application).items():
        stats = outcome_svc.summarise(by_ab, application)
        total_figures += stats["total"]
        total_gaps += stats["gaps"]
        genes.append({
            "name": gene,
            "antibody_count": stats["total"],
            "gap_count": stats["gaps"],
            "conflict_count": stats["conflicts"],
        })

    genes.sort(key=lambda g: (-g["gap_count"], g["name"]))
    # A worklist of its own. The conflicts span every gene, so working them one
    # gene at a time means opening 154 pages to find 113 rows.
    conflict_total = len(outcome_svc.conflicts(application))
    # The second worklist: not recommended, and it did the thing anyway. Every
    # row is drawing the amber it should — this is for confirming the negative
    # is still the one somebody would make in front of the figure, which is a
    # different question from a disagreement and so a different list.
    review_total = len(outcome_svc.unrecommended_but_capable(application))
    # The third: a session measured a ratio no band is derived from, and what
    # the record says is not what that number says. A disagreement like the
    # first list's, but between two pieces of *evidence* rather than between
    # evidence and a verdict — so it is counted and offered apart, and it is
    # ICC-IF's alone because it is the only application whose answer is a
    # number.
    unused_total = len(outcome_svc.ratio_not_used(application))
    return JsonResponse({
        "genes": genes,
        "total_antibodies": total_figures,
        "total_gaps": total_gaps,
        "conflict_total": conflict_total,
        "review_total": review_total,
        "unused_total": unused_total,
        "application": application,
    })


#: Which boolean carries each application, from the one reader — the map was
#: written out here, in `views/recommendations.py` and in the service, and three
#: copies of one mapping is three chances to disagree about `ICC-IF`.
_REC_FIELD = outcome_svc.REC_FIELD


def _supplier_name(ab):
    """OGA canonical supplier name for an antibody, or None."""
    if ab.company_id and ab.company:
        return ab.company.display_name or ab.company.name
    return None


def _payload(rows, summary, application, **extra):
    """The shape both lists return, built once so they cannot drift into two
    card layouts — which is how a reader comes to trust one screen and not the
    other."""
    return JsonResponse(dict({
        "antibodies": rows,
        "summary": summary,
        "axes": outcome_svc.axes_for(application),
        "axis_labels": outcome_svc.AXIS_LABELS,
        "verdict_labels": outcome_svc.VERDICT_LABELS,
        # The buttons, from the writer's own value sets. A page keeping its own
        # list would offer a control the save refuses.
        "axis_values": {
            axis: [{"value": v,
                    "label": outcome_svc.VERDICT_LABELS.get(v, v.title())}
                   for v in outcome_svc.values_for(application, axis)]
            for axis in outcome_svc.axes_for(application)},
        "application": application,
        # Printed on the page beside the bands, because a band with no cut-off
        # under it is a verdict the reader cannot check.
        "thresholds": {"floor": str(outcome_svc.SELECTIVE_FLOOR),
                       "strong": str(outcome_svc.STRONGLY_SELECTIVE)},
    }, **extra))


def _card(ab, axes, img, application, gene, **extra):
    """One antibody's card."""
    return dict({
        "id": ab.pk,
        "name": ab.catalogue_number,
        "gene": gene,
        "rrid": ab.rrid or None,
        "supplier": _supplier_name(ab),
        "clonality": clonality_svc.label(ab),
        "image_url": img.image.url if (img and img.image) else None,
        # A control, not context. Both halves of the answer are set from the
        # screen that has the figure on it: the judgement and the
        # recommendation disagreeing is the thing this page is for, and
        # settling it somewhere else means holding two screens in your head
        # (owner, 29 Aug 2026).
        "recommended": getattr(ab, _REC_FIELD[application]),
        "axes": axes,
        "verdict": outcome_svc.label(axes, application),
        "is_gap": outcome_svc.is_gap(axes, application),
        # Drawn on every card, not only in the conflicts list: a note that
        # vanishes the moment somebody opens the gene it belongs to is a note
        # nobody reads twice.
        "conflict_direction": outcome_svc.conflict_direction(
            axes, getattr(ab, _REC_FIELD[application]), application),
    }, **extra)


def _worklist_cards(application, found, note):
    """One card per antibody in a worklist, ordered by gene.

    Both lists come through here — where the recommendation and the data
    disagree, and where a negative did the thing anyway — because they are the
    same job on the same figures and two builders would drift into two card
    layouts, which is how a reader comes to trust one screen and not the other.

    A worklist spans every gene, which is the whole reason it exists: 113
    ICC-IF rows spread over the gene list means opening 154 pages to find them
    one or two at a time. Ordered by gene, so somebody working down it stays in
    one gene's figures while there are any.
    """
    if not found:
        return _payload([], {"total": 0, "judged": 0, "gaps": 0,
                             "conflicts": 0}, application,
                        conflicts_view=True, worklist_note=note)

    ab_ids = list(found)
    images = {
        img.antibody_id: img
        for img in PublicationImage.objects.using(DB)
        .filter(application_type=application, antibody_id__in=ab_ids)
    }
    antibodies = sorted(
        Antibody.objects.using(DB).select_related("company", "target")
        .filter(pk__in=ab_ids),
        key=lambda a: (found[a.pk]["gene"], a.catalogue_number or ""))

    rows = [
        _card(ab, found[ab.pk]["axes"], images.get(ab.pk), application,
              found[ab.pk]["gene"])
        for ab in antibodies
    ]
    # The header sentence comes from the server, because the two worklists
    # count different things and a page picking the words itself said "where
    # the recommendation and the recorded data disagree" over a list of rows
    # that agree with theirs perfectly well.
    return _payload(rows, {"total": len(rows), "judged": 0, "gaps": 0,
                           "conflicts": len(rows)}, application,
                    conflicts_view=True, worklist_note=note)


@pipeline_member_required
def outcome_antibodies(request):
    """One gene's published WB figures, with both axes and where each came from."""
    application = _application(request)
    if request.GET.get("conflicts"):
        return _worklist_cards(
            application, outcome_svc.conflicts(application),
            "where the recommendation and the recorded data disagree")
    if request.GET.get("review"):
        return _worklist_cards(
            application, outcome_svc.unrecommended_but_capable(application),
            "not recommended, and the data records on-target signal")
    if request.GET.get("unused"):
        return _worklist_cards(
            application, outcome_svc.ratio_not_used(application),
            "where a ratio on file disagrees with what is recorded")

    gene_name = (request.GET.get("gene") or "").strip()
    if not gene_name:
        return JsonResponse({"error": "Missing gene parameter"}, status=400)

    # `.filter().first()`, not `.get()`: `gene_name` is UNIQUE but on PostgreSQL
    # case-sensitively, so `iexact` could match two rows and reach the page as a
    # 500 rather than as a missing gene. Same question, same way, as
    # `targets.published_gene_request`.
    target = Target.objects.using(DB).filter(gene_name__iexact=gene_name).first()
    if target is None:
        return JsonResponse({"error": f"Gene not found: {gene_name}"}, status=404)

    by_ab = outcome_svc.for_gene(target.pk, application)
    images = {
        img.antibody_id: img
        for img in PublicationImage.objects.using(DB)
        .filter(application_type=application, antibody__target_id=target.pk)
    }
    antibodies = (
        Antibody.objects.using(DB)
        .select_related("company")
        .filter(pk__in=list(by_ab))
    )

    rows = [_card(ab, by_ab[ab.pk], images.get(ab.pk), application,
                  target.gene_name)
            for ab in antibodies]
    # Outstanding first, so the work is at the top of the grid as well as the
    # picker; then by catalogue number, which is how people refer to them.
    rows.sort(key=lambda r: (not r["is_gap"], r["name"] or ""))

    return _payload(rows, outcome_svc.summarise(by_ab, application),
                    application)


@require_POST
@pipeline_member_required
def outcome_save(request):
    """Record one axis of one antibody's judgement.

    Refuses to write over what a session recorded. The bench record is the
    record; this page fills its blanks, and a page that silently overrode a
    reading typed at the bench would be the worst kind of write this app makes —
    invisible, and wrong in the direction of confidence.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    application = _application(request, data)
    axis = (data.get("axis") or "").strip()
    value = (data.get("value") or "").strip()
    if axis not in outcome_svc.axes_for(application):
        return JsonResponse(
            {"error": f"{APPLICATION_LABELS[application]} does not answer "
                      f"“{axis}”."}, status=400)
    # The values this axis accepts, from the one reader — not the model's full
    # enum, which holds every application's vocabulary at once.
    allowed = set(outcome_svc.values_for(application, axis)) | {""}
    if value not in allowed:
        return JsonResponse({
            "error": f"“{value}” is not one of the answers "
                     f"{outcome_svc.AXIS_LABELS[axis].lower()} takes for "
                     f"{APPLICATION_LABELS[application]}."}, status=400)

    try:
        ab = Antibody.objects.using(DB).select_related("target").get(
            pk=data.get("antibody_id"))
    except (Antibody.DoesNotExist, ValueError, TypeError):
        return JsonResponse(
            {"error": f"Antibody not found: {data.get('antibody_id')}"}, status=404)

    current = outcome_svc.for_gene(ab.target_id, application).get(ab.pk)
    if current is None:
        return JsonResponse({
            "error": f"{ab.catalogue_number} has no published "
                     f"{APPLICATION_LABELS[application].lower()} figure, so "
                     "there is nothing to judge here."}, status=400)
    # No refusal past this point. Every judgement is makeable here, including
    # one that overrides a reading or a measured ratio (owner, 29 Aug 2026) —
    # the card keeps printing what was overridden, and no session row moves.
    row, _ = AntibodyOutcome.objects.using(DB).get_or_create(
        antibody_id=ab.pk, application_type=application)
    setattr(row, axis, value)
    row.note = data.get("note", row.note) or ""
    row.assessed_by = request.user.username
    row.save(using=DB)

    axes = outcome_svc.for_gene(ab.target_id, application)[ab.pk]
    return JsonResponse(_saved(ab, axes, application))


def _saved(ab, axes, application):
    """What either write hands back, so the card redraws the same way from both.

    The conflict note is part of it: a judgement and a recommendation can each
    create or settle a disagreement with the other, so a reply that carried only
    the half that was written would leave the note on screen saying something
    that was true a moment ago.
    """
    flag = getattr(ab, _REC_FIELD[application])
    return {
        "status": "ok",
        "antibody_id": ab.pk,
        "antibody_name": ab.catalogue_number,
        "axes": axes,
        "verdict": outcome_svc.label(axes, application),
        "is_gap": outcome_svc.is_gap(axes, application),
        "recommended": flag,
        "conflict_direction": outcome_svc.conflict_direction(
            axes, flag, application),
    }


@pipeline_member_required
@require_POST
def outcome_recommend(request):
    """Set the recommendation for one antibody in one application.

    The same write `views/recommendations.py::rec_toggle` makes, reachable from
    the screen that has the figure on it. Two doors to one field, deliberately
    — the judgement and the recommendation are set together or the disagreement
    between them cannot be settled in one place — and one field map behind both,
    so they cannot disagree about which column `ICC-IF` means.

    The stamp comes from ``Antibody.save``, which is why this saves the row
    rather than the field: `update_fields` is exactly what stopped
    `recommendations_set_at` being written when `rec_toggle` moved a flag.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    application = _application(request, data)
    value = data.get("value")
    if not isinstance(value, bool):
        return JsonResponse(
            {"error": "A recommendation is yes or no."}, status=400)

    try:
        ab = Antibody.objects.using(DB).select_related("target").get(
            pk=data.get("antibody_id"))
    except (Antibody.DoesNotExist, ValueError, TypeError):
        return JsonResponse(
            {"error": f"Antibody not found: {data.get('antibody_id')}"},
            status=404)

    axes_by_ab = outcome_svc.for_gene(ab.target_id, application)
    if ab.pk not in axes_by_ab:
        return JsonResponse({
            "error": f"{ab.catalogue_number} has no published "
                     f"{APPLICATION_LABELS[application].lower()} figure, so "
                     "there is nothing to recommend here."}, status=400)

    setattr(ab, _REC_FIELD[application], value)
    ab.save(using=DB)
    return JsonResponse(_saved(ab, axes_by_ab[ab.pk], application))
