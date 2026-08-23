"""
Fill blank Target ``depmap_expression`` from the cached DepMap HAP1 value.

Every target's ``depmap_expression`` is the **HAP1** log2(TPM+1) — that is what
the feasibility "add target" flow stores (feasibility.html sends
``depmap_tpm: dm.hap1_tpm``) and what ``_compute_summary`` scores against the
≥2.5 threshold. The value is already cached locally in the ``DepMapExpression``
table (imported by ``import_depmap_expression``), so this is a **pure local
backfill — no network, no API** — that completes a wholly-empty field and
sharpens the feasibility traffic light.

GAP-FILL ONLY, LOSSLESS:
  * only targets whose ``depmap_expression`` is blank are touched — a populated
    value is never overwritten;
  * a target with no cached HAP1 row (gene not measured in HAP1, or not in the
    DepMap cache) is skipped and reported — never guessed.

SAFETY MODEL (identical to standardize_rrid_format)
  * Defaults to --dry-run: prints every change it would make, writes nothing.
  * --apply writes, inside a single transaction (all-or-nothing). It is local
    and fast, so one transaction is fine.
  * Guarded to pipeline_db only. Fully reversible with a pg_dump backup.

USAGE (run in the Render shell)
  python manage.py backfill_depmap_expression                 # dry-run
  python manage.py backfill_depmap_expression --scope published
  python manage.py backfill_depmap_expression --apply
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Target, DepMapExpression

DB = "pipeline_db"
HAP1 = "HAP1"


class Command(BaseCommand):
    help = ("Fill blank Target depmap_expression from the cached DepMap HAP1 value "
            "(dry-run by default; local, no network).")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes. Without this it is a read-only dry-run.")
        parser.add_argument("--scope", choices=["all", "published"], default="all",
                            help="Which targets to consider (default: all).")
        parser.add_argument("--show", type=int, default=25,
                            help="How many example fills to print (default 25).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        scope = opts["scope"]
        show = opts["show"]

        if Target.objects.db != "pipeline_db":
            raise CommandError(
                f"Target routes to '{Target.objects.db}', expected 'pipeline_db'. "
                f"Aborting to avoid touching the wrong database."
            )

        banner = "APPLY (writing changes)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 70)
        self.stdout.write(f"  backfill_depmap_expression — {banner}   scope={scope}")
        self.stdout.write("=" * 70)

        # Pre-load every HAP1 value ONCE. The DepMap cache has a row per
        # (gene, cell line) — millions of rows — so a per-target query would do a
        # slow scan 500+ times. One indexed query on cell_line (it has a db_index;
        # an exact match uses it, __iexact would not) + an in-memory dict keeps
        # this to seconds. Fall back to __iexact only if the exact form is empty.
        rows = list(DepMapExpression.objects.using(DB)
                    .filter(cell_line=HAP1).values_list("gene_name", "tpm_log2"))
        if not rows:
            rows = list(DepMapExpression.objects.using(DB)
                        .filter(cell_line__iexact=HAP1).values_list("gene_name", "tpm_log2"))
        hap1_by_gene = {}
        for gname, tpm in rows:
            hap1_by_gene.setdefault((gname or "").strip().upper(), tpm)
        self.stdout.write(f"  loaded {len(hap1_by_gene)} HAP1 values from the DepMap cache\n")

        qs = Target.objects.using(DB).filter(depmap_expression__isnull=True).order_by("gene_name")
        if scope == "published":
            qs = qs.filter(status="published")

        stats = dict(considered=0, filled=0, no_hap1=0, no_gene=0)
        to_save = []
        examples = []

        for t in qs:
            stats["considered"] += 1
            gene = (t.gene_name or "").strip()
            if not gene:
                stats["no_gene"] += 1
                continue
            tpm = hap1_by_gene.get(gene.upper())
            if tpm is None:
                stats["no_hap1"] += 1
                continue
            value = Decimal(str(round(tpm, 2)))
            t.depmap_expression = value
            to_save.append(t)
            stats["filled"] += 1
            if len(examples) < show:
                examples.append((gene, value))

        if examples:
            self.stdout.write("\n  Example fills (gene: HAP1 log2(TPM+1)):")
            for gene, value in examples:
                self.stdout.write(f"    {gene:16} -> {value}")

        if apply and to_save:
            with transaction.atomic(using=DB):
                for t in to_save:
                    t.save(using=DB, update_fields=["depmap_expression", "updated_at"])

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    targets with blank depmap_expression : {stats['considered']}")
        self.stdout.write(f"    {'filled' if apply else 'to fill'} from HAP1"
                          f"{'':<20}: {stats['filled']}")
        self.stdout.write(f"    skipped — no HAP1 value cached        : {stats['no_hap1']}")
        if stats["no_gene"]:
            self.stdout.write(f"    skipped — target has no gene name    : {stats['no_gene']}")
        if not apply:
            self.stdout.write("\n  This was a DRY-RUN. No data changed. "
                              "Back up, then rerun with --apply.")
        self.stdout.write("=" * 70)
