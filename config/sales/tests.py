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

    def test_editing_order_date_propagates_to_sibling_orders(self):
        """A data de emissão é uma só para a venda: todos os pedidos acompanham."""
        from core.models import Supplier

        supplier = Supplier.objects.create(name="Fornecedor A")
        sibling = Order.objects.create(
            number="ORC-9999-A",
            quote=self.quote,
            supplier=supplier,
            status=OrderStatus.PENDING,
        )

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
        self.order.refresh_from_db()
        sibling.refresh_from_db()
        self.assertEqual(sibling.created_at, self.order.created_at)
        self.assertEqual(timezone.localtime(sibling.created_at).date(), date(2026, 6, 24))


class OrdersNavTabTests(TestCase):
    """Aba 'Pedidos' na navbar: só financeiro e admin enxergam."""

    def setUp(self):
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.finance = User.objects.create_user(username="f", password="x", role="FINANCE")
        self.admin = User.objects.create_user(username="a", password="x", role="ADMIN")
        self.superuser = User.objects.create_user(
            username="root", password="x", role="SELLER", is_superuser=True,
        )

    def _nav_has_orders_tab(self, username):
        self.client.login(username=username, password="x")
        html = self.client.get(reverse("core:index")).content.decode()
        nav = html.split("</nav>")[0]
        return ">Pedidos</a>" in nav

    def test_finance_and_admin_see_the_tab(self):
        self.assertTrue(self._nav_has_orders_tab("f"))
        self.assertTrue(self._nav_has_orders_tab("a"))

    def test_superuser_sees_the_tab(self):
        """O admin do sistema tem role SELLER: sem is_superuser a aba sumiria."""
        self.assertTrue(self._nav_has_orders_tab("root"))

    def test_seller_does_not_see_the_tab(self):
        self.assertFalse(self._nav_has_orders_tab("v"))

    def test_order_list_only_shows_own_orders_to_a_seller(self):
        from core.models import Customer

        customer = Customer.objects.create(name="Cliente")
        other = User.objects.create_user(username="v2", password="x", role="SELLER")
        mine = Quote.objects.create(
            number="ORC-M", customer=customer, seller=self.seller,
            status=QuoteStatus.CONVERTED,
        )
        theirs = Quote.objects.create(
            number="ORC-T", customer=customer, seller=other,
            status=QuoteStatus.CONVERTED,
        )
        Order.objects.create(number="ORC-M", quote=mine, is_total_conference=True)
        Order.objects.create(number="ORC-T", quote=theirs, is_total_conference=True)

        self.client.login(username="v", password="x")
        html = self.client.get(reverse("sales:order_list")).content.decode()
        self.assertIn("ORC-M", html)
        self.assertNotIn("ORC-T", html)

        self.client.login(username="f", password="x")
        html = self.client.get(reverse("sales:order_list")).content.decode()
        self.assertIn("ORC-M", html)
        self.assertIn("ORC-T", html)


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


class OrderEditFormsetTests(TestCase):
    """Regressão: a linha extra vazia do formset não pode travar a edição.

    O campo `quantity` herda o default 1 como initial; sem a guarda em
    OrderItemForm.has_changed, uma linha nova/órfã cujo quantity chega vazio era
    validada como preenchida e quebrava a edição com "campos obrigatórios".
    """

    def setUp(self):
        from core.models import Customer

        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN")
        self.customer = Customer.objects.create(name="Cliente Teste")
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.quote = Quote.objects.create(
            number="ORC-7000", customer=self.customer, seller=self.seller,
            status=QuoteStatus.CONVERTED, sale_date=date(2026, 7, 5),
        )
        self.order = Order.objects.create(
            number="ORC-7000", quote=self.quote, is_total_conference=True,
            status=OrderStatus.PENDING,
        )
        self.item = self.order.items.create(
            product_name="Cadeira", quantity=3, purchase_unit_cost=Decimal("50.00"),
        )

    def _base(self, total_forms):
        return {
            "supplier": "",
            "status": OrderStatus.PENDING,
            "created_at": "2026-07-05T10:00",
            "purchase_condition_text": "",
            "transport_info": "",
            "delivery_deadline": "",
            "notes": "",
            "items-TOTAL_FORMS": str(total_forms),
            "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(self.item.id),
            "items-0-product_name": "Cadeira",
            "items-0-description": "",
            "items-0-quantity": "3",
            "items-0-purchase_unit_cost": "50,00",
        }

    def test_edit_with_empty_extra_row_quantity_blank(self):
        self.client.login(username="admin", password="x")
        data = self._base(2)
        data.update({
            "items-1-id": "", "items-1-product_name": "",
            "items-1-description": "", "items-1-quantity": "",
            "items-1-purchase_unit_cost": "",
        })
        resp = self.client.post(reverse("sales:order_edit", args=[self.order.id]), data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["formset"].errors)
        self.assertEqual(self.order.items.count(), 1)

    def test_edit_with_orphan_row_after_add_then_delete(self):
        # Usuário adicionou uma linha (TOTAL_FORMS=3) e depois removeu do DOM:
        # o índice 2 não envia dados. Não pode travar a edição.
        self.client.login(username="admin", password="x")
        data = self._base(3)
        data.update({
            "items-1-id": "", "items-1-product_name": "",
            "items-1-quantity": "1", "items-1-purchase_unit_cost": "",
        })
        resp = self.client.post(reverse("sales:order_edit", args=[self.order.id]), data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["formset"].errors)

    def test_edit_adds_a_real_new_item(self):
        self.client.login(username="admin", password="x")
        data = self._base(2)
        data.update({
            "items-1-id": "", "items-1-product_name": "Mesa",
            "items-1-description": "", "items-1-quantity": "1",
            "items-1-purchase_unit_cost": "120,00",
        })
        resp = self.client.post(reverse("sales:order_edit", args=[self.order.id]), data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["formset"].errors)
        self.assertEqual(self.order.items.count(), 2)


