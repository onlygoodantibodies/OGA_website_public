"""The not-supportive list at the command line, for one supplier or all of them.

The portal answers this for a manufacturer holding a key. This answers it for
whoever is standing in Render Shell — which is the only place in this project
that sees real live data, and so the only place the question can be asked of
the real rows before a key has been issued to anybody.

It writes the **same CSV the portal downloads**, from the same reader, because
a second way of producing this file is a second answer to "which of our
products failed", and the two would differ the first time either was touched.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand, CommandError

from core import not_supportive as NS


class Command(BaseCommand):
    help = ("List antibodies whose every tested application came back without "
            "support. Prints a summary; --csv writes the file the portal serves.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--supplier', default='',
            help=("Comma-separated supplier names, matched the way an API key's "
                  "supplier scope is (display_name first, then name). Omit for "
                  "every supplier."))
        parser.add_argument(
            '--csv', default='',
            help="Write the CSV here. '-' writes it to stdout.")
        parser.add_argument(
            '--include-limited', action='store_true',
            help=("Also list antibodies holding a Limited support result. Off "
                  "by default, matching the portal: the default list is the "
                  "hard edge — nothing on-target seen in any application."))
        parser.add_argument(
            '--gene', default='',
            help="Comma-separated gene symbols to narrow to.")

    def handle(self, *args, **options):
        from core.api_views import BASE_URL, _get_supplier_company_ids

        names = (options['supplier'] or '').strip()
        company_ids = None
        if names:
            company_ids = _get_supplier_company_ids(names)
            if not company_ids:
                # Named, not swapped for "everything": an empty scope silently
                # widened to the whole dataset is the one wrong answer here that
                # looks like a right one.
                raise CommandError(
                    f"No supplier on file matches {names!r}. Nothing was listed "
                    f"— run without --supplier to see every supplier, or check "
                    f"the spelling against pipeline_company.name.")

        genes = [g.strip() for g in (options['gene'] or '').split(',') if g.strip()]
        # The same absolute URLs the portal's download carries. A file whose
        # links work only from inside the app is one nobody can send anybody.
        rows = NS.rows(company_ids=company_ids, allowed_genes=genes or None,
                       base_url=BASE_URL,
                       include_limited=options['include_limited'])
        references = NS.reference_figures({r['gene'] for r in rows}, BASE_URL)
        NS.attach_references(rows, references)
        totals = NS.summary(rows)

        scope = names or 'every supplier'
        self.stdout.write(f"Scope: {scope}")
        self.stdout.write(
            f"{totals['antibodies']} antibodies · {totals['findings']} application "
            f"results · {totals['genes']} genes")
        self.stdout.write(
            f"  Not supportive: {totals['not_supportive']}   "
            f"Limited support: {totals['limited_support']}")
        for application, count in sorted(totals['per_application'].items()):
            self.stdout.write(f"  {application}: {count}")
        # A count with no list under it invents the noun.
        for row in rows:
            findings = ', '.join(
                f"{f['application']} {f['finding'].lower()}" for f in row['findings'])
            self.stdout.write(
                f"  {row['gene']:<12} {row['catalogue_number']:<20} "
                f"{row['applications_not_supportive']}/{row['applications_tested']} "
                f"— {findings}")

        target = options['csv']
        if not target:
            return
        body = NS.to_csv(rows)
        if target == '-':
            sys.stdout.buffer.write(body)
            return
        with open(target, 'wb') as handle:
            handle.write(body)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(body)} bytes to {target} "
            f"({totals['findings']} rows, one per application result)."))
