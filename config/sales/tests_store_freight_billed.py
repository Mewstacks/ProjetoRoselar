"""Frete próprio (STORE) informado é cobrado do cliente.

A regra de venda líquida (frete sai das vendas da loja) não muda o que o cliente
paga: ele continua pagando o frete. Frete zero = entrega grátis. O abatimento
manual não pune a comissão pelo frete que o cliente já pagou.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Customer
from sales.margin import simulate_quote
from sales.models import Quote, QuoteStatus

User = get_user_model()


class StoreFreightBilledTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente")
        self.n = 0

    def quote(self, responsible="STORE", freight="225.00", deduction="0.00", item="2251.20"):
        self.n += 1
        quote = Quote.objects.create(
            number=f"STF-{self.n}", customer=self.customer, seller=self.seller,
            status=QuoteStatus.DRAFT, quote_date=date(2026, 11, 26),
            freight_responsible=responsible, freight_value=Decimal(freight),
            freight_commission_deduction=Decimal(deduction), delivery_days_min=1,
        )
        quote.items.create(product_name="Sofá", quantity=1, unit_value=Decimal(item))
        quote.refresh_from_db()
        return quote

    def test_store_freight_is_part_of_the_customer_total(self):
        quote = self.quote()
        self.assertEqual(quote.billable_freight, Decimal("225.00"))
        self.assertEqual(quote.total_value_snapshot, Decimal("2476.20"))

    def test_store_freight_zero_is_free_delivery(self):
        quote = self.quote(freight="0.00")
        self.assertEqual(quote.total_value_snapshot, Decimal("2251.20"))

    def test_net_sales_removes_only_the_freight_the_customer_paid(self):
        quote = self.quote()
        self.assertEqual(quote.net_sales_value, Decimal("2251.20"))

    def test_deduction_equal_to_billed_freight_does_not_zero_the_commission(self):
        quote = self.quote(deduction="225.00")
        ctx = simulate_quote(quote)
        self.assertEqual(ctx["final_total"], Decimal("2476.20"))
        self.assertEqual(ctx["seller_commission_gross_value"], Decimal("67.54"))
        self.assertEqual(ctx["seller_commission_value"], Decimal("67.54"))

    def test_only_the_excess_over_the_billed_freight_hits_the_commission(self):
        quote = self.quote(deduction="250.00")
        ctx = simulate_quote(quote)
        self.assertEqual(ctx["seller_commission_value"], Decimal("42.54"))

    def test_deduction_without_billed_freight_still_reduces_commission(self):
        quote = self.quote(freight="0.00", deduction="50.00")
        ctx = simulate_quote(quote)
        self.assertEqual(ctx["seller_commission_value"], Decimal("17.54"))
