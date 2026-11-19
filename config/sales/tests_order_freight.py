"""Frete editável na tela Editar Pedido (pedido de compra).

O frete mora no orçamento; o pedido só o mostrava. Quem corrige o frete depois
da venda abre o pedido, então a edição precisa estar ali e propagar para o
orçamento (total, comissão e relatórios).
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from sales.models import Order, OrderStatus, Quote, QuoteStatus

User = get_user_model()


class OrderEditFreightTests(TestCase):
    def setUp(self):
        from core.models import Customer

        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN")
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente")
        self.quote = self._quote("ORC-8000", "CARRIER", Decimal("100.00"))
        self.order = Order.objects.create(
            number="ORC-8000", quote=self.quote, is_total_conference=True,
            status=OrderStatus.PENDING,
        )
        self.item = self.order.items.create(
            product_name="Cadeira", quantity=1, purchase_unit_cost=Decimal("50.00"),
        )

    def _quote(self, number, responsible, freight):
        quote = Quote.objects.create(
            number=number, customer=self.customer, seller=self.seller,
            status=QuoteStatus.CONVERTED, sale_date=date(2026, 7, 5),
            freight_responsible=responsible, freight_value=freight,
            shipping_company=None, delivery_days_min=1,
        )
        quote.items.create(product_name="Sofá", quantity=1, unit_value=Decimal("1000.00"))
        return quote

    def _post(self, **extra):
        data = {
            "supplier": "", "status": OrderStatus.PENDING,
            "created_at": "2026-07-05T10:00", "purchase_condition_text": "",
            "transport_info": "", "delivery_deadline": "", "notes": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(self.item.id), "items-0-product_name": "Cadeira",
            "items-0-description": "", "items-0-quantity": "1",
            "items-0-purchase_unit_cost": "50,00",
        }
        data.update(extra)
        self.client.login(username="admin", password="x")
        return self.client.post(reverse("sales:order_edit", args=[self.order.id]), data)

    def test_form_shows_the_current_freight(self):
        self.client.login(username="admin", password="x")
        resp = self.client.get(reverse("sales:order_edit", args=[self.order.id]))
        self.assertContains(resp, 'name="freight_value"')
        self.assertContains(resp, 'value="100,00"')

    def test_editing_freight_updates_the_quote_and_its_total(self):
        resp = self._post(freight_value="250,00", freight_commission_deduction="0,00")
        self.assertEqual(resp.status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.freight_value, Decimal("250.00"))
        self.assertEqual(self.quote.total_value_snapshot, Decimal("1250.00"))

    def test_editing_the_deduction_persists_and_cuts_the_commission(self):
        from sales.margin import persist_quote_commission

        persist_quote_commission(self.quote)
        self.quote.refresh_from_db()
        before = self.quote.commission_value
        self.assertIsNotNone(before)
        resp = self._post(freight_value="100,00", freight_commission_deduction="30,00")
        self.assertEqual(resp.status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.freight_commission_deduction, Decimal("30.00"))
        self.assertLess(self.quote.commission_value, before)

    def test_deduction_above_the_customer_total_is_refused(self):
        resp = self._post(freight_value="100,00", freight_commission_deduction="5000,00")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "maior que o total")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.freight_commission_deduction, Decimal("0.00"))

    def test_negative_freight_is_refused(self):
        resp = self._post(freight_value="-5,00", freight_commission_deduction="0,00")
        self.assertEqual(resp.status_code, 200)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.freight_value, Decimal("100.00"))

    def test_customer_paid_freight_only_offers_the_deduction(self):
        quote = self._quote("ORC-8001", "CUSTOMER", Decimal("0.00"))
        order = Order.objects.create(
            number="ORC-8001", quote=quote, is_total_conference=True,
            status=OrderStatus.PENDING,
        )
        self.client.login(username="admin", password="x")
        resp = self.client.get(reverse("sales:order_edit", args=[order.id]))
        self.assertNotContains(resp, 'name="freight_value"')
        self.assertContains(resp, 'name="freight_commission_deduction"')

    def test_customer_paid_freight_deduction_can_be_saved(self):
        quote = self._quote("ORC-8002", "CUSTOMER", Decimal("0.00"))
        order = Order.objects.create(
            number="ORC-8002", quote=quote, is_total_conference=True,
            status=OrderStatus.PENDING,
        )
        item = order.items.create(
            product_name="Cadeira", quantity=1, purchase_unit_cost=Decimal("50.00"),
        )
        self.client.login(username="admin", password="x")
        resp = self.client.post(reverse("sales:order_edit", args=[order.id]), {
            "supplier": "", "status": OrderStatus.PENDING,
            "created_at": "2026-07-05T10:00", "purchase_condition_text": "",
            "transport_info": "", "delivery_deadline": "", "notes": "",
            "freight_commission_deduction": "45,00",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(item.id), "items-0-product_name": "Cadeira",
            "items-0-description": "", "items-0-quantity": "1",
            "items-0-purchase_unit_cost": "50,00",
        })
        self.assertEqual(resp.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual(quote.freight_commission_deduction, Decimal("45.00"))
        self.assertEqual(quote.freight_value, Decimal("0.00"))

    def test_blank_freight_fields_leave_the_quote_alone(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.freight_value, Decimal("100.00"))