class QuoteSaleDateEndpointTests(TestCase):
    """Editar a data da venda direto no orçamento (Parte 2)."""

    def setUp(self):
        from core.models import Customer

        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN")
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente")
        self.quote = Quote.objects.create(
            number="ORC-7100", customer=self.customer, seller=self.seller,
            status=QuoteStatus.CONVERTED, sale_date=date(2026, 7, 5),
        )

    def test_set_sale_date(self):
        self.client.login(username="admin", password="x")
        resp = self.client.post(
            reverse("sales:quote_set_sale_date", args=[self.quote.id]),
            {"sale_date": "2026-06-20"},
        )
        self.assertEqual(resp.status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.sale_date, date(2026, 6, 20))

    def test_reject_when_not_sold(self):
        self.quote.status = QuoteStatus.DRAFT
        self.quote.save(update_fields=["status"])
        self.client.login(username="admin", password="x")
        resp = self.client.post(
            reverse("sales:quote_set_sale_date", args=[self.quote.id]),
            {"sale_date": "2026-06-20"},
        )
        self.assertEqual(resp.status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.sale_date, date(2026, 7, 5))  # inalterado


class DualPricingTests(TestCase):
    """Orçamento atacado + varejo: dois preços por item (Parte 3)."""

    def setUp(self):
        from core.models import Customer

        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN")
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente")
        self.quote = Quote.objects.create(
            number="ORC-7200", customer=self.customer, seller=self.seller,
            status=QuoteStatus.DRAFT, dual_pricing=True,
            freight_responsible="CUSTOMER",
        )
        self.quote.items.create(
            product_name="Sofá", quantity=2,
            unit_value=Decimal("1000.00"), unit_value_wholesale=Decimal("800.00"),
        )
        self.quote.items.create(
            product_name="Puff", quantity=1,
            unit_value=Decimal("300.00"),  # sem atacado: cai no varejo
        )

    def test_wholesale_subtotal_uses_wholesale_price_with_retail_fallback(self):
        # varejo: 2*1000 + 1*300 = 2300
        self.assertEqual(self.quote.calculate_subtotal(), Decimal("2300.00"))
        # atacado: 2*800 + 1*300(fallback) = 1900
        self.assertEqual(self.quote.calculate_subtotal_wholesale(), Decimal("1900.00"))

    def test_wholesale_total_lower_than_retail(self):
        retail = self.quote.calculate_rounded_total()
        wholesale = self.quote.calculate_rounded_total_wholesale()
        self.assertEqual(retail, Decimal("2300.00"))
        self.assertEqual(wholesale, Decimal("1900.00"))
        self.assertLess(wholesale, retail)

    def test_wholesale_total_ignores_total_override(self):
        # total_override fixa só o varejo; o atacado segue calculado.
        self.quote.total_override = Decimal("2500.00")
        self.quote.save(update_fields=["total_override"])
        self.assertEqual(self.quote.calculate_rounded_total(), Decimal("2500.00"))
        self.assertEqual(self.quote.calculate_rounded_total_wholesale(), Decimal("1900.00"))

    def test_client_pdf_renders_with_dual_pricing(self):
        self.client.login(username="admin", password="x")
        resp = self.client.get(reverse("sales:quote_pdf_client", args=[self.quote.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertGreater(len(resp.content), 1000)
