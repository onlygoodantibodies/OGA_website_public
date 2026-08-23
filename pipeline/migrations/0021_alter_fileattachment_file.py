"""Point a session's raw files at their own storage — and touch no schema.

``storage`` is not a database concern: it decides which bucket the bytes go to,
and the column stays the same ``varchar`` holding the same key. On PostgreSQL
Django's ``AlterField`` would find nothing to change and emit no SQL; on SQLite
the backend rebuilds the whole table for *any* ``AlterField``, which is a real
operation on a table full of rows to achieve nothing at all.

``SeparateDatabaseAndState`` says that outright rather than leaving it to be
inferred from the backend: the migration state learns about the new storage, and
**no database operation is generated on any backend**. This matters more here
than the tidiness suggests — pipeline migrations apply to live PostgreSQL on
deploy with nobody in the loop (CLAUDE.md), so a migration that provably runs no
DDL is one the owner does not have to take on trust.
"""
import pipeline.storages
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0020_target_board"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="fileattachment",
                    name="file",
                    field=models.FileField(
                        storage=pipeline.storages.attachment_storage,
                        upload_to="pipeline/attachments/%Y/%m/",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
