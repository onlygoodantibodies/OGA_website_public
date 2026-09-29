"""Merge the stray `SKNAS` wild type into McGill's `SK-N-AS` (29 Sep 2026).

Row 1645, `SKNAS`, was added through the app on 5 Sep 2026 — a wild type at
McGill with no catalogue, no origin and no batches — and `lab_numbers` gave it
C-764. It is the same line as row 1588, `SK-N-AS` (ATCC CRL-2137, batch C-754),
which the nine SK-N-AS knockout batches (C-755–C-763) name as their parent. So
the session form offered two SK-N-AS wild types, one of them empty.

This is `merge_cell_lines --from 1645 --into 1588`, written as a migration so it
reaches live without the Render Shell. On 29 Sep 2026 nothing pointed at 1645 —
no batch, session, knockout, inventory, culture event or sample — so the only
thing that moves is its **number**: C-764 becomes a freeze-down batch on
SK-N-AS, because a number the app issued may already be on a tube, and a
number that stops resolving is a tube nobody can find.

**Guarded.** It acts only if 1645 is still `SKNAS`, WT, at McGill, C-764 and
referenced by nothing, and 1588 is still McGill's `SK-N-AS` wild type; on any
other database (a fresh clone, CI) it does nothing. **Reversible**: going back
recreates row 1645 as it was and removes the batch this made.
"""
from django.db import migrations

LOSER = dict(pk=1645, name="SKNAS", genotype="WT", site_id=5, c_number=764,
             target_id=None)
WINNER = dict(pk=1588, name="SK-N-AS", genotype="WT", site_id=5)
NOTE = "Kept from SKNAS (row 1645) when it was merged in, 29 Sep 2026."

# (model, field) pointing at CellLine on 29 Sep 2026.
REFERENCES = [
    ("CellLine", "parent_line"), ("CellLine", "arrived_with_ko"),
    ("ExperimentSession", "cell_line_wt"), ("ExperimentSession", "cell_line_ko"),
    ("InventoryLocation", "cell_line"), ("CellCultureEvent", "cell_line"),
    ("CellLineVial", "cell_line"), ("Sample", "cell_line"),
]


def forward(apps, schema_editor):
    db = schema_editor.connection.alias
    CellLine = apps.get_model("pipeline", "CellLine")
    CellLineVial = apps.get_model("pipeline", "CellLineVial")
    if not CellLine.objects.using(db).filter(**LOSER).exists():
        return
    if not CellLine.objects.using(db).filter(**WINNER).exists():
        return
    for model, field in REFERENCES:
        if apps.get_model("pipeline", model).objects.using(db).filter(
                **{f"{field}_id": LOSER["pk"]}).exists():
            return  # something now points at it: leave it for merge_cell_lines
    if not CellLineVial.objects.using(db).filter(
            c_number=LOSER["c_number"], site_id=LOSER["site_id"]).exists():
        CellLineVial.objects.using(db).create(
            cell_line_id=WINNER["pk"], c_number=LOSER["c_number"],
            site_id=WINNER["site_id"], notes=NOTE)
    CellLine.objects.using(db).filter(pk=LOSER["pk"]).delete()


def backward(apps, schema_editor):
    db = schema_editor.connection.alias
    CellLine = apps.get_model("pipeline", "CellLine")
    CellLineVial = apps.get_model("pipeline", "CellLineVial")
    batch = CellLineVial.objects.using(db).filter(
        cell_line_id=WINNER["pk"], c_number=LOSER["c_number"], notes=NOTE)
    if not batch.exists():
        return
    batch.delete()
    if not CellLine.objects.using(db).filter(pk=LOSER["pk"]).exists():
        CellLine.objects.using(db).create(**LOSER)


class Migration(migrations.Migration):

    dependencies = [("pipeline", "0040_retire_empty_duplicate_suppliers")]

    operations = [migrations.RunPython(forward, backward)]
