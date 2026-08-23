"""
Feasibility lookup view.

Lookup (FREE, automatic on search):
  UniProt, DepMap expression, Horizon HAP1 KO availability, Proteomics,
  Pipeline DB check → traffic-light summary.

Bulk Add (FREE):
  Paste list of gene names → preview (bounded UniProt lookups) → create.
  The preview does the network; the commit does not. See
  ``services/bulk_targets.py`` for why that split is the whole design.

URL routes:
  /pipeline/feasibility/              → feasibility (search page)
  /pipeline/feasibility/lookup/       → feasibility_lookup (AJAX)
  /pipeline/feasibility/add/          → feasibility_add_target (POST)
  /pipeline/feasibility/bulk-check/   → feasibility_bulk_check (POST, preview)
  /pipeline/feasibility/bulk-add/     → feasibility_bulk_add (POST, commit)
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from pipeline.decorators import pipeline_member_required
from pipeline.models import (
    Antibody,
    CellLine,
    GrantingAgency,
    Member,
    Project,
    ProteomicsExpression,
    Site,
    Target,
    TargetNomination)
from pipeline.services import uniprot, depmap, horizon
from pipeline.services import bulk_targets, gene_symbol

logger = logging.getLogger(__name__)

DB = "pipeline_db"


@pipeline_member_required
@require_GET
def feasibility(request):
    """Render the feasibility search page.

    The site, funder and project are chosen here rather than assumed. Adding a
    gene has always written a ``TargetNomination`` at the adder's own site,
    unfunded — and said so only *after* the write, in a note under the button.
    Carl Laflamme read that as the app choosing for him: *"McGill is included by
    default when adding a new target entry (ideally there would be a drop down
    menu with McGill, Leicester, uOttawa, UBC, Cornell, etc)"*. It was not McGill
    — his account was on Montreal because uOttawa was not a site at all — but the
    complaint stands whatever the site: a choice you cannot see is a choice you
    did not make.

    **`?gene=` is read because something offers it.** `/pipeline/find/` says
    *nothing matches "ELP3"* and then, underneath, *check its feasibility and add
    it* — carrying the gene. This page rendered its empty form and the reader
    retyped what they had just typed. A destination that does not read `?gene=`
    does not get one (CLAUDE.md); this one is given one, so it has to work.

    No lookup happens here. The box is filled in and the page asks for itself,
    the same call pressing Search makes — so this view still touches no network,
    and a crawler on this URL costs nothing.
    """
    member = _member(request)
    return render(request, "pipeline/feasibility.html", {
        # `""` and not `None`: a template prints `None` as four characters, and
        # an autofocused box reading `None` is worse than an empty one.
        "gene": (request.GET.get("gene") or "").strip(),
        "sites": Site.objects.using(DB).filter(is_active=True).order_by("name"),
        "my_site_id": getattr(member, "site_id", None) or "",
        "my_site_name": getattr(getattr(member, "site", None), "name", ""),
        "agencies": GrantingAgency.objects.using(DB).order_by("name"),
        "projects": (Project.objects.using(DB).filter(is_active=True)
                     .select_related("granting_agency").order_by("name")),
    })


@pipeline_member_required
@require_GET
def feasibility_lookup(request):
    """
    AJAX endpoint — Stage 1 (FREE).
    Runs UniProt + DepMap + Proteomics + Pipeline DB.
    """
    gene = request.GET.get("gene", "").strip()
    if not gene:
        return JsonResponse({"error": "Please enter a gene name or UniProt ID"},
                            status=400)

    results = {
        "gene_query": gene,
        "uniprot": None,
        "depmap": None,
        "proteomics": None,
        "horizon_ko": None,
        "pipeline_status": None,
    }

    # UniProt is remote, DepMap is local — run in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            # `lookup`, not `lookup_gene`: a UniProt accession typed into this
            # box used to search `gene:P37840` and find nothing. Carl Laflamme:
            # "Can we add a target by simply indicating the Uniprot ID?"
            executor.submit(uniprot.lookup, gene): "uniprot",
            executor.submit(depmap.get_expression, gene): "depmap",
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.error("Feasibility lookup failed for %s (%s): %s", gene, key, e)
                results[key] = {"found": False, "error": str(e)}

    # **The local checks follow the symbol, not the string that was typed.**
    # DepMap, the proteomics table, the Horizon catalogue and this pipeline are
    # all keyed on gene symbol, so an accession has to be resolved before any of
    # them is asked — otherwise a lookup by `P37840` reports, accurately and
    # uselessly, that nothing called P37840 is on file.
    up = results.get("uniprot") or {}
    symbol = (up.get("gene_name") or "").strip() if up.get("found") else ""
    key = symbol or gene
    results["resolved_gene"] = symbol
    results["resolved_from"] = up.get("resolved_from", "gene")

    if symbol and symbol.upper() != gene.upper():
        # DepMap was fetched against whatever was typed, in parallel with
        # UniProt. If that was an accession, the answer is about the wrong key.
        results["depmap"] = depmap.get_expression(symbol)

    results["pipeline_status"] = _check_pipeline_db(key)
    results["proteomics"] = _check_proteomics(key)
    results["horizon_ko"] = horizon.lookup(key)

    # Compute summary (Stage 1 only — no antibody data yet)
    results["feasibility_summary"] = _compute_summary(results)

    # **Whether Add to Pipeline can be pressed at all, and why not.**
    # The page states the rule under the paste box below — *a gene UniProt could
    # not confirm is not added* — and offered a live button over a card reading
    # "No human protein found for gene 'ZZZZZZ'". The sentence comes from the
    # same place the bulk door's refusal does, so the two cannot drift, and the
    # button renders it rather than deciding for itself.
    results["add_refusal"] = bulk_targets.why_not_added(results.get("uniprot"),
                                                        symbol or gene)

    return JsonResponse(results)


@pipeline_member_required
@require_GET
def feasibility_antibodies(request):
    """
    AJAX endpoint — commercial antibody availability from the Antibody Registry
    (SciCrunch). Kept SEPARATE from Stage 1 so its slower (~10s) network call
    never delays the fast free lookup. Informational only — does not feed the
    traffic-light score. Degrades gracefully when the registry is unreachable or
    SCICRUNCH_API_KEY is unset.
    """
    from pipeline.services import scicrunch

    gene = request.GET.get("gene", "").strip()
    if not gene:
        return JsonResponse({"error": "Please enter a gene name"}, status=400)
    try:
        data = scicrunch.count_antibodies(gene)
    except Exception as e:  # never 500 the page over a registry hiccup
        logger.error("Registry antibody count failed for '%s': %s", gene, e)
        data = {"found": False, "error": f"Registry lookup failed: {e}"}
    return JsonResponse(data)


def _nominate(target, request, *, site_id=None, agency_id=None, project_id=None):
    """Give a newly added target a nomination — at the site that was chosen.

    Returns the site's name if one was recorded, "" otherwise, so the caller can
    say so. A target's site lives on a ``TargetNomination``, and this page used
    to create the ``Target`` alone: the result had no site, no funder and no
    project, so it was invisible to every board's site filter and the target
    board had no nomination to hang an allocation on.

    **The site is now the caller's to pass**, defaulting to the member's own.
    It was taken from the member unconditionally and reported afterwards, which
    is the same fact arriving too late to be a decision — and for anybody whose
    account is on the wrong site, indistinguishable from the app picking one.

    A funder and a project are optional and recorded when given. ``funded`` is
    derived from whether an agency was named rather than typed separately: a
    nomination with a grant behind it *is* the funded case, and asking twice is
    how the two end up disagreeing.
    """
    member = (Member.objects.using(DB)
              .filter(user__username=request.user.username).first())
    # **A pk that names no site is a stale page, not a value.** Written straight
    # into the FK it is a dangling reference: SQLite lets it through and
    # PostgreSQL refuses the INSERT, so on live the nomination would vanish into
    # the exception handler below and the page would report "No site was
    # recorded" — a target created with nobody shown as pursuing it. Fall back
    # to the member's own site, which is what the <select> defaults to anyway.
    if site_id and not Site.objects.using(DB).filter(pk=site_id).exists():
        logger.warning("feasibility add: unknown site_id %s, using the member's",
                       site_id)
        site_id = None
    site_id = site_id or getattr(member, "site_id", None)
    if not site_id:
        return ""
    try:
        TargetNomination.objects.using(DB).create(
            target_id=target.pk, site_id=site_id,
            granting_agency_id=agency_id or None,
            project_id=project_id or None,
            created_by_id=getattr(member, "pk", None),
            funded=bool(agency_id or project_id))
    except Exception:
        logger.exception("could not nominate target %s at site %s",
                         target.pk, site_id)
        return ""
    site = Site.objects.using(DB).filter(pk=site_id).first()
    return getattr(site, "name", "") or ""


@pipeline_member_required
@require_POST
def feasibility_add_target(request):
    """Create a new Target from feasibility data.

    **One check behind both doors.** This endpoint used to create whatever was
    posted to it — so a search for the deliberately fake `ZZZZZZ` drew an honest
    verdict card ("No human protein found for gene 'ZZZZZZ'"), a fully live Add
    to Pipeline button, and a junk gene on the consortium's master list if
    anybody pressed it. The paste box on the same page refused the same gene and
    printed the rule while doing it. The target board is what the whole
    consortium works from and this is the one screen where a mistyped symbol
    walks straight onto it.

    So the press asks ``bulk_targets.plan`` — the same preview the bulk door
    posts to — and writes what *it* resolved rather than what the page sent.
    Three things follow, and the third is a fix in its own right:

    * A gene UniProt cannot confirm is not created, in the bulk panel's own
      words. The button is greyed before the press too, but a preview is not a
      permission slip and a page can be minutes old.
    * The metadata written is the metadata the server just confirmed, so a stale
      card cannot file one gene's protein name under another's.
    * A **synonym resolves instead of duplicating**. Typing `PARK8` matched no
      `gene_name` and created a second row for a gene already on file as
      `LRRK2`; `plan` answers that from `Target.alternative_name` without a
      network call at all.
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    gene_name = body.get("gene_name", "").strip()
    if not gene_name:
        return JsonResponse({"error": "gene_name is required"}, status=400)

    # No `site=`: `plan` is being asked "is this gene real, and is it already on
    # file", which the site does not bear on — and an unknown site here is a
    # stale <select> rather than a typo, so `_nominate` falls back to the
    # member's own rather than refusing a press that has nothing wrong with it.
    #
    # Upper-cased the way `bulk_targets.parse` hands the other door its genes:
    # `_existing_by_synonym` compares against upper-case aliases, so a lowercase
    # `park8` would miss LRRK2 and be created as a second row for a gene already
    # on file — the exact duplication routing through `plan` is here to prevent.
    member = _member(request)
    preview = bulk_targets.plan([gene_name.upper()], member=member)
    if not preview.get("ok"):
        return JsonResponse({"error": preview.get("error") or "Could not check that gene"},
                            status=400)
    row = preview["rows"][0]

    refusal = bulk_targets.row_refusal(row)
    if refusal:
        return JsonResponse({"created": False, "error": refusal}, status=400)

    if row["target_id"]:
        # Already on the list — under this name, or under the canonical one for
        # a synonym, which is worth saying rather than reporting a gene the
        # reader did not type.
        on_file = gene_symbol.canonical(row.get("gene_name") or gene_name)
        # Compared upper-against-upper: `on_file` keeps `orf` lowercase, so
        # comparing it to `gene_name.upper()` directly would report `C9orf72`
        # as a synonym rename of the `C9ORF72` somebody typed.
        named = (f"'{gene_name}' is already on the list as {on_file}"
                 if on_file.upper() != gene_name.upper()
                 else f"Target '{gene_name}' already exists")
        return JsonResponse({
            "created": False,
            "message": f"{named} (ID: {row['target_id']})",
            "target_id": row["target_id"],
        })

    target = Target(
        # What UniProt confirmed just now, not what the page was carrying —
        # and cased by `gene_symbol.canonical` rather than `.upper()`, or this
        # third door stores UniProt's own `C9orf72` as `C9ORF72`.
        gene_name=gene_symbol.canonical(row.get("gene_name") or gene_name),
        protein_name=row.get("protein_name") or "",
        uniprot_id=row.get("uniprot_id") or "",
        theoretical_mass_kda=row.get("mass_kda"),
        alternative_name=row.get("synonyms") or "",
        status=Target.Status.NOT_STARTED,
    )

    depmap_tpm = body.get("depmap_tpm")
    if depmap_tpm is not None:
        try:
            target.depmap_expression = float(depmap_tpm)
        except (ValueError, TypeError):
            pass

    target.save(using=DB)
    # Chosen on the page, not taken from the account. `strict_id` is deliberately
    # not used: this is a pk from a <select> the page rendered, and an unknown one
    # is a stale page rather than a typo, so it falls back to the member's site
    # rather than refusing a write that has already created the target.
    site_id = _int_or_none(body.get("site_id"))
    agency_id = _int_or_none(body.get("granting_agency_id"))
    project_id = _int_or_none(body.get("project_id"))
    site_name = _nominate(target, request, site_id=site_id,
                          agency_id=agency_id, project_id=project_id)
    funded = bool(agency_id or project_id)

    return JsonResponse({
        "created": True,
        # The name it went in under. A synonym that UniProt resolved is stored
        # canonically, so echoing what was typed would name a row nothing is
        # filed against.
        "message": f"Target '{target.gene_name}' added to pipeline",
        # Say what else was written. One click did three things and mentioned one.
        "site": site_name,
        "funded": funded,
        "nominated_note": (
            f"Nominated to {site_name} and recorded as "
            f"{'funded' if funded else 'not funded'} — change either on the "
            f"target board."
            if site_name else
            "No site was recorded: your account has no site, so nobody is shown "
            "as pursuing this target yet. Set it on the people board."),
        "target_id": target.pk,
    })


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _member(request):
    return (Member.objects.using(DB)
            .filter(user__username=request.user.username, is_active=True).first())


