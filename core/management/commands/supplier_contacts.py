"""Put the manufacturers' contacts on file, one key per organisation. Dry-run by default.

    python manage.py supplier_contacts            # say what would change
    python manage.py supplier_contacts --apply    # do it

Reads ``core/data/supplier_contacts.csv``. For each organisation it finds
**the** manufacturer key whose supplier scope covers the same suppliers, and
makes one only if there is none (owner, 29 Sep 2026: each organisation has one
key, shared). Two keys covering one supplier is refused by name: which one
the contacts belong to is a question for a person.

It fills ``email_domains`` only where blank and says so where the file
disagrees, since a domain somebody typed in admin is newer than this file. It
adds contacts and never removes one; untick *is active* in admin to stop
emailing somebody.

A supplier scope that matches no company on file is refused by name. A key
scoped to nothing answers every supplier endpoint with an error, which is
worse than no key.
"""
from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.api_views import _get_supplier_company_ids
from core.models import APIConsumer, SupplierContact

DEFAULT_FILE = Path(__file__).resolve().parents[2] / 'data' / 'supplier_contacts.csv'


def read(path):
    with open(path, newline='', encoding='utf-8') as fh:
        lines = [line for line in fh if line.strip() and not line.lstrip().startswith('#')]
    orgs = {}
    for row in csv.DictReader(lines):
        org = row['organisation'].strip()
        entry = orgs.setdefault(org, {
            'supplier_filter': row['supplier_filter'].strip(),
            'email_domains': ','.join(d.strip().lower()
                                      for d in row['email_domains'].split(';') if d.strip()),
            'contacts': [],
        })
        role = (row.get('role') or 'to').strip().lower()
        if role not in (SupplierContact.TO, SupplierContact.CC):
            raise CommandError(f"{org}: role {role!r} is neither 'to' nor 'cc'.")
        entry['contacts'].append({'name': row.get('name', '').strip(),
                                  'email': row['email'].strip(), 'role': role})
    return orgs


class Command(BaseCommand):
    help = "Load manufacturer contacts and make sure each has one scoped key (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(DEFAULT_FILE))
        parser.add_argument('--apply', action='store_true',
                            help='Write. Without it nothing is changed.')

    def handle(self, *args, **opts):
        orgs = read(opts['file'])
        manufacturers = list(APIConsumer.objects.filter(consumer_type='manufacturer'))
        scope_of = {c.pk: set(_get_supplier_company_ids(c.supplier_filter or '') or [])
                    for c in manufacturers}
        verb = '' if opts['apply'] else '[dry run] '
        refused = 0

        for org, entry in sorted(orgs.items()):
            wanted = set(_get_supplier_company_ids(entry['supplier_filter']) or [])
            if not wanted:
                self.stdout.write(self.style.ERROR(
                    f"{org}: no supplier on file matches {entry['supplier_filter']!r}. "
                    "No key made. Check the spelling against the suppliers list."))
                refused += 1
                continue
            # OGA's own test keys are not the organisation's key, however they
            # are scoped (#4 "Test" covered Thermo Fisher on live, 29 Sep 2026).
            # A switched-on key beats switched-off ones, which are history.
            holders = [c for c in manufacturers
                       if scope_of[c.pk] & wanted and not c.is_internal]
            if len([c for c in holders if c.is_active]) >= 1:
                holders = [c for c in holders if c.is_active]
            if len(holders) > 1:
                self.stdout.write(self.style.ERROR(
                    f"{org}: {len(holders)} keys already cover this supplier "
                    f"({', '.join(f'#{c.pk} {c.name}' for c in holders)}). Nothing "
                    "done: one organisation has one key. In admin, switch off the "
                    "ones that are not theirs, or tick 'Internal / test key' on "
                    "OGA's own, and run this again."))
                refused += 1
                continue

            with transaction.atomic(using='academy_db'):
                if holders:
                    consumer = holders[0]
                    note = f"uses its existing key (#{consumer.pk} {consumer.name})"
                    if not consumer.is_active:
                        note += ' — that key is switched OFF, so the portal refuses it'
                    if not consumer.email_domains:
                        consumer.email_domains = entry['email_domains']
                        note += f"; email domains set to {entry['email_domains']}"
                        if opts['apply']:
                            consumer.save(update_fields=['email_domains'])
                    elif set(consumer.get_email_domains()) != set(entry['email_domains'].split(',')):
                        note += (f"; email domains left as {consumer.email_domains!r} "
                                 f"(file says {entry['email_domains']!r})")
                else:
                    consumer = APIConsumer(
                        name=org, consumer_type='manufacturer',
                        supplier_filter=entry['supplier_filter'],
                        email_domains=entry['email_domains'])
                    consumer.full_clean()
                    note = f"new key, scoped to {entry['supplier_filter']!r}"
                    if opts['apply']:
                        consumer.save()
                        manufacturers.append(consumer)
                        scope_of[consumer.pk] = wanted

                added = []
                for c in entry['contacts']:
                    exists = (consumer.pk and SupplierContact.objects.filter(
                        consumer=consumer, email__iexact=c['email']).exists())
                    if exists:
                        continue
                    added.append(f"{c['email']} ({c['role']})")
                    if opts['apply']:
                        SupplierContact.objects.create(consumer=consumer, **c)
            self.stdout.write(f"{verb}{org}: {note}; "
                              + (f"adds {', '.join(added)}" if added else 'contacts already on file'))

        if refused:
            self.stdout.write(self.style.WARNING(f"{refused} organisation(s) refused, named above."))
        if not opts['apply']:
            self.stdout.write('Nothing written. Run again with --apply.')
