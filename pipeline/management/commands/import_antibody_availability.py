"""Record what a supplier's own website says about each published antibody.

Every one of the 1,645 antibodies with a published figure — which is exactly the
set the public gene pages draw — was checked against its supplier's catalogue on
11–12 August 2026 and **re-checked on 30 August 2026**. This command writes the
verdicts onto ``Antibody.out_of_market``, which is the field the public pages
already read. No schema change: the flag exists, it was simply eight years stale.

TWO FILES, AND THE LATER ONE IS THE DEFAULT
  ``antibody_availability_2026_08.csv``     the 11–12 Aug pass, kept as the
                                            record of what was seen then.
  ``antibody_availability_2026_08_30.csv``  the 30 Aug recheck — same 1,645 ids,
                                            same catalogue numbers, and its
                                            ``previous_verdict`` column carries
                                            the August 12 answer forward so the
                                            later file does not lose it.
  The recheck moved 43 rows: 38 new withdrawals, 3 that were ``unclear`` and are
  now on sale, and 2 that went out of stock. 93 rows were not re-checked.

WHAT THE VERDICTS MEAN, AND WHICH OF THEM WRITE NOTHING
  ``available``       a live buy panel was seen — a price, a size option, an
                      in-stock line, an active add-to-cart. Backordered and long
                      lead times count, because the product is still orderable.
  ``discontinued``    the supplier's own page or catalogue search said so, in
                      those words, or the search returned no hits for the number.
  ``unclear``         genuinely indeterminate and **left alone** — mostly
                      structural: ABCD Antibodies and IPI entries are registry
                      records offering "contact the facility to assess production
                      feasibility", so they were never catalogue products and are
                      neither on sale nor withdrawn.
  ``not_rechecked``   nobody looked at it in this pass, so the verdict already on
                      the record stands. **This is not the same as ``unclear``**
                      — one says the answer could not be determined, the other
                      says the question was not asked — and the two are counted
                      and listed apart, since only one of them is a gap in the
                      check itself. Two of the 93 were ``discontinued`` on 12 Aug
                      and stay flagged; writing ``available`` over them because
                      nobody re-looked would put a withdrawn product back on
                      public gene pages.
  ``out_of_stock``    **left alone.** Out of stock is a shelf, not a catalogue:
                      ``out_of_market`` says the supplier no longer sells the
                      product, and the public page's tick box says so in those
                      words. Two Thermo products read this way on 30 Aug; the
                      right answer for them is the same as for a backorder.

  Anything else is a typo, and gets its own group in the output rather than being
  folded in with the three above — a misspelling counted as a deliberate
  abstention is a silent omission.

WHAT IT WILL NOT DO
  * **It will not write on the pk alone.** ``oga_id`` is the pk, and a pk is
    only meaningful against the database the file was built from — regenerate
    the check against a restored dump and the same integers name different
    reagents. Every row is confirmed by ``catalogue_number`` before anything is
    written, and a row whose catalogue disagrees is refused **by name, with both
    spellings**, never guessed at and never silently skipped. (All 1,645 agree
    today; the check is there for the next file.)
  * **It will not invent a record.** An ``oga_id`` naming no antibody is
    reported and passed over — this command records availability, it does not
    add reagents.
  * **It will not reach an antibody with no published figure.** Scope is
    ``pipeline/public.py``'s: the check covered the public set, so a row naming
    anything outside it is a sign the file was built somewhere else, and it is
    refused with the rest.

WHY A 404 IS NOT IN HERE ANYWHERE, AND WHY 20 WITHDRAWALS WRITE NOTHING
  A dead supplier URL is **not** evidence of withdrawal. Stale slugs are routine
  — 7 of 9 GeneTex URLs that 404'd were live under a changed slug, and the same
  trap caught Proteintech, Santa Cruz, Miltenyi, Abnova and Aviva. Every
  ``discontinued`` verdict in the 11–12 Aug file rests on an explicit on-page
  statement or a catalogue-search miss, quoted in ``evidence_quote``, never on a
  dead link — 180 rows, 180 quotes, none blank.

  **The 30 Aug recheck first arrived with none of that**, and its upstream
  "evidence" column held one identical string on all 38 rows: *"supplier order
  page, 30 Aug 2026"*. That records that a page was looked at, not what it said,
  and there is no way to turn it into a quote except by re-visiting the pages —
  writing one from inference would manufacture the very evidence the GeneTex
  finding says cannot be assumed. So the 38 were re-visited, and the re-visit
  was a **re-verification**, not a quote harvest. It found that only **18 of the
  38 clear the August standard**:

    18  an on-page statement — 16 Thermo ("The product (cat # …) that you're
        looking for has been discontinued"), ABclonal's "Discontinued" and
        Aviva's "Availability: Discontinued". **These write.**
    17  Abcam, rendering a complete datasheet with no price and no buy panel
        and **no discontinuation notice**. Checked against controls in both
        directions: ab4193 and ab39969, discontinued on the 12 Aug file, title
        as "is not available" and state "This product is discontinued"; five
        known-live products render prices reproducibly. The 17 match neither,
        so they are a third state — not purchasable, not declared withdrawn.
     1  Thermo's "not orderable at the moment … contact our Technical Support
        for possible alternatives" — a supply state, like a backorder.
     2  not established either way: DSHB's P1G12 404s (the URL scheme is right,
        /MF-20 resolves at $50, but their search ignores the query parameter, so
        a renamed listing could not be ruled out — the GeneTex trap exactly),
        and Bio-Techne's MAB6216 returned an empty body on both domains, being
        bot-gated.

  ``WITHDRAWAL_EVIDENCE`` is where that lives, and it holds the 20 back rather
  than writing them. **The verdict column was deliberately left alone** — the
  checker recorded what each page showed and let the evidence carry the
  distinction, which is the same shape as a knockout whose tick and whose note
  disagree: never resolved in favour of the stronger claim. 11 of the 38 carry
  three or more papers, and hiding a product somebody can still buy is the
  direction no screen would contradict.

  Quotes from the earlier pass are carried forward on rows whose verdict did not
  move, prefixed with their date so they cannot be read as something seen on the
  30th.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * Guarded to ``pipeline_db``.
  * Take a Render export before ``--apply`` (see the production-data skill).

    python manage.py import_antibody_availability
    python manage.py import_antibody_availability --apply
    python manage.py import_antibody_availability path/to/another.csv
"""
from __future__ import annotations

