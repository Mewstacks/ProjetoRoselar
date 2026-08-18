from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import (
    Order,
    OrderItem,
    PriceTier,
    Quote,
    QuoteItem,
    QuoteItemImage,
    SOLD_STATUSES,
)
from core.models import PaymentMethodType


def parse_brl_decimal(raw, field_label="valor"):
    """Converte string em formato BR ('1.234,56') ou JS ('1234.56') para Decimal.

    Levanta forms.ValidationError se vazio ou inválido.
    """
    from decimal import Decimal, InvalidOperation
    import re
    if raw is None or str(raw).strip() == "":
        raise forms.ValidationError(f"Informe o {field_label}.")
    raw = str(raw).strip()
    if ',' in raw:
        raw = raw.replace('.', '').replace(',', '.')
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        # "2.300" sem centavos é milhar em pt-BR, nunca 2,30. Sem esta guarda o
        # Decimal lê o ponto como separador decimal e grava R$ 2,30.
        raw = raw.replace('.', '')
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise forms.ValidationError(f"{field_label.capitalize()} inválido.")


class QuoteForm(forms.ModelForm):
    # Add payment_type as a choice field
    payment_type = forms.ChoiceField(
        choices=[('', '--- Selecione ---')] + list(PaymentMethodType.choices),
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
        label="Método de Pagamento"
    )

    # Preço final ao cliente (override). Aceita formato BR ("1.234,56");
    # vazio = None (usa o total calculado).
    total_override = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric", "placeholder": "0,00"}),
        label="Preço Final ao Cliente (R$)",
    )

    def clean_total_override(self):
        raw = self.cleaned_data.get('total_override', '')
        if raw is None or str(raw).strip() == '':
            return None
        val = parse_brl_decimal(raw, 'preço final ao cliente')
        if val < 0:
            raise forms.ValidationError('O preço final ao cliente não pode ser negativo.')
        if val == 0:
            # "0,00" é campo em branco com um zero sobrando, não uma venda de
            # graça. Gravar zero zerava o total de varejo do orçamento inteiro.
            return None
        return val

    class Meta:
        model = Quote
        fields = [
            "customer",
            "quote_date",
            "sale_date",
            "delivery_days_min",
            "delivery_days_max",
            "freight_value",
            "freight_responsible",
            "shipping_company",
            "discount_percent",
            "has_architect",
            "architect",
            "payment_type",
            "payment_installments",
            "payment_fee_percent",
            "total_override",
            "dual_pricing",
            "selected_price_tier",
            "notes",
        ]
        widgets = {
            "quote_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "sale_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "freight_value": forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric", "placeholder": "0,00"}),
            "delivery_days_min": forms.NumberInput(attrs={"class": "form-control", "min": "1", "placeholder": "Ex: 15"}),
            "delivery_days_max": forms.NumberInput(attrs={"class": "form-control", "min": "1", "placeholder": "Ex: 20"}),
            "payment_installments": forms.Select(attrs={"class": "form-control"}),
            "payment_fee_percent": forms.HiddenInput(),
            "dual_pricing": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "selected_price_tier": forms.Select(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Observações gerais do orçamento..."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Pricing fields are optional in Step 1 (set in Step 2 – pricing page)
        self.fields['discount_percent'].required = False
        self.fields['has_architect'].required = False
        self.fields['architect'].required = False
        self.fields['payment_installments'].required = False
        self.fields['payment_fee_percent'].required = False
        self.fields['total_override'].required = False
        self.fields['dual_pricing'].required = False
        self.fields['selected_price_tier'].required = False
        self.fields['notes'].required = False

        # Datas editáveis: quote_date sempre; sale_date só existe após a venda.
        self.fields['quote_date'].required = False
        self.fields['quote_date'].input_formats = ["%Y-%m-%d"]
        self.fields['sale_date'].required = False
        self.fields['sale_date'].input_formats = ["%Y-%m-%d"]
        
        # Freight fields are conditionally required (validated in clean)
        self.fields['freight_value'].required = False
        self.fields['delivery_days_min'].required = False
        self.fields['delivery_days_max'].required = False
        self.fields['shipping_company'].required = False
        
        # Adicionar classes CSS
        for field_name, field in self.fields.items():
            if field_name not in ['payment_fee_percent']:
                if 'class' not in field.widget.attrs:
                    field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned = super().clean()
        responsible = cleaned.get('freight_responsible')
        from decimal import Decimal

        if responsible in ('STORE', 'CARRIER'):
            fv = cleaned.get('freight_value')
            if fv is None or fv <= Decimal('0'):
                self.add_error('freight_value', 'Informe o valor do frete.')
            if not cleaned.get('delivery_days_min'):
                self.add_error('delivery_days_min', 'Informe o prazo mínimo de entrega.')
            if responsible == 'CARRIER' and not cleaned.get('shipping_company'):
                self.add_error('shipping_company', 'Selecione a transportadora.')

        if cleaned.get('has_architect') and not cleaned.get('architect'):
            self.add_error('architect', 'Selecione o arquiteto.')

        if (
            not cleaned.get("dual_pricing")
            or cleaned.get("selected_price_tier") not in PriceTier.values
        ):
            cleaned["selected_price_tier"] = PriceTier.RETAIL

        # Datas em branco não apagam o valor existente (evita venda "sumir"
        # dos painéis por submit sem a data preenchida).
        if not cleaned.get('quote_date'):
            cleaned['quote_date'] = self.instance.quote_date or timezone.localdate()
        if not cleaned.get('sale_date') and self.instance.pk and self.instance.status in SOLD_STATUSES:
            cleaned['sale_date'] = self.instance.sale_date

        return cleaned


class QuoteItemForm(forms.ModelForm):
    # Make architect_percent not required and hidden by default
    architect_percent = forms.DecimalField(
        required=False,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1'}),
        label="% Arquiteto"
    )
    
    # Override unit_value as CharField to accept Brazilian currency format
    unit_value = forms.CharField(
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'inputmode': 'numeric'}),
        label="Valor Unitário"
    )

    # Preço de atacado (opcional, só usado quando o orçamento é atacado+varejo)
    unit_value_wholesale = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'inputmode': 'numeric', 'placeholder': '0,00'}),
        label="Valor Atacado"
    )
    
    # Image upload for the item (shown in buyer's PDF)
    item_image = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
        label="Imagem do Produto"
    )

    def clean_unit_value(self):
        raw = self.cleaned_data.get('unit_value', '')
        if not raw:
            raise forms.ValidationError('Informe o valor unitário.')
        # Accept both "1234.56" (JS-converted) and "1.234,56" (Brazilian format)
        raw = str(raw).strip()
        if ',' in raw:
            raw = raw.replace('.', '').replace(',', '.')
        from decimal import Decimal, InvalidOperation
        try:
            val = Decimal(raw)
        except InvalidOperation:
            raise forms.ValidationError('Valor unitário inválido.')
        if val <= 0:
            raise forms.ValidationError('O valor unitário deve ser maior que zero.')
        return val

    def clean_unit_value_wholesale(self):
        raw = self.cleaned_data.get('unit_value_wholesale', '')
        if raw is None or str(raw).strip() == '':
            return None  # atacado opcional: em branco cai no varejo (line_total_wholesale)
        raw = str(raw).strip()
        if ',' in raw:
            raw = raw.replace('.', '').replace(',', '.')
        from decimal import Decimal, InvalidOperation
        try:
            val = Decimal(raw)
        except InvalidOperation:
            raise forms.ValidationError('Valor de atacado inválido.')
        if val <= 0:
            raise forms.ValidationError('O valor de atacado deve ser maior que zero.')
        return val

    def has_changed(self):
        """Linha nova (sem pk) sem produto e sem valor = vazia: não valida.

        Mesma guarda do OrderItemForm: `quantity` herda o default 1 como
        `initial`, então uma linha extra/nova cujo `quantity` chega vazio pareceria
        "alterada" e quebraria o save do orçamento com "campos obrigatórios" numa
        linha em branco.
        """
        if not self.instance.pk:
            name = (self.data.get(self.add_prefix("product_name")) or "").strip()
            value = (self.data.get(self.add_prefix("unit_value")) or "").strip()
            wholesale = (self.data.get(self.add_prefix("unit_value_wholesale")) or "").strip()
            # Qualquer valor digitado (varejo OU atacado OU nome) torna a linha
            # não-vazia: ela então valida e o usuário é avisado do que falta, em
            # vez de a linha ser descartada em silêncio.
            if not name and not value and not wholesale:
                return False
        return super().has_changed()

    class Meta:
        model = QuoteItem
        fields = [
            "supplier",
            "environment",
            "product_name",
            "description",
            "quantity",
            "unit_value",
            "unit_value_wholesale",
            "architect_percent",
        ]
        widgets = {
            "environment": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Ex.: Dormitório, Cozinha, Sala...",
            }),
            "description": forms.Textarea(attrs={"rows": 1}),
        }


