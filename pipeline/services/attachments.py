"""Raw files against a session — the one reader for what may be attached.

``FileAttachment`` has been in the schema since the initial migration, carrying
exactly the categories a bench produces — WB scan, Ponceau, IF plate overview,
FC raw ``.fcs``, IP scan — and **nothing in the app wrote to it or read it**.
The only door was Django admin, which is not a door a bench scientist opens. So
the readings were recorded here and the images those readings were taken off
lived on somebody's laptop, with nothing joining the two.

This is that join. It is worth having whether or not the Zenodo deposit is ever
built: a result you cannot get back to the image behind it is a number nobody
can check.

Four things it refuses, each because the alternative is a wrong record written
in silence rather than an error anybody would notice:

- **A result row from another session.** ``FileAttachment`` carries a session
  *and* four nullable result FKs, so nothing in the schema stops a WB scan from
  session 480 being filed against session 12's result row. That is a raw image
  attached to an experiment nobody ran it in — the same family as a bench sheet
  uploaded into the wrong session, which is refused on the preview *and* again
  on the commit.
- **A result row of the wrong procedure.** Which of the four FKs a row belongs
  in is decided by the session's own ``procedure_type``, never by the caller, so
  an IF row cannot be handed in as ``wb_result``.
- **A category the model does not define.** Read from
  ``FileAttachment.FileCategory`` rather than typed here — the rule the result
  columns already follow (``session_board.result_field_names``), because a
  hand-written second list is one that drifts.
- **A file past the ceiling**, by name and with the ceiling, the way
  ``services/concentration.py`` and ``services/c_number.py`` refuse: "too big"
  without the number is a refusal nobody can act on.

Ownership is ``deletion.site_refusal`` — your own bench's records, or you are a
superuser (owner's decision, 3 Aug) — asked of the **session**, because that is
what the file is about. Not a second copy of the wording.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.urls import reverse

from pipeline.models import FileAttachment
from pipeline.services import deletion

logger = logging.getLogger(__name__)

DB = "pipeline_db"

# Which result model each procedure's rows live in, and therefore which of
# ``FileAttachment``'s four nullable FKs a file for that row belongs in. The
# caller never picks: it names a result row id, and the session says what that
# row can be.
RESULT_FK = {"WB": "wb_result", "IP": "ip_result",
             "IF": "if_result", "FC": "fc_result"}

# A ceiling, so an upload cannot sit in a web request long enough to be killed
# half-written. Deliberately generous for a gel scan or an .fcs file and
# deliberately below what a raw microscopy stack costs: the moment somebody
# needs more, this is the one number to move, and the refusal already names it
# so they will know which number that was.
MAX_BYTES = 200 * 1024 * 1024


def max_mb() -> int:
    return MAX_BYTES // (1024 * 1024)


def categories() -> list[dict]:
    """Every category, from the model. Never a list typed here."""
    return [{"key": k, "label": v} for k, v in FileAttachment.FileCategory.choices]


# Which categories a procedure actually produces. This orders the picker; it
# does **not** shorten it. A session that ran a gel and photographed something
# unexpected still has somewhere to put it, and a category hidden because the
# registry did not expect it is the failure the read-only session conditions
# exist to avoid.
_LIKELY = {
    "WB": ["wb_scan", "ponceau", "wb_composite"],
    "IP": ["ip_scan", "wb_scan", "ponceau"],
    "IF": ["if_image", "if_plate"],
    "FC": ["fc_histogram", "fc_fcs"],
}


def categories_for(procedure: str) -> list[dict]:
    """The full list, with this procedure's own kinds first.

    Ordering is the whole of the help here: an FC session offering "Ponceau S
    stain" as its first choice is a form that does not know what you just did.
    """
    likely = _LIKELY.get((procedure or "").upper(), [])
    order = {k: i for i, k in enumerate(likely)}
    return sorted(categories(),
                  key=lambda c: (order.get(c["key"], len(likely)), c["label"]))


def storage_refusal() -> str:
    """Why nothing can be attached right now, or "".

    **A file the app knows it will lose is worse than no file**, because the row
    claims it exists: the download 404s weeks later with nothing to explain it,
    and the reading it was evidence for is left pointing at a gap.

    Render rebuilds the container filesystem from git on every deploy, so with
    object storage switched off in production an upload survives exactly until
    the next Manual Deploy — and nothing anywhere would say so. The one variable
    that decides this lives in the Render dashboard, invisible from the repo (the
    same blind spot that let a stale ``staticfiles/`` outrank months of edits), so
    it is checked at the moment it matters rather than assumed.

    The message names no setting: it reaches a bench scientist, and a variable
    they cannot set is not an instruction. The detail goes to the log and to
    ``manage.py check``.
    """
    if getattr(settings, "USE_R2", False):
        return ""
    if settings.DEBUG:
        return ""                      # dev, where local media is the point
    root = str(getattr(settings, "MEDIA_ROOT", "") or "")
    persistent = tuple(getattr(settings, "PERSISTENT_MEDIA_ROOTS", ()) or ())
    if persistent and root.startswith(persistent):
        return ""
    logger.error("attachments refused: USE_R2 is off and MEDIA_ROOT (%s) is not "
                 "on a persistent disk, so uploads would not survive a deploy",
                 root or "unset")
    return ("Files cannot be attached at the moment: this site is not set up to "
            "keep them, and anything uploaded now would be lost the next time "
            "the site is updated. Nothing has been saved. Please tell whoever "
            "looks after the site — the readings themselves are unaffected.")


def write_refusal(session, member=None, is_superuser=False, *,
                  action="add files to", su_verb="attach to") -> str:
    """Why this person may not attach to or remove from this session, or "".

    One name for the question so a caller never has to know the rule lives in
    ``deletion``, and so the panel's greyed button and the endpoint's refusal
    cannot come to disagree about who may do what.

    ``action``/``su_verb`` say **which** act is being refused. The rule is one
    rule and stays in ``deletion``; only the verb moves. Without it this returned
    ``deletion``'s wording verbatim, so a greyed **Attach** button told you
    "nothing is yours to delete" — a sentence about the wrong act, on a screen
    with no delete on it, which reads as the wrong record having been picked.
    """
    return deletion.site_refusal("session", session, member, is_superuser,
                                 action=action, su_verb=su_verb)


def attach_refusal(session, member=None, is_superuser=False) -> str:
    """Why the Attach button is greyed, or "".

    One reader for the two reasons, in the order ``check`` applies them, so the
    greyed button and the endpoint's refusal cannot come to say different
    things — a check that is silent about something the save will refuse is a
    refusal deferred to the worst moment.
    """
    return storage_refusal() or write_refusal(session, member, is_superuser)


def _result_label(att, procedure) -> str:
    """Which result row this file belongs to, in the words the panel prints."""
    row = getattr(att, RESULT_FK.get(procedure, ""), None) if procedure else None
    if row is None:
        return ""
    ab = getattr(row, "antibody", None)
    return str(ab) if ab else f"result #{row.pk}"


def rows_for(session) -> list[dict]:
    """This session's files, as JSON.

    **Every value is a string, number or bool.** Returning the ``FileField``
    itself would raise inside ``JsonResponse`` and take the whole panel down
    while the page blamed the filters — the ``cell_line_board`` bug, which only
    showed on the rows that happened to carry the object.
    """
    fk = RESULT_FK.get(session.procedure_type or "", "")
    qs = FileAttachment.objects.using(DB).filter(session_id=session.pk)
    if fk:
        qs = qs.select_related(f"{fk}__antibody")
    qs = qs.select_related("uploaded_by").order_by("-uploaded_at", "-pk")

    labels = dict(FileAttachment.FileCategory.choices)
    out = []
    for a in qs:
        out.append({
            "id": a.pk,
            # Built here, not in the template: a page that assembles URLs is a
            # page that has to be edited when a route moves.
            "download_url": reverse("pipeline:attachment_download", args=[a.pk]),
            "category": a.category,
            "category_label": labels.get(a.category, a.category),
            "filename": a.original_filename or "",
            "size_bytes": a.file_size_bytes or 0,
            "description": a.description or "",
            "uploaded_by": str(a.uploaded_by) if a.uploaded_by_id else "",
            "uploaded_at": a.uploaded_at.date().isoformat() if a.uploaded_at else "",
            "result_label": _result_label(a, session.procedure_type),
        })
    return out


def result_options(session) -> list[dict]:
    """The rows a file may be filed against, as the picker prints them.

    **One list behind both doors.** The sessions board built this by reading the
    result cards it had already drawn — the right instinct, since it costs no
    second fetch, and the wrong shape the moment a second surface wants the same
    picker: a gene's page lists a session without ever drawing its results, so
    there is nothing in the DOM to read and the picker would have silently
    offered "the whole session" and nothing else. A file that could have named
    its antibody and did not is a raw image nobody can get back to a reading,
    which is the whole reason this model has result FKs at all.

    Same rule as ``services/members.py::experimenters`` and
    ``services/sites.py``: when two surfaces answer one question, what the
    reader sees is the disagreement, not the better answer.
    """
    fk = RESULT_FK.get(session.procedure_type or "")
    if not fk:
        return []
    model = FileAttachment._meta.get_field(fk).remote_field.model
    rows = (model.objects.using(DB).filter(session_id=session.pk)
            .select_related("antibody", "antibody__company").order_by("pk"))
    # The same label ``session_board.results_for`` gives its cards, so the two
    # surfaces name one row the same way.
    return [{"id": r.pk,
             "label": str(r.antibody) if r.antibody_id else f"result #{r.pk}"}
            for r in rows]


def counts_for(session_ids) -> dict:
    """How many files each of these sessions has, in one query.

    For a page that lists sessions without opening any of them. It is one
    aggregate rather than a fifth ``Count`` on the caller's own ``annotate``,
    deliberately: ``views/dashboard.py`` already annotates four result counts,
    and joining a second multi-valued relation alongside them multiplies both —
    a session with 3 results and 2 files would report 6 of each. Same shape as
    ``session_board.reading_counts``, and it does not grow with the row count.
    """
    ids = [int(i) for i in session_ids if i]
    if not ids:
        return {}
    rows = (FileAttachment.objects.using(DB).filter(session_id__in=ids)
            .values("session_id").annotate(n=Count("pk")))
    return {r["session_id"]: r["n"] for r in rows}


def _result_refusal(session, result_id):
    """Resolve a result row id against **this** session, or say why not.

    Returns ``(row, error)``. A row belonging to another session is the reason
    this function exists: nothing in the schema forbids it, the panel would show
    the file against the antibody you picked, and the record would be a raw
    image filed under an experiment it was not produced in.
    """
    if not result_id:
        return None, ""
    fk = RESULT_FK.get(session.procedure_type or "")
    if not fk:
        return None, (f"This session has no procedure recorded, so a file "
                      f"cannot be attached to one of its result rows. Attach it "
                      f"to the session instead.")
    model = FileAttachment._meta.get_field(fk).remote_field.model
    row = model.objects.using(DB).filter(pk=result_id).first()
    if row is None:
        return None, f"There is no {session.procedure_type} result row #{result_id}."
    if row.session_id != session.pk:
        return None, (f"Result row #{result_id} belongs to session "
                      f"#{row.session_id}, not to this one. Open that session to "
                      f"attach a file to its rows.")
    return row, ""


def check(session, *, category, filename, size_bytes, result_id=None,
          member=None, is_superuser=False) -> dict:
    """Everything that would refuse this upload, before a byte is stored.

    Separate from ``save`` so the view answers the same questions in the same
    words whether it is asked before or during — and so a refusal names itself
    rather than arriving as a 500 with the file half-written.
    """
    errors = []

    # First, because it is the one refusal that is nobody's fault and that no
    # amount of correcting the form would get past.
    nowhere_to_put_it = storage_refusal()
    if nowhere_to_put_it:
        errors.append(nowhere_to_put_it)

    not_yours = write_refusal(session, member, is_superuser)
    if not_yours:
        errors.append(not_yours)

    known = {c["key"] for c in categories()}
    if category not in known:
        names = ", ".join(sorted(c["label"] for c in categories()))
        errors.append(f"{category!r} is not a kind of file this records. "
                      f"It takes: {names}.")

    if not (filename or "").strip():
        errors.append("Choose a file first.")

    if size_bytes and size_bytes > MAX_BYTES:
        got = size_bytes / (1024 * 1024)
        errors.append(f"That file is {got:.0f} MB and the limit here is "
                      f"{max_mb()} MB. Large raw stacks are not stored in the "
                      f"app — record where they live in the description, or ask "
                      f"for the limit to be raised.")
    if size_bytes == 0:
        errors.append("That file is empty — nothing was read from it.")

    row, why = _result_refusal(session, result_id)
    if why:
        errors.append(why)

    return {"ok": not errors, "errors": errors, "result_row": row}


@transaction.atomic(using=DB)
def save(session, upload, *, category, result_id=None, description="",
         member=None, is_superuser=False) -> dict:
    """Store one file against a session. Asks ``check`` again rather than
    trusting whatever asked it a moment ago — a preview is not a permission
    slip, and this one has a file input still sitting open beside it.
    """
    verdict = check(session, category=category, filename=upload.name,
                    size_bytes=upload.size, result_id=result_id,
                    member=member, is_superuser=is_superuser)
    if not verdict["ok"]:
        return {"ok": False, "errors": verdict["errors"]}

    # Read once, before the file is handed to storage — `upload` is a stream and
    # a second pass over it after saving would be a second read from the bucket.
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)

    att = FileAttachment(
        session_id=session.pk,
        category=category,
        file=upload,
        checksum_sha256=digest.hexdigest(),
        original_filename=upload.name[:500],
        file_size_bytes=upload.size,
        # `content_type` is what the browser claimed; fall back to the name,
        # because a browser that says nothing is common and an empty column
        # here is worse than a guess made from the extension.
        mime_type=(getattr(upload, "content_type", "")
                   or mimetypes.guess_type(upload.name)[0] or "")[:100],
        description=(description or "").strip(),
    )
    row = verdict["result_row"]
    if row is not None:
        setattr(att, f"{RESULT_FK[session.procedure_type]}_id", row.pk)
    # Cross-DB FK: assign the id, never the object (CLAUDE.md).
    if member is not None:
        att.uploaded_by_id = member.pk
    att.save(using=DB)
    return {"ok": True, "id": att.pk}


def remove(attachment_id, *, member=None, is_superuser=False) -> dict:
    """Delete one file. Nothing points at an attachment, so this is a two-click
    job with no manifest — ``services/deletion.py``'s red panel is for records
    whose removal reaches other rows, and using it here is how it stops meaning
    anything.

    The stored file goes with the row. A row pointing at bytes that are gone is
    a download that fails for a reason nobody can see.
    """
    att = (FileAttachment.objects.using(DB)
           .select_related("session").filter(pk=attachment_id).first())
    if att is None:
        return {"ok": False, "errors": ["That file is already gone."]}
    # Removing, not attaching — the same rule, said as the act it is refusing.
    not_yours = write_refusal(att.session, member, is_superuser,
                              action="remove files from", su_verb="remove one from")
    if not_yours:
        return {"ok": False, "errors": [not_yours]}

    name = att.original_filename
    stored = att.file
    att.delete(using=DB)
    try:
        stored.delete(save=False)
    except Exception:                                    # pragma: no cover
        # The row is the record; orphaned bytes cost storage and mislead nobody.
        logger.exception("could not remove stored file for attachment %s",
                         attachment_id)
    return {"ok": True, "filename": name}
