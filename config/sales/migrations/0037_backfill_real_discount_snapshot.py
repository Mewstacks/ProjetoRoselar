"""Preenche o desconto real dos orçamentos já gravados.

`real_discount_snapshot` / `real_discount_pct_snapshot` são mantidos pelos
signals do snapshot, mas os signals só disparam quando o orçamento ou seus itens
são salvos. Sem este backfill, todo o histórico ficaria com desconto real zero e
os relatórios de desconto só enxergariam orçamentos tocados depois do deploy.

Usa o modelo real (as regras de total não existem no modelo histórico do
`apps`). O SELECT lista só as colunas que o cálculo lê: uma coluna criada em
migração futura não precisa entrar numa lista de `defer()` para um banco novo
conseguir rodar esta migração.
"""

from django.db import migrations


def backfill_real_discount(apps, schema_editor):
    from django.db.models import Prefetch

    from sales.models import Quote, QuoteItem

    items = QuoteItem.objects.only(
        "id", "quote", "quantity", "unit_value", "unit_value_wholesale"
    )
    quotes = (
        Quote.objects.only(
            "id",
            "dual_pricing",
            "selected_price_tier",
            "selected_payment_terms",
            "total_override",
            "total_cash_override",
            "discount_percent",
            "price_increase_percent",
            "freight_value",
            "freight_responsible",
            "total_rounding_mode",
            "total_manual_adjustment",
        )
        .prefetch_related(Prefetch("items", queryset=items))
        .iterator(chunk_size=200)
    )

    for quote in quotes:
        try:
            real_gap, real_pct = quote.calculate_real_gap()
        except Exception:
            # Um orçamento com dados inconsistentes não pode travar o deploy;
            # ele fica com zero até ser salvo de novo.
            continue
        Quote.objects.filter(pk=quote.pk).update(
            real_discount_snapshot=real_gap,
            real_discount_pct_snapshot=real_pct,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0036_quote_real_discount_snapshot"),
    ]

    operations = [
        # Sem volta útil: o valor é derivado e se recalcula a cada salvamento.
        migrations.RunPython(backfill_real_discount, migrations.RunPython.noop),
    ]
