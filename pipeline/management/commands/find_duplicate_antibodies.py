"""
Read-only report: which antibodies appear more than once in the pipeline
database, and which copy is the one to keep.

This CHANGES NOTHING. It is the "what have we got?" step you run before
`merge_duplicate_antibodies`, and the periodic check to run after a bulk import.

The report has two halves:

1. METADATA duplicates — defined in `pipeline/services/duplicates.py`: same
   RRID, or same gene + supplier + catalogue number, or same gene + supplier +
   clone ID (case- and punctuation-insensitive on both codes).

2. SHARED IMAGES — rows pointing at the same publication image FILE in object
   storage. This is the strongest signal there is: a genuinely separate reagent
   has its own blot, so two rows backed by one figure are one antibody entered
   twice. It also catches pairs whose catalogue and RRID were both typed
   differently, which no metadata signal can match.

   Where such a group has exactly one row carrying OGA recommendations, the
   other row(s) can be removed without losing a published verdict — the report
   counts these separately as "safe to remove".

By default the report is limited to PUBLIC duplicates: rows carrying at least
one publication image, i.e. the rows that actually show on a gene page, which
is where a duplicate is visible to users. Pass --all to sweep the whole table
including internal-only rows.

USAGE (run in the Render shell)
  python manage.py find_duplicate_antibodies              # public duplicates
  python manage.py find_duplicate_antibodies --all        # whole table
  python manage.py find_duplicate_antibodies --gene SOD1  # one gene
  python manage.py find_duplicate_antibodies --signal rrid
  python manage.py find_duplicate_antibodies --images-only   # just the count
"""

import os

from django.core.management.base import BaseCommand, CommandError

from pipeline.models import Antibody
from pipeline.rrid_utils import normalize_rrid
from pipeline.services.duplicates import (
    RECOMMENDATION_FIELDS,
    canonical_code,
    SIGNAL_LABELS,
    SIGNALS,
    find_duplicate_groups,
    group_by_shared_image,
    image_files,
    payload_count,
    recommendations,
)


