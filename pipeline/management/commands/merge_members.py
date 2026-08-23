"""Merge two Member rows that are the same person.

Carl Laflamme has two. So the "who ran it" dropdown offers him twice, his
sessions are split across two identities, and the August 2026 Access delta
refused to file an experiment against `MembersID 2` at all rather than guess
which of them ran it — which is the right refusal and is also the symptom that
found this.

A person reaches this database by two routes. `import_access_data` minted a
`Member` (and a login called `access_<name>`) for each of the twenty-four people
in the Access table, and nine of those have since been given real accounts
through `services/members.py::grant`. Where both happened, there are two rows
for one scientist, and nothing on any screen says they are the same person.

    python manage.py merge_members --from 38 --into 58
    python manage.py merge_members --from 38 --into 58 --apply
    python manage.py merge_members --name "Carl Laflamme"      # picks the survivor

Dry-run by default, like every writing command here, and one transaction when
it does write: it all lands or none of it does.

**Which row survives is the account that can still sign in.** With `--name`,
that is chosen for you: the row whose login is a superuser, or failing that the
one that is not an `access_` import. A merge that kept the imported row would
move a scientist's work onto an identity with a deliberately unusable password,
which reads as working right up until they try to log in.

Two things it will not do.

It finds the foreign keys **by introspection** rather than from a list, so a
model added later cannot be left behind pointing at a member who is gone. There
are seven today and a hand-written list would be wrong within a month.

And it **never deletes the losing row by default** — it deactivates it. The
Access import's own people are hidden rather than deleted precisely because
sessions point at them, and a `Member` is half of somebody's access: deleting
one removes a person's route into the pipeline while leaving their login intact,
which is the confusing half of "a working account is three rows in two
databases". `--delete-empty` removes it once nothing points at it.
"""
from __future__ import annotations

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.services import members as members_svc
from pipeline.models import Member

DB = "pipeline_db"


def _member_fks():
    """Every (model, field name) pointing at Member, found rather than listed."""
    out = []
    for model in apps.get_app_config("pipeline").get_models():
        for field in model._meta.get_fields():
            if getattr(field, "many_to_one", False) and field.related_model is Member:
                out.append((model, field.name))
    return sorted(out, key=lambda mf: (mf[0].__name__, mf[1]))


