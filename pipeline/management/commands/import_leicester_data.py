"""
Leicester Excel Import — Management Command

Imports antibody and cell line data from Leicester's Antibody_Database.xlsx
into the pipeline database. Designed as the general-purpose bulk import pattern
(scoping doc §7.4): header-based column mapping, re-runnable with update_or_create.

Match key for antibodies: (catalogue_number, company, target, lot_number, site)
Match key for cell lines: (name, catalogue_number, company)

Usage:
    python manage.py import_leicester_data path/to/Antibody_Database.xlsx
    python manage.py import_leicester_data path/to/Antibody_Database.xlsx --dry-run
"""

import os
import re
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

import openpyxl

from pipeline.services import lab_numbers
from pipeline.models import (
    Site, Company, Target, Antibody, CellLine,
)


# ============================================================================
# Supplier name normalisation
# ============================================================================
SUPPLIER_NORMALISATION = {
    'abcam': 'Abcam',
    'Cell Signalling Technology': 'Cell Signaling Technology',
    'Genetex': 'GeneTex',
    'ProteinTech': 'Proteintech',
}


def normalise_supplier(raw):
    """Normalise supplier name, checking common variants."""
    if not raw:
        return ''
    name = str(raw).strip()
    return SUPPLIER_NORMALISATION.get(name, name)


# ============================================================================
# Clonality mapping
# ============================================================================
CLONALITY_MAP = {
    'monoclonal': 'monoclonal',
    'polyclonal': 'polyclonal',
    'recombinant': 'recombinant',
    'recombinant monoclonal': 'recombinant',
}


def map_clonality(raw):
    if not raw:
        return 'unknown'
    return CLONALITY_MAP.get(str(raw).strip().lower(), 'unknown')


# ============================================================================
# Header detection
# ============================================================================
def find_header_row(ws, required_headers):
    """
    Scan the first 10 rows for a row containing the required headers.
    Returns (row_number, {header_name: column_index}) or raises.
    """
    for row_num in range(1, 11):
        row_vals = {}
        for col in range(1, ws.max_column + 1):
            val = ws.cell(row=row_num, column=col).value
            if val:
                row_vals[str(val).strip()] = col

        # Check if enough required headers match
        matches = sum(1 for h in required_headers if h in row_vals)
        if matches >= len(required_headers) * 0.6:  # 60% match threshold
            return row_num, row_vals

    raise CommandError(
        f"Could not find header row in sheet '{ws.title}'. "
        f"Looking for: {required_headers}"
    )


def get_cell(ws, row, col_map, header, default=None):
    """Get a cell value by header name, with fallback."""
    col = col_map.get(header)
    if col is None:
        return default
    val = ws.cell(row=row, column=col).value
    if val is None:
        return default
    return val


def clean_str(val, default=''):
    """Clean a cell value to a stripped string."""
    if val is None:
        return default
    # Remove non-breaking spaces, trailing whitespace
    return str(val).replace('\xa0', ' ').strip()


def clean_bool(val, default=False):
    """Convert Yes/No/yes/no to bool."""
    if val is None:
        return default
    return str(val).strip().lower() in ('yes', 'true', '1')


def clean_decimal(val, default=None):
    """Convert to Decimal, returning None for unparseable values."""
    if val is None:
        return default
    try:
        return Decimal(str(val).strip())
    except (InvalidOperation, ValueError):
        return default


# ============================================================================
# Catalogue number cleaning
# ============================================================================
def clean_catalogue_number(raw):
    """
    Clean catalogue number edge cases.
    e.g. 'ab210498 two lot nos' → 'ab210498'
    """
    if not raw:
        return ''
    s = str(raw).strip()
    # Remove annotations like "two lot nos"
    s = re.sub(r'\s+(two|three|multiple)\s+lot\s+nos?.*', '', s, flags=re.IGNORECASE)
    return s.strip()


