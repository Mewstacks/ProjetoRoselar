from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0021_quote_notes_quote_total_manual_adjustment_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='quote',
            name='total_override',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Valor exato do total ao cliente. Deixe em branco para usar o total calculado.',
                max_digits=12,
                null=True,
                verbose_name='Preço Final ao Cliente (R$)',
            ),
        ),
    ]
