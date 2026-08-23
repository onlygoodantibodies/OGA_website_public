"""
Recommendation manager — visual, gene-at-a-time curation of the OGA
"recommended" flags (WB / IP / ICC-IF / FC) on pipeline Antibodies.

Moved here from core/views.py (the old `admin_recommendations` tool at
/admin-tools/recommendations/, gated by Django admin `is_staff`). This version
lives on the pipeline behind @pipeline_member_required — the same gate as every
other pipeline write path — and reads/writes pipeline_db directly.

The tool shows every antibody for a gene with its published validation images
side by side; clicking an image toggles that antibody's recommended flag for
that application. Recommendations can also be set one antibody at a time on the
antibody edit form, and at publish time in the cropper; this is the bulk,
image-first review surface for a whole gene.
"""
from __future__ import annotations

import json
import logging

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Target, Antibody, Member, PublicationImage
from pipeline.services import clonality as clonality_svc
from pipeline.services import review as review_svc
from pipeline.services import targets as target_svc

logger = logging.getLogger(__name__)

DB = "pipeline_db"

# application code (public label) → the Antibody boolean field
_APP_FIELD = {
    "WB": "wb_recommended",
    "IP": "ip_recommended",
    "ICC-IF": "if_recommended",
    "FC": "fc_recommended",
}


def _supplier_name(ab):
    """OGA canonical supplier name for an antibody, or None."""
    if ab.company_id and ab.company:
        return ab.company.display_name or ab.company.name
    return None


def _requested_gene(request):
    """What ``?gene=`` on this page asks for, resolved against the database.

    ``?gene=`` means the same thing on all four boards — an exact gene, so a
    link can carry one from board to board — and this page ignored it: the
    picker still read "Select a gene", while the nav links on the same page had
    picked the value up and pointed at the boards filtered to it. The value was
    being read; it was not being used by the one control it is for. With a
    160-item dropdown and no link here from a gene's own page, setting a gene's
    recommendations meant scrolling to find it by hand every time (twentieth
    field test).

    Returns ``(gene, note)``. ``gene`` is the target's own spelling — resolved
    case-insensitively, so a link carrying ``ace`` or one of the mis-cased
    symbols still selects the right option, since the ``<option>`` values are
    what ``rec_genes`` stored. ``note`` is what the page has to say when the
    request cannot be honoured, and there are two of those that look identical
    from outside and are not: a gene that is not in the pipeline at all, and one
    that is but has no published figures, which is the only reason a real gene
    is absent from this particular picker. A picker sitting on its placeholder
    says neither.
    """
    raw = (request.GET.get("gene") or "").strip()
    if not raw:
        return "", ""

    # Shared parsing with the boards: `?gene=` there may name several, and this
    # page is one gene at a time, so say which one was taken rather than
    # silently dropping the rest.
    terms = target_svc.gene_terms(raw) or [raw]
    wanted, extra = terms[0], terms[1:]
    tail = (f" Showing {wanted} only — this page sets one gene at a time, and "
            f"{', '.join(extra)} {'were' if len(extra) > 1 else 'was'} not "
            "opened.") if extra else ""

    target = (Target.objects.using(DB)
              .filter(gene_name__iexact=wanted)
              .first())
    if target is None:
        return "", f"There is no gene called {wanted} in the pipeline." + tail

    gene = target.gene_name
    has_images = Antibody.objects.using(DB).filter(
        target_id=target.pk, publication_images__isnull=False).exists()
    if not has_images:
        return "", (
            f"{gene} has no published validation figures yet, so there is "
            "nothing to judge here. Crop one on Publish figures first — this "
            "page lists a gene once it has at least one." + tail)

    return gene, tail.strip()


@pipeline_member_required
def recommendations(request):
    """The recommendation manager page (gene picker + antibody cards)."""
    gene, note = _requested_gene(request)
    return render(request, "pipeline/recommendations.html", {
        "requested_gene": gene,
        "gene_note": note,
    })


@pipeline_member_required
def rec_genes(request):
    """All genes with at least one antibody that has a published image,
    plus antibody and recommendation counts (feeds the gene dropdown)."""
    targets = (
        Target.objects.using(DB)
        .filter(gene_name__isnull=False, antibodies__publication_images__isnull=False)
        .exclude(gene_name="")
        .distinct()
        .order_by("gene_name")
    )

    result = []
    total_antibodies = 0
    total_recommended = 0

    for target in targets:
        ab_count = target.antibodies.count()
        total_antibodies += ab_count

        rec_count = target.antibodies.filter(
            Q(wb_recommended=True) | Q(ip_recommended=True)
            | Q(if_recommended=True) | Q(fc_recommended=True)
        ).count()
        total_recommended += rec_count

        result.append({
            "name": target.gene_name,
            "antibody_count": ab_count,
            "rec_count": rec_count,
        })

    return JsonResponse({
        "genes": result,
        "total_antibodies": total_antibodies,
        "total_recommended": total_recommended,
    })


