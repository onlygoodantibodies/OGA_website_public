"""
Merge duplicate Antibody rows in the pipeline PostgreSQL database.

Background (see AUDIT.md §3): the live pipeline dataset was built by merging
three sources (legacy core SQLite, the Access DB, the Leicester Excel). An
antibody that appeared in more than one source became several rows, each row
often carrying a *different slice* of the same antibody's characterisation data
(WB/IP/IF/FC results, publication images, inventory locations).

This is therefore a MERGE, never a DELETE. For each group of duplicate rows we:
  1. pick a survivor,
  2. re-point every child record (results, images, locations) onto it,
  3. backfill the survivor's blank fields from the losers,
  4. delete the now-empty loser rows,
all inside a single database transaction per group.

To see what is duplicated before merging anything, run the read-only report
first: `python manage.py find_duplicate_antibodies`.

STRATEGIES
  cat_lot  same company + catalogue + lot, compared EXACTLY. Phase A: the
           narrowest, most unambiguous signal.
  cat      same gene + company + catalogue, any lot, catalogue compared
           case/punctuation-insensitively ("MAB-3418" == "mab3418"). This is the
           signal that catches one product listed twice under two RRIDs.
  clone    same gene + company + clone ID. Catches a clone re-listed under a
           second catalogue number (e.g. an R&D -> Bio-Techne rebrand).
  rrid     same RRID, NORMALISED. RRIDs are stored inconsistently — some as the
           bare "AB_2764072", some as the full URL
           "https://www.antibodyregistry.org/AB_2764072". We canonicalise to the
           bare AB_ id before grouping, so format-variant duplicates are caught.
           Junk placeholders ("?", "n/a", blank, ...) are treated as MISSING and
           never matched.
  image    rows pointing at the same publication image FILE. The strongest
           signal: a separate reagent has its own blot, so one figure behind two
           rows means one antibody entered twice. Catches pairs whose catalogue
           AND RRID were both typed differently, and pairs recorded under two
           lot numbers — which no metadata signal can separate from two genuine
           vials. Pair it with --prefer-recommended.

The `cat`, `clone`, `rrid` and `image` groupings live in
`pipeline/services/duplicates.py`, so this command acts on exactly the groups
`find_duplicate_antibodies` reports.

SURVIVOR
  By default the survivor is the row owning the most child records (fewest to
  move), lowest id breaking a tie. --prefer-recommended puts a row carrying OGA
  recommendations ahead of that, so a published verdict is never the thing that
  gets deleted. Either way it is a MERGE: the survivor inherits the losers'
  results, images, locations, and any field it had blank — including an RRID the
  survivor was missing.

SAFETY MODEL
  * Defaults to --dry-run: prints exactly what it *would* do and changes nothing.
    You must pass --apply to write.
  * Even with --apply, groups that carry data on more than one row ("risky") are
    reported but NOT merged unless you also pass --include-risky.
  * ALWAYS skipped, never mergeable: groups spanning more than one Target
    (a merge would move an antibody to another gene page), groups spanning more
    than one supplier, and groups carrying two different CLONE IDs (that is two
    different products sharing a wrong RRID, not a duplicate).
  * --only-single-recommended restricts the run to groups where exactly ONE row
    carries an OGA recommendation — the "safe to remove the others" rule. Groups
    with none or several recommended rows are skipped for a human to decide.
  * --gene / --ids narrow the sweep so one known duplicate can be fixed on its
    own, without touching the rest of the database.
  * Take a backup first: pg_dump the pipeline DB (or use Render's DB backup)
    before running with --apply. Everything up to --apply is reversible.

USAGE (run in the Render shell)
  python manage.py merge_duplicate_antibodies                 # dry-run, cat+lot
  python manage.py merge_duplicate_antibodies --strategy rrid # dry-run, by RRID
  python manage.py merge_duplicate_antibodies --apply         # merge safe groups
  python manage.py merge_duplicate_antibodies --apply --include-risky
  # one gene only, e.g. the duplicated SOD1 MAB3418:
  python manage.py merge_duplicate_antibodies --strategy cat --gene SOD1
  python manage.py merge_duplicate_antibodies --strategy cat --gene SOD1 \
      --apply --include-risky
  # every shared-figure duplicate whose keeper the recommendation decides:
  python manage.py merge_duplicate_antibodies --strategy image \
      --only-single-recommended --prefer-recommended
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Antibody
from pipeline.rrid_utils import normalize_rrid
from pipeline.services.duplicates import (
    CHILD_RELATIONS,
    SIGNALS,
    canonical_code,
    group_by_shared_image,
    image_files,
    payload_count,
    recommendations,
)

STRATEGY_CHOICES = ("cat_lot", "cat", "clone", "rrid", "image")

# Fields never touched during backfill: identity / audit / unique-key fields.
# catalogue_number, lot_number and site are part of the unique constraint
# (catalogue, company, target, lot, site) — backfilling a blank one from a loser
# can push the survivor onto another row's key and raise IntegrityError, so the
# survivor always keeps its own. site is also a fact about the surviving row
# (which lab holds this vial), not a blank waiting to be filled from elsewhere.
NEVER_BACKFILL = {
    "id",
    "access_id",       # unique
    "created_at",
    "updated_at",
    "ab_number",       # source-specific legacy counter; not content
    "catalogue_number",  # unique-key field
    "lot_number",        # unique-key field
    "site",              # unique-key field
    "site_id",
}

# Positive-information booleans: a True on EITHER row should survive the merge
# (an application recommended / supplier-validated somewhere stays so). All
# other booleans keep the survivor's value.
OR_BOOL_FIELDS = {
    "wb_recommended", "ip_recommended", "if_recommended", "fc_recommended",
    "supplier_validated_wb", "supplier_validated_ip", "supplier_validated_if",
    "supplier_validated_ihc", "supplier_validated_elisa", "supplier_validated_fc",
    "is_recombinant",
}


def group_key(ab, strategy):
    """The duplicate-grouping key for a row, or None to exclude it."""
    if strategy == "cat_lot":
        # Legacy Phase A key: exact strings, lot included, target not consulted
        # (multi-target groups are caught and skipped downstream).
        cat = (ab.catalogue_number or "").strip()
        if not cat:
            return None
        return (ab.company_id, cat, (ab.lot_number or "").strip())
    if strategy == "cat":
        return SIGNALS["catalogue"](ab)
    if strategy in SIGNALS:
        return SIGNALS[strategy](ab)
    raise ValueError(strategy)


def is_empty(value):
    """True if a field value carries no information (safe to backfill over)."""
    return value is None or value == "" or value == {}


class Command(BaseCommand):
    help = "Merge duplicate Antibody rows (dry-run by default). See AUDIT.md §3."

    def add_arguments(self, parser):
        parser.add_argument("--strategy", choices=STRATEGY_CHOICES, default="cat_lot",
                            help="Duplicate signal to group by (default: cat_lot = Phase A).")
        parser.add_argument("--apply", action="store_true",
                            help="Actually perform the merges. Without this it is a read-only dry-run.")
        parser.add_argument("--include-risky", action="store_true",
                            help="Also merge groups that carry data on more than one row.")
        parser.add_argument("--limit", type=int, default=None,
                            help="Only process the first N duplicate groups (for testing).")
        parser.add_argument("--gene", default=None,
                            help="Only consider antibodies against this gene symbol, "
                                 "e.g. --gene SOD1. Narrows the sweep to one gene page.")
        parser.add_argument("--ids", default=None,
                            help="Only consider these antibody row ids (comma-separated). "
                                 "Use to merge one known pair and nothing else.")
        parser.add_argument("--only-single-recommended", action="store_true",
                            help="Only merge groups where EXACTLY ONE row carries an OGA "
                                 "recommendation, so no published verdict can be lost. "
                                 "Groups with none or several are skipped for review.")
        parser.add_argument("--prefer-recommended", action="store_true",
                            help="Keep the row carrying OGA recommendations, even if "
                                 "another row owns more records. Use with --strategy image.")

    # ----- helpers ---------------------------------------------------------

    def scoped_rows(self, gene, ids):
        """The antibody rows this run is allowed to touch."""
        qs = Antibody.objects.all()
        if gene:
            qs = qs.filter(target__gene_name__iexact=gene)
        if ids:
            qs = qs.filter(id__in=ids)
        return qs

    def find_groups(self, strategy, rows):
        """Return a list of row-lists, one per duplicate group (>1 row)."""
        if strategy == "image":
            # Not expressible as a per-row key: it depends on the image files a
            # row owns, so it clusters transitively instead of bucketing.
            return group_by_shared_image(list(rows))
        buckets = {}
        for ab in rows:
            key = group_key(ab, strategy)
            if key is None:
                continue
            buckets.setdefault(key, []).append(ab)
        # Deterministic order: by key so dry-run and apply agree run to run.
        return [buckets[k] for k in sorted(buckets, key=str) if len(buckets[k]) > 1]

    def choose_survivor(self, rows, prefer_recommended=False):
        """
        Survivor = most attached data (fewest to move), tie-break lowest id.

        With prefer_recommended, a row carrying OGA recommendations outranks
        that, so a published verdict is never what gets deleted. The merge still
        backfills every blank field on the survivor from the losers, so keeping
        the recommended row does not lose an RRID the other row had.
        """
        def rank(r):
            base = (payload_count(r), -r.id)
            return (bool(recommendations(r)),) + base if prefer_recommended else base
        return max(rows, key=rank)

    def plan_images(self, survivor, losers):
        """
        Plan how to reconcile loser publication images against the unique
        (antibody, application_type) constraint.

        Returns (to_move, to_delete):
          * to_move   — loser images whose application the survivor lacks; these
                        are re-pointed onto the survivor (reunites a missing app).
          * to_delete — loser images duplicating an application the survivor
                        already has; the DB record is removed but the file on
                        disk stays (still referenced by the survivor's record).
        """
        seen = set(survivor.publication_images.values_list("application_type", flat=True))
        to_move, to_delete = [], []
        for loser in losers:
            for img in loser.publication_images.all():
                if img.application_type in seen:
                    to_delete.append(img)
                else:
                    to_move.append(img)
                    seen.add(img.application_type)
        return to_move, to_delete

    def plan_backfill(self, survivor, losers):
        """Which survivor fields to fill from losers: {field: (old, new)}."""
        changes = {}
        concrete = [f for f in Antibody._meta.get_fields()
                    if getattr(f, "concrete", False) and not f.many_to_many]
        for field in concrete:
            name, attname = field.name, field.attname
            if name in NEVER_BACKFILL or attname in NEVER_BACKFILL:
                continue
            if name in OR_BOOL_FIELDS:
                if not getattr(survivor, name) and any(getattr(l, name) for l in losers):
                    changes[name] = (getattr(survivor, name), True)
                continue
            current = getattr(survivor, attname)
            if not is_empty(current):
                continue
            for loser in losers:
                candidate = getattr(loser, attname)
                if not is_empty(candidate):
                    changes[attname] = (current, candidate)
                    break
        return changes

    def label_for(self, rows, strategy):
        r = rows[0]
        gene = r.target.gene_name or r.target.protein_name
        if strategy == "rrid":
            return f"rrid={normalize_rrid(r.rrid)!r} ({gene})"
        if strategy == "cat_lot":
            return (f"company_id={r.company_id} catalogue={r.catalogue_number!r} "
                    f"lot={r.lot_number!r}")
        if strategy == "clone":
            return f"{gene} company_id={r.company_id} clone={r.clone_id!r}"
        if strategy == "image":
            shared = sorted(set.intersection(*[image_files(x) for x in rows]) or
                            set().union(*[image_files(x) for x in rows]))
            return f"{gene} shared image(s): {', '.join(shared)}"
        return f"{gene} company_id={r.company_id} catalogue={r.catalogue_number!r}"

    # ----- main ------------------------------------------------------------

    def handle(self, *args, **opts):
        strategy, apply = opts["strategy"], opts["apply"]
        include_risky, limit = opts["include_risky"], opts["limit"]

        if Antibody.objects.db != "pipeline_db":
            raise CommandError(
                f"Antibody routes to '{Antibody.objects.db}', expected 'pipeline_db'. "
                f"Aborting to avoid touching the wrong database."
            )

        ids = None
        if opts["ids"]:
            try:
                ids = [int(part) for part in opts["ids"].split(",") if part.strip()]
            except ValueError:
                raise CommandError("--ids must be a comma-separated list of row ids.")
            if not ids:
                raise CommandError("--ids was empty.")

        rows = self.scoped_rows(opts["gene"], ids)
        scope = "whole database"
        if opts["gene"] or ids:
            parts = ([f"gene={opts['gene'].upper()}"] if opts["gene"] else []) \
                + ([f"ids={ids}"] if ids else [])
            scope = " ".join(parts) + f" ({rows.count()} rows)"

        banner = "APPLY (writing changes)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 70)
        self.stdout.write(f"  merge_duplicate_antibodies — {banner}")
        self.stdout.write(f"  strategy = {strategy}   include-risky = {include_risky}")
        self.stdout.write(f"  scope    = {scope}")
        self.stdout.write("=" * 70)

        groups = self.find_groups(strategy, rows)
        if limit is not None:
            groups = groups[:limit]

        stats = dict(groups=len(groups), merged=0, rows_removed=0, children_moved=0,
                     images_deduped=0, skipped_risky=0, skipped_multi_target=0,
                     skipped_multi_vendor=0, skipped_image_conflict=0,
                     skipped_clone_conflict=0, skipped_not_single_recommended=0,
                     errors=0)

        for rows in groups:
            label = self.label_for(rows, strategy)
            targets = {r.target_id for r in rows}
            data_rows = [r for r in rows if payload_count(r) > 0]
            risky = len(data_rows) > 1

            if len(targets) > 1:
                stats["skipped_multi_target"] += 1
                self.stdout.write(
                    f"\n[SKIP: multiple targets] {label}\n"
                    f"    rows {[r.id for r in rows]} span targets {sorted(targets)} "
                    f"— needs manual review."
                )
                continue

            # Rows under different companies sharing one RRID => almost certainly a
            # WRONG rrid on one row (different products), not a duplicate. Never merge.
            if len({r.company_id for r in rows}) > 1:
                stats["skipped_multi_vendor"] += 1
                self.stdout.write(
                    f"\n[SKIP: multiple vendors] {label}\n"
                    f"    rows {[r.id for r in rows]} span companies "
                    f"{sorted({r.company_id for r in rows})} — likely a wrong RRID, fix by hand."
                )
                continue

            # Two DIFFERENT clone IDs is two different products, whatever else
            # matches. This is the shape of a wrong RRID copied onto a second
            # reagent (real case: Abcam ab2730 carrying AB_303255 for both clone
            # AP6 and EPR2688(2)). A blank clone on one row is not a conflict.
            clones = {canonical_code(r.clone_id) for r in rows} - {None}
            if len(clones) > 1:
                stats["skipped_clone_conflict"] += 1
                self.stdout.write(
                    f"\n[SKIP: clone conflict] {label}\n"
                    f"    rows {[r.id for r in rows]} carry different clone IDs "
                    f"{sorted(clones)} — different products, fix the RRID by hand."
                )
                continue

            # The stated safe rule: exactly one row carries an OGA recommendation,
            # so removing the others cannot lose a published verdict. Groups where
            # none or several rows are recommended need a human to pick the keeper.
            if opts["only_single_recommended"]:
                recd = [r for r in rows if recommendations(r)]
                if len(recd) != 1:
                    stats["skipped_not_single_recommended"] += 1
                    self.stdout.write(
                        f"\n[SKIP: {'no' if not recd else len(recd)} recommended rows] {label}\n"
                        f"    rows {[r.id for r in rows]} — the keeper is not decided by "
                        f"the recommendation, choose it by hand."
                    )
                    continue

            # Different image FILES for the same application across rows => different
            # products (e.g. two clones sharing an RRID). Merging would drop a real
            # image, so skip for manual review.
            appfiles = {}
            for r in rows:
                for im in r.publication_images.all():
                    appfiles.setdefault(im.application_type, set()).add(
                        os.path.basename(im.image.name or ""))
            if any(len(v) > 1 for v in appfiles.values()):
                stats["skipped_image_conflict"] += 1
                self.stdout.write(
                    f"\n[SKIP: image conflict] {label}\n"
                    f"    rows {[r.id for r in rows]} have different image files for the "
                    f"same application — different products, review by hand."
                )
                continue

            survivor = self.choose_survivor(rows, opts["prefer_recommended"])
            losers = [r for r in rows if r.id != survivor.id]
            backfill = self.plan_backfill(survivor, losers)
            # Publication images obey a unique (antibody, app) constraint, so they
            # are reconciled separately; the rest re-point by simple FK update.
            simple_rels = [r for r in CHILD_RELATIONS if r != "publication_images"]
            moves = {rel: sum(getattr(l, rel).count() for l in losers) for rel in simple_rels}
            moves = {rel: n for rel, n in moves.items() if n}
            img_move, img_dupe = self.plan_images(survivor, losers)

            self.stdout.write(f"\n[{'RISKY' if risky else 'safe'}] {label}")
            self.stdout.write(f"    survivor id={survivor.id} (owns {payload_count(survivor)} records)")
            for l in losers:
                self.stdout.write(f"    loser    id={l.id} (owns {payload_count(l)} records) -> merge in")
            if moves:
                self.stdout.write("    move children: " + ", ".join(f"{r}+{n}" for r, n in moves.items()))
            if img_move:
                self.stdout.write(f"    move publication_images (survivor lacked app): +{len(img_move)}")
            if img_dupe:
                self.stdout.write(f"    drop duplicate image records (same file kept on survivor): {len(img_dupe)}")
            if backfill:
                self.stdout.write("    backfill: " + ", ".join(f"{f} <- {new!r}"
                                  for f, (old, new) in backfill.items()))

            if risky and not include_risky:
                stats["skipped_risky"] += 1
                self.stdout.write("    >> SKIPPED: data on multiple rows; rerun with "
                                  "--include-risky to merge, or reconcile by hand.")
                continue

            if not apply:
                stats["merged"] += 1
                stats["rows_removed"] += len(losers)
                stats["children_moved"] += sum(moves.values()) + len(img_move)
                stats["images_deduped"] += len(img_dupe)
                continue

            try:
                with transaction.atomic(using="pipeline_db"):
                    moved = 0
                    for rel in simple_rels:
                        for loser in losers:
                            moved += getattr(loser, rel).update(antibody=survivor)
                    # Move images for apps the survivor lacked; delete duplicate
                    # records (the file stays, still referenced by the survivor).
                    for img in img_move:
                        img.antibody = survivor
                        img.save(using="pipeline_db", update_fields=["antibody"])
                        moved += 1
                    for img in img_dupe:
                        img.delete(using="pipeline_db")
                    if backfill:
                        for field, (old, new) in backfill.items():
                            setattr(survivor, field, new)
                        survivor.save(using="pipeline_db",
                                      update_fields=list(backfill.keys()) + ["updated_at"])
                    for loser in losers:
                        loser.delete(using="pipeline_db")
                stats["merged"] += 1
                stats["rows_removed"] += len(losers)
                stats["children_moved"] += moved
                stats["images_deduped"] += len(img_dupe)
                self.stdout.write(f"    >> MERGED: moved {moved} child records, "
                                  f"dropped {len(img_dupe)} duplicate image record(s), "
                                  f"removed {len(losers)} row(s).")
            except Exception as exc:  # noqa: BLE001 — report and continue
                stats["errors"] += 1
                self.stderr.write(f"    !! ERROR on {label}: {exc!r} — group rolled back, continuing.")

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    duplicate groups found       : {stats['groups']}")
        self.stdout.write(f"    groups {'merged' if apply else 'mergeable'}          : {stats['merged']}")
        self.stdout.write(f"    rows {'removed' if apply else 'removable'}            : {stats['rows_removed']}")
        self.stdout.write(f"    child records {'moved' if apply else 'to move'}      : {stats['children_moved']}")
        self.stdout.write(f"    duplicate image records {'dropped' if apply else 'to drop'} : {stats['images_deduped']}")
        self.stdout.write(f"    skipped (risky, >1 data row) : {stats['skipped_risky']}")
        self.stdout.write(f"    skipped (multi-target)       : {stats['skipped_multi_target']}")
        self.stdout.write(f"    skipped (multi-vendor/wrong RRID): {stats['skipped_multi_vendor']}")
        self.stdout.write(f"    skipped (image conflict)     : {stats['skipped_image_conflict']}")
        self.stdout.write(f"    skipped (clone conflict)     : {stats['skipped_clone_conflict']}")
        self.stdout.write(f"    skipped (not single-recommended): "
                          f"{stats['skipped_not_single_recommended']}")
        if apply:
            self.stdout.write(f"    errors (rolled back)         : {stats['errors']}")
        else:
            self.stdout.write("\n  This was a DRY-RUN. No data changed. "
                              "Back up, then rerun with --apply.")
        self.stdout.write("=" * 70)
