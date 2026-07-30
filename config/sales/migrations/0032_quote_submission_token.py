from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0031_quote_selected_price_tier"),
    ]

    operations = [
        migrations.AddField(
            model_name="quote",
            name="submission_token",
            field=models.UUIDField(
                blank=True,
                editable=False,
                help_text="Impede que cliques repetidos criem o mesmo orçamento mais de uma vez.",
                null=True,
                unique=True,
                verbose_name="Token de envio",
            ),
        ),
    ]
