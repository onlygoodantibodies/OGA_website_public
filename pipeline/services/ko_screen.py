"""Which knockout lines exist for a list of genes — a screen that adds nothing.

Asked for by uOttawa (23 Nov 2026): a collaborator sent a 239-gene wish
list and wanted to know which genes have KO lines. The only door that
took a list was *Bulk Add Targets*, which answers a different question — is
this a real gene, and may it go on a site's list — and whose second button
would have put every gene on Ontario's list. The single-gene search answered
the right question one gene at a time. This is that answer for a list.

Four sources, in the order a bench would reach for them:

* a KO or KD line a consortium site already holds (``CellLine``, via
  ``cell_lines.CONTROL_GENOTYPES``);
* Horizon's HAP1 catalogue (``HorizonKoLine``);
* Abcam's and Thermo Fisher's KO lists (``supplier_ko.LISTS``) — never on the
  public website or in the public repo without the owner's permission, though
  a member may share a screen's result with a collaborator (owner, 23 Nov
  2026);
* DepMap expression, so each supplier line is read against its parental line
  (a KO in a line that does not express the gene removes nothing), and so a
  gene with no line anywhere says where one could be made.

**Writes nothing and makes no outbound call.** Every source is local, so there
is no UniProt step and no deadline; a symbol none of the sources know is
reported as unknown here, not as "not a gene" — this screen does not ask
UniProt, and saying "check the spelling" about a real gene is the failure
``bulk_targets`` was rewritten to stop.

**Every query is batched**: the cost is one query per table, never one per
gene (pinned in ``tests_ko_screen``), because a list is exactly the input on
which a per-gene loop looks fine in dev and stalls the database live.
"""
from __future__ import annotations

import re

from pipeline.models import CellLine, DepMapExpression, HorizonKoLine, Target
from pipeline.services import cell_lines, depmap, example_row, gene_symbol, supplier_ko
from pipeline.services import targets as target_svc

DB = "pipeline_db"

# Generous because nothing here touches the network: the bound is on the page
# drawing the table, not on the work.
MAX_GENES = 500

_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_BRACKETED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9.-]*)\s*\((.*)\)\s*$")

# Routes, cheapest first. The number leads so a sorted download keeps the order.
HELD = "1. A consortium site holds a KO/KD line"
BUY_EXPRESSED = "2. A supplier KO line exists in a line that expresses it"
BUY_OTHER = "3. A supplier KO line exists, but not finished or not in an expressing line"
MAKE_HAP1 = "4. No KO line; HAP1 expresses it"
MAKE_OTHER = "5. No KO line; another panel line expresses it"
NOT_EXPRESSED = "6. No KO line; not expressed in any panel line"
UNKNOWN = "Not in OGA's expression data or any KO list"
ROUTES = (HELD, BUY_EXPRESSED, BUY_OTHER, MAKE_HAP1, MAKE_OTHER, NOT_EXPRESSED, UNKNOWN)


def parse(raw: str):
    """`(entries, unreadable)` from a pasted list.

    One entry per symbol, first-seen order: ``{"written", "gene", "note"}``.
    ``PRKN (PARK2)`` is read as PRKN and **says so** — the bracket is a second
    name for the same gene, which is how a collaborator's sheet wrote them. A cell
    naming several genes (``PRKCA/B/G``) is not expanded: which genes ``B`` and
    ``G`` are is a guess, so it is listed back to be written one per line.
    """
    entries, unreadable, seen = [], [], set()
    for token in re.split(r"[,;\n\r\t]+", example_row.drop(raw or "")):
        written = token.strip()
        if not written:
            continue
        note = ""
        m = _BRACKETED.match(written)
        if m:
            symbol = m.group(1)
            note = f"read as {gene_symbol.canonical(symbol)}; ({m.group(2).strip()}) ignored"
        elif _SYMBOL.match(written):
            symbol = written
        else:
            if written not in unreadable:
                unreadable.append(written)
            continue
        gene = gene_symbol.canonical(symbol)
        if target_svc.is_not_applicable(gene) or gene in seen:
            continue
        seen.add(gene)
        entries.append({"written": written, "gene": gene, "note": note})
    return entries, unreadable


def _key(gene):
    return (gene or "").strip().upper()