import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DB = "pipeline_db"

DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "antibody_availability_2026_08_30.csv",
)

#: The verdict column, and what each value writes. ``unclear`` is absent on
#: purpose — see the module docstring; ``None`` would read as "write nothing"
#: and so would a missing key, but only one of them says why in the output.
WRITES = {"available": False, "discontinued": True}

#: The verdicts that deliberately write nothing, and the sentence each one is.
#: They are separate groups on purpose — "nobody could tell", "nobody looked"
#: and "temporarily out of stock" are three different facts about the run, and
#: only the first two leave the previous verdict standing for the same reason.
#: A verdict in none of these is a typo and gets its own group, since a
#: misspelling folded in with the deliberate ones is a silent omission.
LEAVES_ALONE = {
    "unclear": "the check could not tell",
    "not_rechecked": "not looked at in this pass — the previous verdict stands",
    "out_of_stock": "temporarily out of stock, which is not a withdrawal",
}

#: What each kind of evidence is worth *for a withdrawal*, keyed on the
#: ``evidence_type`` column. ``None`` writes; anything else is the sentence
#: saying why the row is held back instead.
#:
#: A withdrawal takes a product off every public gene page, so the standard is
#: the one the 11–12 Aug file set: the supplier's own page says the product is
#: gone. The 30 Aug re-visit found three kinds of row that do not clear it, and
#: the checker recorded each rather than dressing it as a statement:
#:
#:   * Abcam renders a complete datasheet with no price and no buy panel and
#:     **no discontinuation notice**. That is not Abcam's discontinued state —
#:     checked both ways against controls, where a withdrawn product titles as
#:     "is not available" and states "This product is discontinued", and five
#:     live ones render prices. A third state, and not one this flag can say.
#:   * A 404 is not a withdrawal — the whole reason the docstring says so.
#:   * A page that could not be read is evidence of nothing either way.
#:
#: ``on_page_statement_soft`` is the near miss: Thermo's "not orderable at the
#: moment … contact our Technical Support for possible alternatives" is a supply
#: state, and ``out_of_market`` is a catalogue one. Same answer as a backorder.
#:
#: The gate applies **only to a file that carries the column at all**. Absent, it
#: is not a file that recorded evidence types and is read as before; present, a
#: withdrawal with a blank or unfamiliar one is held back, since a type nobody
#: recognises is not a standard anybody checked against.
WITHDRAWAL_EVIDENCE = {
    "on_page_statement": None,
    "on_page_statement_soft":
        "the page says not orderable, which is a supply state and not a withdrawal",
    "no_purchase_option_no_statement":
        "no buy panel, but the page carries no discontinuation notice",
    "unverified_404":
        "the product page 404s, and a dead link is not a withdrawal",
    "unreachable":
        "the page could not be read, so it is evidence neither way",
}

