"""
Put every Bio-Techne antibody under the correct sub-brand record, and merge the
duplicates that the wrong brand was hiding.

Background (see AUDIT.md §3): "Bio-Techne" exists as three Company records —
Novus Biologicals, R&D Systems, and a generic "Bio-Techne" junk-drawer. The same
product was often filed under the wrong one, so the same catalogue number appears
under >1 Bio-Techne record (12 such split duplicates found 2026-07-13).

Owner rule (confirmed): rows already under Novus or R&D are TRUSTED as-is (a
deliberate brand assignment beats a prefix guess, e.g. Novus 'BC100-494'); only
the generic junk-drawer is classified — catalogue starting `NB`/`BC` → Novus,
everything else → R&D Systems. Novus and R&D are kept as SEPARATE records.

For each Bio-Techne antibody this command computes its correct brand from the
catalogue prefix, then groups by final identity
(brand, catalogue, target, lot, site):
  * group of 1 whose company is already correct  -> nothing to do
  * group of 1 filed under the wrong brand        -> REASSIGN (update company)
  * group of >1 (a hidden duplicate)              -> MERGE: keep one survivor
    under the correct brand, re-point its children (WB/IP/IF/FC results, images,
    locations), backfill blank fields, delete the rest.

Publication images obey a unique (antibody, application_type) constraint, so a
loser image is moved only if the survivor lacks that application, else the
duplicate record is dropped (the file on R2 stays).

RRID conflicts (same product, two different non-blank RRIDs) are reported for
manual review — the survivor keeps its own RRID; nothing is silently overwritten.

SAFETY: dry-run by default; --apply writes inside one transaction per group;
guarded to pipeline_db. Back up (`dumpdata pipeline`) before --apply.
"""

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Company, Antibody

CHILD_RELATIONS = ["wb_results", "ip_results", "if_results", "fc_results",
                   "publication_images", "locations"]
SIMPLE_RELATIONS = [r for r in CHILD_RELATIONS if r != "publication_images"]

NEVER_BACKFILL = {"id", "access_id", "created_at", "updated_at", "ab_number",
                  "company_id", "company"}
OR_BOOL_FIELDS = {
    "wb_recommended", "ip_recommended", "if_recommended", "fc_recommended",
    "supplier_validated_wb", "supplier_validated_ip", "supplier_validated_if",
    "supplier_validated_ihc", "supplier_validated_elisa", "supplier_validated_fc",
    "is_recombinant",
}


