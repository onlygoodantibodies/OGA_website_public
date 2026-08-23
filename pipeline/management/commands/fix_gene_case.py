"""Correct human gene symbols typed in the wrong case in live ``pipeline_db``.

The twentieth field test read all 584 targets and found eight: ``DnaJC18``,
``Kif5a``, ``PTK2b``, ``Rab3C``, ``Rab40AL``, ``Rab44``, ``Rab45`` and
``Rab46``. Human symbols are uppercase; those are how the *mouse* symbols are
written, so anybody matching our export against a supplier list or a UniProt
query reads them as a different organism's genes.

``pipeline.services.gene_symbol`` decides the spelling, so this command and the
write paths cannot disagree about it — and it is why the three ``orf`` symbols
on file (``C9orf72``, ``C9orf16``, ``C14orf119``) are **not** candidates here:
lowercase ``orf`` is the HGNC convention and a bare ``.upper()`` would break
them, which is the mistake the obvious fix to this makes.

WHAT IT WILL NOT DO
  * **It changes case and nothing else.** Not a renamer: ``Rab45`` is an older
    name for ``RASEF``, and which symbol a target *should* carry is a curation
    question with UniProt behind it (``enrich_targets_from_uniprot``), not a
    string transform. Every planned change is asserted to differ from what is
    stored only in case before it is written, so the whole plan is reviewable
    by eye.
  * **It refuses a collision by name.** ``Target.gene_name`` is UNIQUE, so if
    ``RAB44`` already exists as its own row then correcting ``Rab44`` would be
    a merge, not a rename — two records for one gene, with antibodies and
    sessions on both. That is ``merge_duplicate_antibodies``-shaped work and a
    separate decision; this reports the pair and leaves both alone. The check
    runs *before* the write, because on PostgreSQL a failed statement poisons
    the transaction and the handler that would explain the refusal cannot then
    run the query it needs.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * Guarded to ``pipeline_db``.
  * Take a Render export before ``--apply`` (see the production-data skill).

    python manage.py fix_gene_case
    python manage.py fix_gene_case --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from pipeline.services import gene_symbol

DB = "pipeline_db"


class Command(BaseCommand):
    help = "Correct gene symbols typed in the wrong case. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes (default is a dry run)")

    def handle(self, *args, **opts):
        from pipeline.models import Target

        planned, refused = [], []

        rows = (Target.objects.using(DB)
                .exclude(gene_name__isnull=True).exclude(gene_name="")
                .order_by("gene_name")
                .only("pk", "gene_name"))

        for target in rows:
            stored = (target.gene_name or "").strip()
            corrected = gene_symbol.canonical(stored)
            if corrected == stored:
                continue

            # Case only, always. Belt and braces over the module's own contract:
            # a plan a reader cannot check by eye is not one to run against live
            # data.
            if corrected.upper() != stored.upper():
                refused.append(
                    f"{stored}: would become {corrected}, which is a different "
                    "symbol and not a case fix — left alone")
                continue

            clash = (Target.objects.using(DB)
                     .filter(gene_name__iexact=corrected)
                     .exclude(pk=target.pk)
                     .first())
            if clash is not None:
                refused.append(
                    f"{stored} (id {target.pk}): {clash.gene_name} already exists "
                    f"as target {clash.pk}, so this is a merge and not a rename — "
                    "left alone")
                continue

            planned.append((target, stored, corrected))

        self.stdout.write(self.style.MIGRATE_HEADING("Gene symbol case"))
        for _target, stored, corrected in planned:
            self.stdout.write(f"  {stored}")
            self.stdout.write(self.style.SUCCESS(f"      new: {corrected}"))
        if not planned:
            self.stdout.write("  nothing to change")

        if refused:
            self.stdout.write(self.style.WARNING("\nLeft alone"))
            for line in refused:
                self.stdout.write(f"  {line}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing written. {len(planned)} change(s) ready."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for target, _stored, corrected in planned:
                target.gene_name = corrected
                target.save(using=DB, update_fields=["gene_name", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied {len(planned)} change(s)."))
