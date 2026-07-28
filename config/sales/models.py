from __future__ import annotations

import io
import logging
import re
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.utils import timezone
from PIL import Image as PILImage, ImageOps


QUOTE_ITEM_IMAGE_SIZE = (900, 900)  # Fixed normalized size for PDF


def quote_item_image_path(instance: "QuoteItemImage", filename: str) -> str:
    """Permanent storage path for quote item images."""
    return f"quotes/{instance.item.quote.number}/items/{instance.item_id}/{filename}"


def quote_item_tmp_path(instance: "QuoteItemImage", filename: str) -> str:
    # kept for backwards compat with old migrations
    return quote_item_image_path(instance, filename)


def validate_discount_percent(value: Decimal):
    if value is None:
        return
    if value < 0 or value > 100:
        raise ValidationError("Desconto deve estar entre 0 e 100.")


class QuoteStatus(models.TextChoices):
    DRAFT = "DRAFT", "Rascunho"
    SENT = "SENT", "Enviado"
    APPROVED = "APPROVED", "Aprovado"
    CONVERTED = "CONVERTED", "Convertido em Pedido"
    POS_VENDA = "POS_VENDA", "Pós-Venda"
    CANCELED = "CANCELED", "Cancelado"


class PriceTier(models.TextChoices):
    RETAIL = "RETAIL", "Varejo"
    WHOLESALE = "WHOLESALE", "Atacado"


# Status que contam como venda em métricas e relatórios. Um orçamento
# convertido continua sendo venda depois que o pedido é concluído e ele
# avança para Pós-Venda — sem isso, a venda "sumiria" dos painéis.
SOLD_STATUSES = (QuoteStatus.CONVERTED, QuoteStatus.POS_VENDA)


class FreightResponsible(models.TextChoices):
    """Responsável pelo pagamento do frete."""
    STORE = "STORE", "Frete Próprio - Empresa"
    CARRIER = "CARRIER", "Transportadora"
    CUSTOMER = "CUSTOMER", "Cliente"


class RoundingMode(models.TextChoices):
    """Granularidade do arredondamento do total de venda ao cliente."""
    NONE = "NONE", "Sem arredondamento"
    R1 = "R1", "Real (R$ 1)"
    R10 = "R10", "Dezena (R$ 10)"
    R50 = "R50", "R$ 50"
    R100 = "R100", "Centena (R$ 100)"


ROUNDING_STEPS = {
    RoundingMode.R1: Decimal("1"),
    RoundingMode.R10: Decimal("10"),
    RoundingMode.R50: Decimal("50"),
    RoundingMode.R100: Decimal("100"),
}


def build_payment_plan(
    *,
    total: Decimal,
    tier: str,
    payment_type: str = "",
    installments: int = 1,
    payment_type_2: str = "",
    installments_2: int = 1,
    split_amount: Decimal | None = None,
    down_payment: Decimal | None = None,
) -> dict:
    """Monta uma projeção de pagamento para um total de varejo ou atacado.

    A mesma função alimenta PDF, detalhe e simulador, impedindo que cada tela
    calcule parcelas de uma base diferente. Taxas bancárias são custo da loja e
    não alteram o total nem as parcelas apresentadas ao cliente.
    """
    from core.models import PaymentMethodType, payment_condition_label, payment_description

    total = max(Decimal("0"), Decimal(str(total or 0)))
    n1 = max(1, int(installments or 1))
    n2 = max(1, int(installments_2 or 1))
    labels = dict(PaymentMethodType.choices)

    def _leg(method: str, count: int, amount: Decimal, role: str) -> dict:
        method_label = labels.get(method, method or "")
        condition = payment_condition_label(method, count)
        description = payment_description(method, count).replace(" - ", " — ")
        return {
            "role": role,
            "method": method,
            "method_label": method_label,
            "installments": count,
            "amount": amount,
            "installment_value": amount / Decimal(count),
            "description": description,
        }

    split_active = bool(payment_type_2) and split_amount is not None
    if split_active:
        first_amount = min(
            max(Decimal("0"), Decimal(str(split_amount or 0))),
            total,
        )
        second_amount = total - first_amount
        effective_down_payment = Decimal("0")
        legs = [
            _leg(payment_type, n1, first_amount, "Entrada"),
            _leg(payment_type_2, n2, second_amount, "Restante"),
        ]
    else:
        effective_down_payment = min(
            max(Decimal("0"), Decimal(str(down_payment or 0))),
            total,
        )
        financed = total - effective_down_payment
        first_amount = financed
        second_amount = Decimal("0")
        legs = [_leg(payment_type, n1, financed, "Pagamento")]

    return {
        "tier": tier,
        "tier_label": PriceTier(tier).label,
        "total": total,
        "split_active": split_active,
        "down_payment": effective_down_payment,
        "financed": total - effective_down_payment,
        "first_amount": first_amount,
        "second_amount": second_amount,
        "legs": legs,
        "primary": legs[0],
        "secondary": legs[1] if len(legs) > 1 else None,
    }