def _genes_from(body):
    """`(genes, error_response)` for either shape a caller sends.

    The textarea sends one blob; a retry for the genes a deadline did not reach
    sends the list it got back. Both end up as an ordered, de-duplicated list.
    """
    raw = body.get("genes", "")
    genes = bulk_targets.parse(raw) if isinstance(raw, str) else [
        str(g).strip().upper() for g in (raw or []) if str(g).strip()]
    if not genes:
        return None, JsonResponse({"ok": False, "error": "No gene names provided"}, status=400)
    if len(genes) > bulk_targets.MAX_GENES:
        return None, JsonResponse(
            {"ok": False,
             "error": f"Maximum {bulk_targets.MAX_GENES} genes per batch "
                      f"({len(genes)} provided)"}, status=400)
    return genes, None


@pipeline_member_required
@require_POST
def feasibility_bulk_check(request):
    """Preview a pasted gene list. Writes nothing; bounded by a deadline.

    This is the step Bulk Add Targets never had. It is a separate request from
    the commit for a reason that is not tidiness: a gene the pipeline has never
    seen costs a UniProt call, and a hundred of them cannot be done inside one
    request without risking a gateway timeout **mid-write**. Genes the deadline
    does not reach come back ``unchecked``, and asking again with just those
    finishes the job — so the caller can show honest progress instead of a
    spinner over an unknown wait.

    POST ``{"genes": "GENE1, GENE2" | ["GENE1", …], "site": <pk|name>}``.

    ``site`` is whose list the press would add to. It belongs in the *check* and
    not only in the save: which genes are already on that site's list is the
    thing the preview counts, so a preview computed for one site and saved
    against another would be a forecast of a write that did not happen.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    genes, refusal = _genes_from(body)
    if refusal:
        return refusal
    return JsonResponse(bulk_targets.plan(genes, member=_member(request),
                                          site=body.get("site") or None))


@pipeline_member_required
@require_POST
def feasibility_bulk_add(request):
    """Create targets from a previewed gene list. **Makes no network call.**

    Two shapes, because two surfaces post here:

    * ``{"rows": [...]}`` — the rows ``feasibility_bulk_check`` returned. Nothing
      is looked up; the preview already did that, so this is pure database work
      and cannot time out half-written.
    * ``{"genes": "..."}`` — the target board's Add panel, whose own preview is
      deliberately offline (``tests_timeouts`` pins that a board makes no
      outbound call), so it has no UniProt data to hand back. The lookups happen
      here instead, under the same deadline, and anything the deadline did not
      reach is reported as not added rather than guessed at.

    A gene UniProt could not confirm is **not created** either way.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    member = _member(request)
    site = body.get("site") or None
    rows = body.get("rows")
    if rows is None:
        genes, refusal = _genes_from(body)
        if refusal:
            return refusal
        preview = bulk_targets.plan(genes, member=member, site=site)
        if not preview.get("ok"):
            return JsonResponse(preview, status=400)
        rows = preview["rows"]

    # One site, one funder, one project and one funded flag for the whole batch —
    # the owner's ask, because a paste is normally one bench's and one grant's
    # worth of genes and setting them per row afterwards is the work this box
    # exists to avoid. All four are optional; both doors post them.
    try:
        result = bulk_targets.apply(
            rows, member=member, site=site,
            agency=body.get("granting_agency") or None,
            project=body.get("project") or None,
            funded=bool(body.get("funded")))
    except ValueError as e:
        # A contradiction between the two — a project whose funder is not the
        # one chosen — is named rather than resolved by picking a side.
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    # The old response shape, kept because the target board's Add panel renders
    # it: `added` / `skipped` / `not_found` / `errors`. The names are wrong for
    # what this now does — `skipped` covered both "already on the list" and
    # "could not be confirmed", which are opposite outcomes — so the new keys are
    # alongside rather than instead.
    not_found = [r for r in rows if r.get("status") in
                 (bulk_targets.NOT_FOUND, bulk_targets.UNCHECKED)]
    result["added"] = result["created"]
    result["not_found"] = [{"gene": r["gene"], "error": r.get("note") or ""}
                           for r in not_found]

    # **A nomination is a record, so a row that gained one was not skipped.**
    # `apply` has always returned `nominated`; nothing rendered it, and every
    # on-file row was counted as "skipped" whether or not it had just been put on
    # your site's list. Run 8 pasted two of McGill's genes, read `2 added ·
    # 4 skipped`, and left Leicester nominations on LRRK2 and MAPT that no
    # cleanup search could reach — "skipped" is precisely the word that stops
    # somebody writing a record down. The preview had warned on both rows; only
    # the result summary disagreed with it.
    created_genes = {(c.get("gene") or "").strip().upper() for c in result["created"]}
    result["nominated_existing"] = [g for g in result["nominated"]
                                    if (g or "").strip().upper() not in created_genes]
    nominated_now = {(g or "").strip().upper() for g in result["nominated_existing"]}
    result["skipped"] = [
        r["gene"] for r in rows
        if r.get("status") in (bulk_targets.ON_FILE, bulk_targets.SYNONYM)
        and (r.get("gene") or "").strip().upper() not in nominated_now]
    # `result["site"]` comes from `apply`, which resolved it — not from the
    # member. Overwriting it here with the adder's own site is how a page that
    # let you choose would still have reported the choice it replaced.
    result["errors"] = []
    result["total_input"] = len(rows)
    return JsonResponse(result)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_pipeline_db(gene: str) -> dict:
    result = {
        "exists": False, "target_id": None, "target_status": "",
        "target_status_display": "", "antibody_count": 0,
        "cell_line_count": 0, "has_sessions": False,
    }
    try:
        target = (Target.objects.using(DB).filter(gene_name__iexact=gene.strip()).first()
                  or Target.objects.using(DB).filter(uniprot_id__iexact=gene.strip()).first()
                  or Target.objects.using(DB).filter(protein_name__icontains=gene.strip()).first())

        if target:
            result["exists"] = True
            result["target_id"] = target.pk
            result["target_status"] = target.status
            result["target_status_display"] = target.get_status_display()
            result["antibody_count"] = Antibody.objects.using(DB).filter(target=target).count()
            result["cell_line_count"] = CellLine.objects.using(DB).filter(target=target).count()
            result["has_sessions"] = target.sessions.using(DB).exists()
    except Exception as e:
        logger.error("Pipeline DB check failed for '%s': %s", gene, e)
    return result


