"""Tests for attaching a raw file to a session.

Pins the four things that would be wrong *silently* — a bad record written and
no error anywhere — rather than the whole surface. The panel itself is new and
unused, and a feature nobody has used yet does not need a suite (CLAUDE.md).

Proves:
  * a result row from **another session** is refused by name, so a gel scan
    cannot be filed against an experiment it was not produced in
  * which of ``FileAttachment``'s four result FKs is used comes from the
    session's own procedure, never from the caller
  * the panel's rows are JSON — a ``FieldFile`` in the payload raises inside
    ``JsonResponse`` and takes the whole panel down while the page blames the
    filters (the ``cell_line_board.arrived_with_ko`` bug)
  * the categories offered are the model's own, so a hand-written second list
    cannot drift from it
  * another site's session refuses both attaching and removing, in the same
    words deleting already uses
"""
from __future__ import annotations

import json
from datetime import date

import pytest

DB = "pipeline_db"


@pytest.fixture()
def seeded(_pipeline_db):
    from django.contrib.auth.models import User
    from django.core.files.uploadedfile import SimpleUploadedFile
    from pipeline.models import (
        Antibody, Company, ExperimentSession, FileAttachment, Member, Site,
        Target, WbResult, IfResult,
    )
    for M in (FileAttachment, WbResult, IfResult, ExperimentSession, Antibody,
              Company, Target, Member, Site):
        M.objects.using(DB).all().delete()

    mine = Site.objects.using(DB).create(name="Att Mine", short_code="ATM")
    theirs = Site.objects.using(DB).create(name="Att Theirs", short_code="ATT")
    user, _ = User.objects.using(DB).get_or_create(username="att-tester")
    # `display_name` deliberately set: `Member.__str__` falls back to the
    # cross-DB `user` without one, which the sessions board already relies on
    # not doing (`session_board.row_for` prints `str(session.experimenter)`).
    member = Member.objects.using(DB).create(
        user_id=user.pk, site=mine, role="admin", display_name="Att Tester")

    target = Target.objects.using(DB).create(
        protein_name="Stathmin 2", gene_name="STMN2")
    company = Company.objects.using(DB).create(name="Att Supplier")
    ab = Antibody.objects.using(DB).create(
        catalogue_number="ATT-1", company=company, target=target)

    day = date(2026, 8, 4)
    wb = ExperimentSession.objects.using(DB).create(
        target=target, site=mine, procedure_type="WB", date=day,
        experimenter=member)
    other = ExperimentSession.objects.using(DB).create(
        target=target, site=mine, procedure_type="WB", date=day,
        experimenter=member)
    elsewhere = ExperimentSession.objects.using(DB).create(
        target=target, site=theirs, procedure_type="WB", date=day,
        experimenter=member)

    row = WbResult.objects.using(DB).create(session=wb, antibody=ab)
    other_row = WbResult.objects.using(DB).create(session=other, antibody=ab)

    return {
        "member": member, "wb": wb, "other": other, "elsewhere": elsewhere,
        "row": row, "other_row": other_row, "ab": ab,
        "upload": lambda name=b"gel", filename="scan.tif": SimpleUploadedFile(
            filename, name, content_type="image/tiff"),
    }


# ── the one that matters ────────────────────────────────────────────────────

def test_a_result_row_from_another_session_is_refused_by_name(seeded):
    """Nothing in the schema forbids it, and the record it would write is a raw
    image attached to an experiment nobody ran it in."""
    from pipeline.services import attachments

    verdict = attachments.check(
        seeded["wb"], category="wb_scan", filename="scan.tif",
        size_bytes=10, result_id=seeded["other_row"].pk,
        member=seeded["member"])

    assert verdict["ok"] is False
    joined = " ".join(verdict["errors"])
    # Named, and it says where the row does belong — a refusal that only says
    # "no" is half a message.
    assert str(seeded["other_row"].pk) in joined
    assert str(seeded["other"].pk) in joined


def test_a_result_row_from_this_session_is_accepted_and_lands_on_the_right_fk(seeded):
    from pipeline.models import FileAttachment
    from pipeline.services import attachments

    out = attachments.save(
        seeded["wb"], seeded["upload"](), category="wb_scan",
        result_id=seeded["row"].pk, member=seeded["member"])
    assert out["ok"] is True, out

    att = FileAttachment.objects.using(DB).get(pk=out["id"])
    # The procedure decides the column, not the caller.
    assert att.wb_result_id == seeded["row"].pk
    assert att.if_result_id is None
    assert att.session_id == seeded["wb"].pk
    # The model carries these three and nothing was filling them in.
    assert att.original_filename == "scan.tif"
    assert att.file_size_bytes == 3
    assert att.mime_type == "image/tiff"
    assert att.uploaded_by_id == seeded["member"].pk


