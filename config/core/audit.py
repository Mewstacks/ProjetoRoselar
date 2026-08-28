"""Trilha de auditoria com antes/depois para o orçamento (a venda).

Antes daqui `EDIT_QUOTE` gravava só "Orçamento X editado" com `extra_data`
vazio: dava para saber que alguém mexeu, nunca no quê. Quando a venda anotada
no papel diverge da que está no sistema, o vendedor corrige direto — e sem o
diff o admin não tem como conferir a correção depois.

A chave do diff é o rótulo em português, gravado no momento da escrita. Assim o
visualizador é um `for` burro sobre o JSON, sem filtro custom e sem mapa de
tradução, e uma linha antiga (sem `diff`) simplesmente não renderiza nada.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from core.models import AuditLog

# Quantas entradas de item cabem numa lista do diff antes de virar "…+N".
_ITEM_LIST_CAP = 20

# Campos que entram no retrato. Só o que mexe em dinheiro ou joga a venda de um
# ciclo de comissão para outro. Ficam de fora de propósito: observações, prazo
# de entrega, transportadora, token de submissão, arredondamento legado e
# imagens de item — ruído que afogaria o diff sem mudar um centavo.
QUOTE_FIELD_LABELS: dict[str, str] = {
    "status": "Situação",
    "seller": "Vendedor",
    "sale_date": "Data da venda",
    "quote_date": "Data do orçamento",
    "total_value_snapshot": "Total da venda",
    "total_override": "Preço final ao cliente",
    "freight_value": "Frete",
    "freight_responsible": "Frete por conta de",
    "discount_percent": "Desconto (%)",
    "price_increase_percent": "Acréscimo (%)",
    "payment_type": "Pagamento",
    "payment_installments": "Parcelas",
    "payment_fee_percent": "Taxa (%)",
    "payment_type_2": "Pagamento 2",
    "payment_installments_2": "Parcelas 2",
    "payment_fee_percent_2": "Taxa 2 (%)",
    "payment_split_amount": "Valor da 2ª forma",
    "down_payment_value": "Entrada",
    "selected_price_tier": "Modalidade",
    "dual_pricing": "Varejo/Atacado",
    "has_architect": "Tem arquiteto",
    "architect": "Arquiteto",
    "commission_pct": "Comissão (%)",
    "commission_value": "Comissão (R$)",
    "commission_source": "Origem da comissão",
    "commission_split": "Divisão de comissão",
}

_ITEMS_ADDED = "Itens incluídos"
_ITEMS_REMOVED = "Itens removidos"
_ITEMS_CHANGED = "Itens alterados"


def _plain(value):
    """Torna o valor gravável em JSONField.

    Obrigatório: o sqlite local engole um `Decimal` cru e o Postgres da
    produção estoura. Sem isto o erro só aparece no Railway.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _payment_label(value) -> str | None:
    from core.models import PaymentMethodType
    if not value:
        return None
    return dict(PaymentMethodType.choices).get(value, value)


def _item_digest(quote) -> dict[str, str]:
    """Retrato compacto dos itens: `{pk: "Mesa x2 @1200.00"}`.

    Guardar o item inteiro faria um orçamento de 300 linhas gerar um payload
    ilegível a cada salvamento. Nome, quantidade e preço são o que muda o
    valor da venda — o resto não pertence a uma trilha financeira.
    """
    return {
        str(item.pk): f"{item.product_name} x{item.quantity} @{item.unit_value or Decimal('0.00')}"
        for item in quote.items.all()
    }


def _join(values: list[str]) -> str:
    """Junta as linhas de item, cortando na trave para o payload não explodir."""
    if len(values) > _ITEM_LIST_CAP:
        values = values[:_ITEM_LIST_CAP] + [f"…+{len(values) - _ITEM_LIST_CAP}"]
    return "; ".join(values)


