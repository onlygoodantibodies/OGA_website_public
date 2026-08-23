"""
Django management command: fix_cell_line_identity

The half of the August 2026 Access delta that `import_access_update` refuses to
touch: the 84 cells that change a cell line's **name** and the 21 that change
its **parental line**.

    python manage.py fix_cell_line_identity              # dry run
    python manage.py fix_cell_line_identity --apply
    python manage.py fix_cell_line_identity --names      # one half at a time
    python manage.py fix_cell_line_identity --parents

**A name is not a field on a cell line, it is what the cell line is.** Live
cell lines were deduplicated *by name* — `restructure_cell_lines` merged every
`(name, site)` wild type and every `(name, site, gene)` knockout into one row
and deleted the losers — so renaming `SKNFI` to `SK-N-FI` where a `SK-N-FI`
already exists is a **merge**, not a rename, and doing it silently would either
collide on the way in or leave two rows for one line with the vials split
between them. That is the same rule `fix_gene_case` holds for gene symbols and
`merge_targets` for targets, and for the same reason: the identifier is unique,
so typing the right value onto the second row runs into the first.

So this command renames only where the new name is free, and where it is not it
**refuses by name**, printing both rows and what each is carrying. Merging them
is a live-data merge and belongs with the other merges, behind the
`production-data` skill and a backup.

### What the 84 name changes actually are

Not renames — a normalisation to Cellosaurus's spelling, applied across the
whole freezer: `SKNFI` → `SK-N-FI`, `A431` → `A-431`, `HepG2` → `Hep G2`,
`PLCPRF5` → `PLC/PRF/5`, `SU8686` → `SU.86.86`. Worth having: `find_cell_line`
matches on Cellosaurus first, and six of these rows gained a `CVCL_` id in the
same export. 84 Access rows collapse to far fewer live rows, because the rows
sharing a name are the ones the dedup already merged.

### What the 21 parental changes are

Twenty rows move from `C-48` to `C-439` and one is blanked. A parent is
recorded as a **C-number** — 145 of the 169 on file are — so this resolves the
number through `cell_lines.by_c_number`, which reads the line's own number *and*
its freeze-down batches', and sets the `parent_line` FK as well as the text.
A number naming more than one line at this site is a question, not a coin toss:
it is refused with both candidates named.

Two rules carry over from `import_access_update`, for the same reasons:
a **blank never clears** a stored value, and a change is applied only if the
site still holds the value the export **expected** — otherwise somebody edited
it on a board since March and this command is not the authority.
"""

import os
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from pipeline.services import cell_lines as cell_lines_svc
from pipeline.services import sites as sites_svc
from pipeline.models import CellLine, CellLineVial, Site
from pipeline.management.commands.import_access_data import clean_str, clean_int, read_csv

DB = "pipeline_db"
DEFAULT_DIR = os.path.join("pipeline", "data", "access_update_2026_08")


