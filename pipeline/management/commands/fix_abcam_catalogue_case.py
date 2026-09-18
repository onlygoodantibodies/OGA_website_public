"""Correct Abcam catalogue numbers typed in the wrong case in live ``pipeline_db``.

Abcam writes its catalogue numbers with a lowercase prefix — ``ab302677``, as
its own product URLs do — and our own data agrees overwhelmingly. Of the 697
Abcam rows on live (30 Aug 2026), **600 are ``ab<digits>``, 69 are
``AB<digits>``** and 28 are something else entirely. So the convention is read
off the database rather than assumed, and the 69 are the outliers.

Sibling of ``fix_proteintech_catalogue_case``, which owns Proteintech's ``-Ig``
suffix. One command per supplier convention, because the rule *is* the
supplier's and there is no general one to share.

WHAT IT COSTS TO LEAVE THEM
  * The public gene page prints the catalogue number verbatim, so a reader
    copies a number into a supplier search in a case the supplier does not use.
  * ``cropper/engine.py::filename_for`` builds a figure's object key from the
    catalogue **verbatim**, so the stored catalogue and the published figure's
    key have already drifted apart: NR3C1's ``AB302677`` has its crop at
    ``NR3C1_ab302677_WB.png``, which is what dates the drift — the record read
    ``ab302677`` when the figure was cropped.
  * It is **not** why any row is missing from the browser extension. Both sides
    of that lookup fold case (``extension_index`` stores ``cat.lower()``,
    ``matcher.js::normaliseKey`` lowercases), so the extension is indifferent
    and always was. Blank ``rrid`` is what excludes a row, and
    ``backfill_rrid_from_registry`` is that half. This command runs first only
    because that one sends the stored string to the Antibody Registry
    **verbatim** (``scicrunch.py``: ``q=vendors.catalogNumber:"{catalogue}"``),
    and whether that field is case-sensitive at SciCrunch's end is not
    something this repository can answer. Correcting the case removes the
    question rather than settling it.

WHAT IT WILL NOT DO
  * **It changes case and nothing else**, and only for ``^AB\\d+$`` — the one
    shape whose correct spelling is established. Every planned change is
    asserted to differ from what is stored only in case before it is written,
    so the whole plan is reviewable by eye. A blanket ``.lower()`` over Abcam
    rows is the mistake the obvious version of this makes: 28 Abcam rows carry
    something else (``YCA-R28814-37`` and the other pre-release codes,
    ``ab243904-100ul`` with a pack size, ``2020A4849(H GCase)``), and one holds
    *both* casings with a newline between them (``AB32071\\nab32071``). None is
    a case fix; all are left alone, and the ones that look like a near miss are
    listed, because a count with no list under it invents the noun.
  * **It refuses a group that would land two rows on one identity.** The check
    is deliberately **case-insensitive**, which the database's own constraint is
    not: ``unique_antibody_per_site_lot`` compares ``varchar`` case-sensitively,
    so PostgreSQL accepts ``AB302677`` beside ``ab302677`` without a murmur and
    leaves two rows every ``__iexact`` reader matches at once — ``.first()``
    then picks one arbitrarily. Checking only what the constraint allows would
    miss the one case that matters, which is the lesson
    ``fix_proteintech_catalogue_case`` was written around.

    **But the group is the identity, not the catalogue number.** An antibody is
    ``(catalogue, company, target)`` and ``lot`` and ``site`` say which vial,
    so the same product held at two benches is two legitimate rows — and for
    Abcam that is the *normal* case, not the exception. Grouping on supplier +
    catalogue alone flags ten groups here and every one of them is McGill and
    Leicester holding different lots of one product (``ab124807`` on OGA,
    ``AB32071`` on PARP1, and so on). Refusing those would leave the data half
    corrected, which is worse than either extreme: one product drawn as
    ``AB32071`` and ``ab32071`` on a single gene page. Grouped on the full
    identity, live has **no** collisions among the 69 — so this refusal is
    currently unreachable, and it is here so that stays checked rather than
    assumed.
  * **It moves no files.** Nothing reads a catalogue number back out of an
    image key — the stored path is served as-is — so no figure changes, moves
    or 404s. What changes is where a *future* re-crop would write: two
    antibodies (PARK7's ``AB201147`` and ``AB76241``, 6 figures) have their
    crops at uppercase keys today, so after this their next re-crop lands at a
    new key instead of replacing the object in place — the invariant
    ``review.py`` relies on. Named in the output rather than left to be found.
    The other 36 figures on these rows are already at lowercase keys, so for
    those this *restores* the agreement.

WHAT IT DOES NOT CLOSE
  Nothing stops a paste re-entering ``AB302677`` tomorrow — the write paths have
  no opinion on a supplier's catalogue format. That is a separate change to
  ``services/`` with its own preview behaviour, not something to bolt onto a
  one-off correction. Same gap ``fix_proteintech_catalogue_case`` names.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * ``--ids`` confines a run to named antibodies, to build confidence first.
  * Guarded to ``pipeline_db``.
  * Take a Render export before ``--apply`` (see the production-data skill).

Saving an antibody clears the extension snapshot cache (``core/apps.py``), so
``/extension/index.json`` rebuilds on its next request. Nothing renumbers: the
``lab_numbers`` ``pre_save`` returns early for a record that already exists.

    python manage.py fix_abcam_catalogue_case
    python manage.py fix_abcam_catalogue_case --ids 3802 3823
    python manage.py fix_abcam_catalogue_case --apply
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

DB = "pipeline_db"

#: The supplier whose convention this command knows. Adding a second means
#: checking that supplier's rows against the database first — which is how
#: Abcam's own rule was settled — not adding a regex here and hoping.
SUPPLIER_MATCH = "abcam"

#: The one shape with an established correct spelling. Anything else on an
#: Abcam row is left alone.
CORRECTABLE = re.compile(r"^AB\d+$")

#: Nearly the correctable shape and deliberately not it. A near miss is worth
#: naming: somebody reading "55 changed" needs to know the newline row was seen
#: and skipped, not that it slipped through.
NEAR_MISS = re.compile(r"(?is)^\s*ab\s*\d")


def corrected(catalogue: str) -> str:
    """The catalogue number with Abcam's own lowercase prefix."""
    return catalogue.lower()


