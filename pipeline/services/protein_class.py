"""
Protein classes for a target — the grant-writing question Carl's spreadsheet
cannot answer.

He reviews the whole target list while writing grants "to mention some kind of
targets that we've done already — GPCR, secreted proteins, nuclear proteins,
endosomal proteins, neurological proteins — showing some know-how for certain
protein classes". There is no column for that in the master sheet; it is done
from memory. This derives it.

Three sources, kept apart so they can be re-derived independently
(``TargetClassification.source``):

  * **uniprot** — UniProt keywords (curated, Swiss-Prot; the good stuff).
  * **go**      — GO term names, as a fallback where keywords are thin.
  * **family**  — the gene-symbol prefix (RAB, TRIM, SLC…), computed offline.

Disease area ("neurological") is deliberately NOT derived here: it is a property
of the grant, not the protein, and the portfolio view reads it off the granting
agency / project instead.

Network calls degrade gracefully — a UniProt outage yields the offline family
label and nothing else, never an exception.
"""
from __future__ import annotations

import logging
import re

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

UNIPROT_ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{accession}.json"
TIMEOUT_SECONDS = 10

# Ordered, curated rubric. Each label matches if any needle appears in a UniProt
# keyword, a GO term name, or a subcellular-location string (all lower-cased).
# Order matters only for readability — every match is recorded, targets are
# routinely several classes at once (a GPCR is also a membrane protein).
CLASS_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("GPCR",                ("g-protein coupled receptor", "g protein-coupled receptor")),
    ("Receptor",            ("receptor",)),
    ("Kinase",              ("kinase",)),
    ("Phosphatase",         ("phosphatase",)),
    ("Protease",            ("protease", "peptidase", "hydrolase")),
    ("Ubiquitin pathway",   ("ubl conjugation pathway", "ubiquitin", "e3 ")),
    ("Transcription factor", ("dna-binding", "transcription regulation",
                              "transcription factor")),
    ("Ion channel",         ("ion channel", "voltage-gated channel", "ionic channel")),
    ("Transporter",         ("transport", "symport", "antiport")),
    ("Small GTPase",        ("gtpase", "gtp-binding")),
    ("Chaperone",           ("chaperone",)),
    ("Cytoskeletal",        ("cytoskeleton", "actin-binding", "microtubule")),
    ("Secreted",            ("secreted",)),
    ("Membrane",            ("membrane", "transmembrane")),
    ("Nuclear",             ("nucleus", "nuclear")),
    ("Mitochondrial",       ("mitochondrion", "mitochondrial")),
    ("Endosomal",           ("endosome", "endosomal")),
    ("Lysosomal",           ("lysosome", "lysosomal")),
    ("Golgi",               ("golgi",)),
    ("Endoplasmic reticulum", ("endoplasmic reticulum",)),
    ("Synaptic",            ("synapse", "synaptic", "postsynaptic", "presynaptic")),
    ("Glycoprotein",        ("glycoprotein",)),
]

# Prefixes that are an artefact of the naming scheme rather than a real family.
_FAMILY_STOPLIST = {"LOC", "ORF", "MIR", "LINC"}


def gene_family(gene: str) -> str:
    """The gene-symbol family prefix, or "" if the symbol has no useful one.

    Deterministic and offline — this is what powers the "a Rab is being nominated
    at Leicester, but McGill has done most Rabs" warning, so it must work without
    a network round-trip at nomination time.

        RAB11A → RAB     SLC17A7 → SLC     TRIM33 → TRIM     DYRK1A → DYRK
        TP53   → ""      SNCA    → ""      CFAP410 → CFAP

    Symbols with no digit have no family here (SNCA, MAPT); a two-letter prefix
    is rejected as too noisy to be worth warning about (TP53 → "TP").
    """
    g = (gene or "").strip().upper()
    m = re.match(r"^([A-Z]{3,})\d", g)
    if not m:
        return ""
    prefix = m.group(1)
    return "" if prefix in _FAMILY_STOPLIST else prefix


def _labels_from_terms(terms, source: str) -> list[dict]:
    """Match a bag of raw strings against CLASS_RULES → label/evidence dicts."""
    out, seen = [], set()
    lowered = [(t, t.lower()) for t in terms if t]
    for label, needles in CLASS_RULES:
        if label in seen:
            continue
        for raw, low in lowered:
            if any(n in low for n in needles):
                out.append({"label": label, "source": source, "evidence": raw[:255]})
                seen.add(label)
                break
    return out


