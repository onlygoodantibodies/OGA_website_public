"""
Management command: verify_core_pipeline_mapping (v4)

Matching rules (applied in order):
  1. Exact match on cleaned catalogue number (strip * and **)
  2. Normalised match: Ig/lg→IG, strip spaces/dashes/underscores
  3. ARP suffix: _P050, _T100, -P050, -T100, _P50, -P50
  4. Already-suffixed ARP: dash↔underscore, P050↔P50
  5. Proteintech suffix: try appending -Ig, -lg, -AP, -RR
  6. CST S suffix: try appending S or -s
  7. Parenthetical prefix: mAB30 → "mAB 30 (asv30)"

Usage:
    python manage.py verify_core_pipeline_mapping
    python manage.py verify_core_pipeline_mapping --csv
"""

import csv
import os
import re
from django.core.management.base import BaseCommand

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
    """Normalise Ig/lg/IG/LG variants to a canonical form before comparison.
    Replace all case variants of -Ig, -lg at end of string with -IG."""
    return re.sub(r'[-]?[IiLl][Gg]$', '-IG', s)


def strip_parenthetical(s):
    return re.sub(r'\s*\(.*\)\s*$', '', s).strip()


def normalise_for_compare(s):
    """Aggressive normalisation for comparison."""
    # First normalise Ig/lg BEFORE uppercasing
    s = normalise_ig_lg(s)
    s = s.upper()
    # Strip spaces, dashes, underscores
    return re.sub(r'[\s\-_]', '', s)


def find_pipeline_antibody(cat_raw, target_id, pipeline_ab_lookup, pipeline_ab_normalised, pipeline_ab_by_target):
    """
    Try to match a core catalogue number to a pipeline antibody.
    Returns (pipeline_antibody, match_method) or (None, None).
    """
    cat_clean = clean_catalogue_number(cat_raw)
    cat_upper = cat_clean.upper()

    # 1. Exact match on cleaned catalogue number
    key = (target_id, cat_upper)
    if key in pipeline_ab_lookup:
        return pipeline_ab_lookup[key], 'exact'

    # 2. Normalised match (handles Ig/lg, spacing, dashes)
    cat_norm = normalise_for_compare(cat_clean)
    norm_key = (target_id, cat_norm)
    if norm_key in pipeline_ab_normalised:
        return pipeline_ab_normalised[norm_key], 'normalised'

    # 3. ARP suffix (only if not already suffixed)
    if cat_upper.startswith('ARP'):
        already_suffixed = bool(re.search(r'[_\-][PT]\d+$', cat_upper, re.IGNORECASE))

        if already_suffixed:
            # 4. Already has suffix — try normalising underscore↔dash, P050↔P50
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
                    return pipeline_ab_lookup[skey], f'arp_suffix ({suffix})'

    # 5. Proteintech suffix: -Ig, -lg, -AP, -RR
    if re.match(r'^\d{4,6}(-\d+)?$', cat_clean):
        for suffix in PROTEINTECH_SUFFIXES:
            skey = (target_id, (cat_clean + suffix).upper())
            if skey in pipeline_ab_lookup:
                return pipeline_ab_lookup[skey], f'proteintech_suffix ({suffix})'

    # 6. CST S suffix: try appending S or -s
    # For bare numeric catalogue numbers (Cell Signaling style)
    if re.match(r'^\d{3,6}$', cat_clean):
        for suffix in ['S', 's']:
            skey = (target_id, (cat_clean + suffix).upper())
            if skey in pipeline_ab_lookup:
                return pipeline_ab_lookup[skey], 'cst_s_suffix'
    # Also try -s for alphanumeric
    for suffix in ['-s', 'S']:
        skey = (target_id, (cat_clean + suffix).upper())
        if skey in pipeline_ab_lookup:
            return pipeline_ab_lookup[skey], 'cst_s_suffix'

    # 7. Parenthetical prefix match
    if target_id in pipeline_ab_by_target:
        cat_compare = normalise_for_compare(cat_clean)
        for pab in pipeline_ab_by_target[target_id]:
            pcat_stripped = strip_parenthetical(pab.catalogue_number)
            if normalise_for_compare(pcat_stripped) == cat_compare:
                return pab, 'parenthetical_prefix'

    return None, None


