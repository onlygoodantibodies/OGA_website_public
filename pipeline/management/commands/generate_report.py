"""
Django management command to generate a .docx report for a target.

Usage:
    # Generate by target primary key
    python manage.py generate_report 42

    # Generate by gene name
    python manage.py generate_report --gene SYT1

    # Specify output path
    python manage.py generate_report 42 --output /path/to/report.docx

    # Generate for all report-ready targets
    python manage.py generate_report --all-ready

    # Dry run — show what would be generated without writing files
    python manage.py generate_report --gene SYT1 --dry-run
"""

import sys
from django.core.management.base import BaseCommand, CommandError


DB_ALIAS = 'pipeline_db'


class Command(BaseCommand):
    help = (
        'Generate a .docx report for a target in Zenodo/F1000 format. '
        'Specify a target PK, --gene name, or --all-ready.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'target_pk',
            nargs='?',
            type=int,
            help='Primary key of the Target to generate a report for.',
        )
        parser.add_argument(
            '--gene',
            type=str,
            help='Gene name to look up (e.g. SYT1). Case-insensitive.',
        )
        parser.add_argument(
            '--output', '-o',
            type=str,
            default=None,
            help='Output file path. Defaults to /tmp/<gene>_report.docx.',
        )
        parser.add_argument(
            '--all-ready',
            action='store_true',
            help='Generate reports for all targets with status "report_ready".',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be generated without writing files.',
        )

    def handle(self, *args, **options):
        from pipeline.models import Target
        from pipeline.services.report_generator import generate_report

        target_pk = options['target_pk']
        gene_name = options.get('gene')
        output_path = options.get('output')
        all_ready = options.get('all_ready', False)
        dry_run = options.get('dry_run', False)

        targets = []

        if all_ready:
            targets = list(
                Target.objects
                .using(DB_ALIAS)
                .filter(status='report_ready')
                .order_by('protein_name')
            )
            if not targets:
                self.stdout.write(
                    self.style.WARNING('No targets with status "report_ready" found.')
                )
                return

        elif gene_name:
            try:
                target = (
                    Target.objects
                    .using(DB_ALIAS)
                    .get(gene_name__iexact=gene_name)
                )
                targets = [target]
            except Target.DoesNotExist:
                raise CommandError(
                    f'No target found with gene name "{gene_name}". '
                    f'Check spelling or use the target PK instead.'
                )
            except Target.MultipleObjectsReturned:
                matches = (
                    Target.objects
                    .using(DB_ALIAS)
                    .filter(gene_name__iexact=gene_name)
                    .values_list('pk', 'protein_name', 'gene_name', 'site__name')
                )
                self.stderr.write(
                    self.style.ERROR(
                        f'Multiple targets found for gene "{gene_name}":'
                    )
                )
                for pk, prot, gene, site in matches:
                    self.stderr.write(f'  PK={pk}  {prot} ({gene})  site={site}')
                raise CommandError(
                    'Use --target_pk to specify which one, or filter by site.'
                )

        elif target_pk:
            try:
                target = Target.objects.using(DB_ALIAS).get(pk=target_pk)
                targets = [target]
            except Target.DoesNotExist:
                raise CommandError(f'Target with PK={target_pk} does not exist.')

        else:
            raise CommandError(
                'Provide a target PK, --gene name, or --all-ready. '
                'Run with --help for usage.'
            )

        # Generate reports
        generated = 0
        errors = 0

        for target in targets:
            gene = target.gene_name or target.protein_name
            self.stdout.write(
                f'{"[DRY RUN] " if dry_run else ""}'
                f'Generating report for {target.protein_name} ({gene}) '
                f'PK={target.pk}...'
            )

            if dry_run:
                self._print_summary(target)
                generated += 1
                continue

            try:
                # For batch mode, auto-name; for single, use --output
                out = output_path if len(targets) == 1 else None
                path = generate_report(target.pk, output_path=out)
                self.stdout.write(
                    self.style.SUCCESS(f'  ✓ Report saved to: {path}')
                )
                generated += 1
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f'  ✗ Error generating report: {e}')
                )
                errors += 1
                if len(targets) == 1:
                    raise CommandError(str(e))

        # Summary
        self.stdout.write('')
        if generated:
            self.stdout.write(
                self.style.SUCCESS(
                    f'{"[DRY RUN] " if dry_run else ""}'
                    f'Generated {generated} report(s).'
                )
            )
        if errors:
            self.stderr.write(
                self.style.ERROR(f'{errors} report(s) failed.')
            )

    def _print_summary(self, target):
        """Print a dry-run summary of what would be generated."""
        from pipeline.models import (
            Antibody, CellLine, ExperimentSession
        )

        ab_count = (
            Antibody.objects
            .using(DB_ALIAS)
            .filter(target=target)
            .count()
        )
        cl_count = (
            CellLine.objects
            .using(DB_ALIAS)
            .filter(target=target)
            .count()
        )
        sessions = (
            ExperimentSession.objects
            .using(DB_ALIAS)
            .filter(target=target)
            .values_list('procedure_type', flat=True)
        )
        proc_counts = {}
        for p in sessions:
            proc_counts[p] = proc_counts.get(p, 0) + 1

        self.stdout.write(f'  Antibodies: {ab_count}')
        self.stdout.write(f'  Cell lines: {cl_count}')
        self.stdout.write(f'  Sessions:   {proc_counts or "none"}')

        if not ab_count:
            self.stdout.write(
                self.style.WARNING('  ⚠ No antibodies — Table 2 will be empty')
            )
        if not cl_count:
            self.stdout.write(
                self.style.WARNING('  ⚠ No cell lines — Table 1 will be empty')
            )
        if not proc_counts:
            self.stdout.write(
                self.style.WARNING(
                    '  ⚠ No sessions — figures and methods will be placeholders'
                )
            )
