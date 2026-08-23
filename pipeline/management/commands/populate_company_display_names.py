"""
Populate Company.display_name from core OGA canonical supplier names.

For each pipeline Company, finds pipeline antibodies from that company,
matches them to core antibodies by catalogue_number, and sets display_name
to the most common core Description.supplier value.

Safe to re-run: only overwrites display_name, never touches Company.name.

Usage:
    python manage.py populate_company_display_names          # dry run
    python manage.py populate_company_display_names --commit  # apply
"""

from collections import Counter

from django.core.management.base import BaseCommand

from core.models import Antibody as CoreAntibody, Description
from pipeline.models import Antibody as PipelineAntibody, Company as PipelineCompany


class Command(BaseCommand):
    help = 'Populate pipeline Company.display_name from core Description.supplier'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Actually save changes (default is dry run)',
        )

    def handle(self, *args, **options):
        commit = options['commit']
        updated = 0
        skipped = 0
        no_match = 0

        companies = PipelineCompany.objects.all().order_by('name')
        self.stdout.write(f"Processing {companies.count()} companies...")

        for company in companies:
            # Get all catalogue numbers for antibodies from this company
            cat_numbers = list(
                PipelineAntibody.objects.filter(company=company)
                .exclude(catalogue_number='')
                .values_list('catalogue_number', flat=True)
            )

            if not cat_numbers:
                self.stdout.write(f"  {company.name}: no antibodies, skipping")
                skipped += 1
                continue

            # Find matching core antibodies and collect their supplier names
            supplier_counts = Counter()
            for cat_num in cat_numbers:
                try:
                    core_ab = CoreAntibody.objects.get(name=cat_num)
                    desc = getattr(core_ab, 'description', None)
                    if desc and desc.supplier:
                        supplier_counts[desc.supplier] += 1
                except CoreAntibody.DoesNotExist:
                    pass
                except CoreAntibody.MultipleObjectsReturned:
                    core_ab = CoreAntibody.objects.filter(name=cat_num).first()
                    desc = getattr(core_ab, 'description', None)
                    if desc and desc.supplier:
                        supplier_counts[desc.supplier] += 1

            if not supplier_counts:
                self.stdout.write(f"  {company.name}: no core matches found")
                no_match += 1
                continue

            # Use the most common supplier name
            canonical_name, count = supplier_counts.most_common(1)[0]

            if canonical_name == company.name:
                self.stdout.write(f"  {company.name}: already matches core ({count} matches)")
                skipped += 1
                continue

            self.stdout.write(
                f"  {company.name} → display_name=\"{canonical_name}\" "
                f"({count}/{sum(supplier_counts.values())} matches)"
            )
            if len(supplier_counts) > 1:
                self.stdout.write(f"    Multiple core names: {dict(supplier_counts)}")

            if commit:
                company.display_name = canonical_name
                company.save(update_fields=['display_name'])
            updated += 1

        self.stdout.write('')
        self.stdout.write(f"Results: {updated} updated, {skipped} skipped, {no_match} no match")
        if not commit:
            self.stdout.write(self.style.WARNING('DRY RUN — re-run with --commit to save'))
        else:
            self.stdout.write(self.style.SUCCESS(f'{updated} companies updated'))