QuoteItemFormSet = inlineformset_factory(
    Quote,
    QuoteItem,
    form=QuoteItemForm,
    extra=1,
    can_delete=True,
)


# ── Edição do Pedido de Compra (Order) ────────────────────────────────────────
class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            "supplier",
            "status",
            "created_at",
            "purchase_condition_text",
            "transport_info",
            "delivery_deadline",
            "notes",
        ]
        widgets = {
            "supplier": forms.Select(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-control"}),
            "created_at": forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "purchase_condition_text": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex: 30/60/90 dias"}),
            "transport_info": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex: Transportadora X, retira na fábrica..."}),
            "delivery_deadline": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Observações do pedido..."}),
        }
        labels = {
            "created_at": "Data do Pedido",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["delivery_deadline"].input_formats = ["%Y-%m-%d"]
        self.fields["created_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        for name in ("supplier", "purchase_condition_text", "transport_info", "delivery_deadline", "notes"):
            self.fields[name].required = False
        # Pedido total não tem fornecedor: trava o campo.
        if self.instance and self.instance.is_total_conference:
            self.fields["supplier"].disabled = True
            self.fields["supplier"].required = False

    def clean(self):
        cleaned = super().clean()
        # Replica Order.clean(): normal exige fornecedor, total não pode ter.
        if self.instance and self.instance.is_total_conference:
            cleaned["supplier"] = None
        else:
            if not cleaned.get("supplier"):
                self.add_error("supplier", "Pedido por fornecedor precisa de fornecedor.")
        return cleaned


class OrderItemForm(forms.ModelForm):
    purchase_unit_cost = forms.CharField(
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric", "placeholder": "0,00"}),
        label="Custo de Compra (R$)",
    )

    def clean_purchase_unit_cost(self):
        val = parse_brl_decimal(self.cleaned_data.get("purchase_unit_cost", ""), "custo de compra")
        if val < 0:
            raise forms.ValidationError("O custo de compra não pode ser negativo.")
        return val

    def has_changed(self):
        """Linha nova (sem pk) sem produto e sem custo = vazia: não valida.

        O campo `quantity` herda o default 1 do modelo como `initial`. Sem esta
        guarda, uma linha extra/nova não preenchida — cujo `quantity` chega vazio
        (linha adicionada e limpa via JS, ou órfã de um add-then-delete que deixa
        o índice no TOTAL_FORMS) — seria considerada "alterada" e passaria pela
        validação `required`, quebrando a edição inteira do pedido com
        "campos obrigatórios" em uma linha que o usuário nem preencheu.
        """
        if not self.instance.pk:
            name = (self.data.get(self.add_prefix("product_name")) or "").strip()
            cost = (self.data.get(self.add_prefix("purchase_unit_cost")) or "").strip()
            desc = (self.data.get(self.add_prefix("description")) or "").strip()
            # `quantity` é ignorado de propósito: ele herda o default 1 e chega
            # sempre preenchido, então não distingue linha vazia de linha real.
            # Qualquer nome/custo/descrição digitado torna a linha validável.
            if not name and not cost and not desc:
                return False
        return super().has_changed()

    class Meta:
        model = OrderItem
        fields = ["product_name", "description", "quantity", "purchase_unit_cost"]
        widgets = {
            "product_name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 1}),
            "quantity": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
        }


OrderItemFormSet = inlineformset_factory(
    Order,
    OrderItem,
    form=OrderItemForm,
    extra=1,
    can_delete=True,
)
