"""The daily full capture.

The owner asked for *"a full capture json of the dataset every 24 hours"*. The
obvious way to build it — `dataset.build_json` — is not one: it covers ten of
the thirty-three pipeline models and omits `ExperimentSession`, all four result
tables and `TargetNomination`. That is every reading, and the only record of
which site is pursuing which gene. A file headed "full capture" containing no
readings is worse than no file, because it is what somebody reaches for on the
worst day.

So what is pinned here is the coverage, the honesty of the manifest, and the
refusal to imply it can restore anything.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from pipeline.models import (Antibody, Company, DatasetSnapshot,
                             ExperimentSession, Member, Site, Target,
                             TargetNomination, WbResult)
from pipeline.services import snapshot
from pipeline.tests_timeouts import DB, _member_client


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


class ItCapturesTheThingsTheLabAuthorsTests(TestCase):
    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)
        self.member = Member.objects.using(DB).get(site_id=self.site.pk)
        self.target = Target.objects.using(DB).create(gene_name="SNCA")
        TargetNomination.objects.using(DB).create(
            target_id=self.target.pk, site_id=self.site.pk, funded=True)
        company = Company.objects.using(DB).create(name="Abcam")
        ab = Antibody.objects.using(DB).create(
            target_id=self.target.pk, company_id=company.pk,
            catalogue_number="ab1", site_id=self.site.pk)
        session = ExperimentSession.objects.using(DB).create(
            target_id=self.target.pk, procedure_type="WB", date="2026-08-01",
            site_id=self.site.pk, experimenter_id=self.member.pk)
        WbResult.objects.using(DB).create(session_id=session.pk, antibody_id=ab.pk,
                                          comments="a reading")

    def test_every_reading_is_in_it(self):
        """The thing the existing family export omits entirely."""
        tables = snapshot.build()["tables"]
        for name in ("ExperimentSession", "WbResult", "IpResult", "IfResult",
                     "FcResult"):
            self.assertIn(name, tables, name)
        self.assertEqual(len(tables["WbResult"]), 1)
        self.assertEqual(tables["WbResult"][0]["comments"], "a reading")

    def test_nominations_are_in_it(self):
        """The only record of which site is pursuing which gene."""
        rows = snapshot.build()["tables"]["TargetNomination"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site_id"], self.site.pk)
        self.assertTrue(rows[0]["funded"])

    def test_it_is_json_serialisable(self):
        """Decimals and dates reach a file, not a TypeError."""
        json.loads(snapshot.to_json(snapshot.build()))


class TheManifestSaysWhatIsMissingTests(TestCase):
    databases = {"default", DB}

    def test_omissions_are_named_not_dropped(self):
        """A reader must be able to tell "not captured" from "none on file"."""
        m = snapshot.build()["manifest"]
        for name in ("DepMapExpression", "ProteomicsExpression", "HorizonKoLine",
                     "CropperSession", "Member", "DatasetSnapshot"):
            self.assertIn(name, m["omitted"], name)
            self.assertTrue(m["omitted"][name], f"{name} has no reason given")

    def test_a_capture_never_contains_the_stored_captures(self):
        """Otherwise each day's file nests the one before it and the size
        doubles every night — silent, and only visible weeks later as a table
        nobody can query."""
        snapshot.store("oga_pipeline_snapshot_20260803T031700Z.json.gz",
                       b"yesterday", {"manifest": {"row_total": 1}}, _utc(2026, 8, 3))
        built = snapshot.build()
        self.assertNotIn("DatasetSnapshot", built["tables"])
        self.assertNotIn("yesterday", snapshot.to_json(built))

    def test_it_does_not_claim_to_restore(self):
        m = snapshot.build()["manifest"]
        self.assertIs(m["restores"], False)
        self.assertIn("Recovery", m["restore_note"])

    def test_every_model_is_either_captured_or_named_as_omitted(self):
        """The module's own rule — everything unless there is a reason, and the
        reasons are on the file — enforced rather than restated.

        Nothing checked it, so a model added later simply fell out of the daily
        capture: no error, no gap in the manifest, and the absence only visible
        on the day somebody opens the file looking for the table. That is how
        ``PendingPublicationImage`` — a whole afternoon's cropping that has not
        been released and exists nowhere else — would have been left out.
        """
        from django.apps import apps

        known = set(snapshot.CAPTURED) | set(snapshot.OMITTED)
        models = {m.__name__ for m in apps.get_app_config("pipeline").get_models()}
        self.assertEqual(
            set(), models - known,
            "these pipeline models are neither captured nor named as omitted, "
            "so they are missing from the backup with nothing saying so")

    def test_it_counts_every_table_it_captured(self):
        m = snapshot.build()["manifest"]
        self.assertEqual(set(m["tables"]), set(snapshot.CAPTURED))
        self.assertEqual(m["row_total"], sum(m["tables"].values()))

    def test_no_account_rows_travel_in_a_file_that_gets_emailed(self):
        tables = snapshot.build()["tables"]
        self.assertNotIn("Member", tables)
        blob = snapshot.to_json(snapshot.build())
        self.assertNotIn("password", blob)


class TheCommandTests(TestCase):
    databases = {"default", DB}

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()

    def _run(self, *extra):
        out = StringIO()
        call_command("dataset_snapshot", "--dir", self.dir, *extra, stdout=out)
        return out.getvalue()

    def test_the_dry_run_writes_nothing(self):
        text = self._run("--dry-run")
        self.assertIn("DRY RUN", text)
        self.assertEqual(list(Path(self.dir).glob("*.json.gz")), [])
        self.assertEqual(DatasetSnapshot.objects.using(DB).count(), 0)

    def test_it_stores_the_capture_where_both_services_can_reach_it(self):
        """A cron job has no disk and cannot share the web service's, so the
        row is the only copy the page can serve."""
        self._run()
        row = DatasetSnapshot.objects.using(DB).get()
        self.assertEqual(json.loads(gzip.decompress(bytes(row.blob)))["manifest"]
                         ["format_version"], snapshot.FORMAT_VERSION)
        self.assertEqual(row.byte_size, len(bytes(row.blob)))
        self.assertEqual(row.table_count, len(snapshot.CAPTURED))

    def test_stored_snapshots_are_pruned_too(self):
        """A table that only grows is the same failure as a directory that
        only grows — and this one is inside the database being captured.

        Seeded by hand because the name carries the time to the second, so four
        runs in one test second are one snapshot, not four.
        """
        for day in (1, 2, 3, 4):
            snapshot.store(f"oga_pipeline_snapshot_2026080{day}T031700Z.json.gz",
                           b"x", {"manifest": {}}, _utc(2026, 8, day))
        self._run("--keep", "2")
        kept = set(DatasetSnapshot.objects.using(DB)
                   .values_list("name", flat=True))
        self.assertEqual(len(kept), 2)
        # The newest survives, and today's run is the newest of all.
        self.assertNotIn("oga_pipeline_snapshot_20260801T031700Z.json.gz", kept)

    def test_running_it_twice_in_one_second_replaces_rather_than_raising(self):
        """The name is second-granular, so a person pressing the line twice in
        a Shell used to get an IntegrityError traceback.

        **The clock is frozen, because otherwise this test is a coin toss.**
        It ran the command twice and asserted the two collapse to one row —
        which is true only when both land inside the same wall-clock second.
        Locally they do; on a loaded CI runner they can straddle the boundary,
        and then the two names genuinely differ, two rows is the correct answer,
        and the test fails having found nothing wrong. It did exactly that on
        9 Aug 2026. What the test is *about* is two runs that produce the same
        name replacing rather than raising, so pinning the name is pinning the
        thing under test rather than working around it.
        """
        frozen = datetime(2026, 8, 9, 3, 17, 0, tzinfo=timezone.utc)
        with mock.patch(
                'pipeline.management.commands.dataset_snapshot.datetime') as clock:
            clock.now.return_value = frozen
            self._run()
            self._run()
        self.assertEqual(DatasetSnapshot.objects.using(DB).count(), 1)

    def test_nothing_lands_on_disk_unless_asked(self):
        """`--dir` is opt-in: the default run has one store, so the page and the
        command cannot disagree about which snapshot is newest."""
        out = StringIO()
        call_command("dataset_snapshot", stdout=out)
        self.assertEqual(list(Path(self.dir).glob("*.json.gz")), [])
        self.assertEqual(DatasetSnapshot.objects.using(DB).count(), 1)

    def test_it_writes_a_readable_gzipped_capture(self):
        self._run()
        files = list(Path(self.dir).glob("*.json.gz"))
        self.assertEqual(len(files), 1)
        payload = json.loads(gzip.decompress(files[0].read_bytes()))
        self.assertIn("manifest", payload)
        self.assertIn("tables", payload)

    def test_it_names_what_it_left_out_on_the_way_past(self):
        self.assertIn("not captured: DepMapExpression", self._run("--dry-run"))

    def test_it_says_which_database_it_read(self):
        """The way this fails on Render is silently: with no
        PIPELINE_DATABASE_URL it captures the empty SQLite fallback, stores it,
        emails it and exits 0, so the job goes green over an empty backup."""
        text = self._run("--dry-run")
        self.assertIn("Read from pipeline_db:", text)
        # The suite runs on the SQLite fallback, which is the case worth naming.
        self.assertIn("local development fallback", text)

    def test_old_snapshots_are_pruned(self):
        """A directory that only grows is a disk that fills and takes the site."""
        for _ in range(4):
            self._run("--keep", "2")
        self.assertLessEqual(len(list(Path(self.dir).glob("*.json.gz"))), 2)

    @override_settings(EMAIL_HOST_PASSWORD="x")
    def test_what_the_email_carries_is_chosen_not_assumed(self):
        """Sending is not arriving. An archive from an unfamiliar sender is what
        an institutional filter quarantines, and the send still reports success —
        the off-platform copy ceasing to exist with every log green."""
        from django.core import mail

        self._run("--email", "a@b.c")
        self.assertEqual(mail.outbox[-1].attachments[0][0][-8:], ".json.gz")

        self._run("--email", "a@b.c", "--attach", "json")
        name, content, mime = mail.outbox[-1].attachments[0]
        self.assertTrue(name.endswith(".json"))
        self.assertEqual(mime, "application/json")
        self.assertIn("manifest", json.loads(content))

        self._run("--email", "a@b.c", "--attach", "none")
        self.assertEqual(mail.outbox[-1].attachments, [])

    @override_settings(EMAIL_HOST_PASSWORD="x")
    def test_the_body_names_the_page_even_when_the_file_is_stripped(self):
        """A quarantined attachment looks identical to one never sent."""
        from django.core import mail

        self._run("--email", "a@b.c", "--attach", "none")
        self.assertIn("Downloads & uploads", mail.outbox[-1].body)

    @override_settings(EMAIL_HOST_PASSWORD="")
    def test_email_that_cannot_be_sent_never_loses_the_snapshot(self):
        """`settings.py`: an empty password means mail is disabled and the site
        stays up. A cron job that failed because mail was off would be one
        nobody trusts."""
        text = self._run("--email", "hsv6@leicester.ac.uk")
        self.assertIn("Not emailing", text)
        self.assertEqual(len(list(Path(self.dir).glob("*.json.gz"))), 1)


class ThePagePresentsItHonestlyTests(TestCase):
    databases = {"default", DB, "academy_db"}

    def setUp(self):
        self.site = Site.objects.using(DB).create(name="Leicester", short_code="LEI")
        self.client = _member_client(self, self.site)

    def test_with_no_snapshot_it_says_so_rather_than_showing_nothing(self):
        body = self.client.get("/pipeline/data/").content.decode()
        self.assertIn("No snapshot has been taken yet", body)

    def test_it_never_calls_itself_a_backup(self):
        body = self.client.get("/pipeline/data/").content.decode()
        self.assertIn("capture, not a restore", body)

    def test_the_download_says_why_when_there_is_nothing_to_download(self):
        resp = self.client.get("/pipeline/data/snapshot/")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("daily cron job", resp.content.decode())

    def test_the_panel_shows_a_stored_capture(self):
        """The whole point of storing it in the database: the cron job writes
        in one service and this page reads in another."""
        call_command("dataset_snapshot", stdout=StringIO())
        body = self.client.get("/pipeline/data/").content.decode()
        self.assertNotIn("No snapshot has been taken yet", body)
        self.assertIn("rows across", body)

    def test_the_download_serves_the_stored_capture_decompressed(self):
        call_command("dataset_snapshot", stdout=StringIO())
        resp = self.client.get("/pipeline/data/snapshot/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("manifest", json.loads(resp.content))
        self.assertTrue(resp["Content-Disposition"].endswith('.json"'))