def test_save_refuses_the_same_things_check_does(seeded):
    """`save` re-asks rather than trusting whatever asked a moment ago — the
    file input is still sitting open beside the button."""
    from pipeline.models import FileAttachment
    from pipeline.services import attachments

    out = attachments.save(
        seeded["wb"], seeded["upload"](), category="wb_scan",
        result_id=seeded["other_row"].pk, member=seeded["member"])
    assert out["ok"] is False
    assert FileAttachment.objects.using(DB).count() == 0


# ── the payload ─────────────────────────────────────────────────────────────

def test_rows_for_is_json(seeded):
    """A `FieldFile` in the payload raises inside `JsonResponse` and the panel
    shows nothing while the page blames something else."""
    from pipeline.services import attachments

    attachments.save(seeded["wb"], seeded["upload"](), category="wb_scan",
                     result_id=seeded["row"].pk, member=seeded["member"])
    rows = attachments.rows_for(seeded["wb"])

    assert len(rows) == 1
    json.dumps(rows)                      # raises if anything is a model/file
    for value in rows[0].values():
        assert isinstance(value, (str, int, float, bool)), value
    # The antibody the file is evidence about, in the words the panel prints.
    assert str(seeded["ab"]) in rows[0]["result_label"]


def test_a_file_against_the_whole_session_has_no_result_row(seeded):
    """A Ponceau is about the run, not about one antibody, so the default must
    not guess a row for it."""
    from pipeline.services import attachments

    attachments.save(seeded["wb"], seeded["upload"](), category="ponceau",
                     member=seeded["member"])
    rows = attachments.rows_for(seeded["wb"])
    assert rows[0]["result_label"] == ""


# ── the lists ───────────────────────────────────────────────────────────────

def test_categories_come_from_the_model(seeded):
    from pipeline.models import FileAttachment
    from pipeline.services import attachments

    keys = {c["key"] for c in attachments.categories()}
    assert keys == {k for k, _ in FileAttachment.FileCategory.choices}


def test_a_procedures_own_kinds_come_first_and_nothing_is_hidden(seeded):
    """Ordering is the help. Hiding would be the bug — a session that
    photographed something unexpected still needs somewhere to put it."""
    from pipeline.services import attachments

    fc = attachments.categories_for("FC")
    assert [c["key"] for c in fc][:2] == ["fc_histogram", "fc_fcs"]
    assert len(fc) == len(attachments.categories())


def test_an_unknown_category_is_refused_with_the_ones_it_takes(seeded):
    from pipeline.services import attachments

    verdict = attachments.check(seeded["wb"], category="microscope_movie",
                                filename="a.tif", size_bytes=10,
                                member=seeded["member"])
    assert verdict["ok"] is False
    joined = " ".join(verdict["errors"])
    assert "microscope_movie" in joined
    assert "Ponceau S Stain" in joined


def test_a_file_past_the_ceiling_is_refused_with_the_ceiling(seeded):
    """"Too big" without the number is a refusal nobody can act on — the same
    rule `services/concentration.py` and `services/c_number.py` follow."""
    from pipeline.services import attachments

    verdict = attachments.check(
        seeded["wb"], category="wb_scan", filename="huge.tif",
        size_bytes=attachments.MAX_BYTES + 1, member=seeded["member"])
    assert verdict["ok"] is False
    assert str(attachments.max_mb()) in " ".join(verdict["errors"])


# ── whose session it is ─────────────────────────────────────────────────────

def test_another_sites_session_refuses_both_attaching_and_removing(seeded):
    from pipeline.models import FileAttachment
    from pipeline.services import attachments

    blocked = attachments.save(
        seeded["elsewhere"], seeded["upload"](), category="wb_scan",
        member=seeded["member"])
    assert blocked["ok"] is False
    assert "Att Theirs" in " ".join(blocked["errors"])

    # A file that already exists on their session is not this member's to
    # remove either. Written without a member, the way an import would.
    att = FileAttachment.objects.using(DB).create(
        session=seeded["elsewhere"], category="wb_scan",
        file="pipeline/attachments/x.tif", original_filename="x.tif")
    gone = attachments.remove(att.pk, member=seeded["member"])
    assert gone["ok"] is False
    assert FileAttachment.objects.using(DB).filter(pk=att.pk).exists()