class Command(BaseCommand):
    help = ("Move every record from one Member onto another, then deactivate "
            "the empty one. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="loser", help="the member id that goes away")
        parser.add_argument("--into", dest="winner", help="the member id that survives")
        parser.add_argument("--name", help="merge every member with this display "
                                           "name, choosing the survivor")
        parser.add_argument("--apply", action="store_true",
                            help="actually move the records (default is a dry run)")
        parser.add_argument("--delete-empty", action="store_true",
                            help="delete the losing Member row instead of "
                                 "deactivating it, once nothing points at it")

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        if opts["name"]:
            loser, winner = self._by_name(opts["name"])
        elif opts["loser"] and opts["winner"]:
            loser = self._by_id(opts["loser"])
            winner = self._by_id(opts["winner"])
        else:
            raise CommandError(
                "Give either --name, or both --from and --into. "
                "`--name \"Carl Laflamme\"` picks the survivor for you.")

        if loser.pk == winner.pk:
            raise CommandError("--from and --into are the same member")

        apply = opts["apply"]
        self.stdout.write(
            f"\n{'MOVING' if apply else 'DRY RUN — nothing will change'}: "
            f"{self._label(loser)} → {self._label(winner)}\n")

        if loser.site_id != winner.site_id:
            # Worth saying out loud: `Member.site` is the bench a person works
            # at, and it moves with them. It is **not** where their past work
            # is filed — `ExperimentSession` carries its own site — so nothing
            # already recorded changes hands. What does change is what this
            # person may delete as an ordinary member (`deletion.site_refusal`)
            # and which bench's list they land on.
            self.stdout.write(self.style.WARNING(
                f"  These two rows are at different benches: "
                f"{self._site_name(loser)} and {self._site_name(winner)}. "
                f"The survivor keeps {self._site_name(winner)}.\n"
                f"  Past sessions keep their own site and do not move.\n"))

        plan, total = [], 0
        for model, field in _member_fks():
            count = model.objects.using(DB).filter(**{field: loser}).count()
            if not count:
                continue
            total += count
            plan.append((model, field, count))
            self.stdout.write(
                f"  {count:>6}  {model._meta.verbose_name_plural.title()} ({field})")

        if not total:
            self.stdout.write(
                f"  Nothing is recorded against {self._label(loser)}.")

        if not apply:
            self.stdout.write(self.style.NOTICE(
                f"\nDry run. {total} record(s) would move onto "
                f"{self._label(winner)}, and the other row would be "
                f"{'deleted' if opts['delete_empty'] else 'deactivated'}. "
                f"Re-run with --apply to do it."))
            return

        with transaction.atomic(using=DB):
            for model, field, _count in plan:
                (model.objects.using(DB).filter(**{field: loser})
                 .update(**{f"{field}_id": winner.pk}))

            still_there = [
                f"{model.__name__}.{field}"
                for model, field in _member_fks()
                if model.objects.using(DB).filter(**{field: loser}).exists()
            ]
            if still_there:
                # Should not happen — but saying so is better than reporting a
                # clean merge over rows that did not move.
                self.stdout.write(self.style.WARNING(
                    f"\nMoved {total}, but {', '.join(still_there)} still points "
                    f"at {self._label(loser)}. Left it in place."))
            elif opts["delete_empty"]:
                loser.delete(using=DB)
                self.stdout.write(self.style.SUCCESS(
                    f"\nMoved {total}. The duplicate row has been deleted."))
            else:
                loser.is_active = False
                loser.save(using=DB, update_fields=["is_active"])
                self.stdout.write(self.style.SUCCESS(
                    f"\nMoved {total}. The duplicate row is deactivated, so it "
                    f"is out of every experimenter list. Its login is untouched "
                    f"— delete it on the people board if it should go entirely."))

        self.stdout.write(
            "\nWorth checking now: the experimenter dropdown should list this "
            "person once, and their sessions should all be under one name.")

    # ------------------------------------------------------------------
    def _by_name(self, name):
        """The two rows for one person, and which of them survives.

        The survivor is the account that can still sign in: a superuser first,
        then any row that is not an `access_` import. Keeping the imported row
        would move a scientist's work onto a login with a deliberately unusable
        password.
        """
        rows = list(Member.objects.using(DB).filter(display_name__iexact=name))
        if len(rows) < 2:
            raise CommandError(
                f"There is {'no' if not rows else 'only one'} member called "
                f"'{name}', so there is nothing to merge.")
        if len(rows) > 2:
            raise CommandError(
                f"{len(rows)} members are called '{name}' "
                f"(ids {sorted(r.pk for r in rows)}). Merge them a pair at a "
                f"time with --from and --into, so each step is one decision.")

        supers = members_svc._superuser_usernames()
        usernames = {r.pk: self._username(r) for r in rows}

        def rank(row):
            username = usernames[row.pk]
            return (username in supers,
                    not username.startswith(members_svc.IMPORTED_PREFIX))

        winner, loser = sorted(rows, key=rank, reverse=True)
        if rank(winner) == rank(loser):
            raise CommandError(
                f"Both members called '{name}' look the same "
                f"(ids {loser.pk} and {winner.pk}) — neither is a superuser and "
                f"neither is an import, so which one survives is your call. "
                f"Say so with --from and --into.")
        self.stdout.write(
            f"  Survivor chosen: member {winner.pk} "
            f"({usernames[winner.pk] or 'no login'}"
            + (", superuser" if usernames[winner.pk] in supers else "") + ")")
        return loser, winner

    def _by_id(self, value):
        try:
            return Member.objects.using(DB).get(pk=int(value))
        except (ValueError, Member.DoesNotExist):
            raise CommandError(f"No member with id {value!r}.")

    @staticmethod
    def _username(member):
        user = User.objects.using(DB).filter(pk=member.user_id).first()
        return user.username if user else ""

    @staticmethod
    def _site_name(member):
        site = getattr(member, "site", None)
        return site.name if site else "no bench"

    def _label(self, member):
        username = self._username(member)
        return (f"member {member.pk} "
                f"({member.display_name or 'no name'}, "
                f"{username or 'no login'}, {self._site_name(member)})")
