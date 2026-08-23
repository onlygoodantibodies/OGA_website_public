# Generated manually — 2026-03-28
# ReviewedAntibody tracks per-antibody review status for portal consumers.
# Routes to academy_db (persistent disk) via db_router — not in CORE_DATA_MODELS.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_apiconsumer_tier'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReviewedAntibody',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('antibody_catalogue', models.CharField(help_text='Antibody catalogue number (e.g. ab12345)', max_length=100)),
                ('reviewed_at', models.DateTimeField(auto_now=True)),
                ('consumer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reviewed_antibodies', to='core.apiconsumer')),
            ],
            options={
                'verbose_name': 'Reviewed Antibody',
                'verbose_name_plural': 'Reviewed Antibodies',
                'unique_together': {('consumer', 'antibody_catalogue')},
            },
        ),
    ]
