"""Correct Proteintech catalogue numbers whose ``-Ig`` suffix was mis-transcribed.

Proteintech names a product for the class of reagent it is: ``-AP`` is an
affinity-purified rabbit polyclonal, ``-RR`` a recombinant rabbit, and ``-Ig``
a mouse monoclonal — ``Ig`` for immunoglobulin, capital ``I``. On live
``pipeline_db`` 19 rows spell that suffix some other way, and they arrived that
way: ``access_csvs/Antibodies.csv`` holds both spellings, so the corruption is
the 2019 Access transcription rather than anything this app wrote. In a
sans-serif face a capital ``I`` and a lowercase ``l`` are the same glyph, which
is how it happened and why nobody caught it by reading.

TWO CLASSES, AND ONLY ONE OF THEM BREAKS ANYTHING
  * ``-lg`` (lowercase L) — **11 rows, genuinely broken.** ``l`` and ``i`` are
    different letters, so no amount of case-folding rescues them: every lookup
    in this repo misses. ``services/find.py`` (the site's own search box),
    ``portal.py``'s ``catalogue_number__iexact``, the extension's
    ``matcher.js::normaliseIdentifier`` and every supplier comparison are all
    matching a string Proteintech never printed. The RRIDs on these rows are
    correct, which is what makes the fix safe to derive.
  * ``-ig`` (lowercase i) — **8 rows, cosmetic.** Every one of those lookups is
    case-insensitive (``__iexact``, ``__icontains``, and ``normaliseKey``'s
    ``toLowerCase``), so these match today and always did. They are wrong on
    screen, in exports and on anything a scientist copies into a supplier site,
    and they are not a matching failure. Reported apart from the 11 for exactly
    that reason; ``--breaking-only`` does the 11 alone.

WHAT IT WILL NOT DO
  * **It changes the suffix and nothing else.** The digits are the product, so
    every planned change is asserted to be byte-identical up to the final two
    characters before it is written — the whole plan is reviewable by eye, and
    a transform that could also renumber a product is not one anybody can check
    against live data. Same guard, and the same reason, as ``fix_gene_case``.
  * **It refuses a duplicate group by name.** ``67322-1-lg`` and ``67322-1-ig``
    are one EPHX2 product recorded twice, and correcting both would point them
    at one string. The check is deliberately **case-insensitive**, which the
    database's own constraint is not: ``unique_antibody_per_site_lot`` compares
    ``varchar`` case-sensitively, so PostgreSQL would accept ``-Ig`` beside
    ``-ig`` without a murmur and leave two rows that every ``__iexact`` reader
    matches at once — ``.first()`` then picks one arbitrarily, which is the
    coin-toss ``services/cell_lines.py`` exists to prevent. Checking what the
    constraint allows would therefore have missed the one case that matters.
    Merging them is ``merge_duplicate_antibodies``-shaped work and a separate
    decision with a human in front of it; this reports the pair and writes
    neither.
  * **It does not touch a non-Proteintech row.** ``-Ig`` is this vendor's
    convention and nobody else's, so a candidate filed under another company is
    refused rather than assumed.

WHAT IT DOES NOT CLOSE
  Nothing stops a paste re-entering ``67499-1-lg`` tomorrow — the write paths
  have no opinion on a supplier's catalogue format. That is a separate change
  to ``services/`` with its own preview behaviour, not something to bolt onto a
  one-off correction.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * Guarded to ``pipeline_db``.
  * Take a Render export before ``--apply`` (see the production-data skill).

    python manage.py fix_proteintech_catalogue_case
    python manage.py fix_proteintech_catalogue_case --breaking-only
    python manage.py fix_proteintech_catalogue_case --apply
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

DB = "pipeline_db"

#: The mis-spellings of the ``-Ig`` suffix seen on live data. ``-Ig`` itself is
#: deliberately absent: a row already correct is not a candidate.
WRONG_SUFFIX_RE = re.compile(r"-(lg|ig|LG|IG|lG|iG|Lg)$")

CORRECT_SUFFIX = "-Ig"

#: ``l`` and ``i`` are different letters, so only this one is a matching
#: failure. The other spellings survive every case-insensitive reader we have.
BREAKING_RE = re.compile(r"-lg$")


def corrected(catalogue: str) -> str:
    """The catalogue number with its suffix spelled the way Proteintech spells it."""
    return WRONG_SUFFIX_RE.sub(CORRECT_SUFFIX, catalogue)


class Command(BaseCommand):
    help = ("Correct Proteintech catalogue numbers whose -Ig suffix is "
            "mis-transcribed. Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes (default is a dry run)")
        parser.add_argument("--breaking-only", action="store_true",
                            help="Only the -lg rows, which are the ones that "
                                 "actually break matching")

    def handle(self, *args, **opts):
        from pipeline.models import Antibody

        rows = (Antibody.objects.using(DB)
                .select_related("company", "target")
                .order_by("catalogue_number"))

        candidates, refused = [], []

        for ab in rows:
            stored = (ab.catalogue_number or "").strip()
            if not WRONG_SUFFIX_RE.search(stored):
                continue

            breaking = bool(BREAKING_RE.search(stored))
            if opts["breaking_only"] and not breaking:
                continue

            company = getattr(ab.company, "name", "") or ""
            if "proteintech" not in company.lower():
                refused.append(
                    f"{stored} (id {ab.pk}): filed under {company or 'no company'}, "
                    "and -Ig is Proteintech's convention — left alone")
                continue

            candidates.append((ab, stored, corrected(stored), breaking))

        # A group is every candidate that would end up at the same string, plus
        # any row already sitting on it. Case-insensitively — see the module
        # docstring: the database's own constraint would not catch this.
        groups = defaultdict(list)
        for item in candidates:
            groups[(item[0].company_id, item[2].upper())].append(item)

        planned = []
        for (company_id, key), members in sorted(groups.items(), key=lambda kv: kv[0][1]):
            occupant = (Antibody.objects.using(DB)
                        .filter(company_id=company_id, catalogue_number__iexact=members[0][2])
                        .exclude(pk__in=[m[0].pk for m in members])
                        .first())

            if len(members) > 1 or occupant is not None:
                named = ", ".join(
                    f"{stored} (id {ab.pk}, {getattr(ab.target, 'gene_name', None) or 'no gene'})"
                    for ab, stored, _c, _b in members)
                if occupant is not None:
                    named += f", and {occupant.catalogue_number} (id {occupant.pk}) already holds it"
                refused.append(
                    f"{members[0][2]}: {named} — correcting these would put one "
                    "string on more than one row, which every case-insensitive "
                    "reader then matches at once. That is a merge, not a rename "
                    "— left alone, both of them.")
                continue

            # Only reachable for a group of one — the refusal above covers the
            # rest. Stated rather than assumed: planning `members[0]` alone is
            # correct only while that holds, and an edit that loosened the
            # refusal would otherwise drop the other rows in silence, which is
            # the failure this whole command is about.
            assert len(members) == 1, members
            ab, stored, fixed, breaking = members[0]

            # Belt and braces over the regex: the digits are the product, and a
            # plan a reader cannot check by eye is not one to run against live
            # data.
            if stored[:-2] != fixed[:-2] or not fixed.endswith(CORRECT_SUFFIX):
                refused.append(
                    f"{stored} (id {ab.pk}): would become {fixed}, which changes "
                    "more than the suffix — left alone")
                continue

            planned.append((ab, stored, fixed, breaking))

        self._report(planned, refused)

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing written. {len(planned)} change(s) ready."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for ab, _stored, fixed, _breaking in planned:
                ab.catalogue_number = fixed
                ab.save(using=DB, update_fields=["catalogue_number", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied {len(planned)} change(s)."))

    def _report(self, planned, refused):
        breaking = [p for p in planned if p[3]]
        cosmetic = [p for p in planned if not p[3]]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "Breaks matching — lowercase L for capital I"))
        for ab, stored, fixed, _b in breaking:
            gene = getattr(ab.target, "gene_name", None) or "no gene"
            self.stdout.write(f"  {stored}   {gene} (id {ab.pk}, {ab.rrid or 'no RRID'})")
            self.stdout.write(self.style.SUCCESS(f"      new: {fixed}"))
        if not breaking:
            self.stdout.write("  nothing to change")

        if cosmetic:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nCosmetic — lowercase i, matched correctly today"))
            for ab, stored, fixed, _b in cosmetic:
                gene = getattr(ab.target, "gene_name", None) or "no gene"
                self.stdout.write(f"  {stored}   {gene} (id {ab.pk})")
                self.stdout.write(self.style.SUCCESS(f"      new: {fixed}"))

        if refused:
            self.stdout.write(self.style.WARNING("\nLeft alone"))
            for line in refused:
                self.stdout.write(f"  {line}")
