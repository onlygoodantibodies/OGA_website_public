"""
Suppliers' knockout cell-line lists for feasibility lookups — Abcam's and
Thermo Fisher's.

Answers: "Which knockout lines has this supplier made (or is making) for this
gene, in which parental lines — and does the parental line express the gene?"
The last half is what makes a knockout a usable control: a KO in a line where
DepMap puts the gene below the 2.5 log2(TPM+1) threshold has nothing to remove.
Both lists hold lines like that, so every row carries its parental line's
readouts rather than the card trusting the supplier's choice of line.

RNA is the readout the threshold is written for and the only one covering
every gene; the Sanger mass-spec readout (``ProteomicsExpression``) rides
beside it for context. Mass spec sees roughly the most abundant 5,000
proteins of a line, so **not detected is not absent** — it is drawn as "not
detected", never as a negative — and a line the dataset never measured
(HAP1, HEK293T) is "not measured", a third answer, not the same as either.

Information only — nothing here feeds the feasibility verdict (owner, 30 Oct
2026), and it is drawn in the pipeline only, never on a public surface; both
files are left out of the public snapshot (``tools/export_public.py``). The
rule is **not the public website and not the public repo without the owner's
permission** (owner, 23 Nov 2026) — sharing what the lists say about a set of
genes with a collaborator is fine, which is why the list screen can download.

Each list is a committed CSV read once per process. No table and no
migration: a new list is a new file and a deploy.
"""

import csv
import functools
import logging
import os
import re
from dataclasses import dataclass, field

from pipeline.services import depmap, gene_symbol

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


@dataclass(frozen=True)
class KoList:
    key: str
    supplier: str
    source_file: str
    source_date: str
    # The status that means a finished line, and the word the card counts it by.
    done_status: str
    done_word: str
    # Order the card lists a gene's lines in: finished first.
    status_order: dict = field(default_factory=dict)

    @property
    def csv_path(self) -> str:
        return os.path.join(DATA_DIR, self.source_file)


LISTS = {
    # Converted from Abcam's KO_cells_complete_or_in_progress_28SEP2026.xlsx via
    # the "KO_cells_summary_by_gene" workbook. The workbook's UniProt column is
    # deliberately not carried: it holds wrong-species accessions (SSB → P0AGE0,
    # an E. coli protein).
    "abcam": KoList(
        key="abcam", supplier="Abcam",
        source_file="abcam_ko_lines_2026_09_28.csv", source_date="28 Sep 2026",
        done_status="Pass", done_word="verified",
        status_order={"Pass": 0, "Unvalidated": 1, "In development": 2},
    ),
    # Converted from "TFS_CRISPR_KO_LIST - 2026.xlsx". The sheet names proteins
    # (ATX3, HER4, IKKA), so the gene is resolved from its UniProt accession
    # (rest.uniprot.org, 4 Nov 2026) and the sheet's own name kept beside it.
    # P19338 is NCL, not UniProt's "NUCLEOLIN"; P31645 is SLC6A4, whose alias
    # 5-HTT the sheet wrote as "HTT" — the accession, not huntingtin.
    "thermo": KoList(
        key="thermo", supplier="Thermo Fisher",
        source_file="thermofisher_ko_lines_2026.csv", source_date="2026",
        done_status="Completed", done_word="completed",
        status_order={
            "Completed": 0,
            "KO confirmed by NGS - Application in progress": 1,
            "KO confirmed by NGS -CRISPR Inconclusive": 2,
            "Failed in Application testing/Closed due to gene essentiality": 3,
        },
    ),
}


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


@functools.lru_cache(maxsize=None)
def _by_gene(list_key: str) -> dict:
    index = {}
    with open(LISTS[list_key].csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            index.setdefault(row["gene_name"].strip().upper(), []).append(row)
    return index


def rows_for(list_key: str, gene: str) -> list:
    """The list's own rows for one gene — no expression, no database.

    For a caller that has already fetched expression for many genes at once
    (`ko_screen`), where `lookup`'s per-gene queries would scale with the list.
    """
    return _by_gene(list_key).get((gene or "").strip().upper(), [])


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


def lookup(list_key: str, gene_name: str, expression: dict | None = None,
           proteomics: dict | None = None) -> dict:
    """
    One supplier's KO lines for a gene, each with the gene's DepMap RNA and
    mass-spec readout in its parental line.

    ``expression`` is ``depmap.get_expression``'s answer and ``proteomics``
    the feasibility view's ``_check_proteomics`` answer, if the caller already
    has them (the view does); otherwise each is fetched.

    Returns dict with keys:
        found (bool), gene (str), supplier (str), source_date (str),
        count (int), done_count (int), done_word (str),
        lines (list of the list's own columns, plus
               tpm (float|None), above_threshold (bool|None), in_depmap (bool),
               protein ('detected'|'not_detected'|'not_measured'|None),
               protein_intensity (float|None)),
        threshold (float), error (str|None)
    """
    ko_list = LISTS[list_key]
    gene = (gene_name or "").strip().upper()
    result = {
        "found": False,
        "gene": gene,
        "supplier": ko_list.supplier,
        "source_date": ko_list.source_date,
        "count": 0,
        "done_count": 0,
        "done_word": ko_list.done_word,
        "lines": [],
        "threshold": depmap.EXPRESSION_THRESHOLD,
        "error": None,
    }
    if not gene:
        result["error"] = "No gene name provided"
        return result

    try:
        rows = _by_gene(list_key).get(gene, [])
    except Exception as e:  # a missing file must not take the page down
        logger.error("%s KO list unreadable: %s", ko_list.supplier, e)
        result["error"] = f"The {ko_list.supplier} knockout list could not be read."
        return result
    if not rows:
        return result

    if expression is None:
        try:
            expression = depmap.get_expression(gene)
        except Exception as e:
            logger.error("DepMap lookup failed for %s KO card (%s): %s", ko_list.supplier, gene, e)
            expression = {}
    tpm_by_line = {
        _line_key(cl.get("name")): cl.get("tpm")
        for cl in (expression or {}).get("cell_lines") or []
    }

    try:
        detected, measured = _protein_readout(gene, proteomics)
    except Exception as e:  # context only — never takes the card down
        logger.error("Proteomics lookup failed for %s KO card (%s): %s", ko_list.supplier, gene, e)
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
        line = {k: v for k, v in row.items() if k != "gene_name"}
        line.update({
            "tpm": tpm,
            "in_depmap": tpm is not None,
            "above_threshold": (tpm >= depmap.EXPRESSION_THRESHOLD) if tpm is not None else None,
            "protein": protein,
            "protein_intensity": (detected or {}).get(key),
        })
        lines.append(line)
    lines.sort(key=lambda ln: (
        ko_list.status_order.get(ln["status"], 9),
        -(ln["tpm"] if ln["tpm"] is not None else -99),
        ln["parental_line"], ln.get("line_name", ""),
    ))

    result["lines"] = lines
    result["count"] = len(lines)
    result["done_count"] = sum(1 for ln in lines if ln["status"] == ko_list.done_status)
    result["found"] = True
    return result
