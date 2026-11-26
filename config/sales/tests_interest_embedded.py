"""Juros do crédito 7x+ entram por dentro no total do cliente.

Total = (produtos + frete) ÷ (1 − taxa) sobre o que é financiado. De 1x a 6x, PIX,
dinheiro, débito e preço final digitado não mudam. Vendas fechadas antes da regra
mantêm o total antigo.
"""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Customer, PaymentTariff
from sales.margin import embed_interest, simulate_quote
from sales.models import Quote, QuoteStatus

User = get_user_model()


class EmbedInterestMathTests(TestCase):
    def test_financed_part_is_grossed_up_and_down_payment_is_not(self):
        total, first, second = embed_interest(D("1000"), fee_1=D("10"), down_payment=D("200"))
        # 200 + 800 / 0,90
        self.assertEqual(total, D("1088.89"))
        self.assertEqual((first, second), (total, D("0")))

    def test_no_fee_means_no_change(self):
        self.assertEqual(embed_interest(D("1000"))[0], D("1000.00"))

    def test_split_uses_the_customer_visible_first_leg(self):
        # 1ª parte 450 com 10% cobre 405 da base; sobram 595 com 20%
        total, first, second = embed_interest(
            D("1000"), fee_1=D("10"), fee_2=D("20"), split_amount=D("450"),
        )
        self.assertEqual(first, D("450"))
        self.assertEqual(second, D("743.75"))
        self.assertEqual(total, D("1193.75"))

    def test_split_first_leg_covering_everything_is_capped(self):
        total, first, second = embed_interest(
            D("1000"), fee_1=D("10"), split_amount=D("5000"),
        )
        self.assertEqual((total, second), (D("1111.11"), D("0")))


class QuoteInterestTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="v", password="x", role="SELLER")
        self.customer = Customer.objects.create(name="Cliente")
        for installments, fee in ((6, "3.00"), (8, "10.50")):
            PaymentTariff.objects.update_or_create(
                payment_type="CREDIT_CARD", installments=installments,
                defaults={"fee_percent": D(fee)},
            )
        self.n = 0

    def quote(self, **kw):
        self.n += 1
        data = dict(
            number=f"INT-{self.n}", customer=self.customer, seller=self.seller,
            status=QuoteStatus.DRAFT, quote_date=date(2026, 11, 26),
            freight_responsible="STORE", freight_value=D("225.00"), delivery_days_min=1,
            payment_type="CREDIT_CARD", payment_installments=8, payment_fee_percent=D("10.50"),
        )
        data.update(kw)
        quote = Quote.objects.create(**data)
        quote.items.create(product_name="Sofá", quantity=1, unit_value=D("2251.20"))
        quote.refresh_from_db()
        return quote

    def test_photo_case_total_and_installments(self):
        quote = self.quote()
        # (2251,20 + 225) ÷ 0,895
        self.assertEqual(quote.total_value_snapshot, D("2766.70"))
        ctx = simulate_quote(quote)
        self.assertEqual(ctx["final_total"], D("2766.70"))
        self.assertEqual(ctx["installment_value_1"].quantize(D("0.01")), D("345.84"))
        self.assertEqual(ctx["interest_embedded_value"], D("2766.70") - D("2476.20"))

    def test_simulator_and_saved_total_agree(self):
        quote = self.quote(down_payment_value=D("500.00"))
        self.assertEqual(simulate_quote(quote)["final_total"], quote.total_value_snapshot)

    def test_up_to_6x_the_store_keeps_absorbing_the_fee(self):
        quote = self.quote(payment_installments=6, payment_fee_percent=D("3.00"))
        self.assertEqual(quote.total_value_snapshot, D("2476.20"))
        self.assertEqual(simulate_quote(quote)["final_total"], D("2476.20"))

    def test_typed_final_price_is_not_touched(self):
        quote = self.quote(total_override=D("2400.00"))
        self.assertEqual(quote.calculate_rounded_total(), D("2400.00"))
        self.assertEqual(simulate_quote(quote)["final_total"], D("2400.00"))

    def test_interest_does_not_change_commission_or_margin(self):
        with_interest = simulate_quote(self.quote())
        without = simulate_quote(self.quote(payment_installments=6, payment_fee_percent=D("3.00")))
        self.assertEqual(
            with_interest["seller_commission_gross_value"],
            without["seller_commission_gross_value"],
        )

    def test_sold_quote_before_the_rule_keeps_its_total(self):
        quote = self.quote(interest_embedded=False)
        self.assertEqual(quote.total_value_snapshot, D("2476.20"))

    def test_interest_is_not_counted_as_a_price_increase(self):
        quote = self.quote()
        gap, _pct = quote.calculate_real_gap()
        self.assertEqual(gap, D("0.00"))


class NetSalesWithoutInterestTests(QuoteInterestTests):
    def test_net_sales_drops_freight_and_interest(self):
        quote = self.quote()
        self.assertEqual(quote.interest_snapshot, D("290.50"))
        self.assertEqual(quote.net_sales_value, D("2251.20"))
        self.assertEqual(simulate_quote(quote)["net_sales_value"], D("2251.20"))

    def test_up_to_6x_net_sales_only_drops_freight(self):
        quote = self.quote(payment_installments=6, payment_fee_percent=D("3.00"))
        self.assertEqual(quote.interest_snapshot, D("0.00"))
        self.assertEqual(quote.net_sales_value, D("2251.20"))
