"""Venda líquida: todo frete informado sai do total de vendas.

Frete cobrado do cliente (transportadora) está dentro do total e sai. Frete da
loja não entra no total, mas também é abatido da venda líquida. O abatimento
manual ("Frete a abater") não soma por cima do frete informado: vale o maior dos
dois, para o mesmo frete não ser descontado duas vezes. A comissão não muda.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Customer
from sales.models import Quote, QuoteStatus

User = get_user_model()


class NetSalesFreightTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN")
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente")
        self.n = 0

    def quote(self, responsible, freight="0.00", deduction="0.00", percent=None, item="1000.00"):
        self.n += 1
        quote = Quote.objects.create(
            number=f"NET-{self.n}", customer=self.customer, seller=self.seller,
            status=QuoteStatus.CONVERTED, quote_date=date(2026, 7, 5),
            sale_date=date(2026, 7, 5), freight_responsible=responsible,
            freight_value=Decimal(freight), freight_percent=percent,
            freight_commission_deduction=Decimal(deduction),
            delivery_days_min=1,
        )
        quote.items.create(product_name="Sofá", quantity=1, unit_value=Decimal(item))
        quote.refresh_from_db()
        return quote

    def test_carrier_freight_billed_to_the_customer_leaves_the_net(self):
        quote = self.quote("CARRIER", freight="100.00")
        self.assertEqual(quote.total_value_snapshot, Decimal("1100.00"))
        self.assertEqual(quote.net_sales_value, Decimal("1000.00"))

    def test_store_freight_is_also_deducted(self):
        quote = self.quote("STORE", freight="180.00")
        self.assertEqual(quote.total_value_snapshot, Decimal("1000.00"))
        self.assertEqual(quote.net_sales_value, Decimal("820.00"))

    def test_customer_paid_sale_without_freight_is_untouched(self):
        quote = self.quote("CUSTOMER")
        self.assertEqual(quote.net_sales_value, Decimal("1000.00"))

    def test_manual_deduction_alone_still_works(self):
        quote = self.quote("CUSTOMER", deduction="50.00")
        self.assertEqual(quote.net_sales_value, Decimal("950.00"))

    def test_manual_deduction_does_not_double_count_the_same_freight(self):
        quote = self.quote("CARRIER", freight="100.00", deduction="100.00")
        self.assertEqual(quote.net_sales_value, Decimal("1000.00"))

    def test_manual_deduction_above_the_freight_wins(self):
        quote = self.quote("CARRIER", freight="100.00", deduction="130.00")
        self.assertEqual(quote.net_sales_value, Decimal("970.00"))

    def test_percent_freight_is_deducted(self):
        quote = self.quote("CARRIER", percent=Decimal("10.00"))
        self.assertEqual(quote.total_value_snapshot, Decimal("1100.00"))
        self.assertEqual(quote.net_sales_value, Decimal("1000.00"))

    def test_net_never_goes_below_zero(self):
        quote = self.quote("STORE", freight="5000.00")
        self.assertEqual(quote.net_sales_value, Decimal("0.00"))

    def test_sales_freight_deduction_reports_what_left_the_sale(self):
        self.assertEqual(self.quote("CARRIER", freight="100.00").sales_freight_deduction, Decimal("100.00"))
        self.assertEqual(self.quote("STORE", freight="180.00").sales_freight_deduction, Decimal("180.00"))
        self.assertEqual(self.quote("CUSTOMER", deduction="50.00").sales_freight_deduction, Decimal("50.00"))
        self.assertEqual(self.quote("CUSTOMER").sales_freight_deduction, Decimal("0.00"))

    def test_commission_is_not_changed_by_the_automatic_freight(self):
        from sales.margin import persist_quote_commission

        with_freight = self.quote("STORE", freight="180.00")
        without = self.quote("STORE", freight="0.00")
        persist_quote_commission(with_freight)
        persist_quote_commission(without)
        with_freight.refresh_from_db()
        without.refresh_from_db()
        self.assertEqual(with_freight.commission_value, without.commission_value)

    def test_dashboard_and_reports_use_the_net(self):
        self.quote("CARRIER", freight="100.00")
        self.quote("STORE", freight="180.00")
        self.client.login(username="admin", password="x")
        period = {"date_from": "2026-07-01", "date_to": "2026-07-31"}

        resp = self.client.get(reverse("core:report_sales"), period)
        self.assertEqual(resp.status_code, 200)
        # 1000 + 820
        self.assertEqual(resp.context["total"], Decimal("1820.00"))

        resp = self.client.get(reverse("core:report_freights"), period)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["count"], 2)
        self.assertEqual(resp.context["total_freight_deduction"], Decimal("280.00"))
