"""Take a gene's published figures off the public site and back into the review queue.

    python manage.py withdraw_figures --gene DDX1 GLE1
    python manage.py withdraw_figures --gene DDX1 GLE1 --apply

A ``PublicationImage`` **is** publication — ``pipeline/public.py`` derives the
whole public site from "a named gene with at least one antibody carrying a
published figure" — and until now nothing could remove one. ``review.discard``
refuses a released row *because* the public figure would stay up, and pointed at
a control on the antibodies board that does not exist; the only thing that
actually deleted a ``PublicationImage`` was deleting the entire antibody, which
takes every reading with it.

This is the missing half of ``review.release``, and it is a move rather than a
delete: the figure goes back to ``/pipeline/review/`` as ``pending``, where
somebody can look at it again and release it if the question that prompted the
withdrawal gets settled. ``pipeline/services/review.py::withdraw`` holds the
rules; this command is the door.

**Why a gene gets withdrawn, and why the count is not the manifest.** The case
this was written for is a gene whose knockout was never confirmed — where the
figures are real but nobody can say whether a band in the KO lane is the
antibody or the line. Withdrawing the last figure on a gene takes its public
page down entirely, so the summary names the genes that stop being public rather
than only counting rows: that is the part of the change a reader of the website
would notice and a row count does not show.

Dry-run by default, like every writing command here. Nothing moves without
``--apply``, and then it is one transaction.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import PublicationImage, Target
from pipeline.services import review

DB = "pipeline_db"


class Command(BaseCommand):
    help = ("Withdraw published figures from the public site, back into the "
            "review queue. Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--gene", nargs="+", metavar="GENE",
                            help="Gene symbol(s) whose figures to withdraw.")
        parser.add_argument("--ids", nargs="+", type=int, metavar="PK",
                            help="PublicationImage ids, for a partial withdrawal.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually do it. Without this, nothing is written.")

    def handle(self, *args, **options):
        genes, ids = options.get("gene"), options.get("ids")
        if not genes and not ids:
            raise CommandError(
                "Nothing named. Pass --gene SYMBOL [SYMBOL ...] or --ids PK [PK ...].")

        qs = (PublicationImage.objects.using(DB)
              .select_related("antibody", "antibody__target", "antibody__company"))
        if genes:
            resolved = []
            for symbol in genes:
                target = (Target.objects.using(DB)
                          .filter(gene_name__iexact=symbol).first())
                if target is None:
                    raise CommandError(
                        f"No gene called {symbol!r}. Check the spelling — the "
                        f"symbol must be the one on the target record, e.g. DDX1.")
                resolved.append(target.pk)
            qs = qs.filter(antibody__target_id__in=resolved)
        if ids:
            qs = qs.filter(pk__in=ids)

        images = list(qs.order_by("antibody__target__gene_name",
                                  "antibody__catalogue_number", "application_type"))
        if not images:
            raise CommandError(
                "No published figures match. Nothing to withdraw — check the "
                "gene is actually on the public site.")

        self._preview(images)

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing was changed. Re-run with --apply to do it."))
            return

        with transaction.atomic(using=DB):
            result = review.withdraw(images, actor="withdraw_figures")

        self.stdout.write(self.style.SUCCESS(
            f"\nWithdrew {len(result.withdrawn)} figure(s)."))
        self.stdout.write(f"  re-staged into the review queue   {result.restaged}")
        if result.already_queued:
            self.stdout.write(
                f"  left alone (a newer crop is queued) {result.already_queued}")
        if result.recommendations_cleared:
            self.stdout.write(
                f"  recommendations cleared           {result.recommendations_cleared}")
        if result.genes_leaving_public:
            self.stdout.write(self.style.WARNING(
                "  no longer on the public site:     "
                + ", ".join(result.genes_leaving_public)))
        self.stdout.write(
            "\nThe figures are at /pipeline/review/ and can be released again.")

    def _preview(self, images):
        """What the press would do, in antibodies and genes rather than rows."""
        by_gene = {}
        for img in images:
            gene = (img.antibody.target.gene_name
                    if img.antibody.target_id else "(no gene)")
            by_gene.setdefault(gene, []).append(img)

        self.stdout.write(
            f"{len(images)} published figure(s) on {len(by_gene)} gene(s):\n")
        for gene in sorted(by_gene):
            rows = by_gene[gene]
            apps = sorted({r.application_type for r in rows})
            abs_ = sorted({r.antibody.catalogue_number for r in rows})
            self.stdout.write(
                f"  {gene:<10} {len(rows):>3} figure(s)  "
                f"{len(abs_)} antibod{'y' if len(abs_) == 1 else 'ies'}  "
                f"[{', '.join(apps)}]")

        # A gene stops being public when its LAST figure goes, which is a
        # different statement from "some of its figures go" and is the one a
        # reader of the website would notice.
        target_ids = {i.antibody.target_id for i in images
                      if i.antibody.target_id is not None}
        chosen = {i.pk for i in images}
        leaving = []
        for target_id in target_ids:
            remaining = (PublicationImage.objects.using(DB)
                         .filter(antibody__target_id=target_id)
                         .exclude(pk__in=chosen).exists())
            if not remaining:
                leaving.append(target_id)
        if leaving:
            names = sorted(
                Target.objects.using(DB).filter(pk__in=leaving)
                .values_list("gene_name", flat=True))
            self.stdout.write(self.style.WARNING(
                f"\n  These gene pages would come off the public site entirely: "
                f"{', '.join(n for n in names if n)}"))

        queued = sum(
            1 for i in images
            if review.PendingPublicationImage.objects.using(DB)
            .filter(antibody_id=i.antibody_id,
                    application_type=i.application_type,
                    status=review.PENDING).exists())
        if queued:
            self.stdout.write(
                f"  {queued} already have a newer crop queued; those crops are "
                f"left as they are.")
