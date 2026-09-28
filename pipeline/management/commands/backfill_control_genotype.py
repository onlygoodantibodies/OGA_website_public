"""Fill `control_genotype` on figures published before the column existed.

    python manage.py backfill_control_genotype                 # dry run
    python manage.py backfill_control_genotype --apply
    python manage.py backfill_control_genotype --gene RAB27A --set KD --apply
    python manage.py backfill_control_genotype --gene RAB13 --application IP --set KD --apply
    python manage.py backfill_control_genotype --gene RAB14 --catalogue A12752 --application ICC-IF --set KO --apply

Which kind of genetic control a public figure shows — knockout or knockdown —
was recorded nowhere until 18 Sep 2026 (`PublicationImage.control_genotype`).
The cropper writes it on everything it stages from now on; every figure
published before it existed carries a blank, and `pipeline/public.py::
control_kinds` reads a blank as **knockout**, the site's historical claim.
The Access export says that claim is wrong for at least fourteen genes, so the
blanks are not harmless. Three sources, tried in this order for each figure:

* **The Access export** (`access_csvs/`, in the repo and therefore on the
  service). Each `Wb`, `IP` and `IF` row names its antibody (`AntibodiesID` ↔
  `Antibody.access_id`) and says whether it was a knockdown — `KnockDown`
  ticked on the blot, or `KD` / `siRNA` in the comment; the two disagree on
  151 rows, so both are read. A figure whose every Access row for that
  application is marked knockdown is a knockdown (230 pairs on the 31 Jul
  2026 export). One with **some** rows marked and some not (45 pairs, on
  six genes that also hold a knockout line) is listed and **not written**:
  the published figure is one of those blots and the export does not say
  which, so a person reads the legend and answers with `--gene X --set`.
  Rows with no mark at all leave the figure alone.
* **The cropper session that staged it**, where the pending row still points
  at one — the same value the legend was rendered from.
* **`--gene X --set KO|KD`**, for what neither settles — narrowed with
  `--application` and `--catalogue` where one gene's figures were controlled
  two ways (RAB13's IPs were knockdowns and its IF plates knockouts).

Never writes over a value already set unless `--overwrite` says so, counts
what it left blank, and is dry-run by default: production-data skill rules
apply, and a wrong write is a public page saying the wrong word about a
figure.
"""
from __future__ import annotations

import csv
import os
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import PendingPublicationImage, PublicationImage, Target

DB = "pipeline_db"
KINDS = {"KO", "KD"}
DEFAULT_DIR = "access_csvs"

# Access table → the application its rows are figures of. No FC table exists.
ACCESS_TABLES = (("Wb.csv", "WB"), ("IP.csv", "IP"), ("IF.csv", "ICC-IF"))

# `KD`, `Lane 2 is a KD`, `siRNA KD in U87`, `it was siRNA KD`, `Knockdown`.
_KD_WORDS = re.compile(r"\bKD\b|knock\s*-?\s*down|si\s*-?\s*rna|sh\s*-?\s*rna", re.I)


def read_access(directory: str) -> dict:
    """`{(antibody_access_id, application): (kd_rows, other_rows)}` — how many
    of an antibody's Access result rows for that application say knockdown,
    and how many say nothing. A blot is a knockdown when its tick is set
    **or** its comment says so, because the export carries 94 rows with the
    tick alone and 57 with the words alone."""
    out = {}
    for filename, app in ACCESS_TABLES:
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    ab_id = int((row.get("AntibodiesID") or "").strip())
                except ValueError:
                    continue
                kd = ((row.get("KnockDown") or "").strip() == "1"
                      or bool(_KD_WORDS.search(row.get("Comments") or "")))
                kd_n, other_n = out.get((ab_id, app), (0, 0))
                out[(ab_id, app)] = (kd_n + 1, other_n) if kd else (kd_n, other_n + 1)
    return out


