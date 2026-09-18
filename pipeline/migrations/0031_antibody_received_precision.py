"""Say how much of ``Antibody.received_date`` is actually known.

Additive, one column, with a default — the shape a rollback survives. Render
re-runs ``migrate`` against the *old* code on a rollback, so a migration is only
safe to fall back over when the previous version does not need the column gone;
nothing before this reads it, and a ``RemoveField`` reversal would drop a column
whose every value is derivable from the fact that older rows are day-precise.

**No backfill, deliberately.** ``default="day"`` is true of all 2,841 antibodies
on live that carry a ``received_date``: they came from the Access import with
real dates — 31 distinct days of the month between them, only 33 on the 1st
(read 1 Sep 2026) — so day precision is what those rows mean, not a guess this
is papering over.

The column is written and read only through ``services/received.py``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0030_detection_ab_help_text_wording"),
    ]

    operations = [
        migrations.AddField(
            model_name="antibody",
            name="received_precision",
            field=models.CharField(
                choices=[("day", "Day"), ("month", "Month"), ("year", "Year")],
                default="day",
                help_text="Whether received_date is known to the day, the month or only the year.",
                max_length=10,
            ),
        ),
    ]