#: What a withdrawal with no recognised evidence type is held back for.
UNVOUCHED_WITHDRAWAL = "no evidence type recorded for this withdrawal"

REQUIRED_COLUMNS = {"oga_id", "catalogue_number", "availability_checked"}


def _norm(value):
    """Compare catalogue numbers the way a person reads them off a label."""
    return (value or "").strip().casefold()


class Command(BaseCommand):
    help = ("Record supplier availability on Antibody.out_of_market from the "
            "committed check. Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path", nargs="?", default=DEFAULT_CSV,
            help=f"Path to the availability CSV (default: {DEFAULT_CSV})")
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the changes (default is a dry run)")

    @staticmethod
    def _why_not_withdrawn(row):
        """Why this row's evidence does not support taking a product off sale.

        ``None`` means it does. An evidence type the file does not name, or one
        this command has never heard of, is held back rather than written: the
        harmful direction is hiding a product somebody can still buy, and that
        is the one no screen would contradict.
        """
        kind = (row.get("evidence_type") or "").strip().lower()
        if not kind:
            return UNVOUCHED_WITHDRAWAL
        if kind not in WITHDRAWAL_EVIDENCE:
            return f"evidence type {kind!r} is not one this command knows"
        return WITHDRAWAL_EVIDENCE[kind]

    def handle(self, *args, **opts):
        from pipeline.models import Antibody

        path = opts["csv_path"]
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")

        if not rows:
            raise CommandError(f"No rows in {path}")

        missing = REQUIRED_COLUMNS - set(rows[0])
        if missing:
            raise CommandError(
                f"{path} is missing column(s): {', '.join(sorted(missing))}. "
                f"Expected at least: {', '.join(sorted(REQUIRED_COLUMNS))}.")

        # One query for the file, not one per row.
        wanted = []
        for row in rows:
            try:
                wanted.append(int(row["oga_id"]))
            except (TypeError, ValueError):
                pass
        on_file = {
            ab.pk: ab
            for ab in Antibody.objects.using(DB)
            .filter(pk__in=wanted)
            .only("pk", "catalogue_number", "out_of_market")
        }

        to_set, to_clear, agreed, refused = [], [], 0, []
        left_alone = {verdict: [] for verdict in LEAVES_ALONE}
        unrecognised = []
        unvouched = []
        # A file that records no evidence types is not held to them.
        grades_evidence = "evidence_type" in rows[0]

        for line_no, row in enumerate(rows, start=2):  # row 1 is the header
            raw_id = (row.get("oga_id") or "").strip()
            catalogue = (row.get("catalogue_number") or "").strip()
            verdict = (row.get("availability_checked") or "").strip().lower()
            label = f"{catalogue or '(no catalogue number)'} (oga_id {raw_id or '?'})"

            try:
                pk = int(raw_id)
            except (TypeError, ValueError):
                refused.append(f"line {line_no}: oga_id {raw_id!r} is not a "
                               f"record number — left alone")
                continue

            antibody = on_file.get(pk)
            if antibody is None:
                refused.append(f"{label}: no antibody with that record number "
                               f"— left alone")
                continue

            # The pk found a row; the catalogue number is what confirms it is
            # the row the checker was looking at.
            if _norm(antibody.catalogue_number) != _norm(catalogue):
                refused.append(
                    f"{label}: record {pk} is "
                    f"{antibody.catalogue_number or '(blank)'}, not {catalogue} "
                    f"— the file was built against a different database, left alone")
                continue

            if verdict in LEAVES_ALONE:
                left_alone[verdict].append(label)
                continue

            if verdict not in WRITES:
                # Not one of the verdicts this command knows, deliberate or
                # otherwise — reported by name rather than assumed to be one.
                unrecognised.append(f"{label}: {verdict or '(blank)'}")
                continue

            wants = WRITES[verdict]
            evidence = row.get("evidence_quote", "")
            if antibody.out_of_market == wants:
                agreed += 1
            elif wants:
                # Everything above this line asks whether the file names this
                # row. This asks whether the file has grounds for what it says.
                held = self._why_not_withdrawn(row) if grades_evidence else None
                if held:
                    unvouched.append((label, held, evidence))
                else:
                    to_set.append((antibody, label, evidence))
            else:
                to_clear.append((antibody, label))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Availability check — {len(rows):,} row(s) from {path}"))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nNow discontinued ({len(to_set)})"))
        for _ab, label, evidence in to_set:
            self.stdout.write(f"  {label}")
            if evidence.strip():
                self.stdout.write(f"      {evidence[:150]}")
            else:
                # A withdrawal takes a product off every public gene page, so
                # the absence of a quote behind it is said out loud rather than
                # printed as a blank line.
                self.stdout.write(self.style.WARNING(
                    "      no on-page quote recorded for this verdict"))
        if not to_set:
            self.stdout.write("  none")

        if unvouched:
            self.stdout.write(self.style.WARNING(
                f"\nCalled discontinued, but not written — the evidence does "
                f"not reach the standard a withdrawal is held to ({len(unvouched)})"))
            for label, why, evidence in unvouched:
                self.stdout.write(f"  {label}: {why}")
                if evidence.strip():
                    self.stdout.write(f"      {evidence[:200]}")
            self.stdout.write(
                "  These stay on their gene pages. To overrule, correct the "
                "evidence in the CSV — the evidence is what is in question.")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nBack on sale ({len(to_clear)})"))
        for _ab, label in to_clear:
            self.stdout.write(f"  {label}")
        if not to_clear:
            self.stdout.write("  none")

        self.stdout.write(
            f"\n{agreed:,} row(s) already say what the check found.")

        for verdict, reason in LEAVES_ALONE.items():
            labels = left_alone[verdict]
            if not labels:
                continue
            self.stdout.write(self.style.WARNING(
                f"\nNothing written — {reason} ({len(labels)})"))
            for line in labels:
                self.stdout.write(f"  {line}")

        if unrecognised:
            self.stdout.write(self.style.ERROR(
                f"\nNothing written — verdict not recognised ({len(unrecognised)})"))
            for line in unrecognised:
                self.stdout.write(f"  {line}")

        if refused:
            self.stdout.write(self.style.ERROR(
                f"\nRefused ({len(refused)})"))
            for line in refused:
                self.stdout.write(f"  {line}")

        planned = len(to_set) + len(to_clear)

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing written. {planned} change(s) ready."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for antibody, _label, _evidence in to_set:
                antibody.out_of_market = True
                antibody.save(using=DB,
                              update_fields=["out_of_market", "updated_at"])
            for antibody, _label in to_clear:
                antibody.out_of_market = False
                antibody.save(using=DB,
                              update_fields=["out_of_market", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"\nApplied {planned} change(s): {len(to_set)} discontinued, "
            f"{len(to_clear)} back on sale."))
