"""Retire the legacy `core` layer.

State-only. The five models being dropped — Gene, Antibody, Experiment,
Description, CellLine — existed *only* in the `default` SQLite database
(`db_core.sqlite3`), which is deleted in the same commit and which `default`
no longer points at. A real `DeleteModel` would emit DROP TABLE against
whatever database this migration is applied to, and neither academy_db nor
pipeline_db ever had these tables: the drop would fail, and pipeline
migrations reach live PostgreSQL unattended.

So the operations are state-only, the same shape the repo already uses for a
storage change that must not touch the schema (`SeparateDatabaseAndState` with
an empty `database_operations`). Django's model state stops knowing about the
five; no database is asked to do anything.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_apiusageday'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                # Order matters for state consistency: dependants before their
                # targets. Description and Experiment point at Antibody,
                # Antibody points at Gene.
                migrations.DeleteModel(name='Description'),
                migrations.DeleteModel(name='Experiment'),
                migrations.DeleteModel(name='Antibody'),
                migrations.DeleteModel(name='Gene'),
                migrations.DeleteModel(name='CellLine'),
            ],
        ),
    ]
