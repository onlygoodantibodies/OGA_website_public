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
from pipeline.views import stored_image

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


def _image_url(im):
    """Where the cropper draws a staged figure from: the members-only view,
    never the storage's own URL — the upload is private (a whole IHC figure is
    copied from it and is private until release; `pipeline/storages.py`)."""
    from django.urls import reverse
    return reverse("pipeline:cropper_image", args=[im.id]) if im.image else None


def _image_payload(im):
    return {
        "id": im.id, "app": im.application_type, "name": im.name,
        "order": im.order, "url": _image_url(im),
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
        # The gene's antibodies as the cropper offers them — the list a figure
        # is mapped against, from the pipeline rather than a pasted table
        # (owner, 25 Sep 2026: the cropper was built for backfilling; the
        # pipeline is now where antibodies are entered, so they are on file
        # before anybody crops). One entry per catalogue, since the commit
        # resolves a crop to its antibody by catalogue.
        "antibodies": list({
            a.catalogue_number.strip().upper(): {
                "catalogue": a.catalogue_number, "company": a.company,
                "published": a.apps_with_images}
            for a in status.antibodies if (a.catalogue_number or "").strip()
        }.values()),
        # Which gene the crops will be filed under, and how it was reached. The
        # legend drawn on every IF/FC crop names the gene, so the page has to
        # draw the resolved one or the preview and the saved figure disagree.
        "matched_via": status.matched_via,
        "resolved_gene": status.resolved_gene or status.gene,
        "ambiguous": status.ambiguous,
        "blocked": status.blocked,
        # The targets board with this gene filled in, when it is not on file —
        # the cropper refuses to add genes, and a refusal names where to go.
        "add_target_url": status.add_target_url,
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
    return JsonResponse({"id": im.id, "url": _image_url(im), "nat_w": w, "nat_h": h,
                         "discard_token": _discard_signer(request).sign(str(im.id))})


def _discard_signer(request):
    """An unsaved upload records no owner, so the right to throw one away is a
    token handed to the page that added it — signed for that person, so a
    member cannot discard somebody else's figure by guessing its id."""
    from django.core.signing import Signer
    return Signer(salt=f"cropper-discard:{_owner(request)}")


@pipeline_member_required
@require_POST
def cropper_discard_image(request):
    """Delete an upload that was dropped before any save (✕, or starting or
    opening another session without saving). Unsaved figures cannot be got
    back after a reload — the page keeps no note of them — so these were left
    in storage for nobody (28 Sep 2026). A figure that already belongs to a
    saved session is left alone: that session still holds it, and its next
    save removes it (`cropper_session_save`)."""
    from django.core.signing import BadSignature
    from pipeline.services.cropper import clear_storage
    try:
        d = json.loads(request.body or "{}")
        image_id = int(_discard_signer(request).unsign(str(d.get("token") or "")))
    except (json.JSONDecodeError, BadSignature, ValueError):
        return JsonResponse({"error": "not a figure this page added"}, status=400)
    im = CropperImage.objects.using(DB).filter(id=image_id, session__isnull=True).first()
    if im is None:
        return JsonResponse({"deleted": False})
    kept = clear_storage.delete_files([im])
    im.delete(using=DB)
    return JsonResponse({"deleted": True, "files_not_deleted": kept})


@pipeline_member_required
@require_GET
def cropper_image(request, pk):
    """A staged figure's bytes, for the cropper's canvas. Members only: the
    upload is on private storage, and this view is the way the app shows it —
    same-origin, so the canvas can read it without the bucket's CORS."""
    im = CropperImage.objects.using(DB).filter(pk=pk).first()
    if im is None or not im.image:
        return JsonResponse({"error": "no staged figure here"}, status=404)
    # An upload is written once, under a name storage never reuses, so the key
    # alone names the bytes (`stored_image`).
    cached = stored_image.not_modified(request, im.image)
    if cached is not None:
        return cached
    try:
        handle = im.image.open("rb")
    except Exception:
        return JsonResponse({"error": (
            "That figure could not be read from storage. The session is fine; "
            "the file behind it is not — add the figure again.")}, status=502)
    return stored_image.serve(handle, im.image)


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

    # drop images the user removed from this session — files first, since a
    # row's delete leaves its file behind with nothing recording it
    from pipeline.services.cropper import clear_storage
    removed = list(sess.images.using(DB).exclude(id__in=incoming_ids))
    clear_storage.delete_files(removed)
    sess.images.using(DB).filter(id__in=[im.id for im in removed]).delete()

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


NOT_ADMIN = ("Clearing the cropper's storage deletes everybody's saved sessions and "
             "every figure no session holds, so it needs an administrator account. "
             "Your own sessions can be deleted one at a time with 🗑 Delete.")


@pipeline_member_required
@require_GET
def cropper_storage_manifest(request):
    """What "Clear storage" would delete — see `services/cropper/clear_storage.py`."""
    from pipeline.services.cropper import clear_storage
    if not request.user.is_superuser:
        return JsonResponse({"error": NOT_ADMIN}, status=403)
    return JsonResponse(clear_storage.manifest())


@pipeline_member_required
@require_POST
def cropper_storage_clear(request):
    """Delete what the manifest listed, only if it is still exactly that set."""
    from pipeline.services.cropper import clear_storage
    if not request.user.is_superuser:
        return JsonResponse({"error": NOT_ADMIN}, status=403)
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    try:
        return JsonResponse(clear_storage.clear(str(d.get("stamp") or "")))
    except clear_storage.Refused as refusal:
        return JsonResponse({"error": str(refusal)}, status=409)


def _stale_note(summary) -> str:
    """What an empty save leaves behind from this session — whole figures it
    no longer declares, which only a save with something in it takes out of
    the queue (`commit.stale_whole_figures`)."""
    queued = summary.get("whole_figures_dropped") or []
    if not queued:
        return ""
    return (f" This session's whole {'figure' if len(queued) == 1 else 'figures'} "
            f"{', '.join(queued)} {'is' if len(queued) == 1 else 'are'} still "
            f"waiting on the review queue — discard "
            f"{'it' if len(queued) == 1 else 'them'} there if "
            f"{'it is' if len(queued) == 1 else 'they are'} no longer wanted.")


@pipeline_member_required
@require_POST
def cropper_commit(request):
    """Dry-run or apply a session commit. dry_run=true returns the summary;
    dry_run=false performs the writes.

    **The crops land in the review queue, not on the public website** — see
    ``services/review.py``. Releasing them is a separate act on
    ``/pipeline/review/``, which is where the reply points.

    The overwrite acknowledgement is still asked for, because a crop for an
    antibody+application that already has a published figure replaces it —
    and since crops are written at their public key, the file a live gene page
    shows can change with this press, not only at release. The person who made
    the crop is the one who knows whether that is intended.

    ``consented_crops`` is the number the page's consent panel showed on its
    button. The write refuses if the saved session now yields a different
    number — the number on the button is what was consented to — and
    ``consented_stamp`` (``commit.plan_stamp``) refuses the same count doing
    something different.
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
        n = summary["images_overwrite"]
        return JsonResponse({"error": (
            f"{n} of these crops replace{'s' if n == 1 else ''} a published figure, "
            f"and the box “I understand these crops replace published figures” "
            f"under the gene is not ticked. Tick it if that is what you mean, "
            f"then save again."),
                             "summary": summary}, status=409)
    if summary["crops"] == 0 and summary["whole_figures"] == 0:
        return JsonResponse({"error": (
            "Nothing to save — no assigned cell is set to an antibody yet, and "
            "no figure is ticked for the gene's IHC page. Click the panels you "
            "want in box 5 and check each has an antibody, or tick “Show this "
            "whole figure on the gene's IHC page” under an IHC figure."
            + _stale_note(summary))}, status=400)
    # Two numbers were agreed to, and each is checked against its own count:
    # a crop that became a whole figure keeps the total and changes the press.
    for key, have, noun in (("consented_crops", summary["crops"], "crops"),
                            ("consented_whole_figures", summary["whole_figures"],
                             "whole IHC figures")):
        consented = d.get(key)
        if consented is None:
            continue
        try:
            consented = int(consented)
        except (TypeError, ValueError):
            consented = None
        if consented != have:
            return JsonResponse({"error": (
                f"You agreed to save {d.get(key)} {noun}, and the saved "
                f"session now makes {have}. Nothing was saved — press "
                f"Save to review queue again to see the new count."),
                "summary": summary}, status=409)
    # The count is not the whole consent: a figure released, or a crop queued
    # from another tab, between the check and the press keeps the count and
    # changes what these crops replace. The panel's stamp is compared too.
    stamp = d.get("consented_stamp")
    if stamp is not None and stamp != summary["stamp"]:
        return JsonResponse({"error": (
            "What these crops would replace has changed since you checked — a "
            "figure for one of these antibodies was released or queued "
            "meanwhile. Nothing was saved — press Save to review queue again "
            "to see what they replace now."),
            "summary": summary}, status=409)

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
