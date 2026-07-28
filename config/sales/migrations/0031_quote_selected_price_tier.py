from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0030_quoteitem_environment_position"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="selected_price_tier",
            field=models.CharField(
                choices=[("RETAIL", "Varejo"), ("WHOLESALE", "Atacado")],
                default="RETAIL",
                help_text="Preço efetivamente escolhido pelo cliente na conversão da venda.",
                max_length=10,
                verbose_name="Modalidade Fechada",
            ),
        ),
    ]
