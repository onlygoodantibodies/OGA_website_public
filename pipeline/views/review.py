"""The review queue — figures the cropper has made, before they are published.

Thin over ``services/review.py``, which owns every refusal and every write.

Four endpoints and one page:

  * ``review_queue``   — the page: genes with figures waiting, one gene at a time.
  * ``review_rows``    — the figures for one gene, as JSON.
  * ``review_image``   — the staged bytes, behind ``pipeline_member_required``.
  * ``review_release`` — publish a chosen set, with the count that was shown.
  * ``review_discard`` — take a crop out of the queue without publishing it.
  * ``review_ihc_image`` / ``review_manifest`` / ``review_ihc_withdraw`` —
    whole IHC figures for a gene's IHC page (``services/ihc_figures.py``):
    their private bytes, what releasing the *ticked* set would do, and taking
    one released figure back off the page. Release and discard take
    ``figure_ids`` beside ``ids``, under one summed count.

The image is served from here rather than linked at storage for the same two
reasons ``views/attachments.py`` gives: the object lives in a bucket with no
public route (``pipeline/storages.py``), and an unreleased figure on a
guessable public URL is released whatever the database says.
"""
from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from OGA_website import edge_cache
from pipeline.decorators import pipeline_member_required
from pipeline.models import (IhcFigure, Member, PendingIhcFigure,
                             PendingPublicationImage, Target)
from pipeline.services import ihc_figures as ihc_svc
from pipeline.services import outcomes as outcome_svc
from pipeline.services import review as svc
from pipeline.views import stored_image

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
    """Serialise with each row's image URL — this app's, not the bucket's —
    and the whole judgement: the outcome axes and what release will print."""
    items = list(items)
    rows = svc.rows_for(
        items, url_of=lambda i: reverse("pipeline:review_image", args=[i.pk]))
    judged = svc.judgements(items) if items and items[0].status == svc.PENDING else {}
    for row in rows:
        row["judgement"] = judged.get(row["id"])
    return rows


def _vocabulary():
    """The axis buttons, from the writer's own value sets — a page keeping its
    own list would offer a control the save refuses — and the applications
    from the writer's own keys too, so a sixth joins here with no edit."""
    return {
        "axes": {app: list(outcome_svc.axes_for(app))
                 for app in outcome_svc.APPLICATION_AXES},
        "axis_labels": outcome_svc.AXIS_LABELS,
        "values": {
            f"{app}|{axis}": [
                {"value": v,
                 "label": outcome_svc.VERDICT_LABELS.get(v, v.title()),
                 "tone": outcome_svc.tone_of(axis, v)}
                for v in outcome_svc.values_for(app, axis)]
            for app in outcome_svc.APPLICATION_AXES
            for axis in outcome_svc.axes_for(app)},
        "verdict_labels": outcome_svc.VERDICT_LABELS,
        "caveat_axes": sorted(outcome_svc.CAVEAT_AXES),
    }


def _figure_rows(figs):
    """Whole IHC figures as JSON, each thumbnail served by this app — the
    bytes are private until released."""
    return ihc_svc.rows_for(
        figs, url_of=lambda f: reverse("pipeline:review_ihc_image", args=[f.pk]))


def _released_figure_rows(target_id):
    """The gene's whole figures on its public IHC page, for the released list
    and its per-figure Withdraw. The image is the public copy."""
    figs = list(ihc_svc.public_for_target(target_id))
    rows = ihc_svc.rows_for(figs, url_of=lambda f: f.image.url if f.image else "")
    return rows


def _gene_payload(request, target_id):
    """Everything the queue redraws for one gene, from one place — so every
    endpoint that redraws it returns the whole figures too."""
    items = list(svc.for_target(target_id or 0))
    figs = list(ihc_svc.for_target(target_id or 0))
    return {
        "rows": _rows_for(request, items),
        "figures": _figure_rows(figs),
        "manifest": svc.manifest(items, figs),
    }


def _ids(payload, key="ids"):
    raw = payload.get(key) or []
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

    focus, rows, manifest, figures = None, [], None, []
    if gene:
        focus = (Target.objects.using(DB)
                 .filter(gene_name__iexact=gene).first())
        if focus is not None:
            payload = _gene_payload(request, focus.pk)
            rows, figures, manifest = (payload["rows"], payload["figures"],
                                       payload["manifest"])

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
        "figures": figures,
        "manifest": manifest,
        "vocabulary": _vocabulary(),
        "released": _rows_for(request, svc.released_qs()
                              .filter(antibody__target_id=focus.pk)[:40])
        if focus else [],
        "released_figures": _released_figure_rows(focus.pk) if focus else [],
        # Withdrawing a whole figure is the same gate as withdrawing a gene.
        "why_not_withdraw": svc.withdraw_refusal(member, is_superuser),
        "ihc_page_url": (reverse("antibody_ihc", kwargs={"gene_name": focus.gene_name})
                         if focus and focus.gene_name else ""),
    })


