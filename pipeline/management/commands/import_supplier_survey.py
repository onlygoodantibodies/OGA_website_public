"""Apply the 2–3 Oct 2026 supplier page survey: discontinued flags and product links.

The survey visited the supplier page of every antibody whose link or availability
looked wrong, and compared what it found with the live database on the morning of
3 Oct. ``pipeline/data/antibody_site_updates_2026_10_03.csv`` is its "Updates"
tab, one row per antibody that needs a change. Three columns write, onto
``Antibody`` in ``pipeline_db``, and a blank means "leave it as it is":

  ``set_out_of_market``   TRUE marks the product discontinued, FALSE unmarks it.
  ``set_supplier_url``    a product link, added where there was none or replacing
                          a dead one, a search page or a different product.
  ``clear_supplier_url``  TRUE empties the link: the stored one is dead or opens a
                          different product, and no page for this one was found.

``evidence_type`` was added when the tab was committed, graded from its
``evidence`` column, and ``screenshot`` names the survey's capture of each page
(the images themselves are the owner's, not in the repo).

WHAT IT WILL NOT DO
  * **Write on the pk alone.** ``id`` is confirmed by ``catalogue_number``, as in
    ``import_antibody_availability``; a row whose catalogue disagrees is refused
    with both spellings.
  * **Overwrite somebody's later edit.** Each change carries the value the survey
    read (``out_of_market_now``, ``current_supplier_url``). Where the database
    holds something else now, somebody changed it on a board since, and the
    change is refused and named. Checked per field, so a link edited since does
    not hold back the row's discontinued flag, or the other way round.
  * **Take a product off its gene page on a catalogue-search miss.** Same
    standard as the August checks: a withdrawal is written only on the supplier's
    own statement (``on_page_statement``). Three rows rest on a search that found
    nothing, and those are held back, named, with their links still fixed:
      - DSHB P1G12 (415): DSHB's search ignores the query parameter, which is
        why the 30 Aug recheck held the same product back.
      - Synaptic Systems "100 000m" / "100 000p" (4560, 4561): the catalogue
        numbers look like placeholders, so the record is what is wrong, not
        the product's availability.
  * **Store a link that is not one.** A ``URLField`` validates nothing on
    ``save()``, so each new link is checked here (http/https, under 500
    characters) and refused by name otherwise.

Every antibody in the file is in scope, published or not: a correct product link
is worth having on a record with no public figure too.

SAFETY MODEL (same as the other write commands)
  * Dry-run by default; ``--apply`` writes, in one transaction.
  * Guarded to ``pipeline_db``. Take a Render export before ``--apply``
    (production-data skill).
  * After ``--apply`` the public pages' cache is purged, as a figure release
    does, since the gene pages, the API and the extension show both fields.

    python manage.py import_supplier_survey
    python manage.py import_supplier_survey --apply
"""
from __future__ import annotations

import csv
import os

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator
from django.db import transaction

DB = "pipeline_db"

DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "antibody_site_updates_2026_10_03.csv",
)

REQUIRED_COLUMNS = {
    "id", "catalogue_number", "out_of_market_now", "set_out_of_market",
    "current_supplier_url", "set_supplier_url", "clear_supplier_url",
    "evidence_type",
}

#: What each kind of evidence is worth *for a withdrawal*. ``None`` writes;
#: anything else is why the row is held back. A type nobody listed is held back
#: too, since a withdrawal takes a product off every public gene page.
WITHDRAWAL_EVIDENCE = {
    "on_page_statement": None,
    "catalogue_search_miss":
        "the supplier's catalogue search found nothing, which is not the "
        "supplier saying the product is withdrawn",
}

BOOLEAN = {"TRUE": True, "FALSE": False, "": None}

URL_MAX = 500
_valid_url = URLValidator(schemes=["http", "https"])


def _norm(value):
    """Compare catalogue numbers the way a person reads them off a label."""
    return (value or "").strip().casefold()


def _text(row, key):
    return (row.get(key) or "").strip()


