"""
Downloads & uploads — the internal, member-only whole-dataset file round-trip.

Download the entire pipeline scientific dataset (incl. unpublished; no academy /
auth data) as Excel or JSON, edit it (optionally with an LLM — e.g. fill missing
F1000/Zenodo DOIs + antibody URLs across many genes), and upload it back as an
**upsert**: gap-fills apply freely, overwriting a populated value needs explicit
confirmation (with a diff preview), and nothing is ever deleted/wiped/cleared.

All logic lives in ``pipeline/services/dataset.py``; these are thin views.
"""
from __future__ import annotations

import json
from django.utils.timezone import now as timezone_now

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member
from pipeline.services import dataset

DB = "pipeline_db"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _member(request):
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None


@pipeline_member_required
@require_GET
def data_io(request):
    """The Downloads & uploads landing page."""
    return render(request, "pipeline/data_io.html", {
        "legend": dataset.LEGEND_ROWS,
        "families": dataset.family_catalogue(),
        # The nightly full capture — everything the lab authors, including every
        # reading, which the family export above deliberately does not carry.
        "snapshot": _latest_snapshot(),
    })


def _latest_snapshot():
    """What the newest stored snapshot is, or why there isn't one.

    Reads the row, not a file: the capture is written by a Render Cron Job and
    read here, and those are two services with no shared filesystem between them
    (``pipeline/snapshot_models.py`` carries the why).

    Never raises — the page must render before the first capture has been taken,
    and before the migration has been applied, which is every environment that
    has not deployed yet.
    """
    from pipeline.services import snapshot as snap
    try:
        row = snap.newest()
    except Exception:
        return None
    if row is None:
        return None
    age = timezone_now() - row.taken
    return {
        "name": row.name,
        "taken": row.taken,
        "bytes": row.byte_size,
        "rows": row.row_total,
        "tables": row.table_count,
        # A snapshot that has stopped being taken is the failure mode worth
        # showing: a stale file looks exactly like a fresh one on a page.
        "stale": age.total_seconds() > 36 * 3600,
        "hours": int(age.total_seconds() // 3600),
    }


@pipeline_member_required
@require_GET
def dataset_snapshot_download(request):
    """The newest snapshot, decompressed, so it opens where it lands."""
    import gzip

    from pipeline.services import snapshot as snap
    row = snap.newest()
    if row is None:
        return HttpResponse(
            "No snapshot has been taken yet. It is written by a daily cron job "
            "running `manage.py dataset_snapshot`.", status=404)
    # `newest()` defers the blob so the page can draw a line of text without
    # pulling several MB; this is the one caller that actually wants the bytes.
    resp = HttpResponse(gzip.decompress(bytes(row.blob)),
                        content_type="application/json")
    resp["Content-Disposition"] = (
        f'attachment; filename="{row.name.replace(".json.gz", ".json")}"')
    return resp


@pipeline_member_required
@require_GET
def dataset_export(request):
    """Download the dataset. ?format=xlsx|json ; scope via ?target=<pk> or
    ?genes=SNCA,MAPT (default: the whole dataset). ?fields=<selection> picks
    which families/fields to include (see dataset.parse_selection); absent =
    the historical default columns."""
    fmt = (request.GET.get("format") or "xlsx").lower()
    selection = dataset.parse_selection(request.GET.get("fields", ""))
    targets = dataset.resolve_targets(
        target_id=request.GET.get("target"), genes=request.GET.get("genes", ""))

    if fmt == "json":
        payload = dataset.build_json(targets, selection=selection)
        resp = HttpResponse(json.dumps(payload, indent=2, ensure_ascii=False),
                            content_type="application/json")
        resp["Content-Disposition"] = 'attachment; filename="oga_pipeline_dataset.json"'
        return resp

    data = dataset.build_workbook(targets, selection=selection)
    resp = HttpResponse(data, content_type=_XLSX)
    resp["Content-Disposition"] = 'attachment; filename="oga_pipeline_dataset.xlsx"'
    return resp


@pipeline_member_required
@require_POST
def dataset_upload_preview(request):
    """Parse an uploaded file and return the read-only change preview (diff)."""
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    parsed = dataset.parse_upload(f)
    return JsonResponse(dataset.plan_upload(parsed))


@pipeline_member_required
@require_POST
def dataset_upload_commit(request):
    """Apply the upsert. Gap-fills + creates always apply; overwrites apply only
    when ``apply_overwrites`` is set (the user ticked the confirmation)."""
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)
    apply_overwrites = request.POST.get("apply_overwrites") in ("true", "1", "on")

    parsed = dataset.parse_upload(f)
    plan = dataset.plan_upload(parsed)
    if not plan.get("ok"):
        return JsonResponse(plan, status=400)
    result = dataset.apply_upload(
        parsed, apply_overwrites=apply_overwrites,
        member=_member(request))
    result["plan"] = plan
    return JsonResponse(result)
