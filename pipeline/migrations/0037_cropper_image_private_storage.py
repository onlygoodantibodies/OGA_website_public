# The cropper's uploads move to private storage (26 Sep 2026): a whole IHC
# figure is copied byte for byte from the upload, and it is private until
# release, so the upload cannot sit in the public media bucket meanwhile.
#
# A storage change is state only — no column changes — so this is
# SeparateDatabaseAndState with no database operations (CLAUDE.md: SQLite
# rebuilds the whole table for any AlterField, and pipeline migrations reach
# live PostgreSQL unattended). Uploads staged before this still resolve
# through the fallback in `pipeline/storages.py::cropper_storage`. A rollback
# un-applies nothing and breaks no page, but the older code looks only in the
# media bucket, so a cropper upload made after this deploy (scratch, never a
# published figure) would not be found by it until rolled forward again.

import pipeline.storages
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0036_ihc_page_figures"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="cropperimage",
                    name="image",
                    field=models.FileField(
                        storage=pipeline.storages.cropper_storage,
                        upload_to="cropper_staging/%Y/%m/"),
                ),
            ],
            database_operations=[],
        ),
    ]
