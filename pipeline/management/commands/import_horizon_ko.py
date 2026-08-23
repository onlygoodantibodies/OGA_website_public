"""
Import the Horizon Discovery HAP1 knockout cell-line catalogue into the pipeline
database so Feasibility can tell people which targets already have a ready-made
commercial KO to order.

Reads a CSV with columns:
    gene_name, item_number, product_name

A committed copy of the catalogue lives at ``pipeline/data/horizon_hap1_ko.csv``
(converted from Horizon's "Available HAP1 KO clones" spreadsheet), so the default
run needs no arguments.

Usage:
    python manage.py import_horizon_ko
    python manage.py import_horizon_ko path/to/other.csv
    python manage.py import_horizon_ko --catalogue-version "2024-Q2"
    python manage.py import_horizon_ko --dry-run

Re-runnable: wipes all existing HorizonKoLine rows before importing.
"""

import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import HorizonKoLine

DB = "pipeline_db"
BATCH_SIZE = 2000
DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "horizon_hap1_ko.csv",
)


class Command(BaseCommand):
    help = "Import the Horizon HAP1 KO catalogue CSV into the pipeline database"

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path", nargs="?", default=DEFAULT_CSV,
            help=f"Path to the catalogue CSV (default: {DEFAULT_CSV})",
        )
        parser.add_argument(
            "--catalogue-version", dest="version", default="",
            help="Catalogue version, stored as metadata",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse and validate without writing to the database",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        version = options["version"]
        dry_run = options["dry_run"]

        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_path}")
        except Exception as e:
            raise CommandError(f"Error reading CSV: {e}")

        if not rows:
            raise CommandError("No rows found in CSV")

        required = {"gene_name", "item_number"}
        missing = required - set(rows[0].keys())
        if missing:
            raise CommandError(f"CSV missing required column(s): {', '.join(sorted(missing))}")

        objects = []
        seen_items = set()
        skipped = 0
        for row in rows:
            gene = (row.get("gene_name") or "").strip().upper()
            item = (row.get("item_number") or "").strip()
            product = (row.get("product_name") or "").strip()
            if not gene or not item or item in seen_items:
                skipped += 1
                continue
            seen_items.add(item)
            objects.append(HorizonKoLine(
                gene_name=gene,
                item_number=item,
                product_name=product,
                supplier="Horizon Discovery",
                background="HAP1",
                dataset_version=version,
            ))

        gene_count = len({o.gene_name for o in objects})
        self.stdout.write(
            f"Parsed {len(objects)} KO lines across {gene_count} genes "
            f"({skipped} rows skipped)."
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return

        with transaction.atomic(using=DB):
            deleted, _ = HorizonKoLine.objects.using(DB).all().delete()
            HorizonKoLine.objects.using(DB).bulk_create(objects, batch_size=BATCH_SIZE)

        self.stdout.write(self.style.SUCCESS(
            f"Imported {len(objects)} Horizon KO lines "
            f"({gene_count} genes); removed {deleted} old rows."
        ))
