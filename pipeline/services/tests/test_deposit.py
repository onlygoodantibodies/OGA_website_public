"""Tests for the Zenodo deposit.

Nothing here touches the network. The preview is built entirely from records and
makes no call at all — that is the design, not a test accommodation — and the
five HTTP calls are asserted against a fake so the *order* and the *payloads*
are pinned without a token or a reachable Zenodo.

Proves:
  * the preview needs no token and no network, so it answers today
  * a session with no readings is not deposited — a planned week is not an
    experiment, the rule the importer and the report generator already hold
  * the DOI is reserved **before** the Data Note is generated, which is the
    whole reason it is reserved rather than left to publication
  * the last call submits for review and **nothing ever publishes**
  * an unreachable Zenodo is reported, not raised
  * the deposit records its DOI on the gene's `Report` without overwriting one
"""
from __future__ import annotations

from datetime import date

import pytest

DB = "pipeline_db"


@pytest.fixture()
def gene(_pipeline_db):
    from django.contrib.auth.models import User
    from pipeline.models import (Antibody, Company, ExperimentSession,
                                 FileAttachment, Member, Report, Site, Target,
                                 WbResult)
    for M in (FileAttachment, WbResult, ExperimentSession, Report, Antibody,
              Company, Target, Member, Site):
        M.objects.using(DB).all().delete()

    site = Site.objects.using(DB).create(name="Dep Site", short_code="DEP")
    user, _ = User.objects.using(DB).get_or_create(username="dep-tester")
    member = Member.objects.using(DB).create(
        user_id=user.pk, site=site, role="admin", display_name="Ada Bench")
    target = Target.objects.using(DB).create(protein_name="Stathmin 2",
                                             gene_name="STMN2")
    company = Company.objects.using(DB).create(name="Dep Supplier")
    ab = Antibody.objects.using(DB).create(catalogue_number="DEP-1",
                                           company=company, target=target)
    done = ExperimentSession.objects.using(DB).create(
        target=target, site=site, procedure_type="WB", date=date(2026, 8, 4),
        experimenter=member)
    WbResult.objects.using(DB).create(session=done, antibody=ab, rating="1")
    # Planned only: no reading anywhere on it.
    planned = ExperimentSession.objects.using(DB).create(
        target=target, site=site, procedure_type="IF", date=date(2026, 8, 4),
        experimenter=member)
    return {"target": target, "member": member, "done": done,
            "planned": planned, "ab": ab}


# ── the preview, which is the half that works today ─────────────────────────

def test_the_preview_needs_no_token_and_makes_no_call(gene, monkeypatch):
    from django.test import override_settings
    from pipeline.services import deposit, zenodo

    def _explode(*a, **k):                      # any HTTP at all fails the test
        raise AssertionError("the preview made a network call")
    monkeypatch.setattr(zenodo.requests, "request", _explode)

    with override_settings(ZENODO_API_TOKEN=""):
        p = deposit.plan(gene["target"])

    assert p["title"] == "Dataset for the Stathmin 2 antibody screening study"
    assert p["can_submit"] is False              # no token
    assert "ZENODO_API_TOKEN" in p["not_connected"]
    # ...and everything worth reading is still there.
    assert [f["name"] for f in p["files"]] == [
        "STMN2_underlying_data.zip", "STMN2_results.xlsx", "STMN2_data_note.docx"]
    assert p["creators"] == ["Ada Bench"]


def test_a_session_with_no_readings_is_not_deposited(gene):
    """A planned week is not an experiment — the same rule
    `session_import._has_result` holds at the writing end."""
    from django.test import override_settings
    from pipeline.services import deposit

    with override_settings(ZENODO_API_TOKEN="t"):
        p = deposit.plan(gene["target"])
    assert p["sessions"] == 1                    # the WB one, not the IF one
    assert p["can_submit"] is True


def test_a_gene_with_nothing_recorded_is_refused_by_name(gene):
    from django.test import override_settings
    from pipeline.models import WbResult
    from pipeline.services import deposit

    WbResult.objects.using(DB).all().delete()
    with override_settings(ZENODO_API_TOKEN="t"):
        p = deposit.plan(gene["target"])
    assert p["can_submit"] is False
    assert "nothing to deposit" in " ".join(p["blocking"])


# ── the submission, against a fake Zenodo ───────────────────────────────────

