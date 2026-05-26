from __future__ import annotations

import json
from collections import defaultdict
from datetime import date as date_type, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum, Count, Q, F, Avg


def health_check(request):
    return HttpResponse("ok", content_type="text/plain")
from django.db.models.functions import TruncMonth
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.contrib import messages

from .models import (
    Customer, ShippingCompany,
    Notification, NotificationType,
    AuditLog, AuditAction,
    SalesGoal, GoalType, GoalPeriod,
    CommunicationHistory,
    QuoteTemplate, QuoteTemplateItem,
)
from sales.models import Quote, QuoteStatus, Order, OrderStatus, QuoteItem
from accounts.models import User, Role
from calendar_app.models import CalendarEvent, EventStatus


def _month_bounds(day: date_type) -> tuple[date_type, date_type]:
    month_start = day.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    return month_start, month_end


def _last_n_month_starts(day: date_type, n: int = 6) -> list[date_type]:
    starts = []
    cur = day.replace(day=1)
    for _ in range(n):
        starts.append(cur)
        cur = (cur - timedelta(days=1)).replace(day=1)
    starts.reverse()
    return starts


def _normalize_month_key(value):
    return value.date() if hasattr(value, "date") else value


def _build_month_series(rows, month_starts: list[date_type]) -> tuple[list[str], list[float], list[int]]:
    rows_by_month = {_normalize_month_key(r["month"]): r for r in rows}
    labels = [m.strftime("%b/%y") for m in month_starts]
    totals = [float((rows_by_month.get(m, {}) or {}).get("total") or 0) for m in month_starts]
    counts = [int((rows_by_month.get(m, {}) or {}).get("count") or 0) for m in month_starts]
    return labels, totals, counts


def _net_quote_value(quote) -> Decimal:
    """Returns the quote's customer-facing total.

    total_value_snapshot already stores the clean value (subtotal - discount + freight)
    with NO payment fee added — the fee is a store cost absorbed by the margin.
    """
    return quote.total_value_snapshot or Decimal("0")


def _sum_net_quote_values(quotes) -> Decimal:
    return sum((_net_quote_value(q) for q in quotes), Decimal("0"))


def _build_net_month_series_from_quotes(quotes, month_starts: list[date_type]) -> tuple:
    totals = {m: Decimal("0") for m in month_starts}
    counts = {m: 0 for m in month_starts}
    for q in quotes:
        m = q.quote_date.replace(day=1)
        if m in totals:
            totals[m] += _net_quote_value(q)
            counts[m] += 1
    return (
        [m.strftime("%b/%y") for m in month_starts],
        [float(totals[m]) for m in month_starts],
        [counts[m] for m in month_starts],
    )


