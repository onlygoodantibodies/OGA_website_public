"""Take the C-numbers back off the 22 knockdown rows (29 Sep 2026).

`backfill_knockdown_lines` made one knockdown row per gene, background and site
on 18 Sep 2026 so a session's control slot could say "knockdown" — and
`lab_numbers` numbered every one on `pre_save`, C-765 to C-786 at McGill. A
knockdown here is a transient siRNA transfection of a wild type (owner, 29 Sep
2026): nothing is frozen down, so those numbers name no tube, and they pushed
McGill's next real line to C-787. `lab_numbers.numbers_genotype` stops a new
one being issued; this clears the ones already given.

Checked read-only on live first: none of the 22 has a freeze-down batch or a
freezer location, none has been edited since the backfill, no session's
conditions name any of the numbers, and McGill has numbered no line since. So
nothing written anywhere reads them, and McGill's next line takes C-765 again.

**Guarded**: a row is cleared only if it still has the number it was given and
no freeze-down batch; on any other database it does nothing. **Reversible**:
going back puts each number back where it was blank.
"""
from django.db import migrations

# pk → the number the backfill gave it (McGill, site 5).
NUMBERS = {
    1646: 765, 1647: 766, 1648: 767, 1649: 768, 1650: 769, 1651: 770,
    1652: 771, 1653: 772, 1654: 773, 1655: 774, 1656: 775, 1657: 776,
    1658: 777, 1659: 778, 1660: 779, 1661: 780, 1662: 781, 1663: 782,
    1664: 783, 1665: 784, 1666: 785, 1667: 786,
}


def forward(apps, schema_editor):
    db = schema_editor.connection.alias
    CellLine = apps.get_model("pipeline", "CellLine")
    CellLineVial = apps.get_model("pipeline", "CellLineVial")
    frozen = set(CellLineVial.objects.using(db)
                 .filter(cell_line_id__in=NUMBERS)
                 .values_list("cell_line_id", flat=True))
    for pk, number in NUMBERS.items():
        if pk in frozen:
            continue
        CellLine.objects.using(db).filter(
            pk=pk, genotype="KD", site_id=5, c_number=number).update(c_number=None)


def backward(apps, schema_editor):
    db = schema_editor.connection.alias
    CellLine = apps.get_model("pipeline", "CellLine")
    for pk, number in NUMBERS.items():
        CellLine.objects.using(db).filter(
            pk=pk, genotype="KD", site_id=5, c_number__isnull=True).update(
            c_number=number)


class Migration(migrations.Migration):

    dependencies = [("pipeline", "0041_merge_sknas_into_sk_n_as")]

    operations = [migrations.RunPython(forward, backward)]
