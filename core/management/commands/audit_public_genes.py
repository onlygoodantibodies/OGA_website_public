"""Which genes does a public surface claim, and which of those have nothing?

Run this after importing a target list, and any time the browser-extension index
is rebuilt. A `Target` row is an intention to work on a gene; a *public* gene is
one with at least one antibody carrying a published figure. Anything in the gap
must not reach the site, the extension or the MCP dataset — that gap is how
`/antibodies/GAPDH/` came to serve a page about 0 antibodies.

    python manage.py audit_public_genes
    python manage.py audit_public_genes --list          # print every gene
    python manage.py audit_public_genes --strict        # exit 1 if anything leaks
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from pipeline.public import public_gene_names, public_targets, unpublished_targets


class Command(BaseCommand):
    help = "Report targets with no published data, and check no public surface claims them."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true",
                            help="Print every unpublished gene, not just a sample")
        parser.add_argument("--strict", action="store_true",
                            help="Exit 1 if a public surface claims an unpublished gene")

    def handle(self, *args, **opts):
        public = set(public_gene_names())
        unpublished = sorted(
            unpublished_targets().values_list("gene_name", flat=True))

        self.stdout.write(f"Targets in the pipeline : {public_targets().count() + len(unpublished)}")
        self.stdout.write(f"  public (has a figure) : {len(public)}")
        self.stdout.write(f"  no published data yet : {len(unpublished)}")

        if unpublished:
            shown = unpublished if opts["list"] else unpublished[:25]
            self.stdout.write("\nNo published data (correct to omit from public surfaces):")
            for gene in shown:
                self.stdout.write(f"  {gene}")
            if len(shown) < len(unpublished):
                self.stdout.write(
                    f"  … and {len(unpublished) - len(shown)} more (--list to see all)")

        # The extension index is the surface that got this wrong, so check it
        # against reality rather than trusting that the code is still right.
        from core.extension_index import build_index

        index = build_index()
        claimed = set(index.get("genes") or [])
        leaked = sorted(claimed - public)
        missing = sorted(public - claimed)

        self.stdout.write(f"\nBrowser-extension index claims {len(claimed)} genes.")
        if leaked:
            self.stdout.write(self.style.ERROR(
                f"  {len(leaked)} claimed with NO public page — each is an amber "
                f"badge linking to a 404:"))
            for gene in leaked[:40]:
                self.stdout.write(self.style.ERROR(f"    {gene}"))
            if len(leaked) > 40:
                self.stdout.write(self.style.ERROR(f"    … and {len(leaked) - 40} more"))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"  {len(missing)} public genes missing from the index: "
                f"{', '.join(missing[:20])}"))
        if not leaked and not missing:
            self.stdout.write(self.style.SUCCESS(
                "  Matches the public set exactly."))

        # "Live" means the publication images are actually on R2. The rule the
        # site has always used tests that a PublicationImage *row* exists, which
        # is the same thing unless a row was written with an empty file. Report
        # any, because such a gene would count as live while rendering nothing.
        from pipeline.models import PublicationImage

        empty = (PublicationImage.objects
                 .filter(image="")
                 .select_related("antibody__target"))
        if empty.exists():
            genes = sorted({p.antibody.target.gene_name for p in empty
                            if p.antibody.target.gene_name})
            self.stdout.write(self.style.WARNING(
                f"\n{empty.count()} publication-image row(s) have no file, "
                f"affecting: {', '.join(genes)}"))
            self.stdout.write(self.style.WARNING(
                "  These count as live but have nothing to show. Check R2."))
        else:
            self.stdout.write(
                "\nEvery publication-image row has a file — live means live.")

        if opts["strict"] and leaked:
            raise SystemExit(1)
