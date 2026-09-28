"""Give the knockdown studies a knockdown cell line, from the Access marks.

    python manage.py backfill_knockdown_lines             # dry run
    python manage.py backfill_knockdown_lines --gene RAB27A
    python manage.py backfill_knockdown_lines --apply

The Access database recorded a knockdown blot as a tick (or a comment) with
the lane pointing at the plain wild type, and the import wrote exactly that:
the session's *control* slot points at a wild type, and the gene's page lists
no knockdown line at all — RAB27A shows one HAP1 knockout while its public
page, since 18 Sep 2026, says knockdown-controlled. This puts the row the
page is missing on file, and it is the same evidence `backfill_control_
genotype` reads, joined the other way: `WbResult`/`IpResult`/`IfResult`
carry the Access row's id, so each pipeline result row can be asked whether
its Access row was a knockdown.

What it does, per knockdown session:

* **One knockdown line per gene, background and site**, named after the
  wild type in the lane (`U-87 MG`), genotype `KD`, parented to that wild
  type, method `siRNA` where a comment or the gene's `CommentsCustomKO` says
  so and blank otherwise. Supplier, catalogue number and sequence stay blank:
  Access never held them, and a value nobody recorded is not filled in.
  A matching line already on file is reused, never duplicated.
* **The session's control slot is pointed at it** — only where every one
  of the session's Access-linked rows is a knockdown and the slot is empty
  or holds a wild type (the import's mistake). A slot already holding a
  knockout is a conflict and is reported, not overwritten.
* **A session with knockdown and knockout rows mixed** — the original
  import made one session per gene and procedure, so RAB27A's WB session
  holds both its 2023 knockout blots and its 2024 knockdown blots — gets
  the line but keeps its slot, and is listed: splitting it is a curation
  call, not a backfill.

Dry-run by default and reported in full first; production-data rules
apply.
"""
from __future__ import annotations

import csv
import os
import re
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import (CellLine, CellLineVial, ExperimentSession, IfResult,
                             IpResult, Target, WbResult)
from pipeline.services import cell_lines as cell_line_svc

DB = "pipeline_db"
DEFAULT_DIR = "access_csvs"
_KD_WORDS = re.compile(r"\bKD\b|knock\s*-?\s*down|si\s*-?\s*rna|sh\s*-?\s*rna", re.I)
_SIRNA = re.compile(r"si\s*-?\s*rna", re.I)

# Access table → the result model whose `access_id` is that table's `ID`, and
# the columns that name the cell lines in its lanes or wells.
TABLES = (
    ("Wb.csv", WbResult, ("lane1CellLineID", "lane2CellLineID",
                          "lane3CellLineID", "lane4CellLineID")),
    ("IP.csv", IpResult, ("CellLineID",)),
    ("IF.csv", IfResult, ("CellLine1ID", "CellLine2ID")),
)


def read_marks(directory: str) -> tuple[dict, set]:
    """`({(model, access_id): mark}, {genes with siRNA stock})`, where a mark is
    `{"kd", "sirna", "wt_lanes": [access cell-line ids], "ko_lanes": [...]}`.

    **The background is the wild type in the row's own lanes**, never the
    session's wild-type slot: the original import made one session per gene
    and procedure and filled that slot from the first row it met, so for a
    gene knocked out first and knocked down later the slot names the
    knockout's parental — RAB27A's HAP1, where the knockdown was in U-87 MG.
    The first dry run on live proposed `RAB27A HAP1 KD` for exactly that
    reason. And a row marked knockdown whose lanes name a knockout line is
    the knockout experiment with a stray tick (DNM1, DDX1, PLEC, RAB5B, two
    PRDX1 wells): it is reported as contradictory and makes no line.
    """
    lines = {}
    path = os.path.join(directory, "CellLines.csv")
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                lines[(row.get("ID") or "").strip()] = (row.get("WTorKO") or "").strip().upper()
    marks = {}
    for filename, model, lane_cols in TABLES:
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    rid = int((row.get("ID") or "").strip())
                except ValueError:
                    continue
                comment = row.get("Comments") or ""
                kd = ((row.get("KnockDown") or "").strip() == "1"
                      or bool(_KD_WORDS.search(comment)))
                wt_lanes, ko_lanes = [], []
                for col in lane_cols:
                    cid = (row.get(col) or "").strip()
                    if not cid or cid == "0" or cid not in lines:
                        continue
                    (wt_lanes if lines[cid] == "WT" else ko_lanes).append(cid)
                marks[(model, rid)] = {"kd": kd, "sirna": bool(_SIRNA.search(comment)),
                                       "wt_lanes": wt_lanes, "ko_lanes": ko_lanes}
    sirna_genes = set()
    path = os.path.join(directory, "Proteins.csv")
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if _SIRNA.search(row.get("CommentsCustomKO") or ""):
                    sirna_genes.add((row.get("Gene") or "").strip().upper())
    return marks, sirna_genes


