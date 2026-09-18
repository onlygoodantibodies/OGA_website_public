"""
Pipeline views — Figure Cropper.

The build spec it was written against was retired on 16 Aug 2026 once the tool
shipped; its standing constraints (AI-free, never modify original pixels, the
object-key format) are in CLAUDE.md, and git history has the spec.

The human-in-the-loop tool that turns composite characterisation figures into
per-antibody crops and writes Target / Antibody / PublicationImage records.
NO AI: deterministic cropping + classical Tesseract OCR only.

This module owns:
  - cropper()             : serves the tool page (login-gated).
  - cropper_gene_status() : JSON — wires the gene field to the pipeline DB
                            (banner + overwrite gate; spec §6a).

OCR and commit endpoints land here in later slices.
All pipeline queries route to pipeline_db (PostgreSQL in prod).
"""
import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import CropperSession, CropperImage
from pipeline.services.cropper import db, engine, ocr, metadata as meta

DB = "pipeline_db"


def _owner(request):
    return request.user.username


def _member(request):
    """The cropper's own ``Member`` row, or None.

    Same shape as ``views/dashboard.py::_dash_member`` and for the same reason:
    pipeline users exist in two databases and are matched by username. It is
    what gives an antibody the cropper creates a **site** — which is half of
    what makes a row a vial, and what decides whether it gets the lab's next
    A-number.
    """
    from django.contrib.auth.models import User
    from pipeline.models import Member
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None


def _image_payload(im):
    return {
        "id": im.id, "app": im.application_type, "name": im.name,
        "order": im.order, "url": im.image.url if im.image else None,
        "nat_w": im.nat_w, "nat_h": im.nat_h, "grid": im.grid, "mapping": im.mapping,
    }


@pipeline_member_required
def cropper(request):
    """Serve the grid-cropper workspace.

    An optional ``?gene=`` prefills the gene field and auto-loads that gene's
    antibodies + DB status on page load — so an assistant (Server B's
    ``cropper_autoload``) can deep-link straight into a ready-to-crop workspace.
    """
    initial_gene = (request.GET.get("gene") or "").strip()
    return render(request, "pipeline/cropper.html", {"initial_gene": initial_gene})


@pipeline_member_required
@require_GET
def cropper_gene_status(request):
    """Return what exists for a gene today, so the front end can show the
    status banner and set the overwrite gate before any work is done."""
    gene = (request.GET.get("gene") or "").strip()
    if not gene:
        return JsonResponse({"error": "gene is required"}, status=400)

    status = db.gene_status(gene)
    return JsonResponse({
        "gene": status.gene,
        "exists": status.exists,
        "target_id": status.target_id,
        "protein_name": status.protein_name,
        "uniprot_id": status.uniprot_id,
        "antibody_count": status.antibody_count,
        "images_by_app": status.images_by_app,
        "existing_catalogues": [a.catalogue_number for a in status.antibodies],
        # Which gene the crops will be filed under, and how it was reached. The
        # legend drawn on every IF/FC crop names the gene, so the page has to
        # draw the resolved one or the preview and the saved figure disagree.
        "matched_via": status.matched_via,
        "resolved_gene": status.resolved_gene or status.gene,
        "ambiguous": status.ambiguous,
        "blocked": status.blocked,
        "banner": status.banner,
    })


@pipeline_member_required
@require_POST
def cropper_ocr(request):
    """Read each cell's printed catalogue title with Tesseract and fuzzy-match it
    to the candidate set (pasted list ∪ the gene's existing DB antibodies).

    POST (multipart): image (file), cells (JSON [[l,t,r,b],...] in natural image
    px, reading order), gene (str), candidates (JSON [catalogue,...]).
    Returns one result per cell, in order. Degrades gracefully: if Tesseract is
    unavailable the caller falls back to reading-order pre-fill.
    """
    from PIL import Image  # local import: only needed on this path

    # image comes either as a fresh upload OR as a staged image id (after a
    # session is reloaded, the browser no longer has the original File).
    upload = request.FILES.get("image")
    staged_id = request.POST.get("staged_id")
    if upload is None and not staged_id:
        return JsonResponse({"error": "image or staged_id is required"}, status=400)
    try:
        cells = json.loads(request.POST.get("cells") or "[]")
        pasted = json.loads(request.POST.get("candidates") or "[]")
    except json.JSONDecodeError:
        return JsonResponse({"error": "cells/candidates must be valid JSON"}, status=400)

    gene = (request.POST.get("gene") or "").strip()
    candidates = db.candidate_catalogues(gene, pasted)

    if not ocr.available():
        # Graceful degradation — tell the client to use reading-order fallback.
        return JsonResponse({
            "tesseract": False, "candidates": candidates,
            "cells": [{"raw": "", "match": None, "score": 0,
                       "ambiguous": False} for _ in cells],
        })

    try:
        if upload is not None:
            img = Image.open(upload)
        else:
            row = CropperImage.objects.using(DB).filter(id=staged_id).first()
            if row is None or not row.image:
                return JsonResponse({"error": "staged image not found"}, status=404)
            img = Image.open(row.image.open("rb"))
        img.load()
    except Exception:
        return JsonResponse({"error": "could not read the image"}, status=400)

    rects = [(int(c[0]), int(c[1]), int(c[2]), int(c[3])) for c in cells]
    results = ocr.prefill(img, rects, candidates)
    return JsonResponse({
        "tesseract": True,
        "candidates": candidates,
        "cells": [{
            "raw": r.raw,
            "match": r.match,
            "score": round(r.score, 1),
            "ambiguous": r.ambiguous,
            "runner_up": r.runner_up,
            "confident": r.confident,
        } for r in results],
    })


