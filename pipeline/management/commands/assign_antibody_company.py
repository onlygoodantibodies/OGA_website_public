"""
Give a supplier to antibodies that have none. Dry-run by default.

    python manage.py assign_antibody_company --company 152 4441=AC-PFN1-4 4428=Z-UBQLN2-7
    python manage.py assign_antibody_company --company 151 4440=sc-138763 --apply

Each antibody is named by pk **and** its catalogue number, and a pair that does
not match live is refused by name: a mistyped pk would otherwise hand a supplier
to some other antibody, and nothing on its gene page would say so.

Fill-only-blank. An antibody that already has a supplier is refused, never
overwritten. Changing a supplier is an identity edit, and that is the identity
dialog's job. A supplier is named by pk because this is a correction to
specific records, where resolving a typed name is a guess.

The supplier is part of an antibody's identity (catalogue, company, target, lot,
site), so a row that would collide with one already on file is refused too.
That is a merge, and `merge_duplicate_antibodies` does merges.

All-or-nothing: if any pair is refused, nothing is written.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Antibody, Company
from pipeline.services.cropper.db import company_label

DB = "pipeline_db"


class Command(BaseCommand):
    help = "Give a supplier to antibodies that have none. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("pairs", nargs="+", metavar="PK=CATALOGUE",
                            help="antibody pk and its catalogue number, e.g. 4441=AC-PFN1-4")
        parser.add_argument("--company", type=int, required=True,
                            help="pk of the supplier to assign")
        parser.add_argument("--apply", action="store_true",
                            help="Write. Without this it is a read-only dry-run.")

    def handle(self, *args, **opts):
        if Antibody.objects.db != DB:
            raise CommandError(f"Antibody routes to '{Antibody.objects.db}', expected '{DB}'.")
        company = Company.objects.using(DB).filter(pk=opts["company"]).first()
        if company is None:
            raise CommandError(f"No supplier with id {opts['company']}.")

        wanted = []
        for pair in opts["pairs"]:
            pk, sep, cat = pair.partition("=")
            if not sep or not pk.strip().isdigit() or not cat.strip():
                raise CommandError(f"“{pair}” is not PK=CATALOGUE — e.g. 4441=AC-PFN1-4.")
            wanted.append((int(pk), cat.strip()))

        banner = "APPLY (writing)" if opts["apply"] else "DRY-RUN (no changes)"
        self.stdout.write(f"assign_antibody_company — {banner}")
        self.stdout.write(f"supplier: id{company.pk} {company_label(company)!r}"
                          + (f" (public name {company.display_name!r})"
                             if company.display_name else ""))

        todo, refused = [], []
        for pk, cat in wanted:
            ab = (Antibody.objects.using(DB).select_related("target", "company")
                  .filter(pk=pk).first())
            if ab is None:
                refused.append(f"id{pk}: no antibody with that id")
                continue
            gene = ab.target.gene_name if ab.target else "no gene"
            what = f"id{pk} {ab.catalogue_number} ({gene})"
            if ab.catalogue_number.strip() != cat:
                refused.append(f"id{pk}: catalogue on file is {ab.catalogue_number!r}, "
                               f"not {cat!r} — check the id")
            elif ab.company_id == company.pk:
                self.stdout.write(f"  [done] {what}: already {company_label(company)!r}")
            elif ab.company_id is not None:
                refused.append(f"{what}: already has supplier "
                               f"{company_label(ab.company)!r} (id{ab.company_id}) — "
                               f"change it in the identity dialog, not here")
            else:
                clash = (Antibody.objects.using(DB).exclude(pk=pk)
                         .filter(company_id=company.pk, target_id=ab.target_id,
                                 catalogue_number=ab.catalogue_number,
                                 lot_number=ab.lot_number, site_id=ab.site_id)
                         .first())
                if clash:
                    refused.append(f"{what}: id{clash.pk} is already this catalogue, "
                                   f"gene, lot and site under {company_label(company)!r} "
                                   f"— that is a merge")
                else:
                    todo.append((ab, what))

        for ab, what in todo:
            self.stdout.write(f"  {what}: no supplier -> {company_label(company)!r}")
        for line in refused:
            self.stdout.write(f"  REFUSED {line}")

        if refused:
            raise CommandError(f"{len(refused)} refused, so nothing was written.")
        if not opts["apply"]:
            self.stdout.write(f"DRY-RUN: {len(todo)} antibody(ies) would be given a "
                              f"supplier. Back up, then rerun with --apply.")
            return
        with transaction.atomic(using=DB):
            for ab, _ in todo:
                ab.company_id = company.pk
                ab.save(using=DB, update_fields=["company", "updated_at"])
        self.stdout.write(f"APPLIED: {len(todo)} antibody(ies) given a supplier.")
