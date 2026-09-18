"""Views for the Antibody & Controls Selection Support prototype.

Public, deterministic, self-contained. The walkthrough itself is a client-side
state machine (``templates/selector/tool.html``); the server does three things:

* ``gene_lookup``  — enrich a gene with OGA / DepMap / Horizon / proteomics data,
  reusing the pipeline *feasibility* engine. Every source degrades gracefully:
  a blocked network or an empty dev database returns ``found: False`` and the
  UI falls back to "here's where to look yourself" — never an error page.
* ``record_plan``  — persist a completed run (the "record that they used it")
  and hand back a shareable code + PDF link.
* ``plan_pdf``     — render the stored plan to a multi-page PDF.
"""
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import PACK_VERSION, SelectionRecord
from .pdf import render_plan_pdf

logger = logging.getLogger(__name__)


@ensure_csrf_cookie
@require_GET
def tool(request):
    """Render the single-page walkthrough."""
    return render(request, "selector/tool.html", {
        "pack_version": PACK_VERSION,
        "gene_suggestions": _oga_gene_names(),
    })


def _oga_gene_names():
    """A short list of genes that have public OGA data — used only for the
    datalist autocomplete. Best-effort; never fatal."""
    try:
        from pipeline.models import Target
        return list(
            Target.objects.using("pipeline_db")
            .filter(gene_name__isnull=False,
                    antibodies__publication_images__isnull=False)
            .exclude(gene_name="")
            .values_list("gene_name", flat=True)
            .distinct()
            .order_by("gene_name")[:600]
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("gene suggestion lookup failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Gene enrichment (AJAX)
# ---------------------------------------------------------------------------

@require_GET
def gene_lookup(request):
    """Enrich a gene for the tool. All sub-lookups are independently guarded."""
    gene = (request.GET.get("gene") or "").strip()
    if not gene:
        return JsonResponse({"error": "Please enter a gene name"}, status=400)

    payload = {
        "query": gene,
        "canonical": gene.upper(),
        "oga": _oga_status(gene),
        "uniprot": _safe(lambda: _uniprot(gene), "uniprot"),
        "depmap": _safe(lambda: _depmap(gene), "depmap"),
        "horizon_ko": _safe(lambda: _horizon(gene), "horizon"),
        "proteomics": _safe(lambda: _proteomics(gene), "proteomics"),
    }
    return JsonResponse(payload)


def _safe(fn, label):
    try:
        return fn()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("selector %s lookup failed: %s", label, e)
        return {"found": False, "error": "unavailable"}


def _oga_status(gene):
    """Is this gene in the public OGA (knockout-controlled) dataset?"""
    result = {"in_dataset": False, "gene_page_url": "", "canonical": gene.upper(),
              "recommended": {}}
    try:
        from pipeline.models import Target
        target = (Target.objects.using("pipeline_db")
                  .filter(gene_name__iexact=gene).first())
        if not target:
            return result
        has_data = target.antibodies.filter(
            publication_images__isnull=False).exists()
        result["canonical"] = target.gene_name
        if has_data:
            result["in_dataset"] = True
            result["gene_page_url"] = reverse("antibody_table",
                                              args=[target.gene_name])
            tested = target.antibodies.filter(
                publication_images__isnull=False).distinct()
            result["recommended"] = {
                "WB": tested.filter(wb_recommended=True).count(),
                "IP": tested.filter(ip_recommended=True).count(),
                "IF": tested.filter(if_recommended=True).count(),
                "FC": tested.filter(fc_recommended=True).count(),
            }
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("OGA status lookup failed for %s: %s", gene, e)
    return result


def _uniprot(gene):
    """The one outbound call on this endpoint, and the reason it is the cautious one.

    This runs on every pause in somebody's typing, so it goes through
    `uniprot.lookup_interactive`: cached, on a short deadline, and capped at a
    couple of threads. Asking `lookup_gene` directly parked all four of the
    site's threads on UniProt and got the instance restarted (2 Sep 2026 — see
    the block comment in `pipeline/services/uniprot.py`).

    `lookup` rather than `lookup_gene` underneath, so a pasted accession
    resolves: somebody typed `P05622` into this box the same afternoon and was
    told nothing was found about PDGFRB.
    """
    from pipeline.services import uniprot
    up = uniprot.lookup_interactive(gene)
    if not up.get("found"):
        return {"found": False, "error": up.get("error", "")}
    return {
        "found": True,
        "gene_name": up.get("gene_name", ""),
        "protein_name": up.get("protein_name", ""),
        "mass_kda": up.get("mass_kda"),
        "uniprot_id": up.get("uniprot_id", ""),
        "synonyms": up.get("gene_synonyms", []),
        "subcellular": up.get("subcellular_location", ""),
    }


def _depmap(gene):
    from pipeline.services import depmap
    dm = depmap.get_expression(gene)
    if not dm.get("found"):
        return {"found": False}
    lines = [
        {"name": ln["name"], "tpm": ln["tpm"]}
        for ln in (dm.get("cell_lines") or [])
        if ln.get("above_threshold")
    ][:8]
    return {
        "found": True,
        "threshold": depmap.EXPRESSION_THRESHOLD,
        "hap1_tpm": dm.get("hap1_tpm"),
        "expressing": lines,
        "expressing_count": dm.get("expressing_count", 0),
        "total_lines": dm.get("total_lines", 0),
    }


def _horizon(gene):
    from pipeline.services import horizon
    hz = horizon.lookup(gene)
    return {
        "found": bool(hz.get("found")),
        "count": hz.get("count", 0),
        "clones": (hz.get("clones") or [])[:6],
    }


def _proteomics(gene):
    from pipeline.models import ProteomicsExpression
    entries = (ProteomicsExpression.objects.using("pipeline_db")
               .filter(gene_name__iexact=gene.strip())
               .order_by("-protein_intensity"))
    if not entries.exists():
        return {"found": False}
    return {
        "found": True,
        "detected_count": entries.count(),
        "cell_lines": [
            {"name": e.cell_line, "intensity": round(e.protein_intensity, 2)}
            for e in entries[:8]
        ],
    }


# ---------------------------------------------------------------------------
# Record + PDF
# ---------------------------------------------------------------------------

MAX_JSON_BYTES = 200_000  # a plan is small; reject anything absurd

# Consumer/free mailbox providers — a *compliance* record needs institutional
# affiliation, so these are rejected for compliance runs (personal runs are fine
# with any address, or none). Not exhaustive, but covers the common ones.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
    "ymail.com", "rocketmail.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "outlook.fr", "live.com", "live.co.uk", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "pm.me", "gmx.com", "gmx.de", "mail.com", "yandex.com", "yandex.ru",
    "zoho.com", "qq.com", "163.com", "126.com", "hey.com", "fastmail.com",
    "tutanota.com",
}


def _is_institutional_email(email):
    """True if `email` looks like a real, non-consumer (institutional) address."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    email = (email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return False
    domain = email.rsplit("@", 1)[-1]
    return domain not in FREE_EMAIL_DOMAINS


def _compliance_identity_problem(name, email, institution):
    """Return a human message if a compliance record's identity is incomplete,
    else "" (empty) when it is valid. A compliance record must be attributable to
    a named person at an institution — that is what makes it auditable."""
    if not name:
        return "A compliance record needs your name."
    if not institution:
        return "A compliance record needs your institution."
    if not email:
        return "A compliance record needs your institutional email."
    if not _is_institutional_email(email):
        return ("A compliance record needs your institutional email — not a "
                "personal address (gmail, outlook, etc.). Use your university or "
                "organisation email, or choose “Just for me” instead.")
    return ""


@require_POST
def record_plan(request):
    """Persist a completed run and return a shareable code + PDF url."""
    if len(request.body) > MAX_JSON_BYTES:
        return JsonResponse({"error": "Payload too large"}, status=413)
    try:
        body = json.loads(request.body or "{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    plan = body.get("plan") or {}
    guidance = body.get("guidance") or []
    if not isinstance(plan, dict) or not isinstance(guidance, list):
        return JsonResponse({"error": "Malformed plan"}, status=400)

    guidance = [str(g)[:600] for g in guidance][:60]

    name = (body.get("name") or "").strip()[:200]
    email = (body.get("email") or "").strip()[:254]
    institution = (body.get("institution") or "").strip()[:200]
    is_compliance = bool(body.get("is_compliance"))

    if is_compliance:
        problem = _compliance_identity_problem(name, email, institution)
        if problem:
            return JsonResponse({"error": problem}, status=400)

    rec = SelectionRecord(
        name=name,
        email=email,
        institution=institution,
        is_compliance=is_compliance,
        target_gene=str(plan.get("target_gene", ""))[:120],
        species=str(plan.get("species", ""))[:60],
        application=str(plan.get("application", ""))[:40],
        question_type=str(plan.get("question_type", ""))[:40],
        decision=str(plan.get("decision", ""))[:40],
        plan=plan,
        guidance=guidance,
        completed=True,
        pack_version=PACK_VERSION,
    )
    if request.user.is_authenticated:
        rec.academy_user_id = request.user.pk
    rec.save()

    return JsonResponse({
        "code": str(rec.public_code),
        "pdf_url": rec.get_pdf_url(),
        "record_url": reverse("selector:record_view", args=[rec.public_code]),
    })


@require_POST
def attach_identity(request, code):
    """Optionally attach a name/email to an already-saved run."""
    rec = SelectionRecord.objects.filter(public_code=code).first()
    if not rec:
        return JsonResponse({"error": "Not found"}, status=404)
    try:
        body = json.loads(request.body or "{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    rec.name = (body.get("name") or rec.name).strip()[:200]
    rec.email = (body.get("email") or rec.email).strip()[:254]
    rec.save(update_fields=["name", "email", "updated_at"])
    return JsonResponse({"ok": True})


def plan_pdf(request, code):
    rec = get_object_or_404(SelectionRecord, public_code=code)
    return render_plan_pdf(rec, request)


def record_view(request, code):
    """A tiny confirmation page shown after a run is saved."""
    rec = get_object_or_404(SelectionRecord, public_code=code)
    return render(request, "selector/record.html", {"rec": rec})
