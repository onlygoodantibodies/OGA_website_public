"""Apply a reviewed file of western blot judgements to the published antibodies.

    python manage.py apply_wb_judgements pipeline/data/wb_judgements_2026_10_03.json
    python manage.py apply_wb_judgements pipeline/data/wb_judgements_2026_10_03.json --apply

The rules are the owner's (version 4, 3 Oct 2026), written out with the
decisions and a measurement prototype in the ``wb-rating`` skill and drawn in
``bin/wb_rating/guide.html``. In short, in order: nothing lost in the KO (a
band that shifts smaller counts as lost) → Not supportive; the lost band
drowned among off-target bands, or directly beside a clearly stronger band
that stays, or only ambiguously reduced → Not supportive; only faint bands
stay → Supportive; clear bands or a clear KO residual stay → Supportive, but
not selective. A knockdown's residual target band is expected and ignored.
``wb_judgements_2026_10_03.json`` was decided before version 4 (it has no
faint-band rule), so re-check it against the guide before running it.

Each result is three stored values, the same three a person sets on Judge
outcomes: the recommendation flag and the two axes. ``outcomes.record`` writes
the axes and ``Antibody.save`` the flag, so every change lands in the
judgement history (``judgement_log``) as made by this command.

**Why a command and not the page.** Only a superuser may change another site's
antibody (``services/ownership.py``), and a reviewed batch spans every site; a
command is outside a request, so the rule does not apply, and one transaction
means a batch lands whole or not at all.

**A row is applied only where it still says what the file says it said.** Each
row carries ``was``; a row whose live values differ from it was changed by
somebody since the review, and a reviewed file is not the authority over that,
so it is refused by name and left alone. A row already at its result is
counted, not rewritten. Dry-run by default.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline import saved_by
from pipeline.models import Antibody
from pipeline.services import outcomes as outcome_svc

DB = "pipeline_db"
APP = "WB"

#: What each result stores: (recommended, detects, selective).
RESULTS = {
    "supportive": (True, "yes", "yes"),
    "supportive_not_selective": (True, "yes", "no"),
    "not_supportive": (False, "no", "no"),
}
WORDS = {
    "supportive": "Supportive",
    "supportive_not_selective": "Supportive, but not selective",
    "not_supportive": "Not supportive",
}


def _state(ab, axes):
    value = lambda axis: ((axes or {}).get(axis) or {}).get("value") or ""
    return {"recommended": bool(getattr(ab, outcome_svc.REC_FIELD[APP])),
            "detects": value("detects"), "selective": value("selective")}


def plan(rows):
    """``(changes, already, refused)`` — read only, nothing written."""
    ids = [r.get("antibody_id") for r in rows]
    abs_ = {a.pk: a for a in Antibody.objects.using(DB)
            .select_related("target").filter(pk__in=ids)}
    axes_by_gene = {}
    changes, already, refused = [], [], []
    for r in rows:
        label = f"{r.get('gene', '?')} {r.get('catalogue', '?')}"
        result = r.get("result")
        if result not in RESULTS:
            refused.append((label, f"result “{result}” is not one of "
                                   f"{', '.join(RESULTS)}"))
            continue
        ab = abs_.get(r.get("antibody_id"))
        if ab is None:
            refused.append((label, f"no antibody with id {r.get('antibody_id')}"))
            continue
        gene = ab.target.gene_name if ab.target_id else ""
        if (ab.catalogue_number, gene) != (r.get("catalogue"), r.get("gene")):
            refused.append((label, f"id {ab.pk} is {gene} {ab.catalogue_number} "
                                   "on file, not what the file names"))
            continue
        if ab.target_id not in axes_by_gene:
            axes_by_gene[ab.target_id] = outcome_svc.for_gene(ab.target_id, APP)
        axes = axes_by_gene[ab.target_id]
        if ab.pk not in axes:
            refused.append((label, "has no published western blot figure"))
            continue
        now = _state(ab, axes[ab.pk])
        rec, det, sel = RESULTS[result]
        want = {"recommended": rec, "detects": det, "selective": sel}
        if now == want:
            already.append(label)
            continue
        was = r.get("was")
        if was is not None and now != was:
            refused.append((label, f"changed since the review: the file says "
                                   f"{was}, live says {now}"))
            continue
        changes.append((ab, result, now, want))
    return changes, already, refused


class Command(BaseCommand):
    help = ("Apply a reviewed file of western blot judgements. "
            "Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("path", help="The JSON file of judgements.")
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes. Without this, nothing is written.")

    def handle(self, *args, **options):
        path = Path(options["path"])
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f"Could not read {path}: {exc}")
        if doc.get("application", APP) != APP:
            raise CommandError("This command applies western blot judgements only.")
        rows = doc.get("judgements") or []
        if not rows:
            raise CommandError(f"{path} lists no judgements.")

        changes, already, refused = plan(rows)
        self.stdout.write(f"{len(rows)} judgement(s) in {path.name}"
                          + (f", decided by {doc['decided_by']}" if doc.get("decided_by") else "")
                          + ":")
        moves = Counter()
        for ab, result, now, want in changes:
            moves[result] += 1
            gene = ab.target.gene_name if ab.target_id else ""
            self.stdout.write(f"  {gene:<10} {ab.catalogue_number:<24} -> {WORDS[result]}")
        self.stdout.write(f"\n  will change               {len(changes)}")
        for result, n in moves.most_common():
            self.stdout.write(f"    to {WORDS[result]:<28} {n}")
        self.stdout.write(f"  already as the file says  {len(already)}")
        if refused:
            self.stdout.write(self.style.WARNING(f"  refused                   {len(refused)}"))
            for label, why in refused:
                self.stdout.write(self.style.WARNING(f"    {label}: {why}"))

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing was changed. Re-run with --apply to do it."))
            return
        if not changes:
            self.stdout.write("\nNothing to change.")
            return

        actor = saved_by.current() or "command:apply_wb_judgements"
        with transaction.atomic(using=DB):
            for ab, result, now, want in changes:
                for axis in ("detects", "selective"):
                    if now[axis] != want[axis]:
                        # note=None keeps whatever note the judgement carries.
                        outcome_svc.record(ab.pk, APP, axis, want[axis],
                                           actor=actor, note=None)
                if now["recommended"] != want["recommended"]:
                    setattr(ab, outcome_svc.REC_FIELD[APP], want["recommended"])
                    ab.save(using=DB)
        self.stdout.write(self.style.SUCCESS(f"\nChanged {len(changes)} judgement(s)."))

        # A judgement moves the gene page, the API and the extension index; the
        # edge holds public pages for a week (CLAUDE.md, Deploy).
        from OGA_website import edge_cache
        purged, note = edge_cache.purge_public_pages()
        self.stdout.write((self.style.SUCCESS if purged else self.style.WARNING)(
            "\n" + note))
