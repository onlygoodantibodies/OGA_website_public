"""
Management command: restructure_cell_lines

Three phases:
  1. Create CellLineVial for every existing CellLine record (preserves c_numbers)
  2. Relink InventoryLocations from cell_line FK to vial FK
  3. Dedup WT lines: merge by (name, site), survivor gets target=NULL
  4. Dedup KO lines: merge by (name, site, target)

Safe to run multiple times. Use --dry-run first.

Usage:
  python manage.py restructure_cell_lines --dry-run
  python manage.py restructure_cell_lines
  python manage.py restructure_cell_lines --skip-dedup   # vials + relink only
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Min

from pipeline.models import (
    CellLine, CellLineVial, InventoryLocation, ExperimentSession,
)

DB = "pipeline_db"


class Command(BaseCommand):
    help = "Restructure cell lines: create vials from c_numbers, dedup WT and KO"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen without making changes",
        )
        parser.add_argument(
            "--skip-dedup", action="store_true",
            help="Only create vials and relink locations, skip dedup",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skip_dedup = options["skip_dedup"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be made\n"))

        self._phase1_create_vials(dry_run)
        self._phase2_relink_locations(dry_run)

        if not skip_dedup:
            self._phase3_dedup_wt(dry_run)
            self._phase4_dedup_ko(dry_run)

        self.stdout.write(self.style.SUCCESS("\nDone."))

    # ------------------------------------------------------------------
    # Phase 1 — Create CellLineVial for every CellLine record
    # ------------------------------------------------------------------
    def _phase1_create_vials(self, dry_run):
        self.stdout.write("\n=== Phase 1: Create CellLineVials ===")

        all_lines = list(CellLine.objects.using(DB).select_related("site"))
        created = 0
        skipped = 0

        for cl in all_lines:
            # Already has a vial? (safe for re-runs)
            # Match on access_id if available, otherwise on (cell_line, c_number)
            if cl.access_id is not None:
                exists = CellLineVial.objects.using(DB).filter(
                    access_id=cl.access_id
                ).exists()
            else:
                exists = CellLineVial.objects.using(DB).filter(
                    cell_line_id=cl.pk, c_number=cl.c_number
                ).exists()

            if exists:
                skipped += 1
                continue

            if not dry_run:
                CellLineVial.objects.using(DB).create(
                    cell_line_id=cl.pk,
                    c_number=cl.c_number,
                    received_date=cl.received_date,
                    acquisition_method=cl.acquisition_method,
                    received=cl.received,
                    thawed=cl.thawed,
                    location_original_vial=cl.location_original_vial,
                    in_kind_value=cl.in_kind_value,
                    in_kind_currency=cl.in_kind_currency,
                    site_id=cl.site_id,
                    access_id=cl.access_id,
                )
            created += 1

        self.stdout.write(f"  Vials created: {created}")
        self.stdout.write(f"  Skipped (already exist): {skipped}")

    # ------------------------------------------------------------------
    # Phase 2 — Relink InventoryLocations to vials
    # ------------------------------------------------------------------
    def _phase2_relink_locations(self, dry_run):
        self.stdout.write("\n=== Phase 2: Relink InventoryLocations to vials ===")

        # Locations that have cell_line FK but no vial FK
        locs = list(
            InventoryLocation.objects.using(DB)
            .filter(cell_line__isnull=False, vial__isnull=True)
        )

        linked = 0
        unmatched = 0

        for loc in locs:
            # Find the vial created from this cell line
            vial = CellLineVial.objects.using(DB).filter(
                cell_line_id=loc.cell_line_id
            ).first()

            if vial:
                if not dry_run:
                    loc.vial_id = vial.pk
                    loc.save(using=DB, update_fields=["vial_id"])
                linked += 1
            else:
                unmatched += 1
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ No vial found for location {loc.pk} "
                    f"(cell_line_id={loc.cell_line_id})"
                ))

        self.stdout.write(f"  Locations linked to vials: {linked}")
        if unmatched:
            self.stdout.write(self.style.WARNING(f"  Unmatched locations: {unmatched}"))

    # ------------------------------------------------------------------
    # Phase 3 — Dedup WT cell lines
    # ------------------------------------------------------------------
    def _phase3_dedup_wt(self, dry_run):
        self.stdout.write("\n=== Phase 3: Dedup WT cell lines ===")

        wt_qs = CellLine.objects.using(DB).filter(genotype="WT")

        groups = (
            wt_qs
            .values("name", "site")
            .annotate(c=Count("id"), min_id=Min("id"))
            .filter(c__gt=1)
            .order_by("-c")
        )

        stats = {"merged": 0, "vials_moved": 0, "sessions": 0, "parent_lines": 0}

        for group in groups:
            survivor_id = group["min_id"]
            survivor = CellLine.objects.using(DB).get(pk=survivor_id)
            duplicates = list(
                wt_qs.filter(name=group["name"], site_id=group["site"])
                .exclude(pk=survivor_id)
            )

            # Survivor: clear target (WT identity has no target)
            if survivor.target_id is not None:
                if not dry_run:
                    survivor.target_id = None
                    survivor.save(using=DB, update_fields=["target_id"])

            for dup in duplicates:
                self._merge_into(dup, survivor, stats, dry_run)

            self.stdout.write(
                f"  {group['name']:30s} site={group['site'] or '?'}: "
                f"merged {len(duplicates)} duplicates → survivor pk={survivor_id}"
            )

        self.stdout.write(f"\n  WT summary:")
        self.stdout.write(f"    Lines merged: {stats['merged']}")
        self.stdout.write(f"    Vials moved: {stats['vials_moved']}")
        self.stdout.write(f"    Sessions repointed: {stats['sessions']}")
        self.stdout.write(f"    Parent lines repointed: {stats['parent_lines']}")

    # ------------------------------------------------------------------
    # Phase 4 — Dedup KO cell lines
    # ------------------------------------------------------------------
    def _phase4_dedup_ko(self, dry_run):
        self.stdout.write("\n=== Phase 4: Dedup KO cell lines ===")

        ko_qs = CellLine.objects.using(DB).filter(genotype="KO")

        groups = (
            ko_qs
            .values("name", "site", "target")
            .annotate(c=Count("id"), min_id=Min("id"))
            .filter(c__gt=1)
            .order_by("-c")
        )

        stats = {"merged": 0, "vials_moved": 0, "sessions": 0, "parent_lines": 0}

        for group in groups:
            survivor_id = group["min_id"]
            survivor = CellLine.objects.using(DB).get(pk=survivor_id)
            duplicates = list(
                ko_qs.filter(
                    name=group["name"],
                    site_id=group["site"],
                    target_id=group["target"],
                )
                .exclude(pk=survivor_id)
            )

            for dup in duplicates:
                self._merge_into(dup, survivor, stats, dry_run)

            target_name = survivor.target.gene_name if survivor.target else "?"
            self.stdout.write(
                f"  {group['name']:30s} {target_name:15s} site={group['site'] or '?'}: "
                f"merged {len(duplicates)} duplicates → survivor pk={survivor_id}"
            )

        self.stdout.write(f"\n  KO summary:")
        self.stdout.write(f"    Lines merged: {stats['merged']}")
        self.stdout.write(f"    Vials moved: {stats['vials_moved']}")
        self.stdout.write(f"    Sessions repointed: {stats['sessions']}")
        self.stdout.write(f"    Parent lines repointed: {stats['parent_lines']}")

    # ------------------------------------------------------------------
    # Merge one duplicate CellLine into the survivor
    # ------------------------------------------------------------------
    def _merge_into(self, dup, survivor, stats, dry_run):
        """Move all child records from dup to survivor, then delete dup."""

        # 1. Move vials
        vials = CellLineVial.objects.using(DB).filter(cell_line_id=dup.pk)
        count = vials.count()
        if not dry_run:
            vials.update(cell_line_id=survivor.pk)
        stats["vials_moved"] += count

        # 2. Move any remaining direct InventoryLocations (belt + braces)
        direct_locs = InventoryLocation.objects.using(DB).filter(cell_line_id=dup.pk)
        if not dry_run:
            direct_locs.update(cell_line_id=survivor.pk)

        # 3. Repoint ExperimentSession.cell_line_wt
        s_wt = ExperimentSession.objects.using(DB).filter(cell_line_wt_id=dup.pk)
        stats["sessions"] += s_wt.count()
        if not dry_run:
            s_wt.update(cell_line_wt_id=survivor.pk)

        # 4. Repoint ExperimentSession.cell_line_ko
        s_ko = ExperimentSession.objects.using(DB).filter(cell_line_ko_id=dup.pk)
        stats["sessions"] += s_ko.count()
        if not dry_run:
            s_ko.update(cell_line_ko_id=survivor.pk)

        # 5. Repoint KO lines whose parent_line points to this dup
        children = CellLine.objects.using(DB).filter(parent_line_id=dup.pk)
        stats["parent_lines"] += children.count()
        if not dry_run:
            children.update(parent_line_id=survivor.pk)

        # 6. Repoint arrived_with_ko references
        arrived = CellLine.objects.using(DB).filter(arrived_with_ko_id=dup.pk)
        if not dry_run:
            arrived.update(arrived_with_ko_id=survivor.pk)

        # 7. Repoint CellCultureEvents (if any exist)
        try:
            from pipeline.models import CellCultureEvent
            events = CellCultureEvent.objects.using(DB).filter(cell_line_id=dup.pk)
            if not dry_run:
                events.update(cell_line_id=survivor.pk)
        except Exception:
            pass  # Model may not exist yet

        # 8. Delete the duplicate
        if not dry_run:
            dup.delete(using=DB)
        stats["merged"] += 1
