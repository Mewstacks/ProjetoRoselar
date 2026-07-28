from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from .validador import validate_cpf, validate_cnpj


class Customer(models.Model):
    name = models.CharField(max_length=120, verbose_name="Nome")

    cpf = models.CharField(
        max_length=14,
        blank=True,
        verbose_name="CPF",
        validators=[validate_cpf],
        help_text="CPF válido (com ou sem máscara)",
    )

    cnpj = models.CharField(
        max_length=18,
        blank=True,
        verbose_name="CNPJ",
        validators=[validate_cnpj],
        help_text="CNPJ válido (com ou sem máscara)",
    )

    phone = models.CharField(max_length=30, blank=True, verbose_name="Telefone")
    email = models.EmailField(blank=True, verbose_name="E-mail")
    notes = models.TextField(blank=True, verbose_name="Observações")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Cadastrado em")

    class Meta:
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"
        constraints = [
            models.UniqueConstraint(
                fields=["cpf"],
                name="uniq_customer_cpf",
                condition=~Q(cpf=""),
            ),
            models.UniqueConstraint(
                fields=["cnpj"],
                name="uniq_customer_cnpj",
                condition=~Q(cnpj=""),
            ),
        ]

    def __str__(self):
        if self.cpf:
            return f"{self.name} (PF - {self.cpf})"
        elif self.cnpj:
            return f"{self.name} (PJ - {self.cnpj})"
        return self.name


class Supplier(models.Model):
    name = models.CharField(max_length=120, verbose_name="Nome")
    supplier_number = models.PositiveIntegerField(
        unique=True,
        editable=False,
        verbose_name="Código",
    )

    email = models.EmailField(
        blank=True,
        verbose_name="E-mail",
    )
    
    phone = models.CharField(max_length=30, blank=True, verbose_name="Contato")
    notes = models.TextField(blank=True, verbose_name="Observações")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Cadastrado em")

    class Meta:
        verbose_name = "Fornecedor"
        verbose_name_plural = "Fornecedores"

    def save(self, *args, **kwargs):
        if not self.supplier_number:
            last = Supplier.objects.order_by('-supplier_number').values_list('supplier_number', flat=True).first()
            self.supplier_number = (last or 0) + 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.supplier_number})"


class ShippingCompany(models.Model):
    """Cadastro de transportadoras."""
    name = models.CharField(max_length=120, verbose_name="Nome", help_text="Nome da transportadora")
    
    cnpj = models.CharField(
        max_length=18,
        blank=True,
        verbose_name="CNPJ",
        validators=[validate_cnpj],
        help_text="CNPJ da transportadora (opcional)"
    )
    
    phone = models.CharField(max_length=30, blank=True, verbose_name="Telefone")
    email = models.EmailField(blank=True, verbose_name="E-mail")
    contact_person = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Contato",
        help_text="Pessoa de contato na transportadora"
    )
    
    address = models.TextField(blank=True, verbose_name="Endereço", help_text="Endereço completo")
    notes = models.TextField(blank=True, verbose_name="Observações", help_text="Observações")
    
    # Payment methods available for this shipping company
    payment_methods = models.TextField(
        blank=True,
        verbose_name="Métodos de Pagamento",
        help_text="Métodos de pagamento disponíveis (um por linha)"
    )
    
    is_active = models.BooleanField(default=True, verbose_name="Ativa", help_text="Transportadora ativa")
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Cadastrado em")
    
    class Meta:
        ordering = ["name"]
        verbose_name = "Transportadora"
        verbose_name_plural = "Transportadoras"
    
    def __str__(self) -> str:
        return self.name


class PaymentMethodType(models.TextChoices):
    """Tipos de métodos de pagamento fixos."""
    CASH = "CASH", "Dinheiro"
    PIX = "PIX", "PIX"
    DEBIT_CARD = "DEBIT_CARD", "Cartão de Débito"
    CREDIT_CARD = "CREDIT_CARD", "Cartão de Crédito"
    CHEQUE = "CHEQUE", "Cheque"
    BOLETO = "BOLETO", "Boleto"
    BOLETO_30 = "BOLETO_30", "Boleto para 30 dias"


