"""
UniProt REST API service for target metadata autofill.

Scoping doc §5.1 / Vision doc Phase 1:
  Type a gene name → auto-populate protein name, UniProt ID, molecular weight, gene synonyms.

API docs: https://www.uniprot.org/help/api
"""

import logging
import re

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

UNIPROT_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"
UNIPROT_ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{accession}.json"
TIMEOUT_SECONDS = 10


# A UniProt accession, as UniProt itself defines the format. Two shapes, and
# both are worth matching because the old-style six-character ones (P37840) are
# what most papers cite while the newer ten-character ones (A0A0B4J2F0) are what
# a modern entry page shows.
_ACCESSION = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$",
    re.IGNORECASE)


def looks_like_accession(text: str) -> bool:
    """True when a typed string is a UniProt accession rather than a gene symbol.

    Carl Laflamme asked: *"Can we add a target by simply indicating the Uniprot
    ID?"* — and the answer was no, because the one search box only ever called
    ``lookup_gene``, which searches ``gene:P37840`` and finds nothing.

    The two namespaces do not collide in practice: an accession always has a
    digit in the second position, and no HGNC symbol does.
    """
    return bool(_ACCESSION.match((text or "").strip()))


def lookup(text: str) -> dict:
    """Look a gene up by symbol **or** by accession, whichever was typed.

    Returns the same shape either way, plus ``resolved_from``, so a page can say
    *"P37840 → SNCA"* rather than silently answering about a different string
    from the one somebody entered.
    """
    text = (text or "").strip()
    if looks_like_accession(text):
        out = lookup_accession(text)
        out["resolved_from"] = "accession"
        return out
    out = lookup_gene(text)
    out["resolved_from"] = "gene"
    return out


