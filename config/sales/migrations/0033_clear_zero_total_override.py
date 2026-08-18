"""Zera-para-nulo o preço final ao cliente gravado como 0,00.

`Quote.total_override` é o "Preço Final ao Cliente". `apply_client_rounding`
testava apenas `is not None`, então um zero gravado no campo — um "0" solto que
a máscara de moeda vira "0,00" — passava a valer como preço: o total de varejo
do orçamento virava R$ 0,00 em todas as telas, no PDF do cliente, no pedido e
no `total_value_snapshot` que alimenta os relatórios de venda e comissão.

Em produção isso atingiu 9 orçamentos, 4 deles já vendidos — contados como
R$ 0,00 no faturamento, com comissão apurada sobre zero.

O código passou a tratar 0 como ausência (`effective_total_override`). Esta
migração limpa o dado: apaga os zeros, recalcula o snapshot e reapura a
comissão congelada das vendas afetadas.
"""

from decimal import Decimal

from django.db import migrations


def clear_zero_override(apps, schema_editor):
    # Precisa dos modelos reais: as regras de total e de comissão não existem
    # no modelo histórico devolvido por `apps.get_model`.
    from sales.models import Quote, SOLD_STATUSES
    from sales.margin import persist_quote_commission

    afetados = list(
        Quote.objects.filter(total_override=Decimal("0.00")).values_list("pk", flat=True)
    )
    if not afetados:
        return

    Quote.objects.filter(pk__in=afetados).update(total_override=None)

    for quote in Quote.objects.filter(pk__in=afetados).prefetch_related("items"):
        try:
            total = quote.calculate_total_for_tier()
        except Exception:
            continue
        if total != quote.total_value_snapshot:
            Quote.objects.filter(pk=quote.pk).update(total_value_snapshot=total)
        if quote.status in SOLD_STATUSES:
            try:
                persist_quote_commission(quote)
            except Exception:
                # Uma venda sem tarifa cadastrada não pode travar o deploy; ela
                # segue com a comissão antiga até ser reaberta no simulador.
                continue


def noop_reverse(apps, schema_editor):
    """Sem volta: gravar 0,00 de novo reintroduziria o defeito."""


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0032_quote_submission_token"),
    ]

    operations = [
        migrations.RunPython(clear_zero_override, noop_reverse),
    ]