def _survey_read(stored, read):
    """Did the survey see this stored link, as it recorded links?

    It recorded them in a reduced form, found on the first dry run against
    live (24 rows refused as "changed since" that nobody had touched): the
    query string and fragment were dropped — ``?srsltid=…`` tracking codes,
    ``?keyword=GTX133213`` on a search page — and a value that is not a web
    address at all (a bare ``PA5-54271`` typed into the link cell) was read
    as empty. No reading in the file carries a ``?``. So the stored value is
    reduced the same way before comparing; anything else that differs is a
    real edit since the survey and is still refused.
    """
    if stored == read:
        return True
    if "://" not in stored:
        return read == ""
    return "?" not in read and "#" not in read and (
        stored.split("#", 1)[0].split("?", 1)[0] == read)


class Command(BaseCommand):
    help = ("Apply the 3 Oct 2026 supplier survey to Antibody.out_of_market and "
            "Antibody.supplier_url. Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path", nargs="?", default=DEFAULT_CSV,
            help=f"Path to the survey CSV (default: {DEFAULT_CSV})")
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the changes (default is a dry run)")

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
                f"{path} is missing column(s): {', '.join(sorted(missing))}.")

        wanted = []
        for row in rows:
            try:
                wanted.append(int(_text(row, "id")))
            except ValueError:
                pass
        on_file = {
            ab.pk: ab
            for ab in Antibody.objects.using(DB).filter(pk__in=wanted)
            .only("pk", "catalogue_number", "out_of_market", "supplier_url")
        }

        withdraw, restore, link_set, link_clear = [], [], [], []
        held, already, moved, refused = [], [], [], []
        changes = {}  # pk -> (antibody, {field: value})

        def plan(antibody, field, value):
            changes.setdefault(antibody.pk, (antibody, {}))[1][field] = value

        for line_no, row in enumerate(rows, start=2):  # row 1 is the header
            raw_id = _text(row, "id")
            catalogue = _text(row, "catalogue_number")
            label = (f"{_text(row, 'gene') or '?'} · {catalogue or '(no catalogue)'}"
                     f" · {_text(row, 'company') or '?'} (id {raw_id or '?'})")
            try:
                pk = int(raw_id)
            except ValueError:
                refused.append(f"line {line_no}: id {raw_id!r} is not a record "
                               f"number")
                continue
            antibody = on_file.get(pk)
            if antibody is None:
                refused.append(f"{label}: no antibody with that record number")
                continue
            if _norm(antibody.catalogue_number) != _norm(catalogue):
                refused.append(
                    f"{label}: record {pk} is "
                    f"{antibody.catalogue_number or '(blank)'}, not {catalogue} "
                    f"— the file was built against a different database")
                continue

            flag_raw = _text(row, "set_out_of_market").upper()
            now_raw = _text(row, "out_of_market_now").upper()
            clear_raw = _text(row, "clear_supplier_url").upper()
            new_url = _text(row, "set_supplier_url")
            bad = [f"{col} {val!r}" for col, val in (
                ("set_out_of_market", flag_raw), ("out_of_market_now", now_raw),
                ("clear_supplier_url", clear_raw)) if val not in BOOLEAN]
            if bad:
                refused.append(f"{label}: {', '.join(bad)} is not TRUE, FALSE "
                               f"or blank")
                continue
            if new_url and BOOLEAN[clear_raw]:
                refused.append(f"{label}: asks to set the link and to clear it")
                continue

            # The discontinued flag.
            wants = BOOLEAN[flag_raw]
            if wants is not None:
                if antibody.out_of_market == wants:
                    already.append(f"{label}: already "
                                   f"{'discontinued' if wants else 'on sale'}")
                elif BOOLEAN[now_raw] is None or (
                        antibody.out_of_market != BOOLEAN[now_raw]):
                    moved.append(
                        f"{label}: discontinued flag is "
                        f"{antibody.out_of_market}, the survey read "
                        f"{now_raw or '(blank)'} — changed since, left alone")
                elif wants:
                    kind = _text(row, "evidence_type").lower()
                    why = WITHDRAWAL_EVIDENCE.get(
                        kind, f"evidence type {kind or '(blank)'!r} is not "
                              f"one this command knows")
                    if why:
                        held.append((label, why, _text(row, "evidence"),
                                     _text(row, "note")))
                    else:
                        withdraw.append((label, _text(row, "evidence")))
                        plan(antibody, "out_of_market", True)
                else:
                    restore.append((label, _text(row, "evidence")))
                    plan(antibody, "out_of_market", False)

            # The product link.
            if new_url or BOOLEAN[clear_raw]:
                target = new_url
                if new_url:
                    try:
                        _valid_url(new_url)
                        if len(new_url) > URL_MAX:
                            raise ValidationError("too long")
                    except ValidationError:
                        refused.append(f"{label}: {new_url!r} is not a usable "
                                       f"link (http/https, under {URL_MAX} "
                                       f"characters)")
                        continue
                stored = (antibody.supplier_url or "").strip()
                read = _text(row, "current_supplier_url")
                if stored == target:
                    already.append(f"{label}: link already "
                                   f"{'set' if target else 'empty'}")
                elif not _survey_read(stored, read):
                    moved.append(
                        f"{label}: link is {stored or '(empty)'}, the survey "
                        f"read {read or '(empty)'} — changed since, left alone")
                elif target:
                    link_set.append((label, read, stored, target))
                    plan(antibody, "supplier_url", target)
                else:
                    link_clear.append((label, stored, _text(row, "link_reason")))
                    plan(antibody, "supplier_url", "")

        w, style = self.stdout.write, self.style
        w(style.MIGRATE_HEADING(f"Supplier survey — {len(rows):,} row(s) from {path}"))

        w(style.MIGRATE_HEADING(f"\nNow discontinued ({len(withdraw)})"))
        for label, evidence in withdraw:
            w(f"  {label}\n      {evidence[:150]}")
        if not withdraw:
            w("  none")

        if held:
            w(style.WARNING(
                f"\nCalled discontinued, but not written — a withdrawal needs the "
                f"supplier's own statement ({len(held)})"))
            for label, why, evidence, note in held:
                w(f"  {label}: {why}")
                for extra in (evidence, note):
                    if extra:
                        w(f"      {extra[:200]}")
            w("  These stay on their gene pages; their link changes, if any, "
              "still apply.")

        w(style.MIGRATE_HEADING(f"\nBack on sale ({len(restore)})"))
        for label, evidence in restore:
            w(f"  {label}\n      {evidence[:150]}")
        if not restore:
            w("  none")

        # Added or replaced is the survey's call (its reading was empty or
        # not); what is printed as "was" is what the database holds now.
        added = [x for x in link_set if not x[1]]
        replaced = [x for x in link_set if x[1]]
        w(style.MIGRATE_HEADING(f"\nLink added ({len(added)})"))
        for label, _read, stored, new in added:
            w(f"  {label}")
            if stored:
                w(f"      was {stored} (not a web address)")
            w(f"      {new}")
        w(style.MIGRATE_HEADING(f"\nLink replaced ({len(replaced)})"))
        for label, _read, stored, new in replaced:
            w(f"  {label}\n      was {stored}\n      now {new}")
        w(style.MIGRATE_HEADING(f"\nLink removed ({len(link_clear)})"))
        for label, old, reason in link_clear:
            w(f"  {label}\n      was {old}")
            if reason:
                w(f"      {reason[:200]}")

        if already:
            w(f"\nAlready as the survey wants — nothing to write ({len(already)})")
            for line in already:
                w(f"  {line}")
        if moved:
            w(style.WARNING(
                f"\nChanged since the survey read it — left alone ({len(moved)})"))
            for line in moved:
                w(f"  {line}")
        if refused:
            w(style.ERROR(f"\nRefused ({len(refused)})"))
            for line in refused:
                w(f"  {line}")

        summary = (f"{len(withdraw)} discontinued, {len(restore)} back on sale, "
                   f"{len(added)} link(s) added, {len(replaced)} replaced, "
                   f"{len(link_clear)} removed, on {len(changes)} antibody(ies)")
        if not opts["apply"]:
            w(style.WARNING(
                f"\nDRY RUN — nothing written. Ready: {summary}."
                "\nTake a Render export, then re-run with --apply."))
            return

        with transaction.atomic(using=DB):
            for antibody, fields in changes.values():
                for field, value in fields.items():
                    setattr(antibody, field, value)
                antibody.save(using=DB,
                              update_fields=[*fields, "updated_at"])

        w(style.SUCCESS(f"\nApplied: {summary}."))

        from OGA_website import edge_cache
        _purged, note = edge_cache.purge_public_pages()
        w(note)
