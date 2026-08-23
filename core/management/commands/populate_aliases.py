"""
Management command: populate_aliases
Usage: python manage.py populate_aliases [--overwrite]

Fetches alias_symbol and prev_symbol from the HGNC REST API for each Gene
and stores them as a comma-separated string in gene.aliases.

By default, skips genes that already have aliases set.
Use --overwrite to refresh all genes.
"""

import re
import time
import requests
from django.core.management.base import BaseCommand
from core.models import Gene

HGNC_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"
HEADERS = {"Accept": "application/json"}


def clean_symbol(name):
    """
    Strip parenthetical suffixes and whitespace from gene names so they
    can be looked up against HGNC.
    e.g. 'GBA1 (GCase)' -> 'GBA1', 'SHIP1(INPP5D)' -> 'SHIP1'
    """
    return re.sub(r'\s*\(.*?\)', '', name).strip()


def fetch_aliases(symbol):
    """
    Query HGNC for alias_symbol and prev_symbol.
    Returns a sorted list of unique aliases, or an empty list on failure.
    """
    try:
        url = HGNC_URL.format(symbol=symbol)
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()

        docs = data.get("response", {}).get("docs", [])
        if not docs:
            return []

        doc = docs[0]
        aliases = set()
        for field in ("alias_symbol", "prev_symbol"):
            for val in doc.get(field, []):
                val = val.strip()
                if val and val.upper() != symbol.upper():
                    aliases.add(val)

        return sorted(aliases)

    except Exception as e:
        return []


class Command(BaseCommand):
    help = "Populate gene aliases from the HGNC REST API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing aliases (default: skip genes that already have aliases)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be set without saving to the database",
        )

    def handle(self, *args, **options):
        overwrite = options["overwrite"]
        dry_run = options["dry_run"]

        genes = Gene.objects.all().order_by("name")
        updated = 0
        skipped = 0
        not_found = 0

        for gene in genes:
            if gene.aliases and not overwrite:
                self.stdout.write(f"  SKIP  {gene.name} (already has aliases)")
                skipped += 1
                continue

            symbol = clean_symbol(gene.name)
            aliases = fetch_aliases(symbol)

            if aliases:
                alias_str = ", ".join(aliases)
                if dry_run:
                    self.stdout.write(f"  DRY   {gene.name} -> {alias_str}")
                else:
                    gene.aliases = alias_str
                    gene.save(update_fields=["aliases"])
                    self.stdout.write(
                        self.style.SUCCESS(f"  OK    {gene.name} -> {alias_str}")
                    )
                updated += 1
            else:
                self.stdout.write(f"  NONE  {gene.name} (no aliases found in HGNC)")
                not_found += 1

            # Be polite to the HGNC API
            time.sleep(0.2)

        self.stdout.write("\n--- Summary ---")
        self.stdout.write(f"  Updated : {updated}")
        self.stdout.write(f"  Skipped : {skipped}")
        self.stdout.write(f"  No data : {not_found}")
        if dry_run:
            self.stdout.write(self.style.WARNING("  DRY RUN — no changes saved."))
