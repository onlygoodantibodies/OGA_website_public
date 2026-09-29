"""Who added a record, and who last saved it — stamped at the save, not asked of
each write path.

The question this answers was put on 27 Sep 2026 — *which names are associated
with new data?* — and the database could not say. Sessions record the
**experimenter**, which is a choice made in a form rather than the person at the
keyboard; cell lines, vials and readings recorded nobody; and `Antibody.
created_by` was an FK nothing on the boards ever filled. The answer had to be
reconstructed from Render's access log by matching browsers, which holds no
usernames and forgets everything after about two weeks. It showed the MCOLN1
readings of 24 Sep were entered by Mickey (Congyao Zha) — filed under an
Access-era profile of the same person as experimenter.

So the stamp is taken where every write passes, on the same principle as
`services/lab_numbers.py` issuing numbers on `pre_save` rather than in eight
write paths: a rule fixed in one surface is a rule for one surface.

Three pieces:

* ``SavedByMiddleware`` remembers the request for its own duration. It does
  **not** touch ``request.user`` — a public page never pays for this, and the
  login is only read when a save actually happens.
* ``SavedByField`` stamps itself in ``pre_save``. That is a *field* method, not
  the ``pre_save`` signal, because ``bulk_create`` calls it too — the paste and
  upload doors create rows in bulk and no signal fires for those.
* ``SavedBy`` is the abstract base carrying the pair. Its ``save`` adds
  ``saved_by`` to an ``update_fields`` list, since a partial save only calls
  ``pre_save`` for the fields it names and most board edits are partial.

What is written is the **username**, not an FK: pipeline users live in two
databases (see `PendingPublicationImage.staged_by`, the same choice). Outside a
request it is ``command:<name>`` when a management command is running — an
import is a thing that saved rows too, and "a script did this" is an answer
where a blank is not — and otherwise blank, which means *not recorded*, never
*nobody*. Rows saved before 27 Sep 2026 are blank for that reason.

Two paths still bypass it, and both bypass ``auto_now`` the same way, so the
stamp and ``updated_at`` stay in step: ``QuerySet.update()`` and
``bulk_update()``.
"""
from __future__ import annotations

import sys
from contextvars import ContextVar

from django.db import models

#: The two columns, for every generic sweep that must leave them alone — the
#: result-column lists (a stamp is not a reading), the merge backfills (the
#: survivor's history is its own) and the payload fields an API may set.
FIELDS = ("added_by", "saved_by")

SCRIPT_PREFIX = "command:"

_request: ContextVar = ContextVar("saved_by_request", default=None)


class SavedByMiddleware:
    """Keep the request reachable from a model's ``save`` — nothing more."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request.set(request)
        try:
            response = self.get_response(request)
        finally:
            _request.reset(token)
        return self._refused(request) or response

    def _refused(self, request):
        """The reply for a save refused as another site's record, or None.

        Taken from the request's mark, not only from an exception reaching
        here, because many write views catch everything and answer with a
        generic "could not save" — which would put a vaguer sentence over the
        one that says whose record it is (`services/ownership.py`).
        """
        from pipeline.services.ownership import REFUSED
        message = getattr(request, REFUSED, "")
        if not message:
            return None
        from django.http import HttpResponseForbidden, JsonResponse
        wants_json = ("json" in request.headers.get("Accept", "")
                      or "json" in request.headers.get("Content-Type", "")
                      or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                      or request.method != "GET")
        if wants_json:
            return JsonResponse({"ok": False, "error": message,
                                 "errors": [message]}, status=403)
        return HttpResponseForbidden(message)

    def process_exception(self, request, exception):
        """A save refused as another site's record is a sentence, not a 500.

        Every board, panel and upload reads ``error``/``errors`` off a JSON
        reply, so the refusal reaches the banner or the panel it was pressed
        from in the same words the previews use (`services/ownership.py`).
        """
        from pipeline.services.ownership import CrossSiteWrite
        if not isinstance(exception, CrossSiteWrite):
            return None
        from django.http import JsonResponse
        return JsonResponse({"ok": False, "error": exception.message,
                             "errors": [exception.message]}, status=403)


def current() -> str:
    """The name to stamp on a save happening now."""
    request = _request.get()
    if request is not None:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return user.get_username()[:150]
        return ""
    argv = sys.argv
    if len(argv) > 1 and argv[0].endswith("manage.py"):
        return f"{SCRIPT_PREFIX}{argv[1]}"[:150]
    return ""


class SavedByField(models.CharField):
    """A username stamped at save. ``on_add`` stamps the insert only."""

    def __init__(self, *args, on_add=False, **kwargs):
        self.on_add = on_add
        kwargs.setdefault("max_length", 150)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("default", "")
        # `db_default` as well as `default`: with only the latter Django fills
        # the existing rows and then drops the column default, so code rolled
        # back past this migration would INSERT without the column and
        # PostgreSQL would refuse every new row (CLAUDE.md, Working agreement).
        kwargs.setdefault("db_default", "")
        kwargs.setdefault("editable", False)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.on_add:
            kwargs["on_add"] = True
        return name, path, args, kwargs

    def pre_save(self, model_instance, add):
        if not self.on_add and not getattr(model_instance, "_ownership_asked", False):
            # Whose bench this row is on (`services/ownership.py`) — asked here
            # only for `bulk_create`, which calls a field's `pre_save` and never
            # `save()`. A plain save asks in `SavedBy.save`, before Django opens
            # its own transaction handling: raised in here, the refusal marks
            # the surrounding transaction broken.
            from pipeline.services import ownership
            ownership.check(model_instance)
        if self.on_add:
            # A writer that already knows who added the row keeps its answer.
            if add and not getattr(model_instance, self.attname):
                setattr(model_instance, self.attname, current())
        else:
            setattr(model_instance, self.attname, current())
        return getattr(model_instance, self.attname)


class SavedBy(models.Model):
    added_by = SavedByField(
        on_add=True,
        help_text="Username of whoever created this row through the website, "
                  "or command:<name> for a management command. Blank = not "
                  "recorded (every row saved before 27 Sep 2026).")
    saved_by = SavedByField(
        help_text="Username of whoever last saved this row; see added_by.")

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        from pipeline.services import ownership
        ownership.check(self)
        fields = kwargs.get("update_fields")
        if fields:
            kwargs["update_fields"] = {*fields, "saved_by"}
        self._ownership_asked = True
        try:
            super().save(*args, **kwargs)
        finally:
            self._ownership_asked = False


def who(name: str) -> str:
    """A stamp as a reader says it: ``command:x`` is *a script (x)*."""
    if name.startswith(SCRIPT_PREFIX):
        return f"a script ({name[len(SCRIPT_PREFIX):]})"
    return name


def describe(added_by: str, saved_by: str, saved_at=None) -> str:
    """One line for a board row, or ``""`` when nothing was recorded.

    Drawn only where there is something to say: a blank is every row older
    than the stamp, and "not recorded" on three thousand of them is noise.
    """
    when = f", {saved_at:%d %b %Y}" if saved_at else ""
    if added_by and added_by == saved_by:
        return f"added and last saved by {who(added_by)}{when}"
    parts = []
    if added_by:
        parts.append(f"added by {who(added_by)}")
    if saved_by:
        parts.append(f"last saved by {who(saved_by)}{when}")
    return " · ".join(parts)
