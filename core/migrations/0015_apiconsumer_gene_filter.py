# Generated manually — core/migrations/0015_apiconsumer_gene_filter.py

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_reviewedantibody"),
    ]

    operations = [
        migrations.AddField(
            model_name="apiconsumer",
            name="gene_filter",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Comma-separated gene names to restrict access to "
                    "(e.g. 'GBA1 (GCase),GPNMB,CD44'). "
                    "Leave blank for unrestricted access to all genes. "
                    "Used for demo/trial accounts — clear when converting to paid."
                ),
                max_length=500,
                null=True,
            ),
        ),
    ]
