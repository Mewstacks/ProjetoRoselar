"""Regressões dos números exibidos no início, painel e metas."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Customer, GoalPeriod, GoalType, SalesGoal
from core.views import _month_bounds, _prev_month_bounds
from sales.models import Quote, QuoteStatus


class DashboardNumbersTests(TestCase):
    def setUp(self):
        self.seller = get_user_model().objects.create_user(
            username="vendedor-painel", password="x", role="SELLER",
            individual_target_value=Decimal("9000.00"),
        )
        self.customer = Customer.objects.create(name="Cliente do painel")
        self.today = timezone.localdate()
        self.start, self.end = _month_bounds(self.today)
        self.client.force_login(self.seller)

    def sale(self, number, day, amount):
        quote = Quote.objects.create(
            number=number,
            customer=self.customer,
            seller=self.seller,
            status=QuoteStatus.CONVERTED,
            quote_date=day,
            sale_date=day,
            freight_responsible="CUSTOMER",
        )
        quote.items.create(product_name="Móvel", quantity=1, unit_value=Decimal(amount))
        quote.refresh_from_db()
        return quote

    def test_home_and_dashboard_use_registered_goal_and_real_sale_total(self):
        SalesGoal.objects.create(
            goal_type=GoalType.INDIVIDUAL, period=GoalPeriod.MONTHLY,
            seller=self.seller, period_start=self.start, period_end=self.end,
            target_value=Decimal("4000.00"),
        )
        self.sale("PAINEL-1", self.start + timedelta(days=1), "1000.00")

        for name in ("core:index", "core:dashboard"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["my_total_sold_month"], Decimal("1000.00"))
                self.assertEqual(response.context["goal_target"], Decimal("4000.00"))
                self.assertEqual(response.context["goal_pct_raw"], 25.0)
                self.assertEqual(response.context["goal_source"], "Meta cadastrada")

    def test_previous_cycle_selector_changes_metrics_and_goal(self):
        prev_start, prev_end = _prev_month_bounds(self.start)
        self.sale("PAINEL-ANT", prev_start + timedelta(days=1), "1500.00")
        SalesGoal.objects.create(
            goal_type=GoalType.INDIVIDUAL, period=GoalPeriod.MONTHLY,
            seller=self.seller, period_start=prev_start, period_end=prev_end,
            target_value=Decimal("3000.00"),
        )

        for name in ("core:index", "core:dashboard"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name), {"ciclo": prev_start.isoformat()})
                self.assertEqual(response.context["cycle_start"], prev_start)
                self.assertEqual(response.context["my_total_sold_month"], Decimal("1500.00"))
                self.assertEqual(response.context["goal_target"], Decimal("3000.00"))
                self.assertEqual(response.context["goal_pct_raw"], 50.0)

    def test_no_current_quotes_is_not_presented_as_zero_conversion(self):
        self.seller.individual_target_value = Decimal("0.00")
        self.seller.save(update_fields=["individual_target_value"])
        response = self.client.get(reverse("core:index"))
        self.assertEqual(response.context["my_quotes_count_month"], 0)
        self.assertContains(response, "Sem orçamentos no ciclo")
        self.assertContains(response, "Meta não definida")

    def test_goals_page_calculates_progress_from_sales(self):
        goal = SalesGoal.objects.create(
            goal_type=GoalType.INDIVIDUAL, period=GoalPeriod.MONTHLY,
            seller=self.seller, period_start=self.start, period_end=self.end,
            target_value=Decimal("4000.00"), target_quantity=4,
        )
        self.sale("PAINEL-META", self.start + timedelta(days=1), "1000.00")

        response = self.client.get(reverse("core:goals_list"))
        rendered_goal = next(item for item in response.context["goals"] if item.pk == goal.pk)
        self.assertEqual(rendered_goal.achieved_value, Decimal("1000.00"))
        self.assertEqual(rendered_goal.achieved_count, 1)
        self.assertEqual(rendered_goal.progress_pct, Decimal("25.0"))
        self.assertContains(response, 'style="width:25.0%;"')
        self.assertContains(response, "1 de 4 vendas")