def payment_condition_label(payment_type: str, installments: int = 1) -> str:
    """Descreve o prazo sem confundir boleto futuro com pagamento à vista."""
    if payment_type == PaymentMethodType.BOLETO_30:
        return "30 dias"
    installments = max(1, int(installments or 1))
    return "À vista" if installments == 1 else f"{installments}x"


def payment_description(payment_type: str, installments: int = 1) -> str:
    """Retorna a condição completa apresentada ao cliente e nos relatórios."""
    label = dict(PaymentMethodType.choices).get(payment_type, payment_type or "")
    if payment_type == PaymentMethodType.BOLETO_30:
        return label
    condition = payment_condition_label(payment_type, installments)
    return f"{label} - {condition}" if label else condition


class PaymentTariff(models.Model):
    """Tarifas configuráveis para métodos de pagamento parcelados."""
    payment_type = models.CharField(
        max_length=20,
        choices=PaymentMethodType.choices,
        verbose_name="Tipo de Pagamento",
        help_text="Tipo do método de pagamento"
    )
    
    installments = models.PositiveIntegerField(
        verbose_name="Parcelas",
        help_text="Número de parcelas (1 = à vista)"
    )
    
    fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name="Taxa (%)",
        help_text="Porcentagem de acréscimo (ex: 5.32 para 5.32%)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ["payment_type", "installments"]
        verbose_name = "Tarifa de Pagamento"
        verbose_name_plural = "Tarifas de Pagamento"
        unique_together = [["payment_type", "installments"]]
    
    def __str__(self) -> str:
        type_display = dict(PaymentMethodType.choices).get(self.payment_type, self.payment_type)
        if self.installments == 1:
            return f"{type_display} - À vista ({self.fee_percent}%)"
        return f"{type_display} - {self.installments}x ({self.fee_percent}%)"
    
    # CHEQUE não tem tabela própria: usa a do cartão como referência.
    # O boleto para 30 dias é uma condição comercial, não uma nova tarifa:
    # usa o custo cadastrado para uma cobrança de boleto.
    TARIFF_LOOKUP_OVERRIDES = {
        "CHEQUE": "CREDIT_CARD",
        "BOLETO_30": "BOLETO",
    }

    @classmethod
    def lookup_type(cls, payment_type):
        """Tipo cuja tabela de tarifas vale para `payment_type`."""
        return cls.TARIFF_LOOKUP_OVERRIDES.get(payment_type, payment_type)

    @classmethod
    def get_fee(cls, payment_type, installments):
        """Retorna a taxa cadastrada, ou None se a combinação não existir.

        None = parcelamento indisponível, nunca 0%. Sem tarifa cadastrada não há
        como saber o custo do banco, e assumir zero oferece a parcela de graça.
        """
        try:
            tariff = cls.objects.get(
                payment_type=cls.lookup_type(payment_type), installments=installments
            )
            return tariff.fee_percent
        except cls.DoesNotExist:
            return None


class Architect(models.Model):
    """Cadastro de arquitetos."""
    name = models.CharField(max_length=160, verbose_name="Nome Completo")
    pix = models.CharField(max_length=120, blank=True, default="", verbose_name="Chave PIX")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Cadastrado em")

    class Meta:
        verbose_name = "Arquiteto"
        verbose_name_plural = "Arquitetos"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ArchitectCommission(models.Model):
    """Configuração global para comissão de arquiteto (singleton)."""
    commission_percent = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=10.0,
        verbose_name="Comissão (%)",
        help_text="Porcentagem de comissão do arquiteto (ex: 10.0 para 10%)"
    )
    
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Comissão de Arquiteto"
        verbose_name_plural = "Comissão de Arquiteto"
    
    def __str__(self) -> str:
        return f"Comissão de Arquiteto: {self.commission_percent}%"
    
    def save(self, *args, **kwargs):
        # Força singleton - apenas um registro
        self.pk = 1
        super().save(*args, **kwargs)
    
    @classmethod
    def get_commission(cls):
        """Retorna a comissão configurada."""
        obj, created = cls.objects.get_or_create(pk=1)
        return obj.commission_percent


