"""
Read-only check for duplicate Company records (same manufacturer under
formatting-variant names). Run periodically or after a bulk import.

The model now prevents new variants (Company.clean on admin saves, Company.resolve
in imports), so this should normally report 0. If it ever finds any, merge them
with `merge_duplicate_companies` (or extend its plan).
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from pipeline.models import Company, Antibody


class Command(BaseCommand):
    help = "List duplicate Company records (formatting variants of one supplier)."

    def handle(self, *args, **opts):
        ab_counts = defaultdict(int)
        for cid in Antibody.objects.values_list("company_id", flat=True):
            if cid is not None:
                ab_counts[cid] += 1

        # Group by canonical key of the NAME (same definition the clean() guard
        # uses). Intentional brand aliases that merely share a display_name — e.g.
        # Bio-Techne's Novus vs R&D records — have distinct names and are NOT dups.
        groups = defaultdict(list)
        for c in Company.objects.all():
            groups[Company.canonical_key(c.name)].append(c)

        dupes = {k: v for k, v in groups.items() if len(v) > 1}
        self.stdout.write("=" * 70)
        self.stdout.write(f"  Company records : {Company.objects.count()}")
        self.stdout.write(f"  duplicate groups: {len(dupes)}")
        self.stdout.write("=" * 70)
        for k, comps in sorted(dupes.items()):
            self.stdout.write(f"\n[{k}]")
            for c in sorted(comps, key=lambda x: -ab_counts.get(x.id, 0)):
                self.stdout.write(f"   id={c.id:<4} antibodies={ab_counts.get(c.id, 0):<4} "
                                  f"name={c.name!r} display={c.display_name!r}")
        if not dupes:
            self.stdout.write("\n  ✓ No duplicate company records.")
        self.stdout.write("=" * 70)