# ──────────────────────────────────────────────────────────────────────
# Home
# ──────────────────────────────────────────────────────────────────────
@login_required
def home(request):
    context = {}
    if request.user.is_authenticated:
        user = request.user
        today = timezone.localdate()
        month_start, month_end = _month_bounds(today)
        is_admin = user.role == Role.ADMIN or user.is_superuser
        is_staff_or_admin = is_admin

        my_quotes = Quote.objects.filter(seller=user)
        my_quotes_month = my_quotes.filter(quote_date__gte=month_start, quote_date__lte=month_end)
        my_converted = my_quotes.filter(status=QuoteStatus.CONVERTED)
        my_converted_month = my_converted.filter(quote_date__gte=month_start, quote_date__lte=month_end)

        my_total_sold_month = _sum_net_quote_values(
            my_converted_month.only("total_value_snapshot")
        )
        my_quotes_count_month = my_quotes_month.count()
        my_converted_count_month = my_converted_month.count()
        my_conversion_rate = (
            round(my_converted_count_month / my_quotes_count_month * 100, 1)
            if my_quotes_count_month > 0 else 0
        )
        my_avg_ticket = (
            round(my_total_sold_month / my_converted_count_month, 2)
            if my_converted_count_month > 0 else Decimal("0")
        )

        # Previous month
        prev_month_end = month_start - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        prev_total = _sum_net_quote_values(
            my_quotes.filter(
                status=QuoteStatus.CONVERTED,
                quote_date__gte=prev_month_start,
                quote_date__lte=prev_month_end,
            ).only("total_value_snapshot")
        )

        # Goal — source of truth is user.individual_target_value set in admin
        goal_target = user.individual_target_value or Decimal("0")
        goal_pct = round(float(my_total_sold_month) / float(goal_target) * 100, 1) if goal_target > 0 else 0

        # Avg discount
        avg_discount = (
            my_converted_month.aggregate(avg=Avg("discount_percent"))["avg"] or 0
        )

        # Pending quotes
        pending_quotes = Quote.objects.filter(status=QuoteStatus.DRAFT)
        if not is_staff_or_admin:
            pending_quotes = pending_quotes.filter(seller=user)
        pending_quotes = (
            pending_quotes
            .select_related("customer", "seller")
            .order_by("-quote_date")[:5]
        )

        # Upcoming deliveries
        upcoming_deliveries = CalendarEvent.objects.filter(
            event_type="DELIVERY",
            status=EventStatus.PENDING,
            event_date__gte=today,
            event_date__lte=today + timedelta(days=7),
        )
        if not is_staff_or_admin:
            upcoming_deliveries = upcoming_deliveries.filter(assigned_to=user)
        upcoming_deliveries = upcoming_deliveries.order_by("event_date")[:5]

        # Overdue
        overdue_events = CalendarEvent.objects.filter(
            status=EventStatus.PENDING,
            event_date__lt=today,
        )
        if not is_staff_or_admin:
            overdue_events = overdue_events.filter(assigned_to=user)
        overdue_events = overdue_events.order_by("event_date")[:5]

        # Notifications
        unread_count = Notification.objects.filter(recipient=user, read=False).count()

        # Pending orders count (Finance / Admin banner)
        pending_orders_count = 0
        if user.role in (Role.FINANCE, Role.ADMIN) or user.is_superuser:
            pending_orders_count = Order.objects.filter(
                status=OrderStatus.PENDING,
                is_total_conference=True,
            ).count()

        # Monthly chart (6 months) — net values
        month_starts = _last_n_month_starts(today, n=6)
        series_start = month_starts[0]
        personal_month_quotes = Quote.objects.filter(
            status=QuoteStatus.CONVERTED,
            seller=user,
            quote_date__gte=series_start,
            quote_date__lte=today,
        ).only("quote_date", "total_value_snapshot")
        chart_labels, chart_values, _ = _build_net_month_series_from_quotes(personal_month_quotes, month_starts)

        # Personal status breakdown (for hero pie)
        my_status_data = (
            my_quotes_month.values("status").annotate(count=Count("id"))
        )
        my_status_labels = [QuoteStatus(d["status"]).label for d in my_status_data]
        my_status_values = [d["count"] for d in my_status_data]

        # Team stats (admin)
        team_total_sold_month = Decimal("0")
        team_quotes_month = 0
        team_conversion_rate = 0
        seller_ranking = []
        collective_goal = None
        collective_goal_pct = 0
        bi_team_chart_labels = bi_team_chart_values = bi_team_chart_counts = []
        bi_status_labels = bi_status_values = []
        bi_prod_labels = bi_prod_values = []
        bi_seller_labels = bi_seller_values = bi_seller_counts = []
        bi_disc_labels = bi_disc_values = []
        bi_top_products = []
        if is_admin:
            team_quotes_month = Quote.objects.filter(
                quote_date__gte=month_start,
                quote_date__lte=month_end,
            ).count()
            _team_conv_qs = list(
                Quote.objects.filter(
                    status=QuoteStatus.CONVERTED,
                    quote_date__gte=month_start,
                    quote_date__lte=month_end,
                ).only("seller_id", "total_value_snapshot")
                .select_related("seller")
            )
            team_converted_month = len(_team_conv_qs)
            team_conversion_rate = (
                round(team_converted_month / team_quotes_month * 100, 1)
                if team_quotes_month > 0 else 0
            )
            _buckets: dict = defaultdict(lambda: {"total": Decimal("0"), "count": 0, "username": ""})
            for _q in _team_conv_qs:
                _buckets[_q.seller_id]["total"] += _net_quote_value(_q)
                _buckets[_q.seller_id]["count"] += 1
                _buckets[_q.seller_id]["username"] = _q.seller.username
            team_total_sold_month = sum((_b["total"] for _b in _buckets.values()), Decimal("0"))
            seller_ranking = sorted(
                [{"seller__username": _b["username"], "total": _b["total"], "count": _b["count"]}
                 for _b in _buckets.values()],
                key=lambda x: x["total"], reverse=True,
            )[:10]
            _collective_goal_sum = SalesGoal.objects.filter(
                goal_type=GoalType.INDIVIDUAL,
                period_start__lte=today,
                period_end__gte=today,
                seller__role=Role.SELLER,
            ).aggregate(total=Sum("target_value"))["total"] or Decimal("0")
            collective_goal = _collective_goal_sum if _collective_goal_sum > 0 else None
            if collective_goal:
                collective_goal_pct = round(
                    float(team_total_sold_month) / float(collective_goal) * 100, 1
                )

            # ── BI: Team monthly evolution (last 6 months) — net values ──
            bi_team_all_quotes = Quote.objects.filter(
                status=QuoteStatus.CONVERTED,
                quote_date__gte=series_start,
                quote_date__lte=today,
            ).only("quote_date", "total_value_snapshot")
            bi_team_chart_labels, bi_team_chart_values, bi_team_chart_counts = _build_net_month_series_from_quotes(
                bi_team_all_quotes, month_starts,
            )

            # ── BI: Quote status breakdown ──
            bi_status_data = (
                Quote.objects.filter(quote_date__gte=month_start, quote_date__lte=month_end)
                .values("status")
                .annotate(count=Count("id"))
            )
            bi_status_labels = [QuoteStatus(d["status"]).label for d in bi_status_data]
            bi_status_values = [d["count"] for d in bi_status_data]

            # ── BI: Top 10 products by revenue ──
            bi_top_products = list(
                QuoteItem.objects.filter(
                    quote__status=QuoteStatus.CONVERTED,
                    quote__quote_date__gte=month_start,
                )
                .values("product_name")
                .annotate(
                    total_revenue=Sum(F("quantity") * F("unit_value")),
                    total_qty=Sum("quantity"),
                )
                .order_by("-total_revenue")[:10]
            )
            bi_prod_labels = [p["product_name"][:25] for p in bi_top_products]
            bi_prod_values = [float(p["total_revenue"] or 0) for p in bi_top_products]

            # ── BI: Seller comparison (bar chart) ──
            bi_seller_labels = [s["seller__username"] for s in seller_ranking]
            bi_seller_values = [float(s["total"] or 0) for s in seller_ranking]
            bi_seller_counts = [s["count"] for s in seller_ranking]

            # ── BI: Avg discount per seller ──
            bi_discount_data = list(
                Quote.objects.filter(
                    status=QuoteStatus.CONVERTED,
                    quote_date__gte=month_start,
                    quote_date__lte=month_end,
                    discount_percent__gt=0,
                )
                .values("seller__username")
                .annotate(avg_disc=Avg("discount_percent"))
                .order_by("-avg_disc")[:10]
            )
            bi_disc_labels = [d["seller__username"] for d in bi_discount_data]
            bi_disc_values = [round(float(d["avg_disc"]), 1) for d in bi_discount_data]

        context = {
            "today": today,
            "is_admin": is_admin,
            "is_staff_or_admin": is_staff_or_admin,
            "my_total_sold_month": my_total_sold_month,
            "my_quotes_count_month": my_quotes_count_month,
            "my_converted_count_month": my_converted_count_month,
            "my_conversion_rate": my_conversion_rate,
            "my_avg_ticket": my_avg_ticket,
            "prev_total": prev_total,
            "avg_discount": round(avg_discount, 1),
            "goal_target": goal_target,
            "goal_pct": min(goal_pct, 100),
            "goal_pct_raw": goal_pct,
            "pending_quotes": pending_quotes,
            "upcoming_deliveries": upcoming_deliveries,
            "overdue_events": overdue_events,
            "unread_count": unread_count,
            "pending_orders_count": pending_orders_count,
            "chart_labels_json": json.dumps(chart_labels),
            "chart_values_json": json.dumps(chart_values),
            "my_status_labels_json": json.dumps(my_status_labels),
            "my_status_values_json": json.dumps(my_status_values),
            "team_total_sold_month": team_total_sold_month,
            "team_quotes_month": team_quotes_month,
            "team_conversion_rate": team_conversion_rate,
            "seller_ranking": seller_ranking,
            "collective_goal": collective_goal,
            "collective_goal_pct": min(collective_goal_pct, 100),
            # BI Charts (admin only)
            "bi_team_chart_labels_json": json.dumps(bi_team_chart_labels),
            "bi_team_chart_values_json": json.dumps(bi_team_chart_values),
            "bi_team_chart_counts_json": json.dumps(bi_team_chart_counts),
            "bi_status_labels_json": json.dumps(bi_status_labels),
            "bi_status_values_json": json.dumps(bi_status_values),
            "bi_prod_labels_json": json.dumps(bi_prod_labels),
            "bi_prod_values_json": json.dumps(bi_prod_values),
            "bi_seller_labels_json": json.dumps(bi_seller_labels),
            "bi_seller_values_json": json.dumps(bi_seller_values),
            "bi_seller_counts_json": json.dumps(bi_seller_counts),
            "bi_disc_labels_json": json.dumps(bi_disc_labels),
            "bi_disc_values_json": json.dumps(bi_disc_values),
            "bi_top_products": bi_top_products,
        }
    return render(request, "core/index.html", context)