class SalesMarginConfig(models.Model):
    """Configuração global de margem / comissão do vendedor (singleton)."""
    total_margin = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=10.0,
        verbose_name="Margem (%)",
        help_text="Margem da empresa (%). Custos acima desse valor bloqueiam o orçamento.",
    )
    min_commission = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=2.0,
        verbose_name="Comissão Mínima (%)",
        help_text="Comissão mínima do vendedor (%)",
    )
    max_commission = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=5.0,
        verbose_name="Comissão Máxima (%)",
        help_text="Comissão máxima do vendedor (%) — quando desconto = 0",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Margem e Comissão de Vendedor"
        verbose_name_plural = "Margem e Comissão de Vendedor"

    def __str__(self) -> str:
        return (
            f"Margem {self.total_margin}% · "
            f"Comissão mín {self.min_commission}%"
        )

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_config(cls):
        """Retorna (total_margin, min_commission, max_commission)."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj.total_margin, obj.min_commission, obj.max_commission


# ──────────────────────────────────────────────────────────────────────
# Notification System
# ──────────────────────────────────────────────────────────────────────

class NotificationType(models.TextChoices):
    DELIVERY_NEAR = "DELIVERY_NEAR", "Entrega próxima"
    QUOTE_NO_RESPONSE = "QUOTE_NO_RESPONSE", "Orçamento sem resposta"
    GOAL_NEAR = "GOAL_NEAR", "Meta próxima de ser atingida"
    DISCOUNT_AUTH = "DISCOUNT_AUTH", "Desconto autorizado"
    ORDER_CONFIRMED = "ORDER_CONFIRMED", "Pedido confirmado"
    GENERAL = "GENERAL", "Geral"


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    notification_type = models.CharField(
        max_length=20,
        choices=NotificationType.choices,
        default=NotificationType.GENERAL,
        verbose_name="Tipo",
    )
    title = models.CharField(max_length=200, verbose_name="Título")
    message = models.TextField(blank=True, verbose_name="Mensagem")
    url = models.CharField(max_length=500, blank=True, verbose_name="Link", help_text="Link para a ação relacionada")
    read = models.BooleanField(default=False, verbose_name="Lida")
    read_at = models.DateTimeField(null=True, blank=True, verbose_name="Lida em")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Notificação"
        verbose_name_plural = "Notificações"
        indexes = [
            models.Index(fields=["recipient", "read", "-created_at"]),
        ]

    def __str__(self):
        return f"[{self.get_notification_type_display()}] {self.title}"

    def mark_as_read(self):
        if not self.read:
            self.read = True
            self.read_at = timezone.now()
            self.save(update_fields=["read", "read_at"])

    @classmethod
    def send(cls, recipient, title, notification_type=NotificationType.GENERAL, message="", url=""):
        return cls.objects.create(
            recipient=recipient,
            notification_type=notification_type,
            title=title,
            message=message,
            url=url,
        )


# ──────────────────────────────────────────────────────────────────────
# Audit Log
# ──────────────────────────────────────────────────────────────────────

class AuditAction(models.TextChoices):
    CREATE_QUOTE = "CREATE_QUOTE", "Criar orçamento"
    EDIT_QUOTE = "EDIT_QUOTE", "Editar orçamento"
    DELETE_QUOTE = "DELETE_QUOTE", "Excluir orçamento"
    APPROVE_DISCOUNT = "APPROVE_DISCOUNT", "Aprovar desconto"
    CONVERT_ORDER = "CONVERT_ORDER", "Converter em pedido"
    EDIT_VALUES = "EDIT_VALUES", "Editar valores"
    SAVE_CONDITIONS = "SAVE_CONDITIONS", "Salvar condições"
    LOGIN = "LOGIN", "Login"
    OTHER = "OTHER", "Outro"


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=20, choices=AuditAction.choices, verbose_name="Ação")
    description = models.TextField(blank=True, verbose_name="Descrição")
    object_type = models.CharField(max_length=50, blank=True, verbose_name="Tipo de Objeto", help_text="Ex: Quote, Order")
    object_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="ID do Objeto")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP")
    extra_data = models.JSONField(default=dict, blank=True, verbose_name="Dados Extras")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Log de Auditoria"
        verbose_name_plural = "Logs de Auditoria"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["action"]),
        ]

    def __str__(self):
        return f"{self.get_action_display()} por {self.user} em {self.created_at:%d/%m/%Y %H:%M}"

    @classmethod
    def log(cls, user, action, description="", obj=None, ip_address=None, extra_data=None):
        return cls.objects.create(
            user=user,
            action=action,
            description=description,
            object_type=type(obj).__name__ if obj else "",
            object_id=obj.pk if obj else None,
            ip_address=ip_address,
            extra_data=extra_data or {},
        )


# ──────────────────────────────────────────────────────────────────────
# Sales Goals
# ──────────────────────────────────────────────────────────────────────

class GoalPeriod(models.TextChoices):
    MONTHLY = "MONTHLY", "Mensal"
    QUARTERLY = "QUARTERLY", "Trimestral"
    YEARLY = "YEARLY", "Anual"


class GoalType(models.TextChoices):
    INDIVIDUAL = "INDIVIDUAL", "Individual"
    COLLECTIVE = "COLLECTIVE", "Coletivo"


class SalesGoal(models.Model):
    goal_type = models.CharField(max_length=12, choices=GoalType.choices, default=GoalType.INDIVIDUAL, verbose_name="Tipo")
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="sales_goals",
        verbose_name="Vendedor",
        help_text="Vendedor (para metas individuais)",
    )
    period = models.CharField(max_length=12, choices=GoalPeriod.choices, default=GoalPeriod.MONTHLY, verbose_name="Período")
    period_start = models.DateField(verbose_name="Início", help_text="Início do período")
    period_end = models.DateField(verbose_name="Fim", help_text="Fim do período")
    target_value = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Meta (R$)", help_text="Meta em R$")
    target_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name="Meta (Qtd)",
        help_text="Meta em quantidade de vendas"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period_start"]
        verbose_name = "Meta de Vendas"
        verbose_name_plural = "Metas de Vendas"

    def __str__(self):
        if self.seller:
            return f"Meta {self.seller.username} {self.period_start:%m/%Y} – R${self.target_value}"
        return f"Meta Coletiva {self.period_start:%m/%Y} – R${self.target_value}"


# ──────────────────────────────────────────────────────────────────────
# Communication History (for Quotes/Customers)
# ──────────────────────────────────────────────────────────────────────

class CommunicationHistory(models.Model):
    CHANNEL_CHOICES = [
        ("PHONE", "Telefone"),
        ("WHATSAPP", "WhatsApp"),
        ("EMAIL", "E-mail"),
        ("IN_PERSON", "Presencial"),
        ("OTHER", "Outro"),
    ]

    customer = models.ForeignKey(
        "core.Customer",
        on_delete=models.CASCADE,
        related_name="communications",
        verbose_name="Cliente",
    )
    quote = models.ForeignKey(
        "sales.Quote",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="communications",
        verbose_name="Orçamento",
    )
    channel = models.CharField(max_length=12, choices=CHANNEL_CHOICES, default="OTHER", verbose_name="Canal")
    summary = models.TextField(verbose_name="Resumo")
    next_steps = models.TextField(blank=True, verbose_name="Próximos Passos", help_text="Próximos passos sugeridos")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        verbose_name="Criado por",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Histórico de Comunicação"
        verbose_name_plural = "Histórico de Comunicações"

    def __str__(self):
        return f"{self.customer.name} - {self.get_channel_display()} ({self.created_at:%d/%m})"


# ──────────────────────────────────────────────────────────────────────
# Quote Templates
# ──────────────────────────────────────────────────────────────────────

class QuoteTemplate(models.Model):
    """Template reutilizável para orçamentos comuns (ex: Cozinha Completa)."""
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Modelo de Orçamento"
        verbose_name_plural = "Modelos de Orçamento"

    def __str__(self):
        return self.name


class QuoteTemplateItem(models.Model):
    template = models.ForeignKey(QuoteTemplate, on_delete=models.CASCADE, related_name="items")
    product_name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    quantity = models.PositiveIntegerField(default=1)
    default_unit_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]
        verbose_name = "Item do Modelo"
        verbose_name_plural = "Itens do Modelo"

    def __str__(self):
        return f"{self.product_name} (template: {self.template.name})"