def test_a_superuser_may_attach_to_any_site(seeded):
    from pipeline.services import attachments

    out = attachments.save(
        seeded["elsewhere"], seeded["upload"](), category="wb_scan",
        member=seeded["member"], is_superuser=True)
    assert out["ok"] is True, out


# ── somewhere to put it ─────────────────────────────────────────────────────
#
# This guard is dangerous in both directions, so both are pinned. Too quiet and
# a scientist's gel scan is destroyed by the next deploy with the row still
# claiming it exists; too eager and the feature is dead in production for a
# reason nobody can find.

def test_an_upload_is_refused_when_it_would_not_survive_a_deploy(seeded):
    from django.test import override_settings
    from pipeline.models import FileAttachment
    from pipeline.services import attachments

    with override_settings(DEBUG=False, USE_R2=False,
                           MEDIA_ROOT="/opt/render/project/src/media"):
        assert attachments.storage_refusal() != ""
        out = attachments.save(seeded["wb"], seeded["upload"](),
                               category="wb_scan", member=seeded["member"])
    assert out["ok"] is False
    assert FileAttachment.objects.using(DB).count() == 0
    # It reaches a bench scientist, so it names no setting they cannot change —
    # and it says the readings are unaffected, because the obvious fear on
    # reading a storage refusal is that the session went with it.
    message = " ".join(out["errors"])
    assert "USE_R2" not in message and "MEDIA_ROOT" not in message
    assert "readings" in message


def test_object_storage_and_dev_and_the_persistent_disk_all_allow_it(seeded):
    """The three ways a file is safe. Each was worth stating: a false refusal
    here takes the feature out in production and looks like a bug in the panel.
    """
    from django.test import override_settings
    from pipeline.services import attachments

    with override_settings(DEBUG=False, USE_R2=True):
        assert attachments.storage_refusal() == ""
    with override_settings(DEBUG=True, USE_R2=False, MEDIA_ROOT="/tmp/media"):
        assert attachments.storage_refusal() == ""
    with override_settings(DEBUG=False, USE_R2=False,
                           MEDIA_ROOT="/var/data/media"):
        assert attachments.storage_refusal() == ""


def test_the_greyed_button_gives_the_storage_reason_before_the_site_one(seeded):
    """One reader for both refusals, so the button's reason and the endpoint's
    cannot drift — and the one nobody can act on comes first."""
    from django.test import override_settings
    from pipeline.services import attachments

    with override_settings(DEBUG=False, USE_R2=False,
                           MEDIA_ROOT="/opt/render/project/src/media"):
        # Somebody else's session *and* nowhere to put it: the storage reason
        # wins, because correcting the other one would not help.
        why = attachments.attach_refusal(seeded["elsewhere"], seeded["member"])
        assert "lost" in why
    with override_settings(DEBUG=False, USE_R2=True):
        why = attachments.attach_refusal(seeded["elsewhere"], seeded["member"])
        assert "Att Theirs" in why
        assert attachments.attach_refusal(seeded["wb"], seeded["member"]) == ""


def test_attachments_are_public_only_until_their_own_bucket_is_set(seeded):
    """What `manage.py check` warns on. Not a refusal — the file is stored and
    downloadable either way; this is about who else can read it."""
    from django.test import override_settings
    from pipeline import storages

    with override_settings(USE_R2=True, R2_ATTACHMENTS_BUCKET=""):
        assert storages.attachments_are_public() is True
    with override_settings(USE_R2=True, R2_ATTACHMENTS_BUCKET="oga-lab-raw"):
        assert storages.attachments_are_public() is False
    with override_settings(USE_R2=False, R2_ATTACHMENTS_BUCKET=""):
        assert storages.attachments_are_public() is False


# ── the picker and the count, once a second surface asks for them ────────────


