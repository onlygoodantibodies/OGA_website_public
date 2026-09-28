"""Fill `added_by` / `saved_by` on rows saved before the stamp existed — from
evidence only.

    python manage.py backfill_saved_by            # dry run: what each rule would write
    python manage.py backfill_saved_by --apply

The stamps (`pipeline/saved_by.py`) went live on 27 Sep 2026; every row saved
before then is blank, which means *not recorded*. This fills a blank only where
something other than a guess says who saved the row, and each rule below names
that something. **A row with no evidence stays blank** — an invented name is
worse than a gap, because it reads as a fact.

The evidence is of three kinds, strongest first:

* **What the access log showed** (24 Sep 2026, DECISIONS.md "Who entered it"):
  the MCOLN1 session, its readings and the knockout line's edit came from the
  one browser that was refused with the message Mickey was sent that evening.
* **What a row carries and when it was written**: an Access id and a creation
  inside the minutes of a run the change log documents, with that run's own
  count (`DUPLICATE_ANTIBODY_FINDINGS.md` — 41 antibodies, 8 lines, 43 sessions
  and 392 readings on 20 Aug; 5,192 readings from the original import, which is
  CLAUDE.md's number). **A rule whose count differs from the documented one is
  printed and not applied**, since the difference means the window caught
  something the run did not write, or missed something it did.
* **A burst with one plausible writer**: the Leicester rows written in the same
  two minutes on 30 Mar, and the NR4A2 antibodies each created within half a
  second of a crop `hsv6` staged — the cropper creates a missing antibody at
  its save.

`saved_by` is filled only where the row has not been saved since it was
created (`updated_at` within a minute of `created_at`), since otherwise the
last save was somebody else's and nothing says whose. Readings have no
timestamps, so they get `added_by` only. Everything is written with
`QuerySet.update()`, which neither restamps `updated_at` nor calls the field's
own `pre_save` — otherwise this command would stamp itself over the evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_tz

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q

from pipeline import saved_by as stamp
from pipeline.models import (Antibody, CellLine, ExperimentSession, FcResult,
                             IfResult, IhcResult, IpResult,
                             PendingPublicationImage, WbResult)

DB = "pipeline_db"
UNTOUCHED = timedelta(minutes=1)


def _utc(*a):
    return datetime(*a, tzinfo=dt_tz.utc)


def _day(y, m, d):
    return Q(created_at__gte=_utc(y, m, d), created_at__lt=_utc(y, m, d) + timedelta(days=1))


def _session_day(y, m, d):
    return Q(session__created_at__gte=_utc(y, m, d),
             session__created_at__lt=_utc(y, m, d) + timedelta(days=1))


@dataclass
class Rule:
    who: str                    # the stamp: a username or command:<name>
    evidence: str
    model: type
    where: Q
    expected: int | None = None  # the documented count, where one exists
    saved_too: str = "untouched"  # "untouched", "always" or "never"
    added_too: bool = True
    extra: dict = field(default_factory=dict)

    def rows(self):
        return self.model.objects.using(DB).filter(self.where, **self.extra)


RULES = [
    # ── The original Access import, 29 Mar 2026 19:24–19:58 UTC ─────────────
    Rule("command:import_access_data",
         "Access id, written 29 Mar 2026 — the one-time Access import",
         Antibody, Q(access_id__isnull=False) & _day(2026, 3, 29), 2817),
    Rule("command:import_access_data",
         "Access id, written 29 Mar 2026 — the one-time Access import",
         CellLine, Q(access_id__isnull=False) & _day(2026, 3, 29), 524),
    Rule("command:import_access_data",
         "written 29 Mar 2026 19:55–19:58, the import's session pass",
         ExperimentSession, _day(2026, 3, 29), 434),
    *[Rule("command:import_access_data",
           "a reading in a session the Access import wrote (5,192 in all)",
           m, _session_day(2026, 3, 29), n, saved_too="never")
      for m, n in ((WbResult, 1912), (IpResult, 1641), (IfResult, 1639),
                   (FcResult, 0), (IhcResult, 0))],

    # ── The Leicester import, 30 Mar 2026 04:01–04:03 UTC ───────────────────
    Rule("command:import_leicester_data",
         "Leicester, written in the same two minutes of 30 Mar 2026",
         Antibody, Q(created_at__gte=_utc(2026, 3, 30, 4, 1),
                     created_at__lt=_utc(2026, 3, 30, 4, 3), site__name="Leicester"), 327),
    Rule("command:import_leicester_data",
         "Leicester, written in the same two minutes of 30 Mar 2026",
         CellLine, Q(created_at__gte=_utc(2026, 3, 30, 4, 1),
                     created_at__lt=_utc(2026, 3, 30, 4, 3), site__name="Leicester"), 34),

    # ── The Access delta, 20 Aug 2026 (DUPLICATE_ANTIBODY_FINDINGS.md) ──────
    Rule("command:import_access_update",
         "Access id, written 20 Aug 2026 — the documented 41 antibodies",
         Antibody, Q(access_id__isnull=False) & _day(2026, 8, 20), 41),
    Rule("command:import_access_update",
         "McGill, written 20 Aug 2026 17:21 — the documented 8 cell lines",
         CellLine, Q(site__name="McGill") & _day(2026, 8, 20), 8),
    Rule("command:import_access_update",
         "written 20 Aug 2026 17:21–17:48 — the documented 43 sessions",
         ExperimentSession, _day(2026, 8, 20), 43),
    *[Rule("command:import_access_update",
           "a reading in a session the Access delta wrote (392 in all)",
           m, _session_day(2026, 8, 20), n, saved_too="never")
      for m, n in ((WbResult, 124), (IpResult, 113), (IfResult, 155),
                   (FcResult, 0), (IhcResult, 0))],

    # ── Two repairs to the cell lines ───────────────────────────────────────
    Rule("command:restore_cell_line_clones",
         "Access id, written 4 Sep 2026 — the clones the Access merge lost",
         CellLine, Q(access_id__isnull=False) & _day(2026, 9, 4), 48),
    Rule("command:backfill_knockdown_lines",
         "genotype KD, written 18 Sep 2026 19:53 — the knockdown backfill",
         CellLine, Q(genotype="KD") & _day(2026, 9, 18), 22),

    # ── The cropper, 14 Aug 2026 ────────────────────────────────────────────
    Rule("hsv6",
         "NR4A2, 14 Aug 2026: each created within a second of a crop hsv6 "
         "staged — the cropper adds a missing antibody at its save",
         Antibody, _day(2026, 8, 14) & Q(Exists(
             PendingPublicationImage.objects.filter(
                 antibody_id=OuterRef("pk"), staged_by="hsv6",
                 created_at__gte=OuterRef("created_at"),
                 created_at__lt=OuterRef("created_at") + timedelta(seconds=2)))), 5),

    # ── The access log, 24 Sep 2026 (DECISIONS.md "Who entered it") ─────────
    Rule("mickey", "the MCOLN1 session planned from mickey's browser at 20:08",
         ExperimentSession, Q(pk=639), 1, saved_too="always"),
    Rule("mickey", "its readings, uploaded from the same browser at 21:21",
         WbResult, Q(session_id=639), 8, saved_too="always"),
    Rule("mickey", "the knockout's edit from the same browser at 21:22",
         CellLine, Q(pk=1585, updated_at__gte=_utc(2026, 9, 24, 21, 22),
                     updated_at__lt=_utc(2026, 9, 24, 21, 23)), 1,
         saved_too="always", added_too=False),
]


class Command(BaseCommand):
    help = "Fill blank added_by/saved_by from evidence (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write. Without it nothing is changed.")

    def handle(self, *args, apply=False, **opts):
        plans, skipped = [], []
        for rule in RULES:
            rows = rule.rows()
            total = rows.count()
            add = rows.filter(added_by="") if rule.added_too else rows.none()
            save = rows.filter(saved_by="")
            if rule.saved_too == "never":
                save = save.none()
            elif rule.saved_too == "untouched":
                save = save.filter(updated_at__lt=F("created_at") + UNTOUCHED)
            n_add, n_save = add.count(), save.count()
            ok = rule.expected is None or total == rule.expected
            mark = "" if ok else f"  ✗ documented {rule.expected} — NOT applied"
            self.stdout.write(
                f"{rule.model.__name__:<18} {total:>5} rows  "
                f"+added_by {n_add:>5}  +saved_by {n_save:>5}  "
                f"→ {stamp.who(rule.who)}\n    {rule.evidence}{mark}")
            (plans if ok else skipped).append((rule, add, save, n_add, n_save))

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing written. Re-run with --apply."))
            return
        written = 0
        with transaction.atomic(using=DB):
            for rule, add, save, _, _ in plans:
                # Each queryset filters on its own column being blank, so the
                # two updates are independent of each other's order.
                written += save.update(saved_by=rule.who)
                written += add.update(added_by=rule.who)
        self.stdout.write(self.style.SUCCESS(
            f"\nWrote {written} stamp(s). {len(skipped)} rule(s) held back "
            "because their count differs from the documented one."))
