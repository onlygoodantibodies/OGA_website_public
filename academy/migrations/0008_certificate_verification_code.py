"""Add a verification code to academy certificates (for QR + a verify page).

Written by hand rather than auto-generated so it is SAFE on a table that already
holds live certificates:

  1. Add the column nullable, with NO default  -> every existing row gets NULL
     (crucially NOT a single shared UUID, which a defaulted AddField would do and
     which would then break the unique constraint).
  2. Back-fill each existing certificate with its OWN uuid4.
  3. Only THEN add the unique constraint.

This never reads, rewrites, or deletes an existing certificate's user, lesson,
score, or issue date — it only fills a new column. It is fully reversible.
"""
import uuid

from django.db import migrations, models, router


def backfill_codes(apps, schema_editor):
    Certificate = apps.get_model("academy", "Certificate")
    db = schema_editor.connection.alias
    # Multi-DB safe: RunPython runs on every alias, but academy_certificate only
    # exists on academy_db — no-op everywhere else (and query/save on THIS db).
    if not router.allow_migrate_model(db, Certificate):
        return
    for cert in Certificate.objects.using(db).filter(verification_code__isnull=True):
        cert.verification_code = uuid.uuid4()
        cert.save(update_fields=["verification_code"], using=db)


def noop_reverse(apps, schema_editor):
    # Reversing just drops the column again (handled by RemoveField below); the
    # backfilled values disappear with it, so there is nothing to undo here.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("academy", "0007_lesson_has_quiz"),
    ]

    operations = [
        # 1. nullable, no default, not-yet-unique
        migrations.AddField(
            model_name="certificate",
            name="verification_code",
            field=models.UUIDField(editable=False, null=True),
        ),
        # 2. give every existing certificate its own code
        migrations.RunPython(backfill_codes, noop_reverse),
        # 3. final shape: defaulted for new rows + unique
        migrations.AlterField(
            model_name="certificate",
            name="verification_code",
            field=models.UUIDField(default=uuid.uuid4, editable=False,
                                   null=True, unique=True),
        ),
    ]
