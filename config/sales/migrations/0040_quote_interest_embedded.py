from django.db import migrations, models


def freeze_sold(apps, schema_editor):
    """Vendas já fechadas mantêm o total sem juros: o histórico não muda."""
    Quote = apps.get_model("sales", "Quote")
    Quote.objects.filter(status__in=["CONVERTED", "POS_VENDA"]).update(interest_embedded=False)


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0039_allow_legacy_cash_column_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="interest_embedded",
            field=models.BooleanField(
                default=True,
                help_text="Crédito 7x+: o cliente paga a taxa por dentro do total. "
                          "Desligado nas vendas anteriores à regra, para não mudar o histórico.",
                verbose_name="Juros no total",
            ),
        ),
        migrations.AddField(
            model_name="quote",
            name="interest_snapshot",
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=12,
                verbose_name="Juros no total (R$)",
            ),
        ),
        migrations.RunPython(freeze_sold, migrations.RunPython.noop),
    ]
