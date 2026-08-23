"""
Management Command: create_api_consumer
========================================

File location: core/management/commands/create_api_consumer.py

Make sure the directories exist:
    mkdir -p core/management/commands
    touch core/management/__init__.py
    touch core/management/commands/__init__.py

Usage:
    python manage.py create_api_consumer "Abcam" manufacturer --supplier "Abcam" --tier full --genes "GBA1 (GCase),GPNMB,RAB3A"
    python manage.py create_api_consumer "Antibody Registry" rrid
    python manage.py create_api_consumer "Bio-Techne" manufacturer --supplier "Bio-Techne,Bio-Techne (Novus Biologicals),Bio-Techne (R&D Systems)" --tier full --genes "GBA1 (GCase),GPNMB,CD44"
"""

from django.core.management.base import BaseCommand, CommandError
from core.models import APIConsumer


class Command(BaseCommand):
    help = 'Create a new API consumer and display their API key'

    def add_arguments(self, parser):
        parser.add_argument('name', type=str, help='Organisation name')
        parser.add_argument(
            'consumer_type',
            type=str,
            choices=['manufacturer', 'rrid'],
            help='Consumer type'
        )
        parser.add_argument(
            '--supplier',
            type=str,
            default=None,
            help='Supplier filter — comma-separated, must match Description.supplier exactly'
        )
        parser.add_argument(
            '--tier',
            type=str,
            default='free',
            choices=['free', 'data', 'intel', 'full'],
            help='Portal tier (default: free)'
        )
        parser.add_argument(
            '--genes',
            type=str,
            default=None,
            help='Gene filter — comma-separated gene names for demo access '
                 '(e.g. "GBA1 (GCase),GPNMB,CD44"). Leave blank for unrestricted.'
        )

    def handle(self, *args, **options):
        name = options['name']
        consumer_type = options['consumer_type']
        supplier_filter = options['supplier']
        tier = options['tier']
        gene_filter = options['genes']

        if consumer_type == 'manufacturer' and not supplier_filter:
            raise CommandError(
                'Manufacturer consumers must have a --supplier filter. '
                'This must exactly match the supplier field in Description '
                '(e.g. "Abcam", "Cell Signaling Technology").'
            )

        consumer = APIConsumer.objects.create(
            name=name,
            consumer_type=consumer_type,
            supplier_filter=supplier_filter,
            tier=tier,
            gene_filter=gene_filter,
        )

        gene_list = consumer.get_gene_filter_list()
        gene_display = ', '.join(gene_list) if gene_list else '(all genes)'

        self.stdout.write(self.style.SUCCESS(
            f'\nCreated API consumer:\n'
            f'  Name:     {consumer.name}\n'
            f'  Type:     {consumer.get_consumer_type_display()}\n'
            f'  Tier:     {consumer.tier}\n'
            f'  Supplier: {consumer.supplier_filter or "(all)"}\n'
            f'  Genes:    {gene_display}\n'
            f'  API Key:  {consumer.api_key}\n'
            f'\nShare this API key with the consumer.\n'
            f'They use it as: X-API-Key: {consumer.api_key}\n'
        ))