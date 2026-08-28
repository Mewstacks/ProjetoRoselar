"""Divisão de comissão de uma venda entre vendedores.

Um único lugar que escreve `QuoteCommissionSplit`. Antes a validação vivia
inline dentro de `quote_convert_to_orders`, o que deixava a divisão editável só
no instante da conversão: depois que a venda fechava, o modal sumia e não havia
outra porta. Aqui a regra fica isolada para que a conversão e a edição pós-venda
usem exatamente o mesmo caminho.

Não mora em `models.py` (que não deve conhecer `accounts.Role`) nem em
`margin.py` (motor de preço, sem dado vindo de request).
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model

from sales.models import Quote, QuoteCommissionSplit


def _names(users) -> list[str]:
    return sorted(u.get_full_name() or u.username for u in users)


def valid_split_sellers(quote: Quote, raw_ids) -> list:
    """Vendedores ativos escolhidos para dividir, sem o dono do orçamento.

    O dono entra sempre depois (ver `apply_commission_split`), então mandá-lo
    junto na lista é redundante — e mandá-lo de fora não o remove.
    """
    from accounts.models import Role

    User = get_user_model()
    ids: set[int] = set()
    for raw in raw_ids or []:
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    ids.discard(quote.seller_id)
    if not ids:
        return []
    return list(
        User.objects.filter(pk__in=ids, is_active=True, role=Role.SELLER)
        .order_by("first_name", "username")
    )


def apply_commission_split(quote: Quote, raw_ids) -> dict:
    """Grava a divisão do orçamento. Devolve o antes/depois para a auditoria.

    Sem participante extra válido a linha é APAGADA, não esvaziada: uma linha
    vazia deixa `getattr(quote, "commission_split", None)` verdadeiro e mente
    para qualquer checagem futura. O relatório já ignora lista vazia e
    `get_sellers()` já cai no `quote.seller`, então apagar mantém um único
    estado canônico de "sem divisão".
    """
    existing = QuoteCommissionSplit.objects.filter(quote=quote).first()
    before = _names(existing.users.all()) if existing else []

    extras = valid_split_sellers(quote, raw_ids)

    if not extras:
        if existing:
            existing.delete()
        after: list[str] = []
    else:
        split = existing or QuoteCommissionSplit.objects.create(quote=quote)
        # O dono da venda nunca sai da divisão: `report_sales` agrupa o
        # faturamento por `Quote.seller`, então tirá-lo daqui creditaria a
        # receita a ele e a comissão a outro — divergência sem causa visível.
        split.users.set([quote.seller, *extras])
        after = _names(split.users.all())

    return {"before": before, "after": after, "changed": before != after}


def split_participants(quote: Quote) -> list[dict]:
    """Quem recebe o quê nesta venda, na mesma conta do relatório.

    O divisor é o número de participantes, não o de quem recebe: a parte de um
    usuário do financeiro evapora em vez de engordar a dos vendedores (mesma
    regra de `core.views.report_commissions`).
    """
    from accounts.models import Role

    split = getattr(quote, "commission_split", None)
    users = list(split.users.all()) if split else []
    if not users:
        users = [quote.seller]

    shares = Decimal(len(users))
    total = quote.commission_value

    rows = []
    for user in users:
        is_finance = user.role == Role.FINANCE
        share = None
        if total is not None and not is_finance:
            share = (total / shares).quantize(Decimal("0.01"))
        rows.append({
            "user": user,
            "name": user.get_full_name() or user.username,
            "is_finance": is_finance,
            "is_inactive": not user.is_active,
            "is_owner": user.pk == quote.seller_id,
            "share": share,
        })
    rows.sort(key=lambda r: (not r["is_owner"], r["name"]))
    return rows
