"""Merge two Site rows that are the same real place.

"McGill" and "Montreal" are one lab, entered under two names: the Access import
used one and everything since has used the other. The consequences are visible
on every screen that groups by site — Overview reports that lab twice (152 active
targets with zero antibodies alongside 139 with 2857), the site filter on all
four boards splits its records, and a sheet uploaded with the wrong one of the
two names lands somewhere that looks empty.

Renaming one row does not fix any of that: there would still be two rows. This
moves every record onto one of them and deletes the other.

Dry-run by default, as every writing command here is. Nothing moves without
``--apply``, and even then it is one transaction: it all lands or none of it
does.

    python manage.py merge_sites --from Montreal --into McGill
    python manage.py merge_sites --from Montreal --into McGill --apply

Two things it deliberately will not do.

It finds the foreign keys **by introspection** rather than from a list, so a
model added later cannot be quietly left behind pointing at a site that no
longer exists. There are fifteen of them today and a hand-written list would be
wrong within a month.

And it never merges records. Two constraints include the site
(``unique_antibody_per_site_lot`` and ``unique_assignment_per_site_task``), so if
both sites hold the same vial of the same antibody, moving one onto the other
would break the constraint. Those rows are reported and left where they are —
deciding that two rows are one antibody is ``merge_duplicate_antibodies``'s job,
with its own dry run and its own evidence. Run that, then run this again.
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from pipeline.models import Site

DB = "pipeline_db"

# The two constraints that include the site. Moving a row onto a site that
# already holds its twin would violate these, so those rows are held back.
GUARDED = {
    "Antibody": ("catalogue_number", "company_id", "target_id", "lot_number"),
    "TargetAssignment": ("target_id", "task_type"),
}


def _site_fks():
    """Every (model, field name) pointing at Site, found rather than listed."""
    out = []
    for model in apps.get_app_config("pipeline").get_models():
        for field in model._meta.get_fields():
            if getattr(field, "many_to_one", False) and field.related_model is Site:
                out.append((model, field.name))
    return sorted(out, key=lambda mf: mf[0].__name__)


def _collisions(model, field, loser, winner):
    """Rows on the loser whose twin already sits on the winner.

    Only for the two guarded models; everything else can move freely.
    """
    keys = GUARDED.get(model.__name__)
    if not keys:
        return set()
    winner_keys = set(
        model.objects.using(DB).filter(**{field: winner}).values_list(*keys))
    if not winner_keys:
        return set()
    clash = set()
    for row in model.objects.using(DB).filter(**{field: loser}).values_list("pk", *keys):
        if tuple(row[1:]) in winner_keys:
            clash.add(row[0])
    return clash


class Command(BaseCommand):
    help = "Move every record from one Site onto another, then delete the empty one."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="loser", required=True,
                            help="the site name that goes away")
        parser.add_argument("--into", dest="winner", required=True,
                            help="the site name that survives")
        parser.add_argument("--apply", action="store_true",
                            help="actually move the records (default is a dry run)")
        parser.add_argument("--keep-empty-site", action="store_true",
                            help="move the records but leave the empty Site row")

    def handle(self, *args, **opts):
        loser = self._site(opts["loser"])
        winner = self._site(opts["winner"])
        if loser.pk == winner.pk:
            raise CommandError("--from and --into are the same site")

        apply = opts["apply"]
        self.stdout.write(
            f"\n{'MOVING' if apply else 'DRY RUN — nothing will change'}: "
            f"'{loser.name}' → '{winner.name}'\n")

        plan, held, total = [], [], 0
        for model, field in _site_fks():
            on_loser = model.objects.using(DB).filter(**{field: loser})
            count = on_loser.count()
            if not count:
                continue
            clash = _collisions(model, field, loser, winner)
            movable = count - len(clash)
            total += movable
            plan.append((model, field, movable, clash))
            label = model._meta.verbose_name_plural.title()
            self.stdout.write(f"  {movable:>6}  {label}")
            if clash:
                held.append((model, clash))
                self.stdout.write(self.style.WARNING(
                    f"  {len(clash):>6}  {label} held back — "
                    f"'{winner.name}' already has the same record"))

        if not total and not held:
            self.stdout.write(self.style.SUCCESS(
                f"\nNothing is recorded against '{loser.name}'. "
                f"{'Deleting it.' if apply else 'It could be deleted.'}"))

        for model, clash in held:
            self.stdout.write(self.style.WARNING(
                f"\n{model.__name__} rows staying on '{loser.name}' "
                f"(ids {sorted(clash)[:20]}{'…' if len(clash) > 20 else ''})"))
            self.stdout.write(
                "  These already exist at the surviving site. Deciding that two "
                "rows are one record is a merge, not a move — run\n"
                "  merge_duplicate_antibodies (dry run first), then run this again.")

        if not apply:
            self.stdout.write(self.style.NOTICE(
                f"\nDry run. {total} record(s) would move. "
                f"Re-run with --apply to do it."))
            return

        with transaction.atomic(using=DB):
            for model, field, _movable, clash in plan:
                qs = model.objects.using(DB).filter(**{field: loser})
                if clash:
                    qs = qs.exclude(pk__in=clash)
                qs.update(**{f"{field}_id": winner.pk})
            still_there = any(
                model.objects.using(DB).filter(**{field: loser}).exists()
                for model, field in _site_fks())
            if still_there:
                self.stdout.write(self.style.WARNING(
                    f"\nMoved {total}. '{loser.name}' still holds the held-back "
                    f"records, so it has been left in place."))
            elif opts["keep_empty_site"]:
                self.stdout.write(self.style.SUCCESS(
                    f"\nMoved {total}. '{loser.name}' is empty and was kept."))
            else:
                loser.delete(using=DB)
                self.stdout.write(self.style.SUCCESS(
                    f"\nMoved {total}. '{loser.name}' is empty and has been deleted."))

        self.stdout.write(
            "\nWorth checking now: Overview should show one row for this lab, "
            "and the site filter on each board should list it once.")

    def _site(self, name):
        site = Site.objects.using(DB).filter(
            Q(name__iexact=name) | Q(short_code__iexact=name)).first()
        if site is None:
            known = ", ".join(Site.objects.using(DB).order_by("name")
                              .values_list("name", flat=True))
            raise CommandError(f"no site called '{name}'. There is: {known}")
        return site