@pipeline_member_required
@require_GET
def review_rows(request):
    """One gene's waiting figures, redrawn after a release without a reload."""
    target_id = (request.GET.get("target_id") or "").strip()
    try:
        target_id = int(target_id or 0)
    except ValueError:
        target_id = 0
    return JsonResponse({"ok": True, **_gene_payload(request, target_id),
                         "released_figures": _released_figure_rows(target_id)})


@pipeline_member_required
@require_GET
def review_image(request, pk):
    """The staged crop's bytes. Members only — this is not published yet."""
    item = PendingPublicationImage.objects.using(DB).filter(pk=pk).first()
    if item is None or not item.image:
        return JsonResponse({"ok": False, "error": "no staged image here"},
                            status=404)
    version = stored_image.version_of(item)
    cached = stored_image.not_modified(request, item.image, version)
    if cached is not None:
        return cached
    try:
        handle = item.image.open("rb")
    except Exception:
        logger.exception("pending figure %s could not be opened", pk)
        return JsonResponse(
            {"ok": False,
             "error": "That figure could not be read from storage. The record "
                      "is fine; the file behind it is not."}, status=502)
    return stored_image.serve(handle, item.image, version)


@pipeline_member_required
@require_GET
def review_ihc_image(request, pk):
    """A queued whole IHC figure's bytes. Members only: these are private
    until released, and this view is the only way the app shows them."""
    fig = PendingIhcFigure.objects.using(DB).filter(pk=pk).first()
    if fig is None or not fig.image:
        return JsonResponse({"ok": False, "error": "no whole figure here"},
                            status=404)
    version = stored_image.version_of(fig)
    cached = stored_image.not_modified(request, fig.image, version)
    if cached is not None:
        return cached
    try:
        handle = fig.image.open("rb")
    except Exception:
        logger.exception("whole IHC figure %s could not be opened", pk)
        return JsonResponse(
            {"ok": False,
             "error": "That figure could not be read from storage. The record "
                      "is fine; the file behind it is not."}, status=502)
    return stored_image.serve(handle, fig.image, version)


def _chosen(payload):
    """The crops and whole figures a press names, as they stand now."""
    items = list(svc.pending_qs().filter(pk__in=_ids(payload)))
    figs = list(ihc_svc.pending_qs().filter(pk__in=_ids(payload, "figure_ids")))
    return items, figs


@pipeline_member_required
@require_POST
def review_manifest(request):
    """What releasing **the ticked** figures would do — asked of the server
    for the chosen set, not read off the whole gene's manifest, because "this
    gives the gene its first public page" and "these whole figures have no
    gene page to go on" change as soon as a crop is unticked."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    items, figs = _chosen(payload)
    return JsonResponse({"ok": True, "manifest": svc.manifest(items, figs)})


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

    items, figs = _chosen(payload)
    try:
        result = svc.release_all(items, figs, actor=request.user.username,
                                 consented_count=payload.get("count"))
    except (svc.Refused, ihc_svc.Refused) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    except Exception:
        # Never `alert(str(e))` — detail to the log, a sentence to the page.
        logger.exception("release failed")
        return JsonResponse(
            {"ok": False,
             "error": "Something went wrong publishing those figures and "
                      "nothing was published. The staged crops are untouched."},
            status=500)

    # After the commit, never inside it: the edge holds public pages for a
    # week, so a release nobody purges after is a release nobody sees.
    purged, purge_note = edge_cache.purge_public_pages()
    crops = result.crops
    ihc_genes = sorted({f["gene"] for f in result.whole_figures if f["gene"]})
    return JsonResponse({
        "ok": True,
        "purged": purged,
        "purge_note": purge_note,
        "released": len(crops.released) if crops else 0,
        "whole_figures": len(result.whole_figures),
        "replaced": crops.replaced if crops else 0,
        "recommended": crops.recommended if crops else 0,
        "genes": result.genes,
        "new_public_genes": crops.new_public_genes if crops else [],
        # The gene page for crops, and the IHC page for whole figures — a
        # figure-only release otherwise linked nothing at all.
        "public_urls": (
            [{"gene": g, "url": reverse("antibody_table", kwargs={"gene_name": g})}
             for g in (crops.genes if crops else []) if g]
            + [{"gene": f"{g} IHC page",
                "url": reverse("antibody_ihc", kwargs={"gene_name": g})}
               for g in ihc_genes]),
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
    return JsonResponse({"ok": True, "changed": changed,
                         **_gene_payload(request, items[0].antibody.target_id)})


@pipeline_member_required
@require_POST
def review_discard(request):
    """Take staged figures out of the queue without publishing them."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)

    member, is_superuser = _asker(request)
    items, figs = _chosen(payload)
    for item in items:
        why_not = svc.discard_refusal(item, member, is_superuser)
        if why_not:
            return JsonResponse({"ok": False, "error": why_not}, status=403)
    for fig in figs:
        why_not = ihc_svc.discard_refusal(fig, member, is_superuser,
                                          request.user.username)
        if why_not:
            return JsonResponse({"ok": False, "error": why_not}, status=403)
    try:
        removed = svc.discard(items, actor=request.user.username)
        removed += ihc_svc.discard(figs)
    except (svc.Refused, ihc_svc.Refused) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    return JsonResponse({"ok": True, "discarded": removed})