def normname(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def payload(ab):
    return sum(getattr(ab, r).count() for r in CHILD_RELATIONS)


def is_empty(v):
    return v is None or v == "" or v == {}


class Command(BaseCommand):
    help = "Reassign/merge Bio-Techne antibodies to the correct brand (NB=Novus, else R&D). Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes. Without this it is a read-only dry-run.")

    # ---- helpers ----

    def plan_backfill(self, survivor, losers):
        changes = {}
        for field in Antibody._meta.get_fields():
            if not getattr(field, "concrete", False) or field.many_to_many:
                continue
            name, attname = field.name, field.attname
            if name in NEVER_BACKFILL or attname in NEVER_BACKFILL:
                continue
            if name in OR_BOOL_FIELDS:
                if not getattr(survivor, name) and any(getattr(l, name) for l in losers):
                    changes[name] = True
                continue
            if not is_empty(getattr(survivor, attname)):
                continue
            for loser in losers:
                v = getattr(loser, attname)
                if not is_empty(v):
                    changes[attname] = v
                    break
        return changes

    def plan_images(self, survivor, losers):
        seen = set(survivor.publication_images.values_list("application_type", flat=True))
        move, drop = [], []
        for loser in losers:
            for img in loser.publication_images.all():
                if img.application_type in seen:
                    drop.append(img)
                else:
                    move.append(img)
                    seen.add(img.application_type)
        return move, drop

    # ---- main ----

    def handle(self, *args, **opts):
        apply = opts["apply"]
        if Antibody.objects.db != "pipeline_db":
            raise CommandError(f"Antibody routes to '{Antibody.objects.db}', expected 'pipeline_db'.")

        fam = {c.id: c for c in Company.objects.all()
               if normname(c.display_name or c.name) == "biotechne"}
        novus = next((c for c in fam.values() if "novus" in c.name.lower()), None)
        rnd = next((c for c in fam.values()
                    if "r&d" in c.name.lower() or "rdsystems" in normname(c.name)), None)
        if not novus or not rnd:
            raise CommandError(f"Could not identify Novus/R&D records among {[(c.id, c.name) for c in fam.values()]}")

        def target_brand(ab):
            # Trust an existing Novus / R&D assignment (e.g. Novus 'BC100-494',
            # which the NB rule would otherwise mis-move). Only the generic
            # junk-drawer is classified by catalogue prefix.
            if ab.company_id in (novus.id, rnd.id):
                return ab.company_id
            c = re.sub(r'[^a-z0-9]', '', (ab.catalogue_number or '').lower())
            return novus.id if (c.startswith("nb") or c.startswith("bc")) else rnd.id

        banner = "APPLY (writing)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 72)
        self.stdout.write(f"  fix_biotechne_brands — {banner}")
        self.stdout.write(f"  Novus = id{novus.id} {novus.name!r} | R&D = id{rnd.id} {rnd.name!r}")
        self.stdout.write(f"  other family records (drained): {[c.id for c in fam.values() if c.id not in (novus.id, rnd.id)]}")
        self.stdout.write("=" * 72)

        # group family antibodies by their FINAL identity
        groups = {}
        for ab in Antibody.objects.filter(company_id__in=fam).select_related("target"):
            key = (target_brand(ab),
                   re.sub(r'[^a-z0-9]', '', (ab.catalogue_number or '').lower()),
                   ab.target_id, (ab.lot_number or '').strip().lower(), ab.site_id)
            groups.setdefault(key, []).append(ab)

        stats = dict(reassigned=0, merged_groups=0, rows_removed=0,
                     images_dropped=0, rrid_conflicts=0)

        for key, rows in groups.items():
            target_company = key[0]
            if len(rows) == 1:
                ab = rows[0]
                if ab.company_id == target_company:
                    continue
                stats["reassigned"] += 1
                self.stdout.write(f"[reassign] id={ab.id} {ab.catalogue_number!r} "
                                  f"({fam[ab.company_id].name}) -> "
                                  f"{'Novus' if target_company == novus.id else 'R&D'}")
                if apply:
                    ab.company_id = target_company
                    ab.save(using="pipeline_db", update_fields=["company_id", "updated_at"])
                continue

            # duplicate group -> merge, survivor under the correct brand
            survivor = max(rows, key=lambda r: (payload(r), -r.id))
            losers = [r for r in rows if r.id != survivor.id]
            rrids = sorted({(r.rrid or '').strip() for r in rows if (r.rrid or '').strip()})
            conflict = len(rrids) > 1
            if conflict:
                stats["rrid_conflicts"] += 1
            backfill = self.plan_backfill(survivor, losers)
            img_move, img_drop = self.plan_images(survivor, losers)

            self.stdout.write(f"\n[merge] {survivor.catalogue_number!r} | "
                              f"{survivor.target.gene_name or survivor.target.protein_name} | "
                              f"-> {'Novus' if target_company == novus.id else 'R&D'}")
            self.stdout.write(f"    survivor id={survivor.id} (data={payload(survivor)}, "
                              f"company {fam.get(survivor.company_id).name if survivor.company_id in fam else survivor.company_id})")
            for l in losers:
                self.stdout.write(f"    loser    id={l.id} (data={payload(l)})")
            if img_drop:
                self.stdout.write(f"    drop {len(img_drop)} duplicate image record(s) (file kept)")
            if backfill:
                self.stdout.write(f"    backfill: {', '.join(backfill.keys())}")
            if conflict:
                self.stdout.write(f"    !! RRID CONFLICT — survivor keeps {survivor.rrid!r}; "
                                  f"others: {[r for r in rrids if r != survivor.rrid]} — REVIEW")

            stats["merged_groups"] += 1
            stats["rows_removed"] += len(losers)
            stats["images_dropped"] += len(img_drop)

            if not apply:
                continue
            try:
                with transaction.atomic(using="pipeline_db"):
                    if survivor.company_id != target_company:
                        survivor.company_id = target_company
                    for rel in SIMPLE_RELATIONS:
                        for loser in losers:
                            getattr(loser, rel).update(antibody=survivor)
                    for img in img_move:
                        img.antibody = survivor
                        img.save(using="pipeline_db", update_fields=["antibody"])
                    for img in img_drop:
                        img.delete(using="pipeline_db")
                    for field, val in backfill.items():
                        setattr(survivor, field, val)
                    survivor.save(using="pipeline_db")
                    for loser in losers:
                        loser.delete(using="pipeline_db")
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"    !! ERROR: {exc!r} — group rolled back")

        # report drained records
        self.stdout.write("\n" + "=" * 72)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    antibodies reassigned   : {stats['reassigned']}")
        self.stdout.write(f"    duplicate groups merged : {stats['merged_groups']}")
        self.stdout.write(f"    rows removed            : {stats['rows_removed']}")
        self.stdout.write(f"    duplicate images dropped: {stats['images_dropped']}")
        self.stdout.write(f"    RRID conflicts to review: {stats['rrid_conflicts']}")
        for c in fam.values():
            if c.id not in (novus.id, rnd.id):
                remaining = Antibody.objects.filter(company_id=c.id).count() if apply \
                    else "(run --apply to drain)"
                self.stdout.write(f"    generic record id{c.id} {c.name!r} remaining antibodies: {remaining}")
        if not apply:
            self.stdout.write("\n  DRY-RUN. No data changed. Back up, then rerun with --apply.")
        self.stdout.write("=" * 72)
