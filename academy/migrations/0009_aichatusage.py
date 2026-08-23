"""Create the AIChatUsage table — the per-user daily message counter for the
in-app AI assistant (the cost guardrail). Purely additive: a brand-new table
with no bearing on existing certificates, users, or progress rows."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("academy", "0008_certificate_verification_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIChatUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("message_count", models.PositiveIntegerField(default=0)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "unique_together": {("user", "date")},
            },
        ),
    ]
