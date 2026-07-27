"""Backfill da comissão para vendas fechadas antes do campo existir.

Até aqui a comissão nunca foi persistida: o Relatório de Comissões recalculava
uma aproximação própria, divergente do simulador. Como os relatórios passam a
somar apenas o valor gravado, as vendas históricas ficariam com comissão zero.

Esta migração roda o motor de margem real sobre cada venda já fechada e grava o
resultado marcado como BACKFILL. A entrada (down_payment) daquelas vendas nunca
foi registrada, então o motor roda com entrada zero — exatamente a premissa que
o relatório antigo já adotava implicitamente. Por isso o valor é ESTIMADO, não
apurado, e o relatório sinaliza essas linhas.
"""

from decimal import Decimal

from django.db import migrations


def backfill_commissions(apps, schema_editor):
    # O motor precisa dos métodos de cálculo do modelo real (calculate_subtotal,
    # billable_freight), que o modelo histórico do `apps` não possui.
    from sales.margin import simulate_quote
    from sales.models import Quote, SOLD_STATUSES

    sold = (
        Quote.objects.filter(status__in=SOLD_STATUSES, commission_value__isnull=True)
        .prefetch_related("items")
    )

    for quote in sold.iterator(chunk_size=200):
        try:
            ctx = simulate_quote(quote)
        except Exception:
            # Uma venda com dados inconsistentes não pode travar o deploy inteiro.
            # Ela fica sem comissão gravada e aparece como pendente no relatório.
            continue
        Quote.objects.filter(pk=quote.pk).update(
            commission_pct=Decimal(str(ctx["seller_commission_percent"])).quantize(Decimal("0.01")),
            commission_value=Decimal(str(ctx["seller_commission_value"])).quantize(Decimal("0.01")),
            commission_calculated_at=None,
            commission_source="BACKFILL",
        )


def clear_backfilled(apps, schema_editor):
    Quote = apps.get_model("sales", "Quote")
    Quote.objects.filter(commission_source="BACKFILL").update(
        commission_pct=None,
        commission_value=None,
        commission_calculated_at=None,
        commission_source="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0027_quote_commission_calculated_at_quote_commission_pct_and_more"),
        # PaymentTariff é lido pelo motor para obter as taxas.
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_commissions, clear_backfilled),
    ]