@pipeline_member_required
@require_POST
def cropper_parse_metadata(request):
    """Parse a pasted antibody table into per-antibody metadata rows (spec §4).
    Also reports, per row, whether the vendor resolves to an existing Company or
    would create a new one (so the human sees it before commit)."""
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    rows = meta.parse_table(d.get("text", ""))
    for r in rows:
        existing = db.resolve_company(r.get("company", ""), r.get("catalogue", ""), create=False)
        r["company_status"] = "existing" if existing else ("new" if r.get("company") else "none")
        # The name the *write* will store, always — not `display_name` (the public
        # spelling, which announced a substitution for rows nothing was changing)
        # and not "only when a row already exists" (which said nothing on exactly
        # the catalogue-prefix path that can file a vial under the wrong vendor).
        # Both halves of the run-5 bug, still here on the cropper after the other
        # previews were fixed — a preview rule belongs everywhere or nowhere.
        r["company_resolved"] = db.resolved_company_name(
            r.get("company", ""), r.get("catalogue", ""), existing=existing)
    return JsonResponse({"rows": rows, "count": len(rows)})


# ── Session persistence (spec §7) ─────────────────────────────────────────────

@pipeline_member_required
@require_POST
def cropper_stage_image(request):
    """Stage an uploaded composite so it survives a reload. Returns the new
    CropperImage id + URL + natural dimensions. Attached to a session on save."""
    from PIL import Image
    upload = request.FILES.get("image")
    if upload is None:
        return JsonResponse({"error": "image file is required"}, status=400)
    try:
        w, h = Image.open(upload).size
    except Exception:
        return JsonResponse({"error": "could not read the image"}, status=400)

    # Refused at the door, before anything is stored. `Image.open` parses the
    # header without decoding the pixels, so this costs nothing and happens
    # before the figure can reach a commit — which is where an oversized one
    # would take the container down mid-save, with nothing on screen to say why.
    too_big = engine.size_refusal(w, h, name=(request.POST.get("name") or upload.name))
    if too_big:
        return JsonResponse({"error": too_big}, status=400)

    upload.seek(0)
    im = CropperImage(
        application_type=request.POST.get("app", "WB"),
        name=(request.POST.get("name") or upload.name)[:255],
        nat_w=w, nat_h=h,
    )
    im.image.save(upload.name, upload, save=False)
    im.save(using=DB)
    return JsonResponse({"id": im.id, "url": im.image.url, "nat_w": w, "nat_h": h})


@pipeline_member_required
@require_POST
def cropper_session_save(request):
    """Create or update a session with its gene settings and per-image grid +
    mapping. Images are referenced by the ids returned from stage-image."""
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    owner = _owner(request)
    sid = d.get("session_id")
    if sid:
        sess = CropperSession.objects.using(DB).filter(id=sid, owner_username=owner).first()
        if sess is None:
            return JsonResponse({"error": "session not found"}, status=404)
    else:
        sess = CropperSession(owner_username=owner)

    for f in ("gene", "cell_line", "genotype", "uniprot_id", "protein_name", "antibody_list"):
        setattr(sess, f, d.get(f, getattr(sess, f)) or "")
    sess.fc_secondary = bool(d.get("fc_secondary", sess.fc_secondary))
    sess.overwrite_ack = bool(d.get("overwrite_ack", sess.overwrite_ack))
    if isinstance(d.get("metadata"), dict):
        sess.metadata = d["metadata"]
    if isinstance(d.get("recommended"), dict):
        sess.recommended = d["recommended"]
    sess.save(using=DB)

    incoming_ids = []
    for i, img in enumerate(d.get("images", [])):
        im = CropperImage.objects.using(DB).filter(id=img.get("id")).first()
        if im is None:
            continue          # image was never staged / bad id — skip
        im.session = sess
        im.order = i
        im.application_type = img.get("app", im.application_type)
        im.name = (img.get("name") or im.name)[:255]
        im.grid = img.get("grid", {}) or {}
        im.mapping = img.get("mapping", {}) or {}
        im.save(using=DB)
        incoming_ids.append(im.id)

    # drop images the user removed from this session
    sess.images.using(DB).exclude(id__in=incoming_ids).delete()

    return JsonResponse({"session_id": sess.id, "saved": len(incoming_ids)})


