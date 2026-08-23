from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_remove_gene_cell_lines_gene_cell_line_link_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='gene',
            name='aliases',
            field=models.CharField(
                blank=True,
                null=True,
                max_length=500,
                help_text='Comma-separated list of alternative gene names/symbols (auto-populated from HGNC)',
            ),
        ),
    ]