class Command(BaseCommand):
    help = ("Report duplicate Antibody rows (read-only). Run before "
            "merge_duplicate_antibodies.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Include rows with no publication images (internal-only records). "
                 "Default is public rows only — the ones visible on a gene page.")
        parser.add_argument(
            "--gene", default=None,
            help="Restrict to one gene symbol, e.g. --gene SOD1.")
        parser.add_argument(
            "--signal", action="append", choices=sorted(SIGNALS), default=None,
            help="Only use this duplicate signal (repeatable). Default: all of them.")
        parser.add_argument(
            "--images-only", action="store_true",
            help="Skip the metadata section and report only rows sharing a "
                 "publication image file.")

    # ----- helpers ---------------------------------------------------------

    def row_lines(self, ab, keeper_id):
        """
        The report block for one row of a duplicate group. `keeper_id` is None
        for groups that need manual review — there we mark every row neutrally
        rather than implying one of them is safe to drop.
        """
        recs = recommendations(ab)
        images = sorted(ab.publication_images.values_list("application_type", flat=True))
        if keeper_id is None:
            marker = "REVIEW"
        else:
            marker = "KEEP  " if ab.id == keeper_id else "DROP  "
        lines = [
            f"    {marker} id={ab.id}  {ab.catalogue_number or '(no catalogue)'}  "
            f"rrid={normalize_rrid(ab.rrid) or '(none)'}",
            f"           supplier={ab.company.name if ab.company else '(none)'!r:<28} "
            f"host={ab.host_species!r} clonality={ab.clonality} clone={ab.clone_id or '-'}",
            f"           data records={payload_count(ab)}  "
            f"images={','.join(images) or '(none)'}  "
            f"recommended={','.join(recs) or '(none)'}",
            f"           added={ab.created_at:%Y-%m-%d} link={ab.supplier_url or '(none)'}",
        ]
        return lines

    def choose_keeper(self, rows):
        """
        Same rule the merge uses: the row owning the most data survives (fewest
        records to move), lowest id breaks a tie. Reported here so the two
        commands never disagree about which copy is the keeper.
        """
        return max(rows, key=lambda r: (payload_count(r), -r.id))

    # ----- main ------------------------------------------------------------

    def handle(self, *args, **opts):
        if Antibody.objects.db != "pipeline_db":
            raise CommandError(
                f"Antibody routes to '{Antibody.objects.db}', expected 'pipeline_db'. "
                f"Aborting to avoid reading the wrong database."
            )

        scope = "whole table" if opts["all"] else "public rows only (have publication images)"
        qs = Antibody.objects.select_related("company", "target")
        if not opts["all"]:
            qs = qs.filter(publication_images__isnull=False).distinct()
        if opts["gene"]:
            qs = qs.filter(target__gene_name__iexact=opts["gene"])
            scope += f", gene={opts['gene'].upper()}"

        rows = list(qs)
        signals = opts["signal"] or sorted(SIGNALS)
        groups = [] if opts["images_only"] else find_duplicate_groups(rows, signals)

        self.stdout.write("=" * 78)
        self.stdout.write("  find_duplicate_antibodies — READ-ONLY, nothing is changed")
        self.stdout.write(f"  scope   : {scope}")
        self.stdout.write(f"  rows    : {len(rows)}")
        if not opts["images_only"]:
            self.stdout.write("  signals : " + ", ".join(f"{s} ({SIGNAL_LABELS[s]})"
                                                         for s in signals))
        self.stdout.write("=" * 78)

        public_groups = mergeable = removable = review = 0
        for entry in groups:
            group = entry["rows"]
            visible = [r for r in group if r.publication_images.exists()]
            if len(visible) > 1:
                public_groups += 1
            target = group[0].target
            gene = target.gene_name or target.protein_name

            # Every condition the merge refuses to act on. When any holds, these
            # rows are not one product listed twice — they are different products
            # wrongly matched, so no row is "the one to drop". Kept in step with
            # merge_duplicate_antibodies so this count never invites a run that
            # the merge would reject.
            flags = []
            if len({r.target_id for r in group}) > 1:
                flags.append("SPANS MULTIPLE GENES — do not merge, fix by hand")
            if len({r.company_id for r in group}) > 1:
                flags.append("SPANS MULTIPLE SUPPLIERS — likely a wrong RRID on one row, "
                             "fix by hand")
            clones = {canonical_code(r.clone_id) for r in group} - {None}
            if len(clones) > 1:
                flags.append(f"DIFFERENT CLONE IDs {sorted(clones)} — different products, "
                             "fix the clone or the RRID by hand")
            # Separate figures for the same application means the rows were
            # characterised independently: two real reagents (often one clone in
            # two formats, e.g. a BSA/azide-free variant), not one entered twice.
            appfiles = {}
            for r in group:
                for im in r.publication_images.all():
                    appfiles.setdefault(im.application_type, set()).add(
                        os.path.basename(im.image.name or ""))
            if any(len(v) > 1 for v in appfiles.values()):
                flags.append("SEPARATE FIGURES for the same application — independently "
                             "characterised, NOT a duplicate; leave both")
            if flags:
                review += 1
                keeper_id = None
            else:
                mergeable += 1
                removable += len(group) - 1
                keeper_id = self.choose_keeper(group).id
            if len(visible) > 1:
                flags.append(f"{len(visible)} copies are LIVE on the {gene} gene page")

            self.stdout.write(
                f"\n[{gene}] {len(group)} rows — matched on: "
                + ", ".join(SIGNAL_LABELS[s] for s in entry["signals"]))
            for flag in flags:
                self.stdout.write(f"    !! {flag}")
            for ab in group:
                for line in self.row_lines(ab, keeper_id):
                    self.stdout.write(line)

        # ----- shared publication images ----------------------------------
        img_groups = group_by_shared_image(rows)
        safe, ambiguous, none_recommended, safe_rows = [], [], [], 0
        for group in img_groups:
            with_recs = [r for r in group if recommendations(r)]
            if len(with_recs) == 1:
                safe.append((group, with_recs[0]))
                safe_rows += len(group) - 1
            elif not with_recs:
                none_recommended.append(group)
            else:
                ambiguous.append(group)

        self.stdout.write("\n" + "=" * 78)
        self.stdout.write("  ROWS SHARING A PUBLICATION IMAGE FILE")
        self.stdout.write("  Same figure in object storage => the same antibody entered twice.")
        self.stdout.write("=" * 78)

        def image_block(group, keeper_id, note):
            gene = group[0].target.gene_name or group[0].target.protein_name
            shared = sorted(set.intersection(*[image_files(r) for r in group]) or
                            set().union(*[image_files(r) for r in group]))
            self.stdout.write(f"\n[{gene}] {len(group)} rows — {note}")
            self.stdout.write(f"    shared file(s): {', '.join(shared)}")
            for ab in group:
                for line in self.row_lines(ab, keeper_id):
                    self.stdout.write(line)

        for group, keeper in safe:
            image_block(group, keeper.id,
                        "one row has recommendations, the rest have none")
        for group in ambiguous:
            image_block(group, None,
                        "MORE THAN ONE row carries recommendations — reconcile by hand")
        for group in none_recommended:
            image_block(group, None,
                        "NO row carries a recommendation — pick the keeper by hand")

        self.stdout.write("\n" + "=" * 78)
        self.stdout.write("  SUMMARY")
        if not opts["images_only"]:
            self.stdout.write("  metadata duplicates (catalogue / RRID / clone)")
            self.stdout.write(f"    duplicate groups                 : {len(groups)}")
            self.stdout.write(f"    ...showing twice on a gene page  : {public_groups}")
            self.stdout.write(f"    groups the merge would accept    : {mergeable}")
            self.stdout.write(f"    extra rows those would remove    : {removable}")
            self.stdout.write(f"    groups the merge REFUSES         : {review}")
        self.stdout.write("  rows sharing a publication image file")
        self.stdout.write(f"    groups sharing an image          : {len(img_groups)}")
        self.stdout.write(f"    ...exactly one row recommended   : {len(safe)}"
                          f"   <- SAFE TO REMOVE THE OTHERS")
        self.stdout.write(f"    ...rows removable in those groups: {safe_rows}")
        self.stdout.write(f"    ...several rows recommended      : {len(ambiguous)}")
        self.stdout.write(f"    ...no row recommended            : {len(none_recommended)}")

        if not groups and not img_groups:
            self.stdout.write("\n  ✓ No duplicates found in this scope.")
        else:
            self.stdout.write(
                "\n  Nothing was changed. To act on these, back up the database, then:\n"
                "    python manage.py merge_duplicate_antibodies --strategy cat --gene <GENE>\n"
                "  (dry-run; add --apply --include-risky when the plan looks right).\n"
                "  For an image-only match the catalogue/RRID may differ, so merge it by id:\n"
                "    python manage.py merge_duplicate_antibodies --strategy cat --ids <a>,<b> \\\n"
                "        --prefer-recommended --apply --include-risky")
        self.stdout.write("=" * 78)
