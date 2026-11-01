"""
Abcam knockout cell-line list for feasibility lookups.

Answers: "Which knockout lines has Abcam made (or is making) for this gene, in
which parental lines — and does the parental line express the gene?" The last
half is what makes a knockout a usable control: a KO in a line where DepMap
puts the gene below the 2.5 log2(TPM+1) threshold has nothing to remove.

RNA is the readout the threshold is written for and the only one covering
every gene; the Sanger mass-spec readout (``ProteomicsExpression``) rides
beside it for context. Mass spec sees roughly the most abundant 5,000
proteins of a line, so **not detected is not absent** — it is drawn as "not
detected", never as a negative — and a line the dataset never measured
(HAP1, HEK293T) is "not measured", a third answer, not the same as either.

Information only — nothing here feeds the feasibility verdict (owner, 30 Oct
2026), and it is drawn in the pipeline only, never on a public surface.

Backed by a committed CSV (``pipeline/data/abcam_ko_lines_2026_09_28.csv``,
converted from Abcam's ``KO_cells_complete_or_in_progress_28SEP2026.xlsx`` via
the "KO_cells_summary_by_gene" workbook), read once per process. No table and
no migration: a new list is a new file and a deploy. The workbook's UniProt
column is deliberately not carried — it holds wrong-species accessions (SSB →
P0AGE0, an E. coli protein).
"""

import csv
import functools
import logging
import os
import re

from pipeline.services import depmap, gene_symbol

logger = logging.getLogger(__name__)

SOURCE_FILE = "abcam_ko_lines_2026_09_28.csv"
SOURCE_DATE = "28 Sep 2026"
CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", SOURCE_FILE,
)

# Order the card lists a gene's lines in: verified first.
STATUS_ORDER = {"Pass": 0, "Unvalidated": 1, "In development": 2}


def _line_key(name: str) -> str:
    """A cell-line name folded for matching across sources.

    Abcam writes ``U-87 MG``, ``U-2 OS``, ``A-431``; DepMap's cache (after
    ``depmap.CELL_LINE_RENAME``) writes ``U87MG``, ``U2OS``, ``A431``. Only
    punctuation and case differ, so only those are dropped — ``HEK293`` and
    ``HEK293T`` stay two lines.
    """
    key = re.sub(r"[^A-Z0-9]", "", (name or "").upper())
    return LINE_ALIASES.get(key, key)


# Spellings one source uses for a line another spells differently, after
# punctuation and case are dropped. The proteomics data keeps DepMap's
# 'LNCaP clone FGC' where Abcam writes 'LnCaP'.
LINE_ALIASES = {"LNCAPCLONEFGC": "LNCAP"}


@functools.lru_cache(maxsize=1)
def _by_gene() -> dict:
    index = {}
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            index.setdefault(row["gene_name"].strip().upper(), []).append(row)
    return index


def _protein_readout(gene: str, proteomics: dict | None):
    """``(intensity by line key, line keys the dataset measured at all)``."""
    from pipeline.models import ProteomicsExpression
    measured = {
        _line_key(n) for n in ProteomicsExpression.objects.using(depmap.DB)
        .values_list("cell_line", flat=True).distinct()
    }
    if proteomics is None:
        proteomics = {"cell_lines": [
            {"name": e.cell_line, "intensity": round(e.protein_intensity, 2)}
            for e in ProteomicsExpression.objects.using(depmap.DB)
            .filter(gene_name__in=gene_symbol.spellings(gene))
        ]}
    detected = {
        _line_key(cl.get("name")): cl.get("intensity")
        for cl in (proteomics or {}).get("cell_lines") or []
    }
    return detected, measured


def lookup(gene_name: str, expression: dict | None = None,
           proteomics: dict | None = None) -> dict:
    """
    Abcam KO lines for a gene, each with the gene's DepMap expression in its
    parental line.

    ``expression`` is ``depmap.get_expression``'s answer and ``proteomics``
    the feasibility view's ``_check_proteomics`` answer, if the caller already
    has them (the view does); otherwise each is fetched.

    Returns dict with keys:
        found (bool), gene (str), supplier ('Abcam'), source_date (str),
        count (int), pass_count (int),
        lines (list of {line_name, parental_line, status, catalogue, note,
                        tpm (float|None), above_threshold (bool|None),
                        in_depmap (bool),
                        protein ('detected'|'not_detected'|'not_measured'|None),
                        protein_intensity (float|None)}),
        threshold (float), error (str|None)
    """
    gene = (gene_name or "").strip().upper()
    result = {
        "found": False,
        "gene": gene,
        "supplier": "Abcam",
        "source_date": SOURCE_DATE,
        "count": 0,
        "pass_count": 0,
        "lines": [],
        "threshold": depmap.EXPRESSION_THRESHOLD,
        "error": None,
    }
    if not gene:
        result["error"] = "No gene name provided"
        return result

    try:
        rows = _by_gene().get(gene, [])
    except Exception as e:  # a missing file must not take the page down
        logger.error("Abcam KO list unreadable: %s", e)
        result["error"] = "The Abcam knockout list could not be read."
        return result
    if not rows:
        return result

    if expression is None:
        try:
            expression = depmap.get_expression(gene)
        except Exception as e:
            logger.error("DepMap lookup failed for Abcam KO card (%s): %s", gene, e)
            expression = {}
    tpm_by_line = {
        _line_key(cl.get("name")): cl.get("tpm")
        for cl in (expression or {}).get("cell_lines") or []
    }

    try:
        detected, measured = _protein_readout(gene, proteomics)
    except Exception as e:  # context only — never takes the card down
        logger.error("Proteomics lookup failed for Abcam KO card (%s): %s", gene, e)
        detected, measured = None, set()

    lines = []
    for row in rows:
        key = _line_key(row["parental_line"])
        tpm = tpm_by_line.get(key)
        if detected is None:
            protein = None
        elif key in detected:
            protein = "detected"
        elif key in measured:
            protein = "not_detected"
        else:
            protein = "not_measured"
        lines.append({
            "line_name": row["line_name"],
            "parental_line": row["parental_line"],
            "status": row["status"],
            "catalogue": row["catalogue"],
            "note": row["note"],
            "tpm": tpm,
            "in_depmap": tpm is not None,
            "above_threshold": (tpm >= depmap.EXPRESSION_THRESHOLD) if tpm is not None else None,
            "protein": protein,
            "protein_intensity": (detected or {}).get(key),
        })
    lines.sort(key=lambda ln: (
        STATUS_ORDER.get(ln["status"], 9),
        -(ln["tpm"] if ln["tpm"] is not None else -99),
        ln["parental_line"], ln["line_name"],
    ))

    result["lines"] = lines
    result["count"] = len(lines)
    result["pass_count"] = sum(1 for ln in lines if ln["status"] == "Pass")
    result["found"] = True
    return result
