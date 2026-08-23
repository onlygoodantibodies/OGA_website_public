"""
Django management command: import_access_data

Imports YCharOS Access database (exported as CSV files) into the pipeline
PostgreSQL database. Run once for initial data migration.

v1.1 (3 April 2026): Maps ALL Access fields per Sara's requirement.
No field pruning — everything captured.

Usage:
    python manage.py import_access_data /path/to/csv/directory

The CSV directory should contain files exported from YCharosDataBaseFE_NON-SPLIT.accdb
using mdbtools: Companies.csv, GrantingAgencies.csv, Members.csv, Projects.csv,
Proteins.csv, CellLines.csv, Antibodies.csv, Wb.csv, IP.csv, IF.csv

Import order (respects foreign key dependencies):
    1. Sites (creates Montreal if not exists)
    2. Companies
    3. GrantingAgencies
    4. Projects
    5. Members (creates Django User + pipeline Member)
    6. Proteins → Target + Report
    7. CellLines → CellLine + InventoryLocation
    8. Antibodies → Antibody + InventoryLocation
    9. Wb → ExperimentSession + WbResult (session grouping heuristic)
   10. IP → ExperimentSession + IpResult
   11. IF → ExperimentSession + IfResult

Append tables (AbAppend, WbAppend, IFappend, IPappend, CellsAppend) are
skipped per scoping doc §6.2: replaced by bulk import feature.

Tissues table is skipped: 0 rows in Access.
"""

import csv
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.db import transaction

from pipeline.services import lab_numbers
from pipeline.models import (
    Site, Member, GrantingAgency, Project, Company,
    Target, Report, CellLine, Antibody, InventoryLocation,
    ExperimentSession, WbResult, IpResult, IfResult,
)


# =============================================================================
# Value mapping tables
# =============================================================================

STATUS_MAP = {
    '': 'in_progress',
    'Report complete': 'published',
    'Revisited target-Report complete': 'published',
    'Target cancelled-no renewable': 'cancelled',
    'Target cancelled': 'cancelled',
    'Target cancelled-no KO line': 'cancelled',
    'Not started': 'not_started',
    'Not Started': 'not_started',
    'Revisit target': 'in_progress',
    'KO in development': 'in_progress',
    'New strategy required': 'on_hold',
    'Antibody screening- on going': 'in_progress',
}

CLONALITY_MAP = {
    'polyclonal': 'polyclonal',
    'monoclonal': 'monoclonal',
    'recombinant mono': 'recombinant',
    'recombinant poly': 'recombinant',
    'recombinant super': 'recombinant',
    '': 'unknown',
}

ACQUISITION_MAP = {
    'in-kind': 'in_kind',
    'In-kind': 'in_kind',
    'in kind': 'in_kind',
    'In Kind': 'in_kind',
    'Purchased': 'purchased',
    'purchased': 'purchased',
    '': 'unknown',
}


# =============================================================================
# Helper functions
# =============================================================================

def clean_str(value):
    """Strip whitespace, return empty string for None."""
    if value is None:
        return ''
    return str(value).strip()


def clean_int(value):
    """Parse integer, return None for empty/invalid."""
    s = clean_str(value)
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def clean_decimal(value, digits=None):
    """Parse decimal, return None for empty/invalid."""
    s = clean_str(value)
    if not s:
        return None
    try:
        d = Decimal(s)
        if digits is not None:
            d = round(d, digits)
        return d
    except (InvalidOperation, ValueError):
        return None


def clean_bool(value):
    """Parse boolean from Access (0/1 or True/False)."""
    s = clean_str(value).lower()
    return s in ('1', 'true', 'yes')