@pipeline_member_required
def rec_antibodies(request):
    """Antibodies (with images + current flags) for a single gene."""
    gene_name = request.GET.get("gene", "").strip()
    if not gene_name:
        return JsonResponse({"error": "Missing gene parameter"}, status=400)

    # `.filter().first()`, not `.get()`: `gene_name` is UNIQUE, but on
    # PostgreSQL that is case-*sensitive*, so the schema permits `Rab44` and
    # `RAB44` side by side — and `iexact` would then match two and raise
    # `MultipleObjectsReturned`, which reaches the page as a 500 rather than as
    # a missing gene. No such pair is on file, and every door that creates a
    # target checks `iexact` first, so this is about what the column allows
    # rather than about a route anybody can take today. `_requested_gene` above
    # asks the same question the same way.
    target = Target.objects.using(DB).filter(gene_name__iexact=gene_name).first()
    if target is None:
        return JsonResponse({"error": f"Gene not found: {gene_name}"}, status=404)

    antibodies = (
        target.antibodies
        .select_related("company")
        .prefetch_related("publication_images")
        .filter(publication_images__isnull=False)
        .distinct()
    )

    results = []
    for ab in antibodies:
        experiments = []
        for img in ab.publication_images.all():
            if img.image:
                experiments.append({
                    "app": img.application_type,
                    "app_display": img.get_application_type_display(),
                    # storage-backed URL — works for local media and R2
                    "image_url": img.image.url,
                })

        recs = {
            "WB": ab.wb_recommended,
            "IP": ab.ip_recommended,
            "ICC-IF": ab.if_recommended,
            "FC": ab.fc_recommended,
        }

        results.append({
            "id": ab.id,
            "name": ab.catalogue_number,
            "gene": target.gene_name,
            "rrid": ab.rrid or None,
            "supplier": _supplier_name(ab),
            "host": ab.host_species or None,
            # The label, not the enum — this panel asks a person to decide
            # which antibody the field should buy, and recombinant-versus-not
            # is most of that judgement. services/clonality.py.
            "clonality": clonality_svc.label(ab),
            "recommendations": recs,
            "experiments": experiments,
        })

    member, is_superuser = _asker(request)
    return JsonResponse({
        "antibodies": results,
        # Drawn with the cards rather than fetched separately: a button whose
        # count arrives after the grid is a button that is briefly wrong.
        "withdraw": dict(review_svc.withdraw_manifest(target.pk),
                         gene=target.gene_name,
                         why_not=review_svc.withdraw_refusal(member, is_superuser)),
    })


def _asker(request):
    """Who is asking — their ``Member`` row, and whether they are a superuser.

    Same shape as ``views/review.py::_asker``; withdrawing is gated on the same
    answer, because it is the same act in the other direction.
    """
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        member = Member.objects.using(DB).select_related("site").get(
            user_id=pu.pk, is_active=True)
    except Exception:
        member = None
    return member, bool(request.user.is_superuser)


@pipeline_member_required
@require_POST
def rec_withdraw(request):
    """Take this gene's published figures off the public site, into the queue.

    The other half of the press this page has always had. Setting a
    recommendation says *which* antibody the field should buy; this says the
    evidence should not be public at all yet — the case it was built for being a
    gene whose knockout was never confirmed, where the figures are real and
    nobody can say whether a band in the KO lane is the antibody or the line.

    It is not a delete. ``review.withdraw`` re-stages the crops into
    ``/pipeline/review/`` first, so releasing them again is one press once the
    question is settled.

    ``count`` is the number the panel printed, passed back so a set that has
    grown since is refused rather than withdrawn — ``deletion.py``'s rule, and
    ``release``'s.
    """
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    member, is_superuser = _asker(request)
    why_not = review_svc.withdraw_refusal(member, is_superuser)
    if why_not:
        return JsonResponse({"ok": False, "error": why_not}, status=403)

    gene = (payload.get("gene") or "").strip()
    target = Target.objects.using(DB).filter(gene_name__iexact=gene).first()
    if target is None:
        return JsonResponse(
            {"ok": False, "error": f"There is no gene called {gene}."}, status=404)

    images = list(PublicationImage.objects.using(DB)
                  .filter(antibody__target_id=target.pk)
                  .select_related("antibody"))
    if not images:
        return JsonResponse(
            {"ok": False,
             "error": f"{target.gene_name} has no published figures, so there "
                      f"is nothing to withdraw."}, status=409)

    try:
        result = review_svc.withdraw(images, actor=request.user.username,
                                     consented_count=payload.get("count"))
    except review_svc.Refused as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    except Exception:
        # Never `alert(str(e))` — detail to the log, a sentence to the page.
        logger.exception("withdraw failed for %s", target.gene_name)
        return JsonResponse(
            {"ok": False,
             "error": "Something went wrong withdrawing those figures and "
                      "nothing was changed. The gene is still on the public "
                      "site."}, status=500)

    return JsonResponse({
        "ok": True,
        "gene": target.gene_name,
        "withdrawn": len(result.withdrawn),
        "restaged": result.restaged,
        "already_queued": result.already_queued,
        "recommendations_cleared": result.recommendations_cleared,
        "genes_leaving_public": result.genes_leaving_public,
        "review_url": "/pipeline/review/",
    })


@pipeline_member_required
@require_POST
def rec_toggle(request):
    """
    Toggle a recommendation for a specific antibody + application.
    Expects JSON body: { "antibody_id": 123, "application": "WB", "value": true }
    WRITES to pipeline_db (Antibody recommendation booleans).
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    antibody_id = data.get("antibody_id")
    application = data.get("application", "").strip()
    value = data.get("value", False)

    if application not in _APP_FIELD:
        return JsonResponse({"error": f"Invalid application: {application}"}, status=400)

    try:
        ab = Antibody.objects.using(DB).get(pk=antibody_id)
    except Antibody.DoesNotExist:
        return JsonResponse({"error": f"Antibody not found: {antibody_id}"}, status=404)

    field = _APP_FIELD[application]
    setattr(ab, field, bool(value))
    ab.save(using=DB, update_fields=[field])

    return JsonResponse({
        "status": "ok",
        "antibody_id": antibody_id,
        "antibody_name": ab.catalogue_number,
        "application": application,
        "value": bool(value),
    })
