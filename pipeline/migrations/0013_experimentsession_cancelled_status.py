# pipeline/migrations/0013_experimentsession_cancelled_status.py
# Generated manually — adds 'cancelled' to SessionStatus choices

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0012_antibody_supplier_recommended_dilutions_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='experimentsession',
            name='status',
            field=models.CharField(
                choices=[
                    ('planned', 'Planned'),
                    ('in_progress', 'In Progress'),
                    ('complete', 'Complete'),
                    ('failed', 'Failed'),
                    ('repeat_needed', 'Repeat Needed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='planned',
                max_length=20,
            ),
        ),
    ]
