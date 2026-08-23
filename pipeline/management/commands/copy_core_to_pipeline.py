"""
Management command: copy_core_to_pipeline

Phase 2: Copies OGA-specific data from core SQLite to pipeline PostgreSQL.

What it copies:
  Gene → Target:  citation, aliases, cell_line_link
  Description → Antibody:  supplier_url, out_of_market, is_recombinant,
                            wb/ip/if/fc_recommended
  Experiment → PublicationImage:  cropped publication images

Copy rules:
  - Never overwrites existing pipeline data (copy only when pipeline field is empty/false)
  - Recommendations always copy (pipeline had no recommendation data before)
  - Unmatched core antibodies are created as new pipeline records
  - Dry-run mode by default — pass --commit to actually write

Usage:
    python manage.py copy_core_to_pipeline              # dry run
    python manage.py copy_core_to_pipeline --commit      # actually write
    python manage.py copy_core_to_pipeline --commit --csv # write + CSV log

Place in: pipeline/management/commands/copy_core_to_pipeline.py
"""

import csv
import os
import re
import shutil
from django.core.management.base import BaseCommand
from django.conf import settings


# ─── Matching logic (same as verify_core_pipeline_mapping v4) ─────

GENE_ALIASES = {
    'GBA1 (GCASE)': 'GBA1',
    'SHIP1(INPP5D)': 'INPP5D',
    'VLCS': 'SLC27A2',
}

ARP_SUFFIXES = ['_P050', '_T100', '-P050', '-T100', '_P50', '-P50']
PROTEINTECH_SUFFIXES = ['-Ig', '-lg', '-AP', '-RR']


def clean_catalogue_number(cat):
    return cat.rstrip('*').strip()


def normalise_ig_lg(s):
    return re.sub(r'[-]?[IiLl][Gg]$', '-IG', s)


def strip_parenthetical(s):
    return re.sub(r'\s*\(.*\)\s*$', '', s).strip()


def normalise_for_compare(s):
    s = normalise_ig_lg(s)
    s = s.upper()
    return re.sub(r'[\s\-_]', '', s)


def find_pipeline_antibody(cat_raw, target_id, pipeline_ab_lookup, pipeline_ab_normalised, pipeline_ab_by_target):
    cat_clean = clean_catalogue_number(cat_raw)
    cat_upper = cat_clean.upper()

    key = (target_id, cat_upper)
    if key in pipeline_ab_lookup:
        return pipeline_ab_lookup[key], 'exact'

    cat_norm = normalise_for_compare(cat_clean)
    norm_key = (target_id, cat_norm)
    if norm_key in pipeline_ab_normalised:
        return pipeline_ab_normalised[norm_key], 'normalised'

    if cat_upper.startswith('ARP'):
        already_suffixed = bool(re.search(r'[_\-][PT]\d+$', cat_upper, re.IGNORECASE))
        if already_suffixed:
            for variant in [
                cat_clean.replace('_', '-'),
                cat_clean.replace('-', '_'),
                re.sub(r'P0?50$', 'P050', cat_clean),
                re.sub(r'P0?50$', 'P50', cat_clean),
            ]:
                vkey = (target_id, variant.upper())
                if vkey in pipeline_ab_lookup:
                    return pipeline_ab_lookup[vkey], 'arp_variant'
                for swap in [variant.replace('_', '-'), variant.replace('-', '_')]:
                    skey = (target_id, swap.upper())
                    if skey in pipeline_ab_lookup:
                        return pipeline_ab_lookup[skey], 'arp_variant'
        else:
            for suffix in ARP_SUFFIXES:
                skey = (target_id, (cat_clean + suffix).upper())
                if skey in pipeline_ab_lookup:
                    return pipeline_ab_lookup[skey], f'arp_suffix'

    if re.match(r'^\d{4,6}(-\d+)?$', cat_clean):
        for suffix in PROTEINTECH_SUFFIXES:
            skey = (target_id, (cat_clean + suffix).upper())
            if skey in pipeline_ab_lookup:
                return pipeline_ab_lookup[skey], 'proteintech_suffix'

    if re.match(r'^\d{3,6}$', cat_clean):
        for suffix in ['S', 's']:
            skey = (target_id, (cat_clean + suffix).upper())
            if skey in pipeline_ab_lookup:
                return pipeline_ab_lookup[skey], 'cst_s_suffix'
    for suffix in ['-s', 'S']:
        skey = (target_id, (cat_clean + suffix).upper())
        if skey in pipeline_ab_lookup:
            return pipeline_ab_lookup[skey], 'cst_s_suffix'

    if target_id in pipeline_ab_by_target:
        cat_compare = normalise_for_compare(cat_clean)
        for pab in pipeline_ab_by_target[target_id]:
            pcat_stripped = strip_parenthetical(pab.catalogue_number)
            if normalise_for_compare(pcat_stripped) == cat_compare:
                return pab, 'parenthetical_prefix'

    return None, None


