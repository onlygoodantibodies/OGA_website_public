"""Where the daily full capture is kept, so both services can reach it.

Split out of ``models.py`` for the same reason ``cropper_models.py`` is — one
feature, one file, no parallel edits to the big module.

**Why the database and not a file.** The capture is written by a Render **Cron
Job** and read by the **web service**, and those are two services. Render's disks
cannot be attached to a cron job at all, and a disk is "accessible by only a
single service instance", so there is no filesystem both of them can see. Written
to ``/var/data/snapshots`` from cron it lands in a container that is torn down
when the process exits: built, emailed, and gone. That is why the Latest snapshot
panel on Downloads & uploads sat permanently on *"No snapshot has been taken
yet."* — it was reading a disk nothing could write to.

``pipeline_db`` is the one store both services already reach, over Render's
private network, with no new credentials and no new service. The email remains
the **off-platform** copy and the real backup; this row is what makes the newest
one downloadable from the page.

**It is never captured by the thing that captures everything else** — see
``services/snapshot.py::OMITTED``. A capture that contained yesterday's captures
would double in size every day, and the gzipped blob is the one field in this
database that is already a copy of the rest of it.
"""
from __future__ import annotations

from django.db import models


class DatasetSnapshot(models.Model):
    """One gzipped full capture, with the manifest's headline numbers beside it.

    The counts are stored rather than read back out of the blob so the page can
    say what is in a snapshot without decompressing and parsing several MB to
    draw one line of text.
    """

    name = models.CharField(
        max_length=120, unique=True,
        help_text="Filename this capture is served as, e.g. "
                  "oga_pipeline_snapshot_20260804T031700Z.json.gz",
    )
    taken = models.DateTimeField(db_index=True)
    blob = models.BinaryField(help_text="The capture, gzipped.")
    byte_size = models.PositiveIntegerField(help_text="Size of the gzipped blob.")

    # Straight off the manifest, so the page needs no decompression to describe
    # what it is offering.
    row_total = models.PositiveIntegerField(default=0)
    table_count = models.PositiveIntegerField(default=0)
    format_version = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-taken"]
        verbose_name = "dataset snapshot"

    def __str__(self):
        return f"{self.name} ({self.row_total:,} rows)"
