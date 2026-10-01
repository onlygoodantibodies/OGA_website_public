"""The module editor: a published flag, a version history, and certificates
that survive both a rename and an attempted delete.

Safe on the live table of issued certificates, and checked with ``sqlmigrate``
against that question:

* ``Certificate.lesson_title`` and ``Lesson.is_published`` are added with a
  database default, so on PostgreSQL each is one ``ADD COLUMN ... DEFAULT``:
  no existing row is read or rewritten, and code rolled back past this
  migration can still insert (it omits the columns and the database fills them).
* ``Certificate.lesson`` going from CASCADE to PROTECT is enforced by Django,
  not the database, so it produces no SQL at all.
* ``LessonRevision`` is a new table.

No data migration: a blank ``lesson_title`` means "the module has not been
renamed since", and ``Lesson.save`` fills it the moment one is.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academy", "0009_aichatusage"),
    ]

    operations = [
        migrations.AddField(
            model_name="certificate",
            name="lesson_title",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=200
            ),
        ),
        migrations.AddField(
            model_name="lesson",
            name="is_published",
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AlterField(
            model_name="certificate",
            name="lesson",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="academy.lesson"
            ),
        ),
        migrations.CreateModel(
            name="LessonRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("saved_at", models.DateTimeField(auto_now_add=True)),
                ("saved_by", models.CharField(blank=True, max_length=150)),
                ("note", models.CharField(blank=True, max_length=300)),
                ("snapshot", models.JSONField()),
                (
                    "lesson",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="revisions",
                        to="academy.lesson",
                    ),
                ),
            ],
            options={
                "ordering": ["-saved_at", "-pk"],
            },
        ),
    ]
