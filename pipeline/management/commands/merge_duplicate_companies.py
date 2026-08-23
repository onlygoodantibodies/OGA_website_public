"""
Merge the known duplicate Company records into one canonical record each, and
delete the drained generic "Bio-Techne" (id139).

See AUDIT.md §3. 52 company records include duplicate manufacturers stored under
formatting-variant names (e.g. "abcam"/"Abcam"). This command merges the eight
clean groups (Bio-Techne is handled separately by `fix_biotechne_brands`, which
keeps Novus and R&D distinct).

The merge plan is EXPLICIT (below) rather than auto-detected, so it can never
accidentally fold distinct brands (e.g. the Bio-Techne Novus/R&D records, which
share a display name) together.

For each group:
  * Antibodies from the losers are moved onto the survivor company. Because the
    same product can exist under both records, antibodies are grouped by
    (catalogue, target, lot, site) and reassign-or-merged (survivor kept), so the
    unique (catalogue, company, target, lot, site) constraint is never violated.
    This also collapses catalogue-case duplicates (`ab51037` vs `AB51037`).
  * All other company FKs (cell lines, contacts, reagent requests, shipments) are
    re-pointed to the survivor.
  * Blank survivor fields (display_name, website) are backfilled from the losers.
  * The loser company rows are deleted.

SAFETY: dry-run by default; --apply writes inside one transaction per group;
guarded to pipeline_db. Back up (`dumpdata pipeline`) before --apply.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Company, Antibody
from pipeline.dedup_utils import normcat, payload, is_empty, apply_merge

# (survivor_id, [loser_ids], label) — verified from the 2026-07-14 company audit.
MERGES = [
    (130, [176], "Abcam"),
    (131, [178], "ABclonal"),
    (137, [173], "BioLegend"),
    (151, [180], "Santa Cruz"),
    (142, [177], "DSHB"),
    (148, [179], "MilliporeSigma (Sigma)"),
    (174, [134], "BD Biosciences"),
    (147, [171], "Atlas Antibodies"),
]
# Companies expected to be empty (drained by fix_biotechne_brands) — delete them.
DELETE_EMPTY = [139]

COMPANY_BACKFILL = ["display_name", "website"]


class Command(BaseCommand):
    help = "Merge known duplicate Company records + delete drained id139. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes. Without this it is a read-only dry-run.")

    def other_company_fks(self, survivor, losers, do_apply):
        """Re-point every non-Antibody FK relation from losers onto survivor.
        Returns {ModelName: count}. Errors on an unexpected reverse M2M."""
        moved = {}
        for rel in Company._meta.related_objects:
            if rel.related_model is Antibody:
                continue
            if getattr(rel, "many_to_many", False):
                for loser in losers:
                    if getattr(loser, rel.get_accessor_name()).exists():
                        raise CommandError(f"Unexpected M2M {rel} on company {loser.id}; handle manually.")
                continue
            fname = rel.field.name
            for loser in losers:
                qs = rel.related_model.objects.filter(**{fname: loser})
                n = qs.count()
                if n:
                    moved[rel.related_model.__name__] = moved.get(rel.related_model.__name__, 0) + n
                    if do_apply:
                        qs.update(**{fname: survivor})
        return moved

    def handle(self, *args, **opts):
        apply = opts["apply"]
        if Company.objects.db != "pipeline_db":
            raise CommandError(f"Company routes to '{Company.objects.db}', expected 'pipeline_db'.")

        banner = "APPLY (writing)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 72)
        self.stdout.write(f"  merge_duplicate_companies — {banner}")
        self.stdout.write("=" * 72)

        totals = dict(groups=0, ab_reassigned=0, ab_merged=0, ab_removed=0,
                      imgs_dropped=0, companies_deleted=0, errors=0)

        for survivor_id, loser_ids, label in MERGES:
            survivor = Company.objects.filter(id=survivor_id).first()
            if not survivor:
                self.stderr.write(f"[skip] {label}: survivor id{survivor_id} not found")
                continue
            losers = list(Company.objects.filter(id__in=loser_ids))
            if not losers:
                self.stdout.write(f"[done] {label}: no loser records present (already merged)")
                continue

            company_ids = [survivor_id] + [l.id for l in losers]
            # group antibodies across survivor+losers by final identity
            groups = {}
            for ab in Antibody.objects.filter(company_id__in=company_ids).select_related("target"):
                key = (normcat(ab.catalogue_number), ab.target_id,
                       (ab.lot_number or '').strip().lower(), ab.site_id)
                groups.setdefault(key, []).append(ab)

            plan, n_reassign, n_merge, n_remove = [], 0, 0, 0
            for rows in groups.values():
                if len(rows) == 1 and rows[0].company_id == survivor_id:
                    continue
                surv_ab = max(rows, key=lambda r: (payload(r), -r.id))
                lose_ab = [r for r in rows if r.id != surv_ab.id]
                plan.append((surv_ab, lose_ab))
                if lose_ab:
                    n_merge += 1
                    n_remove += len(lose_ab)
                else:
                    n_reassign += 1

            other_fk = self.other_company_fks(survivor, losers, do_apply=False)
            cbf = {}
            for f in COMPANY_BACKFILL:
                if is_empty(getattr(survivor, f)):
                    for l in losers:
                        if not is_empty(getattr(l, f)):
                            cbf[f] = getattr(l, f)
                            break

            self.stdout.write(f"\n[{label}] keep id{survivor_id} {survivor.name!r} "
                              f"(display {survivor.display_name!r}) <- delete {loser_ids}")
            self.stdout.write(f"    antibodies: reassign {n_reassign}, merge-groups {n_merge} "
                              f"(remove {n_remove} rows)")
            if other_fk:
                self.stdout.write(f"    other FKs re-pointed: {other_fk}")
            if cbf:
                self.stdout.write(f"    company backfill: {cbf}")

            totals["groups"] += 1
            totals["ab_reassigned"] += n_reassign
            totals["ab_merged"] += n_merge
            totals["ab_removed"] += n_remove

            if not apply:
                continue
            try:
                with transaction.atomic(using="pipeline_db"):
                    for surv_ab, lose_ab in plan:
                        if lose_ab:
                            _, dropped = apply_merge(surv_ab, lose_ab, "pipeline_db",
                                                     target_company_id=survivor_id)
                            totals["imgs_dropped"] += dropped
                        elif surv_ab.company_id != survivor_id:
                            surv_ab.company_id = survivor_id
                            surv_ab.save(using="pipeline_db", update_fields=["company_id", "updated_at"])
                    self.other_company_fks(survivor, losers, do_apply=True)
                    if cbf:
                        for f, v in cbf.items():
                            setattr(survivor, f, v)
                        survivor.save(using="pipeline_db", update_fields=list(cbf.keys()))
                    for l in losers:
                        l.delete(using="pipeline_db")
                self.stdout.write("    >> MERGED")
            except Exception as exc:  # noqa: BLE001
                totals["errors"] += 1
                self.stderr.write(f"    !! ERROR on {label}: {exc!r} — rolled back")

        # delete drained records
        for cid in DELETE_EMPTY:
            c = Company.objects.filter(id=cid).first()
            if not c:
                continue
            ab_n = Antibody.objects.filter(company_id=cid).count()
            other = self.other_company_fks(c, [c], do_apply=False)
            if ab_n or other:
                self.stdout.write(f"\n[keep] id{cid} {c.name!r}: NOT empty "
                                  f"(antibodies={ab_n}, other={other}) — not deleting")
                continue
            self.stdout.write(f"\n[delete-empty] id{cid} {c.name!r} (0 references)")
            if apply:
                c.delete(using="pipeline_db")
                totals["companies_deleted"] += 1

        self.stdout.write("\n" + "=" * 72)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    company groups {'merged' if apply else 'to merge'} : {totals['groups']}")
        self.stdout.write(f"    antibodies reassigned      : {totals['ab_reassigned']}")
        self.stdout.write(f"    antibody groups merged     : {totals['ab_merged']}")
        self.stdout.write(f"    antibody rows removed      : {totals['ab_removed']}")
        self.stdout.write(f"    duplicate images dropped   : {totals['imgs_dropped']}")
        self.stdout.write(f"    empty companies {'deleted' if apply else 'to delete'} : "
                          f"{totals['companies_deleted'] if apply else len(DELETE_EMPTY)}")
        if apply:
            self.stdout.write(f"    errors (rolled back)       : {totals['errors']}")
        else:
            self.stdout.write("\n  DRY-RUN. No data changed. Back up, then rerun with --apply.")
        self.stdout.write(f"    company records remaining  : {Company.objects.count()}")
        self.stdout.write("=" * 72)
