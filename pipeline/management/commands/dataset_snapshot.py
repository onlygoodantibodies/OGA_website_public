"""Write a daily full capture of the scientific dataset, and optionally email it.

The owner asked for *"a full capture json of the dataset every 24 hours … have
that available as latest snapshot as default on that page — with the option for
a superuser to be able to set up automated email of that json. In the first
instance it should send to hsv6@leicester.ac.uk."*

    python manage.py dataset_snapshot                      # write one
    python manage.py dataset_snapshot --email hsv6@leicester.ac.uk
    python manage.py dataset_snapshot --email … --attach json   # past a mail filter
    python manage.py dataset_snapshot --dry-run            # say what it would do

Run it from a **Render Cron Job**, not from the web service: building it walks
every scientific table, and doing that behind a request would sit inside
gunicorn's worker timeout with the site's own traffic. The roadmap's action
register carries the exact settings.

**Where it goes.** Into ``pipeline_db``, as a ``DatasetSnapshot`` row, keeping
the newest ``KEEP``. It used to go to ``/var/data/snapshots`` and that was wrong
for the one place it actually runs: Render cron jobs **cannot have a disk**, and
a disk is "accessible by only a single service instance", so there is no
filesystem the cron job and the web service can both see. Written to a path on
cron it lands in a container that is torn down when the process exits — built,
emailed, gone — which is why the page's Latest snapshot panel could never fill.
``snapshot_models.py`` carries the reasoning.

``--dir`` still writes files, and is now **opt-in**: it is for a Shell session or
a local run where you want the artefact on disk. Nothing writes there by default,
so there is one answer to "where is the newest snapshot" rather than two stores
that can disagree.

**Two copies, and they do different jobs.** The row is what the page serves; the
**email is the off-platform one**, and it is the actual backup — a capture living
in the database it captures is a convenience, not a safety net.

Gzipped on disk. A capture of every reading is not small, and the page serves it
decompressed.

**Email is best-effort and never fatal.** ``EMAIL_HOST_PASSWORD`` is absent in
some environments by design (``settings.py``: "Empty fallback = email disabled,
site stays up"), so a send that cannot happen is reported and the snapshot still
lands. A cron job that failed because the mail server was busy would be a cron
job nobody trusts.

**Sending it is not the same as it arriving**, and only one of those is in this
file's gift. Leicester's filter treated the first real `.json.gz` as dangerous —
an archive from an unfamiliar sender is exactly what an institutional filter
quarantines — while the send succeeded and the log went green. That is the
off-platform backup silently ceasing to exist, which is the failure this whole
module is written against. ``--attach json`` sends the same capture as plain
text: ~22× the bytes and still well inside Gmail's 25 MB, and filters treat it
very differently. ``--attach none`` sends the counts alone. The body names the
page either way, because a stripped attachment looks identical to one that was
never sent.
"""
from __future__ import annotations

import gzip
import os
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand

from pipeline.services import snapshot as snap

KEEP = 14
PREFIX = "oga_pipeline_snapshot_"


def source() -> tuple[str, bool]:
    """Which database this capture was read from, and whether it is a real one.

    Printed on every run, and carried in the email, because the way this command
    fails on Render is **silently**. ``PIPELINE_DATABASE_URL`` has a local SQLite
    fallback (``settings.py``), so a cron job created without it does not crash:
    it captures an empty database in a fresh container, stores it, emails it, and
    **exits 0**. Render shows the job green, and what lands in the inbox looks
    exactly like a backup. A file that looks like a backup and is not one is the
    most dangerous artefact this app could produce — so the log and the email
    both say which database answered, rather than leaving it to be inferred from
    a row count nobody has memorised.
    """
    from django.db import connections

    d = connections[snap.DB].settings_dict
    engine = (d.get("ENGINE") or "").rsplit(".", 1)[-1]
    if engine == "sqlite3":
        return f"SQLite {d.get('NAME')}", False
    host = d.get("HOST") or "localhost"
    return f"{engine} {d.get('NAME')} at {host}", True