class Command(BaseCommand):
    help = 'Verify mapping between core Gene/Antibody and pipeline Target/Antibody (v4)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv', action='store_true',
            help='Write detailed CSV reports to pipeline/mapping_reports/',
        )

    def handle(self, *args, **options):
        from core.models import Gene, Antibody as CoreAntibody, Description, Experiment
        from pipeline.models import Target, Antibody as PipelineAntibody

        write_csv = options['csv']

        self.stdout.write(self.style.MIGRATE_HEADING('\n=== PHASE 0: Core ↔ Pipeline Mapping Verification (v4) ===\n'))

        # ─── 1. Gene → Target matching ───────────────────────

        core_genes = Gene.objects.using('default').all().order_by('name')
        pipeline_targets = {
            t.gene_name.strip().upper(): t
            for t in Target.objects.using('pipeline_db').all() if t.gene_name
        }

        gene_matched = []
        gene_unmatched = []

        for gene in core_genes:
            key = gene.name.strip().upper()
            target = pipeline_targets.get(key)

            if not target and key in GENE_ALIASES:
                alias_key = GENE_ALIASES[key].upper()
                target = pipeline_targets.get(alias_key)
                if target:
                    self.stdout.write(f'  Gene alias: "{gene.name}" → "{target.gene_name}"')

            if target:
                gene_matched.append({
                    'core_gene_id': gene.id,
                    'core_gene_name': gene.name,
                    'pipeline_target_id': target.id,
                    'pipeline_gene_name': target.gene_name,
                    'pipeline_protein_name': target.protein_name,
                    'core_has_citation': bool(gene.citation),
                    'core_has_aliases': bool(gene.aliases),
                    'core_has_f1000_link': bool(gene.f1000_report_link),
                    'core_has_cell_line_link': bool(gene.cell_line_link),
                })
            else:
                gene_unmatched.append({
                    'core_gene_id': gene.id,
                    'core_gene_name': gene.name,
                })

        self.stdout.write(f'\nCore genes total:       {core_genes.count()}')
        self.stdout.write(f'Pipeline targets total: {len(pipeline_targets)}')
        self.stdout.write(self.style.SUCCESS(f'Genes matched:          {len(gene_matched)}'))
        if gene_unmatched:
            self.stdout.write(self.style.ERROR(f'Genes UNMATCHED:        {len(gene_unmatched)}'))
            for g in gene_unmatched:
                self.stdout.write(f'  ✗ {g["core_gene_name"]} (core id={g["core_gene_id"]})')
        else:
            self.stdout.write(self.style.SUCCESS('All core genes matched to pipeline targets.'))

        citation_count = sum(1 for g in gene_matched if g['core_has_citation'])
        aliases_count = sum(1 for g in gene_matched if g['core_has_aliases'])
        f1000_count = sum(1 for g in gene_matched if g['core_has_f1000_link'])
        cell_line_link_count = sum(1 for g in gene_matched if g['core_has_cell_line_link'])

        self.stdout.write(f'\nGene-level field usage (of {len(gene_matched)} matched):')
        self.stdout.write(f'  citation:        {citation_count}')
        self.stdout.write(f'  aliases:         {aliases_count}')
        self.stdout.write(f'  f1000_link:      {f1000_count}')
        self.stdout.write(f'  cell_line_link:  {cell_line_link_count}')

        # ─── 2. Antibody matching ─────────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING('\n--- Antibody Matching ---\n'))

        # Build lookups
        pipeline_ab_lookup = {}       # (target_id, CAT_UPPER) → PipelineAntibody
        pipeline_ab_normalised = {}   # (target_id, NORMALISED) → PipelineAntibody
        pipeline_ab_by_target = {}    # target_id → [PipelineAntibody, ...]

        for pab in PipelineAntibody.objects.using('pipeline_db').select_related('target', 'company').all():
            if pab.target and pab.catalogue_number:
                cat = pab.catalogue_number.strip()
                key = (pab.target_id, cat.upper())
                pipeline_ab_lookup[key] = pab

                norm_key = (pab.target_id, normalise_for_compare(cat))
                # Don't overwrite — first match wins
                if norm_key not in pipeline_ab_normalised:
                    pipeline_ab_normalised[norm_key] = pab

                pipeline_ab_by_target.setdefault(pab.target_id, []).append(pab)

        # Build gene_id → target mapping
        gene_to_target = {}
        for gm in gene_matched:
            gene_to_target[gm['core_gene_id']] = pipeline_targets.get(
                gm['pipeline_gene_name'].strip().upper()
            )

        core_antibodies = CoreAntibody.objects.using('default').select_related('gene').all().order_by('gene__name', 'name')

        ab_matched = []
        ab_unmatched = []
        ab_gene_unmatched = []
        match_methods = {}

        for cab in core_antibodies:
            target = gene_to_target.get(cab.gene_id)
            if not target:
                ab_gene_unmatched.append({
                    'core_ab_id': cab.id,
                    'core_ab_name': cab.name,
                    'core_gene_name': cab.gene.name,
                    'reason': 'gene_not_matched',
                })
                continue

            pab, method = find_pipeline_antibody(
                cab.name, target.id,
                pipeline_ab_lookup, pipeline_ab_normalised, pipeline_ab_by_target
            )

            if pab:
                match_methods[method] = match_methods.get(method, 0) + 1

                try:
                    desc = Description.objects.using('default').get(antibody_id=cab.id)
                except Description.DoesNotExist:
                    desc = None

                exps = Experiment.objects.using('default').filter(antibody_id=cab.id)
                exp_types = [e.experiment_type for e in exps if e.file_path]

                ab_matched.append({
                    'core_ab_id': cab.id,
                    'core_ab_name': cab.name,
                    'core_gene_name': cab.gene.name,
                    'pipeline_ab_id': pab.id,
                    'pipeline_cat_number': pab.catalogue_number,
                    'pipeline_target_gene': target.gene_name,
                    'pipeline_company': pab.company.name if pab.company else '',
                    'match_method': method,
                    'core_rrid': desc.rrid if desc else '',
                    'core_supplier': desc.supplier if desc else '',
                    'core_host': desc.host if desc else '',
                    'core_clonality': desc.clonality if desc else '',
                    'core_clone_id': desc.clone_ID if desc else '',
                    'core_recombinant': desc.recombinant if desc else '',
                    'core_product_link': desc.product_link if desc else '',
                    'core_discontinued': desc.discontinued if desc else False,
                    'core_wb_app': desc.wb_app if desc else False,
                    'core_ip_app': desc.ip_app if desc else False,
                    'core_icc_if_app': desc.icc_if_app if desc else False,
                    'core_fc_app': desc.fc_app if desc else False,
                    'core_has_description': desc is not None,
                    'core_experiment_images': ','.join(exp_types),
                    'core_experiment_count': len(exp_types),
                    'pipeline_rrid': pab.rrid,
                    'pipeline_clonality': pab.clonality,
                    'pipeline_host': pab.host_species,
                })
            else:
                ab_unmatched.append({
                    'core_ab_id': cab.id,
                    'core_ab_name': cab.name,
                    'core_ab_name_cleaned': clean_catalogue_number(cab.name),
                    'core_gene_name': cab.gene.name,
                    'pipeline_target_id': target.id,
                    'pipeline_target_gene': target.gene_name,
                    'reason': 'no_catalogue_match',
                })

        total_core_abs = core_antibodies.count()
        self.stdout.write(f'Core antibodies total:      {total_core_abs}')
        self.stdout.write(self.style.SUCCESS(f'Antibodies matched:         {len(ab_matched)}'))
        if ab_unmatched:
            self.stdout.write(self.style.WARNING(f'Antibodies UNMATCHED:       {len(ab_unmatched)} (gene matched but catalogue number not found)'))
        if ab_gene_unmatched:
            self.stdout.write(self.style.ERROR(f'Antibodies GENE UNMATCHED:  {len(ab_gene_unmatched)} (parent gene not in pipeline)'))

        self.stdout.write(f'\nMatch methods:')
        for method, count in sorted(match_methods.items()):
            self.stdout.write(f'  {method}: {count}')

        # ─── 3. Data to copy ─────────────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING('\n--- Data to Copy (from matched antibodies) ---\n'))

        stats = {
            'Description': sum(1 for a in ab_matched if a['core_has_description']),
            'recommendations': sum(1 for a in ab_matched if any([
                a['core_wb_app'], a['core_ip_app'], a['core_icc_if_app'], a['core_fc_app']
            ])),
            'product_link': sum(1 for a in ab_matched if a['core_product_link']),
            'discontinued': sum(1 for a in ab_matched if a['core_discontinued']),
            'recombinant': sum(1 for a in ab_matched if a['core_recombinant']),
        }
        has_images = sum(1 for a in ab_matched if a['core_experiment_count'] > 0)
        total_images = sum(a['core_experiment_count'] for a in ab_matched)

        for label, count in stats.items():
            self.stdout.write(f'  With {label:20s} {count}')
        self.stdout.write(f'  With {"pub images":20s} {has_images} ({total_images} total images)')

        # ─── 4. Supplier comparison ───────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING('\n--- Supplier Name Comparison ---\n'))

        mismatches = []
        for a in ab_matched:
            if a['core_supplier'] and a['pipeline_company']:
                if a['core_supplier'].strip().lower() != a['pipeline_company'].strip().lower():
                    mismatches.append(a)

        if mismatches:
            self.stdout.write(self.style.WARNING(f'Supplier name mismatches: {len(mismatches)}'))
            seen = set()
            for m in mismatches:
                pair = (m['core_supplier'], m['pipeline_company'])
                if pair not in seen:
                    seen.add(pair)
                    self.stdout.write(f'  core: "{m["core_supplier"]}" → pipeline: "{m["pipeline_company"]}"')
        else:
            self.stdout.write(self.style.SUCCESS('No supplier name mismatches found.'))

        # ─── 5. Unmatched ─────────────────────────────────────

        if ab_unmatched:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n--- Unmatched Antibodies ({len(ab_unmatched)} total) ---\n'))
            for a in ab_unmatched:
                self.stdout.write(
                    f'  ✗ Gene={a["core_gene_name"]} '
                    f'CatNo="{a["core_ab_name"]}" '
                    f'cleaned="{a["core_ab_name_cleaned"]}" '
                    f'(core id={a["core_ab_id"]})'
                )

        # ─── 6. CSV output ────────────────────────────────────

        if write_csv:
            report_dir = os.path.join('pipeline', 'mapping_reports')
            os.makedirs(report_dir, exist_ok=True)

            for name, data in [
                ('genes_matched', gene_matched),
                ('genes_unmatched', gene_unmatched),
                ('antibodies_matched', ab_matched),
                ('antibodies_unmatched', ab_unmatched),
            ]:
                if data:
                    path = os.path.join(report_dir, f'{name}.csv')
                    with open(path, 'w', newline='') as f:
                        w = csv.DictWriter(f, fieldnames=data[0].keys())
                        w.writeheader()
                        w.writerows(data)
                    self.stdout.write(f'\nWrote {path}')

            if mismatches:
                path = os.path.join(report_dir, 'supplier_mismatches.csv')
                fields = ['core_ab_name', 'core_gene_name', 'core_supplier', 'pipeline_company']
                with open(path, 'w', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    w.writeheader()
                    for m in mismatches:
                        w.writerow({k: m[k] for k in fields})
                self.stdout.write(f'Wrote {path}')

        # ─── Summary ─────────────────────────────────────────

        self.stdout.write(self.style.MIGRATE_HEADING('\n=== SUMMARY ===\n'))
        self.stdout.write(f'Genes:      {len(gene_matched)}/{core_genes.count()} matched')
        self.stdout.write(f'Antibodies: {len(ab_matched)}/{total_core_abs} matched')
        pct = (len(ab_matched) / total_core_abs * 100) if total_core_abs else 0
        self.stdout.write(f'Match rate: {pct:.1f}%')

        if not gene_unmatched and not ab_unmatched and not ab_gene_unmatched:
            self.stdout.write(self.style.SUCCESS(
                '\n✓ All core records have pipeline matches. Safe to proceed to Phase 1.'
            ))
        else:
            remaining = len(ab_unmatched) + len(ab_gene_unmatched)
            self.stdout.write(self.style.WARNING(
                f'\n⚠ {remaining} antibodies still unmatched. '
                f'These will be created as new pipeline records during data copy.'
            ))