def screen(genes, db=DB) -> dict:
    """One row per gene, in the order given. Read-only, local, batched."""
    genes = [g for g in genes if g]
    keys = [_key(g) for g in genes]
    spellings = sorted({s for g in genes for s in gene_symbol.spellings(g)})
    threshold = depmap.EXPRESSION_THRESHOLD

    # DepMap, folded the way the single-gene card folds it (renames, subclones
    # dropped) so a supplier's parental line matches by `supplier_ko._line_key`.
    expression = {}
    for e in (DepMapExpression.objects.using(db)
              .filter(gene_name__in=spellings)
              .values("gene_name", "cell_line", "tpm_log2")):
        if e["cell_line"] in depmap.CELL_LINE_EXCLUDE:
            continue
        name = depmap.CELL_LINE_RENAME.get(e["cell_line"], e["cell_line"])
        expression.setdefault(_key(e["gene_name"]), {})[supplier_ko._line_key(name)] = (
            name, round(e["tpm_log2"], 2))

    horizon = {}
    for h in (HorizonKoLine.objects.using(db).filter(gene_name__in=spellings)
              .order_by("item_number").values("gene_name", "item_number")):
        horizon.setdefault(_key(h["gene_name"]), []).append(h["item_number"])

    targets = {}
    for t in Target.objects.using(db).filter(gene_name__in=spellings).only("pk", "gene_name"):
        targets[_key(t.gene_name)] = t
    held = {}
    if targets:
        by_pk = {t.pk: k for k, t in targets.items()}
        for line in (CellLine.objects.using(db)
                     .filter(target_id__in=list(by_pk),
                             genotype__in=cell_lines.CONTROL_GENOTYPES)
                     .select_related("site", "target")):
            held.setdefault(by_pk[line.target_id], []).append(
                f"{cell_lines.label(line)} ({line.genotype})")

    # **A list that cannot be read is named, never read as "no lines".** The
    # files are left out of the public repo (`tools/export_public.py`), and a
    # missing file read as empty would tell somebody no supplier had made a
    # knockout of anything — the silent omission that reads as success.
    unreadable_lists = []
    for list_key, ko_list in supplier_ko.LISTS.items():
        try:
            supplier_ko.rows_for(list_key, "")
        except OSError:
            unreadable_lists.append(list_key)

    rows = []
    for gene, key in zip(genes, keys):
        expr = expression.get(key, {})
        hap1 = expr.get("HAP1", (None, None))[1]
        expressing = sorted((v for v in expr.values() if v[1] >= threshold),
                            key=lambda v: -v[1])

        supplier = {}
        any_line = bool(horizon.get(key))
        buy_expressed = bool(horizon.get(key)) and hap1 is not None and hap1 >= threshold
        for list_key, ko_list in supplier_ko.LISTS.items():
            lines = []
            if list_key in unreadable_lists:
                supplier[list_key] = lines
                continue
            for r in supplier_ko.rows_for(list_key, key):
                name, tpm = expr.get(supplier_ko._line_key(r["parental_line"]), (None, None))
                finished = r["status"] == ko_list.done_status
                expresses = tpm is not None and tpm >= threshold
                any_line = True
                buy_expressed = buy_expressed or (finished and expresses)
                lines.append({"parental_line": r["parental_line"], "status": r["status"],
                              "finished": finished, "tpm": tpm, "expresses": expresses})
            lines.sort(key=lambda ln: (not ln["finished"], not ln["expresses"],
                                       ln["parental_line"]))
            supplier[list_key] = lines

        if held.get(key):
            route = HELD
        elif buy_expressed:
            route = BUY_EXPRESSED
        elif any_line:
            route = BUY_OTHER
        elif not expr:
            route = UNKNOWN
        elif hap1 is not None and hap1 >= threshold:
            route = MAKE_HAP1
        elif expressing:
            route = MAKE_OTHER
        else:
            route = NOT_EXPRESSED

        target = targets.get(key)
        rows.append({
            "gene": gene,
            "route": route,
            "target_id": getattr(target, "pk", None),
            "held": sorted(held.get(key, [])),
            "horizon": horizon.get(key, []),
            "abcam": supplier.get("abcam", []),
            "thermo": supplier.get("thermo", []),
            "hap1_tpm": hap1,
            "expressing": [f"{n} {t}" for n, t in expressing[:3]],
            "expressing_count": len(expressing),
            "panel_size": len(expr),
        })

    counts = {r: 0 for r in ROUTES}
    for row in rows:
        counts[row["route"]] += 1
    return {
        "rows": rows,
        "summary": [{"route": r, "count": n} for r, n in counts.items() if n],
        "threshold": threshold,
        "sources": {k: f"{v.supplier}, {v.source_date}" for k, v in supplier_ko.LISTS.items()},
        "unreadable_lists": [supplier_ko.LISTS[k].supplier for k in unreadable_lists],
    }
