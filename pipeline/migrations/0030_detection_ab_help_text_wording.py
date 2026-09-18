"""`detection_ab`'s help text: "KO-validated" becomes "KO-controlled".

Owner's terminology correction, 29 Aug 2026: OGA characterises antibodies
against knockout controls; it does not validate them. The word was wrong in
the Django admin help text for the IP-WB detection antibody, and the same
wording is fixed in `views/session_entry.py` and the default IP-WB protocol
template, neither of which needs a migration.

`SeparateDatabaseAndState` with no database operations, per the standing rule:
help text alters no column, SQLite rebuilds the whole table for any
`AlterField`, and pipeline migrations reach live PostgreSQL unattended.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0029_recommendations_set_at"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name="ipresult",
                    name="detection_ab",
                    field=models.CharField(
                        blank=True,
                        max_length=255,
                        help_text="KO-controlled antibody used for WB "
                                  "detection step",
                    ),
                ),
            ],
        ),
    ]
