"""Recalcula total_value_snapshot de todos os orçamentos.

O snapshot é a base de TODO relatório de valores (Vendas, CSV, painéis, metas).
Ele é mantido por signals, mas os signals só disparam quando o orçamento ou seus
itens são salvos — nunca houve backfill quando a regra do total mudou.

Resultado: orçamentos não tocados desde a mudança "a taxa de pagamento é
absorvida pela margem da loja e NÃO é repassada ao cliente" continuam com o
total inflado pela taxa (ex.: crédito 6x a 8,58% ⇒ total 8,58% maior que o
real). Todo relatório de faturamento herda esse erro.

Esta migração reescreve o snapshot de todos os orçamentos usando a regra atual
(`calculate_rounded_total`), alinhando o histórico com o que o sistema calcula
hoje.
"""

from django.db import migrations


def refresh_snapshots(apps, schema_editor):
    # Precisa do modelo real: calculate_rounded_total() não existe no modelo
    # histórico que `apps.get_model` devolve.
    from sales.models import Quote

    for quote in (
        # Campos criados depois desta migração não existem ainda ao reconstruir
        # um banco do zero, embora o modelo Python atual já os conheça.
        Quote.objects.defer("selected_price_tier", "submission_token")
        .prefetch_related("items")
        .iterator(chunk_size=200)
    ):
        try:
            total = quote.calculate_rounded_total()
        except Exception:
            continue
        if total != quote.total_value_snapshot:
            Quote.objects.filter(pk=quote.pk).update(total_value_snapshot=total)


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0028_backfill_quote_commission"),
    ]

    operations = [
        # Irreversível por natureza: o valor antigo estava errado, não há para
        # onde voltar. `noop` mantém a migração revertível sem restaurar o erro.
        migrations.RunPython(refresh_snapshots, migrations.RunPython.noop),
    ]