def fetch_terms(accession: str) -> dict:
    """UniProt keywords + GO term names + subcellular locations for an accession.

    Returns ``{"found": bool, "keywords": [...], "go_terms": [...],
    "locations": [...], "error": str|None}``. Never raises.
    """
    result = {"found": False, "keywords": [], "go_terms": [], "locations": [],
              "error": None}
    accession = (accession or "").strip()
    if not accession:
        result["error"] = "No accession provided"
        return result
    try:
        response = requests.get(
            UNIPROT_ENTRY_URL.format(accession=accession),
            params={"fields": "accession,keyword,go,cc_subcellular_location"},
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        entry = response.json()
    except RequestException as e:
        logger.warning("UniProt class lookup failed for '%s': %s", accession, e)
        result["error"] = f"UniProt unavailable: {e}"
        return result
    except ValueError as e:
        result["error"] = f"Could not parse UniProt response: {e}"
        return result

    result["keywords"] = [
        k.get("name", "") for k in (entry.get("keywords") or []) if k.get("name")
    ]
    for xref in entry.get("uniProtKBCrossReferences") or []:
        if xref.get("database") != "GO":
            continue
        for prop in xref.get("properties") or []:
            if prop.get("key") == "GoTerm" and prop.get("value"):
                # GO terms arrive prefixed, e.g. "C:endosome membrane".
                result["go_terms"].append(prop["value"].split(":", 1)[-1])
    for comment in entry.get("comments") or []:
        if comment.get("commentType") == "SUBCELLULAR LOCATION":
            for loc in comment.get("subcellularLocations") or []:
                v = (loc.get("location") or {}).get("value")
                if v:
                    result["locations"].append(v)
    result["found"] = True
    return result


def classify(gene: str, accession: str = "", *, terms: dict | None = None) -> list[dict]:
    """Derive class labels for one target.

    ``terms`` lets a caller supply an already-fetched ``fetch_terms`` result (the
    backfill command reuses one call per target). With no accession and no terms
    this still returns the offline gene-family label, so nomination-time checks
    work with the network down.
    """
    labels: list[dict] = []

    family = gene_family(gene)
    if family:
        labels.append({"label": f"{family} family", "source": "family",
                       "evidence": f"gene symbol prefix of {gene}"})

    if terms is None and accession:
        terms = fetch_terms(accession)
    terms = terms or {}
    if not terms.get("found"):
        return labels

    labels += _labels_from_terms(terms.get("keywords") or [], "uniprot")
    derived = {d["label"] for d in labels}
    # GO only fills gaps UniProt keywords left — same label from two sources is
    # noise, and the unique constraint is per (target, label, source).
    labels += [d for d in _labels_from_terms(
        (terms.get("go_terms") or []) + (terms.get("locations") or []), "go")
        if d["label"] not in derived]
    return labels


def sync_target(target, *, db: str = "pipeline_db", terms: dict | None = None) -> dict:
    """Write derived classifications for a target, leaving manual tags alone.

    Idempotent: derived rows for the sources it owns are replaced, ``manual``
    rows are never touched. Returns a small summary for the backfill command.
    """
    from pipeline.models import TargetClassification

    labels = classify(target.gene_name or "", target.uniprot_id or "", terms=terms)
    owned = [s for s in ("uniprot", "go", "family")]
    existing = {
        (c.label, c.source): c
        for c in TargetClassification.objects.using(db)
        .filter(target_id=target.pk, source__in=owned)
    }
    wanted = {(d["label"], d["source"]): d for d in labels}

    created = 0
    for key, d in wanted.items():
        if key not in existing:
            TargetClassification.objects.using(db).create(
                target_id=target.pk, label=d["label"], source=d["source"],
                evidence=d["evidence"])
            created += 1
    stale = [c.pk for key, c in existing.items() if key not in wanted]
    if stale:
        TargetClassification.objects.using(db).filter(pk__in=stale).delete()
    return {"gene": target.gene_name, "created": created, "removed": len(stale),
            "labels": sorted({d["label"] for d in labels})}