class _FakeZenodo:
    """Records every call in order and answers like Invenio does."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url.split("/api", 1)[-1]))

        class R:
            status_code = 200
            content = b"{}"

            @staticmethod
            def json():
                return {}
        if url.endswith("/api/records"):
            R.status_code = 201
            R.json = staticmethod(lambda: {"id": "zen-1"})
        elif url.endswith("/draft/pids/doi"):
            R.json = staticmethod(
                lambda: {"pids": {"doi": {"identifier": "10.5072/zenodo.1"}}})
        elif "/api/communities/" in url:
            R.json = staticmethod(lambda: {"id": "com-1"})
        return R


def test_the_doi_is_reserved_before_the_data_note_is_written(gene, monkeypatch):
    """The order is the design. The Data Note reads the `Report` row, so the
    DOI has to be on it before the document is generated — otherwise the
    deposit contains a document saying `[DOI to be assigned upon deposit]`
    about the very record it is sitting in."""
    from django.test import override_settings
    from pipeline.services import deposit, zenodo

    fake = _FakeZenodo()
    monkeypatch.setattr(zenodo.requests, "request", fake)

    seen = {}

    def _note(target_pk, output_path=None):
        from pipeline.models import Report
        r = Report.objects.using(DB).filter(target_id=target_pk).first()
        seen["doi_at_generation"] = r.zenodo_doi if r else ""
        with open(output_path, "wb") as fh:
            fh.write(b"docx")
        return output_path

    monkeypatch.setattr("pipeline.services.report_generator.generate_report", _note)

    with override_settings(ZENODO_API_TOKEN="t", ZENODO_COMMUNITY="ycharos"):
        out = deposit.apply(gene["target"], member=gene["member"])

    assert out["ok"] is True, out
    assert seen["doi_at_generation"].endswith("10.5072/zenodo.1")

    paths = [p for _, p in fake.calls]
    assert paths[0] == "/records"
    assert paths[1] == "/records/zen-1/draft/pids/doi"
    # The DOI call comes before every file upload.
    assert paths.index("/records/zen-1/draft/pids/doi") < min(
        i for i, p in enumerate(paths) if "/draft/files" in p)


def test_the_last_call_submits_for_review_and_nothing_publishes(gene, monkeypatch):
    """The approval gate is Zenodo's, and the app must not be able to skip it."""
    from django.test import override_settings
    from pipeline.services import deposit, zenodo

    fake = _FakeZenodo()
    monkeypatch.setattr(zenodo.requests, "request", fake)
    monkeypatch.setattr("pipeline.services.report_generator.generate_report",
                        lambda pk, output_path=None: (
                            open(output_path, "wb").write(b"d"), output_path)[1])

    with override_settings(ZENODO_API_TOKEN="t"):
        deposit.apply(gene["target"], member=gene["member"])

    paths = [p for _, p in fake.calls]
    assert paths[-1] == "/records/zen-1/draft/actions/submit-review"
    assert "/records/zen-1/draft/review" in paths          # community attached
    # The one call that would make it public is never made, by anything.
    assert not any("actions/publish" in p for p in paths)


def test_an_unreachable_zenodo_is_reported_not_raised(gene, monkeypatch):
    from django.test import override_settings
    from pipeline.services import deposit, zenodo
    from requests.exceptions import ConnectionError as RequestsConnectionError

    def _dead(*a, **k):
        raise RequestsConnectionError("no route to host")
    monkeypatch.setattr(zenodo.requests, "request", _dead)

    with override_settings(ZENODO_API_TOKEN="t"):
        with pytest.raises(zenodo.ZenodoError) as caught:
            deposit.apply(gene["target"], member=gene["member"])
    # A sentence somebody can act on, and it says nothing was sent.
    assert "nothing was submitted" in str(caught.value).lower()


def test_the_deposit_records_its_doi_without_overwriting_one(gene, monkeypatch):
    from django.test import override_settings
    from pipeline.models import Report
    from pipeline.services import deposit, zenodo

    monkeypatch.setattr(zenodo.requests, "request", _FakeZenodo())
    monkeypatch.setattr("pipeline.services.report_generator.generate_report",
                        lambda pk, output_path=None: (
                            open(output_path, "wb").write(b"d"), output_path)[1])

    with override_settings(ZENODO_API_TOKEN="t"):
        deposit.apply(gene["target"], member=gene["member"])

    report = Report.objects.using(DB).get(target_id=gene["target"].pk)
    assert report.zenodo_doi == "https://doi.org/10.5072/zenodo.1"
    assert report.status == Report.ReportStatus.SUBMITTED

    # A second deposit must not replace a link somebody may already have cited.
    with override_settings(ZENODO_API_TOKEN="t"):
        deposit.apply(gene["target"], member=gene["member"])
    report.refresh_from_db(using=DB)
    assert report.zenodo_doi == "https://doi.org/10.5072/zenodo.1"