def _line_for_access(cache: dict, access_id: str):
    """The live `CellLine` an Access CellLines id means. `CellLineVial.access_id`
    is the reliable lookup (one Access row is a freeze-down batch); the line's
    own `access_id` is the fallback."""
    if access_id in cache:
        return cache[access_id]
    try:
        n = int(float(access_id))
    except ValueError:
        cache[access_id] = None
        return None
    vial = CellLineVial.objects.using(DB).select_related("cell_line").filter(access_id=n).first()
    cl = vial.cell_line if vial else CellLine.objects.using(DB).filter(access_id=n).first()
    cache[access_id] = cl
    return cl


class Command(BaseCommand):
    help = ("Create knockdown cell lines for the sessions Access marked as "
            "knockdowns, and point those sessions' control slot at them. "
            "Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=DEFAULT_DIR, metavar="DIR")
        parser.add_argument("--gene", nargs="+", metavar="GENE")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        marks, sirna_genes = read_marks(options["dir"])
        if not marks:
            raise CommandError(f"no Access tables found under {options['dir']}/")

        sessions = (ExperimentSession.objects.using(DB)
                    .select_related("target", "site", "cell_line_wt", "cell_line_ko")
                    .order_by("target__gene_name", "procedure_type", "date", "pk"))
        if options["gene"]:
            genes = [g.strip() for g in options["gene"] if g.strip()]
            found = set(Target.objects.using(DB).filter(gene_name__in=genes)
                        .values_list("gene_name", flat=True))
            missing = sorted(set(genes) - found)
            if missing:
                raise CommandError(f"no target called: {', '.join(missing)}")
            sessions = sessions.filter(target__gene_name__in=genes)

        # Per session: every Access-linked row, and for each knockdown row the
        # wild type in its own lanes.
        per_session = defaultdict(lambda: {"linked": 0, "kd": [], "contradictory": 0, "sirna": False})
        for _f, model, _cols in TABLES:
            rows = (model.objects.using(DB).exclude(access_id__isnull=True)
                    .values_list("session_id", "access_id"))
            for session_id, access_id in rows:
                mark = marks.get((model, access_id))
                if mark is None:
                    continue
                t = per_session[session_id]
                t["linked"] += 1
                if not mark["kd"]:
                    continue
                if mark["ko_lanes"]:
                    t["contradictory"] += 1
                    continue
                t["kd"].append(mark["wt_lanes"])
                t["sirna"] = t["sirna"] or mark["sirna"]

        cache = {}
        lines_to_make = {}      # (target_id, background name lower, site_id) -> spec
        repoint, mixed, no_wt, conflict, contradictory, two_backgrounds = [], [], [], [], [], []
        for s in sessions:
            t = per_session.get(s.pk)
            if t is None:
                continue
            if t["contradictory"] and not t["kd"]:
                contradictory.append((s, t["contradictory"]))
                continue
            if not t["kd"]:
                continue
            gene = (s.target.gene_name or "").strip()
            backgrounds = {}
            for wt_lanes in t["kd"]:
                for cid in wt_lanes:
                    wt = _line_for_access(cache, cid)
                    if wt is not None and (wt.genotype or "").upper() == "WT":
                        backgrounds[wt.pk] = wt
            if not backgrounds:
                no_wt.append(s)
                continue
            keys = []
            for wt in backgrounds.values():
                key = (s.target_id, (wt.name or "").strip().lower(), s.site_id)
                spec = lines_to_make.get(key)
                if spec is None:
                    existing = (CellLine.objects.using(DB)
                                .filter(name__iexact=wt.name, genotype="KD",
                                        target_id=s.target_id, site_id=s.site_id).first())
                    spec = {"name": wt.name, "wt": wt, "target": s.target,
                            "site_id": s.site_id, "existing": existing,
                            "method": ("siRNA" if (t["sirna"] or gene.upper() in sirna_genes) else ""),
                            "sessions": []}
                    lines_to_make[key] = spec
                spec["sessions"].append(s.pk)
                keys.append(key)
            if len(t["kd"]) + t["contradictory"] < t["linked"]:
                mixed.append((s, len(t["kd"]), t["linked"]))
                continue
            if len(keys) > 1:
                two_backgrounds.append((s, [lines_to_make[k]["name"] for k in keys]))
                continue
            ko = s.cell_line_ko
            if ko is not None and cell_line_svc.is_control(ko):
                conflict.append((s, ko))
                continue
            repoint.append((s, keys[0]))

        def gene_of(s):
            return s.target.gene_name if s.target_id else "?"
        def line(s):
            return (f"    #{s.pk:<6} {gene_of(s):10s} {s.procedure_type:6s} "
                    f"{s.date.isoformat() if s.date else '':10s}")

        self.stdout.write(f"{len(lines_to_make)} knockdown line(s) needed "
                          f"({sum(1 for v in lines_to_make.values() if v['existing'])} already on file)")
        for spec in lines_to_make.values():
            what = "on file" if spec["existing"] else "would be created"
            self.stdout.write(f"    {spec['target'].gene_name:10s} {spec['name']} KD"
                              f"{' · ' + spec['method'] if spec['method'] else ''}"
                              f"  ← parent {cell_line_svc.label(spec['wt'])}  ({what})")
        self.stdout.write(f"{len(repoint)} session(s) would have their control slot set to it")
        for s, _k in repoint:
            was = f"was {cell_line_svc.label(s.cell_line_ko)}" if s.cell_line_ko_id else "was empty"
            self.stdout.write(f"{line(s)} {was}")
        if mixed:
            self.stdout.write(self.style.WARNING(
                f"{len(mixed)} session(s) mix knockdown and other rows — the line is "
                f"created, the slot is left alone; split the session on the board"))
            for s, kd, linked in mixed:
                self.stdout.write(f"{line(s)} {kd} of {linked} rows are knockdowns")
        if conflict:
            self.stdout.write(self.style.WARNING(
                f"{len(conflict)} session(s) already name a knockout as their control — left alone"))
            for s, ko in conflict:
                self.stdout.write(f"{line(s)} control is {cell_line_svc.label(ko)}")
        if two_backgrounds:
            self.stdout.write(self.style.WARNING(
                f"{len(two_backgrounds)} session(s) knock down in more than one "
                f"background — lines created, slot left alone"))
            for s, names in two_backgrounds:
                self.stdout.write(f"{line(s)} {', '.join(names)}")
        if contradictory:
            self.stdout.write(self.style.WARNING(
                f"{len(contradictory)} session(s) whose only knockdown-marked rows "
                f"name a knockout line in their lanes — a stray tick on a knockout "
                f"experiment; no line made"))
            for s, n in contradictory:
                self.stdout.write(f"{line(s)} {n} such row(s)")
        if no_wt:
            self.stdout.write(self.style.WARNING(
                f"{len(no_wt)} knockdown session(s) whose rows name no wild type the "
                f"app can find, so there is no background to make the line from"))
            for s in no_wt:
                self.stdout.write(line(s))

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing was changed. Re-run with --apply to write."))
            return

        created, pointed = 0, 0
        with transaction.atomic(using=DB):
            made = {}
            for key, spec in lines_to_make.items():
                cl = spec["existing"]
                if cl is None:
                    cl = CellLine(name=spec["name"], genotype="KD",
                                  target_id=spec["target"].pk, site_id=spec["site_id"],
                                  parent_line_id=spec["wt"].pk,
                                  parental_line_name=spec["wt"].name,
                                  knockdown_method=spec["method"],
                                  species=spec["wt"].species or "Human")
                    cl.save(using=DB)
                    created += 1
                made[key] = cl
            for s, key in repoint:
                s.cell_line_ko_id = made[key].pk
                s.save(using=DB, update_fields=["cell_line_ko"])
                pointed += 1
        self.stdout.write(self.style.SUCCESS(
            f"\nCreated {created} knockdown line(s); pointed {pointed} session(s) at them."))