# ============================================================================
# Command
# ============================================================================
class Command(BaseCommand):
    help = 'Import Leicester Antibody_Database.xlsx into pipeline database'

    def add_arguments(self, parser):
        parser.add_argument(
            'excel_path',
            help='Path to Antibody_Database.xlsx'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and validate without writing to database'
        )

    def handle(self, *args, **options):
        # No numbers are issued during a historical import. These commands
        # reconstruct what the lab recorded years ago: a row that arrives with
        # no A-number or C-number arrives that way because nobody wrote one on
        # the box, and minting one here would put a number on a 2019 record that
        # no freezer agrees with. See `services/lab_numbers.py::suspended`.
        with lab_numbers.suspended():
            return self._run(*args, **options)

    def _run(self, *args, **options):
        path = options['excel_path']
        dry_run = options['dry_run']

        if not os.path.exists(path):
            raise CommandError(f"File not found: {path}")

        self.stdout.write(f"Loading {path}...")
        wb = openpyxl.load_workbook(path, data_only=True)

        self.stdout.write(f"Sheets found: {wb.sheetnames}")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes"))

        db = 'pipeline_db'

        # Get or create Leicester site
        if not dry_run:
            leicester, _ = Site.objects.using(db).get_or_create(
                short_code='LEI',
                defaults={'name': 'Leicester'}
            )
        else:
            leicester = None

        # Track stats
        stats = {
            'targets_created': 0, 'targets_updated': 0, 'targets_matched': 0,
            'companies_created': 0, 'companies_matched': 0,
            'antibodies_created': 0, 'antibodies_updated': 0,
            'cell_lines_created': 0, 'cell_lines_updated': 0,
            'skipped': 0, 'errors': [],
        }

        # Import in order: targets/companies first, then antibodies, then cell lines
        if not dry_run:
            with transaction.atomic(using=db):
                self._import_antibodies(wb, leicester, stats, db, dry_run)
                self._import_cell_lines(wb, leicester, stats, db, dry_run)
        else:
            self._import_antibodies(wb, leicester, stats, db, dry_run)
            self._import_cell_lines(wb, leicester, stats, db, dry_run)

        # Report
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("LEICESTER IMPORT RESULTS")
        self.stdout.write("=" * 60)
        for key, val in stats.items():
            if key != 'errors':
                self.stdout.write(f"  {key}: {val}")
        if stats['errors']:
            self.stdout.write(self.style.WARNING(f"\n  ERRORS ({len(stats['errors'])}):"))
            for err in stats['errors']:
                self.stdout.write(f"    {err}")
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN complete — re-run without --dry-run to import"
            ))

    # ========================================================================
    # Antibodies sheet
    # ========================================================================
    def _import_antibodies(self, wb, leicester, stats, db, dry_run):
        if 'Antibodies' not in wb.sheetnames:
            self.stdout.write(self.style.WARNING("No 'Antibodies' sheet found — skipping"))
            return

        ws = wb['Antibodies']
        self.stdout.write("\n--- Antibodies sheet ---")

        # Expected headers
        expected = [
            'Gene', 'UniProt ID', 'Supplier', 'Catalogue number',
            'RRID', 'Lot Number', 'Concentration', 'Host', 'Clonality'
        ]
        header_row, col_map = find_header_row(ws, expected)
        self.stdout.write(f"  Header row: {header_row}")
        self.stdout.write(f"  Columns found: {list(col_map.keys())}")

        for row in range(header_row + 1, ws.max_row + 1):
            gene_raw = get_cell(ws, row, col_map, 'Gene')
            if not gene_raw:
                continue  # skip empty rows

            gene_name = clean_str(gene_raw)
            uniprot_id = clean_str(get_cell(ws, row, col_map, 'UniProt ID'))
            protein_name = clean_str(get_cell(ws, row, col_map, 'UniProt Protein Name'))
            supplier_raw = get_cell(ws, row, col_map, 'Supplier')
            supplier_name = normalise_supplier(supplier_raw)
            cat_number = clean_catalogue_number(get_cell(ws, row, col_map, 'Catalogue number'))
            lot_number = clean_str(get_cell(ws, row, col_map, 'Lot Number'))
            rrid = clean_str(get_cell(ws, row, col_map, 'RRID'))
            # Handle concentration — Leicester already in µg/mL
            conc_raw = get_cell(ws, row, col_map, 'Concentration (µg/mL)')
            if conc_raw is None:
                conc_raw = get_cell(ws, row, col_map, 'Concentration')
            concentration = clean_decimal(conc_raw)
            host = clean_str(get_cell(ws, row, col_map, 'Host'))
            clonality = map_clonality(get_cell(ws, row, col_map, 'Clonality'))
            clone_id = clean_str(get_cell(ws, row, col_map, 'Clone ID'))
            recombinant = clean_bool(get_cell(ws, row, col_map, 'Recombinant'))
            received = clean_bool(get_cell(ws, row, col_map, 'Received'))
            storage = clean_str(get_cell(ws, row, col_map, 'Storage'))
            in_kind_raw = get_cell(ws, row, col_map, 'In Kind Contribution')
            in_kind_value = clean_decimal(in_kind_raw)

            # Vendor-recommended applications → supplier_validated_* fields
            sv_wb = clean_bool(get_cell(ws, row, col_map, 'WB'))
            sv_ip = clean_bool(get_cell(ws, row, col_map, 'IP'))
            sv_if = clean_bool(get_cell(ws, row, col_map, 'ICC'))  # ICC = IF
            sv_fc = clean_bool(get_cell(ws, row, col_map, 'FC'))
            sv_ihc = clean_bool(get_cell(ws, row, col_map, 'IHC'))
            sv_elisa = clean_bool(get_cell(ws, row, col_map, 'ELISA'))

            # Handle N/A clone IDs
            if clone_id and clone_id.upper() in ('N/A', 'NA', '-'):
                clone_id = ''

            # Skip rows with no real gene target
            if gene_name.lower() in ('unrequested', 'n/a', 'na', ''):
                stats['skipped'] += 1
                if not dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Skipped row {row}: gene='{gene_name}' "
                            f"({supplier_name} {cat_number})"
                        )
                    )
                continue

            if dry_run:
                self.stdout.write(
                    f"  Row {row}: {gene_name} | {supplier_name} | "
                    f"{cat_number} | lot={lot_number}"
                )
                stats['antibodies_created'] += 1
                continue

            try:
                # 1. Get or create Target
                target = self._get_or_create_target(
                    gene_name, uniprot_id, protein_name, leicester, stats, db
                )

                # 2. Get or create Company
                company = None
                if supplier_name:
                    company, created = Company.resolve(supplier_name, db=db)
                    if created:
                        stats['companies_created'] += 1
                    else:
                        stats['companies_matched'] += 1

                # 3. Create or update Antibody
                match_kwargs = {
                    'catalogue_number': cat_number,
                    'company': company,
                    'target': target,
                    'lot_number': lot_number,
                    'site': leicester,
                }
                update_fields = {
                    'rrid': rrid,
                    'concentration': concentration,
                    'host_species': host,
                    'clonality': clonality,
                    'clone_id': clone_id,
                    'is_recombinant': recombinant,
                    'in_kind_value': in_kind_value,
                    'in_kind_currency': 'GBP',
                    'acquisition_method': 'in_kind' if in_kind_value else 'unknown',
                    'supplier_validated_wb': sv_wb,
                    'supplier_validated_ip': sv_ip,
                    'supplier_validated_if': sv_if,
                    'supplier_validated_fc': sv_fc,
                    'supplier_validated_ihc': sv_ihc,
                    'supplier_validated_elisa': sv_elisa,
                }
                if received:
                    update_fields['received_date'] = None  # Leicester only tracks Yes/No

                ab, created = Antibody.objects.using(db).update_or_create(
                    **match_kwargs,
                    defaults=update_fields
                )
                if created:
                    stats['antibodies_created'] += 1
                else:
                    stats['antibodies_updated'] += 1

            except Exception as e:
                stats['errors'].append(f"Row {row} ({gene_name}, {cat_number}): {e}")

    # ========================================================================
    # Cell lines sheet
    # ========================================================================
    def _import_cell_lines(self, wb, leicester, stats, db, dry_run):
        if 'Cell lines' not in wb.sheetnames:
            self.stdout.write(self.style.WARNING("No 'Cell lines' sheet found — skipping"))
            return

        ws = wb['Cell lines']
        self.stdout.write("\n--- Cell lines sheet ---")

        expected = ['Cell line', 'Gene KO', 'Supplier', 'Catalogue number']
        header_row, col_map = find_header_row(ws, expected)
        self.stdout.write(f"  Header row: {header_row}")
        self.stdout.write(f"  Columns found: {list(col_map.keys())}")

        for row in range(header_row + 1, ws.max_row + 1):
            cell_line_name = clean_str(get_cell(ws, row, col_map, 'Cell line'))
            if not cell_line_name:
                continue

            gene_ko = clean_str(get_cell(ws, row, col_map, 'Gene KO'))
            uniprot_id = clean_str(get_cell(ws, row, col_map, 'UniProt ID'))
            supplier_name = normalise_supplier(get_cell(ws, row, col_map, 'Supplier'))
            cat_number = clean_str(get_cell(ws, row, col_map, 'Catalogue number'))
            lot_number = clean_str(get_cell(ws, row, col_map, 'Lot number'))
            received = clean_bool(get_cell(ws, row, col_map, 'Received'))
            in_kind_value = clean_decimal(get_cell(ws, row, col_map, 'In Kind Contribution'))
            cellosaurus_id = clean_str(get_cell(ws, row, col_map, 'Cellosaurus ID'))

            # Determine genotype
            is_ko = gene_ko and gene_ko.upper() not in ('N/A', 'NA', '-', '')
            genotype = 'KO' if is_ko else 'WT'

            if dry_run:
                self.stdout.write(
                    f"  Row {row}: {cell_line_name} | KO={gene_ko} | "
                    f"{supplier_name} | {cat_number} | {genotype}"
                )
                stats['cell_lines_created'] += 1
                continue

            try:
                # Get or create company
                company = None
                if supplier_name:
                    company, _ = Company.resolve(supplier_name, db=db)

                # Link to Target if this is a KO line
                target = None
                if is_ko:
                    target = Target.objects.using(db).filter(
                        gene_name=gene_ko
                    ).first()
                    if not target and uniprot_id and uniprot_id.upper() not in ('N/A', 'NA', ''):
                        target = Target.objects.using(db).filter(
                            uniprot_id=uniprot_id
                        ).first()

                # Match key: (name, catalogue_number, company)
                # Use get_or_create + selective update to avoid overwriting
                # good data with blanks from duplicate spreadsheet rows
                match_kwargs = {
                    'name': cell_line_name,
                    'catalogue_number': cat_number,
                    'company': company,
                }

                cl, created = CellLine.objects.using(db).get_or_create(
                    **match_kwargs,
                    defaults={
                        'genotype': genotype,
                        'target': target,
                        'lot_number': lot_number,
                        'cellosaurus_id': cellosaurus_id,
                        'site': leicester,
                        'received': received,
                        'in_kind_value': in_kind_value,
                        'in_kind_currency': 'GBP',
                    }
                )

                if not created:
                    # Fill-not-erase: only update fields where existing is
                    # empty and new value is non-empty
                    updated = False
                    if lot_number and not cl.lot_number:
                        cl.lot_number = lot_number
                        updated = True
                    if cellosaurus_id and not cl.cellosaurus_id:
                        cl.cellosaurus_id = cellosaurus_id
                        updated = True
                    if target and not cl.target:
                        cl.target = target
                        updated = True
                    if in_kind_value and not cl.in_kind_value:
                        cl.in_kind_value = in_kind_value
                        updated = True
                    if received and not cl.received:
                        cl.received = received
                        updated = True
                    if updated:
                        cl.save(using=db)
                if created:
                    stats['cell_lines_created'] += 1
                else:
                    stats['cell_lines_updated'] += 1

            except Exception as e:
                stats['errors'].append(f"Cell line row {row} ({cell_line_name}): {e}")

    # ========================================================================
    # Target get-or-create with update logic
    # ========================================================================
    def _get_or_create_target(self, gene_name, uniprot_id, protein_name,
                              leicester, stats, db):
        """
        Find existing target by gene_name first, then uniprot_id.
        If found, update uniprot_id/protein_name if Leicester has better data.
        If not found, create new target for Leicester.
        """
        target = None

        # Try matching by gene_name first
        if gene_name:
            target = Target.objects.using(db).filter(gene_name=gene_name).first()

        # Fallback: try matching by uniprot_id
        if not target and uniprot_id:
            target = Target.objects.using(db).filter(uniprot_id=uniprot_id).first()

        if target:
            stats['targets_matched'] += 1
            updated = False

            # Update uniprot_id if Montreal's is empty and Leicester has one
            if uniprot_id and not target.uniprot_id:
                target.uniprot_id = uniprot_id
                updated = True

            # Update protein_name if Montreal's is empty/generic and Leicester has one
            if protein_name and (
                not target.protein_name
                or target.protein_name == target.gene_name
            ):
                target.protein_name = protein_name
                updated = True

            if updated:
                target.save(using=db)
                stats['targets_updated'] += 1

            return target

        # Create new target (Leicester-only gene)
        target = Target.objects.using(db).create(
            gene_name=gene_name,
            protein_name=protein_name or gene_name,
            uniprot_id=uniprot_id or '',
            site=leicester,
            status='not_started',
        )
        stats['targets_created'] += 1
        self.stdout.write(
            self.style.SUCCESS(f"    NEW TARGET: {gene_name} (Leicester-only)")
        )
        return target
