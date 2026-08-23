"""
Import filtered DepMap expression data into the pipeline database.

Expects a long-format CSV with columns:
    gene_name, entrez_id, cell_line, depmap_id, tpm_log2

This CSV is produced by running the extraction script locally (via Cowork
or a standalone Python script) against the full DepMap
OmicsExpressionAllGenesTPMLogp1Profile.csv + Model.csv files.

Usage:
    python manage.py import_depmap_expression path/to/depmap_subset.csv
    python manage.py import_depmap_expression path/to/depmap_subset.csv --release 25Q2
    python manage.py import_depmap_expression path/to/depmap_subset.csv --dry-run

Re-runnable: wipes all existing DepMapExpression rows before importing.
"""

import csv
import time
from django.core.management.base import BaseCommand, CommandError
from pipeline.models import DepMapExpression


DB = 'pipeline_db'
BATCH_SIZE = 5000


class Command(BaseCommand):
    help = 'Import filtered DepMap expression CSV into pipeline database'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_path',
            help='Path to the filtered long-format CSV file'
        )
        parser.add_argument(
            '--release',
            default='',
            help='DepMap release version, e.g. "25Q2" (stored as metadata)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and validate without writing to database'
        )

    def handle(self, *args, **options):
        csv_path = options['csv_path']
        release = options['release']
        dry_run = options['dry_run']

        try:
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_path}")
        except Exception as e:
            raise CommandError(f"Error reading CSV: {e}")

        # Validate columns
        required_cols = {'gene_name', 'cell_line', 'tpm_log2'}
        if not required_cols.issubset(set(rows[0].keys()) if rows else set()):
            raise CommandError(
                f"CSV must have columns: {required_cols}. "
                f"Found: {set(rows[0].keys()) if rows else 'empty file'}"
            )

        self.stdout.write(f"Read {len(rows):,} rows from {csv_path}")

        # Count unique genes and cell lines
        genes = set()
        cell_lines = set()
        for row in rows:
            genes.add(row['gene_name'])
            cell_lines.add(row['cell_line'])

        self.stdout.write(f"  Unique genes: {len(genes):,}")
        self.stdout.write(f"  Cell lines: {sorted(cell_lines)}")

        if dry_run:
            self.stdout.write(self.style.SUCCESS("DRY RUN — no database changes"))

            # Show a sample
            self.stdout.write("\nSample rows:")
            for row in rows[:10]:
                tpm = float(row['tpm_log2'])
                marker = '✓' if tpm >= 2.5 else '✗'
                self.stdout.write(
                    f"  {marker} {row['gene_name']:>10} in {row['cell_line']:<15} "
                    f"TPM={tpm:.2f}"
                )
            return

        # Wipe existing data
        old_count = DepMapExpression.objects.using(DB).count()
        if old_count > 0:
            self.stdout.write(f"Deleting {old_count:,} existing records...")
            DepMapExpression.objects.using(DB).all().delete()

        # Build model instances
        self.stdout.write("Building records...")
        objects = []
        skipped = 0
        for row in rows:
            try:
                tpm = float(row['tpm_log2'])
            except (ValueError, TypeError):
                skipped += 1
                continue

            gene_name = row['gene_name'].strip()
            if not gene_name:
                skipped += 1
                continue

            objects.append(DepMapExpression(
                gene_name=gene_name,
                entrez_id=int(row['entrez_id']) if row.get('entrez_id') else None,
                cell_line=row['cell_line'].strip(),
                depmap_id=row.get('depmap_id', '').strip(),
                tpm_log2=tpm,
                depmap_release=release,
            ))

        # Bulk insert in batches
        self.stdout.write(f"Inserting {len(objects):,} records (skipped {skipped})...")
        start = time.time()

        total_created = 0
        for i in range(0, len(objects), BATCH_SIZE):
            batch = objects[i:i + BATCH_SIZE]
            DepMapExpression.objects.using(DB).bulk_create(batch)
            total_created += len(batch)
            if total_created % 50000 == 0:
                self.stdout.write(f"  ... {total_created:,} inserted")

        elapsed = time.time() - start
        self.stdout.write(self.style.SUCCESS(
            f"\nDone: {total_created:,} records imported in {elapsed:.1f}s"
        ))

        # Quick sanity check
        self.stdout.write("\nSanity check — HAP1 expression for a few genes:")
        for gene in ['SYT1', 'LRRK2', 'CD44', 'GAPDH', 'TP53']:
            expr = (DepMapExpression.objects.using(DB)
                    .filter(gene_name=gene, cell_line='HAP1')
                    .first())
            if expr:
                marker = '✓' if expr.above_threshold else '✗'
                self.stdout.write(f"  {marker} {gene}: {expr.tpm_log2:.2f}")
            else:
                self.stdout.write(f"  ? {gene}: not found")
