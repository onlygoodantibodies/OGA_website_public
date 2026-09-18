"""Pipeline people: the one place that keeps a login and its access in step.

**A working pipeline login is three rows in two databases**, and nothing on any
screen showed all three until this module existed:

  1. ``academy_db.auth_user``   — the login. The password lives here, and this is
     the only row Django admin edits.
  2. ``pipeline_db.auth_user``  — a second user row, matched to the first by
     **username**, deliberately carrying an unusable password.
  3. ``pipeline_db.pipeline_member`` — the access: site, role, ``is_active``.

``pipeline_member_required`` gates on 2 and 3; ``authenticate()`` reads 1. So a
person can sign in perfectly and be refused every pipeline page, or hold a
flawless Member row and be unable to sign in at all — and the two failures look
identical from the outside ("it doesn't work").

That is not hypothetical. On 3 Aug 2026 a password change made in Django admin
"registered as though it had worked" and did not: the admin's user change form
carries the password as a **read-only hash** with a separate button, so the email
half of the edit saved and the password half was never submitted. Nothing showed
that ``check_password`` was still false, that ``is_staff`` was False on a
superuser, or that the same person held a second Member row under an
Access-import username. Reading it took a shell session.

So the rules this module holds:

* **One write path, two doors.** ``manage.py pipeline_users`` and the
  user-management board both call these functions. A second implementation of
  "make a login work" is how the three rows drift, which is the whole defect.
* **Never write one row of the three alone.** ``grant`` writes all of them or
  none.
* **A privileged surface refuses to let you lock yourself out.** You cannot drop
  your own superuser flag, deactivate yourself, or remove the last superuser —
  and each refusal says which rule stopped it, not "could not save that".
"""
from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.db import transaction

from pipeline.services import board_page as board_page_svc
from pipeline.models import Member, Site

DB = "pipeline_db"
ACADEMY = "academy_db"

# The Access import gave every person it carried across a username of its own,
# because there was no login to match them to. Fifteen of the twenty-four rows
# on this board are those, and none of them is somebody who signs in — but they
# are attached to real historical sessions, so they cannot be deleted and must
# not be silently dropped either. Hidden by default, counted where they are
# hidden, and one tick brings them back.
IMPORTED_PREFIX = "access_"

# A generated password is shown once and never stored anywhere but the hash.
_TEMP_PASSWORD_BYTES = 9


class Refused(ValueError):
    """A refusal we wrote ourselves — safe and useful to show the person."""


def experimenters(db: str = DB):
    """Every active member, in one order, for every "who ran it" control."""
    return (Member.objects.using(db)
            .filter(is_active=True)
            .select_related("site")
            .order_by("display_name", "pk"))


def for_request(request, db: str = DB):
    """The signed-in person's `Member` row, or `None`.

    The join is by **username** across two databases — a pipeline login has a
    row in `academy_db.auth_user` and a second in `pipeline_db.auth_user`, and
    only the latter is what a `Member` points at (see the module docstring).

    Eight views have written this out for themselves. New callers use this one;
    the existing copies are identical and can move here whenever one of them is
    being touched for another reason.
    """
    from django.contrib.auth.models import User
    username = getattr(getattr(request, "user", None), "username", "")
    if not username:
        return None
    try:
        pipeline_user = User.objects.using(db).get(username=username)
        return Member.objects.using(db).get(user_id=pipeline_user.pk, is_active=True)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Read — the four-function board contract
# ---------------------------------------------------------------------------

def board_queryset():
    return Member.objects.using(DB).select_related("site")


def apply_filters(qs, *, q="", site="", role="", active="", imported=""):
    from django.db.models import Q
    if q:
        term = q.strip()
        ids = list(User.objects.using(DB)
                   .filter(Q(username__icontains=term)
                           | Q(first_name__icontains=term)
                           | Q(last_name__icontains=term)
                           | Q(email__icontains=term))
                   .values_list("pk", flat=True))
        qs = qs.filter(Q(display_name__icontains=term) | Q(user_id__in=ids))
    if site:
        from pipeline.services import sites as site_svc
        qs = site_svc.filter_by(qs, "site_id", site)
    if role:
        qs = qs.filter(role=role)
    if active in ("yes", "no"):
        qs = qs.filter(is_active=(active == "yes"))
    if imported != "yes":
        qs = qs.exclude(user_id__in=_imported_user_ids())
    return qs


def _imported_user_ids():
    return list(User.objects.using(DB)
                .filter(username__startswith=IMPORTED_PREFIX)
                .values_list("pk", flat=True))