class Command(BaseCommand):
    help = 'Copy OGA-specific data from core SQLite to pipeline PostgreSQL'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Actually write to pipeline_db (default is dry run)',
        )
        parser.add_argument(
            '--csv', action='store_true',
            help='Write CSV log of all operations',
        )

    def handle(self, *args, **options):
        from core.models import Gene, Antibody as CoreAntibody, Description, Experiment
        from pipeline.models import Target, Antibody as PipelineAntibody, Company, PublicationImage

        commit = options['commit']
        write_csv = options['csv']

        mode = 'COMMIT' if commit else 'DRY RUN'
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== Phase 2: Copy Core → Pipeline ({mode}) ===\n'
        ))

        # ─── Build lookups ────────────────────────────────────

        pipeline_targets = {
            t.gene_name.strip().upper(): t
            for t in Target.objects.using('pipeline_db').all() if t.gene_name
        }

        pipeline_ab_lookup = {}
        pipeline_ab_normalised = {}
        pipeline_ab_by_target = {}
        for pab in PipelineAntibody.objects.using('pipeline_db').select_related('target', 'company').all():
            if pab.target and pab.catalogue_number:
                cat = pab.catalogue_number.strip()
                key = (pab.target_id, cat.upper())
                pipeline_ab_lookup[key] = pab
                norm_key = (pab.target_id, normalise_for_compare(cat))
                if norm_key not in pipeline_ab_normalised:
                    pipeline_ab_normalised[norm_key] = pab
                pipeline_ab_by_target.setdefault(pab.target_id, []).append(pab)

        # Company lookup for creating new antibodies
        pipeline_companies = {
            c.name.strip().lower(): c
            for c in Company.objects.using('pipeline_db').all()
        }

        # Stats
        stats = {
            'genes_updated': 0,
            'gene_citation_copied': 0,
            'gene_aliases_copied': 0,
            'gene_cell_line_link_copied': 0,
            'ab_matched': 0,
            'ab_created': 0,
            'supplier_url_copied': 0,
            'out_of_market_copied': 0,
            'is_recombinant_copied': 0,
            'wb_rec_copied': 0,
            'ip_rec_copied': 0,
            'if_rec_copied': 0,
            'fc_rec_copied': 0,
            'pub_images_created': 0,
            'pub_images_skipped': 0,
        }

        operations_log = []

        # ─── 1. Gene → Target ────────────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING('\n--- Copying Gene → Target fields ---\n'))

        for gene in Gene.objects.using('default').all():
            key = gene.name.strip().upper()
            if key in GENE_ALIASES:
                key = GENE_ALIASES[key].upper()
            target = pipeline_targets.get(key)
            if not target:
                continue

            updated_fields = []

            if gene.citation and not target.citation:
                target.citation = gene.citation
                updated_fields.append('citation')
                stats['gene_citation_copied'] += 1

            if gene.aliases and not target.aliases:
                target.aliases = gene.aliases
                updated_fields.append('aliases')
                stats['gene_aliases_copied'] += 1

            if gene.cell_line_link and not target.cell_line_link:
                target.cell_line_link = gene.cell_line_link
                updated_fields.append('cell_line_link')
                stats['gene_cell_line_link_copied'] += 1

            if updated_fields:
                if commit:
                    target.save(using='pipeline_db', update_fields=updated_fields)
                stats['genes_updated'] += 1
                operations_log.append({
                    'operation': 'update_target',
                    'core_gene': gene.name,
                    'pipeline_target_id': target.id,
                    'fields_updated': ','.join(updated_fields),
                })

        self.stdout.write(f'  Targets updated:      {stats["genes_updated"]}')
        self.stdout.write(f'  Citations copied:     {stats["gene_citation_copied"]}')
        self.stdout.write(f'  Aliases copied:       {stats["gene_aliases_copied"]}')
        self.stdout.write(f'  Cell line links:      {stats["gene_cell_line_link_copied"]}')

        # ─── 2. Antibody matching and field copy ──────────────

        self.stdout.write(self.style.MIGRATE_HEADING('\n--- Copying Antibody fields ---\n'))

        # Build gene_id → target mapping
        gene_to_target = {}
        for gene in Gene.objects.using('default').all():
            key = gene.name.strip().upper()
            if key in GENE_ALIASES:
                key = GENE_ALIASES[key].upper()
            t = pipeline_targets.get(key)
            if t:
                gene_to_target[gene.id] = t

        for cab in CoreAntibody.objects.using('default').select_related('gene').all():
            target = gene_to_target.get(cab.gene_id)
            if not target:
                continue

            try:
                desc = Description.objects.using('default').get(antibody_id=cab.id)
            except Description.DoesNotExist:
                desc = None

            pab, method = find_pipeline_antibody(
                cab.name, target.id,
                pipeline_ab_lookup, pipeline_ab_normalised, pipeline_ab_by_target
            )

            if pab:
                # ── Update existing pipeline antibody ──
                stats['ab_matched'] += 1
                updated_fields = []

                if desc:
                    # supplier_url (product_link) — copy if pipeline empty
                    core_link = (desc.product_link or '').strip()
                    if core_link and not (pab.supplier_url or '').strip():
                        pab.supplier_url = core_link
                        updated_fields.append('supplier_url')
                        stats['supplier_url_copied'] += 1

                    # out_of_market (discontinued) — copy if pipeline is False
                    if desc.discontinued and not pab.out_of_market:
                        pab.out_of_market = True
                        updated_fields.append('out_of_market')
                        stats['out_of_market_copied'] += 1

                    # is_recombinant — copy if pipeline is False
                    core_rec = (desc.recombinant or '').strip().lower()
                    if core_rec in ('yes', 'true', 'recombinant') and not pab.is_recombinant:
                        pab.is_recombinant = True
                        updated_fields.append('is_recombinant')
                        stats['is_recombinant_copied'] += 1

                    # Recommendations — always copy (pipeline had none before)
                    if desc.wb_app:
                        pab.wb_recommended = True
                        updated_fields.append('wb_recommended')
                        stats['wb_rec_copied'] += 1
                    if desc.ip_app:
                        pab.ip_recommended = True
                        updated_fields.append('ip_recommended')
                        stats['ip_rec_copied'] += 1
                    if desc.icc_if_app:
                        pab.if_recommended = True
                        updated_fields.append('if_recommended')
                        stats['if_rec_copied'] += 1
                    if desc.fc_app:
                        pab.fc_recommended = True
                        updated_fields.append('fc_recommended')
                        stats['fc_rec_copied'] += 1

                if updated_fields and commit:
                    pab.save(using='pipeline_db', update_fields=updated_fields)

                if updated_fields:
                    operations_log.append({
                        'operation': 'update_antibody',
                        'core_ab_name': cab.name,
                        'core_gene': cab.gene.name,
                        'pipeline_ab_id': pab.id,
                        'match_method': method,
                        'fields_updated': ','.join(updated_fields),
                    })

                # ── Copy publication images ──
                self._copy_publication_images(
                    cab, pab, commit, stats, operations_log
                )

            else:
                # ── Create new pipeline antibody ──
                stats['ab_created'] += 1

                # Try to find the company in pipeline
                supplier_name = (desc.supplier if desc else '') or ''
                company = pipeline_companies.get(supplier_name.strip().lower())

                new_ab_data = {
                    'target': target,
                    'company': company,
                    'catalogue_number': clean_catalogue_number(cab.name),
                    'rrid': (desc.rrid if desc else '') or '',
                    'host_species': (desc.host if desc else '') or '',
                    'clone_id': (desc.clone_ID if desc else '') or '',
                    'supplier_url': (desc.product_link if desc else '') or '',
                    'out_of_market': desc.discontinued if desc else False,
                    'is_recombinant': (desc.recombinant or '').strip().lower() in ('yes', 'true', 'recombinant') if desc else False,
                    'wb_recommended': desc.wb_app if desc else False,
                    'ip_recommended': desc.ip_app if desc else False,
                    'if_recommended': desc.icc_if_app if desc else False,
                    'fc_recommended': desc.fc_app if desc else False,
                }

                # Map clonality
                if desc and desc.clonality:
                    cl = desc.clonality.strip().lower()
                    if cl in ('monoclonal', 'polyclonal', 'recombinant'):
                        new_ab_data['clonality'] = cl
                    else:
                        new_ab_data['clonality'] = 'unknown'

                if commit:
                    new_pab = PipelineAntibody.objects.using('pipeline_db').create(**new_ab_data)
                    # Copy images for the new antibody
                    self._copy_publication_images(
                        cab, new_pab, commit, stats, operations_log
                    )
                else:
                    # Log what would be created
                    self._copy_publication_images(
                        cab, None, False, stats, operations_log
                    )

                operations_log.append({
                    'operation': 'create_antibody',
                    'core_ab_name': cab.name,
                    'core_gene': cab.gene.name,
                    'pipeline_target_id': target.id,
                    'catalogue_number': new_ab_data['catalogue_number'],
                    'supplier': supplier_name,
                    'company_found': company is not None,
                })

        self.stdout.write(f'  Antibodies matched:   {stats["ab_matched"]}')
        self.stdout.write(f'  Antibodies created:   {stats["ab_created"]}')
        self.stdout.write(f'  supplier_url copied:  {stats["supplier_url_copied"]}')
        self.stdout.write(f'  out_of_market copied: {stats["out_of_market_copied"]}')
        self.stdout.write(f'  is_recombinant:       {stats["is_recombinant_copied"]}')
        self.stdout.write(f'  WB recommended:       {stats["wb_rec_copied"]}')
        self.stdout.write(f'  IP recommended:       {stats["ip_rec_copied"]}')
        self.stdout.write(f'  IF recommended:       {stats["if_rec_copied"]}')
        self.stdout.write(f'  FC recommended:       {stats["fc_rec_copied"]}')

        self.stdout.write(self.style.MIGRATE_HEADING('\n--- Publication Images ---\n'))
        self.stdout.write(f'  Images created:       {stats["pub_images_created"]}')
        self.stdout.write(f'  Images skipped:       {stats["pub_images_skipped"]}')

        # ─── CSV output ───────────────────────────────────────

        if write_csv and operations_log:
            report_dir = os.path.join('pipeline', 'mapping_reports')
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir, 'copy_operations.csv')

            # Collect all keys across all operations
            all_keys = set()
            for op in operations_log:
                all_keys.update(op.keys())
            all_keys = sorted(all_keys)

            with open(path, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=all_keys, extrasaction='ignore')
                w.writeheader()
                for op in operations_log:
                    w.writerow(op)
            self.stdout.write(f'\nWrote {path}')

        # ─── Summary ─────────────────────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== SUMMARY ({mode}) ===\n'))
        self.stdout.write(f'Targets updated:        {stats["genes_updated"]}')
        self.stdout.write(f'Antibodies updated:     {stats["ab_matched"]}')
        self.stdout.write(f'Antibodies created:     {stats["ab_created"]}')
        self.stdout.write(f'Publication images:     {stats["pub_images_created"]}')

        if not commit:
            self.stdout.write(self.style.WARNING(
                '\nThis was a DRY RUN. No data was written. '
                'Run with --commit to apply changes.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('\n✓ Data copy complete.'))

    def _copy_publication_images(self, core_ab, pipeline_ab, commit, stats, log):
        """Copy core Experiment images to PublicationImage records."""
        from core.models import Experiment
        from pipeline.models import PublicationImage

        # Map core experiment types to pipeline application types
        type_map = {
            'WB': 'WB',
            'IP': 'IP',
            'ICC-IF': 'ICC-IF',
            'FC': 'FC',
        }

        for exp in Experiment.objects.using('default').filter(antibody_id=core_ab.id):
            if not exp.file_path:
                stats['pub_images_skipped'] += 1
                continue

            app_type = type_map.get(exp.experiment_type)
            if not app_type:
                stats['pub_images_skipped'] += 1
                continue

            if commit and pipeline_ab:
                # Check if already exists
                exists = PublicationImage.objects.using('pipeline_db').filter(
                    antibody=pipeline_ab,
                    application_type=app_type,
                ).exists()

                if exists:
                    stats['pub_images_skipped'] += 1
                    continue

                # Create the PublicationImage record pointing to the same file path
                # The actual file stays in media/ for now — image storage migration
                # (Phase 5) will move files to S3 later
                PublicationImage.objects.using('pipeline_db').create(
                    antibody=pipeline_ab,
                    application_type=app_type,
                    image=str(exp.file_path),
                )

            stats['pub_images_created'] += 1
            log.append({
                'operation': 'create_pub_image',
                'core_ab_name': core_ab.name,
                'application_type': app_type,
                'file_path': str(exp.file_path),
                'pipeline_ab_id': pipeline_ab.id if pipeline_ab else 'NEW',
            })
