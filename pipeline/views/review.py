"""The review queue — figures the cropper has made, before they are published.

Thin over ``services/review.py``, which owns every refusal and every write.

Four endpoints and one page:

  * ``review_queue``   — the page: genes with figures waiting, one gene at a time.
  * ``review_rows``    — the figures for one gene, as JSON.
  * ``review_image``   — the staged bytes, behind ``pipeline_member_required``.
  * ``review_release`` — publish a chosen set, with the count that was shown.
  * ``review_discard`` — take a crop out of the queue without publishing it.

The image is served from here rather than linked at storage for the same two
reasons ``views/attachments.py`` gives: the object lives in a bucket with no
public route (``pipeline/storages.py``), and an unreleased figure on a
guessable public URL is released whatever the database says.
"""
from __future__ import annotations

import json
import logging

from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member, PendingPublicationImage, Target
from pipeline.services import review as svc

logger = logging.getLogger(__name__)

DB = "pipeline_db"


def _asker(request):
    """Who is asking — their ``Member`` row, and whether they are a superuser."""
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        member = Member.objects.using(DB).select_related("site").get(
            user_id=pu.pk, is_active=True)
    except Exception:
        member = None
    return member, bool(request.user.is_superuser)


def _rows_for(request, items):
    """Serialise with each row's image URL — this app's, not the bucket's."""
    return svc.rows_for(
        items, url_of=lambda i: reverse("pipeline:review_image", args=[i.pk]))


def _ids(payload):
    raw = payload.get("ids") or []
    out = []
    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


@pipeline_member_required
@require_GET
def review_queue(request):
    """Every gene with figures waiting, and one gene's figures when named.

    ``?gene=`` means the same thing it means on the four boards — an exact
    gene — so a link can carry one here from anywhere else in the app. The
    cropper's success panel and the gene page's panel both send it.
    """
    gene = (request.GET.get("gene") or "").strip()
    waiting = svc.genes_waiting()
    # Whether this person may release, and why not if not — asked of the same
    # reader the endpoint asks, so the greyed button and the refused save cannot
    # disagree. The control is drawn and disabled with the reason **on the
    # page**, never hidden: a control that is simply absent is a feature a
    # reader concludes does not exist, and somebody who can see the queue needs
    # to know who to ask.
    member, is_superuser = _asker(request)
    why_not_release = svc.release_refusal(member, is_superuser)

    focus, rows, manifest = None, [], None
    if gene:
        focus = (Target.objects.using(DB)
                 .filter(gene_name__iexact=gene).first())
        if focus is not None:
            items = list(svc.for_target(focus.pk))
            rows = _rows_for(request, items)
            manifest = svc.manifest(items)

    return render(request, "pipeline/review_queue.html", {
        "nav_gene": (focus.gene_name if focus else gene) or "",
        "can_release": not why_not_release,
        "why_not_release": why_not_release,
        "waiting": waiting,
        "waiting_total": sum(w["count"] for w in waiting),
        "gene": gene,
        "focus": focus,
        # A gene named in the URL that is not on file is not the same as a gene
        # with nothing waiting, and the page says which.
        "unknown_gene": bool(gene and focus is None),
        "rows": rows,
        "manifest": manifest,
        "released": _rows_for(request, svc.released_qs()
                              .filter(antibody__target_id=focus.pk)[:40])
        if focus else [],
    })


@pipeline_member_required
@require_GET
def review_rows(request):
    """One gene's waiting figures, redrawn after a release without a reload."""
    target_id = (request.GET.get("target_id") or "").strip()
    items = list(svc.for_target(target_id or 0))
    return JsonResponse({
        "ok": True,
        "rows": _rows_for(request, items),
        "manifest": svc.manifest(items),
    })


@pipeline_member_required
@require_GET
def review_image(request, pk):
    """The staged crop's bytes. Members only — this is not published yet."""
    item = PendingPublicationImage.objects.using(DB).filter(pk=pk).first()
    if item is None or not item.image:
        return JsonResponse({"ok": False, "error": "no staged image here"},
                            status=404)
    try:
        handle = item.image.open("rb")
    except Exception:
        logger.exception("pending figure %s could not be opened", pk)
        return JsonResponse(
            {"ok": False,
             "error": "That figure could not be read from storage. The record "
                      "is fine; the file behind it is not."}, status=502)
    return FileResponse(handle, filename=item.image.name.rsplit("/", 1)[-1])


@pipeline_member_required
@require_POST
def review_release(request):
    """Publish the named figures.

    Takes ``count`` — the number the panel printed — and refuses a set that has
    changed since. Two presses with the list between them, and the number on
    the button is what was consented to.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)

    member, is_superuser = _asker(request)
    why_not = svc.release_refusal(member, is_superuser)
    if why_not:
        return JsonResponse({"ok": False, "error": why_not}, status=403)

    items = list(svc.pending_qs().filter(pk__in=_ids(payload)))
    try:
        result = svc.release(items, actor=request.user.username,
                             consented_count=payload.get("count"))
    except svc.Refused as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    except Exception:
        # Never `alert(str(e))` — detail to the log, a sentence to the page.
        logger.exception("release failed")
        return JsonResponse(
            {"ok": False,
             "error": "Something went wrong publishing those figures and "
                      "nothing was published. The staged crops are untouched."},
            status=500)

    return JsonResponse({
        "ok": True,
        "released": len(result.released),
        "replaced": result.replaced,
        "recommended": result.recommended,
        "genes": result.genes,
        "new_public_genes": result.new_public_genes,
        "public_urls": [
            {"gene": g, "url": f"/antibodies/{g}/"} for g in result.genes if g],
    })


@pipeline_member_required
@require_POST
def review_recommend(request):
    """Set or clear OGA's recommendation on staged figures.

    Answers with the gene's rows **and its manifest**, because both change: the
    card's verdict, and the confirm panel's *"N antibodies will be marked
    recommended"*. A page that redrew one and not the other would print two
    disagreeing answers about one press.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)

    member, is_superuser = _asker(request)
    items = list(svc.pending_qs().filter(pk__in=_ids(payload)))
    if not items:
        return JsonResponse(
            {"ok": False,
             "error": "Those figures are no longer waiting — somebody may have "
                      "released or discarded them. Reload to see the queue as "
                      "it stands."}, status=409)
    for item in items:
        why_not = svc.recommend_refusal(item, member, is_superuser)
        if why_not:
            return JsonResponse({"ok": False, "error": why_not}, status=403)

    changed = svc.set_recommended(items, payload.get("recommended"),
                                  actor=request.user.username)
    target_id = items[0].antibody.target_id
    fresh = list(svc.for_target(target_id))
    return JsonResponse({
        "ok": True,
        "changed": changed,
        "rows": _rows_for(request, fresh),
        "manifest": svc.manifest(fresh),
    })


@pipeline_member_required
@require_POST
def review_discard(request):
    """Take staged figures out of the queue without publishing them."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)

    member, is_superuser = _asker(request)
    items = list(svc.pending_qs().filter(pk__in=_ids(payload)))
    for item in items:
        why_not = svc.discard_refusal(item, member, is_superuser)
        if why_not:
            return JsonResponse({"ok": False, "error": why_not}, status=403)
    try:
        removed = svc.discard(items, actor=request.user.username)
    except svc.Refused as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    return JsonResponse({"ok": True, "discarded": removed})