def clean_date(value):
    """Parse Access date format (MM/DD/YY HH:MM:SS) to date object."""
    s = clean_str(value)
    if not s:
        return None
    for fmt in ('%m/%d/%y %H:%M:%S', '%m/%d/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%m/%d/%y', '%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def clean_doi(value):
    """
    Access stores DOIs as hyperlinks with # delimiters.
    Extract the first clean URL.
    """
    s = clean_str(value)
    if not s:
        return ''
    parts = [p.strip() for p in s.split('#') if p.strip()]
    for part in parts:
        if part.startswith('http'):
            return part
    return ''


def clean_hyperlink(value):
    """
    Extract URL from Access hyperlink fields (# delimited).
    Similar to clean_doi but for general links.
    """
    s = clean_str(value)
    if not s:
        return ''
    # Access hyperlinks: #URL#DisplayText# or just #URL#
    parts = [p.strip() for p in s.split('#') if p.strip()]
    for part in parts:
        if part.startswith('http'):
            return part
    # If no http, return the whole thing (might be a plain URL or text)
    return s if s.startswith('http') else ''


def pk(obj):
    """Safely get pk from an object, or return None."""
    return obj.pk if obj is not None else None


def read_csv(directory, filename):
    """Read a CSV file and return list of dicts."""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        raise CommandError(f"CSV file not found: {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))


class Command(BaseCommand):
    help = 'Import YCharOS Access database CSV exports into pipeline PostgreSQL'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_directory',
            help='Path to directory containing exported CSV files'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and validate without writing to database'
        )
        parser.add_argument(
            '--skip-experiments',
            action='store_true',
            help='Skip WB/IP/IF experiment import (useful for testing core entities first)'
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
        csv_dir = options['csv_directory']
        dry_run = options['dry_run']
        skip_experiments = options['skip_experiments']

        if not os.path.isdir(csv_dir):
            raise CommandError(f"Directory not found: {csv_dir}")

        self.stdout.write(self.style.MIGRATE_HEADING(
            'YCharOS Access → PostgreSQL Import (v1.1 — full field mapping)'
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no data will be written'))

        # Lookup maps built during import (Access ID → Django object)
        self.company_map = {}
        self.agency_map = {}
        self.project_map = {}
        self.member_map = {}
        self.target_map = {}
        self.cell_line_map = {}
        self.antibody_map = {}
        self.montreal = None

        with transaction.atomic(using='pipeline_db'):
            # Phase 1: Reference data
            self._import_site()
            self._import_companies(read_csv(csv_dir, 'Companies.csv'), dry_run)
            self._import_agencies(read_csv(csv_dir, 'GrantingAgencies.csv'), dry_run)
            self._import_projects(read_csv(csv_dir, 'Projects.csv'), dry_run)
            self._import_members(read_csv(csv_dir, 'Members.csv'), dry_run)

            # Phase 2: Core entities
            self._import_targets(read_csv(csv_dir, 'Proteins.csv'), dry_run)
            self._import_cell_lines(read_csv(csv_dir, 'CellLines.csv'), dry_run)
            self._import_antibodies(read_csv(csv_dir, 'Antibodies.csv'), dry_run)

            # Phase 3: Experiment data
            if skip_experiments:
                self.stdout.write(self.style.WARNING(
                    '\nSkipping experiment import (--skip-experiments)'
                ))
            else:
                self._import_wb(read_csv(csv_dir, 'Wb.csv'), dry_run)
                self._import_ip(read_csv(csv_dir, 'IP.csv'), dry_run)
                self._import_if(read_csv(csv_dir, 'IF.csv'), dry_run)

            if dry_run:
                self.stdout.write(self.style.WARNING(
                    '\nDRY RUN complete — rolling back all changes'
                ))
                transaction.set_rollback(True, using='pipeline_db')

        self.stdout.write(self.style.SUCCESS('\nImport complete!'))

    # =========================================================================
    # Phase 1: Reference data (unchanged from v1.0)
    # =========================================================================

    def _import_site(self):
        self.montreal, created = Site.objects.using('pipeline_db').get_or_create(
            short_code='MTL',
            defaults={'name': 'Montreal', 'is_active': True}
        )
        status = 'CREATED' if created else 'EXISTS'
        self.stdout.write(f'  Site Montreal: {status}')

    def _import_companies(self, rows, dry_run):
        self.stdout.write(f'\n  Companies: {len(rows)} rows')
        for row in rows:
            access_id = clean_int(row['ID'])
            name = clean_str(row['Company'])
            if not name:
                continue
            if not dry_run:
                obj, _ = Company.resolve(name, db='pipeline_db')
                self.company_map[str(access_id)] = obj
            else:
                self.company_map[str(access_id)] = access_id
        self.stdout.write(self.style.SUCCESS(f'    → {len(self.company_map)} companies'))

    def _import_agencies(self, rows, dry_run):
        self.stdout.write(f'\n  Granting Agencies: {len(rows)} rows')
        for row in rows:
            access_id = clean_int(row['ID'])
            name = clean_str(row['GrantingAgency'])
            if not name:
                continue
            if not dry_run:
                obj, _ = GrantingAgency.objects.using('pipeline_db').get_or_create(name=name)
                self.agency_map[str(access_id)] = obj
            else:
                self.agency_map[str(access_id)] = access_id
        self.stdout.write(self.style.SUCCESS(f'    → {len(self.agency_map)} agencies'))

    def _import_projects(self, rows, dry_run):
        self.stdout.write(f'\n  Projects: {len(rows)} rows')
        for row in rows:
            access_id = clean_int(row['ID'])
            name = clean_str(row['ProjectName'])
            if not name:
                continue
            if not dry_run:
                obj, _ = Project.objects.using('pipeline_db').get_or_create(name=name)
                self.project_map[str(access_id)] = obj
            else:
                self.project_map[str(access_id)] = access_id
        self.stdout.write(self.style.SUCCESS(f'    → {len(self.project_map)} projects'))

    def _import_members(self, rows, dry_run):
        self.stdout.write(f'\n  Members: {len(rows)} rows')
        for row in rows:
            access_id = clean_int(row['ID'])
            name = clean_str(row['Name'])
            if not name:
                continue
            parts = name.split()
            first = parts[0] if parts else name
            last = ' '.join(parts[1:]) if len(parts) > 1 else ''
            username = f"access_{name.lower().replace(' ', '_')}"
            if not dry_run:
                user, _ = User.objects.using('pipeline_db').get_or_create(
                    username=username,
                    defaults={'first_name': first, 'last_name': last, 'is_active': True}
                )
                member, _ = Member.objects.using('pipeline_db').get_or_create(
                    user_id=user.pk,
                    defaults={
                        'site_id': self.montreal.pk,
                        'role': 'experimenter',
                        'display_name': name,
                    }
                )
                self.member_map[str(access_id)] = member
            else:
                self.member_map[str(access_id)] = access_id
        self.stdout.write(self.style.SUCCESS(f'    → {len(self.member_map)} members'))

    # =========================================================================
    # Phase 2: Core entities — UPDATED v1.1 (full field mapping)
    # =========================================================================

    def _import_targets(self, rows, dry_run):
        """
        Proteins → Target + Report.
        v1.1: Now maps ALL Proteins columns including SharedInCRAC,
        RequestedAb, RequestCustomKO, etc.
        """
        self.stdout.write(f'\n  Proteins → Targets: {len(rows)} rows')
        report_count = 0
        self._seen_gene = set()
        self._seen_uniprot = set()

        for row in rows:
            access_id = clean_int(row['ID'])
            protein_name = clean_str(row['Protein'])
            if not protein_name:
                continue

            gene = clean_str(row.get('Gene', '')) or None
            if gene:
                if gene in self._seen_gene:
                    self.stdout.write(self.style.WARNING(
                        f'    ⚠ Duplicate gene {gene} on {protein_name} — setting to NULL'
                    ))
                    gene = None
                else:
                    self._seen_gene.add(gene)

            uniprot = clean_str(row.get('Uniprot', '')) or None
            if uniprot:
                if uniprot in self._seen_uniprot:
                    self.stdout.write(self.style.WARNING(
                        f'    ⚠ Duplicate UniProt {uniprot} on {protein_name} — setting to NULL'
                    ))
                    uniprot = None
                else:
                    self._seen_uniprot.add(uniprot)

            status_raw = clean_str(row.get('Status', ''))
            status = STATUS_MAP.get(status_raw, 'in_progress')

            mass_raw = clean_str(row.get('TheoreticalMassINkDa', ''))
            mass = clean_decimal(mass_raw, 2)

            if not dry_run:
                target, _ = Target.objects.using('pipeline_db').update_or_create(
                    access_id=access_id,
                    defaults={
                        'protein_name': protein_name,
                        'gene_name': gene,
                        'alternative_name': clean_str(row.get('AlternProteinName', '')),
                        'uniprot_id': uniprot,
                        'theoretical_mass_kda': mass,
                        'protein_type': clean_str(row.get('Type', '')),
                        'project_id': pk(self.project_map.get(clean_str(row.get('ProjectsID', '')))),
                        'granting_agency_id': pk(self.agency_map.get(clean_str(row.get('GrantingAgenciesID', '')))),
                        'status': status,
                        'commercial_ko_available': clean_bool(row.get('AvailableComercialKO', '')),
                        'requested_commercial_ko': clean_bool(row.get('RequestedComercialKO', '')),
                        'request_custom_ko': clean_bool(row.get('RequestCustomKO', '')),
                        'comments_custom_ko': clean_str(row.get('CommentsCustomKO', '')),
                        'ko_validated': clean_bool(row.get('KOvalidation', '')),
                        'expecting_ab_from': clean_str(row.get('ExpectingAbFrom', '')),
                        'funding_available': clean_bool(row.get('FundingAvailable', '')),
                        'shared_in_crac': clean_date(row.get('SharedInCRAC', '')),
                        'requested_ab_date': clean_date(row.get('RequestedAb', '')),
                        'date_of_nomination': clean_date(row.get('DateOfNomination', '')),
                        'project_comment': clean_str(row.get('ProjectComment', '')),
                        'site_id': self.montreal.pk,
                    }
                )
                self.target_map[str(access_id)] = target

                # Create Report if any DOI or publication date exists
                zenodo_doi = clean_doi(row.get('ZenodoDOI', ''))
                f1000_doi = clean_doi(row.get('F1000DOI', ''))
                zenodo_date = clean_date(row.get('DateAddedZenodo', ''))
                f1000_date = clean_date(row.get('DateAddedF1000', ''))

                if zenodo_doi or f1000_doi or zenodo_date or f1000_date:
                    report_status = 'published' if status == 'published' else 'generated'
                    Report.objects.using('pipeline_db').update_or_create(
                        target_id=target.pk,
                        defaults={
                            'status': report_status,
                            'zenodo_doi': zenodo_doi,
                            'f1000_doi': f1000_doi,
                            'zenodo_date': zenodo_date,
                            'f1000_date': f1000_date,
                        }
                    )
                    report_count += 1
            else:
                self.target_map[str(access_id)] = access_id

        self.stdout.write(self.style.SUCCESS(
            f'    → {len(self.target_map)} targets, {report_count} reports'
        ))

    def _import_cell_lines(self, rows, dry_run):
        """
        CellLines → CellLine + InventoryLocation.
        v1.1: Now maps ALL CellLine columns including LabLabel (c_number),
        Clone, GrowthProperties, Medium, ReceivedDate, PurchasedOrInKind,
        Thawed, LocationOriginalVial, OriginComments, ParentalLine.
        Also handles both sets of -80 storage columns.
        """
        self.stdout.write(f'\n  CellLines: {len(rows)} rows')
        location_count = 0
        geno_map = {'WT': 'WT', 'KO': 'KO', '': 'other'}

        for row in rows:
            access_id = clean_int(row['ID'])
            name = clean_str(row.get('CellLine', ''))
            if not name:
                name = f"CellLine-{access_id}"

            wt_ko = clean_str(row.get('WTorKO', ''))
            genotype = geno_map.get(wt_ko, 'other')
            protein_id = clean_str(row.get('ProteinsID', ''))

            # Acquisition method mapping
            acq_raw = clean_str(row.get('PurchasedOrInKind', ''))
            acquisition = ACQUISITION_MAP.get(acq_raw, 'unknown')

            if not dry_run:
                target_obj = self.target_map.get(protein_id)
                cell_line, _ = CellLine.objects.using('pipeline_db').update_or_create(
                    access_id=access_id,
                    defaults={
                        'name': name,
                        'target_id': pk(target_obj),
                        'genotype': genotype,
                        'c_number': clean_int(row.get('LabLabel', '')),
                        'catalogue_number': clean_str(row.get('CatNumber', '')),
                        'lot_number': clean_str(row.get('Lot', '')),
                        'species': clean_str(row.get('Species', '')) or 'Human',
                        'origin': clean_str(row.get('Origin', '')),
                        'origin_comments': clean_str(row.get('OriginComments', '')),
                        'clone': clean_str(row.get('Clone', '')),
                        'growth_properties': clean_str(row.get('GrowthProperties', '')),
                        'medium': clean_str(row.get('Medium', '')),
                        'parental_line_name': clean_str(row.get('ParentalLine', '')),
                        'acquisition_method': acquisition,
                        'received_date': clean_date(row.get('ReceivedDate', '')),
                        'thawed': clean_bool(row.get('Thawed', '')),
                        'location_original_vial': clean_str(row.get('LocationOriginalVial', '')),
                        'site_id': self.montreal.pk,
                        'ko_validated': clean_bool(row.get('KOconfirmed', '')),
                        'ko_validation_notes': clean_str(row.get('KOInfo', '')),
                    }
                )
                self.cell_line_map[str(access_id)] = cell_line

                # LN2 storage location
                tank = clean_str(row.get('TankLN', ''))
                rack_ln = clean_str(row.get('RackLN', ''))
                box_ln = clean_str(row.get('BoxLN', ''))
                vials_ln = clean_str(row.get('VialsLN', ''))
                if tank or rack_ln or box_ln:
                    InventoryLocation.objects.using('pipeline_db').update_or_create(
                        cell_line_id=cell_line.pk,
                        storage_type='ln2',
                        site_id=self.montreal.pk,
                        defaults={
                            'freezer': tank,
                            'rack': rack_ln,
                            'box': box_ln,
                            'position': vials_ln,
                        }
                    )
                    location_count += 1

                # First set of -80 storage (RackNeg80, TrayNeg80, BoxNeg80, VialsNeg80)
                rack_80 = clean_str(row.get('RackNeg80', ''))
                tray_80 = clean_str(row.get('TrayNeg80', ''))
                box_80 = clean_str(row.get('BoxNeg80', ''))
                vials_80 = clean_str(row.get('VialsNeg80', ''))
                if rack_80 or tray_80 or box_80:
                    InventoryLocation.objects.using('pipeline_db').update_or_create(
                        cell_line_id=cell_line.pk,
                        storage_type='-80',
                        site_id=self.montreal.pk,
                        rack=rack_80,
                        defaults={
                            'shelf': tray_80,
                            'box': box_80,
                            'position': vials_80,
                        }
                    )
                    location_count += 1

                # Second set of -80 storage (RackN80, TrayN80, BoxN80, VialsN80)
                rack_n80 = clean_str(row.get('RackN80', ''))
                tray_n80 = clean_str(row.get('TrayN80', ''))
                box_n80 = clean_str(row.get('BoxN80', ''))
                vials_n80 = clean_str(row.get('VialsN80', ''))
                if rack_n80 or tray_n80 or box_n80:
                    # Only create if different from first -80 set
                    if (rack_n80, tray_n80, box_n80) != (rack_80, tray_80, box_80):
                        InventoryLocation.objects.using('pipeline_db').update_or_create(
                            cell_line_id=cell_line.pk,
                            storage_type='-80',
                            site_id=self.montreal.pk,
                            rack=rack_n80,
                            defaults={
                                'shelf': tray_n80,
                                'box': box_n80,
                                'position': vials_n80,
                                'notes': 'Second -80 location (Access RackN80 set)',
                            }
                        )
                        location_count += 1
            else:
                self.cell_line_map[str(access_id)] = access_id

        self.stdout.write(self.style.SUCCESS(
            f'    → {len(self.cell_line_map)} cell lines, {location_count} inventory locations'
        ))

    def _import_antibodies(self, rows, dry_run):
        """
        Antibodies → Antibody + InventoryLocation.
        v1.1: Now maps ALL Antibody columns including BoxNumber (→InventoryLocation),
        Fridge4C, Neg80C (→ InventoryLocation storage_type), RRIDlink, Antigen,
        Link, Test, EmptyVial, Others, ValidatedApplicationsBySup, Createdby.
        Concentration converted from µg/µL to µg/mL (×1000).
        """
        self.stdout.write(f'\n  Antibodies: {len(rows)} rows')
        skipped = 0
        location_count = 0

        for row in rows:
            access_id = clean_int(row['ID'])
            protein_id = clean_str(row.get('ProteinsID', ''))
            target = self.target_map.get(protein_id) if not dry_run else protein_id

            if not dry_run and target is None:
                skipped += 1
                continue

            conc_raw = clean_decimal(row.get('ConcentrationInUgUl', ''), 3)
            concentration = conc_raw * 1000 if conc_raw is not None else None

            clonality_raw = clean_str(row.get('Clonality', ''))
            clonality = CLONALITY_MAP.get(clonality_raw, 'unknown')

            acquisition_raw = clean_str(row.get('PurchasedORinKind', ''))
            acquisition = ACQUISITION_MAP.get(acquisition_raw, 'unknown')

            # Map Createdby text to Member if possible
            created_by_name = clean_str(row.get('Createdby', ''))
            created_by_member = None
            if created_by_name and not dry_run:
                username = f"access_{created_by_name.lower().replace(' ', '_')}"
                try:
                    user = User.objects.using('pipeline_db').get(username=username)
                    created_by_member = Member.objects.using('pipeline_db').get(user_id=user.pk)
                except (User.DoesNotExist, Member.DoesNotExist):
                    pass

            if not dry_run:
                company_obj = self.company_map.get(clean_str(row.get('CompaniesID', '')))
                try:
                    obj, _ = Antibody.objects.using('pipeline_db').update_or_create(
                        access_id=access_id,
                        defaults={
                            'ab_number': clean_int(row.get('AbNumber', '')),
                            'target_id': target.pk,
                            'company_id': pk(company_obj),
                            'catalogue_number': clean_str(row.get('CatNumber', '')),
                            'lot_number': clean_str(row.get('Lot', '')),
                            'rrid': clean_str(row.get('RRID', '')),
                            'rrid_link': clean_hyperlink(row.get('RRIDlink', '')),
                            'clonality': clonality,
                            'clone_id': clean_str(row.get('Clone', '')),
                            'host_species': clean_str(row.get('ExpressionSystem', '')),
                            'concentration': concentration,
                            'isotype': clean_str(row.get('Isotype', '')),
                            'species_reactivity': clean_str(row.get('SpeciesReactivity', '')),
                            'antigen': clean_str(row.get('Antigen', '')),
                            'supplier_validated_wb': clean_bool(row.get('WBvalidatedBySupplier', '')),
                            'supplier_validated_ip': clean_bool(row.get('IPvalidatedBySupplier', '')),
                            'supplier_validated_if': clean_bool(row.get('IFvalidatedBySupplier', '')),
                            'supplier_validated_ihc': clean_bool(row.get('IHCvalidatedBySupplier', '')),
                            'supplier_validated_elisa': clean_bool(row.get('ELISAvalidatedBySupplier', '')),
                            'supplier_validated_fc': clean_bool(row.get('FlowCytValidatedBySupplier', '')),
                            'supplier_validated_applications': clean_str(row.get('SupplierValidatedApplication', '')),
                            'validated_apps_by_supplier': clean_str(row.get('ValidatedApplicationsBySup', '')),
                            'validation_details_wb': clean_str(row.get('ValidationByManufacturerForWB', '')),
                            'validation_details_if': clean_str(row.get('ValidationByManufacturerForIF', '')),
                            'acquisition_method': acquisition,
                            'received_date': clean_date(row.get('ReceivedDate', '')),
                            'out_of_market': clean_bool(row.get('OutOfMarket', '')),
                            'empty_vial': clean_bool(row.get('EmptyVial', '')),
                            'is_test': clean_bool(row.get('Test', '')),
                            'others_flag': clean_bool(row.get('Others', '')),
                            'supplier_url': clean_hyperlink(row.get('Link', '')),
                            'comments': clean_str(row.get('Comments', '')),
                            'site_id': self.montreal.pk,
                            'created_by_id': pk(created_by_member),
                        }
                    )
                except Exception:
                    cat = clean_str(row.get('CatNumber', ''))
                    obj = Antibody.objects.using('pipeline_db').filter(
                        catalogue_number=cat, company_id=pk(company_obj), target_id=target.pk
                    ).first()
                    if obj:
                        self.stdout.write(self.style.WARNING(
                            f'    ⚠ Duplicate antibody {cat} for target {target} — using existing'
                        ))
                    else:
                        skipped += 1
                        continue
                self.antibody_map[str(access_id)] = obj

                # Create InventoryLocation for antibody storage
                box_number = clean_str(row.get('BoxNumber', ''))
                fridge_4c = clean_bool(row.get('Fridge4C', ''))
                neg80 = clean_bool(row.get('Neg80C', ''))

                if box_number or fridge_4c or neg80:
                    if fridge_4c:
                        storage_type = '4c'
                    elif neg80:
                        storage_type = '-80'
                    else:
                        storage_type = '-20'  # default if box but no temp specified

                    InventoryLocation.objects.using('pipeline_db').update_or_create(
                        antibody_id=obj.pk,
                        site_id=self.montreal.pk,
                        defaults={
                            'storage_type': storage_type,
                            'box': box_number,
                        }
                    )
                    location_count += 1

        msg = f'    → {len(self.antibody_map)} antibodies, {location_count} storage locations'
        if skipped:
            msg += f' ({skipped} skipped — no matching target)'
        self.stdout.write(self.style.SUCCESS(msg))

    # =========================================================================
    # Phase 3: Experiment data (session grouping — unchanged from v1.0)
    # =========================================================================

    def _get_or_create_session(self, procedure_type, antibody_access_id,
                                member_access_id, when_raw, dry_run,
                                cell_line_wt_id=None, cell_line_ko_id=None):
        antibody = self.antibody_map.get(clean_str(antibody_access_id))
        if not antibody or dry_run:
            return None, False

        target = antibody.target
        if not target:
            return None, False

        session_key = (procedure_type, target.pk)

        if not hasattr(self, '_session_cache'):
            self._session_cache = {}

        if session_key in self._session_cache:
            return self._session_cache[session_key], False

        experimenter = self.member_map.get(clean_str(member_access_id))
        if not experimenter:
            if not hasattr(self, '_unknown_member'):
                user, _ = User.objects.using('pipeline_db').get_or_create(
                    username='access_unknown',
                    defaults={'first_name': 'Unknown', 'last_name': 'Experimenter', 'is_active': True}
                )
                self._unknown_member, _ = Member.objects.using('pipeline_db').get_or_create(
                    user_id=user.pk,
                    defaults={'site_id': self.montreal.pk, 'role': 'experimenter', 'display_name': 'Unknown (Access import)'}
                )
            experimenter = self._unknown_member

        date = clean_date(when_raw) or datetime(2020, 1, 1).date()

        cell_wt = self.cell_line_map.get(clean_str(cell_line_wt_id))
        cell_ko = self.cell_line_map.get(clean_str(cell_line_ko_id))

        session = ExperimentSession.objects.using('pipeline_db').create(
            procedure_type=procedure_type,
            target_id=target.pk,
            experimenter_id=experimenter.pk,
            date=date,
            site_id=self.montreal.pk,
            cell_line_wt_id=pk(cell_wt),
            cell_line_ko_id=pk(cell_ko),
            status='complete',
        )
        self._session_cache[session_key] = session
        return session, True

    def _import_wb(self, rows, dry_run):
        self.stdout.write(f'\n  Wb → Sessions + WbResult: {len(rows)} rows')
        session_count = 0
        result_count = 0
        skipped = 0

        for row in rows:
            ab_id = clean_str(row.get('AntibodiesID', ''))
            member_id = clean_str(row.get('MembersID', ''))
            lane1 = clean_str(row.get('lane1CellLineID', ''))
            lane2 = clean_str(row.get('lane2CellLineID', ''))

            session, is_new = self._get_or_create_session(
                'WB', ab_id, member_id, row.get('When', ''),
                dry_run, cell_line_wt_id=lane1, cell_line_ko_id=lane2
            )
            if session is None:
                skipped += 1
                continue
            if is_new:
                session_count += 1

            extra_lanes = []
            for i in (3, 4):
                lane_val = clean_str(row.get(f'lane{i}CellLineID', ''))
                if lane_val and lane_val != '0':
                    cl = self.cell_line_map.get(lane_val)
                    extra_lanes.append({
                        'cell_line_id': cl.pk if cl else None,
                        'access_cell_line_id': int(float(lane_val)),
                        'label': f'Lane {i}',
                    })

            antibody = self.antibody_map.get(ab_id)
            if not antibody:
                skipped += 1
                continue

            WbResult.objects.using('pipeline_db').create(
                session_id=session.pk,
                antibody_id=antibody.pk,
                signal=clean_str(row.get('SpecificSignal', '')),
                rating=clean_str(row.get('SelectiveSignal', '')),
                dilution=clean_str(row.get('1AbDilution', '')),
                gel=clean_str(row.get('Gel', '')),
                membrane=clean_str(row.get('Membrane', '')),
                ecl=clean_str(row.get('ECL', '')),
                detection_system=clean_str(row.get('DetectionSystem', '')),
                primary_ab_dilution=clean_str(row.get('1AbDilution', '')),
                secondary_ab=clean_str(row.get('2ndaryAb', '')),
                secondary_ab_dilution=clean_str(row.get('2ndaryAbDilution', '')),
                extra_lanes=extra_lanes if extra_lanes else [],
                comments=clean_str(row.get('Comments', '')),
                access_id=clean_int(row.get('ID', '')),
            )
            result_count += 1

        msg = f'    → {session_count} sessions, {result_count} WB results'
        if skipped:
            msg += f' ({skipped} skipped)'
        self.stdout.write(self.style.SUCCESS(msg))

    def _import_ip(self, rows, dry_run):
        self.stdout.write(f'\n  IP → Sessions + IpResult: {len(rows)} rows')
        session_count = 0
        result_count = 0
        skipped = 0

        for row in rows:
            ab_id = clean_str(row.get('AntibodiesID', ''))
            member_id = clean_str(row.get('MemberID', ''))
            cell_line_id = clean_str(row.get('CellLineID', ''))

            session, is_new = self._get_or_create_session(
                'IP', ab_id, member_id, row.get('When', ''),
                dry_run, cell_line_wt_id=cell_line_id
            )
            if session is None:
                skipped += 1
                continue
            if is_new:
                session_count += 1

            antibody = self.antibody_map.get(ab_id)
            if not antibody:
                skipped += 1
                continue

            IpResult.objects.using('pipeline_db').create(
                session_id=session.pk,
                antibody_id=antibody.pk,
                enrichment=clean_str(row.get('Enrichment', '')),
                amount_of_antibody=clean_str(row.get('AmountOfAb', '')),
                amount_of_lysate=clean_str(row.get('AmountOfLysate', '')),
                protein_concentration=clean_str(row.get('ConcentrationOfProtein(mg/mL)', '')),
                volume_of_lysate_ml=clean_str(row.get('VolumeOfLysateOrMedium(mL)', '')),
                bead_type=clean_str(row.get('Beads', '')),
                lysis_buffer=clean_str(row.get('LysisBuffer', '')),
                detection_ab=clean_str(row.get('1AbUsedForWb', '')),
                detection_ab_dilution=clean_str(row.get('1AbDilution', '')),
                secondary_ab=clean_str(row.get('2ndaryAbForWb', '')),
                secondary_ab_dilution=clean_str(row.get('2ndaryAbDilution', '')),
                gel=clean_str(row.get('Gel', '')),
                membrane=clean_str(row.get('Membrane', '')),
                ecl=clean_str(row.get('ECL', '')),
                detection_system=clean_str(row.get('DetectionSystem', '')),
                comments=clean_str(row.get('Comments', '')),
                access_id=clean_int(row.get('ID', '')),
            )
            result_count += 1

        msg = f'    → {session_count} sessions, {result_count} IP results'
        if skipped:
            msg += f' ({skipped} skipped)'
        self.stdout.write(self.style.SUCCESS(msg))

    def _import_if(self, rows, dry_run):
        self.stdout.write(f'\n  IF → Sessions + IfResult: {len(rows)} rows')
        session_count = 0
        result_count = 0
        skipped = 0

        for row in rows:
            ab_id = clean_str(row.get('AntibodiesID', ''))
            member_id = clean_str(row.get('MemberID', ''))
            cell_line1 = clean_str(row.get('CellLine1ID', ''))
            cell_line2 = clean_str(row.get('CellLine2ID', ''))

            session, is_new = self._get_or_create_session(
                'IF', ab_id, member_id, row.get('When', ''),
                dry_run, cell_line_wt_id=cell_line1, cell_line_ko_id=cell_line2
            )
            if session is None:
                skipped += 1
                continue
            if is_new:
                session_count += 1

            antibody = self.antibody_map.get(ab_id)
            if not antibody:
                skipped += 1
                continue

            IfResult.objects.using('pipeline_db').create(
                session_id=session.pk,
                antibody_id=antibody.pk,
                specific_signal=clean_str(row.get('SpecificSignal', '')),
                wt_ko_ratio_1=clean_decimal(row.get('WTKOratio1', ''), 4),
                wt_ko_ratio_2=clean_decimal(row.get('WTKOratio2', ''), 4),
                concentration_1=clean_str(row.get('TestedConcentration1', '')),
                concentration_2=clean_str(row.get('TestedConcentration2', '')),
                best_concentration=clean_str(row.get('BestConcentration', '')),
                fixative=clean_str(row.get('Fixative', '')),
                blocking=clean_str(row.get('BlockingCondition', '')),
                permeabilisation=clean_str(row.get('Permeabilization', '')),
                primary_ab_dilution=clean_str(row.get('1AbDilution', '')),
                dilution_buffer=clean_str(row.get('DilutionBuffer', '')),
                primary_ab_condition=clean_str(row.get('1AbCondition', '')),
                secondary_ab=clean_str(row.get('SecondaryAb', '')),
                secondary_ab_condition=clean_str(row.get('SecondaryAbCondition', '')),
                plate_number=clean_str(row.get('PlateNumber', '')),
                well_number=clean_str(row.get('WellNumber', '')),
                image_acquired_by=clean_str(row.get('ImageAcquisitionBy', '')),
                image_analysed_by=clean_str(row.get('ImageAnalyzedBy', '')),
                microscope=clean_str(row.get('Microscope', '')),
                objective=clean_str(row.get('Objective', '')),
                comments=clean_str(row.get('Comments', '')),
                access_id=clean_int(row.get('ID', '')),
            )
            result_count += 1

        msg = f'    → {session_count} sessions, {result_count} IF results'
        if skipped:
            msg += f' ({skipped} skipped)'
        self.stdout.write(self.style.SUCCESS(msg))