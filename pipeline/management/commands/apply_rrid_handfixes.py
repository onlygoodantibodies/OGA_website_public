"""
Apply the owner's decisions for the 15 RRID hand-review groups (AUDIT.md §3).

Two kinds of fix:
  A. RRID_SETS — the row that carried the WRONG shared RRID gets its correct one
     (and canonical rrid_link). The two rows then become distinct, correct
     products with different RRIDs.
  B. MERGES_RETARGET — the same antibody was recorded under two target genes
     (e.g. a RAB5C antibody also filed under RAB5A). Merge the pair into one row
     under the correct target, keeping the survivor with the most data.

Dry-run by default; --apply writes (per-fix / per-group). Back up first.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Antibody, Target
from pipeline.dedup_utils import payload, apply_merge
from pipeline.rrid_utils import registry_url

# antibody_id -> correct RRID (verified by owner 2026-07-14)
RRID_SETS = {
    817:  "AB_777905",     # G1  INPP5D  Abcam ab45142
    4436: "AB_11131428",   # G2  C9orf72 Abcam ab121779
    3851: "AB_1126314",    # G3  MMP7    Santa Cruz sc-80205
    474:  "AB_2797822",    # G5  ITCH    CST 12117
    1090: "AB_2037981",    # G6  SH3GL1  GeneTex GTX113548
    835:  "AB_2617216",    # G7  DAG1    DSHB IIH6 C4
    3576: "AB_3702305",    # G8  AGER    Thermo 701316 (catalogue left as-is)
    1975: "AB_2848874",    # G9  NLRP3   Thermo MA5-34969
    3749: "AB_10568118",   # G10 AGER    Aviva ARP41464_P050
    523:  "AB_3094924",    # G14 FCER1G  CST 78401
}

# (correct_gene, [antibody_ids]) -> merge into one row under that target
MERGES_RETARGET = [
    ("RAB5C", [1588, 4450]),  # G4  CST 3547     (RAB5A row -> RAB5C)
    ("RAB5C", [1560, 4451]),  # G11 Thermo MA5-37697 (RAB5B -> RAB5C)
    ("RAB5C", [331, 4449]),   # G12 Abcam ab199530   (RAB5A -> RAB5C)
    ("NTN1",  [1939, 2521]),  # G13 R&D AF6419 (erroneous SH3GL2 row folded into NTN1)
]


class Command(BaseCommand):
    help = "Apply the 15 RRID hand-review decisions (AUDIT.md §3). Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes. Without this it is a read-only dry-run.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        if Antibody.objects.db != "pipeline_db":
            raise CommandError(f"Antibody routes to '{Antibody.objects.db}', expected 'pipeline_db'.")

        banner = "APPLY (writing)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 72)
        self.stdout.write(f"  apply_rrid_handfixes — {banner}")
        self.stdout.write("=" * 72)

        set_n = merged_n = removed_n = errors = 0

        self.stdout.write("\n-- A. RRID corrections --")
        for ab_id, rrid in RRID_SETS.items():
            ab = Antibody.objects.filter(id=ab_id).select_related("target", "company").first()
            if not ab:
                self.stderr.write(f"  [skip] id{ab_id} not found")
                continue
            self.stdout.write(f"  id{ab_id} | {ab.target.gene_name} | {ab.catalogue_number!r} : "
                              f"{ab.rrid!r} -> {rrid!r}")
            if apply:
                ab.rrid = rrid
                ab.rrid_link = registry_url(rrid)
                ab.save(using="pipeline_db", update_fields=["rrid", "rrid_link", "updated_at"])
            set_n += 1

        self.stdout.write("\n-- B. Mis-targeted duplicate merges --")
        for gene, ids in MERGES_RETARGET:
            tgt = Target.objects.filter(gene_name=gene).first()
            if not tgt:
                self.stderr.write(f"  [skip] target {gene!r} not found")
                continue
            rows = list(Antibody.objects.filter(id__in=ids).select_related("target"))
            if len(rows) < 2:
                self.stdout.write(f"  [merge->{gene}] {ids}: fewer than 2 rows present — skip")
                continue
            survivor = max(rows, key=lambda r: (payload(r), -r.id))
            losers = [r for r in rows if r.id != survivor.id]
            self.stdout.write(
                f"  [merge->{gene}] survivor id{survivor.id} "
                f"(now {survivor.target.gene_name}, data {payload(survivor)}) "
                f"<- {[l.id for l in losers]}; retarget -> {gene}")
            if not apply:
                merged_n += 1
                removed_n += len(losers)
                continue
            try:
                with transaction.atomic(using="pipeline_db"):
                    apply_merge(survivor, losers, "pipeline_db")
                    if survivor.target_id != tgt.id:
                        survivor.target_id = tgt.id
                        survivor.save(using="pipeline_db", update_fields=["target", "updated_at"])
                merged_n += 1
                removed_n += len(losers)
                self.stdout.write("      >> MERGED")
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self.stderr.write(f"      !! ERROR {gene}: {exc!r} — rolled back")

        self.stdout.write("\n" + "=" * 72)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    RRIDs {'set' if apply else 'to set'}          : {set_n}")
        self.stdout.write(f"    merges {'done' if apply else 'to do'}         : {merged_n}")
        self.stdout.write(f"    rows removed by merges     : {removed_n}")
        if apply:
            self.stdout.write(f"    errors (rolled back)       : {errors}")
        else:
            self.stdout.write("\n  DRY-RUN. No data changed. Back up, then rerun with --apply.")
        self.stdout.write("=" * 72)
