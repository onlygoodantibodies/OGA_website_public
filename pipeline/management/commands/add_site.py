"""Add a YCharOS site.

There was no way to create a `Site` from anywhere — not the people board, not
the boards, not a command — so the list of institutions was whatever the Access
import left behind. That surfaced as a bug report about something else entirely.
Carl Laflamme wrote: *"McGill is included by default when adding a new target
entry (ideally there would be a drop down menu with McGill, Leicester, uOttawa,
UBC, Cornell, etc)"* — and it was not McGill. He and Riham Ayoubi had moved to
uOttawa, which was not on file at all, so their Member rows still said Montreal
and every target they added was nominated there. From outside, that is an app
filing your work under a site you did not choose.

    python manage.py add_site --name uOttawa --code UOT
    python manage.py add_site --name uOttawa --code UOT --apply

Dry-run by default, like every writing command here.

**It creates the site and stops there.** Who works where is the people board's
job (`/pipeline/users/board/`, superusers only), which already sets a member's
site, shows both halves of their account and guards against locking yourself
out. A second way to do the same thing is how two surfaces end up disagreeing,
which is a failure this codebase has paid for more than once.

Nor does it move records. A Member's `site` says where somebody works now; the
antibodies, cell lines and sessions already entered keep the site they were
entered under, because those say where the *reagent* is and where the
*experiment happened* — neither of which changes when a person moves lab.
Moving records between sites is ``merge_sites``, which has its own dry run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.models import Site

DB = "pipeline_db"


class Command(BaseCommand):
    help = "Create a YCharOS site. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True,
                            help="The site's name, as it should appear on screen")
        parser.add_argument("--code", default="",
                            help="Short code (e.g. UOT). Derived from the name if omitted.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually write. Without this, nothing changes.")

    def handle(self, *args, **opts):
        name = (opts["name"] or "").strip()
        if not name:
            raise CommandError("--name cannot be blank")
        code = (opts["code"] or name[:3]).strip().upper()[:10]

        existing = Site.objects.using(DB).filter(name__iexact=name).first()
        if existing:
            self.stdout.write(
                f"'{existing.name}' ({existing.short_code}) is already on file, "
                f"id {existing.pk}. Nothing to do.")
            return

        clash = Site.objects.using(DB).filter(short_code__iexact=code).first()
        if clash:
            raise CommandError(
                f"Short code '{code}' already belongs to {clash.name}. "
                f"Pass a different --code.")

        on_file = ", ".join(Site.objects.using(DB).order_by("name")
                            .values_list("name", flat=True))
        self.stdout.write(f"On file: {on_file}")
        self.stdout.write(f"WOULD CREATE '{name}' ({code})")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing was written. Re-run with --apply, after "
                "taking an export from Render → ycharos-pipeline-db → Recovery."))
            return

        with transaction.atomic(using=DB):
            site = Site.objects.using(DB).create(
                name=name, short_code=code, is_active=True)
        self.stdout.write(self.style.SUCCESS(
            f"Created '{site.name}' ({site.short_code}), id {site.pk}.\n"
            f"Put people on it from the people board — /pipeline/users/board/ — "
            f"which is where site, role and access are set."))
