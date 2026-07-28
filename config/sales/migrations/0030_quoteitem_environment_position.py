from django.db import migrations, models


def backfill_item_positions(apps, schema_editor):
    QuoteItem = apps.get_model("sales", "QuoteItem")
    batch = []
    current_quote_id = None
    position = 0

    for item in QuoteItem.objects.order_by("quote_id", "id").iterator(chunk_size=500):
        if item.quote_id != current_quote_id:
            current_quote_id = item.quote_id
            position = 0
        item.position = position
        position += 1
        batch.append(item)
        if len(batch) >= 500:
            QuoteItem.objects.bulk_update(batch, ["position"])
            batch = []

    if batch:
        QuoteItem.objects.bulk_update(batch, ["position"])


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0029_refresh_quote_snapshots"),
    ]

    operations = [
        migrations.AddField(
            model_name="quoteitem",
            name="environment",
            field=models.CharField(
                blank=True,
                help_text="Ex.: Dormitório, Cozinha ou Sala de Estar.",
                max_length=100,
                verbose_name="Ambiente",
            ),
        ),
        migrations.AddField(
            model_name="quoteitem",
            name="position",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Ordem do item no orçamento e no PDF.",
                verbose_name="Posição",
            ),
        ),
        migrations.RunPython(backfill_item_positions, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="quoteitem",
            options={
                "ordering": ["position", "id"],
                "verbose_name": "Item do Orçamento",
                "verbose_name_plural": "Itens do Orçamento",
            },
        ),
    ]
