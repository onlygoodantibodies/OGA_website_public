"""
Horizon HAP1 knockout catalogue service for feasibility lookups.

Answers one question: "Can I just order a ready-made HAP1 KO for this gene?"
Backed by the local ``HorizonKoLine`` cache (loaded via ``import_horizon_ko``),
so it never touches the network — mirrors the DepMap service pattern.
"""

import logging

from pipeline.models import HorizonKoLine

logger = logging.getLogger(__name__)

DB = "pipeline_db"


def lookup(gene_name: str) -> dict:
    """
    Look up commercially available Horizon HAP1 KO clones for a gene.

    Returns dict with keys:
        found (bool)         — any clones available,
        gene (str)           — the queried gene (upper-cased),
        supplier (str)       — 'Horizon Discovery',
        background (str)     — 'HAP1',
        count (int)          — number of distinct clones,
        clones (list)        — [{item_number, product_name}],
        error (str|None)
    """
    result = {
        "found": False,
        "gene": (gene_name or "").strip().upper(),
        "supplier": "Horizon Discovery",
        "background": "HAP1",
        "count": 0,
        "clones": [],
        "error": None,
    }

    if not result["gene"]:
        result["error"] = "No gene name provided"
        return result

    try:
        lines = (
            HorizonKoLine.objects.using(DB)
            .filter(gene_name__iexact=result["gene"])
            .order_by("item_number")
        )
        result["count"] = lines.count()
        result["found"] = result["count"] > 0
        result["clones"] = [
            {"item_number": ln.item_number, "product_name": ln.product_name}
            for ln in lines
        ]
    except Exception as e:  # table may not be populated yet — never fatal
        logger.error("Horizon KO lookup failed for '%s': %s", result["gene"], e)
        result["error"] = str(e)

    return result


def is_cache_populated() -> dict:
    """Report whether the Horizon catalogue has been loaded (for UI status)."""
    try:
        total = HorizonKoLine.objects.using(DB).count()
        genes = (HorizonKoLine.objects.using(DB)
                 .values("gene_name").distinct().count())
        return {"populated": total > 0, "clone_count": total, "gene_count": genes}
    except Exception:
        return {"populated": False, "clone_count": 0, "gene_count": 0}
