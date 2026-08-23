"""
Import filtered Cell Model Passports proteomics data into the pipeline database.

Expects a long-format CSV with columns:
    gene_name, uniprot_id, cell_line, protein_intensity

This CSV is produced by running Cowork against the full
Protein_matrix_averaged_YYYYMMDD.tsv file.

Usage:
    python manage.py import_proteomics imports/proteomics_subset.csv
    python manage.py import_proteomics imports/proteomics_subset.csv --dataset-version 20250211
    python manage.py import_proteomics imports/proteomics_subset.csv --dry-run

Re-runnable: wipes all existing ProteomicsExpression rows before importing.
"""
import csv
import time
from django.core.management.base import BaseCommand, CommandError
from pipeline.models import ProteomicsExpression


DB = 'pipeline_db'
BATCH_SIZE = 5000


class Command(BaseCommand):
    help = 'Import filtered proteomics CSV into pipeline database'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_path',
            help='Path to the filtered long-format CSV file'
        )
        parser.add_argument(
            '--dataset-version',
            default='',
            help='Dataset version date, e.g. "20250211"'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and validate without writing to database'
        )

    def handle(self, *args, **options):
        csv_path = options['csv_path']
        version = options['dataset_version']
        dry_run = options['dry_run']

        try:
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_path}")
        except Exception as e:
            raise CommandError(f"Error reading CSV: {e}")

        if not rows:
            raise CommandError("CSV file is empty")

        # Validate columns
        required_cols = {'gene_name', 'cell_line', 'protein_intensity'}
        found_cols = set(rows[0].keys())
        if not required_cols.issubset(found_cols):
            raise CommandError(
                f"CSV must have columns: {required_cols}. "
                f"Found: {found_cols}"
            )

        self.stdout.write(f"Read {len(rows):,} rows from {csv_path}")

        # Count unique genes and cell lines
        genes = set()
        cell_lines = set()
        for row in rows:
            genes.add(row['gene_name'])
            cell_lines.add(row['cell_line'])

        self.stdout.write(f"  Unique genes: {len(genes):,}")
        self.stdout.write(f"  Unique proteins with UniProt: {sum(1 for r in rows if r.get('uniprot_id')):,}")
        self.stdout.write(f"  Cell lines: {sorted(cell_lines)}")

        if dry_run:
            self.stdout.write(self.style.SUCCESS("DRY RUN — no database changes"))

            self.stdout.write("\nSample rows:")
            for row in rows[:10]:
                intensity = float(row['protein_intensity'])
                uniprot = row.get('uniprot_id', '')
                self.stdout.write(
                    f"  {row['gene_name']:>10} ({uniprot:<10}) in {row['cell_line']:<20} "
                    f"intensity={intensity:.2f}"
                )
            return

        # Wipe existing data
        old_count = ProteomicsExpression.objects.using(DB).count()
        if old_count > 0:
            self.stdout.write(f"Deleting {old_count:,} existing records...")
            ProteomicsExpression.objects.using(DB).all().delete()

        # Build model instances
        self.stdout.write("Building records...")
        objects = []
        skipped = 0
        for row in rows:
            try:
                intensity = float(row['protein_intensity'])
            except (ValueError, TypeError):
                skipped += 1
                continue

            gene_name = row['gene_name'].strip()
            if not gene_name:
                skipped += 1
                continue

            objects.append(ProteomicsExpression(
                gene_name=gene_name,
                uniprot_id=row.get('uniprot_id', '').strip(),
                cell_line=row['cell_line'].strip(),
                protein_intensity=intensity,
                dataset_version=version,
            ))

        # Bulk insert in batches
        self.stdout.write(f"Inserting {len(objects):,} records (skipped {skipped})...")
        start = time.time()

        total_created = 0
        for i in range(0, len(objects), BATCH_SIZE):
            batch = objects[i:i + BATCH_SIZE]
            ProteomicsExpression.objects.using(DB).bulk_create(batch)
            total_created += len(batch)
            if total_created % 25000 == 0:
                self.stdout.write(f"  ... {total_created:,} inserted")

        elapsed = time.time() - start
        self.stdout.write(self.style.SUCCESS(
            f"\nDone: {total_created:,} records imported in {elapsed:.1f}s"
        ))

        # Quick sanity check
        self.stdout.write("\nSanity check — protein detection for a few genes:")
        for gene in ['SYT1', 'GAPDH', 'TP53', 'CD44', 'LRRK2']:
            entries = (ProteomicsExpression.objects.using(DB)
                       .filter(gene_name=gene)
                       .order_by('-protein_intensity'))
            if entries.exists():
                top = entries.first()
                count = entries.count()
                self.stdout.write(
                    f"  ✓ {gene}: detected in {count} lines, "
                    f"highest in {top.cell_line} ({top.protein_intensity:.2f})"
                )
            else:
                self.stdout.write(f"  ✗ {gene}: not detected in any line")
