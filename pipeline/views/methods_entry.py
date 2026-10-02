"""Methods — type a gene's published methods, see the paragraph, save it.

Thin over ``services/methods_entry.py``, which owns the cleaning, the preview
and the write. Three JSON endpoints behind one page, so a person on the page
and a session driving it with a login send the same requests:

* ``GET  /pipeline/methods/state/?gene=&app=`` — what is stored, and the gene's antibodies;
* ``POST /pipeline/methods/preview/`` — ``{gene, application, conditions,
  report_file, antibodies: [{id, amount, basis, where}]}`` → the paragraphs;
* ``POST /pipeline/methods/save/`` — the same body plus ``based_on`` (the
  preview's ``stamp``).
"""
from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.services import methods_entry as svc

logger = logging.getLogger(__name__)

__all__ = ["methods_entry", "methods_entry_state", "methods_entry_preview",
           "methods_entry_save"]

APP_LABELS = {"WB": "Western blot", "IP": "Immunoprecipitation",
              "ICC-IF": "Immunofluorescence", "FC": "Flow cytometry",
              "IHC": "Immunohistochemistry"}


def _not_found(gene):
    if not gene:
        return "Type a gene symbol, e.g. DHX58."
    return (f"No gene called “{gene}” is in the pipeline. Check the spelling, or add "
            "it on the target board first.")


def _app(value):
    value = (value or "WB").strip().upper()
    return "ICC-IF" if value in ("IF", "ICC-IF") else value


@pipeline_member_required
@require_GET
def methods_entry(request):
    gene = (request.GET.get("gene") or "").strip()
    app = _app(request.GET.get("app"))
    target = svc.target_for(gene)
    error = "" if target else (_not_found(gene) if gene else "")
    if app not in svc.APPLICATIONS:
        error, target = f"“{app}” is not an application.", None
    return render(request, "pipeline/methods_entry.html", {
        "gene": target.gene_name if target else gene,
        "nav_gene": target.gene_name if target else gene,
        "app": app,
        "applications": [(a, APP_LABELS[a]) for a in svc.APPLICATIONS],
        "state": svc.state(target, app) if target else None,
        "target": target,
        "error": error,
    })


@pipeline_member_required
@require_GET
def methods_entry_state(request):
    gene = (request.GET.get("gene") or "").strip()
    app = _app(request.GET.get("app"))
    target = svc.target_for(gene)
    if target is None:
        return JsonResponse({"ok": False, "error": _not_found(gene)}, status=404)
    if app not in svc.APPLICATIONS:
        return JsonResponse({"ok": False, "error": f"“{app}” is not an application — use "
                             f"one of {', '.join(svc.APPLICATIONS)}."}, status=400)
    return JsonResponse({"ok": True, **svc.state(target, app)})


def _body(request):
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return None, None, None, JsonResponse(
            {"ok": False, "error": "The request was not readable — reload the page and try again."},
            status=400)
    target = svc.target_for(str(d.get("gene") or ""))
    if target is None:
        return None, None, None, JsonResponse(
            {"ok": False, "error": _not_found(str(d.get("gene") or ""))}, status=404)
    return d, target, _app(d.get("application")), None


@pipeline_member_required
@require_POST
def methods_entry_preview(request):
    d, target, app, refused = _body(request)
    if refused:
        return refused
    try:
        return JsonResponse({"ok": True, **svc.plan(target, app, d)})
    except svc.MethodsRefusal as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)


@pipeline_member_required
@require_POST
def methods_entry_save(request):
    d, target, app, refused = _body(request)
    if refused:
        return refused
    try:
        result = svc.apply(target, app, d, based_on=str(d.get("based_on") or ""),
                           by=request.user.username)
    except svc.MethodsRefusal as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=409)
    logger.info("methods saved: %s %s by %s (%d antibody values)", target.gene_name, app,
                request.user.username, result["antibody_values"])
    return JsonResponse({"ok": True, **result})
