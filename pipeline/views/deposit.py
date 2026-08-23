"""Depositing a gene's data to Zenodo — preview, then submit.

Two endpoints, both POST, because the second one leaves the building and the
first is the only place a person sees what it will send. Logic is
``services/deposit.py``; these are thin.

The preview makes **no network call**, so it answers with no token configured
and on a blocked network — which is the state the site is in until somebody has
made a Zenodo account. That is deliberate: the thing worth reading is what would
be sent, and it is readable now.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member, Target
from pipeline.services import deposit as svc
from pipeline.services import zenodo

logger = logging.getLogger(__name__)

DB = "pipeline_db"


def _member(request):
    return (Member.objects.using(DB)
            .filter(user__username=request.user.username).first())


def _target(request, pk):
    return Target.objects.using(DB).filter(pk=pk).first()


@pipeline_member_required
@require_POST
def deposit_preview(request, pk):
    target = _target(request, pk)
    if target is None:
        return JsonResponse({"ok": False, "error": "unknown gene"}, status=404)
    try:
        return JsonResponse({"ok": True, **svc.plan(target)})
    except Exception:
        logger.exception("deposit preview failed for target %s", pk)
        return JsonResponse({"ok": False, "error":
                             "That deposit could not be worked out. Nothing "
                             "has been changed or sent."}, status=500)


@pipeline_member_required
@require_POST
def deposit_submit(request, pk):
    """Create the draft and hand it to the community for review.

    It does not publish: a curator accepting the submission is what does that,
    which is Zenodo's own behaviour and the reason this route was chosen.
    """
    target = _target(request, pk)
    if target is None:
        return JsonResponse({"ok": False, "errors": ["unknown gene"]}, status=404)
    try:
        result = svc.apply(target, member=_member(request),
                           message=(request.POST.get("message") or "").strip())
    except zenodo.ZenodoError as exc:
        # Already logged with Zenodo's own field-level detail; the page gets the
        # sentence, never the exception.
        return JsonResponse({"ok": False, "errors": [str(exc)]})
    except Exception:
        logger.exception("deposit failed for target %s", pk)
        return JsonResponse({"ok": False, "errors": [
            "That deposit could not be completed. If a draft was created it is "
            "unpublished and can be removed from Zenodo — nothing is public."]},
            status=500)
    return JsonResponse(result)
