"""Plan an experiment session by pasting / uploading the antibodies to test.

A one-page alternative to the multi-step ``session_create`` wizard, built on the
same *parse → preview → submit* pattern as the Add antibodies / Add cell lines
tools:

  header form (target, procedure, date, site, experimenter, cell lines)
    + a pasted / uploaded antibody list
    → editable preview (each antibody resolved: found / not found)
    → submit, which creates the session + a result row per antibody via the
      shared ``services.sessions.apply`` write-spine.

The step-by-step wizard (with protocol templates + full conditions) stays
available at ``session_create`` for people who want it.
"""
import json
from datetime import date

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import Member, Site, ExperimentSession, Target
from pipeline.services import bulk_sessions as bs
from pipeline.services import bench_results
from pipeline.services import sessions as sess

DB = "pipeline_db"


def _member(request):
    from django.contrib.auth.models import User
    try:
        pu = User.objects.using(DB).get(username=request.user.username)
        return Member.objects.using(DB).get(user_id=pu.pk, is_active=True)
    except Exception:
        return None

def _resolve_target(gene):
    target, _ = sess._resolve_target({"gene": (gene or "").strip()})
    return target

@pipeline_member_required
@require_POST
def session_plan_parse(request):
    """Parse pasted text (or re-preview edited rows) and resolve each antibody
    against the chosen target."""
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    target = _resolve_target(d.get("gene", ""))
    rows = d.get("rows")
    if rows is None:
        rows = bs.parse(d.get("text", ""))
    member = _member(request)
    items = bs.preview(target, rows, member)
    return JsonResponse({
        "items": items,
        "summary": bs.summarize(items),
        "target": target.gene_name if target else None,
        # The check used to answer about the antibody rows only, so the two cell
        # lines — the thing a WT/KO session *is* — went from typed to written
        # with nothing said in between. A bare `HAP1` resolves to your own site's
        # line, correctly, out of two that share the name; that is a sentence the
        # panel owes the reader, and a name that will be refused should be
        # refused here rather than at the end of a save.
        "cell_lines": _cell_line_preview(d, member),
        # A required field the check stays quiet about is a refusal deferred to
        # the save. Run 9's check said nothing about an empty Date and **Create
        # them** then answered `date is required and must be YYYY-MM-DD; got ''`
        # — the whole point of a check is to say what will happen before
        # anything is written.
        "blocking": _blocking(d),
    })


def _blocking(d):
    """Header problems that will stop the commit, named at check time.

    Asked of the same rule the writer uses (`services/sessions.py` refuses a
    date it cannot parse), not a second opinion about what is required.
    """
    from django.utils.dateparse import parse_date
    out = []
    raw = (d.get("date") or "").strip()
    if not raw:
        out.append("Date is empty — a session needs one before it can be created.")
    elif not parse_date(raw):
        out.append(f"Date {raw!r} is not YYYY-MM-DD.")
    if not (d.get("procedure_type") or "").strip():
        out.append("No application chosen.")
    return out


def _cell_line_preview(d, member):
    """What the commit will make of the two cell-line boxes. Never writes."""
    from pipeline.services import cell_lines as clines
    site_id = getattr(member, "site_id", None)
    return {
        "wt": clines.preview(d.get("cell_line_wt", ""),
                             genotype="WT", site_id=site_id),
        "ko": clines.preview(d.get("cell_line_ko", ""),
                             genotype="KO", site_id=site_id),
    }

def _header(d):
    return {
        "procedure_type": (d.get("procedure_type") or "").strip(),
        "gene": (d.get("gene") or "").strip(),
        "date": (d.get("date") or "").strip(),
        "experimenter_id": d.get("experimenter_id") or None,
        "site_id": d.get("site_id") or None,
        "status": (d.get("status") or "planned").strip(),
        "cell_line_wt": (d.get("cell_line_wt") or "").strip(),
        "cell_line_ko": (d.get("cell_line_ko") or "").strip(),
    }


@pipeline_member_required
@require_POST
def session_plan_commit(request):
    """Create the session + one result row per antibody.

    The WT/KO cell lines and any new antibody catalogue numbers are created
    here; **the target is not** (owner, 5 Sep 2026) — it is resolved, and a gene
    that is not in the pipeline is refused by name with the board to add it on.
    This panel used to mint one silently, with no tick and nothing in the
    preview naming it. Everything else commits in one transaction, so a bad
    header rolls the whole lot back — no orphans."""
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    from django.db import transaction
    from django.urls import reverse
    from pipeline.services.targets import resolve_target

    member = _member(request)
    header = _header(d)
    gene = header["gene"]
    if not gene:
        return JsonResponse({"ok": False, "errors": ["choose a target gene"]})
    rows = [r for r in (d.get("rows") or []) if (r.get("antibody") or "").strip()]
    if not rows:
        return JsonResponse({"ok": False, "errors": ["add at least one antibody"]})

    # **A target is added on the targets doors and nowhere else** (owner, 5 Sep
    # 2026). This panel created one silently — no tick, no preview naming it,
    # and a 10 s UniProt call inside the transaction below — so recording a
    # session against a mistyped gene minted that gene. It resolves now, and
    # refuses by name, before anything is opened.
    target = resolve_target(gene)
    if target is None:
        return JsonResponse({"ok": False, "errors": [
            f"'{gene}' is not a gene in the pipeline yet. A session is recorded "
            f"against a target that already exists — add it on the target board "
            f"first, then come back."]})

    created = {"target": None, "antibodies": [], "cell_lines": []}
    try:
        with transaction.atomic(using=DB):

            # Cell lines — resolve existing by name, or create a minimal record.
            wt_cl, wt_new = bs.resolve_or_create_cell_line(
                header["cell_line_wt"], genotype="WT", member=member)
            if wt_new:
                created["cell_lines"].append(str(wt_cl))
            ko_cl, ko_new = bs.resolve_or_create_cell_line(
                header["cell_line_ko"], genotype="KO", target=target, parent=wt_cl, member=member)
            if ko_new:
                created["cell_lines"].append(str(ko_cl))

            # Antibodies — reuse existing, create new catalogue numbers on target.
            result_specs = []
            for r in rows:
                ab, ab_new = bs.resolve_or_create(target, r, member)
                if ab_new:
                    created["antibodies"].append(str(ab))
                spec = {"antibody": ab.id}
                note = (r.get("comments") or "").strip()
                if note:
                    spec["comments"] = note
                result_specs.append(spec)

            # Session — pass target_id (avoids re-resolving); cell-line names now
            # exist so services.sessions resolves them within this transaction.
            payload = {**header, "target_id": target.pk, "results": result_specs}
            res = sess.apply(payload, member=member)
            if not res.get("ok"):
                raise ValueError("; ".join(res.get("errors", ["could not create session"])))
    except ValueError as e:
        return JsonResponse({"ok": False, "errors": [str(e)]})

    return JsonResponse({
        "ok": True,
        "session_id": res["session_id"],
        "created_results": res["created_results"],
        "created_antibodies": created["antibodies"],
        "created_target": created["target"],
        "created_cell_lines": created["cell_lines"],
        # The board, with the new session's results open — not the retired page.
        "detail_url": (f"{reverse('pipeline:session_board')}"
                       f"?open={res['session_id']}"),
    })


