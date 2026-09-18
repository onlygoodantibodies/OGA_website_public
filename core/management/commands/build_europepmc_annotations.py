"""``manage.py`` wrapper around ``core/europepmc_annotations.py``.

The work is deliberately NOT here — same shape as ``resolve_citation_dois``. It
needs no database, no settings and no Django, so it lives in a module a bare
``python3`` can run. This exists so the job is discoverable beside
``build_citation_index`` and ``build_extension_index``, the two builds whose
outputs it consumes.
"""
from django.core.management.base import BaseCommand

from core import europepmc_annotations


class Command(BaseCommand):
    help = ("Build Europe PMC annotation files from the citation snapshot and the "
            "extension index. Runnable without Django: "
            "python3 core/europepmc_annotations.py")

    def add_arguments(self, parser):
        parser.add_argument("--artefact", default=None)
        parser.add_argument("--index", default=None)
        parser.add_argument("--provider", default=None)
        parser.add_argument("--email", default=None)
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--cache", default=None)
        parser.add_argument("--out", default=None)
        parser.add_argument("--delay", type=float,
                            default=europepmc_annotations.DELAY_SECONDS)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--submit", action="store_true")
        parser.add_argument("--submit-file", default=None)
        parser.add_argument("--results", nargs="?", const="", default=None)

    def handle(self, *args, **options):
        argv = []
        for flag in ("artefact", "index", "provider", "email", "cache", "out"):
            if options.get(flag):
                argv += [f"--{flag}", str(options[flag])]
        for flag in ("check", "apply", "submit"):
            if options.get(flag):
                argv.append(f"--{flag}")
        if options.get("submit_file"):
            argv += ["--submit-file", str(options["submit_file"])]
        if options.get("results") is not None:
            argv += ["--results"] + ([options["results"]] if options["results"] else [])
        if options.get("limit"):
            argv += ["--limit", str(options["limit"])]
        argv += ["--delay", str(options["delay"])]
        code = europepmc_annotations.main(argv)
        if code:
            raise SystemExit(code)
