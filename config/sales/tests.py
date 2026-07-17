from datetime import date
from decimal import Decimal

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


class SimulationTariffTests(TestCase):
    """Simulador não pode liberar parcela cujo custo do banco é desconhecido."""

    def setUp(self):
        from core.models import PaymentTariff

        PaymentTariff.objects.all().delete()
        for inst, fee in ((1, "4.00"), (6, "3.00"), (12, "13.30")):
            PaymentTariff.objects.create(
                payment_type="CREDIT_CARD", installments=inst, fee_percent=Decimal(fee)
            )

    def _sim(self, payment_type, installments):
        from sales.views import _build_simulation_context

        return _build_simulation_context(
            subtotal=Decimal("10000"),
            freight_value=Decimal("0"),
            sim_payment_type=payment_type,
            sim_has_architect=False,
            sim_discount=Decimal("0"),
            price_increase_pct=Decimal("0"),
            sim_installments=installments,
        )

    def test_cartao_12x_estoura_margem_e_bloqueia(self):
        ctx = self._sim("CREDIT_CARD", 12)
        self.assertEqual(ctx["payment_fee_percent"], Decimal("13.30"))
        self.assertLess(ctx["margin_balance"], 0)
        self.assertTrue(ctx["controls_blocked"])

    def test_cheque_12x_cobra_taxa_do_cartao(self):
        ctx = self._sim("CHEQUE", 12)
        self.assertEqual(ctx["payment_fee_percent"], Decimal("13.30"))
        self.assertTrue(ctx["controls_blocked"])

    def test_parcela_sem_tarifa_bloqueia_em_vez_de_sair_de_graca(self):
        ctx = self._sim("CREDIT_CARD", 7)
        self.assertTrue(ctx["controls_blocked"])

    def test_parcela_sem_tarifa_nao_aparece_na_tela(self):
        import json

        ctx = self._sim("CREDIT_CARD", 1)
        oferecidas = [
            o["installments"]
            for o in json.loads(ctx["tariffs_by_type_json"])["CREDIT_CARD"]
        ]
        self.assertEqual(oferecidas, [1, 6, 12])


class SimulationSuggestionTests(TestCase):
    """Sugestoes de acrescimo: o motor precisa dizer QUANTO falta, nao so 'ta ruim'.

    A 'reforma testuaria' (6b8c4a2) fixou esses campos em 0/False e o template,
    que ja tinha a UI pronta, passou a cair sempre na mensagem generica.
    """

    def setUp(self):
        from core.models import PaymentTariff

        PaymentTariff.objects.all().delete()
        for inst, fee in ((1, "4.00"), (6, "3.00"), (7, "9.87"), (12, "13.30")):
            PaymentTariff.objects.create(
                payment_type="CREDIT_CARD", installments=inst, fee_percent=Decimal(fee)
            )

    def _sim(self, installments, price_increase=Decimal("0")):
        from sales.views import _build_simulation_context

        return _build_simulation_context(
            subtotal=Decimal("10000"),
            freight_value=Decimal("0"),
            sim_payment_type="CREDIT_CARD",
            sim_has_architect=False,
            sim_discount=Decimal("0"),
            price_increase_pct=price_increase,
            sim_installments=installments,
        )

    def test_margem_folgada_nao_sugere_desbloqueio(self):
        ctx = self._sim(6)
        self.assertFalse(ctx["controls_blocked"])
        self.assertEqual(ctx["min_increase_to_unblock"], Decimal("0"))

    def test_bloqueado_diz_quanto_falta(self):
        ctx = self._sim(12)
        self.assertTrue(ctx["controls_blocked"])
        self.assertGreater(ctx["min_increase_to_unblock"], Decimal("0"))

    def test_sugestao_realmente_desbloqueia(self):
        ctx = self._sim(12)
        sugerido = ctx["min_increase_to_unblock"]
        depois = self._sim(12, price_increase=sugerido)
        self.assertFalse(depois["controls_blocked"])
        self.assertGreaterEqual(depois["margin_balance"], 0)

    def test_sugestao_e_o_minimo(self):
        # Um passo (0,1%) abaixo do sugerido ainda tem que travar, senao o
        # simulador esta pedindo mais dinheiro do que o necessario ao cliente.
        ctx = self._sim(12)
        quase = ctx["min_increase_to_unblock"] - Decimal("0.1")
        self.assertTrue(self._sim(12, price_increase=quase)["controls_blocked"])

    def test_oportunidade_quando_comissao_abaixo_do_teto(self):
        ctx = self._sim(7)
        self.assertFalse(ctx["controls_blocked"])
        self.assertTrue(ctx["suggestion_is_opportunity"])
        self.assertGreater(ctx["suggested_increase"], Decimal("0"))
        depois = self._sim(7, price_increase=ctx["suggested_increase"])
        self.assertGreaterEqual(
            depois["seller_commission_percent"], ctx["commission_max"]
        )

    def test_tarifa_ausente_nao_sugere_nada(self):
        # Sem tarifa nao da para saber o custo; sugerir um numero seria inventar.
        ctx = self._sim(9)
        self.assertTrue(ctx["controls_blocked"])
        self.assertEqual(ctx["min_increase_to_unblock"], Decimal("0"))
        self.assertEqual(ctx["suggested_increase"], Decimal("0"))
