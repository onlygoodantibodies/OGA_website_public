"""
Derive protein classes for targets from UniProt keywords + GO terms.

Feeds the portfolio view ("we have characterised N GPCRs, N secreted proteins"),
which Carl currently reconstructs from memory while writing grants.

Re-runnable and safe: derived rows are refreshed, hand-added (``manual``) tags are
never touched. One UniProt call per target, so it is rate-limit friendly by
default — use ``--genes`` to do a handful after an import rather than the lot.

    python manage.py backfill_protein_classes --database=pipeline_db
    python manage.py backfill_protein_classes --genes SNCA,MAPT --verbose
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from pipeline.models import Target
from pipeline.services import protein_class


class Command(BaseCommand):
    help = "Derive TargetClassification rows from UniProt keywords and GO terms."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="pipeline_db")
        parser.add_argument("--genes", default="",
                            help="Comma-separated gene list (default: every target)")
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after N targets (0 = no limit)")
        parser.add_argument("--offline", action="store_true",
                            help="Skip UniProt; write only the gene-family label")
        parser.add_argument("--sleep", type=float, default=0.2,
                            help="Seconds between UniProt calls")
        parser.add_argument("--resume", action="store_true",
                            help="Skip targets that already have classifications. "
                                 "Makes a re-run after a dropped shell cost only "
                                 "the targets still outstanding.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        db = opts["database"]
        qs = Target.objects.using(db).order_by("gene_name")
        if opts["genes"]:
            genes = [g.strip() for g in opts["genes"].split(",") if g.strip()]
            qs = qs.filter(gene_name__in=genes)
        if opts["limit"]:
            qs = qs[:opts["limit"]]

        if opts["resume"]:
            from pipeline.models import TargetClassification
            done = set(TargetClassification.objects.using(db)
                       .values_list("target_id", flat=True))
            before = qs.count() if hasattr(qs, "count") else len(qs)
            qs = [t for t in qs if t.pk not in done]
            self.stdout.write(f"Resuming: {before - len(qs)} already classified, "
                              f"{len(qs)} to go.")

        targets = list(qs)
        self.stdout.write(f"{len(targets)} target(s) to classify on '{db}'.")
        touched = failed = 0

        for i, target in enumerate(targets, 1):
            # A sentinel, not None: classify() treats None as "fetch them for me",
            # so passing None here made --offline hit UniProt anyway.
            terms = {"found": False} if opts["offline"] else None
            if not opts["offline"] and target.uniprot_id:
                terms = protein_class.fetch_terms(target.uniprot_id)
                if terms.get("error"):
                    failed += 1
                if opts["sleep"]:
                    time.sleep(opts["sleep"])

            if opts["dry_run"]:
                labels = protein_class.classify(
                    target.gene_name or "", target.uniprot_id or "", terms=terms)
                self.stdout.write(
                    f"  [{i}/{len(targets)}] {target.gene_name}: "
                    f"{', '.join(sorted({d['label'] for d in labels})) or '(none)'}")
                continue

            summary = protein_class.sync_target(target, db=db, terms=terms)
            if summary["created"] or summary["removed"]:
                touched += 1
            # Each target is written as it is done, so a dropped shell keeps
            # everything up to that point; --resume picks up from there.
            if i % 25 == 0:
                self.stdout.write(f"  … {i}/{len(targets)} done")
                self.stdout.flush()
            if opts["verbosity"] > 1:
                self.stdout.write(
                    f"  [{i}/{len(targets)}] {summary['gene']}: "
                    f"+{summary['created']} -{summary['removed']} "
                    f"({', '.join(summary['labels']) or 'no classes'})")

        verb = "would change" if opts["dry_run"] else "changed"
        self.stdout.write(self.style.SUCCESS(
            f"Done. {touched} target(s) {verb}."
            + (f" {failed} UniProt lookup(s) failed — re-run to fill them in."
               if failed else "")))