class CommissionSource(models.TextChoices):
    """Como a comissão gravada no orçamento foi obtida.

    ENGINE   — apurada pelo motor de margem no fechamento da venda, com todos
               os parâmetros da venda disponíveis. É o número confiável.
    BACKFILL — estimada retroativamente pela migração para vendas fechadas
               antes deste campo existir. A entrada (down payment) daquelas
               vendas não foi registrada, então é aproximação, e os relatórios
               precisam sinalizar isso.
    """
    ENGINE = "ENGINE", "Apurada no fechamento"
    BACKFILL = "BACKFILL", "Estimada retroativamente"


class QuoteQuerySet(models.QuerySet):
    def sold(self):
        """Orçamentos que contam como venda (convertidos, inclusive em Pós-Venda).

        Anota `sold_on` com a data efetiva da venda: a data da conversão
        (sale_date) quando registrada, senão a data do orçamento — fallback
        para registros anteriores à existência do campo.
        """
        from django.db.models.functions import Coalesce

        return self.filter(status__in=SOLD_STATUSES).annotate(
            sold_on=Coalesce("sale_date", "quote_date")
        )


class Quote(models.Model):
    number = models.CharField(max_length=20, unique=True, verbose_name="Número")

    customer = models.ForeignKey(
        "core.Customer",
        on_delete=models.PROTECT,
        related_name="quotes",
        verbose_name="Cliente",
    )
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="quotes",
        verbose_name="Vendedor",
    )

    status = models.CharField(max_length=12, choices=QuoteStatus.choices, default=QuoteStatus.DRAFT, verbose_name="Status")

    quote_date = models.DateField(default=timezone.localdate, verbose_name="Data")

    # data em que o orçamento virou venda (conversão em pedido). É esta data
    # que contabiliza a venda nos painéis/relatórios — não a data do orçamento.
    sale_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Data da Venda",
        help_text="Data em que o orçamento foi convertido em pedido.",
    )
    
    # prazo de entrega estimado em dias (no orçamento)
    delivery_days_min = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Prazo Mínimo de Entrega (dias)",
        help_text="Prazo mínimo estimado em dias"
    )
    delivery_days_max = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Prazo Máximo de Entrega (dias)",
        help_text="Prazo máximo estimado em dias"
    )
    
    # frete
    freight_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Valor do Frete")
    freight_responsible = models.CharField(
        max_length=10,
        choices=FreightResponsible.choices,
        default=FreightResponsible.CUSTOMER,
        verbose_name="Responsável pelo Frete",
        help_text="Quem paga o frete"
    )
    shipping_company = models.ForeignKey(
        "core.ShippingCompany",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="quotes",
        verbose_name="Transportadora",
        help_text="Transportadora responsável (se aplicável)"
    )
    shipping_payment_method = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Forma de Pagamento (Frete)",
        help_text="Método de pagamento selecionado da transportadora"
    )

    # desconto
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=Decimal("0.0"),
        verbose_name="Desconto (%)",
        validators=[validate_discount_percent],
    )
    discount_authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authorized_quote_discounts",
        verbose_name="Desconto Autorizado por",
    )
    discount_authorized_at = models.DateTimeField(null=True, blank=True, verbose_name="Desconto Autorizado em")

    # ajuste de preço (markup repassado ao cliente) — definido na simulação
    price_increase_percent = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=Decimal("0.0"),
        verbose_name="Ajuste de Preço (%)",
        help_text="Acréscimo percentual sobre o subtotal, repassado ao cliente.",
    )

    # arquiteto
    has_architect = models.BooleanField(
        default=False,
        verbose_name="Possui Arquiteto",
        help_text="Cliente possui arquiteto?"
    )
    architect = models.ForeignKey(
        "core.Architect",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quotes",
        verbose_name="Arquiteto",
    )

    # condição/pagamento
    payment_type = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="Tipo de Pagamento",
        help_text="Tipo de pagamento (dinheiro, pix, cartão, etc.)"
    )
    payment_installments = models.PositiveIntegerField(
        default=1,
        verbose_name="Parcelas",
        help_text="Número de parcelas (1 = à vista)"
    )
    payment_fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Taxa de Pagamento (%)",
        help_text="Taxa (juros) aplicada conforme parcelas"
    )

    # split payment (second method — optional)
    payment_type_2 = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="Tipo de Pagamento 2",
        help_text="Segundo método (pagamento dividido entre dois meios)"
    )
    payment_installments_2 = models.PositiveIntegerField(
        default=1,
        verbose_name="Parcelas 2",
    )
    payment_fee_percent_2 = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Taxa de Pagamento 2 (%)",
    )
    payment_split_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Valor no Pagamento 1 (R$)",
        help_text="Quanto do total vai ao primeiro método; o restante vai ao segundo."
    )

    # preço final de venda ao cliente (override): quando preenchido, é o valor
    # exato do total ao cliente. Substitui o antigo arredondamento + ajuste manual.
    total_override = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Preço Final ao Cliente (R$)",
        help_text="Valor exato do total ao cliente. Deixe em branco para usar o total calculado.",
    )

    # ── legado (mantidos por compatibilidade; não exibidos na UI) ────────────
    total_rounding_mode = models.CharField(
        max_length=5,
        choices=RoundingMode.choices,
        default=RoundingMode.NONE,
        verbose_name="Arredondamento do Total",
        help_text="Arredonda o total de venda ao cliente para um número redondo.",
    )
    total_manual_adjustment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Ajuste Manual (R$)",
        help_text="Valor somado (ou subtraído, se negativo) ao total após o arredondamento.",
    )

    # Orçamento atacado + varejo: quando ligado, cada item carrega também um
    # preço de atacado (QuoteItem.unit_value_wholesale) e o PDF/detalhe mostram
    # os dois totais (varejo e atacado) lado a lado.
    dual_pricing = models.BooleanField(
        default=False,
        verbose_name="Orçamento Atacado + Varejo",
        help_text="Mostra preços de varejo e atacado lado a lado.",
    )
    selected_price_tier = models.CharField(
        max_length=10,
        choices=PriceTier.choices,
        default=PriceTier.RETAIL,
        verbose_name="Modalidade Fechada",
        help_text="Preço efetivamente escolhido pelo cliente na conversão da venda.",
    )

    # observações gerais do orçamento
    notes = models.TextField(blank=True, verbose_name="Observações")

    # entrada (down payment) usada na simulação. Precisa ser persistida: sem ela
    # a comissão apurada pelo motor de margem é irreproduzível depois da venda.
    down_payment_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Entrada (R$)",
    )

    # snapshots
    total_value_snapshot = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Total (R$)")

    # ── Comissão apurada no fechamento da venda ───────────────────────────────
    # Gravada pelo motor de margem (sales.margin) quando o orçamento vira venda.
    # Os relatórios apenas SOMAM estes campos — não recalculam — para não
    # divergirem do simulador, que é a regra vista pelo vendedor na hora da venda.
    commission_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        verbose_name="Comissão (%)",
    )
    commission_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name="Comissão (R$)",
    )
    commission_calculated_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Comissão apurada em",
    )
    commission_source = models.CharField(
        max_length=10,
        choices=CommissionSource.choices,
        blank=True,
        default="",
        verbose_name="Origem da comissão",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")

    objects = QuoteQuerySet.as_manager()

    class Meta:
        verbose_name = "Orçamento"
        verbose_name_plural = "Orçamentos"
        indexes = [
            models.Index(fields=["number"]),
            models.Index(fields=["quote_date"]),
            models.Index(fields=["sale_date"], name="sales_quote_sale_date_idx"),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"Orçamento {self.number}"

    @property
    def has_orders(self) -> bool:
        return self.orders.exists()
    
    def get_payment_description(self) -> str:
        """Retorna descrição formatada do pagamento."""
        from core.models import payment_description

        if not self.payment_type:
            return "Não definido"

        desc1 = payment_description(self.payment_type, self.payment_installments)

        if self.payment_type_2 and self.payment_split_amount is not None:
            desc2 = payment_description(
                self.payment_type_2,
                self.payment_installments_2,
            )
            return f"{desc1} + {desc2}"

        return desc1
    
    def calculate_subtotal(self) -> Decimal:
        """Calcula subtotal dos itens (varejo) sem desconto e sem frete."""
        subtotal = sum(item.line_total for item in self.items.all())
        return Decimal(str(subtotal))

    def calculate_subtotal_wholesale(self) -> Decimal:
        """Subtotal dos itens no preço de atacado (sem desconto e sem frete)."""
        subtotal = sum(item.line_total_wholesale for item in self.items.all())
        return Decimal(str(subtotal))

    @property
    def billable_freight(self) -> Decimal:
        """Frete efetivamente repassado ao cliente.

        Quando o frete é 'Frete Próprio - Empresa' (STORE), a loja arca com o
        custo e ele NÃO entra no total de venda ao cliente — caso contrário o
        transporte inflaria o total e, com ele, a comissão do vendedor.
        """
        if self.freight_responsible == FreightResponsible.STORE:
            return Decimal("0.00")
        return self.freight_value or Decimal("0.00")

    def calculate_total_with_freight_and_discount(self, subtotal: Decimal = None) -> Decimal:
        """Calcula total com frete, ajuste de preço e desconto, mas SEM taxa de pagamento.

        O ajuste de preço (markup) e o desconto incidem apenas sobre os produtos
        (subtotal), não sobre o frete, alinhado com o motor de simulação
        (_run_simulation): adj = subtotal × (1 + ajuste% − desconto%).

        O frete só entra no total quando é repassado ao cliente (billable_freight);
        frete por conta da loja (STORE) fica de fora.

        `subtotal` opcional permite reaproveitar a mesma regra para o total de
        atacado (passando calculate_subtotal_wholesale()); por padrão usa o
        subtotal de varejo.
        """
        if subtotal is None:
            subtotal = self.calculate_subtotal()
        markup_pct = self.price_increase_percent or Decimal("0.0")
        discount_pct = self.discount_percent or Decimal("0.0")
        adj_subtotal = subtotal * (
            Decimal("1") + markup_pct / Decimal("100") - discount_pct / Decimal("100")
        )
        return adj_subtotal + self.billable_freight
    
    def calculate_payment_fee_value(self, base_total: Decimal | None = None) -> Decimal:
        """Calcula o custo bancário da modalidade efetivamente selecionada."""
        if base_total is None:
            base_total = self.calculate_total_for_tier()
        if self.payment_type_2 and self.payment_split_amount is not None:
            split_1 = min(self.payment_split_amount, base_total)
            split_2 = max(Decimal("0"), base_total - split_1)
            fee1 = split_1 * (self.payment_fee_percent or Decimal("0")) / Decimal("100")
            fee2 = split_2 * (self.payment_fee_percent_2 or Decimal("0")) / Decimal("100")
            return fee1 + fee2
        fee_value = base_total * (self.payment_fee_percent or Decimal("0.000")) / Decimal("100")
        return fee_value
    
    def calculate_final_total(self) -> Decimal:
        """Calcula o total final incluindo taxa de pagamento."""
        base_total = self.calculate_total_for_tier()
        fee_value = self.calculate_payment_fee_value(base_total)
        return base_total + fee_value

    def apply_client_rounding(self, base: Decimal, use_override: bool = True) -> Decimal:
        """Total de venda ao cliente a partir de um valor base.

        Se houver preço final digitado (total_override), ele manda: é o valor
        exato ao cliente, ignorando o valor base. Caso contrário, mantém o
        comportamento legado (arredondamento + ajuste manual) para orçamentos
        antigos. Lógica única reutilizada pelo snapshot do pedido e pelo PDF do
        cliente (para não divergirem).

        `use_override=False` ignora o total_override — usado pelo total de
        atacado, já que o preço final digitado se refere só ao total de varejo.
        """
        if use_override and self.total_override is not None:
            return self.total_override
        step = ROUNDING_STEPS.get(self.total_rounding_mode)
        if step:
            base = (base / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
        return base + (self.total_manual_adjustment or Decimal("0.00"))

    def calculate_rounded_total(self) -> Decimal:
        """Total de venda ao cliente com arredondamento e ajuste manual.

        Parte do total 'para o cliente' (subtotal ajustado + frete, sem taxa de
        pagamento — igual ao snapshot), arredonda conforme o modo escolhido e
        soma o ajuste manual (pode ser negativo).
        """
        base = self.calculate_total_with_freight_and_discount()
        return self.apply_client_rounding(base)

    def calculate_rounded_total_wholesale(
        self,
        markup_pct: Decimal | None = None,
    ) -> Decimal:
        """Total de venda ao cliente no preço de atacado.

        O desconto comercial do orçamento incide exclusivamente sobre o
        varejo. O atacado já é uma segunda tabela de preços e não pode receber
        o desconto novamente. Mantém apenas o ajuste de preço, o frete e o
        arredondamento/ajuste legado. Ignora ``total_override``, que se refere
        somente ao total de varejo.
        """
        subtotal = self.calculate_subtotal_wholesale()
        if markup_pct is None:
            markup_pct = self.price_increase_percent or Decimal("0.0")
        base = (
            subtotal * (Decimal("1") + markup_pct / Decimal("100"))
            + self.billable_freight
        )
        return self.apply_client_rounding(base, use_override=False)

    def calculate_subtotal_for_tier(self, tier: str | None = None) -> Decimal:
        """Subtotal de produtos da modalidade informada ou efetivamente fechada."""
        tier = tier or self.selected_price_tier
        if self.dual_pricing and tier == PriceTier.WHOLESALE:
            return self.calculate_subtotal_wholesale()
        return self.calculate_subtotal()

    def calculate_total_for_tier(self, tier: str | None = None) -> Decimal:
        """Total final da modalidade informada ou efetivamente fechada."""
        tier = tier or self.selected_price_tier
        if self.dual_pricing and tier == PriceTier.WHOLESALE:
            return self.calculate_rounded_total_wholesale()
        return self.calculate_rounded_total()

    def get_payment_plans(self) -> list[dict]:
        """Opções de pagamento persistidas, uma por modalidade oferecida."""
        common = {
            "payment_type": self.payment_type or "",
            "installments": self.payment_installments or 1,
            "payment_type_2": self.payment_type_2 or "",
            "installments_2": self.payment_installments_2 or 1,
            "split_amount": self.payment_split_amount,
            "down_payment": self.down_payment_value,
        }
        plans = [
            build_payment_plan(
                total=self.calculate_rounded_total(),
                tier=PriceTier.RETAIL,
                **common,
            )
        ]
        if self.dual_pricing:
            plans.append(
                build_payment_plan(
                    total=self.calculate_rounded_total_wholesale(),
                    tier=PriceTier.WHOLESALE,
                    **common,
                )
            )
        return plans



class QuoteItem(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="items")

    # fornecedor opcional no orçamento, mas NECESSÁRIO para gerar pedido por fornecedor
    supplier = models.ForeignKey(
        "core.Supplier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="quote_items",
    )

    product_name = models.CharField(max_length=160, verbose_name="Produto")
    description = models.TextField(blank=True, verbose_name="Descrição")
    environment = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Ambiente",
        help_text="Ex.: Dormitório, Cozinha ou Sala de Estar.",
    )
    position = models.PositiveIntegerField(
        default=0,
        verbose_name="Posição",
        help_text="Ordem do item no orçamento e no PDF.",
    )

    quantity = models.PositiveIntegerField(default=1, verbose_name="Qtd")
    unit_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Valor Unit.")
    # Preço de atacado por item (opcional). Só usado quando Quote.dual_pricing
    # está ligado; unit_value continua sendo o preço de varejo.
    unit_value_wholesale = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name="Valor Unit. Atacado",
    )

    # condição do item (se precisar)
    condition_text = models.CharField(max_length=200, blank=True, verbose_name="Condição")

    # % arquiteto: armazenar, mas NÃO exibir no orçamento/pedido final (regra sua)
    architect_percent = models.DecimalField(
        max_digits=4, 
        decimal_places=1, 
        null=True, 
        blank=True, 
        default=Decimal("0.0"),
        verbose_name="Comissão Arq. (%)"
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Item do Orçamento"
        verbose_name_plural = "Itens do Orçamento"
        indexes = [
            models.Index(fields=["quote"]),
            models.Index(fields=["supplier"]),
        ]

    def __str__(self) -> str:
        return f"{self.product_name} ({self.quote.number})"

    @property
    def line_total(self) -> Decimal:
        return (self.unit_value or Decimal("0.00")) * Decimal(self.quantity or 0)

    @property
    def wholesale_unit_value(self) -> Decimal:
        """Preço unitário de atacado, com fallback seguro para o varejo."""
        if self.unit_value_wholesale is not None:
            return self.unit_value_wholesale
        return self.unit_value or Decimal("0.00")

    @property
    def line_total_wholesale(self) -> Decimal:
        """Total da linha no preço de atacado.

        Cai de volta no preço de varejo (unit_value) quando o atacado do item
        não foi preenchido, para o total de atacado nunca ficar subestimado.
        """
        return self.wholesale_unit_value * Decimal(self.quantity or 0)


class QuoteItemImage(models.Model):
    item = models.ForeignKey(QuoteItem, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=quote_item_image_path)
    caption = models.CharField(max_length=120, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Imagem do Item"
        verbose_name_plural = "Imagens do Item"

    def save(self, *args, **kwargs):
        # Normalize image to fixed size on first save
        if self.image and not self.pk:  # only on creation
            try:
                img = PILImage.open(self.image)
                # Respect camera/phone orientation (EXIF) before anything else;
                # sem isso fotos de celular saem deitadas.
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                # Encaixa a imagem inteira no quadro mantendo proporção, SEM cortar,
                # e centraliza sobre fundo branco (letterbox). Assim o produto nunca
                # é cortado, independente de ser foto vertical, horizontal ou quadrada.
                fitted = ImageOps.contain(img, QUOTE_ITEM_IMAGE_SIZE, PILImage.LANCZOS)
                canvas = PILImage.new("RGB", QUOTE_ITEM_IMAGE_SIZE, (255, 255, 255))
                offset = (
                    (QUOTE_ITEM_IMAGE_SIZE[0] - fitted.width) // 2,
                    (QUOTE_ITEM_IMAGE_SIZE[1] - fitted.height) // 2,
                )
                canvas.paste(fitted, offset)
                # Save to buffer
                buf = io.BytesIO()
                canvas.save(buf, format="JPEG", quality=90, optimize=True)
                buf.seek(0)
                # Replace the file content
                name = self.image.name.rsplit('.', 1)[0] + '.jpg'
                self.image.save(name, ContentFile(buf.read()), save=False)
            except Exception:
                logger.warning(
                    "Falha ao processar imagem do item %s; salvando original.",
                    self.pk or "<new>",
                    exc_info=True,
                )
        super().save(*args, **kwargs)



class OrderStatus(models.TextChoices):
    PENDING = "PENDING", "Aguardando Aprovação"
    ONGOING = "ONGOING", "Em Andamento"
    DONE = "DONE", "Concluído"
    CANCELED = "CANCELED", "Cancelado"


class Order(models.Model):
    number = models.CharField(max_length=20, verbose_name="Número")  # igual ao Quote.number (não unique por permitir vários pedidos)
    # null = pedido avulso da loja (compra de estoque, sem venda/vendedor)
    quote = models.ForeignKey(Quote, null=True, blank=True, on_delete=models.PROTECT, related_name="orders", verbose_name="Orçamento")

    supplier = models.ForeignKey(
        "core.Supplier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="orders",
        verbose_name="Fornecedor",
    )
    is_total_conference = models.BooleanField(default=False, verbose_name="Conferência Total")

    status = models.CharField(max_length=10, choices=OrderStatus.choices, default=OrderStatus.PENDING, verbose_name="Status")

    purchase_condition_text = models.CharField(max_length=200, blank=True, verbose_name="Condição de Compra")
    transport_info = models.CharField(max_length=300, blank=True, verbose_name="Informações de Transporte")
    notes = models.TextField(blank=True, verbose_name="Observações")

    # data real de entrega (obrigatória ao converter o orçamento em pedido)
    delivery_deadline = models.DateField(
        null=True,
        blank=True,
        verbose_name="Prazo de Entrega (data real)",
        help_text="Data real de entrega acordada com o cliente"
    )

    # editável: usuário pode ajustar a data do pedido manualmente
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Criado em")

    class Meta:
        verbose_name = "Pedido de Compra"
        verbose_name_plural = "Pedidos de Compra"
        indexes = [
            models.Index(fields=["number"]),
            models.Index(fields=["quote"]),
            models.Index(fields=["supplier"]),
        ]
        constraints = [
            # 1 orçamento -> no máximo 1 "pedido total"
            models.UniqueConstraint(
                fields=["quote"],
                condition=models.Q(is_total_conference=True),
                name="uniq_total_order_per_quote",
            ),
            # se for pedido total, supplier deve ser null
            models.CheckConstraint(
                condition=~(models.Q(is_total_conference=True) & models.Q(supplier__isnull=False)),
                name="total_order_has_no_supplier",
            ),
        ]

    def clean(self):
        # validação extra: pedido normal (não total) deve ter supplier
        if not self.is_total_conference and self.supplier_id is None:
            raise ValidationError("Pedido por fornecedor precisa de fornecedor.")
        if self.is_total_conference and self.supplier_id is not None:
            raise ValidationError("Pedido total não pode ter fornecedor.")

    def __str__(self) -> str:
        return f"OC {self.number}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items", verbose_name="Pedido")

    product_name = models.CharField(max_length=160, verbose_name="Produto")
    description = models.TextField(blank=True, verbose_name="Descrição")

    quantity = models.PositiveIntegerField(default=1, verbose_name="Qtd")

    # custo de compra (interno).
    purchase_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Custo de Compra")

    # link opcional ao item do orçamento (useful para rastrear origem)
    quote_item = models.ForeignKey(
        QuoteItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
    )

    class Meta:
        verbose_name = "Item do Pedido"
        verbose_name_plural = "Itens do Pedido"

    def __str__(self) -> str:
        return f"{self.product_name} ({self.order.number})"
    
    @property
    def line_total(self) -> Decimal:
        """Calcula o total da linha."""
        return (self.purchase_unit_cost or Decimal("0.00")) * Decimal(self.quantity or 0)


# ── Proposal PDF configuration (singleton) ────────────────────────────────────
class ProposalConfig(models.Model):
    """
    Singleton – upload the decorative background images used in client proposal PDFs.
    Page 1 = cover, Page 2 = Sobre Nós.
    Only one record exists (pk=1); use ProposalConfig.get_config() to access it.
    """
    cover_image = models.ImageField(
        upload_to="proposal/cover/",
        null=True,
        blank=True,
        verbose_name="Imagem de Capa (página 1)",
        help_text="Fundo da capa. Tamanho recomendado: A4 portrait (2480×3508 px).",
    )
    about_image = models.ImageField(
        upload_to="proposal/about/",
        null=True,
        blank=True,
        verbose_name="Imagem 'Sobre Nós' (página 2)",
        help_text="Fundo da página Sobre Nós. Tamanho recomendado: A4 portrait.",
    )

    class Meta:
        verbose_name = "Configuração da Proposta"
        verbose_name_plural = "Configuração da Proposta"

    def __str__(self) -> str:
        return "Configuração da Proposta"

    @classmethod
    def get_config(cls) -> "ProposalConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ── Signals to keep Quote.total_value_snapshot up to date ─────────────────────
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


def _refresh_quote_snapshot(quote_id: int) -> None:
    """Recalculates and persists total_value_snapshot for a quote without triggering signals.

    A taxa de pagamento (cartão/boleto) é absorvida pela margem da loja e NÃO é
    repassada ao cliente. O total do snapshot é portanto subtotal−desconto+frete,
    sem acréscimo de taxa — idêntico ao 'Total para o Cliente' exibido no simulador.
    """
    try:
        quote = Quote.objects.prefetch_related('items').get(pk=quote_id)
        Quote.objects.filter(pk=quote_id).update(
            total_value_snapshot=quote.calculate_total_for_tier()
        )
    except Quote.DoesNotExist:
        pass


@receiver(post_save, sender=QuoteItem)
def _on_quote_item_save(sender, instance, **kwargs):
    _refresh_quote_snapshot(instance.quote_id)


@receiver(post_delete, sender=QuoteItem)
def _on_quote_item_delete(sender, instance, **kwargs):
    _refresh_quote_snapshot(instance.quote_id)


@receiver(post_save, sender=Quote)
def _on_quote_save(sender, instance, update_fields=None, **kwargs):
    # Skip if this save was triggered by _refresh_quote_snapshot itself
    if update_fields is not None and set(update_fields) == {'total_value_snapshot'}:
        return
    # Only recalculate when financially relevant fields may have changed
    # (runs on every save except snapshot-only saves to stay safe)
    _refresh_quote_snapshot(instance.pk)


# ── Commission Split ───────────────────────────────────────────────────────────
class QuoteCommissionSplit(models.Model):
    """
    Opcional: define como a comissão de um orçamento é dividida entre vendedores.
    Se nenhum usuário for selecionado (ou o registro não existir), 100% vai ao
    quote.seller original.  Se houver usuários, o valor é dividido igualmente.
    """
    quote = models.OneToOneField(
        Quote,
        on_delete=models.CASCADE,
        related_name="commission_split",
        verbose_name="Orçamento",
    )
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="shared_commission_quotes",
        verbose_name="Vendedores",
        blank=True,
    )

    class Meta:
        verbose_name = "Divisão de Comissão"
        verbose_name_plural = "Divisões de Comissão"

    def __str__(self) -> str:
        return f"Divisão de comissão — {self.quote.number}"

    def get_sellers(self):
        """Retorna lista de usuários que recebem comissão. Fallback para quote.seller."""
        users = list(self.users.all())
        return users if users else [self.quote.seller]


# ── Documentos / Notas Fiscais da venda ───────────────────────────────────────
class SaleDocumentType(models.TextChoices):
    NF_COMPRA = "NF_COMPRA", "NF de Compra"
    NF_CLIENTE = "NF_CLIENTE", "NF do Cliente"
    OUTRO = "OUTRO", "Outro"


def sale_document_path(instance: "SaleDocument", filename: str) -> str:
    """Storage path para documentos anexados à venda."""
    return f"quotes/{instance.quote.number}/documentos/{filename}"


class SaleDocument(models.Model):
    """Anexo (NF de compra, NF do cliente ou outro) vinculado a uma venda (Quote)."""
    quote = models.ForeignKey(
        Quote,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name="Venda",
    )
    doc_type = models.CharField(
        max_length=12,
        choices=SaleDocumentType.choices,
        default=SaleDocumentType.OUTRO,
        verbose_name="Tipo",
    )
    supplier = models.ForeignKey(
        "core.Supplier",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sale_documents",
        verbose_name="Fornecedor",
        help_text="Fornecedor da NF de compra (opcional).",
    )
    file = models.FileField(upload_to=sale_document_path, verbose_name="Arquivo")
    description = models.CharField(max_length=160, blank=True, verbose_name="Descrição")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_sale_documents",
        verbose_name="Enviado por",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="Enviado em")

    class Meta:
        verbose_name = "Documento da Venda"
        verbose_name_plural = "Documentos da Venda"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["quote"]),
            models.Index(fields=["doc_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_doc_type_display()} — {self.quote.number}"

    @property
    def filename(self) -> str:
        import os
        return os.path.basename(self.file.name) if self.file else ""

    @property
    def is_image(self) -> bool:
        name = (self.file.name or "").lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
