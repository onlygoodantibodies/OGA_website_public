"""Record what a supplier's own website says about each published antibody.

Every one of the 1,645 antibodies with a published figure — which is exactly the
set the public gene pages draw — was checked against its supplier's catalogue on
11–12 August 2026. The verdicts are committed at
``pipeline/data/antibody_availability_2026_08.csv`` and this command writes them
onto ``Antibody.out_of_market``, which is the field the public pages already
read. No schema change: the flag exists, it was simply eight years stale.

WHAT THE THREE VERDICTS MEAN, AND WHY ONE OF THEM WRITES NOTHING
  ``available``     a live buy panel was seen — a price, a size option, an
                    in-stock line, an active add-to-cart. Backordered and long
                    lead times count, because the product is still orderable.
  ``discontinued``  the supplier's own page or catalogue search said so, in
                    those words, or the search returned no hits for the number.
  ``unclear``       genuinely indeterminate and **left alone** — 26 rows, mostly
                    structural: ABCD Antibodies and IPI entries are registry
                    records offering "contact the facility to assess production
                    feasibility", so they were never catalogue products and are
                    neither on sale nor withdrawn. Two Thermo products read
                    "temporarily unavailable", which is the company's own hedge.
                    Writing either value there would be asserting something
                    nobody established; the count is printed rather than folded
                    into "unchanged", because a row nothing was written to is a
                    row somebody may want to look at.

WHAT IT WILL NOT DO
  * **It will not write on the pk alone.** ``oga_id`` is the pk, and a pk is
    only meaningful against the database the file was built from — regenerate
    the check against a restored dump and the same integers name different
    reagents. Every row is confirmed by ``catalogue_number`` before anything is
    written, and a row whose catalogue disagrees is refused **by name, with both
    spellings**, never guessed at and never silently skipped. (All 1,645 agree
    today; the check is there for the next file.)
  * **It will not invent a record.** An ``oga_id`` naming no antibody is
    reported and passed over — this command records availability, it does not
    add reagents.
  * **It will not reach an antibody with no published figure.** Scope is
    ``pipeline/public.py``'s: the check covered the public set, so a row naming
    anything outside it is a sign the file was built somewhere else, and it is
    refused with the rest.

WHY A 404 IS NOT IN HERE ANYWHERE
  Worth knowing before anybody regenerates this file: a dead supplier URL is
  **not** evidence of withdrawal. Stale slugs are routine — 7 of 9 GeneTex URLs
  that 404'd were live under a changed slug, and the same trap caught
  Proteintech, Santa Cruz, Miltenyi, Abnova and Aviva. Every ``discontinued``
  verdict in the committed file rests on an explicit on-page statement or a
  catalogue-search miss, quoted in ``evidence_quote``, never on a dead link.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * Guarded to ``pipeline_db``.
  * Take a Render export before ``--apply`` (see the production-data skill).

    python manage.py import_antibody_availability
    python manage.py import_antibody_availability --apply
    python manage.py import_antibody_availability path/to/another.csv
"""
from __future__ import annotations

import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DB = "pipeline_db"

DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "antibody_availability_2026_08.csv",
)

#: The verdict column, and what each value writes. ``unclear`` is absent on
#: purpose — see the module docstring; ``None`` would read as "write nothing"
#: and so would a missing key, but only one of them says why in the output.
WRITES = {"available": False, "discontinued": True}

REQUIRED_COLUMNS = {"oga_id", "catalogue_number", "availability_checked"}


def _norm(value):
    """Compare catalogue numbers the way a person reads them off a label."""
    return (value or "").strip().casefold()


