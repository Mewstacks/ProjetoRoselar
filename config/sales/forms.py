from decimal import Decimal

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


def parse_brl_percent(raw, field_label="porcentagem"):
    """Converte "12,5" ou "12.5" para Decimal. Vazio devolve None.

    Porcentagem não tem separador de milhar, então a vírgula é sempre decimal.
    """
    from decimal import Decimal, InvalidOperation
    if raw is None or str(raw).strip() == "":
        return None
    raw = str(raw).strip().replace("%", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise forms.ValidationError(f"{field_label.capitalize()} inválida.")


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
        help_text="Use para o preço a prazo ou para um preço único sem condição alternativa.",
        widget=forms.TextInput(attrs={
            "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
            "data-financial": "money",
        }),
        label="Preço Final ao Cliente (R$)",
    )

    # O frete é digitado em reais como qualquer outro valor da tela. Como
    # DecimalField do modelo ele recusava "1.250,50" e o envio voltava com erro
    # em um campo que o vendedor tinha preenchido certo.
    freight_unit = forms.ChoiceField(
        choices=(("BRL", "R$"), ("PERCENT", "%")), required=False,
        widget=forms.Select(attrs={"class": "form-select"}), label="Unidade do frete",
    )
    freight_percent = forms.DecimalField(
        required=False, min_value=Decimal("0"), max_value=Decimal("99999.99"),
        max_digits=7, decimal_places=2, localize=True, label="Percentual de frete (%)",
        widget=forms.TextInput(attrs={"class": "form-control", "inputmode": "decimal", "autocomplete": "off"}),
    )

    freight_value = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
            "data-financial": "money",
        }),
        label="Valor do Frete (R$)",
    )

    freight_commission_deduction = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
            "data-financial": "money", "autocomplete": "off",
            "aria-describedby": "freight-deduction-help",
        }),
        label="Frete a abater da venda e comissão (R$)",
    )

    def clean_freight_value(self):
        if self.data.get("freight_unit") == "PERCENT":
            return Decimal("0.00")
        raw = self.cleaned_data.get('freight_value', '')
        if raw is None or str(raw).strip() == '':
            return Decimal("0.00")
        val = parse_brl_decimal(raw, 'valor do frete')
        if val < 0:
            raise forms.ValidationError('O valor do frete não pode ser negativo.')
        return val

    def clean_freight_commission_deduction(self):
        raw = self.cleaned_data.get('freight_commission_deduction', '')
        if raw is None or str(raw).strip() == '':
            return Decimal("0.00")
        val = parse_brl_decimal(raw, 'frete a abater')
        if val < 0:
            raise forms.ValidationError('O frete a abater não pode ser negativo.')
        return val

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

    # Preço final à vista. Mesmo parser e mesma leitura de zero do campo acima;
    # vazio = não há um preço alternativo à vista.
    total_cash_override = forms.CharField(
        required=False,
        help_text="Use este campo para o valor final à vista. Deixe o outro preço vazio se esta for a única condição.",
        widget=forms.TextInput(attrs={
            "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
            "data-financial": "money",
        }),
        label="Preço Final à Vista (R$)",
    )

    def clean_total_cash_override(self):
        raw = self.cleaned_data.get('total_cash_override', '')
        if raw is None or str(raw).strip() == '':
            return None
        val = parse_brl_decimal(raw, 'preço final à vista')
        if val < 0:
            raise forms.ValidationError('O preço final à vista não pode ser negativo.')
        if val == 0:
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
            "freight_percent",
            "freight_embedded",
            "freight_commission_deduction",
            "freight_responsible",
            "shipping_company",
            "discount_percent",
            "has_architect",
            "architect",
            "payment_type",
            "payment_installments",
            "payment_fee_percent",
            "total_override",
            "total_cash_override",
            "dual_pricing",
            "selected_price_tier",
            "notes",
        ]
        widgets = {
            "quote_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "sale_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "freight_value": forms.TextInput(attrs={
                "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
                "data-financial": "money",
            }),
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
        self.fields['total_cash_override'].required = False
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
        self.fields["freight_embedded"].widget.attrs.update({
            "class": "form-check-input", "aria-describedby": "freight-embedded-help",
        })
        self.initial["freight_unit"] = (
            "PERCENT" if self.instance.freight_percent is not None else "BRL"
        )
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

        percent_mode = cleaned.get("freight_unit") == "PERCENT" and responsible != "CUSTOMER"
        if not percent_mode:
            cleaned["freight_percent"] = None
        if responsible != "CARRIER":
            cleaned["freight_embedded"] = False

        if responsible in ('STORE', 'CARRIER'):
            field = "freight_percent" if percent_mode else "freight_value"
            if not cleaned.get(field) and field not in self.errors:
                self.add_error(field, "Informe o valor do frete.")
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

        # À vista maior que a prazo é sempre digitação trocada — barrar aqui
        # evita a proposta sair com o desconto invertido.
        cash = cleaned.get('total_cash_override')
        if cash is not None:
            installment = cleaned.get('total_override')
            if installment is not None and cash > installment:
                self.add_error(
                    'total_cash_override',
                    'O preço à vista não pode ser maior que o preço a prazo.',
                )

        # Datas em branco não apagam o valor existente (evita venda "sumir"
        # dos painéis por submit sem a data preenchida).
        if not cleaned.get('quote_date'):
            cleaned['quote_date'] = self.instance.quote_date or timezone.localdate()
        if not cleaned.get('sale_date') and self.instance.pk and self.instance.status in SOLD_STATUSES:
            cleaned['sale_date'] = self.instance.sale_date

        return cleaned

    def validate_freight_items(self, formset):
        quote = self.instance
        items = [
            form.instance for form in formset.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]

        # A mesma trava do simulador também vale no formulário normal. Sem
        # ela, um abatimento maior que a venda poderia zerar silenciosamente
        # os relatórios. Usa os itens ainda não salvos do formset para que a
        # validação funcione tanto na criação quanto na edição.
        tier = quote.selected_price_tier
        wholesale = quote.dual_pricing and tier == PriceTier.WHOLESALE
        subtotal = sum(
            item.line_total_wholesale if wholesale else item.line_total
            for item in items
        )
        billable_freight = Decimal("0.00")
        if quote.freight_responsible != "STORE":
            billable_freight = sum(
                quote.freight_allocations(tier, items), Decimal("0.00")
            )
        if wholesale:
            base = subtotal * (
                Decimal("1") + (quote.price_increase_percent or Decimal("0")) / Decimal("100")
            ) + billable_freight
            totals = [quote.apply_client_rounding(base, use_override=False)]
        else:
            base = subtotal * (
                Decimal("1")
                + (quote.price_increase_percent or Decimal("0")) / Decimal("100")
                - (quote.discount_percent or Decimal("0")) / Decimal("100")
            ) + billable_freight
            totals = [quote.apply_client_rounding(base)]
            if quote.effective_cash_override is not None:
                totals.append(quote.effective_cash_override)

        deduction = quote.freight_commission_deduction or Decimal("0.00")
        positive_totals = [total for total in totals if total is not None and total > 0]
        if positive_totals and deduction > min(positive_totals):
            self.add_error(
                "freight_commission_deduction",
                "O frete a abater não pode ser maior que o total cobrado do cliente.",
            )
            return False

        if not quote.embeds_freight:
            return True
        field = "freight_percent" if quote.freight_percent is not None else "freight_value"
        if not any(item.line_total > 0 for item in items):
            self.add_error(field, "Informe pelo menos um produto com valor positivo para ratear o frete.")
            return False
        freight = sum(quote.freight_allocations(PriceTier.RETAIL, items))
        prices = (quote.effective_total_override, quote.effective_cash_override)
        if any(price is not None and freight > price for price in prices):
            self.add_error(field, "O frete embutido não pode superar o preço final informado.")
            return False
        return True


class QuoteItemForm(forms.ModelForm):
    # Make architect_percent not required and hidden by default
    # CharField + parser proprio: o vendedor digita "12,5" e o DecimalField do
    # Django recusaria a virgula antes de qualquer clean rodar.
    architect_percent = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'inputmode': 'decimal',
            'data-financial': 'percent',
            'placeholder': '0,00',
        }),
        label="% Arquiteto"
    )

    def clean_architect_percent(self):
        val = parse_brl_percent(self.cleaned_data.get('architect_percent', ''), '% do arquiteto')
        if val is None:
            return None
        if val < 0:
            raise forms.ValidationError('A porcentagem do arquiteto não pode ser negativa.')
        return val
    
    # Override unit_value as CharField to accept Brazilian currency format
    unit_value = forms.CharField(
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'inputmode': 'numeric', 'data-financial': 'money',
        }),
        label="Valor Unitário"
    )

    # Preço de atacado (opcional, só usado quando o orçamento é atacado+varejo)
    unit_value_wholesale = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'inputmode': 'numeric', 'placeholder': '0,00',
            'data-financial': 'money',
        }),
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

    # O frete mora no orçamento. Estes dois campos editam o orçamento do pedido:
    # em branco = não mexe.
    freight_value = forms.CharField(
        required=False, label="Valor do frete (R$)",
        widget=forms.TextInput(attrs={
            "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
            "data-financial": "money", "autocomplete": "off",
        }),
    )
    freight_commission_deduction = forms.CharField(
        required=False, label="Frete a abater (R$)",
        widget=forms.TextInput(attrs={
            "class": "form-control", "inputmode": "numeric", "placeholder": "0,00",
            "data-financial": "money", "autocomplete": "off",
        }),
    )

    @staticmethod
    def _money_text(value):
        return f"{(value or Decimal('0.00')):.2f}".replace(".", ",")

    def _freight_quote(self):
        quote = self.instance.quote if self.instance and self.instance.quote_id else None
        if quote is None or quote.freight_responsible == "CUSTOMER":
            return None
        return quote

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        quote = self._freight_quote()
        if quote is None:
            del self.fields["freight_value"]
            del self.fields["freight_commission_deduction"]
        else:
            if quote.freight_percent is not None:
                # Frete em %: o valor sai do percentual, que se edita no orçamento.
                del self.fields["freight_value"]
            else:
                self.initial["freight_value"] = self._money_text(quote.freight_value)
            self.initial["freight_commission_deduction"] = self._money_text(
                quote.freight_commission_deduction
            )
        self.fields["delivery_deadline"].input_formats = ["%Y-%m-%d"]
        self.fields["created_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        for name in ("supplier", "purchase_condition_text", "transport_info", "delivery_deadline", "notes"):
            self.fields[name].required = False
        # Pedido total não tem fornecedor: trava o campo.
        if self.instance and self.instance.is_total_conference:
            self.fields["supplier"].disabled = True
            self.fields["supplier"].required = False

    def _clean_optional_money(self, name, label):
        raw = self.cleaned_data.get(name, "")
        if raw is None or str(raw).strip() == "":
            return None
        value = parse_brl_decimal(raw, label)
        if value < 0:
            raise forms.ValidationError(f"O {label} não pode ser negativo.")
        return value

    def clean_freight_value(self):
        return self._clean_optional_money("freight_value", "valor do frete")

    def clean_freight_commission_deduction(self):
        return self._clean_optional_money("freight_commission_deduction", "frete a abater")

    def clean(self):
        cleaned = super().clean()
        # Replica Order.clean(): normal exige fornecedor, total não pode ter.
        if self.instance and self.instance.is_total_conference:
            cleaned["supplier"] = None
        else:
            if not cleaned.get("supplier"):
                self.add_error("supplier", "Pedido por fornecedor precisa de fornecedor.")

        quote = self._freight_quote()
        deduction = cleaned.get("freight_commission_deduction")
        if quote is not None and "freight_commission_deduction" not in self.errors:
            # O abatimento nunca pode passar do que o cliente paga. O total é
            # calculado com o frete novo, sem gravar nada antes de validar.
            import copy
            probe = copy.copy(quote)
            new_freight = cleaned.get("freight_value")
            if new_freight is not None:
                probe.freight_value = new_freight
            if deduction is None:
                deduction = quote.freight_commission_deduction or Decimal("0.00")
            total = probe.calculate_rounded_total()
            if total and total > 0 and deduction > total:
                self.add_error(
                    "freight_commission_deduction",
                    "O frete a abater não pode ser maior que o total cobrado do cliente.",
                )
        return cleaned

    def save_freight(self):
        """Grava o frete no orçamento. Devolve True se algo mudou."""
        quote = self._freight_quote()
        if quote is None:
            return False
        changed = []
        new_freight = self.cleaned_data.get("freight_value")
        if new_freight is not None and new_freight != quote.freight_value:
            quote.freight_value = new_freight
            changed.append("freight_value")
        new_deduction = self.cleaned_data.get("freight_commission_deduction")
        if new_deduction is not None and new_deduction != quote.freight_commission_deduction:
            quote.freight_commission_deduction = new_deduction
            changed.append("freight_commission_deduction")
        if not changed:
            return False
        quote.save()
        return True


class OrderItemForm(forms.ModelForm):
    purchase_unit_cost = forms.CharField(
        required=True,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "inputmode": "numeric",
            "placeholder": "0,00",
            "data-financial": "money",
        }),
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
