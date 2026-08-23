"""Correct five identity fields that are wrong in live ``pipeline_db``.

Found by the target-list import: uploading Carl's spreadsheet reported 40
disagreements, and most were harmless (a rounded mass, a shorter synonym list).
These five are not — they are defects in our data, not differences of opinion,
and one of them is visible on a public gene page.

    ARID1B  protein_name       "T-rich …"          — the leading "A" is missing
    ATXN2   protein_name       "Ataxin2"           — missing hyphen
    VCP     alternative_name   "VCPValosin-…"      — symbol and name concatenated
    SMPD1   alternative_name   "Acid sphingomyelinase,"  — trailing comma
    BDH2    uniprot_id         "D6RFG2"            — see below

BDH2 is the substantive one and is opt-in separately. The stored accession has a
theoretical mass of 9.58 kDa, which is far too small for a dehydrogenase, so it
looks like an isoform fragment rather than the canonical entry; Carl's file says
``Q9BUT1``. Rather than trust either source, ``--fix-bdh2`` asks UniProt what
each accession actually is and prints it, and on ``--apply`` sets the accession
and **clears the mass** so ``enrich_targets_from_uniprot`` refills it from the
right entry. That keeps the data hierarchy intact: UniProt decides, not a
spreadsheet and not this file.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * Every correction states the value it expects to find. If the database holds
    something else — already fixed, or edited since — that row is skipped and
    reported, never overwritten.
  * Guarded to ``pipeline_db``.
  * Take a Render export before ``--apply`` (see the production-data skill).

    python manage.py fix_target_identity_typos
    python manage.py fix_target_identity_typos --fix-bdh2
    python manage.py fix_target_identity_typos --fix-bdh2 --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

DB = "pipeline_db"

# (gene, field, value we expect to find, corrected value, why)
CORRECTIONS = [
    ("ARID1B", "protein_name",
     "T-rich interactive domain-containing protein 1B",
     "AT-rich interactive domain-containing protein 1B",
     "Leading 'A' lost. This one is live on the public gene page."),
    ("ATXN2", "protein_name",
     "Ataxin2", "Ataxin-2",
     "Missing hyphen."),
    ("VCP", "alternative_name",
     "VCPValosin-containing protein", "Valosin-containing protein",
     "Gene symbol and protein name concatenated with no separator."),
    ("SMPD1", "alternative_name",
     "Acid sphingomyelinase,", "Acid sphingomyelinase",
     "Trailing comma."),
]

BDH2_WRONG = "D6RFG2"
BDH2_RIGHT = "Q9BUT1"


class Command(BaseCommand):
    help = "Fix five known-wrong target identity fields. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes (default is a dry run)")
        parser.add_argument("--fix-bdh2", action="store_true",
                            help="Also repoint BDH2's accession and clear its mass")

    def handle(self, *args, **opts):
        from pipeline.models import Target

        apply_changes = opts["apply"]
        planned, skipped = [], []

        for gene, field, expected, corrected, why in CORRECTIONS:
            target = Target.objects.using(DB).filter(gene_name__iexact=gene).first()
            if target is None:
                skipped.append(f"{gene}: no such target")
                continue
            current = (getattr(target, field) or "").strip()
            if current == corrected:
                skipped.append(f"{gene}: already correct")
                continue
            if current != expected:
                skipped.append(
                    f"{gene}: expected {expected!r} but found {current!r} — left alone")
                continue
            planned.append((target, field, current, corrected, why))

        self.stdout.write(self.style.MIGRATE_HEADING("Text corrections"))
        for target, field, current, corrected, why in planned:
            self.stdout.write(f"  {target.gene_name}  ({field})")
            self.stdout.write(f"      now: {current}")
            self.stdout.write(self.style.SUCCESS(f"      new: {corrected}"))
            self.stdout.write(f"      {why}")
        if not planned:
            self.stdout.write("  nothing to change")

        bdh2 = None
        if opts["fix_bdh2"]:
            bdh2 = self._plan_bdh2()

        if skipped:
            self.stdout.write(self.style.WARNING("\nSkipped"))
            for line in skipped:
                self.stdout.write(f"  {line}")

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing written. {len(planned)}"
                f"{' + BDH2' if bdh2 else ''} change(s) ready."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for target, field, _current, corrected, _why in planned:
                setattr(target, field, corrected)
                target.save(using=DB, update_fields=[field, "updated_at"])
            if bdh2:
                bdh2.uniprot_id = BDH2_RIGHT
                bdh2.theoretical_mass_kda = None
                bdh2.save(using=DB, update_fields=[
                    "uniprot_id", "theoretical_mass_kda", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied {len(planned)}{' + BDH2' if bdh2 else ''} change(s)."))
        if bdh2:
            self.stdout.write(
                "Now run: python manage.py enrich_targets_from_uniprot --apply\n"
                "to refill BDH2's mass from the corrected accession.")

    def _plan_bdh2(self):
        """Show what UniProt says about both accessions before touching anything."""
        from pipeline.models import Target
        from pipeline.services import uniprot

        self.stdout.write(self.style.MIGRATE_HEADING("\nBDH2 accession"))
        target = Target.objects.using(DB).filter(gene_name__iexact="BDH2").first()
        if target is None:
            self.stdout.write("  no BDH2 target")
            return None
        current = (target.uniprot_id or "").strip()
        if current != BDH2_WRONG:
            self.stdout.write(
                f"  expected {BDH2_WRONG} but found {current!r} — left alone")
            return None

        seen = {}
        for accession in (BDH2_WRONG, BDH2_RIGHT):
            info = uniprot.lookup_accession(accession)
            seen[accession] = info.get("found")
            if info.get("found"):
                self.stdout.write(
                    f"  {accession}: {info.get('protein_name') or '(no name)'} — "
                    f"{info.get('mass_kda')} kDa")
            else:
                self.stdout.write(self.style.WARNING(
                    f"  {accession}: could not be read ({info.get('error')})"))

        # The four text fixes are self-evident from the strings themselves. This
        # one is a judgement about which UniProt entry is the right protein, so
        # it must not be applied on the strength of a comment in this file.
        if not seen.get(BDH2_RIGHT):
            self.stdout.write(self.style.ERROR(
                f"  Cannot confirm {BDH2_RIGHT} against UniProt, so BDH2 will be "
                "left alone. Re-run when UniProt is reachable."))
            return None
        self.stdout.write(
            f"      now: {current}, mass {target.theoretical_mass_kda}")
        self.stdout.write(self.style.SUCCESS(
            f"      new: {BDH2_RIGHT}, mass cleared for UniProt to refill"))
        self.stdout.write(
            "      Check the two entries above before applying — if D6RFG2 is the "
            "one you want, skip --fix-bdh2.")
        return target