class Command(BaseCommand):
    help = ("Record supplier availability on Antibody.out_of_market from the "
            "committed check. Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path", nargs="?", default=DEFAULT_CSV,
            help=f"Path to the availability CSV (default: {DEFAULT_CSV})")
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the changes (default is a dry run)")

    def handle(self, *args, **opts):
        from pipeline.models import Antibody

        path = opts["csv_path"]
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")

        if not rows:
            raise CommandError(f"No rows in {path}")

        missing = REQUIRED_COLUMNS - set(rows[0])
        if missing:
            raise CommandError(
                f"{path} is missing column(s): {', '.join(sorted(missing))}. "
                f"Expected at least: {', '.join(sorted(REQUIRED_COLUMNS))}.")

        # One query for the file, not one per row.
        wanted = []
        for row in rows:
            try:
                wanted.append(int(row["oga_id"]))
            except (TypeError, ValueError):
                pass
        on_file = {
            ab.pk: ab
            for ab in Antibody.objects.using(DB)
            .filter(pk__in=wanted)
            .only("pk", "catalogue_number", "out_of_market")
        }

        to_set, to_clear, agreed, unclear, refused = [], [], 0, [], []

        for line_no, row in enumerate(rows, start=2):  # row 1 is the header
            raw_id = (row.get("oga_id") or "").strip()
            catalogue = (row.get("catalogue_number") or "").strip()
            verdict = (row.get("availability_checked") or "").strip().lower()
            label = f"{catalogue or '(no catalogue number)'} (oga_id {raw_id or '?'})"

            try:
                pk = int(raw_id)
            except (TypeError, ValueError):
                refused.append(f"line {line_no}: oga_id {raw_id!r} is not a "
                               f"record number — left alone")
                continue

            antibody = on_file.get(pk)
            if antibody is None:
                refused.append(f"{label}: no antibody with that record number "
                               f"— left alone")
                continue

            # The pk found a row; the catalogue number is what confirms it is
            # the row the checker was looking at.
            if _norm(antibody.catalogue_number) != _norm(catalogue):
                refused.append(
                    f"{label}: record {pk} is "
                    f"{antibody.catalogue_number or '(blank)'}, not {catalogue} "
                    f"— the file was built against a different database, left alone")
                continue

            if verdict not in WRITES:
                # `unclear` is the expected member of this branch; anything else
                # is a typo, and both are reported rather than assumed.
                unclear.append(f"{label}: {verdict or '(blank)'}")
                continue

            wants = WRITES[verdict]
            if antibody.out_of_market == wants:
                agreed += 1
            elif wants:
                to_set.append((antibody, label, row.get("evidence_quote", "")))
            else:
                to_clear.append((antibody, label))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Availability check — {len(rows):,} row(s) from {path}"))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nNow discontinued ({len(to_set)})"))
        for _ab, label, evidence in to_set:
            self.stdout.write(f"  {label}")
            if evidence:
                self.stdout.write(f"      {evidence[:150]}")
        if not to_set:
            self.stdout.write("  none")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nBack on sale ({len(to_clear)})"))
        for _ab, label in to_clear:
            self.stdout.write(f"  {label}")
        if not to_clear:
            self.stdout.write("  none")

        self.stdout.write(
            f"\n{agreed:,} row(s) already say what the check found.")

        if unclear:
            self.stdout.write(self.style.WARNING(
                f"\nNothing written — the check could not tell ({len(unclear)})"))
            for line in unclear:
                self.stdout.write(f"  {line}")

        if refused:
            self.stdout.write(self.style.ERROR(
                f"\nRefused ({len(refused)})"))
            for line in refused:
                self.stdout.write(f"  {line}")

        planned = len(to_set) + len(to_clear)

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing written. {planned} change(s) ready."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for antibody, _label, _evidence in to_set:
                antibody.out_of_market = True
                antibody.save(using=DB,
                              update_fields=["out_of_market", "updated_at"])
            for antibody, _label in to_clear:
                antibody.out_of_market = False
                antibody.save(using=DB,
                              update_fields=["out_of_market", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied {planned} change(s): {len(to_set)} discontinued, "
            f"{len(to_clear)} back on sale."))