def _check_proteomics(gene: str) -> dict:
    result = {"found": False, "cell_lines": [], "detected_count": 0, "total_lines_checked": 12}
    try:
        entries = (ProteomicsExpression.objects.using(DB)
                   .filter(gene_name__iexact=gene.strip())
                   .order_by('-protein_intensity'))
        if entries.exists():
            result["found"] = True
            result["detected_count"] = entries.count()
            result["cell_lines"] = [
                {"name": e.cell_line, "intensity": round(e.protein_intensity, 2),
                 "uniprot_id": e.uniprot_id}
                for e in entries
            ]
    except Exception as e:
        logger.error("Proteomics check failed for '%s': %s", gene, e)
    return result


def _compute_summary(results: dict) -> dict:
    """Traffic-light feasibility from Stage 1 data only."""
    reasons = []
    score = 0

    # UniProt
    up = results.get("uniprot", {}) or {}
    if up.get("found"):
        score += 1
        reasons.append("✓ Protein found in UniProt")
    elif up.get("error"):
        reasons.append(f"⚠ UniProt: {up['error']}")
    else:
        reasons.append("✗ Protein not found in UniProt")
        score -= 2

    # DepMap expression
    dm = results.get("depmap", {}) or {}
    if dm.get("found"):
        hap1 = dm.get("hap1_tpm")
        best = dm.get("best_line")
        expressing = dm.get("expressing_count", 0)
        total = dm.get("total_lines", 0)

        if hap1 is not None:
            if dm.get("above_threshold"):
                score += 2
                reasons.append(f"✓ Expressed in HAP1: {hap1} log₂(TPM+1) (≥2.5 threshold)")
            else:
                score -= 1
                reasons.append(f"⚠ Low expression in HAP1: {hap1} log₂(TPM+1) (<2.5 threshold)")
                if best and best.get("above_threshold"):
                    score += 1
                    reasons.append(f"✓ But expressed in {best['name']}: {best['tpm']} log₂(TPM+1) — alternative KO background")
        else:
            reasons.append("⚠ HAP1 not in cached data — check other cell lines")

        if expressing > 0:
            reasons.append(f"ℹ Expressed above threshold in {expressing}/{total} cached cell lines")
    elif dm.get("error"):
        reasons.append(f"⚠ DepMap: {dm['error']}")
    else:
        reasons.append("⚠ No DepMap expression data available")

    # Horizon HAP1 KO availability — a ready-made KO removes the biggest cost
    hz = results.get("horizon_ko", {}) or {}
    if hz.get("found"):
        score += 1
        n = hz.get("count", 0)
        reasons.append(f"✓ {n} commercial HAP1 KO clone{'s' if n != 1 else ''} available from Horizon — no need to make one")
    else:
        reasons.append("⚠ No commercial HAP1 KO clone listed by Horizon — a KO would need to be made")

    # Proteomics
    prot = results.get("proteomics", {}) or {}
    if prot.get("found"):
        reasons.append(f"✓ Protein detected by mass spec in {prot['detected_count']} cell line(s)")
    else:
        reasons.append("⚠ Protein not detected by mass spectrometry in cached lines")

    # Pipeline status
    pipe = results.get("pipeline_status", {}) or {}
    if pipe.get("exists"):
        reasons.append(f"ℹ Already in pipeline: {pipe.get('target_status_display', '')}")

    # Traffic light
    if score >= 4:
        signal = "green"
    elif score >= 1:
        signal = "amber"
    elif score <= -1:
        signal = "red"
    else:
        signal = "unknown"

    return {"signal": signal, "score": score, "reasons": reasons}