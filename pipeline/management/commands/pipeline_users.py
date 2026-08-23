"""
Manage pipeline logins across the two databases.

A working pipeline login needs, in sync:
  - a login user in `academy_db` (Django auth checks the password here),
  - a matching user (same username) in `pipeline_db`, and
  - an active `Member` row in `pipeline_db` (site + role).

This command keeps those in step so access is created/reviewed correctly.

  python manage.py pipeline_users list
  python manage.py pipeline_users add --username jsmith --email j@lab.org \\
        --first-name Jo --last-name Smith --site MTL --role experimenter
  python manage.py pipeline_users deactivate --username jsmith
  python manage.py pipeline_users reactivate --username jsmith

`add` prints a temporary password for new logins — share it securely; the person
should change it after first sign-in. Run it in the Render shell (that's the only
place the production databases are reachable).
"""
import secrets

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from pipeline.models import Member, Site
from pipeline.services import members as member_svc

ACADEMY = "academy_db"   # login lives here (password auth)
PIPELINE = "pipeline_db"  # Member + the matching user live here


class Command(BaseCommand):
    help = "Review and manage pipeline logins (academy_db login + pipeline_db Member)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["list", "add", "deactivate", "reactivate"])
        parser.add_argument("--username")
        parser.add_argument("--email", default="")
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")
        parser.add_argument("--display-name", default="")
        parser.add_argument("--from-username",
                            help="Rename an existing pipeline user (e.g. an Access-import 'access_*' "
                                 "account) to --username first, so its Member row is reused instead of "
                                 "creating a duplicate. Use this to attach a login to an imported member.")
        parser.add_argument("--site", help="Site short code (e.g. MTL) or name")
        parser.add_argument("--role", help=f"One of: {', '.join(r for r, _ in Member.Role.choices)}")
        parser.add_argument("--password", help="Set an explicit login password (else a temp one is generated for new logins)")
        parser.add_argument("--reset-password", action="store_true", help="Reset the login password of an existing user")
        parser.add_argument("--superuser", action="store_true", help="Grant Django superuser/staff on the login account")

    def handle(self, *args, **o):
        action = o["action"]
        if action == "list":
            return self._list()
        if not o.get("username"):
            raise CommandError("--username is required for this action.")
        if action == "add":
            return self._add(o)
        if action in ("deactivate", "reactivate"):
            return self._set_active(o["username"], action == "reactivate")

    # ── review ────────────────────────────────────────────────────────────────
    def _list(self):
        members = Member.objects.using(PIPELINE).select_related("site").order_by("site__short_code")
        if not members:
            self.stdout.write("No Members found.")
            return
        self.stdout.write(f"{'USERNAME':30} {'NAME':22} {'ROLE':13} {'SITE':6} {'ACTIVE':7} {'LOGIN':11} SUPER")
        self.stdout.write("-" * 102)
        for m in members:
            pu = User.objects.using(PIPELINE).filter(pk=m.user_id).first()
            username = pu.username if pu else "(no pipeline user)"
            au = User.objects.using(ACADEMY).filter(username=username).first() if pu else None
            if au is None:
                login = "MISSING"
            elif not au.has_usable_password():
                login = "no password"
            else:
                login = "ok"
            name = (m.display_name or (au.get_full_name() if au else "") or "").strip()
            self.stdout.write(
                f"{username:30} {name:22.22} {m.role:13} "
                f"{(m.site.short_code if m.site else '—'):6} "
                f"{('yes' if m.is_active else 'NO'):7} {login:11} "
                f"{'yes' if (au and au.is_superuser) else ''}")
        self.stdout.write("-" * 92)
        self.stdout.write("LOGIN=MISSING means no academy_db account → the person cannot sign in.")

    # ── create / update access ─────────────────────────────────────────────────
    def _add(self, o):
        username = o["username"].strip()
        role = (o.get("role") or "").strip()
        if role not in dict(Member.Role.choices):
            raise CommandError(f"--role must be one of: {', '.join(r for r, _ in Member.Role.choices)}")
        if not o.get("site"):
            raise CommandError("--site is required (short code or name).")
        s = (o["site"] or "").strip()
        site = (Site.objects.using(PIPELINE).filter(short_code__iexact=s).first()
                or Site.objects.using(PIPELINE).filter(name__iexact=s).first())
        if site is None:
            avail = ", ".join(f"{x.short_code} ({x.name})" for x in Site.objects.using(PIPELINE).all())
            raise CommandError(f"No site matches '{s}'. Available: {avail or '(none)'}")

        temp_pw = None

        # 0) optionally relink an existing (e.g. Access-import) pipeline user to this
        #    username, so its Member row is reused rather than duplicated.
        from_username = (o.get("from_username") or "").strip()
        if from_username and from_username != username:
            old = User.objects.using(PIPELINE).filter(username=from_username).first()
            if old is None:
                raise CommandError(f"--from-username '{from_username}' not found in pipeline_db.")
            clash = User.objects.using(PIPELINE).filter(username=username).exclude(pk=old.pk).first()
            if clash is not None:
                raise CommandError(
                    f"Cannot rename to '{username}': that username is already taken in pipeline_db.")
            if not Member.objects.using(PIPELINE).filter(user_id=old.pk).exists():
                raise CommandError(
                    f"'{from_username}' has no Member row to reuse — add without --from-username instead.")
            old.username = username
            old.save(using=PIPELINE)
            self.stdout.write(f"  Relinked existing pipeline record '{from_username}' → '{username}'.")

        # 1-3) The three records, through the one write path the people board
        # uses too (services/members.py::grant). This used to be written out
        # here, and a second implementation of "make a login work" is exactly
        # how the three rows drift apart.
        existed = User.objects.using(ACADEMY).filter(username=username).exists()
        out = member_svc.grant([{
            "username": username, "site": o["site"], "role": role,
            "email": o["email"], "first_name": o["first_name"],
            "last_name": o["last_name"], "display_name": o.get("display_name", ""),
        }])
        for bad in out["skipped"]:
            raise CommandError(bad["why"])
        for made in out["passwords"]:
            temp_pw = made["password"]
        created_m = bool(out["created"])

        # Flags and an explicit password stay here: they are the command's own
        # options, not part of what it means to have access.
        au = User.objects.using(ACADEMY).get(username=username)
        pu = User.objects.using(PIPELINE).get(username=username)
        if o.get("reset_password") or (o.get("password") and not existed):
            temp_pw = o.get("password") or secrets.token_urlsafe(9)
            au.set_password(temp_pw)
        if o.get("superuser"):
            au.is_superuser = au.is_staff = True
            pu.is_superuser = pu.is_staff = True
            pu.save(using=PIPELINE, update_fields=["is_superuser", "is_staff"])
        au.save(using=ACADEMY)
        m = Member.objects.using(PIPELINE).get(user_id=pu.pk)

        verb = "Created" if (not existed or created_m) else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} pipeline access for '{username}' — {dict(Member.Role.choices)[role]} @ {site.short_code}."))
        if temp_pw:
            self.stdout.write(self.style.WARNING(
                f"  Temporary password: {temp_pw}\n"
                f"  Share this securely; have them change it after first sign-in."))
        else:
            self.stdout.write("  (Existing login password left unchanged — use --reset-password to reset it.)")

    def _set_active(self, username, active):
        pu = User.objects.using(PIPELINE).filter(username=username).first()
        m = Member.objects.using(PIPELINE).filter(user_id=pu.pk).first() if pu else None
        if m is None:
            raise CommandError(f"No pipeline Member found for '{username}'.")
        m.is_active = active
        m.save(using=PIPELINE)
        self.stdout.write(self.style.SUCCESS(
            f"{'Reactivated' if active else 'Deactivated'} pipeline access for '{username}'. "
            f"{'' if active else '(Login account kept; revoke it separately if needed.)'}"))