class Command(BaseCommand):
    help = "Write a full-capture JSON snapshot of the scientific dataset."

    def add_arguments(self, parser):
        parser.add_argument("--email", action="append", default=[], metavar="ADDR",
                            help="Send the snapshot to this address. Repeatable.")
        parser.add_argument("--dir", default="",
                            help="Also write the file to this directory. Opt-in — "
                                 "for a Shell or local run that wants the artefact "
                                 "on disk. The stored copy is in the database.")
        parser.add_argument("--keep", type=int, default=KEEP,
                            help=f"How many snapshots to retain (default {KEEP}).")
        parser.add_argument("--attach", choices=["gzip", "json", "none"],
                            default="gzip",
                            help="What the email carries. 'gzip' is smallest; "
                                 "'json' is plain text, which institutional mail "
                                 "filters accept where they quarantine an "
                                 "archive; 'none' sends the counts and no file. "
                                 "Default gzip.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Build it and report, but write nothing and send nothing.")

    def handle(self, *args, **opts):
        read_from, is_live = source()
        self.stdout.write(f"Read from pipeline_db: {read_from}")
        if not is_live:
            self.stdout.write(self.style.WARNING(
                "  That is the local development fallback, not the live "
                "database. On Render this means PIPELINE_DATABASE_URL is not "
                "set on this service, and the capture below is of an empty "
                "database."))

        payload = snap.build()
        text = snap.to_json(payload)
        manifest = payload["manifest"]

        self.stdout.write(snap.summary_line(payload))
        for name, note in sorted(manifest["omitted"].items()):
            self.stdout.write(f"  not captured: {name} — {note}")
        if manifest["not_found"]:
            self.stdout.write(self.style.WARNING(
                f"  named but not found: {', '.join(manifest['not_found'])}"))

        taken = datetime.now(timezone.utc)
        name = f"{PREFIX}{taken.strftime('%Y%m%dT%H%M%SZ')}.json.gz"
        raw = text.encode("utf-8")
        blob = gzip.compress(raw)

        if opts["dry_run"]:
            where = f", and to {opts['dir']}" if opts["dir"] else ""
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — would store {name} "
                f"({len(blob):,} bytes gzipped, {len(raw):,} raw) in the "
                f"database{where}, and nothing was sent."))
            return

        keep = max(1, opts["keep"])

        # The store the page reads. Not best-effort: keeping it is the job, so a
        # failure here should fail the run loudly rather than exit clean having
        # kept nothing.
        snap.store(name, blob, payload, taken)
        self.stdout.write(self.style.SUCCESS(
            f"Stored {name} ({len(blob):,} bytes gzipped) in pipeline_db"))
        for gone in snap.prune(keep):
            self.stdout.write(f"Removed old snapshot {gone}")

        if opts["dir"]:
            self._write_file(Path(opts["dir"]), name, blob, keep)

        for address in opts["email"]:
            self._send(address, name, blob, raw, payload, opts["attach"])

    def _write_file(self, out_dir, name, blob, keep):
        """The opt-in artefact on disk. Nothing reads it — it is for a person."""
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        path.write_bytes(blob)
        self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
        # A directory that only grows is a disk that fills.
        for old in sorted(out_dir.glob(f"{PREFIX}*.json.gz"), reverse=True)[keep:]:
            os.remove(old)
            self.stdout.write(f"Removed old file {old.name}")

    def _send(self, address, name, blob, raw, payload, attach):
        """Best-effort. A snapshot that landed is worth more than a clean exit.

        ``attach`` decides what travels, because **the recipient's mail filter
        gets a vote and it is not the sender's to overrule.** A `.json.gz` is an
        archive, and an archive from an unfamiliar sender is what institutional
        filters quarantine — so the off-platform copy stops arriving while every
        log stays green, which is this file's own worst case wearing a different
        coat. Plain JSON is ~22× larger and far more likely to be delivered;
        that trade is the recipient's to make, so it is a flag rather than a
        default nobody can see.
        """
        if not getattr(settings, "EMAIL_HOST_PASSWORD", ""):
            self.stdout.write(self.style.WARNING(
                f"Not emailing {address}: EMAIL_HOST_PASSWORD is not set in this "
                f"environment, so mail is disabled. The snapshot was still written."))
            return
        m = payload["manifest"]
        carried = {
            "gzip": (name, blob, "application/gzip"),
            "json": (name.replace(".json.gz", ".json"), raw, "application/json"),
            "none": None,
        }[attach]
        body = "\n".join([
            f"OGA pipeline snapshot — {snap.summary_line(payload)}.",
            "",
            # An attachment that travels says what it is a capture *of*. The
            # inbox is the one place this file is read with no log beside it.
            f"Read from: {source()[0]}",
            # Always, not only when nothing is attached: a stripped attachment
            # looks identical to one that was never sent, and the reader needs
            # somewhere to go either way.
            "The same capture is on Downloads & uploads in the pipeline, under "
            "Latest snapshot.",
            "",
            "Tables:",
            *[f"  {k}: {v:,}" for k, v in sorted(m["tables"].items())],
            "",
            "Not captured:",
            *[f"  {k} — {v}" for k, v in sorted(m["omitted"].items())],
            "",
            m["restore_note"],
        ])
        try:
            msg = EmailMessage(
                subject=f"OGA pipeline snapshot — {snap.summary_line(payload)}",
                body=body, to=[address])
            if carried:
                msg.attach(*carried)
            msg.send(fail_silently=False)
            told = (f"carrying {carried[0]} ({len(carried[1]):,} bytes)"
                    if carried else "with no attachment")
            self.stdout.write(self.style.SUCCESS(f"Emailed {address} {told}"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(
                f"Could not email {address}: {e}. The snapshot was still written."))
