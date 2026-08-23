"""
Standardise the stored RRID format on pipeline Antibody rows.

Why (see AUDIT.md §3): RRIDs are stored inconsistently — some as the bare
"AB_2764072", some as the full URL "https://www.antibodyregistry.org/AB_2764072",
plus junk placeholders like "?". A read-only scan on 2026-07-13 found that at
least 55 duplicate groups exist ONLY because of this format inconsistency.

This command is a PURE FORMATTING FIX. It merges no rows, deletes no rows, and
loses no data:
  * every recognisable RRID -> canonical bare form  AB_<digits>
  * rrid_link                -> canonical registry URL for that id
  * junk placeholders ("?", "n/a", ...) -> reported, and cleared to "" only if
    you pass --clear-junk
  * unrecognised non-AB values -> reported and left untouched (never guessed)

It changes only the `rrid` / `rrid_link` text fields, so it cannot affect the
antibody uniqueness constraint or any child data. It is a sensible step BEFORE
any row-merge, because it makes RRID matching (and the public display) reliable.

SAFETY MODEL
  * Defaults to --dry-run: prints every change it would make, writes nothing.
  * --apply writes, inside a single transaction (all-or-nothing).
  * Guarded to pipeline_db only.
  * Fully reversible with a pg_dump backup taken before --apply.

USAGE (run in the Render shell)
  python manage.py standardize_rrid_format                 # dry-run
  python manage.py standardize_rrid_format --clear-junk    # dry-run, incl. junk
  python manage.py standardize_rrid_format --apply
  python manage.py standardize_rrid_format --apply --clear-junk
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Antibody
# Single source of truth for RRID parsing.
from pipeline.rrid_utils import normalize_rrid, registry_url, JUNK_RRID


class Command(BaseCommand):
    help = "Standardise RRID format to canonical AB_<n> (dry-run by default). See AUDIT.md §3."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes. Without this it is a read-only dry-run.")
        parser.add_argument("--clear-junk", action="store_true",
                            help="Also blank out junk placeholder RRIDs ('?', 'n/a', ...).")
        parser.add_argument("--show", type=int, default=25,
                            help="How many example changes to print (default 25).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        clear_junk = opts["clear_junk"]
        show = opts["show"]

        if Antibody.objects.db != "pipeline_db":
            raise CommandError(
                f"Antibody routes to '{Antibody.objects.db}', expected 'pipeline_db'. "
                f"Aborting to avoid touching the wrong database."
            )

        banner = "APPLY (writing changes)" if apply else "DRY-RUN (no changes)"
        self.stdout.write("=" * 70)
        self.stdout.write(f"  standardize_rrid_format — {banner}")
        self.stdout.write(f"  clear-junk = {clear_junk}")
        self.stdout.write("=" * 70)

        stats = dict(
            total=0, already_canonical=0, reformatted=0, link_filled=0,
            junk=0, junk_cleared=0, unrecognised=0, blank=0,
        )
        to_save = []          # (antibody, update_fields)
        examples = []
        unrecognised_ex = []

        for ab in Antibody.objects.all():
            stats["total"] += 1
            raw = (ab.rrid or "").strip()
            bare = normalize_rrid(raw)

            if bare:
                new_link = registry_url(bare)
                fields = []
                if ab.rrid != bare:
                    fields.append("rrid")
                if ab.rrid_link != new_link:
                    fields.append("rrid_link")
                    if not (ab.rrid_link or "").strip():
                        stats["link_filled"] += 1
                if fields:
                    stats["reformatted"] += 1
                    if len(examples) < show:
                        examples.append(
                            (ab.id, ab.rrid, ab.rrid_link, bare, new_link, fields)
                        )
                    ab.rrid, ab.rrid_link = bare, new_link
                    to_save.append((ab, fields))
                else:
                    stats["already_canonical"] += 1
                continue

            # No AB_<n> could be extracted.
            if raw == "":
                stats["blank"] += 1
            elif raw.upper() in JUNK_RRID:
                stats["junk"] += 1
                if clear_junk and (ab.rrid or ab.rrid_link):
                    stats["junk_cleared"] += 1
                    ab.rrid, ab.rrid_link = "", ""
                    to_save.append((ab, ["rrid", "rrid_link"]))
            else:
                stats["unrecognised"] += 1
                if len(unrecognised_ex) < 15:
                    unrecognised_ex.append((ab.id, ab.rrid))

        # ----- report -----
        if examples:
            self.stdout.write("\n  Example reformats (id: old -> new):")
            for _id, old_r, old_l, new_r, new_l, fields in examples:
                self.stdout.write(f"    id={_id}")
                if "rrid" in fields:
                    self.stdout.write(f"        rrid      {old_r!r} -> {new_r!r}")
                if "rrid_link" in fields:
                    self.stdout.write(f"        rrid_link {old_l!r} -> {new_l!r}")
        if unrecognised_ex:
            self.stdout.write("\n  Unrecognised RRIDs (left untouched — review by hand):")
            for _id, val in unrecognised_ex:
                self.stdout.write(f"    id={_id}: {val!r}")

        if apply and to_save:
            with transaction.atomic(using="pipeline_db"):
                for ab, fields in to_save:
                    ab.save(using="pipeline_db", update_fields=fields + ["updated_at"])

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("  SUMMARY")
        self.stdout.write(f"    antibody rows scanned        : {stats['total']}")
        self.stdout.write(f"    already canonical            : {stats['already_canonical']}")
        self.stdout.write(f"    {'reformatted' if apply else 'to reformat'}"
                          f"{'':<18}: {stats['reformatted']}")
        self.stdout.write(f"      of which rrid_link filled  : {stats['link_filled']}")
        self.stdout.write(f"    blank (no RRID)              : {stats['blank']}")
        self.stdout.write(f"    junk placeholder ('?', ...)  : {stats['junk']}"
                          + (f"  ({'cleared' if apply else 'to clear'}: {stats['junk_cleared']})"
                             if clear_junk else "  (use --clear-junk to blank)"))
        self.stdout.write(f"    unrecognised (left as-is)    : {stats['unrecognised']}")
        if not apply:
            self.stdout.write("\n  This was a DRY-RUN. No data changed. "
                              "Back up, then rerun with --apply.")
        self.stdout.write("=" * 70)