def hidden_import_count(**filters) -> int:
    """How many rows the default view is leaving out, so the chip can say so."""
    shown = dict(filters, imported="yes")
    return (apply_filters(board_queryset(), **shown).count()
            - apply_filters(board_queryset(), **filters).count())


def _pipeline_users(member_ids):
    """{member.user_id: pipeline_db User} in one query."""
    return {u.pk: u for u in User.objects.using(DB).filter(pk__in=member_ids)}


def _logins(usernames):
    """{username: academy_db User} in one query. The login half."""
    return {u.username: u for u in
            User.objects.using(ACADEMY).filter(username__in=list(usernames))}


def row_for(member, pipe_user, login) -> dict:
    """One board row. Every value JSON — a string, number or bool.

    Both halves of the truth on one line, which is the whole point: the login
    columns come from ``academy_db`` and the access columns from ``pipeline_db``,
    and a person is only actually working when they agree.
    """
    username = pipe_user.username if pipe_user else ""
    return {
        "id": member.pk,
        "username": username,
        "name": (member.display_name
                 or (f"{pipe_user.first_name} {pipe_user.last_name}".strip()
                     if pipe_user else "")),
        "display_name": member.display_name or "",
        # The **name**, as every other board's site cell holds — and as the
        # filter above the grid and the picker inside it both offer. It was the
        # short code, with the name sitting unread in a `site_name` key beside
        # it, so the cell said `OTT` under a filter saying `Ontario`; once the
        # cell became a `<select>` the mismatch was no longer cosmetic, since a
        # picker whose options do not include the current value draws it as
        # "not a listed value". `sites.resolve` reads either spelling, so
        # nothing about what can be saved changes.
        "site": member.site.name if member.site_id else "",
        "role": member.role or "",
        # pipeline_db — the access half
        "is_active": bool(member.is_active),
        "pipeline_user": bool(pipe_user),
        # academy_db — the login half
        "login_exists": bool(login),
        "login_active": bool(login.is_active) if login else False,
        "has_password": bool(login and login.has_usable_password()),
        "email": (login.email if login else "") or "",
        "is_staff": bool(login.is_staff) if login else False,
        "is_superuser": bool(login.is_superuser) if login else False,
        "last_login": (login.last_login.strftime("%Y-%m-%d")
                       if login and login.last_login else ""),
        # The one-line verdict, computed the way the app actually gates.
        "can_sign_in": bool(login and login.is_active and login.has_usable_password()),
        "can_use_pipeline": bool(pipe_user and member.is_active),
    }


def _ordered(**filters):
    return (apply_filters(board_queryset(), **filters)
            .order_by("site__short_code", "display_name", "pk"))


def _rows_for(members) -> list[dict]:
    """The two cross-database lookups, asked once for a list of members.

    Both are batched deliberately: a person's login lives in `academy_db` and
    their pipeline user in `pipeline_db`, so doing this per row is two queries
    per person across two databases.
    """
    pipe = _pipeline_users([m.user_id for m in members])
    logins = _logins({u.username for u in pipe.values()})
    return [row_for(m, pipe.get(m.user_id),
                    logins.get(getattr(pipe.get(m.user_id), "username", "")))
            for m in members]


def board_rows(**filters) -> list[dict]:
    """Every matching row."""
    return _rows_for(list(_ordered(**filters)))