class Command(BaseCommand):
    help = ("Fill the control kind (KO/KD) on published figures from the Access "
            "export, the cropper session that staged them, or by gene with --set. "
            "Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=DEFAULT_DIR, metavar="DIR",
                            help=f"Where the Access CSVs are (default {DEFAULT_DIR}/).")
        parser.add_argument("--gene", nargs="+", metavar="GENE",
                            help="Only figures on these genes.")
        parser.add_argument("--application", nargs="+", metavar="APP",
                            choices=[a for _f, a in ACCESS_TABLES] + ["FC"],
                            help="Only figures of these applications: WB, IP, ICC-IF, FC.")
        parser.add_argument("--catalogue", nargs="+", metavar="CAT",
                            help="Only figures of antibodies with these catalogue numbers.")
        parser.add_argument("--set", choices=sorted(KINDS), metavar="KO|KD",
                            help="Write this kind onto the selected figures instead "
                                 "of inferring it. Requires --gene.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Also replace a value already recorded.")
        parser.add_argument("--apply", action="store_true",
                            help="Write. Without it, report only.")

    def handle(self, *args, **options):
        genes = [g.strip() for g in (options["gene"] or []) if g.strip()]
        forced = options["set"]
        if forced and not genes:
            raise CommandError("--set needs --gene: a kind written onto every "
                               "figure on the site is a guess, not a backfill.")

        figures = (PublicationImage.objects.using(DB)
                   .select_related("antibody__target").order_by("pk"))
        if genes:
            targets = list(Target.objects.using(DB).filter(gene_name__in=genes))
            missing = sorted(set(genes) - {t.gene_name for t in targets})
            if missing:
                raise CommandError(f"no target called: {', '.join(missing)}")
            figures = figures.filter(antibody__target__in=targets)
        if options["application"]:
            figures = figures.filter(application_type__in=options["application"])
        if options["catalogue"]:
            figures = figures.filter(antibody__catalogue_number__in=options["catalogue"])
        figures = list(figures)
        if forced and not figures:
            raise CommandError("nothing matches those filters — check the gene, "
                               "application and catalogue number")

        access = {} if forced else read_access(options["dir"])
        if not forced and not access:
            self.stdout.write(self.style.WARNING(
                f"no Access tables found under {options['dir']}/ — only the "
                f"cropper sessions can answer"))

        pending_by_key = {
            (p.antibody_id, p.application_type): p
            for p in PendingPublicationImage.objects.using(DB)
            .select_related("source_session")
            .filter(antibody_id__in=[f.antibody_id for f in figures])
        }

        planned, kept, mixed, unknown = [], 0, [], []
        for fig in figures:
            if fig.control_genotype and not options["overwrite"]:
                kept += 1
                continue
            pending = pending_by_key.get((fig.antibody_id, fig.application_type))
            kind, why = "", ""
            if forced:
                kind, why = forced, "--set"
            else:
                kd_n, other_n = access.get(
                    (fig.antibody.access_id, fig.application_type), (0, 0))
                if kd_n and not other_n:
                    kind, why = "KD", f"Access: all {kd_n} row(s) say knockdown"
                elif kd_n and other_n:
                    mixed.append((fig, kd_n, other_n))
                    continue
                else:
                    session = getattr(pending, "source_session", None) if pending else None
                    kind = (getattr(session, "genotype", "") or "").strip().upper()
                    why = f"cropper session {session.pk}" if session else ""
            if kind not in KINDS:
                unknown.append(fig)
                continue
            planned.append((fig, pending, kind, why))

        gene_of = lambda f: (f.antibody.target.gene_name if f.antibody.target_id else "?")
        line = lambda f: f"    {gene_of(f):10s} {f.antibody.catalogue_number:16s} {f.application_type:6s}"
        self.stdout.write(f"{len(figures)} published figure(s) considered")
        self.stdout.write(f"  already recorded, left alone   {kept}")
        self.stdout.write(f"  would be written               {len(planned)}")
        for fig, _p, kind, why in planned:
            self.stdout.write(f"{line(fig)} → {kind}  ({why})")
        if mixed:
            self.stdout.write(self.style.WARNING(
                f"  Access rows disagree            {len(mixed)} — some of this "
                f"antibody's blots are marked knockdown and some not, and the "
                f"export does not say which was published. Read the figure's "
                f"legend and answer with --gene <GENE> --set KO|KD"))
            for fig, kd_n, other_n in mixed:
                self.stdout.write(f"{line(fig)} {kd_n} knockdown row(s), {other_n} unmarked")
        if unknown:
            self.stdout.write(
                f"  no record of the kind          {len(unknown)} — no Access row "
                f"marked knockdown and no cropper session; these stay blank and "
                f"read as knockout")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing was changed. Re-run with --apply to write."))
            return

        with transaction.atomic(using=DB):
            for fig, pending, kind, _why in planned:
                fig.control_genotype = kind
                fig.save(using=DB, update_fields=["control_genotype"])
                if pending is not None and (options["overwrite"] or not pending.control_genotype):
                    pending.control_genotype = kind
                    pending.save(using=DB, update_fields=["control_genotype", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"\nWrote {len(planned)} figure(s)."))