# ── Record results from a filled bench sheet (on a session) ──────────────

def _get_session(pk):
    return get_object_or_404(
        ExperimentSession.objects.using(DB).select_related("target"), pk=pk)


def _looks_like_workbook(f) -> bool:
    """Is this the full workbook rather than a printed bench sheet?

    Asked here rather than of the person uploading. A session now offers two
    files and they go back through different parsers, and "which of these two
    did you pick?" is a question the file itself answers: a workbook tab is
    headed `session_ref`, a bench sheet is not. One file input, no wrong answer
    available.
    """
    from pipeline.services import session_import
    try:
        parsed = session_import.parse_template(f)
    except Exception:
        return False
    return any("session_ref" in {str(h).strip().lower() for h in block.get("header", [])}
               for block in parsed.values())


def _workbook_is_this_session(parsed, session):
    """`""` if every tab names this session, otherwise the refusal.

    The same check the bench sheet's stamp gets, for the same reason: two
    sessions of one gene differ only in the readings on them, so the wrong file
    in the wrong box produces a completely plausible record.
    """
    from pipeline.services import session_import
    for block in parsed.values():
        for row in block.get("rows", []):
            ref = next((v for k, v in row.items()
                        if str(k).strip().lower() == "session_ref"), "")
            found = session_import._parse_session_ref(ref)
            if found is None:
                return ("This workbook creates new sessions — its session_ref column "
                        f"ends '(new)'. To fill in session #{session.pk}, download "
                        "its own workbook from this panel. To create sessions from "
                        "this file, use the gene's Upload filled workbook button.")
            if found != session.pk:
                return (f"This workbook was downloaded for session #{found}, but you "
                        f"are recording into session #{session.pk}. Nothing has been "
                        f"read from it.")
    return ""


@pipeline_member_required
@require_POST
def session_results_upload(request, pk):
    """Preview what an uploaded sheet would record — bench sheet or workbook."""
    session = _get_session(pk)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "no file uploaded"}, status=400)

    if _looks_like_workbook(f):
        from pipeline.services import session_import
        try:
            parsed = session_import.parse_template(f)
        except ValueError as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        refusal = _workbook_is_this_session(parsed, session)
        if refusal:
            return JsonResponse({"ok": False, "error": refusal})
        out = session_import.plan_import(parsed, uploader=_member(request))
        out["kind"] = "workbook"
        return JsonResponse(out)

    # `_looks_like_workbook` has read the stream to the end. `parse_template`
    # rewinds itself; `bench_results.parse` does not, and an exhausted upload
    # reads as an empty sheet — which would be reported as "the sheet is empty"
    # about a sheet with rows on it.
    try:
        f.seek(0)
    except Exception:
        pass
    try:
        parsed = bench_results.parse(f, session.procedure_type)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"could not read the sheet ({e})"}, status=400)
    result = bench_results.plan(session, parsed)
    result["kind"] = "bench"
    # The parsed sheet goes back with the preview so the save records exactly
    # what was shown, rather than re-reading a file that may have been swapped —
    # the same reason `import_upload` returns its `text`. The stamp travels with
    # it so the commit re-checks it too: a preview is not a permission slip.
    result["rows"] = parsed["rows"]
    result["sheet_session_id"] = parsed["sheet_session_id"]
    # Which procedure's sheet this is travels with the stamp, and for the same
    # reason: the commit re-asks both. A sheet with no stamp — an older download,
    # or one somebody typed — still has its columns to be judged on.
    result["sheet_procedure"] = parsed["sheet_procedure"]
    return JsonResponse(result)


@pipeline_member_required
@require_POST
def session_results_commit(request, pk):
    """Write the previewed results into the session's result rows."""
    session = _get_session(pk)
    try:
        d = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
    rows = d.get("rows") or []
    if not rows:
        return JsonResponse({"ok": False, "error": "nothing to record"}, status=400)
    # The stamp is re-checked here, not only on the preview: a preview is not a
    # permission slip, and the two requests are minutes apart with a file input
    # in between.
    return JsonResponse(bench_results.apply(
        session,
        {"rows": rows,
         "sheet_session_id": d.get("sheet_session_id"),
         "sheet_procedure": d.get("sheet_procedure")},
        member=_member(request)))