def board_page(*, page=1, per_page=board_page_svc.DEFAULT_PER_PAGE, locate=None,
               **filters) -> dict:
    """One page of people — the same contract as the other four boards.

    Twenty-four rows today, nine of them once the Access-era imports are
    hidden, so this changes nothing on screen. It is here because `board.js` is
    shared: a board whose server does not answer `pages` gets no pager, and the
    honest way to keep the five consistent is for all five to paginate rather
    than for the shared file to guess.
    """
    qs = _ordered(**filters)
    count = qs.count()
    pages = max(1, -(-count // per_page))
    located = board_page_svc.page_of(qs, locate, per_page) if locate else None
    if located:
        page = located
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    return {"rows": _rows_for(list(qs[start:start + per_page])), "count": count,
            "page": page, "pages": pages, "per_page": per_page,
            "located": (bool(located) if locate else None)}


def filter_options() -> dict:
    return {
        "sites": list(Site.objects.using(DB).filter(is_active=True).order_by("name")),
        "roles": [{"value": v, "label": l} for v, l in Member.Role.choices],
    }


# ---------------------------------------------------------------------------
# Guards — a privileged page must not be able to lock its operator out
# ---------------------------------------------------------------------------

def _superuser_usernames() -> set:
    return set(User.objects.using(ACADEMY)
               .filter(is_superuser=True, is_active=True)
               .values_list("username", flat=True))


def _guard_self_lockout(actor_username, username, field, value):
    """Refuse the edits that would leave nobody able to undo them.

    Only ``is_active`` can do that now: this page is behind
    ``pipeline_superuser_required``, which wraps ``pipeline_member_required``, so
    deactivating a superuser's **Member** row locks them out of the very page
    that could put it back. The superuser flag itself is no longer editable here.

    Named individually, because "could not save that" on a page whose whole job
    is access is the least useful message in the app.
    """
    if field != "is_active" or _as_bool(value):
        return
    same = (actor_username or "").lower() == (username or "").lower()
    if same:
        raise Refused(
            "You cannot deactivate yourself — this page is superusers only, and "
            "it needs an active pipeline record too, so you would not be able to "
            "open it again. Somebody else with a superuser account has to do it.")
    if username in _superuser_usernames() and _superuser_usernames() <= {username}:
        raise Refused(
            f"'{username}' is the only active superuser left, and deactivating "
            f"them closes this page to everybody. Give somebody else superuser "
            f"in Django admin first.")


# ---------------------------------------------------------------------------
# Write — one implementation, called by the board and by the command
# ---------------------------------------------------------------------------

# What this page may change: **pipeline membership**, and nothing wider.
#
# `is_superuser` and `is_staff` were editable here for a few hours and are not
# any more. They are not pipeline permissions — `is_staff` is raw access to
# every table on the site, the public pages and the academy included — so they
# belong in Django admin, where the change is logged. The board still *shows*
# who is a superuser, because you need to know who else can open it.
#
# `login_active` went with them: it is the flag that refuses a correct password,
# and resetting a password turns it back on (see `reset_password`), so the one
# thing it was needed for is covered without a toggle that can lock somebody out
# in one click.
_TRUE = {"1", "true", "yes", "on", "True"}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip() in _TRUE


MEMBER_FIELDS = {"role", "site", "is_active", "display_name"}
LOGIN_FIELDS = {"email"}
EDITABLE_FIELDS = MEMBER_FIELDS | LOGIN_FIELDS


#: Every spelling of a role that anything should accept, mapped to the stored
#: code. Built from the model's own choices, so a role added there is readable
#: here without a second edit.
def _role_spellings() -> dict:
    out = {}
    for code, label in Member.Role.choices:
        out[code.lower()] = code
        out[str(label).lower()] = code
    return out


def role_from(value) -> tuple[str, str]:
    """``(code, refusal)`` for a typed or pasted role — **either spelling**.

    The board's cell is a `<select>` showing *Administrator*; the Add panel's
    grid is a text box serialised to TSV, so it offers the same words and they
    have to read back. Before this the two surfaces named the same five roles
    differently — display names on the board, codes in the panel — which run 18
    found by comparing the two screens (F3). One fact, two vocabularies, on one
    page.

    Blank is not a refusal here; the callers decide whether a role is required.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return "", ""
    code = _role_spellings().get(text.lower())
    if code:
        return code, ""
    return "", (f"'{text}' is not a role. The roles are: "
                + ", ".join(f"{label} ({code})" for code, label in Member.Role.choices))


def cell_choices() -> dict:
    """What the people board's cells may hold.

    Both are closed and both were bare text boxes, on the page that decides who
    can reach the pipeline at all. `set_field` already refuses an unknown role
    by name and `sites.strict_id` an unknown site — so the grid was offering a
    mistake and reporting it after the save, on the one board where the
    consequence of a wrong value is somebody unable to sign in.

    The **Add people** panel had the same gap in a worse form: its intro listed
    the roles in prose (*"Roles: admin, pi, coordinator…"*) beside a blank
    column, which is a limited set described rather than offered. `views/
    user_board.py` hands the same lists to that panel's `suggestions`.
    """
    from pipeline.services import vocabulary

    return {
        "site": vocabulary.sites(),
        "role": {"strict": True,
                 "values": [{"value": v, "label": label}
                            for v, label in Member.Role.choices]},
    }


def set_field(member_id, field, value, *, actor_username=""):
    """Save one cell. Returns the fresh ``Member``. Raises ``Refused``.

    Which database a field lives in is not the caller's business — role and site
    are access (pipeline_db), email and the Django flags are the login
    (academy_db), and ``is_superuser`` is written to **both** user rows the way
    ``pipeline_users`` has always written it.
    """
    if field not in EDITABLE_FIELDS:
        raise Refused(f"'{field}' is not editable here.")
    member = Member.objects.using(DB).filter(pk=member_id).first()
    if member is None:
        raise Refused("That person is not on file.")
    pipe_user = User.objects.using(DB).filter(pk=member.user_id).first()
    username = pipe_user.username if pipe_user else ""

    _guard_self_lockout(actor_username, username, field, value)

    if field in MEMBER_FIELDS:
        if field == "site":
            from pipeline.services import sites as site_svc
            member.site_id = site_svc.strict_id(value)   # refuses by name
            if member.site_id is None:
                raise Refused("Everybody needs a site — it is what their records "
                              "are filed under.")
        elif field == "role":
            key, refusal = role_from(value)
            if refusal or not key:
                raise Refused(refusal or "Everybody needs a role.")
            member.role = key
        elif field == "is_active":
            member.is_active = _as_bool(value)
        else:
            member.display_name = str(value).strip()
        member.save(using=DB)
        return member

    # ── the login half ──
    login = (User.objects.using(ACADEMY).filter(username=username).first()
             if username else None)
    if login is None:
        raise Refused(
            f"There is no login account for '{username}', so there is nothing to "
            f"change yet. Use “Give them a login” to create one.")
    if field == "email":
        login.email = str(value).strip()
    login.save(using=ACADEMY)
    return member


def reset_password(member_id, *, password="", actor_username=""):
    """Set the login password. Returns the password to show **once**.

    The reason this exists on a page at all: the Django admin change form cannot
    do it — its password field is a read-only hash with a separate button — so a
    superuser who edits a user there and presses Save is told it worked and has
    changed no password.
    """
    member = Member.objects.using(DB).filter(pk=member_id).first()
    if member is None:
        raise Refused("That person is not on file.")
    pipe_user = User.objects.using(DB).filter(pk=member.user_id).first()
    username = pipe_user.username if pipe_user else ""
    login = (User.objects.using(ACADEMY).filter(username=username).first()
             if username else None)
    if login is None:
        raise Refused(f"There is no login account for '{username}' to reset. "
                      f"Use “Give them a login” to create one.")
    pw = (password or "").strip() or secrets.token_urlsafe(_TEMP_PASSWORD_BYTES)
    if len(pw) < 8:
        raise Refused("A password needs at least 8 characters.")
    login.set_password(pw)
    # A reset is pointless against an inactive login, and leaving it off is how
    # a correct password still fails as "invalid username or password".
    login.is_active = True
    login.save(using=ACADEMY)
    return pw


# ---------------------------------------------------------------------------
# Creating people — parse → plan → apply, the shape every bulk path here uses
# ---------------------------------------------------------------------------

COLUMNS = ["username", "first name", "last name", "email", "site", "role",
           "display name", "password"]
# The password column is optional and usually left blank: blank generates one
# and shows it once. It is here because "create a working login" is the job this
# page exists for, and being able to set the password you are about to read out
# to somebody over the phone is part of that.
EXAMPLE = ["jsmith", "Jo", "Smith", "j.smith@lab.org", "LEI", "experimenter",
           "Jo Smith", "leave blank to generate one"]

_ALIASES = {
    "username": "username", "user": "username", "login": "username",
    "first name": "first_name", "firstname": "first_name", "first": "first_name",
    "last name": "last_name", "lastname": "last_name", "last": "last_name",
    "surname": "last_name",
    "email": "email", "e-mail": "email", "mail": "email",
    "site": "site", "institution": "site", "lab": "site",
    "role": "role",
    "display name": "display_name", "displayname": "display_name",
    "name": "display_name",
    "password": "password", "pw": "password",
}


def parse(text: str) -> list[dict]:
    """Header-led TSV → row dicts. A header row is required, as everywhere."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    header = [_ALIASES.get(c.strip().lower().strip(" .:#"), "")
              for c in lines[0].split("\t")]
    if sum(1 for h in header if h) < 2:
        return []
    rows = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        row = {h: (cells[i].strip() if i < len(cells) else "")
               for i, h in enumerate(header) if h}
        if any(row.values()):
            rows.append(row)
    return rows


def plan(rows) -> list[dict]:
    """Read-only: what creating these would do, per row, before anything writes."""
    from pipeline.services import sites as site_svc
    items = []
    for r in rows:
        username = (r.get("username") or "").strip()
        note, status = "", "create"
        site = site_svc.resolve(r.get("site", ""))
        role, role_refusal = role_from(r.get("role"))

        if not username:
            status, note = "blocked", "needs a username — it is what links the two halves"
        elif site is None:
            status, note = "blocked", site_svc.refusal(r.get("site", "") or "(blank)")
        elif not role:
            status, note = "blocked", (role_refusal or
                                       ("needs a role: " + ", ".join(
                                           f"{label}" for _c, label in Member.Role.choices)))
        elif 0 < len((r.get("password") or "").strip()) < 8:
            status, note = "blocked", ("a typed password needs at least 8 characters — "
                                       "leave it blank to have one generated")
        else:
            pipe_user = User.objects.using(DB).filter(username=username).first()
            login = User.objects.using(ACADEMY).filter(username=username).first()
            existing = (Member.objects.using(DB).filter(user_id=pipe_user.pk).first()
                        if pipe_user else None)
            if existing:
                status = "update"
                note = "already has pipeline access — site and role will be updated"
            typed_pw = (r.get("password") or "").strip()
            parts = []
            if login:
                parts.append("a login already exists and is kept — its password is "
                             "left alone" + (", and the one typed here is ignored"
                                             if typed_pw else ""))
            elif typed_pw:
                parts.append("a login will be created with the password typed here")
            else:
                parts.append("a login will be created, with a password shown once")
            if pipe_user and not existing:
                parts.append("an existing pipeline record will be reused")
            note = f"{note} · {' · '.join(parts)}" if note else " · ".join(parts)

        items.append({
            "row": r, "username": username, "site": site.short_code if site else "",
            "role": role, "status": status, "note": note,
            "name": (r.get("display_name")
                     or f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()),
        })
    return items


def summarize(items) -> dict:
    return {
        "rows": len(items),
        "create": sum(1 for i in items if i["status"] == "create"),
        "update": sum(1 for i in items if i["status"] == "update"),
        "blocked": sum(1 for i in items if i["status"] == "blocked"),
        "sites": sorted({i["site"] for i in items if i["site"]}),
    }


def grant(rows, *, actor_username="") -> dict:
    """Create or update access for each row — all three records or none.

    The transaction is on ``pipeline_db`` only, because the login lives in a
    different database and Django cannot span the two. So the login is written
    **last**: a half-made person who can reach nothing is recoverable by running
    this again, while a login with no access is the state that reads as "it
    doesn't work".
    """
    from pipeline.services import sites as site_svc
    items = plan(rows)
    out = {"created": [], "updated": [], "skipped": [], "passwords": []}

    for it in items:
        if it["status"] == "blocked":
            out["skipped"].append({"username": it["username"], "why": it["note"]})
            continue
        r = it["row"]
        username = it["username"]
        site_id = site_svc.strict_id(r.get("site", ""))

        with transaction.atomic(using=DB):
            pipe_user, made_user = User.objects.using(DB).get_or_create(
                username=username,
                defaults={"email": r.get("email", ""),
                          "first_name": r.get("first_name", ""),
                          "last_name": r.get("last_name", "")})
            if made_user:
                pipe_user.set_unusable_password()   # never used for auth
                pipe_user.save(using=DB)
            member, made_member = Member.objects.using(DB).get_or_create(
                user_id=pipe_user.pk,
                defaults={"site_id": site_id, "role": it["role"], "is_active": True,
                          "display_name": r.get("display_name", "")})
            if not made_member:
                member.site_id = site_id
                member.role = it["role"]
                member.is_active = True
                if r.get("display_name"):
                    member.display_name = r["display_name"]
                member.save(using=DB)

        login = User.objects.using(ACADEMY).filter(username=username).first()
        if login is None:
            login = User(username=username, email=r.get("email", ""),
                         first_name=r.get("first_name", ""),
                         last_name=r.get("last_name", ""), is_active=True)
            pw = (r.get("password") or "").strip() or secrets.token_urlsafe(
                _TEMP_PASSWORD_BYTES)
            login.set_password(pw)
            login.save(using=ACADEMY)
            out["passwords"].append({"username": username, "password": pw})
        elif r.get("email"):
            login.email = r["email"]
            login.save(using=ACADEMY, update_fields=["email"])

        rec = {"username": username, "site": it["site"], "role": it["role"],
               "member_id": member.pk}
        (out["created"] if made_member else out["updated"]).append(rec)
    return out