class Command(BaseCommand):
    help = ("Correct Abcam catalogue numbers typed in the wrong case "
            "(AB302677 -> ab302677). Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes (default is a dry run)")
        parser.add_argument("--ids", nargs="+", type=int, default=None,
                            help="Confine the run to these antibody ids")

    def handle(self, *args, **opts):
        from pipeline.models import Antibody, Company

        companies = list(Company.objects.using(DB)
                         .filter(name__icontains=SUPPLIER_MATCH)
                         .order_by("name").only("pk", "name"))
        if not companies:
            self.stdout.write(self.style.ERROR(
                f"No supplier whose name contains '{SUPPLIER_MATCH}' is on "
                "file — nothing to do."))
            return

        rows = (Antibody.objects.using(DB)
                .filter(company__in=companies)
                .select_related("company", "target")
                .order_by("catalogue_number", "pk"))
        if opts["ids"]:
            rows = rows.filter(pk__in=opts["ids"])

        candidates, near = [], []
        seen = 0

        for ab in rows:
            seen += 1
            stored = (ab.catalogue_number or "").strip()
            if not stored:
                continue
            if not CORRECTABLE.match(stored):
                if NEAR_MISS.match(stored) and stored != stored.lower():
                    near.append(
                        f"{stored!r} (id {ab.pk}): not a plain AB<digits>, so "
                        "its correct spelling is not established here — left "
                        "alone")
                continue
            fixed = corrected(stored)
            if fixed == stored:
                continue
            candidates.append((ab, stored, fixed))

        planned, refused = self._plan(candidates)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "Abcam catalogue number case"))
        self.stdout.write(
            f"  read {seen} row(s) under "
            + ", ".join(f"{c.name} (id {c.pk})" for c in companies))

        for ab, stored, fixed in planned:
            gene = getattr(ab.target, "gene_name", None) or "no gene"
            self.stdout.write(f"  {stored}  ({gene}, antibody {ab.pk})")
            self.stdout.write(self.style.SUCCESS(f"      new: {fixed}"))
        if not planned:
            self.stdout.write("  nothing to change")

        self._report_figures(planned)

        if near:
            self.stdout.write(self.style.WARNING(
                f"\nNot a plain AB<digits> — left alone ({len(near)})"))
            for line in near:
                self.stdout.write(f"  {line}")

        if refused:
            self.stdout.write(self.style.WARNING(f"\nRefused ({len(refused)})"))
            for line in refused:
                self.stdout.write(f"  {line}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing written. {len(planned)} change(s) ready."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for ab, _stored, fixed in planned:
                ab.catalogue_number = fixed
                ab.save(using=DB,
                        update_fields=["catalogue_number", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied {len(planned)} change(s)."
            "\nRun backfill_rrid_from_registry --scope published next."))

    def _plan(self, candidates):
        """Split the candidates into what will be written and what is refused.

        A group is every candidate that would end up on the same **identity** —
        (catalogue, company, target, lot, site), case-insensitively — plus any
        row already sitting on it. See the module docstring for why the
        identity and not the catalogue number alone: two benches holding one
        product is the normal case for this supplier, not a duplicate.
        """
        from pipeline.models import Antibody

        groups = defaultdict(list)
        for ab, stored, fixed in candidates:
            groups[(ab.company_id, ab.target_id, ab.lot_number or "",
                    ab.site_id, fixed.upper())].append((ab, stored, fixed))

        planned, refused = [], []
        for key, members in sorted(groups.items(), key=lambda kv: kv[0][4]):
            company_id, target_id, lot, site_id, _upper = key
            occupant = (Antibody.objects.using(DB)
                        .filter(company_id=company_id, target_id=target_id,
                                lot_number=lot, site_id=site_id,
                                catalogue_number__iexact=members[0][2])
                        .exclude(pk__in=[m[0].pk for m in members])
                        .first())

            if len(members) > 1 or occupant is not None:
                named = ", ".join(
                    f"{stored} (id {ab.pk}, "
                    f"{getattr(ab.target, 'gene_name', None) or 'no gene'})"
                    for ab, stored, _f in members)
                if occupant is not None:
                    named += (f", and {occupant.catalogue_number} "
                              f"(id {occupant.pk}) already holds it")
                refused.append(
                    f"{members[0][2]}: {named} — correcting these would put one "
                    "string on one supplier, gene, lot and site more than once, "
                    "which every case-insensitive reader then matches at once. "
                    "That is a merge, not a rename — left alone, both of them.")
                continue

            # Only reachable for a group of one; the refusal above covers the
            # rest. Stated rather than assumed, because an edit that loosened
            # the refusal would otherwise drop the other rows in silence, which
            # is the failure this command is about.
            assert len(members) == 1, members
            ab, stored, fixed = members[0]

            # Belt and braces over the regex: a plan a reader cannot check by
            # eye is not one to run against live data.
            if fixed.upper() != stored.upper():
                refused.append(
                    f"{stored} (id {ab.pk}): would become {fixed}, which is a "
                    "different number and not a case fix — left alone")
                continue

            planned.append((ab, stored, fixed))

        return planned, refused

    def _report_figures(self, planned):
        """Name the published figures whose object key will stop matching.

        Nothing breaks — the stored path is what gets served, and no reader
        parses a catalogue number back out of it. What changes is where the
        *next* crop of that figure would be written, so a re-crop would add an
        object beside the live one rather than replacing it in place.
        """
        from pipeline.models import PublicationImage

        if not planned:
            return
        images = (PublicationImage.objects.using(DB)
                  .filter(antibody_id__in=[a.pk for a, _s, _f in planned])
                  .only("antibody_id", "application_type", "image"))
        by_antibody = defaultdict(list)
        for img in images:
            by_antibody[img.antibody_id].append(img)

        diverging = []
        for ab, stored, fixed in planned:
            for img in by_antibody.get(ab.pk, []):
                if stored in (img.image.name or ""):
                    diverging.append(
                        f"{stored} ({img.application_type}): figure is at "
                        f"{img.image.name}, which will no longer match the "
                        f"catalogue {fixed}")

        if diverging:
            self.stdout.write(self.style.WARNING(
                "\nPublished figures whose object key will stop matching "
                f"({len(diverging)})"))
            self.stdout.write(
                "  Nothing moves and nothing 404s — this only decides where a "
                "future re-crop would be written.")
            for line in diverging:
                self.stdout.write(f"  {line}")