# ──────────────────────────────────────────────────────────────────────
# Dashboard with Metrics
# ──────────────────────────────────────────────────────────────────────
@login_required
def dashboard(request):
    user = request.user
    today = timezone.localdate()
    month_start, month_end = _month_bounds(today)
    is_admin = user.role == Role.ADMIN or user.is_superuser
    is_staff_or_admin = is_admin

    # ── Personal Stats ──
    my_quotes = Quote.objects.filter(seller=user)
    my_quotes_month = my_quotes.filter(quote_date__gte=month_start, quote_date__lte=month_end)
    my_converted = my_quotes.filter(status=QuoteStatus.CONVERTED)
    my_converted_month = my_converted.filter(quote_date__gte=month_start, quote_date__lte=month_end)

    my_total_sold_month = _sum_net_quote_values(
        my_converted_month.only("total_value_snapshot")
    )
    my_quotes_count_month = my_quotes_month.count()
    my_converted_count_month = my_converted_month.count()
    my_conversion_rate = (
        round(my_converted_count_month / my_quotes_count_month * 100, 1)
        if my_quotes_count_month > 0 else 0
    )
    my_avg_ticket = (
        round(my_total_sold_month / my_converted_count_month, 2)
        if my_converted_count_month > 0 else Decimal("0")
    )

    # ── Previous month comparison ──
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    prev_converted = my_quotes.filter(
        status=QuoteStatus.CONVERTED,
        quote_date__gte=prev_month_start,
        quote_date__lte=prev_month_end,
    )
    prev_total = _sum_net_quote_values(
        prev_converted.only("total_value_snapshot")
    )

    # ── My Goal — source of truth is user.individual_target_value set in admin ──
    goal_target = user.individual_target_value or Decimal("0")
    goal_pct = round(float(my_total_sold_month) / float(goal_target) * 100, 1) if goal_target > 0 else 0

    # ── Team Stats (admin) ──
    team_total_sold_month = Decimal("0")
    team_quotes_month = 0
    team_converted_month = 0
    team_conversion_rate = 0
    seller_ranking = []
    collective_goal = None
    collective_goal_pct = 0

    if is_admin:
        team_quotes_month = Quote.objects.filter(
            quote_date__gte=month_start,
            quote_date__lte=month_end,
        ).count()
        _team_conv_qs = list(
            Quote.objects.filter(
                status=QuoteStatus.CONVERTED,
                quote_date__gte=month_start,
                quote_date__lte=month_end,
            ).only("seller_id", "total_value_snapshot")
            .select_related("seller")
        )
        team_converted_month = len(_team_conv_qs)
        team_conversion_rate = (
            round(team_converted_month / team_quotes_month * 100, 1)
            if team_quotes_month > 0 else 0
        )
        _buckets: dict = defaultdict(lambda: {"total": Decimal("0"), "count": 0, "username": ""})
        for _q in _team_conv_qs:
            _buckets[_q.seller_id]["total"] += _net_quote_value(_q)
            _buckets[_q.seller_id]["count"] += 1
            _buckets[_q.seller_id]["username"] = _q.seller.username
        team_total_sold_month = sum((_b["total"] for _b in _buckets.values()), Decimal("0"))

        # Ranking — net values
        seller_ranking = sorted(
            [{"seller__username": _b["username"], "total": _b["total"], "count": _b["count"]}
             for _b in _buckets.values()],
            key=lambda x: x["total"], reverse=True,
        )[:10]

        # Collective goal = sum of individual seller goals
        _collective_goal_sum = SalesGoal.objects.filter(
            goal_type=GoalType.INDIVIDUAL,
            period_start__lte=today,
            period_end__gte=today,
            seller__role=Role.SELLER,
        ).aggregate(total=Sum("target_value"))["total"] or Decimal("0")
        collective_goal = _collective_goal_sum if _collective_goal_sum > 0 else None
        if collective_goal:
            collective_goal_pct = round(
                float(team_total_sold_month) / float(collective_goal) * 100, 1
            )

    # ── Monthly evolution (last 6 months) — net values ──
    month_starts = _last_n_month_starts(today, n=6)
    series_start = month_starts[0]
    personal_month_quotes = Quote.objects.filter(
        status=QuoteStatus.CONVERTED,
        seller=user,
        quote_date__gte=series_start,
        quote_date__lte=today,
    ).only("quote_date", "total_value_snapshot")
    chart_labels, chart_values, _ = _build_net_month_series_from_quotes(personal_month_quotes, month_starts)

    # ── Pending quotes ──
    pending_quotes = Quote.objects.filter(status=QuoteStatus.DRAFT)
    if not is_staff_or_admin:
        pending_quotes = pending_quotes.filter(seller=user)
    pending_quotes = (
        pending_quotes
        .select_related("customer", "seller")
        .order_by("-quote_date")[:5]
    )

    # ── Upcoming deliveries ──
    upcoming_deliveries = CalendarEvent.objects.filter(
        event_type="DELIVERY",
        status=EventStatus.PENDING,
        event_date__gte=today,
        event_date__lte=today + timedelta(days=7),
    )
    if not is_staff_or_admin:
        upcoming_deliveries = upcoming_deliveries.filter(assigned_to=user)
    upcoming_deliveries = (
        upcoming_deliveries
        .select_related("customer", "assigned_to")
        .order_by("event_date")[:5]
    )

    # ── Overdue events ──
    overdue_events = CalendarEvent.objects.filter(
        status=EventStatus.PENDING,
        event_date__lt=today,
    )
    if not is_staff_or_admin:
        overdue_events = overdue_events.filter(assigned_to=user)
    overdue_events = (
        overdue_events
        .select_related("customer", "assigned_to")
        .order_by("event_date")[:5]
    )

    # ── Notifications count ──
    unread_count = Notification.objects.filter(recipient=user, read=False).count()

    # ── Avg discount ──
    avg_discount = (
        my_converted_month.aggregate(avg=Avg("discount_percent"))["avg"] or 0
    )

    context = {
        "today": today,
        "is_admin": is_admin,
        "is_staff_or_admin": is_staff_or_admin,
        # Personal
        "my_total_sold_month": my_total_sold_month,
        "my_quotes_count_month": my_quotes_count_month,
        "my_converted_count_month": my_converted_count_month,
        "my_conversion_rate": my_conversion_rate,
        "my_avg_ticket": my_avg_ticket,
        "prev_total": prev_total,
        "avg_discount": round(avg_discount, 1),
        # Goal
        "goal_target": goal_target,
        "goal_pct": min(goal_pct, 100),
        "goal_pct_raw": goal_pct,
        # Team
        "team_total_sold_month": team_total_sold_month,
        "team_quotes_month": team_quotes_month,
        "team_converted_month": team_converted_month,
        "team_conversion_rate": team_conversion_rate,
        "seller_ranking": seller_ranking,
        "collective_goal": collective_goal,
        "collective_goal_pct": min(collective_goal_pct, 100),
        # Chart
        "chart_labels_json": json.dumps(chart_labels),
        "chart_values_json": json.dumps(chart_values),
        # Lists
        "pending_quotes": pending_quotes,
        "upcoming_deliveries": upcoming_deliveries,
        "overdue_events": overdue_events,
        "unread_count": unread_count,
    }
    return render(request, "core/dashboard.html", context)


