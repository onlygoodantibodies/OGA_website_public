"""``manage.py`` wrapper around ``core/citations_doi.py``.

The work is deliberately NOT here. It needs no database, no settings and no
Django, so it lives in a module that can be run with a bare ``python3`` — see
that file for why. This exists only so the job is discoverable in the same place
as ``build_citation_index``, which does need Django.
"""
from django.core.management.base import BaseCommand

from core import citations_doi


class Command(BaseCommand):
    help = ("Resolve PubMed/preprint ids in the citation snapshot to DOIs via "
            "Europe PMC. Runnable without Django: python3 core/citations_doi.py")

    def add_arguments(self, parser):
        parser.add_argument("--artefact", default=None)
        parser.add_argument("--email", default=None)
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--cache", default=None)
        parser.add_argument("--delay", type=float, default=citations_doi.DELAY_SECONDS)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        argv = []
        for flag in ("artefact", "email", "cache"):
            if options.get(flag):
                argv += [f"--{flag}", str(options[flag])]
        for flag in ("check", "apply"):
            if options.get(flag):
                argv.append(f"--{flag}")
        if options.get("limit"):
            argv += ["--limit", str(options["limit"])]
        argv += ["--delay", str(options["delay"])]
        citations_doi.main(argv)
