from django import forms
from django.forms import inlineformset_factory

from .models import Quote, QuoteItem, QuoteItemImage, Order, OrderItem
from core.models import PaymentMethodType


def parse_brl_decimal(raw, field_label="valor"):
    """Converte string em formato BR ('1.234,56') ou JS ('1234.56') para Decimal.

    Levanta forms.ValidationError se vazio ou inválido.
    """
    from decimal import Decimal, InvalidOperation
    if raw is None or str(raw).strip() == "":
        raise forms.ValidationError(f"Informe o {field_label}.")
    raw = str(raw).strip()
    if ',' in raw:
        raw = raw.replace('.', '').replace(',', '.')
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
        return val

    class Meta:
        model = Quote
        fields = [
            "customer",
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
            "notes",
        ]
        widgets = {
            "freight_value": forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric", "placeholder": "0,00"}),
            "delivery_days_min": forms.NumberInput(attrs={"class": "form-control", "min": "1", "placeholder": "Ex: 15"}),
            "delivery_days_max": forms.NumberInput(attrs={"class": "form-control", "min": "1", "placeholder": "Ex: 20"}),
            "payment_installments": forms.Select(attrs={"class": "form-control"}),
            "payment_fee_percent": forms.HiddenInput(),
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
        self.fields['notes'].required = False
        
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
    
    class Meta:
        model = QuoteItem
        fields = [
            "supplier",
            "product_name",
            "description",
            "quantity",
            "unit_value",
            "architect_percent",
        ]
        widgets = {
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
