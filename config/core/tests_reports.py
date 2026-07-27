"""Testes dos relatórios.

Cobrem os defeitos que motivaram a auditoria: comissão divergente do motor de
margem, descontos contaminados por orçamentos cancelados, ranking de produtos
que não reconciliava com o faturamento e exportação que ignorava os filtros.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Customer, PaymentTariff, PaymentMethodType
from core.views import _product_revenue, _report_period
from sales.margin import persist_quote_commission, simulate_quote
from sales.models import CommissionSource, Quote, QuoteCommissionSplit, QuoteStatus

User = get_user_model()


class ReportTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="x", role="ADMIN")
        self.seller = User.objects.create_user(username="vendedor", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente Teste")
        self.today = timezone.localdate()
        self.client.login(username="admin", password="x")

    def _tariff(self, payment_type, installments, fee):
        # As migrações já semeiam tarifas padrão; aqui só fixamos o valor.
        obj, _ = PaymentTariff.objects.update_or_create(
            payment_type=payment_type, installments=installments,
            defaults={"fee_percent": Decimal(fee)},
        )
        return obj

    def _sale(self, number, *, items, sold_on=None, seller=None, **kwargs):
        quote = Quote.objects.create(
            number=number,
            customer=self.customer,
            seller=seller or self.seller,
            status=QuoteStatus.CONVERTED,
            sale_date=sold_on or self.today,
            freight_responsible=kwargs.pop("freight_responsible", "CUSTOMER"),
            **kwargs,
        )
        for name, qty, unit in items:
            quote.items.create(product_name=name, quantity=qty, unit_value=Decimal(unit))
        quote.refresh_from_db()
        return quote

    def _period_qs(self, **extra):
        params = {"date_from": self.today.isoformat(), "date_to": self.today.isoformat()}
        params.update(extra)
        return params


class ReportSalesTests(ReportTestBase):
    def test_total_is_sum_of_snapshots(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")])
        self._sale("V-2", items=[("Cadeira", 2, "250.00")])

        resp = self.client.get(reverse("core:report_sales"), self._period_qs())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total"], Decimal("1500.00"))
        self.assertEqual(resp.context["count"], 2)
        self.assertEqual(resp.context["avg_value"], Decimal("750.00"))

    def test_canceled_quote_is_not_a_sale(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")])
        canceled = self._sale("V-2", items=[("Mesa", 1, "9999.00")])
        canceled.status = QuoteStatus.CANCELED
        canceled.save(update_fields=["status"])

        resp = self.client.get(reverse("core:report_sales"), self._period_qs())

        self.assertEqual(resp.context["count"], 1)
        self.assertEqual(resp.context["total"], Decimal("1000.00"))

    def test_sale_outside_period_is_excluded(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")], sold_on=self.today - timedelta(days=40))

        resp = self.client.get(reverse("core:report_sales"), self._period_qs())

        self.assertEqual(resp.context["count"], 0)

    def test_inverted_dates_are_swapped_instead_of_returning_nothing(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")], sold_on=self.today - timedelta(days=3))

        resp = self.client.get(reverse("core:report_sales"), {
            "date_from": self.today.isoformat(),
            "date_to": (self.today - timedelta(days=10)).isoformat(),
        })

        self.assertEqual(resp.context["count"], 1)


class CommissionReportTests(ReportTestBase):
    """O relatório precisa devolver EXATAMENTE o que o motor de margem apurou."""

    def setUp(self):
        super().setUp()
        self._tariff(PaymentMethodType.BOLETO, 1, "0.00")
        self._tariff(PaymentMethodType.BOLETO, 3, "4.00")
        self._tariff(PaymentMethodType.CREDIT_CARD, 4, "6.00")
        self._tariff(PaymentMethodType.CREDIT_CARD, 10, "12.00")
        self._tariff(PaymentMethodType.PIX, 1, "0.00")

    def _sold_with_commission(self, number, **kwargs):
        quote = self._sale(number, items=[("Mesa", 1, "1000.00")], **kwargs)
        persist_quote_commission(quote)
        return quote

    def test_boleto_a_vista_pays_4_percent_not_3(self):
        """Regressão: o relatório antigo agrupava boleto com crédito e pagava 3%."""
        quote = self._sold_with_commission(
            "V-BOL1", payment_type=PaymentMethodType.BOLETO, payment_installments=1,
        )
        self.assertEqual(quote.commission_pct, Decimal("4.00"))

    def test_credit_card_up_to_6x_pays_3_percent(self):
        quote = self._sold_with_commission(
            "V-CC4", payment_type=PaymentMethodType.CREDIT_CARD, payment_installments=4,
        )
        self.assertEqual(quote.commission_pct, Decimal("3.00"))

    def test_persisted_commission_matches_the_margin_engine(self):
        cases = [
            ("V-A", PaymentMethodType.BOLETO, 1, "0.0", "0.0"),
            ("V-B", PaymentMethodType.BOLETO, 3, "0.0", "0.0"),
            ("V-C", PaymentMethodType.CREDIT_CARD, 4, "0.0", "0.0"),
            ("V-D", PaymentMethodType.CREDIT_CARD, 10, "0.0", "0.0"),
            ("V-E", PaymentMethodType.PIX, 1, "8.0", "0.0"),
            ("V-F", PaymentMethodType.PIX, 1, "0.0", "10.0"),
        ]
        for number, ptype, inst, discount, markup in cases:
            with self.subTest(number=number):
                quote = self._sold_with_commission(
                    number,
                    payment_type=ptype,
                    payment_installments=inst,
                    discount_percent=Decimal(discount),
                    price_increase_percent=Decimal(markup),
                )
                engine = simulate_quote(quote)
                self.assertEqual(
                    quote.commission_pct,
                    Decimal(str(engine["seller_commission_percent"])).quantize(Decimal("0.01")),
                )
                self.assertEqual(
                    quote.commission_value,
                    Decimal(str(engine["seller_commission_value"])).quantize(Decimal("0.01")),
                )

    def test_commission_base_excludes_freight(self):
        """Frete repassado inflava a comissão: 3% incidiam sobre produtos + frete."""
        quote = self._sold_with_commission(
            "V-FRETE",
            payment_type=PaymentMethodType.CREDIT_CARD,
            payment_installments=4,
            freight_value=Decimal("5000.00"),
            freight_responsible="CARRIER",
        )
        # Produtos = 1.000; frete = 5.000. A comissão é 3% dos produtos apenas.
        self.assertEqual(quote.commission_value, Decimal("30.00"))
        self.assertGreater(quote.total_value_snapshot, Decimal("5000.00"))

    def test_report_sums_persisted_values(self):
        self._sold_with_commission(
            "V-1", payment_type=PaymentMethodType.CREDIT_CARD, payment_installments=4,
        )
        self._sold_with_commission(
            "V-2", payment_type=PaymentMethodType.BOLETO, payment_installments=1,
        )

        resp = self.client.get(reverse("core:report_commissions"), self._period_qs())

        self.assertEqual(resp.status_code, 200)
        # 3% de 1000 + 4% de 1000
        self.assertEqual(resp.context["total_commission"], Decimal("70.00"))
        self.assertEqual(resp.context["sales_count"], 2)
        self.assertEqual(resp.context["pending_count"], 0)

    def test_split_divides_commission_exactly(self):
        other = User.objects.create_user(username="parceiro", password="x", role="SELLER")
        quote = self._sold_with_commission(
            "V-SPLIT", payment_type=PaymentMethodType.CREDIT_CARD, payment_installments=4,
        )
        split = QuoteCommissionSplit.objects.create(quote=quote)
        split.users.set([self.seller, other])

        resp = self.client.get(reverse("core:report_commissions"), self._period_qs())
        rows = resp.context["commissions"]

        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["est_commission"] for r in rows), quote.commission_value)
        self.assertEqual(sum(r["total_sold"] for r in rows), quote.total_value_snapshot)
        # Cada participante conta uma participação, mas a venda é uma só.
        self.assertEqual(sum(r["count"] for r in rows), 2)
        self.assertEqual(resp.context["sales_count"], 1)

    def test_unpriced_sale_is_flagged_not_counted_as_zero(self):
        self._sale("V-SEM", items=[("Mesa", 1, "1000.00")])  # sem persist_quote_commission

        resp = self.client.get(reverse("core:report_commissions"), self._period_qs())

        self.assertEqual(resp.context["pending_count"], 1)
        self.assertEqual(resp.context["total_commission"], Decimal("0.00"))
        self.assertEqual(resp.context["commissions"], [])

    def test_backfilled_sales_are_flagged(self):
        quote = self._sale("V-OLD", items=[("Mesa", 1, "1000.00")],
                           payment_type=PaymentMethodType.PIX, payment_installments=1)
        persist_quote_commission(quote, source=CommissionSource.BACKFILL)

        resp = self.client.get(reverse("core:report_commissions"), self._period_qs())

        self.assertEqual(resp.context["estimated_count"], 1)


class DiscountReportTests(ReportTestBase):
    def test_canceled_quote_does_not_pollute_average(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")], discount_percent=Decimal("10.0"))
        canceled = self._sale("V-2", items=[("Mesa", 1, "1000.00")],
                              discount_percent=Decimal("30.0"))
        canceled.status = QuoteStatus.CANCELED
        canceled.save(update_fields=["status"])

        resp = self.client.get(reverse("core:report_discounts"), self._period_qs())

        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(resp.context["avg_discount"], Decimal("10.0"))

    def test_draft_excluded_by_default_and_included_in_broad_mode(self):
        Quote.objects.create(
            number="ORC-DRAFT", customer=self.customer, seller=self.seller,
            status=QuoteStatus.DRAFT, quote_date=self.today,
            discount_percent=Decimal("25.0"), freight_responsible="CUSTOMER",
        )

        default = self.client.get(reverse("core:report_discounts"), self._period_qs())
        self.assertEqual(default.context["total_count"], 0)

        broad = self.client.get(reverse("core:report_discounts"), self._period_qs(sold_only="0"))
        self.assertEqual(broad.context["total_count"], 1)
        self.assertEqual(broad.context["date_basis"], "data do orçamento")


class ProductReportTests(ReportTestBase):
    def test_net_revenue_reconciles_with_sale_total(self):
        quote = self._sale(
            "V-1",
            items=[("Mesa", 1, "1000.00"), ("Cadeira", 4, "250.00")],
            discount_percent=Decimal("10.0"),
        )

        rows = _product_revenue(self.today, self.today)
        total = sum(r["total_value"] for r in rows)

        # Desconto de 10% sobre 2.000 => 1.800, e a soma dos itens tem que fechar.
        self.assertEqual(quote.total_value_snapshot, Decimal("1800.00"))
        self.assertEqual(total, Decimal("1800.00"))

    def test_freight_is_not_counted_as_product_revenue(self):
        quote = self._sale(
            "V-1",
            items=[("Mesa", 1, "1000.00")],
            freight_value=Decimal("400.00"),
            freight_responsible="CARRIER",
        )

        rows = _product_revenue(self.today, self.today)

        self.assertEqual(quote.total_value_snapshot, Decimal("1400.00"))
        self.assertEqual(sum(r["total_value"] for r in rows), Decimal("1000.00"))

    def test_total_override_flows_into_product_revenue(self):
        quote = self._sale("V-1", items=[("Mesa", 1, "1000.00")])
        quote.total_override = Decimal("900.00")
        quote.save()
        quote.refresh_from_db()

        rows = _product_revenue(self.today, self.today)

        self.assertEqual(quote.total_value_snapshot, Decimal("900.00"))
        self.assertEqual(rows[0]["total_value"], Decimal("900.00"))

    def test_product_names_group_case_and_space_insensitively(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")])
        self._sale("V-2", items=[("MESA ", 1, "500.00")])
        self._sale("V-3", items=[("mesa", 1, "500.00")])

        rows = _product_revenue(self.today, self.today)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(rows[0]["total_value"], Decimal("2000.00"))


class CsvExportTests(ReportTestBase):
    def test_export_honors_seller_filter(self):
        other = User.objects.create_user(username="outro", password="x", role="SELLER")
        self._sale("V-MINE", items=[("Mesa", 1, "1000.00")])
        self._sale("V-THEIRS", items=[("Mesa", 1, "2000.00")], seller=other)

        resp = self.client.get(
            reverse("core:report_csv_export"), self._period_qs(seller=str(self.seller.pk)),
        )
        body = resp.content.decode("utf-8-sig")

        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("V-MINE", body)
        self.assertNotIn("V-THEIRS", body)

    def test_export_honors_period(self):
        self._sale("V-OLD", items=[("Mesa", 1, "1000.00")],
                   sold_on=self.today - timedelta(days=60))
        self._sale("V-NOW", items=[("Mesa", 1, "1000.00")])

        resp = self.client.get(reverse("core:report_csv_export"), self._period_qs())
        body = resp.content.decode("utf-8-sig")

        self.assertIn("V-NOW", body)
        self.assertNotIn("V-OLD", body)

    def test_export_keeps_header_and_semicolon_delimiter(self):
        self._sale("V-1", items=[("Mesa", 1, "1000.00")])

        resp = self.client.get(reverse("core:report_csv_export"), self._period_qs())
        body = resp.content.decode("utf-8-sig")

        self.assertTrue(body.startswith("Número;Data;Cliente;Vendedor;Desconto %;Total R$"))


class ReportAccessTests(ReportTestBase):
    def test_seller_cannot_open_reports(self):
        self.client.logout()
        self.client.login(username="vendedor", password="x")
        for name in (
            "core:reports_hub", "core:report_sales", "core:report_commissions",
            "core:report_discounts", "core:report_products", "core:report_csv_export",
        ):
            with self.subTest(view=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 302)
