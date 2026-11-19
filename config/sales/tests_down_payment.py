"""Regression for the amount charged on an installment sale with an entry."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Customer, PaymentTariff
from sales.margin import _build_simulation_context, simulate_quote
from sales.models import Quote, QuoteStatus


class DownPaymentSimulationTests(TestCase):
    def setUp(self):
        PaymentTariff.objects.all().delete()
        PaymentTariff.objects.create(
            payment_type="CREDIT_CARD",
            installments=6,
            fee_percent=Decimal("10.00"),
        )

    def _simulate(self, down_payment):
        return _build_simulation_context(
            subtotal=Decimal("10000.00"),
            freight_value=Decimal("0"),
            sim_payment_type="CREDIT_CARD",
            sim_has_architect=False,
            sim_discount=Decimal("0"),
            price_increase_pct=Decimal("0"),
            sim_installments=6,
            down_payment_value=down_payment,
        )

    def test_entry_reduces_fee_base_and_installments(self):
        without_entry = self._simulate(None)
        with_entry = self._simulate(Decimal("5000.00"))

        self.assertEqual(without_entry["payment_fee_value"], Decimal("1000.00"))
        self.assertEqual(with_entry["final_total"], Decimal("10000.00"))
        self.assertEqual(with_entry["down_payment_value"], Decimal("5000.00"))
        self.assertEqual(with_entry["financed_value"], Decimal("5000.00"))
        self.assertEqual(with_entry["payment_fee_value"], Decimal("500.00"))
        self.assertEqual(with_entry["installment_value_1"] * 6, Decimal("5000.00"))

    def test_saved_entry_is_used_by_quote_and_payment_plan(self):
        user = get_user_model().objects.create_user(
            username="seller-entry", password="x", role="SELLER"
        )
        customer = Customer.objects.create(name="Cliente Entrada")
        quote = Quote.objects.create(
            number="ORC-ENTRADA", customer=customer, seller=user,
            status=QuoteStatus.DRAFT, freight_responsible="CUSTOMER",
        )
        quote.items.create(
            product_name="Sofá", quantity=1, unit_value=Decimal("10000.00")
        )
        self.client.force_login(user)
        url = reverse("sales:quote_simulate", args=[quote.pk])

        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="h-down-payment" form="save-form"')

        saved = self.client.post(url, {
            "action": "save_conditions",
            "sim_payment_type": "CREDIT_CARD",
            "sim_installments": "6",
            "down_payment_value": "5000.00",
        })
        self.assertEqual(saved.status_code, 302)

        quote.refresh_from_db()
        self.assertEqual(quote.down_payment_value, Decimal("5000.00"))
        self.assertEqual(simulate_quote(quote)["payment_fee_value"], Decimal("500.00"))
        plan = quote.get_payment_plans()[0]
        self.assertEqual(plan["down_payment"], Decimal("5000.00"))
        self.assertEqual(plan["primary"]["amount"], Decimal("5000.00"))
        self.assertEqual(plan["primary"]["installment_value"] * 6, Decimal("5000.00"))

        detail = self.client.get(reverse("sales:quote_detail", args=[quote.pk]))
        self.assertContains(detail, "Entrada à vista: R$ 5000,00")
        self.assertContains(detail, "6x de R$ 833,33")

        missing_tariff = self.client.post(url, {
            "action": "save_conditions",
            "sim_payment_type": "CREDIT_CARD",
            "sim_installments": "13",
            "down_payment_value": "5000.00",
        })
        self.assertEqual(missing_tariff.status_code, 200)
        self.assertContains(
            missing_tariff,
            "Informe a taxa do cartão para a parcela escolhida antes de salvar.",
        )
        self.assertContains(missing_tariff, 'role="alert"')
        quote.refresh_from_db()
        self.assertEqual(quote.payment_installments, 6)
        self.assertEqual(quote.down_payment_value, Decimal("5000.00"))