# ──────────────────────────────────────────────────────────────────────
# Customer APIs (existing)
# ──────────────────────────────────────────────────────────────────────
@login_required
def search_customer(request):
    document = request.GET.get("document", "").strip()
    if not document:
        return JsonResponse({"found": False})
    customer = (
        Customer.objects.filter(cpf=document).first()
        or Customer.objects.filter(cnpj=document).first()
    )
    if customer:
        return JsonResponse({
            "found": True, "id": customer.id, "name": customer.name,
            "cpf": customer.cpf, "cnpj": customer.cnpj,
            "phone": customer.phone, "email": customer.email,
        })
    return JsonResponse({"found": False})

@login_required
@require_http_methods(["POST"])
def create_customer(request):
    try:
        data = json.loads(request.body)
        name = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip()
        if not name:
            return JsonResponse({"success": False, "error": "Nome é obrigatório."}, status=400)
        if not phone:
            return JsonResponse({"success": False, "error": "Celular é obrigatório."}, status=400)
        customer = Customer(
            name=name,
            cpf=data.get("cpf", ""),
            cnpj=data.get("cnpj", ""),
            phone=phone,
            email=data.get("email", ""),
        )
        customer.full_clean()  # validates CPF/CNPJ checksums if provided
        customer.save()
        return JsonResponse({"success": True, "customer": {"id": customer.id, "name": str(customer)}})
    except ValidationError as e:
        msgs = []
        if hasattr(e, 'message_dict'):
            for field, errors in e.message_dict.items():
                msgs.extend(errors)
        else:
            msgs = list(e.messages)
        return JsonResponse({"success": False, "error": " ".join(msgs)}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@login_required
def search_customer_by_name(request):
    query = request.GET.get("query", "").strip()
    if not query or len(query) < 2:
        return JsonResponse({"results": []})
    customers = Customer.objects.filter(name__icontains=query)[:3]
    results = [
        {"id": c.id, "name": c.name, "display": str(c), "cpf": c.cpf or "", "cnpj": c.cnpj or ""}
        for c in customers
    ]
    return JsonResponse({"results": results})


@login_required
def get_shipping_company_payment_methods(request, company_id):
    try:
        company = ShippingCompany.objects.get(id=company_id, is_active=True)
        return JsonResponse({"success": True, "payment_methods": company.payment_methods or ""})
    except ShippingCompany.DoesNotExist:
        return JsonResponse({"success": False, "payment_methods": ""}, status=404)


@login_required
def search_shipping_company(request):
    query = request.GET.get("query", "").strip()
    if not query or len(query) < 2:
        return JsonResponse({"results": []})
    companies = ShippingCompany.objects.filter(name__icontains=query, is_active=True)[:5]
    results = [{"id": c.id, "name": c.name, "cnpj": c.cnpj or ""} for c in companies]
    return JsonResponse({"results": results})


@login_required
@require_http_methods(["POST"])
def create_shipping_company(request):
    try:
        data = json.loads(request.body)
        name = (data.get("name") or "").strip()
        cnpj = (data.get("cnpj") or "").strip().replace(".", "").replace("/", "").replace("-", "")
        email = (data.get("email") or "").strip()
        if not name:
            return JsonResponse({"success": False, "error": "Nome é obrigatório."}, status=400)
        if not cnpj:
            return JsonResponse({"success": False, "error": "CNPJ é obrigatório."}, status=400)
        if not email:
            return JsonResponse({"success": False, "error": "E-mail é obrigatório."}, status=400)
        company = ShippingCompany(name=name, cnpj=cnpj, email=email)
        company.full_clean()
        company.save()
        return JsonResponse({"success": True, "company": {"id": company.id, "name": str(company)}})
    except ValidationError as e:
        msgs = []
        if hasattr(e, 'message_dict'):
            for field, errors in e.message_dict.items():
                msgs.extend(errors)
        else:
            msgs = list(e.messages)
        return JsonResponse({"success": False, "error": " ".join(msgs)}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


# ──────────────────────────────────────────────────────────────────────
# Architect Search / Create
# ──────────────────────────────────────────────────────────────────────
@login_required
def search_architect(request):
    query = request.GET.get("query", "").strip()
    if not query or len(query) < 2:
        return JsonResponse({"results": []})
    from .models import Architect
    architects = Architect.objects.filter(name__icontains=query)[:5]
    results = [{"id": a.id, "name": a.name, "pix": a.pix} for a in architects]
    return JsonResponse({"results": results})


@login_required
@require_http_methods(["POST"])
def create_architect(request):
    import json
    try:
        data = json.loads(request.body)
        name = (data.get("name") or "").strip()
        pix = (data.get("pix") or "").strip()
        if not name:
            return JsonResponse({"success": False, "error": "Nome é obrigatório."}, status=400)
        from .models import Architect
        architect = Architect.objects.create(name=name, pix=pix)
        return JsonResponse({"success": True, "architect": {"id": architect.id, "name": architect.name, "pix": architect.pix}})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


# ──────────────────────────────────────────────────────────────────────
# Global Search
# ──────────────────────────────────────────────────────────────────────
@login_required
def global_search(request):
    q = request.GET.get("q", "").strip()
    if not q or len(q) < 2:
        return JsonResponse({"results": []})

    results = []

    # Customers
    for c in Customer.objects.filter(Q(name__icontains=q) | Q(cpf__icontains=q) | Q(cnpj__icontains=q))[:3]:
        results.append({"type": "Cliente", "title": str(c), "url": ""})

    # Quotes
    for qq in Quote.objects.filter(Q(number__icontains=q) | Q(customer__name__icontains=q)).select_related("customer")[:3]:
        results.append({
            "type": "Orçamento",
            "title": f"{qq.number} – {qq.customer.name}",
            "url": f"/sales/quotes/{qq.id}/",
        })

    # Orders
    for o in Order.objects.filter(Q(number__icontains=q)).select_related("quote")[:3]:
        results.append({
            "type": "Pedido",
            "title": f"OC {o.number}",
            "url": f"/sales/orders/{o.id}/",
        })

    return JsonResponse({"results": results})


# ──────────────────────────────────────────────────────────────────────
# Notifications
# ──────────────────────────────────────────────────────────────────────
@login_required
def notifications_list(request):
    notifications = Notification.objects.filter(recipient=request.user).order_by("-created_at")[:50]
    return render(request, "core/notifications.html", {"notifications": notifications})


@login_required
def notifications_api(request):
    """Badge count + unread list for navbar dropdown."""
    qs = Notification.objects.filter(recipient=request.user, read=False).order_by("-created_at")[:10]
    data = {
        "unread_count": Notification.objects.filter(recipient=request.user, read=False).count(),
        "items": [
            {
                "id": n.id,
                "type": n.notification_type,
                "title": n.title,
                "message": n.message[:100],
                "url": n.url,
                "created_at": n.created_at.strftime("%d/%m %H:%M"),
            }
            for n in qs
        ],
    }
    return JsonResponse(data)


@login_required
@require_http_methods(["POST"])
def notification_mark_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    n.mark_as_read()
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["POST"])
def notification_mark_all_read(request):
    Notification.objects.filter(recipient=request.user, read=False).update(
        read=True, read_at=timezone.now()
    )
    return JsonResponse({"ok": True})


# ──────────────────────────────────────────────────────────────────────
# Communication History
# ──────────────────────────────────────────────────────────────────────
@login_required
@require_http_methods(["POST"])
def add_communication(request):
    customer_id = request.POST.get("customer_id")
    quote_id = request.POST.get("quote_id") or None
    customer = get_object_or_404(Customer, pk=customer_id)

    CommunicationHistory.objects.create(
        customer=customer,
        quote_id=quote_id,
        channel=request.POST.get("channel", "OTHER"),
        summary=request.POST.get("summary", ""),
        next_steps=request.POST.get("next_steps", ""),
        created_by=request.user,
    )
    messages.success(request, "Comunicação registrada.")

    redirect_url = request.POST.get("redirect_url", "/dashboard/")
    return redirect(redirect_url)


# ──────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────
def _is_admin_user(user):
    """True for ADMIN or superuser."""
    return user.is_superuser or user.role == Role.ADMIN


@login_required
def reports_hub(request):
    if not _is_admin_user(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("core:index")
    return render(request, "core/reports_hub.html")


@login_required
def report_sales(request):
    if not _is_admin_user(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("core:index")
    today = timezone.localdate()
    date_from = request.GET.get("date_from", str(today.replace(day=1)))
    date_to = request.GET.get("date_to", str(today))
    seller_id = request.GET.get("seller", "")

    qs = Quote.objects.filter(
        status=QuoteStatus.CONVERTED,
        quote_date__gte=date_from,
        quote_date__lte=date_to,
    ).select_related("customer", "seller")

    if seller_id:
        qs = qs.filter(seller_id=seller_id)

    total = qs.aggregate(total=Sum("total_value_snapshot"))["total"] or 0
    count = qs.count()
    avg_value = round(total / count, 2) if count > 0 else 0

    sellers = User.objects.filter(is_active=True).order_by("username")

    return render(request, "core/report_sales.html", {
        "quotes": qs.order_by("-quote_date"),
        "total": total,
        "count": count,
        "avg_value": avg_value,
        "date_from": date_from,
        "date_to": date_to,
        "seller_id": seller_id,
        "sellers": sellers,
    })


@login_required
def report_commissions(request):
    if not _is_admin_user(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("core:index")
    today = timezone.localdate()
    date_from = request.GET.get("date_from", str(today.replace(day=1)))
    date_to = request.GET.get("date_to", str(today))

    from core.models import SalesMarginConfig
    TOTAL_MARGIN, MIN_COMM, MAX_COMM = SalesMarginConfig.get_config()

    qs = Quote.objects.filter(
        status=QuoteStatus.CONVERTED,
        quote_date__gte=date_from,
        quote_date__lte=date_to,
    ).select_related("seller")

    MAX_DISC = 30.0

    def _per_quote_commission_pct(payment_type, discount, fee, installments):
        """Comissão estimada por orçamento — espelha a lógica de _run_simulation.

        PIX/CASH:          clamp(12 - desc, 2, 5)
        Débito:            4% fixo
        Crédito/Boleto 1x-6x: 3% fixo
        Crédito/Boleto 7x+:   clamp(12 - taxa - desc, 2, 4)
        Outros/Cheque:     clamp(12 - taxa - desc, 2, 4)
        """
        disc = float(discount or 0)
        fee_pct = float(fee or 0)
        inst = int(installments or 1)
        if payment_type in ('PIX', 'CASH'):
            mld = 12.0 - disc
            return round(max(2.0, min(mld, 5.0)), 2)
        elif payment_type == 'DEBIT_CARD':
            return 4.0
        elif payment_type in ('CREDIT_CARD', 'BOLETO'):
            if inst >= 7:
                mld = 12.0 - fee_pct - disc
                return round(max(2.0, min(mld, 4.0)), 2)
            return 3.0
        else:
            # Cheque, sem forma, etc.
            mld = 12.0 - fee_pct - disc
            return round(max(2.0, min(mld, 4.0)), 2)

    from collections import defaultdict
    seller_totals = defaultdict(lambda: {"total_sold": 0.0, "commission": 0.0, "count": 0, "disc_sum": 0.0, "seller_name": ""})

    # Pre-fetch commission splits for all quotes in the queryset
    from sales.models import QuoteCommissionSplit
    split_map: dict[int, list] = {}  # quote_id -> list of (user_id, username)
    for sp in (
        QuoteCommissionSplit.objects
        .filter(quote__in=qs)
        .prefetch_related("users")
    ):
        users = list(sp.users.all())
        if users:
            split_map[sp.quote_id] = [(u.pk, u.get_full_name() or u.username) for u in users]

    for q in qs.select_related("seller"):
        val = float(q.total_value_snapshot or 0)
        comm_pct = _per_quote_commission_pct(q.payment_type, q.discount_percent, q.payment_fee_percent, q.payment_installments)
        comm_value = val * comm_pct / 100
        disc = float(q.discount_percent or 0)

        recipients = split_map.get(q.pk)
        if recipients:
            share = comm_value / len(recipients)
            share_val = val / len(recipients)
            for uid, uname in recipients:
                seller_totals[uid]["seller_name"] = uname
                seller_totals[uid]["total_sold"] += share_val
                seller_totals[uid]["commission"] += share
                seller_totals[uid]["count"] += 1
                seller_totals[uid]["disc_sum"] += disc
        else:
            sid = q.seller_id
            seller_totals[sid]["seller_name"] = q.seller.get_full_name() or q.seller.username
            seller_totals[sid]["total_sold"] += val
            seller_totals[sid]["commission"] += comm_value
            seller_totals[sid]["count"] += 1
            seller_totals[sid]["disc_sum"] += disc

    commissions = []
    for sid, data in sorted(seller_totals.items(), key=lambda x: -x[1]["total_sold"]):
        count = data["count"]
        avg_disc = data["disc_sum"] / count if count else 0
        total_sold = data["total_sold"]
        est_commission = data["commission"]
        comm_pct = est_commission / total_sold * 100 if total_sold else 0
        commissions.append({
            "seller": data["seller_name"],
            "total_sold": round(total_sold, 2),
            "count": count,
            "avg_discount": round(avg_disc, 1),
            "comm_pct": round(comm_pct, 1),
            "est_commission": round(est_commission, 2),
        })

    return render(request, "core/report_commissions.html", {
        "commissions": commissions,
        "date_from": date_from,
        "date_to": date_to,
    })


@login_required
def report_discounts(request):
    if not _is_admin_user(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("core:index")
    today = timezone.localdate()
    date_from = request.GET.get("date_from", str(today.replace(day=1)))
    date_to = request.GET.get("date_to", str(today))

    qs = Quote.objects.filter(
        quote_date__gte=date_from,
        quote_date__lte=date_to,
        discount_percent__gt=0,
    ).select_related("seller", "customer", "discount_authorized_by").order_by("-discount_percent")

    avg = qs.aggregate(avg=Avg("discount_percent"))["avg"] or 0
    authorized_count = qs.filter(discount_authorized_by__isnull=False).count()

    return render(request, "core/report_discounts.html", {
        "quotes": qs,
        "avg_discount": round(avg, 1),
        "authorized_count": authorized_count,
        "date_from": date_from,
        "date_to": date_to,
    })


@login_required
def report_products(request):
    if not _is_admin_user(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("core:index")
    today = timezone.localdate()
    date_from = request.GET.get("date_from", str(today.replace(day=1)))
    date_to = request.GET.get("date_to", str(today))

    items = (
        QuoteItem.objects.filter(
            quote__status=QuoteStatus.CONVERTED,
            quote__quote_date__gte=date_from,
            quote__quote_date__lte=date_to,
        )
        .values("product_name")
        .annotate(
            qty=Sum("quantity"),
            total_value=Sum(F("quantity") * F("unit_value")),
        )
        .order_by("-total_value")[:50]
    )

    return render(request, "core/report_products.html", {
        "items": items,
        "date_from": date_from,
        "date_to": date_to,
    })


@login_required
def report_csv_export(request):
    if not _is_admin_user(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("core:index")
    import csv
    today = timezone.localdate()
    date_from = request.GET.get("date_from", str(today.replace(day=1)))
    date_to = request.GET.get("date_to", str(today))

    qs = Quote.objects.filter(
        status=QuoteStatus.CONVERTED,
        quote_date__gte=date_from,
        quote_date__lte=date_to,
    ).select_related("customer", "seller").order_by("-quote_date")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="vendas_{date_from}_{date_to}.csv"'
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Número", "Data", "Cliente", "Vendedor", "Desconto %", "Total R$"])

    for q in qs:
        writer.writerow([
            q.number,
            q.quote_date.strftime("%d/%m/%Y"),
            q.customer.name,
            q.seller.username,
            f"{q.discount_percent}",
            f"{q.total_value_snapshot}",
        ])

    return response


# ──────────────────────────────────────────────────────────────────────
# Audit Log View
# ──────────────────────────────────────────────────────────────────────
@login_required
def audit_log_list(request):
    user = request.user
    is_admin = user.role == Role.ADMIN or user.is_superuser
    if not is_admin:
        messages.error(request, "Acesso negado.")
        return redirect("core:dashboard")

    action_filter = request.GET.get("action", "")
    qs = AuditLog.objects.select_related("user").all()
    if action_filter:
        qs = qs.filter(action=action_filter)

    return render(request, "core/audit_log.html", {
        "logs": qs[:200],
        "actions": AuditAction.choices,
        "action_filter": action_filter,
    })


# ──────────────────────────────────────────────────────────────────────
# Sales Goals Management
# ──────────────────────────────────────────────────────────────────────
@login_required
def goals_list(request):
    user = request.user
    is_admin = user.role == Role.ADMIN or user.is_superuser

    if is_admin:
        goals = SalesGoal.objects.select_related("seller").all()
    else:
        goals = SalesGoal.objects.filter(Q(seller=user) | Q(goal_type=GoalType.COLLECTIVE))

    sellers = User.objects.filter(is_active=True, role=Role.SELLER).order_by("username")

    return render(request, "core/goals_list.html", {
        "goals": goals,
        "is_admin": is_admin,
        "sellers": sellers,
    })


@login_required
@require_http_methods(["POST"])
def goal_create(request):
    user = request.user
    is_admin = user.role == Role.ADMIN or user.is_superuser
    if not is_admin:
        messages.error(request, "Apenas administradores podem criar metas.")
        return redirect("core:goals_list")

    goal_type = request.POST.get("goal_type", GoalType.INDIVIDUAL)
    seller_id = request.POST.get("seller_id") or None

    try:
        target_value = Decimal(request.POST.get("target_value", "0") or "0")
        target_quantity = int(request.POST.get("target_quantity", "0") or "0")
    except (ValueError, ArithmeticError):
        messages.error(request, "Valores de meta inválidos.")
        return redirect("core:goals_list")

    period_start_str = request.POST.get("period_start")
    period_end_str = request.POST.get("period_end")
    if not period_start_str or not period_end_str:
        messages.error(request, "Período da meta é obrigatório.")
        return redirect("core:goals_list")

    try:
        requested_start = date_type.fromisoformat(period_start_str)
        requested_end = date_type.fromisoformat(period_end_str)
    except ValueError:
        messages.error(request, "Datas da meta inválidas.")
        return redirect("core:goals_list")

    if requested_start > requested_end:
        messages.error(request, "A data inicial não pode ser maior que a data final.")
        return redirect("core:goals_list")

    if goal_type == GoalType.INDIVIDUAL:
        if not seller_id:
            messages.error(request, "Selecione um vendedor para a meta individual.")
            return redirect("core:goals_list")

        month_start, month_end = _month_bounds(requested_start)
        SalesGoal.objects.update_or_create(
            goal_type=GoalType.INDIVIDUAL,
            period=GoalPeriod.MONTHLY,
            seller_id=seller_id,
            period_start=month_start,
            period_end=month_end,
            defaults={
                "target_value": target_value,
                "target_quantity": target_quantity,
            },
        )
        messages.success(request, "Meta individual mensal salva.")
        return redirect("core:goals_list")

    SalesGoal.objects.create(
        goal_type=GoalType.COLLECTIVE,
        seller=None,
        period=request.POST.get("period", GoalPeriod.MONTHLY),
        period_start=requested_start,
        period_end=requested_end,
        target_value=target_value,
        target_quantity=target_quantity,
    )
    messages.success(request, "Meta criada.")
    return redirect("core:goals_list")
