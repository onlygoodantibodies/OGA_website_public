"""Replace a figure — find an antibody, upload its new panel, send it to review.

Thin over ``services/figure_replace.py``, which owns the refusals and the
write. The upload is a plain form post followed by a redirect (so a reload does
not stage the file twice), and the receipt is drawn from the pending row the
redirect names.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Antibody, PendingPublicationImage
from pipeline.services import figure_replace as svc
from pipeline.services import review

logger = logging.getLogger(__name__)

DB = "pipeline_db"


def _public_url(img):
    try:
        return img.image.url
    except Exception:
        return ""


def _pending_url(item):
    return reverse("pipeline:review_image", args=[item.pk])


def _page(request, *, error="", error_ab=None, error_app=""):
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    gene = (request.GET.get("gene") or request.POST.get("gene") or "").strip()
    antibodies = svc.search(q, gene)
    figures = svc.figures_for(antibodies, public_url=_public_url,
                              pending_url=_pending_url)
    rows = [{"ab": ab,
             "apps": [{"code": app, "label": svc.APP_LABELS[app],
                       **figures[ab.pk][app]}
                      for app in review.APPLICATIONS]}
            for ab in antibodies]

    receipt = None
    staged_pk = request.GET.get("staged")
    if staged_pk and staged_pk.isdigit():
        item = (PendingPublicationImage.objects.using(DB)
                .select_related("antibody", "antibody__target",
                                "antibody__company")
                .filter(pk=int(staged_pk)).first())
        if item is not None:
            receipt = {
                "item": item,
                "label": svc.APP_LABELS.get(item.application_type,
                                            item.application_type),
                "gene": item.antibody.target.gene_name
                if item.antibody.target_id else "",
                "now": request.GET.get("now") == "1",
                "had_live": request.GET.get("had_live") == "1",
                "image_url": _pending_url(item),
            }

    return render(request, "pipeline/figure_replace.html", {
        "q": q,
        "gene": gene,
        "nav_gene": gene,
        "searched": bool(q or gene),
        "rows": rows,
        "max_results": svc.MAX_RESULTS,
        "applications": [(a, svc.APP_LABELS[a]) for a in review.APPLICATIONS],
        "receipt": receipt,
        "error": error,
        "error_ab": error_ab,
        "error_app": error_app,
    }, status=400 if error else 200)


@pipeline_member_required
@require_GET
def figure_replace(request):
    return _page(request)


@pipeline_member_required
@require_POST
def figure_replace_upload(request):
    ab_id = request.POST.get("antibody_id") or ""
    app = (request.POST.get("application_type") or "").strip()
    antibody = (Antibody.objects.using(DB).select_related("target")
                .filter(pk=int(ab_id)).first() if ab_id.isdigit() else None)
    if antibody is None:
        return _page(request, error="That antibody is no longer on file. "
                                    "Search for it again.")
    upload = request.FILES.get("image")
    if upload is None:
        return _page(request, error_ab=antibody.pk, error_app=app,
                     error="Choose the image file to upload first — a PNG or "
                           "JPG of the finished panel.")
    try:
        staged = svc.stage(antibody=antibody, application_type=app,
                           content=upload.read(),
                           staged_by=request.user.username)
    except svc.Refused as exc:
        return _page(request, error=str(exc), error_ab=antibody.pk,
                     error_app=app)
    except Exception:
        # Detail to the log, a sentence to the page — never the exception text.
        logger.exception("figure replace failed for antibody %s %s",
                         antibody.pk, app)
        return _page(request, error_ab=antibody.pk, error_app=app,
                     error="Something went wrong storing that image and "
                           "nothing was queued. The figure on the site is "
                           "unchanged.")

    params = {"q": request.POST.get("q") or "",
              "staged": staged.item.pk,
              "now": "1" if staged.live_changed_now else "0",
              "had_live": "1" if staged.had_live else "0"}
    if request.POST.get("gene"):
        params["gene"] = request.POST["gene"]
    return redirect(f"{reverse('pipeline:figure_replace')}?{urlencode(params)}")
