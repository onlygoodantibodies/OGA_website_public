"""Staged crops move to the public bucket, at the key they keep once released.

Owner's decision, 23 Aug 2026: partners build on cropped figures before formal
release, so a crop is written once at its final public URL and `release` never
moves the bytes. Review still gates the **gene page**; it no longer gates the
object.

`SeparateDatabaseAndState` with no database operations, per the standing rule:
a storage/`upload_to` change alters no column, SQLite rebuilds the whole table
for any `AlterField`, and pipeline migrations reach live PostgreSQL unattended.

It also does not move the 158 objects already staged under `pending_figures/`.
Those rows keep the name they were written with and go on resolving; only new
crops take the public key. Moving them is a data job, not a schema one.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0026_generequest_applications_other"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name="pendingpublicationimage",
                    name="image",
                    field=models.FileField(
                        upload_to="publication_images/%Y/",
                        help_text="The crop, at the public URL it will keep "
                                  "once released.",
                    ),
                ),
            ],
        ),
    ]
