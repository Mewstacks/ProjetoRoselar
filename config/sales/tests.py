from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Supplier
from sales.models import Order, OrderStatus, Quote, QuoteStatus

User = get_user_model()


class StandaloneOrderTests(TestCase):
    """Pedido avulso da loja: compra de estoque sem orçamento/vendedor."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", password="x", role="ADMIN"
        )
        self.seller = User.objects.create_user(
            username="vendedor", password="x", role="SELLER"
        )
        self.supplier = Supplier.objects.create(name="Fornecedor Teste")

    def _create_standalone(self):
        return Order.objects.create(
            number="LOJA-0001",
            quote=None,
            supplier=self.supplier,
            is_total_conference=False,
            status=OrderStatus.PENDING,
        )

    def test_order_without_quote_is_allowed(self):
        order = self._create_standalone()
        self.assertIsNone(order.quote)

    def test_create_view_requires_finance_or_admin(self):
        self.client.login(username="vendedor", password="x")
        resp = self.client.get(reverse("sales:order_create_standalone"))
        self.assertEqual(resp.status_code, 302)  # redirect com "Acesso negado"

        self.client.login(username="admin", password="x")
        resp = self.client.get(reverse("sales:order_create_standalone"))
        self.assertEqual(resp.status_code, 200)

    def test_create_standalone_order_via_post(self):
        self.client.login(username="admin", password="x")
        now = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        resp = self.client.post(
            reverse("sales:order_create_standalone"),
            {
                "supplier": self.supplier.id,
                "status": OrderStatus.PENDING,
                "created_at": now,
                "purchase_condition_text": "",
                "transport_info": "",
                "delivery_deadline": "",
                "notes": "",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-product_name": "Sofá estoque",
                "items-0-description": "",
                "items-0-quantity": "2",
                "items-0-purchase_unit_cost": "1.500,00",
            },
        )
        order = Order.objects.filter(quote__isnull=True).first()
        self.assertIsNotNone(order, resp.context["form"].errors if resp.context else None)
        self.assertTrue(order.number.startswith("LOJA-"))
        self.assertEqual(order.items.count(), 1)
        self.assertRedirects(resp, reverse("sales:order_detail", args=[order.id]))

    def test_detail_hidden_from_seller(self):
        order = self._create_standalone()
        self.client.login(username="vendedor", password="x")
        resp = self.client.get(reverse("sales:order_detail", args=[order.id]))
        self.assertEqual(resp.status_code, 302)

    def test_detail_and_list_render_for_admin(self):
        order = self._create_standalone()
        self.client.login(username="admin", password="x")
        resp = self.client.get(reverse("sales:order_detail", args=[order.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Compra da Loja")
        resp = self.client.get(reverse("sales:order_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "LOJA-0001")

    def test_cancel_standalone_order(self):
        order = self._create_standalone()
        self.client.login(username="admin", password="x")
        resp = self.client.post(reverse("sales:order_cancel", args=[order.id]))
        self.assertRedirects(resp, reverse("sales:order_list"))
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())


class OrderDateSyncTests(TestCase):
    """Editar a data do pedido realinha Quote.sale_date (mês da comissão)."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", password="x", role="ADMIN"
        )
        self.seller = User.objects.create_user(
            username="vendedor", password="x", role="SELLER"
        )
        from core.models import Customer

        self.customer = Customer.objects.create(name="Cliente Teste")
        self.quote = Quote.objects.create(
            number="ORC-9999",
            customer=self.customer,
            seller=self.seller,
            status=QuoteStatus.CONVERTED,
            sale_date=date(2026, 7, 5),
        )
        self.order = Order.objects.create(
            number="ORC-9999",
            quote=self.quote,
            is_total_conference=True,
            status=OrderStatus.PENDING,
        )

    def test_editing_quote_dates_via_quote_edit(self):
        """Editar Data do Orçamento e Data da Venda direto na tela do orçamento."""
        self.client.login(username="admin", password="x")
        resp = self.client.post(
            reverse("sales:quote_edit", args=[self.quote.id]),
            {
                "customer": self.customer.id,
                "quote_date": "2026-06-10",
                "sale_date": "2026-06-24",
                "freight_responsible": "CUSTOMER",
                "payment_type": "",
                "total_override": "",
                "notes": "",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.quote_date, date(2026, 6, 10))
        self.assertEqual(self.quote.sale_date, date(2026, 6, 24))

    def test_blank_sale_date_keeps_existing_value(self):
        """Submit sem sale_date não apaga a data da venda de orçamento vendido."""
        self.client.login(username="admin", password="x")
        resp = self.client.post(
            reverse("sales:quote_edit", args=[self.quote.id]),
            {
                "customer": self.customer.id,
                "quote_date": "",
                "sale_date": "",
                "freight_responsible": "CUSTOMER",
                "payment_type": "",
                "total_override": "",
                "notes": "",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.sale_date, date(2026, 7, 5))

    def test_editing_order_date_updates_sale_date(self):
        self.client.login(username="admin", password="x")
        resp = self.client.post(
            reverse("sales:order_edit", args=[self.order.id]),
            {
                "supplier": "",
                "status": OrderStatus.PENDING,
                "created_at": "2026-06-24T10:00",
                "purchase_condition_text": "",
                "transport_info": "",
                "delivery_deadline": "",
                "notes": "",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.sale_date, date(2026, 6, 24))
