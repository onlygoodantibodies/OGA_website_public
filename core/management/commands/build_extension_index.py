"""Write the browser extension's bundled data snapshot.

The served endpoint (/extension/index.json) builds this on demand and caches it,
so this command is only needed to refresh the copy that ships *inside* the
extension zip — the one a fresh install uses before its first refresh.

    python manage.py build_extension_index
    python manage.py build_extension_index --out /tmp/index.json
"""

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from core.extension_index import build_index, load_aliases


class Command(BaseCommand):
    help = "Build the browser extension's antibody lookup snapshot"

    def add_arguments(self, parser):
        parser.add_argument(
            '--out',
            default=None,
            help='Output path (default: browser-extension/data/index.json)',
        )
        parser.add_argument(
            '--indent',
            action='store_true',
            help='Pretty-print, for inspecting the output by eye',
        )

    def handle(self, *args, **options):
        out = options['out'] or os.path.join(
            settings.BASE_DIR, 'browser-extension', 'data', 'index.json'
        )

        index = build_index(aliases=load_aliases())
        counts = index['counts']

        if counts['antibodies'] == 0:
            # Almost always means this is running against the local SQLite
            # fallback rather than production PostgreSQL.
            self.stderr.write(self.style.WARNING(
                'No antibodies found. Is PIPELINE_DATABASE_URL pointing at the '
                'live pipeline database? Local dev falls back to an empty SQLite.'
            ))

        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as fh:
            if options['indent']:
                json.dump(index, fh, indent=1, sort_keys=True)
            else:
                json.dump(index, fh, separators=(',', ':'), sort_keys=True)

        size_kb = os.path.getsize(out) / 1024
        self.stdout.write(self.style.SUCCESS(
            f"wrote {out} ({size_kb:.1f} kB) — "
            f"{counts['antibodies']} antibodies across "
            f"{counts['genes_with_antibody_records']} genes, "
            f"{counts['genes']} genes total"
        ))
        ambiguous = index['ambiguous_catalogue']
        if ambiguous:
            # These antibodies can still be found by RRID, but not by the
            # catalogue number papers overwhelmingly cite — so a silent drop
            # looks exactly like the antibody being absent from the dataset.
            self.stderr.write(self.style.WARNING(
                f"\n{len(ambiguous)} catalogue number(s) map to genuinely different "
                f"products and were dropped. Papers citing these by catalogue number "
                f"will not be matched:"
            ))
            for key in ambiguous[:20]:
                self.stderr.write(f"    {key}")
            if len(ambiguous) > 20:
                self.stderr.write(f"    ... and {len(ambiguous) - 20} more")
            self.stderr.write(
                "  Duplicate rows for the SAME product are now resolved automatically; "
                "these are conflicts. See DUPLICATE_ANTIBODY_FINDINGS.md."
            )
