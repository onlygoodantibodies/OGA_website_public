"""Feasibility assessment for a gene — the "is this target worth doing?" check.

Wraps Stage 1 of the feasibility view (UniProt + DepMap expression + proteomics +
pipeline-DB status + a traffic-light summary) as a reusable, request-free service
so MCP Server B can run it. The pure scoring helpers already live in
``views/feasibility.py`` and are reused here (not duplicated).

Stage 2 (the paid Claude-API recombinant-antibody web search) is opt-in via
``include_antibody_search`` and only runs when configured — it degrades
gracefully otherwise, as does UniProt/DepMap when outbound network is blocked in
dev.

Adding the gene as a target is a *separate* step — use the existing
``resolve_or_create_target`` (dedup-safe, UniProt-enriched). This service is the
"assess" half; that tool is the "add" half.
"""
from __future__ import annotations

DB = "pipeline_db"


def assess(gene: str, *, include_antibody_search: bool = False) -> dict:
    from pipeline.services import antibody_search as ab_search_service
    from pipeline.services import depmap, uniprot, horizon, scicrunch
    from pipeline.views.feasibility import (
        _check_pipeline_db, _check_proteomics, _compute_summary,
    )

    gene = (gene or "").strip()
    if not gene:
        return {"ok": False, "error": "gene is required"}

    results = {"gene_query": gene}
    try:
        results["uniprot"] = uniprot.lookup_gene(gene)
    except Exception as e:  # network often blocked in dev — never fatal
        results["uniprot"] = {"found": False, "error": str(e)}
    try:
        results["depmap"] = depmap.get_expression(gene)
    except Exception as e:
        results["depmap"] = {"found": False, "error": str(e)}
    try:
        # Commercial antibody availability from the Antibody Registry (SciCrunch).
        results["antibody_registry"] = scicrunch.count_antibodies(gene)
    except Exception as e:
        results["antibody_registry"] = {"found": False, "error": str(e)}

    results["pipeline_status"] = _check_pipeline_db(gene)
    results["proteomics"] = _check_proteomics(gene)
    try:
        results["horizon_ko"] = horizon.lookup(gene)
    except Exception as e:
        results["horizon_ko"] = {"found": False, "error": str(e)}
    results["feasibility_summary"] = _compute_summary(results)
    results["antibody_search_available"] = ab_search_service.is_configured()

    if include_antibody_search and ab_search_service.is_configured():
        try:
            results["antibody_search"] = ab_search_service.search_recombinant_antibodies(gene)
        except Exception as e:
            results["antibody_search"] = {"error": str(e)}

    results["ok"] = True
    return results