def lookup_accession(accession: str) -> dict:
    """
    Fetch ONE UniProt entry by its exact accession (e.g. "P02649") and return
    structured metadata for target gap-fill.

    Preferred over ``lookup_gene`` when the accession is already known (every
    pipeline Target stores it): it hits the entry endpoint directly, so there is
    no search / best-match ambiguity.

    Returns dict with keys:
        found (bool), unavailable (bool), uniprot_id, protein_name,
        alternative_names (list[str]), gene_synonyms (list[str]),
        mass_kda (float|None), subcellular_location, error (str|None)

    ``unavailable`` means the request failed, not that the accession is wrong —
    see ``lookup_gene`` for why those must not be one flag.
    """
    result = {
        "found": False,
        "unavailable": False,
        "uniprot_id": "",
        "protein_name": "",
        # **The symbol, which this returned for nobody.** `lookup` promises the
        # same shape whichever way in you took, and every local check downstream
        # — DepMap, proteomics, Horizon, the pipeline itself — is keyed on gene
        # symbol, so `feasibility_lookup`'s "follow the symbol, not the string
        # that was typed" simply never fired for an accession: it read
        # `gene_name`, found nothing, and asked all four about `P37840`. Adding
        # by accession then created a Target whose *gene name* was P37840. The
        # field was already being requested from the API and thrown away.
        "gene_name": "",
        "alternative_names": [],
        "gene_synonyms": [],
        "mass_kda": None,
        "subcellular_location": "",
        "error": None,
    }

    accession = (accession or "").strip()
    if not accession:
        result["error"] = "No accession provided"
        return result

    try:
        response = requests.get(
            UNIPROT_ENTRY_URL.format(accession=accession),
            params={"fields": ("accession,protein_name,gene_names,gene_synonym,"
                               "mass,cc_subcellular_location,sequence")},
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        entry = response.json()
    except RequestException as e:
        logger.error("UniProt entry request failed for '%s': %s", accession, e)
        result["unavailable"] = True
        result["error"] = f"UniProt API unavailable: {str(e)}"
        return result
    except ValueError as e:
        logger.error("UniProt entry parse error for '%s': %s", accession, e)
        result["unavailable"] = True
        result["error"] = f"Error parsing UniProt response: {str(e)}"
        return result

    result["uniprot_id"] = entry.get("primaryAccession", "") or accession

    protein_desc = entry.get("proteinDescription", {}) or {}
    rec_name = protein_desc.get("recommendedName", {}) or {}
    if rec_name:
        result["protein_name"] = rec_name.get("fullName", {}).get("value", "")
    else:
        sub_names = protein_desc.get("submissionNames", []) or []
        if sub_names:
            result["protein_name"] = sub_names[0].get("fullName", {}).get("value", "")

    # Alternative PROTEIN names (maps to Target.alternative_name).
    alt_names = []
    for alt in protein_desc.get("alternativeNames", []) or []:
        val = (alt.get("fullName", {}) or {}).get("value", "")
        if val:
            alt_names.append(val)
    result["alternative_names"] = alt_names

    # The primary gene symbol, and its synonyms (the latter map to
    # Target.aliases — "alternative gene names/symbols").
    genes = entry.get("genes", []) or []
    if genes:
        result["gene_name"] = (genes[0].get("geneName", {}) or {}).get("value", "")
        synonyms = genes[0].get("synonyms", []) or []
        result["gene_synonyms"] = [s.get("value", "") for s in synonyms if s.get("value")]

    # Mass — Da → kDa.
    mass_da = (entry.get("sequence", {}) or {}).get("molWeight")
    if mass_da:
        result["mass_kda"] = round(mass_da / 1000, 2)

    # Subcellular location (reference only; NOT auto-mapped to protein_type).
    for comment in entry.get("comments", []) or []:
        if comment.get("commentType") == "SUBCELLULAR LOCATION":
            loc_names = []
            for loc in comment.get("subcellularLocations", []) or []:
                v = (loc.get("location", {}) or {}).get("value")
                if v:
                    loc_names.append(v)
            result["subcellular_location"] = "; ".join(loc_names[:5])

    result["found"] = True
    return result


def lookup_gene(gene_name: str) -> dict:
    """
    Search UniProt for a human gene and return structured metadata.

    Returns dict with keys:
        found (bool), unavailable (bool), uniprot_id, protein_name, gene_name,
        gene_synonyms, mass_kda, function_summary, subcellular_location,
        error (str|None)

    **`found=False` is two different answers and callers must be able to tell
    them apart.** "UniProt says there is no such gene" is permanent and the
    reader's to fix; "UniProt could not be reached" is transient and nobody's
    fault. Collapsed into one flag, the bulk-add preview reported a blocked
    proxy as *"TRPA1 not in UniProt — not added, check the spelling"* about a
    perfectly real gene, and sent the reader to correct a spelling that was
    already right. ``unavailable`` is the one that means try again.

    ``error`` carries the exception text and is for the **log**, not a screen:
    on the unreachable path it is a proxy URL and an ``OSError``.
    """
    result = {
        "found": False,
        "unavailable": False,
        "uniprot_id": "",
        "protein_name": "",
        "gene_name": "",
        "gene_synonyms": [],
        "mass_kda": None,
        "function_summary": "",
        "subcellular_location": "",
        "error": None,
    }

    if not gene_name or not gene_name.strip():
        result["error"] = "No gene name provided"
        return result

    gene_name = gene_name.strip().upper()

    try:
        params = {
            "query": f"gene:{gene_name} AND organism_id:9606",
            "format": "json",
            "size": 5,
            "fields": (
                "accession,protein_name,gene_names,gene_primary,gene_synonym,"
                "mass,cc_function,cc_subcellular_location,sequence"
            ),
        }

        response = requests.get(
            UNIPROT_SEARCH_URL,
            params=params,
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            result["error"] = f"No human protein found for gene '{gene_name}'"
            return result

        # Pick the best match: prefer reviewed (Swiss-Prot) over unreviewed (TrEMBL),
        # and prefer exact gene name match.
        best = None
        for entry in results:
            entry_type = entry.get("entryType", "")
            is_reviewed = "Swiss-Prot" in entry_type

            # Check if primary gene name matches exactly
            genes = entry.get("genes", [])
            primary_gene = ""
            if genes:
                primary_gene = genes[0].get("geneName", {}).get("value", "")

            is_exact = primary_gene.upper() == gene_name

            if best is None:
                best = entry
                best_reviewed = is_reviewed
                best_exact = is_exact
            elif is_exact and not best_exact:
                best = entry
                best_reviewed = is_reviewed
                best_exact = is_exact
            elif is_reviewed and not best_reviewed and (is_exact == best_exact):
                best = entry
                best_reviewed = is_reviewed
                best_exact = is_exact

        entry = best

        # Extract accession
        result["uniprot_id"] = entry.get("primaryAccession", "")

        # Extract protein name
        protein_desc = entry.get("proteinDescription", {})
        rec_name = protein_desc.get("recommendedName", {})
        if rec_name:
            result["protein_name"] = rec_name.get("fullName", {}).get("value", "")
        else:
            sub_names = protein_desc.get("submissionNames", [])
            if sub_names:
                result["protein_name"] = sub_names[0].get("fullName", {}).get("value", "")

        # Extract gene names
        genes = entry.get("genes", [])
        if genes:
            gene_info = genes[0]
            result["gene_name"] = gene_info.get("geneName", {}).get("value", "")
            synonyms = gene_info.get("synonyms", [])
            result["gene_synonyms"] = [s.get("value", "") for s in synonyms if s.get("value")]

        # Extract mass — convert from Da to kDa
        sequence_info = entry.get("sequence", {})
        mass_da = sequence_info.get("molWeight")
        if mass_da:
            result["mass_kda"] = round(mass_da / 1000, 2)

        # Extract function summary
        comments = entry.get("comments", [])
        for comment in comments:
            if comment.get("commentType") == "FUNCTION":
                texts = comment.get("texts", [])
                if texts:
                    result["function_summary"] = texts[0].get("value", "")
            elif comment.get("commentType") == "SUBCELLULAR LOCATION":
                locations = comment.get("subcellularLocations", [])
                loc_names = []
                for loc in locations:
                    location = loc.get("location", {})
                    if location.get("value"):
                        loc_names.append(location["value"])
                result["subcellular_location"] = "; ".join(loc_names[:5])

        result["found"] = True
        return result

    except RequestException as e:
        logger.error("UniProt API request failed for '%s': %s", gene_name, e)
        result["unavailable"] = True
        result["error"] = f"UniProt API unavailable: {str(e)}"
        return result
    except (KeyError, IndexError, ValueError) as e:
        # An answer we could not read is not an answer. Same class as the one
        # above from a caller's point of view: nothing was learned about this
        # gene, so asking again is the right advice.
        logger.error("UniProt response parsing error for '%s': %s", gene_name, e)
        result["unavailable"] = True
        result["error"] = f"Error parsing UniProt response: {str(e)}"
        return result
