"""OGA's verdict for immunohistochemistry: `Antibody.ihc_recommended`.

One `AddField`, additive, and nothing else — no `RunPython`, no `AlterField`.
Every existing antibody starts unflagged, which is what it was.

**`db_default=False` is what keeps a rollback working.** A plain `default` is
used by Django to fill the existing rows and is then dropped from the column,
so the column would be NOT NULL with no database default. Code rolled back past
this migration does not know the column exists and would leave it out of every
INSERT, and PostgreSQL would refuse every new antibody — the cropper, the bulk
add and the boards all at once. With a database default the old code's INSERT
still succeeds.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0034_ihc_figure_application"),
    ]

    operations = [
        migrations.AddField(
            model_name="antibody",
            name="ihc_recommended",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Recommended for Immunohistochemistry (HAP1 cell pellets)",
            ),
        ),
    ]
