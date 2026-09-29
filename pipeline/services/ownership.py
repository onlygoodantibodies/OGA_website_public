"""Whose record this is, asked at every save: your own bench's, or a superuser.

**Only a superuser may create, edit or delete another site's records** (owner,
29 Sep 2026). Deleting, adding a freeze-down batch, attaching a file and
renumbering already asked `deletion.site_refusal`; every other door did not, so
a McGill member could paste antibodies onto Leicester's bench and edit any of
Leicester's cells — and then could not delete what they had just made, because
the one door that did ask was the last one they tried.

A rule fixed in one surface is a rule for one surface, so this is checked where
every write already passes: ``saved_by.SavedByField.pre_save`` calls ``check``,
which ``Model.save`` and ``bulk_create`` both reach. That covers antibodies,
cell lines, sessions and every result table — including doors nobody has
written yet. The previews ask ``refusal`` first so a row is named as blocked
before anything is pressed; the save is the backstop, and it raises
``CrossSiteWrite`` rather than writing, so a door that forgot its preview check
still fails loudly instead of editing somebody else's bench.

Three things it deliberately does not refuse:

* **A record with no site.** It is nobody's bench, not another bench — 32
  antibodies on live carry none. Deleting one stays a superuser's job
  (`deletion.site_refusal`), a stricter rule for an irreversible act.
* **Outside a request** — a management command, a migration, a test calling a
  service directly. The owner runs those deliberately.
* **Moving is two sites.** An edit is checked against the site the row has on
  file *and* the one it is being given, so a member cannot take another site's
  record by retyping its site to their own.

The wording is `deletion.site_refusal`'s, with the verb moved, so every door
says the same sentence.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

DB = "pipeline_db"

#: Cached on the request so a paste of fifty rows asks once who is pasting.
_ASKER = "_oga_ownership_asker"
#: Set on the request when a save was refused, whatever the view did next.
REFUSED = "_oga_ownership_refused"


class CrossSiteWrite(PermissionDenied):
    """A save refused because the row belongs to another site."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def asker(request):
    """``(member, is_superuser)`` for the signed-in person, once per request."""
    cached = getattr(request, _ASKER, None)
    if cached is not None:
        return cached
    from pipeline.services import members
    user = getattr(request, "user", None)
    is_su = bool(getattr(user, "is_superuser", False))
    member = None if is_su else members.for_request(request)
    cached = (member, is_su)
    try:
        setattr(request, _ASKER, cached)
    except Exception:  # noqa: BLE001 — a request that refuses attributes
        pass
    return cached


def _site_names(site_ids) -> str:
    from pipeline.models import Site
    return ", ".join(sorted(Site.objects.using(DB).filter(pk__in=site_ids)
                            .values_list("name", flat=True))) or "another site"


def refusal(site_ids, member, is_superuser, *, action="edit",
            su_verb=None, new_record=None) -> str:
    """Why this person may not write a record belonging to ``site_ids``, or "".

    ``site_ids`` is every site the write touches — the one on file and the one
    it will have. Blank ids are ignored: a row with no site is nobody's.
    """
    if is_superuser:
        return ""
    sites = {s for s in site_ids if s}
    if sites:
        # An id that names no site is a stale page, not somebody else's bench —
        # the door it came through falls back or refuses it in its own words.
        from pipeline.models import Site
        sites = set(Site.objects.using(DB).filter(pk__in=sites)
                    .values_list("pk", flat=True))
    if not sites:
        return ""
    mine = getattr(member, "site_id", None)
    su_verb = su_verb or action
    if not mine:
        return (f"Your account has no site, so no bench's records are yours to "
                f"{action}. A superuser can set one on the people board.")
    others = sites - {mine}
    if not others:
        return ""
    if new_record:
        # A record that does not exist yet does not *belong* to anybody — "This
        # record belongs to Leicester" over a session still being planned named
        # a record nobody could find (field test, 29 Sep 2026).
        return (f"This {new_record} would be {_site_names(others)}'s, not your "
                f"site's. You can {action} your own bench; a superuser can "
                f"{su_verb} any site.")
    return (f"This record belongs to {_site_names(others)}, not to your site. "
            f"You can {action} your own bench's records; a superuser can "
            f"{su_verb} any.")


def row_refusal(site_id, request, *, action="add") -> str:
    """The preview's question for one row about to be written at ``site_id``."""
    if request is None:
        return ""
    member, is_su = asker(request)
    return refusal({site_id}, member, is_su, action=action)


def _sites_of(instance) -> set:
    """Every site this save touches: the row's new site and its stored one."""
    model = type(instance)
    if hasattr(instance, "site_id"):
        sites = {instance.site_id}
        if instance.pk is not None:
            sites |= set(model.objects.using(DB).filter(pk=instance.pk)
                         .values_list("site_id", flat=True))
        return sites
    session_id = getattr(instance, "session_id", None)
    if session_id:
        from pipeline.models import ExperimentSession
        return set(ExperimentSession.objects.using(DB).filter(pk=session_id)
                   .values_list("site_id", flat=True))
    return set()


def check(instance) -> None:
    """Raise ``CrossSiteWrite`` if the person saving may not write this row."""
    from pipeline import saved_by
    request = saved_by._request.get()
    if request is None:
        return
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return
    member, is_su = asker(request)
    if is_su:
        return
    message = refusal(_sites_of(instance), member, is_su)
    if message:
        # Marked on the request as well as raised: a view that catches every
        # exception and answers "could not save" would otherwise put a vaguer
        # sentence over this one (`saved_by.SavedByMiddleware` reads the mark).
        try:
            setattr(request, REFUSED, message)
        except Exception:  # noqa: BLE001
            pass
        raise CrossSiteWrite(message)


def refusal_now(site_ids, *, action="add", su_verb=None, new_record=None) -> str:
    """``refusal`` for the person making the current request, or "" outside one.

    For the previews, which run inside the request but are handed a member
    rather than the request itself — so no preview signature has to change to
    ask the question the save will ask.
    """
    from pipeline import saved_by
    request = saved_by._request.get()
    if request is None:
        return ""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    member, is_su = asker(request)
    return refusal(site_ids, member, is_su, action=action, su_verb=su_verb,
                   new_record=new_record)


def viewer(request) -> dict:
    """``{"site": name, "superuser": bool}`` for a page that draws other sites'
    rows, so it can mark them read-only before anybody types (`board.js::
    lockedFor`). The save still asks `check`; this only says so sooner.
    """
    member, is_su = asker(request)
    site = getattr(member, "site", None) if getattr(member, "site_id", None) else None
    return {"site": getattr(site, "name", "") or "", "superuser": bool(is_su)}