def test_the_row_picker_is_the_servers_answer_not_the_pages(seeded):
    """Which antibody a file may be filed against comes from here.

    The sessions board built this list by reading the result cards it had
    already drawn — no second fetch, which is right, and unusable the moment a
    second surface wants the same panel: a gene's page lists a session without
    ever drawing its results, so the picker would have offered "the whole
    session" and nothing else. A scan the scientist meant to file against one
    antibody, stored against the run, with no error and a record that looks
    perfectly plausible.

    It offers **this** session's rows only. Nothing in the schema stops a file
    being filed against another session's result, which is why `_result_refusal`
    exists; offering one in the picker is how a person would reach for it.
    """
    from pipeline.services import attachments

    opts = attachments.result_options(seeded["wb"])
    assert [o["id"] for o in opts] == [seeded["row"].pk]
    # Labelled by the antibody, the same words `session_board.results_for` gives
    # its cards — a row named one way here and another there is one row a reader
    # cannot match up.
    assert "ATT-1" in opts[0]["label"]

    # The other session's row is the other session's.
    assert [o["id"] for o in attachments.result_options(seeded["other"])] == [
        seeded["other_row"].pk]


def test_a_session_with_no_procedure_offers_no_rows(seeded):
    """Which of the four result FKs a file lands in comes from the procedure, so
    with none recorded there is nothing to offer. Empty, never a guess."""
    from pipeline.models import ExperimentSession
    from pipeline.services import attachments

    seeded["wb"].procedure_type = ""
    seeded["wb"].save(using=DB)
    fresh = ExperimentSession.objects.using(DB).get(pk=seeded["wb"].pk)
    assert attachments.result_options(fresh) == []


def test_the_count_a_page_draws_is_the_files_that_are_there(seeded):
    """A panel that counts must list what it counted.

    The gene page draws a per-session file count beside a button that opens the
    panel listing them, and the two must be one fact. It is its own query rather
    than a fifth `Count` on `views/dashboard.py`'s annotate: joining a second
    multi-valued relation alongside the four result counts multiplies both, so a
    session with 3 results and 2 files would report 6 of each.
    """
    from pipeline.services import attachments

    wb, other = seeded["wb"], seeded["other"]
    assert attachments.counts_for([wb.pk, other.pk]) == {}

    attachments.save(wb, seeded["upload"](filename="a.tif"), category="wb_scan",
                     member=seeded["member"])
    attachments.save(wb, seeded["upload"](filename="b.tif"), category="ponceau",
                     member=seeded["member"])
    attachments.save(other, seeded["upload"](filename="c.tif"),
                     category="wb_scan", member=seeded["member"])

    counts = attachments.counts_for([wb.pk, other.pk, seeded["elsewhere"].pk])
    assert counts.get(wb.pk) == 2
    assert counts.get(other.pk) == 1
    # A session with none is absent rather than zero; the caller defaults.
    assert seeded["elsewhere"].pk not in counts

    # And it is what the panel goes on to list, not a second answer about it.
    assert len(attachments.rows_for(wb)) == 2

    assert attachments.counts_for([]) == {}


def test_the_count_is_one_query_however_many_sessions(seeded):
    """Cost is pinned as "does not grow with the row count" — a gene's page
    lists every session it has, and this is drawn beside all of them."""
    from django.db import connections
    from django.test.utils import CaptureQueriesContext
    from pipeline.services import attachments

    ids = [seeded["wb"].pk, seeded["other"].pk, seeded["elsewhere"].pk]
    with CaptureQueriesContext(connections[DB]) as few:
        attachments.counts_for(ids[:1])
    with CaptureQueriesContext(connections[DB]) as many:
        attachments.counts_for(ids)
    assert len(many) == len(few) == 1


def test_a_refusal_names_the_act_it_is_refusing(seeded):
    """The Attach button used to say "nothing is yours to delete".

    `write_refusal` returned `deletion.site_refusal`'s wording verbatim, and that
    wording is written for deleting — so a greyed **Attach** button described an
    act that is not on the screen, which reads as the wrong record having been
    picked rather than as a permission problem. The rule is still one rule in
    `deletion`; only the verb moves.
    """
    from pipeline.services import attachments

    attaching = attachments.attach_refusal(seeded["elsewhere"], seeded["member"])
    assert attaching, "somebody else's session should be refused"
    assert "delete" not in attaching.lower(), attaching
    assert "add files to" in attaching or "attach" in attaching, attaching

    removing = attachments.write_refusal(
        seeded["elsewhere"], seeded["member"],
        action="remove files from", su_verb="remove one from")
    assert "remove" in removing.lower(), removing

    # And the rule itself is unchanged: your own bench's session is still fine.
    assert attachments.attach_refusal(seeded["wb"], seeded["member"]) == ""


def test_deleting_keeps_its_own_wording(seeded):
    """The defaults are what every existing caller gets, unchanged."""
    from pipeline.services import deletion

    why = deletion.site_refusal("session", seeded["elsewhere"], seeded["member"], False)
    assert "delete" in why.lower(), why
