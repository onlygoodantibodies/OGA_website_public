"""Retire the empty second rows that shadowed real suppliers (29 Sep 2026).

Each of these is a supplier typed a second way that the resolver, before
display-name matching, minted as a row of its own — and, once it existed, every
later paste of that spelling matched it exactly and filed the vial under an
empty supplier no manufacturer's portal key could see. All of them held no
antibody and no cell line on live on 29 Sep 2026.

**Nothing is deleted.** Each is marked ``is_active=False``, which the resolver
reads as "not a canonical supplier": it is offered by no picker and matched only
when nothing active answers (`cropper/db.py::resolve_company`). Reversing the
migration makes them active again. Each row is touched only if it still has the
name it had when this was written, so a row somebody has since renamed is left
alone.

`Horizon` (9 cell lines) takes the display name `Horizon Discovery`, its own
company name, so typing either lands on it.
"""
from django.db import migrations

# id → the name it must still carry to be retired.
RETIRE = {
    188: "Atlas Antibodies",    # Human Protein Atlas' display name
    189: "BD Biosciences",      # BD Bioscience's display name
    192: "Horizon Discovery",   # Horizon (below)
    193: "Cell Signaling",      # Cell Signaling Technology
    196: "Thermo Fisher",       # Thermo Fisher Scientific
    198: "Millipore",           # MilliporeSigma
    202: "Sigma",               # MilliporeSigma
}
HORIZON = (181, "Horizon", "Horizon Discovery")


def forward(apps, schema_editor):
    Company = apps.get_model("pipeline", "Company")
    db = schema_editor.connection.alias
    for pk, name in RETIRE.items():
        Company.objects.using(db).filter(pk=pk, name=name).update(is_active=False)
    pk, name, display = HORIZON
    Company.objects.using(db).filter(pk=pk, name=name, display_name="").update(
        display_name=display)


def backward(apps, schema_editor):
    Company = apps.get_model("pipeline", "Company")
    db = schema_editor.connection.alias
    for pk, name in RETIRE.items():
        Company.objects.using(db).filter(pk=pk, name=name).update(is_active=True)
    pk, name, display = HORIZON
    Company.objects.using(db).filter(pk=pk, name=name, display_name=display).update(
        display_name="")


class Migration(migrations.Migration):

    dependencies = [("pipeline", "0039_saved_by")]

    operations = [migrations.RunPython(forward, backward)]