@pipeline_member_required
@require_POST
def review_ihc_withdraw(request):
    """Take whole IHC figures off a gene's IHC page, back into the queue.

    Superusers only — the same reader as withdrawing a gene
    (``review.withdraw_refusal``), since it changes what the public site says.
    It **unpublishes**: the public copy is deleted and the private one stays
    on the queued row. ``count`` is the number the panel printed. The edge is
    purged after the commit, or the page goes on showing the figure for a week.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    member, is_superuser = _asker(request)
    why_not = svc.withdraw_refusal(member, is_superuser)
    if why_not:
        return JsonResponse({"ok": False, "error": why_not}, status=403)
    figs = list(IhcFigure.objects.using(DB)
                .filter(pk__in=_ids(payload, "figure_ids"))
                .select_related("target")
                .prefetch_related("antibody_links__antibody"))
    try:
        result = svc.withdraw_whole_figures(figs, actor=request.user.username,
                                            consented_count=payload.get("count"))
    except svc.Refused as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    except Exception:
        logger.exception("whole-figure withdraw failed")
        return JsonResponse(
            {"ok": False,
             "error": "Something went wrong withdrawing that figure and nothing "
                      "was changed. It is still on the IHC page."}, status=500)
    purged, purge_note = edge_cache.purge_public_pages()
    return JsonResponse({"ok": True, "withdrawn": len(result.withdrawn),
                         "restaged": result.restaged,
                         "already_queued": result.already_queued,
                         "purged": purged, "purge_note": purge_note})


@pipeline_member_required
@require_POST
def review_judge(request):
    """Record one outcome axis for a waiting figure's antibody.

    The half of the judgement the recommendation button cannot hold — *does it
    detect the target*, *is it selective* — made from the same card, because
    the public wording is built from both and a meeting that could only set one
    of them sent people to Judge outcomes, which refuses a figure that is not
    published yet.

    Writes ``AntibodyOutcome`` through ``outcomes.record``, the writer Judge
    outcomes uses. Unlike the recommendation it is **not held on the staged
    row**: it is a fact about the antibody, so on an antibody that already has
    a public figure for this application it reaches that page now. The card
    says so on exactly those rows.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)

    items = list(svc.pending_qs().filter(pk__in=_ids(payload)))
    if not items:
        return JsonResponse(
            {"ok": False,
             "error": "That figure is no longer waiting — somebody may have "
                      "released or discarded it. Pick the gene again to see "
                      "the queue as it stands."}, status=409)
    item = items[0]
    member, is_superuser = _asker(request)
    # Same gate as the recommendation beside it: the two halves of one call.
    why_not = svc.recommend_refusal(item, member, is_superuser)
    if why_not:
        return JsonResponse({"ok": False, "error": why_not}, status=403)
    axis = (payload.get("axis") or "").strip()
    value = (payload.get("value") or "").strip()
    # A caveat rides on a recommendation — here the one the meeting has set on
    # this staged figure, which is what release will apply.
    why_not = outcome_svc.caveat_refusal(axis, item.recommended,
                                         item.application_type)
    if why_not and value:
        return JsonResponse({"ok": False, "error": why_not}, status=400)
    try:
        outcome_svc.record(
            item.antibody_id, item.application_type, axis, value,
            actor=request.user.username)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return JsonResponse({"ok": True,
                         **_gene_payload(request, item.antibody.target_id)})