class Command(BaseCommand):
    help = ("Apply the cell-line name and parental-line changes from the August "
            "2026 Access delta. Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument("--csv-dir", default=DEFAULT_DIR)
        parser.add_argument(
            "--site",
            help="Which bench these lines belong to — a name, short code or "
                 "pk. Defaults to the site the Access import put its rows on.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually write. Without it nothing is saved.")
        parser.add_argument("--names", action="store_true",
                            help="Only the name changes.")
        parser.add_argument("--parents", action="store_true",
                            help="Only the parental-line changes.")

    def handle(self, *args, **options):
        self.dry_run = not options["apply"]
        csv_dir = options["csv_dir"]
        if not os.path.isdir(csv_dir):
            raise CommandError(f"Directory not found: {csv_dir}")

        # Neither half asks for a number, but a save on `CellLine` goes through
        # the same `pre_save` as every other, and this command is not the place
        # a line acquires a C-number it never had.
        both = not (options["names"] or options["parents"])

        # Derived rather than assumed — see `import_access_update._resolve_site`
        # for why a hardcoded short code was wrong. Asked of the cell lines here
        # rather than the antibodies, because these are the rows being renamed.
        self.site = self._resolve_site(options.get("site"))
        self.stdout.write(f"  Bench: {self.site.name} "
                          f"({self.site.short_code}, id {self.site.pk})")

        self.notes = defaultdict(list)
        self.counts = defaultdict(int)

        rows = read_csv(csv_dir, "CellLines_changed.csv")
        by_column = defaultdict(list)
        for row in rows:
            by_column[clean_str(row["Column"])].append(row)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "Cell-line identity changes — August 2026 Access delta"))
        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing will be saved. Re-run with --apply to write."))

        with transaction.atomic(using=DB):
            if both or options["names"]:
                self._names(by_column["CellLine"])
            if both or options["parents"]:
                self._parents(by_column["ParentalLine"])
            if self.dry_run:
                transaction.set_rollback(True, using=DB)

        self._report()


    def _resolve_site(self, typed):
        """The bench whose cell lines these are."""
        if typed:
            site = sites_svc.resolve(typed, db=DB)
            if site is None:
                raise CommandError(sites_svc.refusal(typed, db=DB))
            return site
        from pipeline.management.commands.import_access_update import (
            Command as UpdateCommand)
        counts = UpdateCommand._access_site_counts()
        if counts:
            return Site.objects.using(DB).get(pk=counts[0]["site_id"])
        known = ", ".join(sites_svc.known_names(db=DB)) or "none"
        raise CommandError(
            "Cannot tell which bench these lines belong to: no cell line on "
            "this database carries an Access id. Sites on file: "
            f"{known}. Pass --site to name one.")

    # ------------------------------------------------------------------
    def _line_for(self, access_id):
        """The live `CellLine` an Access CellLines.ID means.

        The vial table is asked first: the dedup deleted the merged-away lines,
        so `CellLine.access_id` survives only on the row that won, while every
        original id is still on a freeze-down batch.
        """
        value = clean_int(access_id)
        if not value:
            return None
        vial = CellLineVial.objects.using(DB).filter(access_id=value).first()
        if vial:
            return vial.cell_line
        return CellLine.objects.using(DB).filter(access_id=value).first()

    # ------------------------------------------------------------------
    # Names
    # ------------------------------------------------------------------
    def _names(self, rows):
        self.stdout.write(f"\n  Name changes: {len(rows)} Access rows")

        # Several Access rows are batches of one live line, so collapse to the
        # live row first — otherwise the same rename is attempted ten times and
        # nine of them look like a collision with the one that just succeeded.
        wanted = {}
        for row in rows:
            line = self._line_for(row["ID"])
            if line is None:
                self.notes["skipped — cell line not on file"].append(
                    f"Access id {clean_str(row['ID'])} "
                    f"({clean_str(row['OldValue'])} → {clean_str(row['NewValue'])})")
                continue
            wanted.setdefault(line.pk, (line, clean_str(row["OldValue"]), set()))
            wanted[line.pk][2].add(clean_str(row["NewValue"]))

        self.stdout.write(f"    → {len(wanted)} live cell lines to consider")

        for line, expected, new_names in wanted.values():
            new_names = {n for n in new_names if n}
            if not new_names:
                self.counts["names left — export blanked them"] += 1
                self.notes["names the export blanked (kept as they are)"].append(
                    f"{line.name} (id {line.pk})")
                continue
            if len(new_names) > 1:
                self.counts["names refused"] += 1
                self.notes["names refused — the export disagrees with itself"].append(
                    f"{line.name} (id {line.pk}) → {sorted(new_names)}")
                continue
            self._rename(line, expected, new_names.pop())

    def _rename(self, line, expected, new_name):
        if line.name == new_name:
            self.counts["names already correct"] += 1
            return
        if expected and line.name != expected:
            self.counts["names refused"] += 1
            self.notes["names refused — the site has moved on"].append(
                f"id {line.pk}: export expected {expected!r}, site holds "
                f"{line.name!r}, {new_name!r} not applied")
            return

        # The key the dedup used, and therefore the key a rename can collide on.
        clash = (CellLine.objects.using(DB)
                 .filter(name__iexact=new_name, genotype=line.genotype,
                         target_id=line.target_id, site_id=line.site_id)
                 .exclude(pk=line.pk).first())
        if clash is not None:
            self.counts["names refused — would be a merge"] += 1
            self.notes["names refused — this is a merge, not a rename"].append(
                self._merge_message(line, clash, new_name))
            return

        line.name = new_name
        line.save(using=DB, update_fields=["name"])
        self.counts["names corrected"] += 1
        self.notes["names corrected"].append(
            f"{expected or '?'} → {new_name} (id {line.pk})")

    def _merge_message(self, line, clash, new_name):
        def carrying(obj):
            batches = CellLineVial.objects.using(DB).filter(cell_line_id=obj.pk).count()
            numbers = cell_lines_svc.batch_numbers(obj, db=DB)
            return (f"id {obj.pk} ({obj.name}, {batches} batch(es)"
                    + (f", {cell_lines_svc.format_batches(numbers)}" if numbers else "")
                    + ")")
        return (f"{line.name} → {new_name}: there is already a {new_name} with the "
                f"same genotype, gene and site. Renaming would put two rows on one "
                f"line with the freezer split between them. "
                f"Renaming: {carrying(line)}. Already there: {carrying(clash)}. "
                f"If they are one line, merge them — see the production-data skill.")

    # ------------------------------------------------------------------
    # Parental lines
    # ------------------------------------------------------------------
    def _parents(self, rows):
        self.stdout.write(f"\n  Parental-line changes: {len(rows)} Access rows")

        wanted = {}
        for row in rows:
            line = self._line_for(row["ID"])
            if line is None:
                self.notes["skipped — cell line not on file"].append(
                    f"Access id {clean_str(row['ID'])} (parental line)")
                continue
            wanted.setdefault(line.pk, (line, clean_str(row["OldValue"]), set()))
            wanted[line.pk][2].add(clean_str(row["NewValue"]))

        self.stdout.write(f"    → {len(wanted)} live cell lines to consider")

        for line, expected, new_values in wanted.values():
            values = {v for v in new_values if v}
            if not values:
                self.counts["parents left — export blanked them"] += 1
                self.notes["parents the export blanked (kept as they are)"].append(
                    f"{line.name} (id {line.pk}) keeps {line.parental_line_name!r}")
                continue
            if len(values) > 1:
                self.counts["parents refused"] += 1
                self.notes["parents refused — the export disagrees with itself"].append(
                    f"{line.name} (id {line.pk}) → {sorted(values)}")
                continue
            self._reparent(line, expected, values.pop())

    def _reparent(self, line, expected, new_value):
        # A parent is written as a C-number on 145 of the 169 rows that have
        # one, so the number is resolved to a real line rather than stored as
        # text nothing points at.
        matches = [m for m in cell_lines_svc.by_c_number(new_value, db=DB)
                   if m.site_id == line.site_id and m.pk != line.pk]

        # Asked before the precondition, so a second run reports the work as
        # done rather than refusing it for no longer matching the value the
        # export expected — which is exactly what the first run changed.
        settled = (line.parental_line_name or "") == new_value
        if settled and (len(matches) != 1 or line.parent_line_id == matches[0].pk):
            self.counts["parents already correct"] += 1
            return

        if expected and (line.parental_line_name or "") != expected and not settled:
            self.counts["parents refused"] += 1
            self.notes["parents refused — the site has moved on"].append(
                f"{line.name} (id {line.pk}): export expected {expected!r}, site "
                f"holds {line.parental_line_name!r}, {new_value!r} not applied")
            return

        parent_id = line.parent_line_id
        if len(matches) > 1:
            self.counts["parents refused"] += 1
            self.notes["parents refused — the number names more than one line"].append(
                f"{line.name} (id {line.pk}) → {new_value}: "
                + ", ".join(f"{m.name} (id {m.pk})" for m in matches))
            return
        if len(matches) == 1:
            parent_id = matches[0].pk
        elif new_value != (line.parental_line_name or ""):
            # Worth having as text even unresolved — it is what is written down
            # — but say so rather than let a dead reference read as a link.
            self.notes["parents stored as text — no line carries that number"].append(
                f"{line.name} (id {line.pk}) → {new_value!r}")

        fields = []
        if (line.parental_line_name or "") != new_value:
            line.parental_line_name = new_value
            fields.append("parental_line_name")
        if parent_id != line.parent_line_id:
            line.parent_line_id = parent_id
            fields.append("parent_line")
        if not fields:
            self.counts["parents already correct"] += 1
            return
        line.save(using=DB, update_fields=fields)
        self.counts["parents corrected"] += 1
        self.notes["parents corrected"].append(
            f"{line.name} (id {line.pk}): {expected or '?'} → {new_value}"
            + (f", linked to id {parent_id}" if "parent_line" in fields else ""))

    # ------------------------------------------------------------------
    def _report(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        for key in sorted(self.counts):
            self.stdout.write(f"  {self.counts[key]:>5}  {key}")
        for heading in sorted(self.notes):
            items = self.notes[heading]
            self.stdout.write(self.style.WARNING(f"\n  {len(items)} {heading}:"))
            for item in items[:40]:
                self.stdout.write(f"    · {item}")
            if len(items) > 40:
                self.stdout.write(f"    … and {len(items) - 40} more")
        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — everything above was rolled back. "
                "Re-run with --apply to write it."))
        else:
            self.stdout.write(self.style.SUCCESS("\nWritten."))