def quote_financial_snapshot(quote) -> dict:
    """Retrato dos campos financeiros do orçamento, pronto para JSON."""
    split = getattr(quote, "commission_split", None)
    split_names = (
        sorted(u.get_full_name() or u.username for u in split.users.all())
        if split else []
    )

    snapshot = {
        "status": quote.get_status_display(),
        "seller": quote.seller.get_full_name() or quote.seller.username if quote.seller_id else None,
        "sale_date": _plain(quote.sale_date),
        "quote_date": _plain(quote.quote_date),
        "total_value_snapshot": _plain(quote.total_value_snapshot),
        # `effective_total_override` e não o campo cru: um "0,00" gravado por
        # engano é ausência de preço final, não um preço de zero real.
        "total_override": _plain(quote.effective_total_override),
        "freight_value": _plain(quote.freight_value),
        "freight_responsible": quote.get_freight_responsible_display(),
        "discount_percent": _plain(quote.discount_percent),
        "price_increase_percent": _plain(quote.price_increase_percent),
        # `payment_type` é CharField solto (as escolhas vivem em
        # core.PaymentMethodType), então não há get_..._display aqui.
        "payment_type": _payment_label(quote.payment_type),
        "payment_installments": quote.payment_installments,
        "payment_fee_percent": _plain(quote.payment_fee_percent),
        "payment_type_2": _payment_label(quote.payment_type_2),
        "payment_installments_2": quote.payment_installments_2,
        "payment_fee_percent_2": _plain(quote.payment_fee_percent_2),
        "payment_split_amount": _plain(quote.payment_split_amount),
        "down_payment_value": _plain(quote.down_payment_value),
        "selected_price_tier": quote.get_selected_price_tier_display() if quote.selected_price_tier else None,
        "dual_pricing": quote.dual_pricing,
        "has_architect": quote.has_architect,
        "architect": quote.architect.name if quote.architect_id else None,
        "commission_pct": _plain(quote.commission_pct),
        "commission_value": _plain(quote.commission_value),
        "commission_source": quote.commission_source or None,
        "commission_split": split_names,
    }
    snapshot["items"] = _item_digest(quote)
    return snapshot


def _format(value) -> str:
    if value is None or value == "":
        return "—"
    if value is True:
        return "Sim"
    if value is False:
        return "Não"
    if isinstance(value, list):
        return ", ".join(value) if value else "—"
    return str(value)


def diff_snapshots(before: dict, after: dict) -> dict:
    """Diferença entre dois retratos, já rotulada em português."""
    diff: dict = {}

    for field, label in QUOTE_FIELD_LABELS.items():
        old, new = before.get(field), after.get(field)
        if old != new:
            diff[label] = {"de": _format(old), "para": _format(new)}

    old_items = before.get("items") or {}
    new_items = after.get("items") or {}

    added = [v for k, v in new_items.items() if k not in old_items]
    removed = [v for k, v in old_items.items() if k not in new_items]
    changed = [
        (old_items[k], v)
        for k, v in new_items.items()
        if k in old_items and old_items[k] != v
    ]

    # Toda entrada do diff tem a mesma forma {de, para} — inclusive as de item.
    # É o que deixa o visualizador ser um `for` de duas linhas, sem filtro nem
    # ramo especial para lista.
    if added:
        diff[_ITEMS_ADDED] = {"de": "—", "para": _join(added)}
    if removed:
        diff[_ITEMS_REMOVED] = {"de": _join(removed), "para": "—"}
    if changed:
        diff[_ITEMS_CHANGED] = {
            "de": _join([old for old, _ in changed]),
            "para": _join([new for _, new in changed]),
        }

    return diff


def log_quote_diff(user, action, quote, before, *, description="", ip_address=None):
    """Grava um `AuditLog` com o antes/depois. Nada mudou, nada é gravado.

    Devolver None no diff vazio é o que impede o log de encher de "Orçamento X
    editado" toda vez que alguém abre o formulário e salva sem mexer em nada.
    """
    diff = diff_snapshots(before, quote_financial_snapshot(quote))
    if not diff:
        return None
    return AuditLog.log(
        user,
        action,
        description or f"Orçamento {quote.number} alterado.",
        obj=quote,
        ip_address=ip_address,
        extra_data={"quote": quote.number, "diff": diff},
    )
