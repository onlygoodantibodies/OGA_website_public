"""IHC becomes a fifth application a figure can be filed under.

A new value in the `application_type` choices of `PublicationImage` and
`PendingPublicationImage` — and of `AntibodyOutcome`, which borrows the same
list — and nothing else: the column is already a
`varchar(10)`, so `'IHC'` fits it as it stands.

`SeparateDatabaseAndState` with no database operations, per the standing rule:
choices alter no column, SQLite rebuilds the whole table for any `AlterField`,
and pipeline migrations reach live PostgreSQL unattended.
"""
from django.db import migrations, models


CHOICES = [
    ("WB", "Western Blot"),
    ("IP", "Immunoprecipitation"),
    ("ICC-IF", "Immunocytochemistry/IF"),
    ("FC", "Flow Cytometry"),
    ("IHC", "Immunohistochemistry"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0033_fc_background_caveat"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name="publicationimage",
                    name="application_type",
                    field=models.CharField(max_length=10, choices=CHOICES),
                ),
                migrations.AlterField(
                    model_name="pendingpublicationimage",
                    name="application_type",
                    field=models.CharField(max_length=10, choices=CHOICES),
                ),
                migrations.AlterField(
                    model_name="antibodyoutcome",
                    name="application_type",
                    field=models.CharField(max_length=10, choices=CHOICES),
                ),
            ],
        ),
    ]
