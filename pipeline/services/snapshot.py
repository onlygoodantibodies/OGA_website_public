"""A full capture of the scientific dataset, as JSON.

The owner asked for *"a full capture json of the dataset every 24 hours"*, and
the obvious way to build it — `services/dataset.py::build_json` — is not one.
That export covers **ten of the thirty-three** pipeline models, and what it omits
is the part the pipeline exists for: `ExperimentSession` and all four result
tables, so **every reading**, and `TargetNomination`, the only record of which
site is pursuing which gene. A file headed "full capture" that contains no
readings is worse than no file, because it is the thing somebody reaches for on
the worst day.

So this is a separate reader, and its rule is the opposite of `dataset.py`'s:
**everything, unless there is a reason to leave it out, and the reasons are
listed on the file itself.**

What is deliberately out, and why:

* ``DepMapExpression`` (461,162 rows), ``ProteomicsExpression`` (63,375) and
  ``HorizonKoLine`` (7,009) are **reference data imported from public sources**.
  They are reproducible from their importers, they would be 95% of the bytes,
  and nothing in the lab authors them.
* ``CropperSession`` / ``CropperImage`` are scratch state for a tool that
  stages into ``PendingPublicationImage`` and, on release, ``PublicationImage``
  — both of which *are* captured. The staged rows are captured deliberately
  rather than dismissed as scratch: an afternoon's cropping that has not been
  released yet exists nowhere else, and it is exactly the kind of work somebody
  reaches for this file on the worst day to find.
* Django's own ``auth_user`` and ``Member`` — this is scientific data, and a
  snapshot that travels by email should not carry password hashes. Who did what
  survives as the display name already stored on the record.

**It is a capture, not a restore.** `dataset.apply_upload` can only *update*
rows that still exist for eight of its ten families, so uploading yesterday's
file after a deletion reports every row as blocked and writes nothing. The
manifest says so in as many words, because a file that looks like a backup and
is not one is the most dangerous artefact this app could produce.

Every snapshot carries a ``manifest`` naming its coverage, its omissions and a
count per table, so the file answers "what is in here?" without being parsed —
and so a count that looks wrong is visible next to the thing that produced it.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder

DB = "pipeline_db"

# The version of the *shape* of this file. Bump it when a table joins or leaves,
# so a reader of an old snapshot can tell what it was reasonable to expect.
# 2 since 13 Aug 2026: ``PendingPublicationImage`` joined, when the figure
# cropper stopped writing straight to the public site.
# 3 since 28 Aug 2026: ``AntibodyOutcome`` joined — the two-axis judgement made
# from a published figure, which exists nowhere else once it is made.
FORMAT_VERSION = 3

# Everything the lab authors, in dependency order — the order a restore would
# want, and the order that reads sensibly in a diff.
CAPTURED = [
    "Site", "GrantingAgency", "Project", "Company",
    "Target", "TargetNomination", "TargetAssignment", "TargetClassification",
    "CellLine", "CellLineVial", "CellCultureEvent",
    "Antibody", "InventoryLocation", "Sample",
    "ProtocolTemplate",
    "ExperimentSession", "WbResult", "IpResult", "IfResult", "FcResult",
    "Report", "PublicationImage", "PendingPublicationImage", "AntibodyOutcome",
    "FileAttachment",
    "ManufacturerContact", "ReagentRequestBatch", "ReagentRequest", "Shipment",
]

# Named, not silently dropped — the same rule the boards' read-only columns
# follow. A reader must be able to tell "not captured" from "none on file".
OMITTED = {
    "DepMapExpression": "public reference data, re-importable, 461k rows",
    "ProteomicsExpression": "public reference data, re-importable, 63k rows",
    "HorizonKoLine": "supplier catalogue, re-importable, 7k rows",
    "CropperSession": "scratch state; published figures are in PublicationImage",
    "CropperImage": "scratch state; published figures are in PublicationImage",
    "Member": "people, not science — and a snapshot that travels by email "
              "should not carry account rows",
    # Load-bearing, not tidiness: this table holds the stored captures
    # themselves, so capturing it would put yesterday's snapshot inside today's
    # and double the size every day.
    "DatasetSnapshot": "the stored captures themselves — capturing them would "
                       "nest each day's file inside the next",
    # Same argument as Member, and stronger: these rows are strangers' email
    # addresses, given to us for one purpose. A snapshot is a file that travels
    # by email, and personal data does not belong in one. The rows themselves
    # are covered by the database's own point-in-time recovery, below.
    "GeneRequest": "public gene nominations — personal data (email addresses) "
                   "given for one purpose, and a snapshot travels by email",
}

RESTORE_NOTE = (
    "This is a capture, not a restore. The upload path on Downloads & uploads "
    "updates values on rows that still exist; it cannot recreate a deleted row "
    "for most families. To roll the database back, use Render → "
    "ycharos-pipeline-db → Recovery (point-in-time, rolling 3 days)."
)


def _value(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _rows(model):
    """Every row of one model as plain dicts, local fields only.

    `values()` rather than the model serializer: it is one query, it follows no
    relations (a FK arrives as its id, which is what a capture wants), and it
    cannot trip over a property that hits the database per row.
    """
    return [{k: _value(v) for k, v in row.items()}
            for row in model.objects.using(DB).values()]


def build(captured=None) -> dict:
    """The whole capture, manifest first."""
    names = list(captured or CAPTURED)
    by_name = {m.__name__: m for m in apps.get_app_config("pipeline").get_models()}

    tables, counts = {}, {}
    missing = []
    for name in names:
        model = by_name.get(name)
        if model is None:            # a model renamed or removed since
            missing.append(name)
            continue
        rows = _rows(model)
        tables[name] = rows
        counts[name] = len(rows)

    return {
        "manifest": {
            "format_version": FORMAT_VERSION,
            "tables": counts,
            "row_total": sum(counts.values()),
            # What is NOT here, by name. "A panel that counts must list what it
            # counted" — and the inverse matters more on a file called a full
            # capture: absence has to be distinguishable from emptiness.
            "omitted": OMITTED,
            "not_found": missing,
            "restores": False,
            "restore_note": RESTORE_NOTE,
        },
        "tables": tables,
    }


def to_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, cls=DjangoJSONEncoder)


def summary_line(payload: dict) -> str:
    """One sentence for an email subject or a page — counts, not adjectives."""
    m = payload.get("manifest", {})
    return (f"{m.get('row_total', 0):,} rows across "
            f"{len(m.get('tables', {}))} tables")


# ── Where it is kept ────────────────────────────────────────────────────────
# The capture is written by a Render Cron Job and read by the web service, and
# those are two services with no filesystem between them (see
# `snapshot_models.py` for why). `pipeline_db` is the store both already reach,
# so these three functions are the one door to it — the page, the download and
# the command all come through here rather than each knowing the model.

def store(name: str, blob: bytes, payload: dict, taken):
    """Keep one capture. The manifest's headline numbers ride along with it.

    ``update_or_create`` rather than ``create``: the name carries the time to the
    second, so running the command twice inside one second is a name collision —
    and the second run is a capture of the same second, not a lost one. It should
    replace, not raise ``IntegrityError`` at somebody typing the line twice.
    """
    from pipeline.models import DatasetSnapshot

    m = payload.get("manifest", {})
    row, _ = DatasetSnapshot.objects.using(DB).update_or_create(
        name=name,
        defaults={
            "blob": blob, "byte_size": len(blob), "taken": taken,
            "row_total": m.get("row_total", 0),
            "table_count": len(m.get("tables", {})),
            "format_version": m.get("format_version", 0),
        },
    )
    return row


def newest():
    """The most recent stored capture, or ``None``.

    Deliberately `.defer("blob")`: this answers "what is the newest one?" for a
    page that draws a date and a size, and pulling several MB of gzip across the
    wire to render one line of text is how a cheap panel becomes a slow one.
    """
    from pipeline.models import DatasetSnapshot

    return DatasetSnapshot.objects.using(DB).defer("blob").order_by("-taken").first()


def prune(keep: int) -> list[str]:
    """Drop all but the newest ``keep``, returning what went.

    A table that only grows is the same failure as a directory that only grows,
    one store over — and this one is inside the database being captured.
    """
    from pipeline.models import DatasetSnapshot

    qs = DatasetSnapshot.objects.using(DB).order_by("-taken")
    old = list(qs.values_list("pk", "name")[max(1, keep):])
    if old:
        DatasetSnapshot.objects.using(DB).filter(
            pk__in=[pk for pk, _ in old]).delete()
    return [name for _, name in old]
