from decimal import Decimal

from django.db import migrations, models


def mark_existing_sales_as_legacy(apps, schema_editor):
    Quote = apps.get_model("sales", "Quote")
    Quote.objects.filter(status__in=["CONVERTED", "POS_VENDA"]).update(
        commission_rule="LEGACY"
    )


class Migration(migrations.Migration):
    dependencies = [("sales", "0034_quote_selected_payment_terms_and_more")]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="commission_gross_value",
            field=models.DecimalField(blank=True, decimal_places=2,
                                      help_text="Comissão antes do abatimento de frete do vendedor.",
                                      max_digits=12, null=True, verbose_name="Comissão Bruta (R$)"),
        ),
        migrations.AddField(
            model_name="quote",
            name="freight_commission_deduction",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"),
                                      help_text="Valor de frete assumido pelo vendedor e abatido da comissão.",
                                      max_digits=12, verbose_name="Frete descontado da comissão (R$)"),
        ),
        migrations.AddField(
            model_name="quote",
            name="commission_rule",
            field=models.CharField(default="FIXED_3", editable=False, max_length=12,
                                   verbose_name="Regra de comissão"),
        ),
        migrations.RunPython(mark_existing_sales_as_legacy, migrations.RunPython.noop),
    ]
