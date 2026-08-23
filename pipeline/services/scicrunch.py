"""
SciCrunch / Antibody Registry API service for antibody counts.

Scoping doc §5.1 / Vision doc Phase 1:
  Are enough commercial antibodies available? Ideally 10-15, with at least 3 renewable
  (recombinant). Check the Antibody Registry via SciCrunch API for RRID-registered antibodies.

API: Elasticsearch endpoint on SciCrunch
API key provided by Anita Bandrowski, SciCrunch.
"""

import logging
import os
import re

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

SCICRUNCH_URL = "https://api.scicrunch.io/elastic/v1/RIN_Antibody_pr/_search"
# Set SCICRUNCH_API_KEY in Render. Empty fallback disables only the RRID lookup
# feature; it does not crash the app.
API_KEY = os.environ.get("SCICRUNCH_API_KEY", "")
TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# Catalogue -> RRID lookup (Antibody Registry), per the API usage documented by
# Anita Bandrowski (SciCrunch), 2026-03:
#
#   Search by catalog number (exact):
#     GET .../_search?q=vendors.catalogNumber:"ab4674"&size=25&apikey=<KEY>
#   Search by RRID:
#     GET .../_search?q=rrid.curie:"RRID:AB_304558"&size=25&apikey=<KEY>
#
# NOTE the two things the legacy ``count_antibodies`` above gets wrong (which is
# why it never returned data): the auth query param is ``apikey`` (not ``key``),
# and the supported form is a simple ``q=`` GET, not a POST Elasticsearch DSL
# body. This function follows the documented form. The exact ``_source`` shape is
# parsed defensively (see ``_parse_hit``) because it has not been verified from
# this environment — run the caller's dry-run with --show-raw in Render first.
# ---------------------------------------------------------------------------