@pipeline_member_required
@require_GET
def cropper_session_load(request):
    owner = _owner(request)
    sess = CropperSession.objects.using(DB).filter(
        id=request.GET.get("id"), owner_username=owner).first()
    if sess is None:
        return JsonResponse({"error": "session not found"}, status=404)
    return JsonResponse({
        "session_id": sess.id,
        "gene": sess.gene, "cell_line": sess.cell_line, "genotype": sess.genotype,
        "fc_secondary": sess.fc_secondary, "uniprot_id": sess.uniprot_id,
        "protein_name": sess.protein_name, "antibody_list": sess.antibody_list,
        "overwrite_ack": sess.overwrite_ack, "metadata": sess.metadata or {},
        "recommended": sess.recommended or {},
        "images": [_image_payload(im) for im in sess.images.using(DB).all()],
    })


@pipeline_member_required
@require_GET
def cropper_session_list(request):
    owner = _owner(request)
    sessions = CropperSession.objects.using(DB).filter(owner_username=owner)[:50]
    return JsonResponse({"sessions": [{
        "id": s.id, "gene": s.gene,
        "images": s.images.using(DB).count(),
        "updated_at": s.updated_at.isoformat(),
    } for s in sessions]})


@pipeline_member_required
@require_POST
def cropper_session_delete(request):
    """Delete one of the caller's own saved sessions, plus its staged figures on
    R2 (the CASCADE removes the CropperImage rows; FileField files aren't deleted
    automatically). Does NOT touch anything already committed to the live site."""
    owner = _owner(request)
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    sess = CropperSession.objects.using(DB).filter(
        id=d.get("session_id"), owner_username=owner).first()
    if sess is None:
        return JsonResponse({"error": "session not found"}, status=404)
    for im in sess.images.using(DB).all():
        if im.image:
            try:
                im.image.storage.delete(im.image.name)
            except Exception:
                pass
    sess.delete(using=DB)
    return JsonResponse({"deleted": True})


@pipeline_member_required
@require_POST
def cropper_commit(request):
    """Dry-run or apply a session commit. dry_run=true returns the summary;
    dry_run=false performs the writes.

    **The crops land in the review queue, not on the public website** — see
    ``services/review.py``. Releasing them is a separate act on
    ``/pipeline/review/``, which is where the reply points.

    The overwrite acknowledgement is still asked for, and it is now a statement
    about what *release* will do: a crop for an antibody+application that
    already has a published figure is queued as a replacement for it. The
    person who made the crop is the one who knows whether that is intended, and
    they are standing here rather than at the review meeting.
    """
    from pipeline.services.cropper import commit as commit_mod
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    sess = CropperSession.objects.using(DB).filter(
        id=d.get("session_id"), owner_username=_owner(request)).first()
    if sess is None:
        return JsonResponse({"error": "session not found — save first"}, status=404)
    if not sess.gene:
        return JsonResponse({"error": "set a gene before committing"}, status=400)

    target, items, match = commit_mod.build_plan(sess)
    summary = commit_mod.summarize(sess, target, items, match)

    if d.get("dry_run", True):
        return JsonResponse({"dry_run": True, "summary": summary})

    # An older symbol naming two genes on file has no answer, and picking one
    # publishes a figure under a gene nobody chose. Refused before the overwrite
    # question, because which gene this is decides what "already published"
    # even means.
    if summary["refusal"]:
        return JsonResponse({"error": summary["refusal"], "summary": summary},
                            status=400)
    if summary["images_overwrite"] > 0 and not sess.overwrite_ack:
        return JsonResponse({"error": "overwrite not acknowledged",
                             "summary": summary}, status=409)
    if summary["crops"] == 0:
        return JsonResponse({"error": "nothing to commit — assign and map some cells"},
                            status=400)

    member = _member(request)
    try:
        target, summary = commit_mod.apply(sess, actor_member=member)
    except commit_mod.Refused as e:
        return JsonResponse({"error": str(e)}, status=400)
    from django.urls import reverse
    from urllib.parse import quote, urlencode
    board_url = reverse("pipeline:antibody_board")
    for c in summary.get("committed", []):
        c["edit_url"] = f"{board_url}?q={quote(str(c.get('catalogue') or ''))}"
    review_url = reverse("pipeline:review_queue")
    if target.gene_name:
        review_url = f"{review_url}?{urlencode({'gene': target.gene_name})}"
    return JsonResponse({
        "dry_run": False, "summary": summary, "target_id": target.id,
        # Where the crops went, and the page they are released from. Not the
        # public gene page: nothing this press did is on it, and linking there
        # would be the app claiming a publication it has deliberately withheld.
        "review_url": review_url,
        "gene_page_url": reverse("pipeline:target_detail", args=[target.id]),
    })