def lookup_by_catalogue(catalogue: str, size: int = 25) -> dict:
    """Look up Antibody Registry records for an exact catalogue number.

    Returns dict:
        found (bool),
        records (list of {rrid (bare AB_<n> or ""), vendors (list of
            {vendor, catalogue, url}), source (raw _source)}),
        error (str|None)
    """
    result = {"found": False, "records": [], "error": None}
    catalogue = (catalogue or "").strip()
    if not catalogue:
        result["error"] = "No catalogue number provided"
        return result
    if not API_KEY:
        result["error"] = "SCICRUNCH_API_KEY is not set"
        return result

    try:
        response = requests.get(
            SCICRUNCH_URL,
            params={"q": f'vendors.catalogNumber:"{catalogue}"',
                    "size": size, "apikey": API_KEY},
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    except RequestException as e:
        logger.error("SciCrunch catalogue lookup failed for '%s': %s", catalogue, e)
        result["error"] = f"SciCrunch API unavailable: {str(e)}"
        return result
    except ValueError as e:
        logger.error("SciCrunch catalogue parse error for '%s': %s", catalogue, e)
        result["error"] = f"Error parsing SciCrunch response: {str(e)}"
        return result

    for hit in data.get("hits", {}).get("hits", []):
        result["records"].append(_parse_hit(hit.get("_source", {}) or {}))
    result["found"] = bool(result["records"])
    if not result["found"]:
        result["error"] = f"No registry record for catalogue '{catalogue}'"
    return result


# --- defensive _source parsing (schema not yet verified from this env) --------

def _first(d, *keys):
    """First truthy value among the given keys of a dict."""
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return ""


def _extract_curie(source: dict) -> str:
    """Bare AB_<n> for a hit.

    Confirmed from live registry data (2026-07): the reliable location is
    ``item.identifier`` (e.g. "AB_3698202"); ``item.curie`` is "ab:3698202"
    (wrong form) so it is NOT used. ``rrid.curie`` ("RRID:AB_...") and
    ``item.alternateIdentifiers[].identifier`` are fallbacks.
    """
    from pipeline.rrid_utils import normalize_rrid
    item = source.get("item") if isinstance(source.get("item"), dict) else {}
    candidates = [item.get("identifier")]
    for alt in (item.get("alternateIdentifiers") or []):
        if isinstance(alt, dict):
            candidates.append(alt.get("identifier"))
    rrid = source.get("rrid")
    if isinstance(rrid, dict):
        candidates.append(rrid.get("curie"))
    candidates.append(source.get("identifier"))
    for c in candidates:
        bare = normalize_rrid(c if isinstance(c, str) else "")
        if bare:
            return bare
    return ""


def _extract_target_genes(source: dict) -> list:
    """Target gene symbols for a hit, from ``antibodies.primary[].targets[].name``
    (confirmed from live data, e.g. "YWHAG"). Used as an identity cross-check."""
    genes = []
    prim = (source.get("antibodies") or {}).get("primary") or []
    for a in prim:
        if not isinstance(a, dict):
            continue
        for t in (a.get("targets") or []):
            name = (t.get("name") or "").strip() if isinstance(t, dict) else ""
            if name:
                genes.append(name)
    return genes


def _clean_url(u) -> str:
    """Return u only if it is a real http(s) URL. The registry's ``link`` field is
    often the literal placeholder ``"no"`` (seen live 2026-07), which must never be
    written as a product URL."""
    u = (u or "").strip() if isinstance(u, str) else ""
    return u if u.lower().startswith(("http://", "https://")) else ""


def _extract_vendors(source: dict) -> list:
    """List of {vendor, catalogue, url}. ``vendors[].catalogNumber`` is the field
    the registry search matches on (Anita Bandrowski's documented query); the
    vendor-name and url subfield names are tried defensively. A non-URL ``link``
    (e.g. the placeholder "no") is dropped to "" by ``_clean_url``."""
    vendors = source.get("vendors")
    if not vendors and isinstance(source.get("item"), dict):
        vendors = source["item"].get("vendors")
    if isinstance(vendors, dict):
        vendors = [vendors]
    out = []
    for v in (vendors or []):
        if not isinstance(v, dict):
            continue
        out.append({
            "vendor": _first(v, "vendor", "vendorName", "name", "organization", "supplier"),
            "catalogue": _first(v, "catalogNumber", "catalog_number", "cat_num", "catalogueNumber"),
            "url": _clean_url(_first(v, "url", "vendorUrl", "link", "uri")),
        })
    return out


def _parse_hit(source: dict) -> dict:
    return {"rrid": _extract_curie(source),
            "vendors": _extract_vendors(source),
            "genes": _extract_target_genes(source),
            "source": source}


def _extract_clonality(source: dict) -> str:
    """Clonality for a hit, from ``antibodies.primary[].clonality.name`` (values
    seen live: "unknown", "polyclonal", "monoclonal", "recombinant"). Lowercased."""
    prim = (source.get("antibodies") or {}).get("primary") or []
    for a in prim:
        if isinstance(a, dict):
            name = ((a.get("clonality") or {}).get("name") or "").strip().lower()
            if name:
                return name
    return ""


# ---------------------------------------------------------------------------
# Antibody availability count for a gene (feasibility). Uses the SAME working
# API form as lookup_by_catalogue (GET, q=, apikey) and tallies client-side from
# the returned records, keeping only those whose registered target IS the gene.
# ---------------------------------------------------------------------------

def _gene_in_hit(hit: dict, gene_u: str) -> bool:
    genes = [g.strip().upper() for g in _extract_target_genes(hit.get("_source", {}) or {}) if g]
    return gene_u in genes


def _gene_hits(gene: str, size: int):
    """Return (hits_list, error_or_None) for a gene's registry antibodies.

    A bare ``q=<gene>`` full-text search is far too broad (APOE returns ~2000 hits
    on unrelated proteins — TRAIL, Fas, anything containing "APO"), so query the
    target field first and fall back to the vanilla search only if the
    field-scoped form errors or yields nothing on-target. The caller re-filters
    client-side either way, so a loose fallback never inflates the count.
    """
    gene_u = gene.upper()
    queries = ['antibodies.primary.targets.name:"%s"' % gene, gene]
    last_err, hits = None, []
    for q in queries:
        try:
            resp = requests.get(
                SCICRUNCH_URL,
                params={"q": q, "size": size, "apikey": API_KEY},
                timeout=TIMEOUT_SECONDS,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", []) or []
        except (RequestException, ValueError) as e:
            logger.error("SciCrunch count query %r failed for '%s': %s", q, gene, e)
            last_err = f"SciCrunch API unavailable: {e}"
            continue
        if any(_gene_in_hit(h, gene_u) for h in hits):
            return hits, None      # precise on-target hits — use them
    if last_err and not hits:
        return None, last_err
    return hits, None              # no on-target hits (caller → total 0)


def count_antibodies(gene_name: str, size: int = 200) -> dict:
    """Count commercial antibodies registered against a gene.

    Returns dict:
        found (bool), total_count, recombinant_count, monoclonal_count,
        polyclonal_count, top_vendors (list of {vendor, count}), error (str|None).

    ``total_count`` counts registry records whose registered target IS
    ``gene_name`` (a bare text search on a gene like APOE returns ~2000 unrelated
    hits — TRAIL, Fas, anything containing "APO" — so we query the target field
    and also re-filter client-side). Capped at ``size``.
    """
    from collections import Counter

    result = {"found": False, "total_count": 0, "recombinant_count": 0,
              "monoclonal_count": 0, "polyclonal_count": 0, "top_vendors": [],
              "error": None}
    gene = (gene_name or "").strip()
    if not gene:
        result["error"] = "No gene name provided"
        return result
    if not API_KEY:
        result["error"] = "SCICRUNCH_API_KEY is not set"
        return result

    hits, err = _gene_hits(gene, size)
    if err:
        result["error"] = err
        return result

    gene_u = gene.upper()
    vendors = Counter()
    for hit in hits:
        source = hit.get("_source", {}) or {}
        genes = [g.strip().upper() for g in _extract_target_genes(source) if g]
        if gene_u not in genes:
            continue  # a text hit whose registered target is a different protein
        result["total_count"] += 1
        clon = _extract_clonality(source)
        if "recombinant" in clon:
            result["recombinant_count"] += 1
        elif "monoclonal" in clon:
            result["monoclonal_count"] += 1
        elif "polyclonal" in clon:
            result["polyclonal_count"] += 1
        seen = set()
        for v in _extract_vendors(source):
            name = (v.get("vendor") or "").strip()
            if name and name not in seen:
                seen.add(name)
                vendors[name] += 1

    result["found"] = result["total_count"] > 0
    result["top_vendors"] = [{"vendor": n, "count": c} for n, c in vendors.most_common(10)]
    if not result["found"]:
        result["error"] = f"No registry antibodies found for '{gene}'"
    return result


# ---------------------------------------------------------------------------
# Catalogue -> RRID resolution (shared by the backfill command AND antibody
# entry). Strictly gated on target gene + vendor; see the backfill command's
# docstring for the rationale. Returns a HIGH-confidence RRID or None.
# ---------------------------------------------------------------------------

VENDOR_SIMILARITY_MIN = 80   # rapidfuzz token_set_ratio threshold for "same vendor"


def _norm_cat(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).upper()


def _loose_cat(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _cat_match(ncat: str, lcat: str, other: str) -> bool:
    if not other or not lcat:
        return False
    return (ncat and _norm_cat(other) == ncat) or _loose_cat(other) == lcat


def _vendor_similar(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    try:
        from rapidfuzz import fuzz
        return fuzz.token_set_ratio(a, b) >= VENDOR_SIMILARITY_MIN
    except Exception:
        return False


def match_registry(catalogue: str, company: str, gene: str, records: list):
    """Match one antibody (catalogue + company + gene) against registry records.

    HIGH only when the catalogue matches and at least one of {target gene, vendor}
    agrees while neither contradicts. The gene check rejects a catalogue that
    resolves to a different antibody.

    Returns (status, rrid, url, note): status in {"high","review","nomatch"}.
    """
    from pipeline.rrid_utils import normalize_rrid
    ncat, lcat = _norm_cat(catalogue), _loose_cat(catalogue)
    if not lcat:
        return ("nomatch", "", "", "")
    gene_u = (gene or "").strip().upper()

    cand = {}
    for rec in records:
        rrid = normalize_rrid(rec.get("rrid"))
        if not rrid:
            continue
        vendors = rec.get("vendors") or []
        rec_cats = [v.get("catalogue") for v in vendors if v.get("catalogue")]
        if rec_cats and not any(_cat_match(ncat, lcat, c) for c in rec_cats):
            continue
        rec_genes = [g.strip().upper() for g in (rec.get("genes") or []) if g]
        gene_ok = bool(gene_u) and gene_u in rec_genes
        gene_bad = bool(gene_u) and bool(rec_genes) and not gene_ok
        vendor_ok = any(_vendor_similar(company, v.get("vendor")) for v in vendors)
        url = ""
        for v in vendors:
            if (not rec_cats or _cat_match(ncat, lcat, v.get("catalogue"))) and v.get("url"):
                url = v["url"]
                break
        e = cand.setdefault(rrid, {"url": "", "gene_ok": False, "gene_bad": False,
                                   "vendor_ok": False})
        e["url"] = e["url"] or url
        e["gene_ok"] = e["gene_ok"] or gene_ok
        e["gene_bad"] = e["gene_bad"] or gene_bad
        e["vendor_ok"] = e["vendor_ok"] or vendor_ok

    if not cand:
        return ("nomatch", "", "", "")
    strong = {r: e for r, e in cand.items()
              if not e["gene_bad"] and (e["gene_ok"] or e["vendor_ok"])}
    if len(strong) == 1:
        r, e = next(iter(strong.items()))
        conf = "+".join([s for s, ok in (("gene", e["gene_ok"]), ("vendor", e["vendor_ok"])) if ok])
        return ("high", r, e["url"], f"confirmed by {conf}")
    if len(strong) > 1:
        return ("review", "", "", f"multiple confirmed RRIDs {sorted(strong)}")
    if any(e["gene_bad"] for e in cand.values()):
        return ("review", "", "", f"catalogue matched but registry target gene differs (ours={gene})")
    return ("review", "", "", f"catalogue matched RRID(s) {sorted(cand)} but could not confirm "
                              f"vendor/gene (ours vendor={company!r})")


def resolve_rrid(catalogue: str, company: str, gene: str):
    """Best-effort HIGH-confidence RRID for one antibody. Returns
    (rrid_or_None, url, note). Never raises — network/parse failures return
    (None, "", reason), so callers at entry time can degrade gracefully."""
    catalogue = (catalogue or "").strip()
    if not catalogue:
        return (None, "", "no catalogue")
    try:
        reg = lookup_by_catalogue(catalogue)
    except Exception as e:
        return (None, "", f"lookup failed: {e}")
    if not reg.get("records"):
        return (None, "", reg.get("error") or "no registry record")
    status, rrid, url, note = match_registry(catalogue, company, gene, reg["records"])
    return (rrid, url, note) if status == "high" else (None, url, note or status)
