from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import date as date_type, time as time_type, timedelta
from decimal import Decimal
from io import BytesIO
from urllib.parse import quote as url_quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db import models
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# Motor de Margem — vive em sales/margin.py para poder ser reaproveitado fora das
# views (fechamento de venda, migrações, relatórios). Reexportado aqui porque os
# testes e o código existente ainda o importam de `sales.views`.
from sales.margin import (  # noqa: F401
    _run_simulation,
    _build_simulation_context,
    clear_quote_commission,
    persist_quote_commission,
)

logger = logging.getLogger(__name__)

def _is_admin(user):
    from accounts.models import Role
    return user.is_superuser or user.role == Role.ADMIN

def _is_finance(user):
    from accounts.models import Role
    return (not user.is_superuser) and user.role == Role.FINANCE

def _is_staff_or_admin(user):
    from accounts.models import Role
    return user.is_superuser or user.role == Role.ADMIN

def _can_access_all_quotes(user):
    """Admin, Finance e superuser podem ver todos os orçamentos.
    Finance precisa disso para converter orçamentos em pedidos.
    """
    from accounts.models import Role
    return user.is_superuser or user.role in (Role.ADMIN, Role.FINANCE)

def _is_seller(user):
    from accounts.models import Role
    return user.role == Role.SELLER and not user.is_superuser

def _can_view_all_orders(user):
    return _is_admin(user) or _is_finance(user)

def _can_generate_order_pdf(user):
    return _is_admin(user) or _is_finance(user)

def _can_view_commission(user):
    return not _is_finance(user)


def _build_value_breakdown(quote):
    """Detalhamento dos valores de venda ao cliente para o financeiro.

    Mostra o valor original (subtotal dos produtos), acréscimo, desconto em R$,
    frete, taxa de pagamento e o total final — tudo que o financeiro precisa
    revisar antes de aprovar.
    """
    subtotal = quote.calculate_subtotal()
    markup_pct = quote.price_increase_percent or Decimal("0.00")
    discount_pct = quote.discount_percent or Decimal("0.00")
    markup_amount = subtotal * markup_pct / Decimal("100")
    discount_amount = subtotal * discount_pct / Decimal("100")
    freight = quote.freight_value or Decimal("0.00")
    from .models import FreightResponsible
    freight_to_store = quote.freight_responsible == FreightResponsible.STORE
    payment_fee = quote.calculate_payment_fee_value()
    total_with_discount = quote.calculate_total_with_freight_and_discount()
    rounded_total = quote.calculate_rounded_total()
    from .models import RoundingMode
    breakdown = {
        "subtotal": subtotal,
        "markup_pct": markup_pct,
        "markup_amount": markup_amount,
        "discount_pct": discount_pct,
        "discount_amount": discount_amount,
        "has_discount": discount_pct > 0,
        "has_markup": markup_pct > 0,
        "freight": freight,
        "freight_to_store": freight_to_store,
        "total_with_discount": total_with_discount,
        "payment_fee": payment_fee,
        "payment_fee_pct": quote.payment_fee_percent or Decimal("0.00"),
        "final_total": rounded_total,
        "rounded_total": rounded_total,
        "rounding_diff": rounded_total - total_with_discount,
        "has_rounding": (
            quote.total_override is not None
            or quote.total_rounding_mode != RoundingMode.NONE
            or (quote.total_manual_adjustment or Decimal("0.00")) != Decimal("0.00")
        ),
        "dual_pricing": quote.dual_pricing,
    }
    if quote.dual_pricing:
        # Rótulos explícitos: o total padrão é o de varejo.
        breakdown["subtotal_wholesale"] = quote.calculate_subtotal_wholesale()
        breakdown["rounded_total_wholesale"] = quote.calculate_rounded_total_wholesale()
    return breakdown

def _get_quote_or_403(request, quote_id, **extra_filters):
    from django.http import HttpResponseForbidden
    quote = get_object_or_404(Quote, id=quote_id, **extra_filters)
    if not _can_access_all_quotes(request.user) and quote.seller_id != request.user.id:
        return None, HttpResponseForbidden("Acesso negado.")
    return quote, None

def _get_order_or_403(request, order_id, **extra_filters):
    from django.http import HttpResponseForbidden
    order = get_object_or_404(Order, pk=order_id, **extra_filters)
    # Pedido avulso (sem orçamento) só é visível a quem vê todos os pedidos.
    if not _can_view_all_orders(request.user) and (
        order.quote is None or order.quote.seller_id != request.user.id
    ):
        return None, HttpResponseForbidden("Acesso negado.")
    return order, None

def _safe_content_disposition(filename: str) -> str:
    nfkd = unicodedata.normalize('NFKD', filename)
    ascii_name = nfkd.encode('ascii', 'ignore').decode('ascii')
    ascii_name = re.sub(r'[^\w.\-]', '_', ascii_name)
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{url_quote(filename)}"
    )

def _persist_item_images_from_formset(formset) -> None:
    for item_form in formset.forms:
        if not hasattr(item_form, "cleaned_data"):
            continue
        if item_form.cleaned_data.get("DELETE"):
            continue

        item = item_form.instance
        if not item or not item.pk:
            continue

        uploaded_image = item_form.cleaned_data.get("item_image")
        if not uploaded_image:
            continue

        existing_images = QuoteItemImage.objects.filter(item=item)
        for old_image in existing_images:
            if old_image.image:
                try:
                    old_image.image.delete(save=False)
                except Exception:
                    pass
        existing_images.delete()

        QuoteItemImage.objects.create(item=item, image=uploaded_image)


def _capture_order_item_links(quote) -> dict:
    """Fotografa o vínculo OrderItem→QuoteItem ANTES de salvar a edição.

    Como QuoteItem tem on_delete=SET_NULL sobre OrderItem.quote_item, ao apagar
    um item do orçamento o vínculo é zerado e perdemos a origem. Capturamos antes
    para conseguir remover, depois, apenas os itens de pedido derivados de itens
    que foram removidos — preservando itens adicionados manualmente no pedido
    (que têm quote_item nulo).
    Retorna {order_item_id: quote_item_id} apenas para vínculos não nulos.
    """
    from .models import OrderItem as _OI
    return {
        row["id"]: row["quote_item_id"]
        for row in _OI.objects.filter(order__quote=quote, quote_item_id__isnull=False)
                              .values("id", "quote_item_id")
    }


def _sync_orders_from_quote(quote, original_links: dict) -> None:
    """Re-sincroniza os pedidos de compra já gerados com os itens atuais do
    orçamento (usado ao editar um orçamento já convertido).

    Regras:
    - Preserva dados do pedido (status, prazo, observações) e o custo de compra
      já digitado em itens existentes; só define custo em itens novos.
    - Itens adicionados manualmente ao pedido (quote_item nulo) são preservados.
    - Remove linhas derivadas de itens do orçamento que foram apagados.
    - Reflete alterações de fornecedor: cria pedido para fornecedor novo e
      remove pedido por fornecedor que fica sem itens.
    """
    from collections import defaultdict

    orders = list(quote.orders.all())
    if not orders:
        return

    items = list(quote.items.select_related("supplier").all())
    current_ids = {it.id for it in items}

    # 1) Remove itens de pedido derivados de itens do orçamento que sumiram.
    removed_order_item_ids = [
        oi_id for oi_id, qi_id in original_links.items()
        if qi_id not in current_ids
    ]
    if removed_order_item_ids:
        OrderItem.objects.filter(id__in=removed_order_item_ids).delete()

    by_supplier: dict[int, list] = defaultdict(list)
    for it in items:
        if it.supplier_id is not None:
            by_supplier[it.supplier_id].append(it)

    suppliers_with_order: set[int] = set()

    for order in orders:
        if order.is_total_conference:
            target = items  # o pedido total contém todos os itens
        else:
            suppliers_with_order.add(order.supplier_id)
            target = by_supplier.get(order.supplier_id, [])
        target_ids = {it.id for it in target}

        existing = {
            oi.quote_item_id: oi
            for oi in order.items.all()
            if oi.quote_item_id is not None
        }

        # remove linhas cujo item deixou de pertencer a este pedido
        # (ex.: item mudou de fornecedor e migrou para outro pedido).
        stale_ids = [oi.id for qi_id, oi in existing.items() if qi_id not in target_ids]
        if stale_ids:
            OrderItem.objects.filter(id__in=stale_ids).delete()

        for it in target:
            oi = existing.get(it.id)
            if oi is not None:
                # atualiza dados do produto, preserva custo de compra manual
                if (oi.product_name != it.product_name
                        or oi.description != it.description
                        or oi.quantity != it.quantity):
                    oi.product_name = it.product_name
                    oi.description = it.description
                    oi.quantity = it.quantity
                    oi.save(update_fields=["product_name", "description", "quantity"])
            else:
                OrderItem.objects.create(
                    order=order,
                    product_name=it.product_name,
                    description=it.description,
                    quantity=it.quantity,
                    purchase_unit_cost=it.unit_value,
                    quote_item=it,
                )

        # pedido por fornecedor que ficou vazio → remove
        if not order.is_total_conference and not order.items.exists():
            order.delete()

    # 2) Fornecedores novos (sem pedido ainda) → cria pedido de compra.
    for supplier_id, supplier_items in by_supplier.items():
        if supplier_id in suppliers_with_order:
            continue
        new_order = Order.objects.create(
            number=quote.number,
            quote=quote,
            supplier_id=supplier_id,
            is_total_conference=False,
            status=OrderStatus.PENDING,
            delivery_deadline=None,
        )
        OrderItem.objects.bulk_create([
            OrderItem(
                order=new_order,
                product_name=it.product_name,
                description=it.description,
                quantity=it.quantity,
                purchase_unit_cost=it.unit_value,
                quote_item=it,
            )
            for it in supplier_items
        ])


from .forms import QuoteForm, QuoteItemFormSet, OrderForm, OrderItemFormSet
from .models import (
    Quote,
    QuoteStatus,
    QuoteItem,
    QuoteItemImage,
    Order,
    OrderItem,
    OrderStatus,
    QuoteCommissionSplit,
    FreightResponsible,
    ProposalConfig,
    SaleDocument,
    SaleDocumentType,
    SOLD_STATUSES,
)
from calendar_app.models import (
    CalendarEvent,
    EventStatus,
    EventType,
    Reminder,
)

def generate_next_quote_number() -> str:
    with transaction.atomic():
        last_quote = Quote.objects.select_for_update().order_by("-id").first()
        if not last_quote:
            candidate = 1
        else:
            try:
                if last_quote.number.startswith("ORC-"):
                    candidate = int(last_quote.number.split("-")[1]) + 1
                else:
                    candidate = Quote.objects.count() + 1
            except (ValueError, IndexError):
                candidate = Quote.objects.count() + 1

        while Quote.objects.filter(number=f"ORC-{candidate:04d}").exists():
            candidate += 1

        return f"ORC-{candidate:04d}"

@login_required
def quotes_hub(request: HttpRequest) -> HttpResponse:
    return render(request, 'sales/quotes_hub.html')

@login_required
def payment_method_fees_api(request: HttpRequest) -> JsonResponse:
    from core.models import PaymentTariff, PaymentMethodType
    
    payment_type = request.GET.get('payment_type')
    
    if not payment_type:
        return JsonResponse({'error': 'payment_type required'}, status=400)
    
    max_installments_map = {
        'CASH': 1,
        'PIX': 1,
        'DEBIT_CARD': 1,
        'CREDIT_CARD': 18,
        'CHEQUE': 12,
        'BOLETO': 4,
    }
    
    is_installment = payment_type in ['CREDIT_CARD', 'CHEQUE', 'BOLETO']
    max_installments = max_installments_map.get(payment_type, 1)

    tariffs = PaymentTariff.objects.filter(
        payment_type=PaymentTariff.lookup_type(payment_type)
    ).order_by('installments')

    # Só expõe parcelas com tarifa cadastrada. Preencher as faltantes com 0%
    # oferecia parcelamento sem custo e estourava a margem silenciosamente.
    tariffs_data = [
        {'installments': t.installments, 'fee_percent': str(t.fee_percent)}
        for t in tariffs
        if t.installments <= max_installments
    ]
    max_installments = tariffs_data[-1]['installments'] if tariffs_data else 1

    type_display = dict(PaymentMethodType.choices).get(payment_type, payment_type)
    
    data = {
        'payment_type': payment_type,
        'type_display': type_display,
        'is_installment': is_installment,
        'max_installments': max_installments,
        'tariffs': tariffs_data,
    }
    
    return JsonResponse(data)

@login_required
@require_http_methods(["POST"])
def authorize_discount_api(request: HttpRequest) -> JsonResponse:
    import json
    from django.contrib.auth import authenticate
    from core.ratelimit import client_ip, is_rate_limited, register_failure

    rl_ident = client_ip(request)
    # Só falhas contam: aprovar muitos descontos legítimos não bloqueia o admin.
    if is_rate_limited("authorize_discount", rl_ident, limit=10):
        return JsonResponse({'authorized': False, 'error': 'Muitas tentativas. Aguarde alguns minutos.'}, status=429)

    try:
        data = json.loads(request.body)
        username = data.get('username', '').strip()
        password = data.get('password')
        discount = data.get('discount')

        if not password or discount is None:
            return JsonResponse({'authorized': False, 'error': 'Missing parameters'}, status=400)

        discount_value = float(discount)

        if discount_value <= 15:
            return JsonResponse({'authorized': False, 'error': 'Discount must be > 15%'}, status=400)

        target_username = username if username else request.user.username
        user = authenticate(username=target_username, password=password)

        if user and _is_admin(user):
            return JsonResponse({
                'authorized': True,
                'authorized_by': user.username,
                'discount': discount_value
            })

        register_failure("authorize_discount", rl_ident, window=300)
        return JsonResponse({'authorized': False, 'error': 'Credenciais inválidas'}, status=403)
        
    except (json.JSONDecodeError, ValueError, TypeError):
        return JsonResponse({'authorized': False, 'error': 'Dados inválidos'}, status=400)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('authorize_discount_api error')
        return JsonResponse({'authorized': False, 'error': 'Erro interno'}, status=500)

@login_required
@require_http_methods(["GET"])
def get_architect_commission_api(request: HttpRequest) -> JsonResponse:
    from core.models import ArchitectCommission
    
    try:
        commission = ArchitectCommission.get_commission()
        return JsonResponse({
            'commission_percent': float(commission)
        })
    except Exception:
        logger.exception('get_architect_commission_api error')
        return JsonResponse({'error': 'Erro interno.'}, status=500)

@login_required
def quote_list(request: HttpRequest) -> HttpResponse:
    quotes = Quote.objects.select_related('customer', 'seller').order_by('-created_at')
    if not _can_access_all_quotes(request.user):
        quotes = quotes.filter(seller=request.user)
    
    search_query = request.GET.get('search', '').strip()
    if search_query:
        quotes = quotes.filter(
            models.Q(number__icontains=search_query) |
            models.Q(customer__name__icontains=search_query) |
            models.Q(seller__username__icontains=search_query)
        )
    
    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        quotes = quotes.filter(status=status_filter)
    
    context = {
        'quotes': quotes,
        'search_query': search_query,
        'status_filter': status_filter,
        'is_admin': _is_admin(request.user),
    }
    
    return render(request, 'sales/quote_list.html', context)

@login_required
@require_http_methods(["GET", "POST"])
def quote_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = QuoteForm(request.POST)
        formset = QuoteItemFormSet(request.POST, request.FILES)

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                quote: Quote = form.save(commit=False)
                quote.seller = request.user
                quote.status = QuoteStatus.DRAFT
                if not quote.quote_date:
                    quote.quote_date = timezone.localdate()
                quote.number = generate_next_quote_number()
                
                if quote.discount_percent is None:
                    quote.discount_percent = Decimal("0.0")
                if quote.payment_installments is None:
                    quote.payment_installments = 1
                if quote.payment_fee_percent is None:
                    quote.payment_fee_percent = Decimal("0.0")
                
                if quote.freight_responsible == FreightResponsible.CUSTOMER:
                    quote.freight_value = Decimal("0.00")
                
                discount_percent = quote.discount_percent or Decimal("0")
                if discount_percent > 15:
                    authorized_by_username = request.POST.get('discount_authorized_by')
                    if authorized_by_username:
                        from django.contrib.auth import get_user_model
                        User = get_user_model()
                        try:
                            auth_user = User.objects.get(username=authorized_by_username, is_staff=True)
                            quote.discount_authorized_by = auth_user
                            quote.discount_authorized_at = timezone.now()
                        except User.DoesNotExist:
                            messages.error(request, "Usuário autorizador não encontrado.")
                            return render(request, "sales/quote_form.html", {"form": form, "formset": formset})
                    else:
                        messages.error(request, "Desconto acima de 15% requer autorização.")
                        return render(request, "sales/quote_form.html", {"form": form, "formset": formset})
                
                quote.save()

                formset.instance = quote
                formset.save()
                _persist_item_images_from_formset(formset)

            from core.models import AuditLog, AuditAction
            AuditLog.log(request.user, AuditAction.CREATE_QUOTE,
                         f"Orçamento {quote.number} criado", obj=quote,
                         ip_address=request.META.get('REMOTE_ADDR'))

            messages.success(request, f"Orçamento {quote.number} criado.")
            
            action = request.POST.get('action', 'save')
            if action == 'next_step':
                return redirect("sales:quote_simulate", quote_id=quote.id)
            
            return redirect("sales:quote_detail", quote_id=quote.id)
        else:
            messages.error(request, "Corrija os campos inválidos.")
    else:
        initial_data = {
            'discount_percent': Decimal('0.0'),
            'payment_installments': 1,
            'payment_fee_percent': Decimal('0.0'),
        }
        form = QuoteForm(initial=initial_data)
        formset = QuoteItemFormSet()

    return render(
        request,
        "sales/quote_form.html",
        {"form": form, "formset": formset},
    )

@login_required
@require_http_methods(["GET", "POST"])
def quote_edit(request: HttpRequest, quote_id: int) -> HttpResponse:
    quote = get_object_or_404(Quote, id=quote_id)
    if not _is_staff_or_admin(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    if request.method == "POST":
        form = QuoteForm(request.POST, instance=quote)
        formset = QuoteItemFormSet(request.POST, request.FILES, instance=quote)

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                quote_obj = form.save(commit=False)
                
                if quote_obj.freight_responsible == FreightResponsible.CUSTOMER:
                    quote_obj.freight_value = Decimal("0.00")
                
                discount_percent = quote_obj.discount_percent or Decimal("0")
                if discount_percent > 15:
                    if not quote.discount_authorized_by or quote.discount_percent != discount_percent:
                        authorized_by_username = request.POST.get('discount_authorized_by')
                        if authorized_by_username:
                            from django.contrib.auth import get_user_model
                            User = get_user_model()
                            try:
                                auth_user = User.objects.get(username=authorized_by_username, is_staff=True)
                                quote_obj.discount_authorized_by = auth_user
                                quote_obj.discount_authorized_at = timezone.now()
                            except User.DoesNotExist:
                                messages.error(request, "Usuário autorizador não encontrado.")
                                return render(request, "sales/quote_form.html", {"form": form, "formset": formset, "quote": quote})
                        else:
                            messages.error(request, "Desconto acima de 15% requer autorização.")
                            return render(request, "sales/quote_form.html", {"form": form, "formset": formset, "quote": quote})
                
                # Captura os vínculos pedido→item ANTES de salvar (SET_NULL zera
                # a origem ao apagar itens); usado para sincronizar os pedidos.
                original_links = _capture_order_item_links(quote)

                quote_obj.save()
                formset.save()
                _persist_item_images_from_formset(formset)

                # Orçamento já convertido: propaga as alterações para os pedidos
                # de compra já gerados.
                if quote.orders.exists():
                    _sync_orders_from_quote(quote, original_links)

            from core.models import AuditLog, AuditAction
            AuditLog.log(request.user, AuditAction.EDIT_QUOTE,
                         f"Orçamento {quote.number} editado", obj=quote,
                         ip_address=request.META.get('REMOTE_ADDR'))

            messages.success(request, f"Orçamento {quote.number} atualizado.")
            
            action = request.POST.get('action', 'save')
            if action == 'next_step':
                return redirect("sales:quote_simulate", quote_id=quote.id)
            
            return redirect("sales:quote_detail", quote_id=quote.id)
        else:
            messages.error(request, "Corrija os campos inválidos.")
    else:
        form = QuoteForm(instance=quote)
        formset = QuoteItemFormSet(instance=quote)

    return render(
        request,
        "sales/quote_form.html",
        {"form": form, "formset": formset, "quote": quote},
    )

@login_required
def quote_detail(request: HttpRequest, quote_id: int) -> HttpResponse:
    quote = get_object_or_404(
        Quote.objects
        .select_related("customer", "seller")
        .prefetch_related("items", "items__supplier", "orders", "orders__items"),
        id=quote_id,
    )
    if not _can_access_all_quotes(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    _fin_or_admin = _is_finance(request.user) or _is_admin(request.user)
    total_order = next(
        (o for o in quote.orders.all() if o.is_total_conference), None
    )

    from accounts.models import Role
    from django.contrib.auth import get_user_model
    User = get_user_model()
    sellers_for_split = (
        User.objects.filter(is_active=True, role=Role.SELLER)
        .exclude(pk=quote.seller_id)
        .order_by("first_name", "username")
    )
    existing_split = getattr(quote, "commission_split", None)
    existing_split_ids = (
        set(existing_split.users.values_list("pk", flat=True))
        if existing_split
        else set()
    )

    return render(request, "sales/quote_detail.html", {
        "quote": quote,
        "sellers_for_split": sellers_for_split,
        "existing_split_ids": existing_split_ids,
        "value_breakdown": _build_value_breakdown(quote),
        "today": timezone.localdate(),
        "is_seller": _is_seller(request.user),
        "is_finance": _is_finance(request.user),
        "is_admin": _is_admin(request.user),
        "can_generate_order_pdf": _can_generate_order_pdf(request.user),
        "can_view_supplier_pdf": _is_admin(request.user),
        "can_view_commission": _can_view_commission(request.user) or (quote.seller_id == request.user.id),
        "total_order": total_order,
        "can_approve_order": (
            _fin_or_admin
            and total_order is not None
            and total_order.status == OrderStatus.PENDING
        ),
        "can_conclude_order": (
            _fin_or_admin
            and total_order is not None
            and total_order.status == OrderStatus.ONGOING
        ),
    })

@login_required
@require_http_methods(["POST"])
def quote_set_sale_date(request: HttpRequest, quote_id: int) -> HttpResponse:
    """Edita a data da venda (data de conversão) diretamente no orçamento.

    A data da venda define o mês em que a venda entra nos painéis e comissões.
    Só faz sentido em orçamentos já vendidos (SOLD_STATUSES). Observação: editar
    depois a "Data do Pedido" no pedido re-deriva Quote.sale_date a partir do
    pedido mais antigo (order_edit), então a edição direta aqui é autoritativa
    até a próxima edição de data de um pedido do mesmo orçamento.
    """
    quote, forbidden = _get_quote_or_403(request, quote_id)
    if forbidden:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    if quote.status not in SOLD_STATUSES:
        messages.error(request, "Só é possível editar a data da venda de um orçamento convertido.")
        return redirect("sales:quote_detail", quote_id=quote.id)

    raw = request.POST.get("sale_date", "").strip()
    try:
        new_date = date_type.fromisoformat(raw) if raw else None
    except ValueError:
        new_date = None

    if not new_date:
        messages.error(request, "Data da venda inválida.")
        return redirect("sales:quote_detail", quote_id=quote.id)

    old_date = quote.sale_date
    if new_date != old_date:
        quote.sale_date = new_date
        quote.save(update_fields=["sale_date"])

        from core.models import AuditLog, AuditAction
        AuditLog.log(
            request.user,
            AuditAction.EDIT_VALUES,
            f"Data da venda do orçamento {quote.number} alterada de "
            f"{old_date.strftime('%d/%m/%Y') if old_date else '—'} para "
            f"{new_date.strftime('%d/%m/%Y')}.",
            obj=quote,
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        messages.success(
            request,
            f"Data da venda atualizada para {new_date.strftime('%d/%m/%Y')}.",
        )

    return redirect("sales:quote_detail", quote_id=quote.id)


@login_required
@require_http_methods(["GET", "POST"])
def quote_reminders(request: HttpRequest, quote_id: int) -> HttpResponse:
    quote = get_object_or_404(
        Quote.objects.select_related("customer", "seller"),
        id=quote_id,
    )
    if not _can_access_all_quotes(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    auto_followup_titles = [
        "Follow-up: orçamento sem resposta (3 dias)",
        "Follow-up: orçamento ainda em rascunho (7 dias)",
    ]
    CalendarEvent.objects.filter(
        quote=quote,
        event_type=EventType.QUOTE_FOLLOWUP,
        title__in=auto_followup_titles,
    ).delete()

    if request.method == "POST":
        titles = request.POST.getlist("reminder_title[]")
        dates = request.POST.getlist("reminder_date[]")
        times = request.POST.getlist("reminder_time[]")
        descriptions = request.POST.getlist("reminder_description[]")

        created_count = 0
        invalid_rows: list[int] = []

        total_rows = max(len(titles), len(dates), len(times), len(descriptions))
        for idx in range(total_rows):
            title = (titles[idx] if idx < len(titles) else "").strip()
            date_str = (dates[idx] if idx < len(dates) else "").strip()
            time_str = (times[idx] if idx < len(times) else "").strip()
            description = (descriptions[idx] if idx < len(descriptions) else "").strip()

            if not title and not date_str and not time_str and not description:
                continue

            if not title or not date_str:
                invalid_rows.append(idx + 1)
                continue

            try:
                event_date = date_type.fromisoformat(date_str)
            except ValueError:
                invalid_rows.append(idx + 1)
                continue

            event_time = None
            if time_str:
                try:
                    event_time = time_type.fromisoformat(time_str)
                except ValueError:
                    invalid_rows.append(idx + 1)
                    continue

            event = CalendarEvent.objects.create(
                title=title,
                description=description,
                event_type=EventType.QUOTE_FOLLOWUP,
                status=EventStatus.PENDING,
                event_date=event_date,
                event_time=event_time,
                assigned_to=quote.seller,
                quote=quote,
                customer=quote.customer,
            )

            Reminder.objects.create(
                event=event,
                remind_date=event_date,
                message=title,
            )
            created_count += 1

        if invalid_rows:
            messages.warning(
                request,
                f"Algumas linhas foram ignoradas por dados inválidos: {', '.join(str(x) for x in invalid_rows)}.",
            )

        if created_count > 0:
            messages.success(request, f"{created_count} lembrete(s) criado(s) com sucesso.")
            return redirect("sales:quote_reminders", quote_id=quote.id)

        if not invalid_rows:
            messages.error(request, "Preencha ao menos um lembrete com título e data.")

    reminder_events = (
        CalendarEvent.objects.filter(quote=quote, event_type=EventType.QUOTE_FOLLOWUP)
        .select_related("assigned_to")
        .prefetch_related("reminders")
        .order_by("event_date", "event_time", "id")
    )

    return render(request, "sales/quote_reminders.html", {
        "quote": quote,
        "reminder_events": reminder_events,
        "is_admin": _is_admin(request.user),
    })

@login_required
@require_http_methods(["POST"])
def quote_convert_to_orders(request: HttpRequest, quote_id: int) -> HttpResponse:
    quote = get_object_or_404(
        Quote.objects.prefetch_related("items", "items__supplier"),
        id=quote_id,
    )
    if not _can_access_all_quotes(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    try:
        with transaction.atomic():
            if quote.orders.exists():
                raise ValidationError("Este orçamento já foi convertido.")

            if quote.status == QuoteStatus.CANCELED:
                raise ValidationError("Orçamento cancelado não pode ser convertido.")

            all_items = list(quote.items.all())
            if not all_items:
                raise ValidationError("Orçamento sem itens não pode ser convertido.")

            # Compatibilidade: se o formulário não enviar seleção explícita,
            # mantém comportamento antigo (todos os itens do orçamento).
            has_item_selection = request.POST.get("has_item_selection") == "1"
            selected_item_ids_raw = request.POST.getlist("selected_item_ids")
            if has_item_selection:
                # `items` = itens que precisam de pedido de compra ao fornecedor.
                # Seleção vazia é válida: significa que tudo saiu do estoque da
                # loja e nenhum pedido de compra ao fornecedor é necessário. O
                # pedido total (conferência) abaixo continua com todos os itens.
                try:
                    selected_item_ids = {int(item_id) for item_id in selected_item_ids_raw}
                except (TypeError, ValueError):
                    raise ValidationError("Seleção de itens inválida.")

                items = [it for it in all_items if it.id in selected_item_ids]
            else:
                items = all_items

            missing_supplier = [it for it in items if it.supplier_id is None]
            if missing_supplier:
                nomes = ", ".join([it.product_name for it in missing_supplier[:5]])
                extra = "" if len(missing_supplier) <= 5 else f" (+{len(missing_supplier)-5})"
                raise ValidationError(f"Itens sem fornecedor: {nomes}{extra}.")

            by_supplier: dict[int, list] = defaultdict(list)
            for it in items:
                by_supplier[it.supplier_id].append(it)

            for supplier_id, supplier_items in by_supplier.items():
                order = Order.objects.create(
                    number=quote.number,
                    quote=quote,
                    supplier_id=supplier_id,
                    is_total_conference=False,
                    status=OrderStatus.PENDING,
                    delivery_deadline=None,
                )

                OrderItem.objects.bulk_create([
                    OrderItem(
                        order=order,
                        product_name=it.product_name,
                        description=it.description,
                        quantity=it.quantity,
                        purchase_unit_cost=it.unit_value,
                        quote_item=it,
                    )
                    for it in supplier_items
                ])

            total_order = Order.objects.create(
                number=quote.number,
                quote=quote,
                supplier=None,
                is_total_conference=True,
                status=OrderStatus.PENDING,
                delivery_deadline=None,
            )

            # Pedido total (conferência) é o pedido mestre da venda: sempre
            # contém TODOS os itens vendidos, independente de quais precisam de
            # compra ao fornecedor.
            OrderItem.objects.bulk_create([
                OrderItem(
                    order=total_order,
                    product_name=it.product_name,
                    description=it.description,
                    quantity=it.quantity,
                    purchase_unit_cost=it.unit_value,
                    quote_item=it,
                )
                for it in all_items
            ])

            quote.status = QuoteStatus.CONVERTED
            quote.sale_date = timezone.localdate()
            quote.save(update_fields=["status", "sale_date"])

            # Congela a comissão apurada pelo motor de margem. É a única forma de
            # o relatório mostrar exatamente o mesmo número que o vendedor viu no
            # simulador — depois da venda os parâmetros podem mudar.
            persist_quote_commission(quote)

            # Divisão de comissão: vendedores marcados dividem a comissão
            # igualmente COM o vendedor do orçamento. Nenhum marcado = 100% ao
            # vendedor original (não cria registro de divisão).
            from accounts.models import Role
            from django.contrib.auth import get_user_model
            User = get_user_model()

            selected_seller_ids: set[int] = set()
            for raw in request.POST.getlist("commission_seller_ids"):
                try:
                    selected_seller_ids.add(int(raw))
                except (TypeError, ValueError):
                    continue
            selected_seller_ids.discard(quote.seller_id)

            valid_split_sellers = list(
                User.objects.filter(
                    pk__in=selected_seller_ids,
                    is_active=True,
                    role=Role.SELLER,
                )
            ) if selected_seller_ids else []

            if valid_split_sellers:
                split, _ = QuoteCommissionSplit.objects.get_or_create(quote=quote)
                split.users.set([quote.seller, *valid_split_sellers])

            if quote.has_architect:
                reminder_date = timezone.localdate() + timedelta(days=30)
                architect_label = quote.architect.name if quote.architect_id else "Arquiteto"
                reminder_title = f"Pagamento arquiteto - Orçamento {quote.number}"
                reminder_description = (
                    f"Lembrar pagamento do arquiteto ({architect_label}) referente ao orçamento {quote.number}."
                )

                from django.contrib.auth import get_user_model
                User = get_user_model()
                recipients = [
                    user for user in User.objects.filter(is_active=True)
                    if _can_view_all_orders(user)
                ]

                for recipient in recipients:
                    event, created = CalendarEvent.objects.get_or_create(
                        quote=quote,
                        order=total_order,
                        event_type=EventType.ARCHITECT_PAYMENT,
                        assigned_to=recipient,
                        defaults={
                            "title": reminder_title,
                            "description": reminder_description,
                            "status": EventStatus.PENDING,
                            "event_date": reminder_date,
                            "customer": quote.customer,
                        },
                    )
                    if created:
                        Reminder.objects.create(
                            event=event,
                            remind_date=reminder_date,
                            message=reminder_title,
                        )

            # NÃO apagar as imagens dos itens ao converter: elas continuam sendo
            # usadas no PDF do cliente e precisam sobreviver a edições posteriores
            # do orçamento (agora editável mesmo após a conversão).

        from core.models import AuditLog, AuditAction, Notification, NotificationType
        AuditLog.log(request.user, AuditAction.CONVERT_ORDER,
                     f"Orçamento {quote.number} convertido em pedido", obj=quote,
                     ip_address=request.META.get('REMOTE_ADDR'))

        if quote.seller != request.user:
            Notification.send(
                quote.seller,
                f"Pedido gerado: {quote.number}",
                NotificationType.ORDER_CONFIRMED,
                message=f"Orçamento {quote.number} (cliente: {quote.customer.name}) foi convertido em pedido.",
                url=f"/sales/quotes/{quote.id}/",
            )

        from django.contrib.auth import get_user_model
        User = get_user_model()
        for finance_user in User.objects.filter(is_active=True):
            if _can_view_all_orders(finance_user) and finance_user != request.user:
                Notification.send(
                    finance_user,
                    f"Pedido aguardando aprovação: {quote.number}",
                    NotificationType.ORDER_CONFIRMED,
                    message=(
                        f"Orçamento {quote.number} (cliente: {quote.customer.name}) "
                        f"foi convertido em pedido pelo vendedor {request.user.get_full_name() or request.user.username}. "
                        f"Aguardando sua aprovação."
                    ),
                    url=f"/sales/orders/?status=PENDING",
                )

        if not items:
            messages.success(
                request,
                (
                    f"Orçamento {quote.number} convertido em pedido (venda de estoque da loja). "
                    "Nenhum pedido de compra ao fornecedor foi gerado. "
                    "Aguardando aprovação do financeiro."
                ),
            )
        elif len(items) != len(all_items):
            messages.success(
                request,
                (
                    f"Orçamento {quote.number} convertido em pedido com "
                    f"{len(items)} de {len(all_items)} item(ns) enviado(s) para compra ao fornecedor. "
                    "Aguardando aprovação do financeiro."
                ),
            )
        else:
            messages.success(request, f"Orçamento {quote.number} convertido em pedido. Aguardando aprovação do financeiro.")
        return redirect("sales:quote_detail", quote_id=quote.id)

    except ValidationError as e:
        messages.error(request, str(e))
        return redirect("sales:quote_detail", quote_id=quote.id)

_BRAND_FONTS_CACHE = None


def _register_brand_fonts():
    """Registra a fonte da marca (Manrope) para os PDFs de proposta.

    O app usa Manrope (Google Font) na identidade visual, mas o reportlab não
    lê Google Fonts — precisa do arquivo .ttf local. Procura os arquivos em
    pastas conhecidas e registra; se não achar, cai para Helvetica.

    Para ativar a fonte correta, coloque os arquivos em
    ``config/templates/fonts/``:
        - Manrope-Regular.ttf
        - Manrope-Bold.ttf

    Retorna (nome_regular, nome_bold).
    """
    global _BRAND_FONTS_CACHE
    if _BRAND_FONTS_CACHE is not None:
        return _BRAND_FONTS_CACHE

    import os
    from django.conf import settings as _s
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    search_dirs = [
        _s.BASE_DIR / 'config' / 'templates' / 'fonts',
        _s.BASE_DIR / 'templates' / 'fonts',
        _s.BASE_DIR / 'config' / 'templates' / 'proposal' / 'fonts',
    ]
    reg = bold = None
    for d in search_dirs:
        rp = os.path.join(str(d), 'Manrope-Regular.ttf')
        bp = os.path.join(str(d), 'Manrope-Bold.ttf')
        if os.path.isfile(rp) and os.path.isfile(bp):
            try:
                pdfmetrics.registerFont(TTFont('Manrope', rp))
                pdfmetrics.registerFont(TTFont('Manrope-Bold', bp))
                reg, bold = 'Manrope', 'Manrope-Bold'
                break
            except Exception:
                logger.warning("Falha ao registrar a fonte Manrope.", exc_info=True)

    if not reg:
        reg, bold = 'Helvetica', 'Helvetica-Bold'

    _BRAND_FONTS_CACHE = (reg, bold)
    return _BRAND_FONTS_CACHE


@login_required
def quote_pdf_client(request: HttpRequest, quote_id: int) -> HttpResponse:
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.pdfmetrics import stringWidth

    quote = get_object_or_404(
        Quote.objects.select_related("customer", "seller")
                     .prefetch_related("items", "items__images"),
        id=quote_id,
    )
    if not _can_access_all_quotes(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    config = ProposalConfig.get_config()

    buffer = BytesIO()
    page_w, page_h = A4
    c = pdf_canvas.Canvas(buffer, pagesize=A4)

    WHITE = colors.white
    # Loja não usa amarelo/dourado nem azul — paleta neutra (grafite + cinzas).
    GOLD   = colors.HexColor('#9A9A9A')   # accent neutro (antigo dourado)
    NAVY   = colors.HexColor('#1F1F1F')   # grafite quase-preto (antigo azul)
    LINEN  = colors.HexColor('#FAF7F2')
    GRAY   = colors.HexColor('#888888')
    LGRAY  = colors.HexColor('#DDDDDD')
    RULE   = colors.HexColor('#CCCCCC')   # cor única das linhas divisórias
    RULE_W = 0.8                          # espessura única das linhas

    # Fonte da marca (Manrope) com fallback para Helvetica.
    FONT_REG, FONT_BOLD = _register_brand_fonts()
    FONT_ITALIC = "Helvetica-Oblique"

    def _sw(text, font, size):
        return stringWidth(text, font, size)

    def _spaced_w(text, font, size, cs):
        return _sw(text, font, size) + cs * max(0, len(text) - 1)

    def _draw_spaced(text, x, y, font, size, cs=2.0):
        c.setFont(font, size)
        cur_x = x
        for ch in text:
            c.drawString(cur_x, y, ch)
            cur_x += _sw(ch, font, size) + cs

    def _draw_spaced_centered(text, cx, y, font, size, cs=2.0):
        tw = _spaced_w(text, font, size, cs)
        _draw_spaced(text, cx - tw / 2, y, font, size, cs)

    def _draw_bg(field, fallback='#1a0f07'):
        drawn = False
        if field:
            try:
                c.drawImage(ImageReader(field.path), 0, 0,
                            width=page_w, height=page_h,
                            preserveAspectRatio=False, mask='auto')
                drawn = True
            except Exception:
                pass
        if not drawn:
            c.setFillColor(colors.HexColor(fallback))
            c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    def _wrap(text, font, size, max_w):
        words = text.split()
        lines, cur, cw_acc = [], [], 0
        for word in words:
            ww = _sw(word + ' ', font, size)
            if cur and cw_acc + ww > max_w:
                lines.append(' '.join(cur))
                cur, cw_acc = [word], _sw(word + ' ', font, size)
            else:
                cur.append(word)
                cw_acc += ww
        if cur:
            lines.append(' '.join(cur))
        return lines

    def _fmt_brl(value):
        s = f"{float(value):,.2f}"
        s = s.replace(',', '\x00').replace('.', ',').replace('\x00', '.')
        return f"R$ {s}"

    _months_pt = [
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    ]
    qd = quote.quote_date
    date_str = f"{qd.day} de {_months_pt[qd.month - 1]} de {qd.year}"

    import os as _os
    from django.conf import settings as _settings
    _PROPOSAL_DIR = _settings.BASE_DIR / 'config' / 'templates' / 'proposal'

    def _draw_static_page(filename):
        drawn = False
        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
            candidate = _PROPOSAL_DIR / (filename + ext)
            if _os.path.isfile(candidate):
                try:
                    c.drawImage(ImageReader(str(candidate)), 0, 0,
                                width=page_w, height=page_h,
                                preserveAspectRatio=False, mask='auto')
                    drawn = True
                except Exception:
                    pass
                break
        if not drawn:
            c.setFillColor(LINEN)
            c.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        c.showPage()

    _draw_static_page('page1')

    _draw_static_page('page2')

    MX       = 2.2 * cm
    MY       = 2.2 * cm
    CW       = page_w - 2 * MX
    HEADER_H = 72
    ITEM_H   = 178
    IMG_SZ   = 128
    FOOTER_H = 230

    items = list(quote.items.prefetch_related('images').all())

    def _items_page_bg():
        # Fundo branco em toda a página de itens (header + lista de produtos),
        # evitando o tom dourado/creme (LINEN) que tornava o texto ilegível.
        c.setFillColor(WHITE)
        c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    def _draw_header():
        top = page_h - MY

        c.setFillColor(GRAY)
        _draw_spaced("Cliente", MX, top - 14, FONT_REG, 7.5, cs=1.0)
        c.setFillColor(NAVY)
        c.setFont(FONT_BOLD, 12)
        c.drawString(MX, top - 30, quote.customer.name)

        c.setFillColor(GRAY)
        _draw_spaced_centered("Consultor(a)", page_w / 2, top - 14, FONT_REG, 7.5, cs=1.0)
        seller_label = quote.seller.get_full_name() or quote.seller.username
        c.setFillColor(NAVY)
        c.setFont(FONT_BOLD, 12)
        c.drawCentredString(page_w / 2, top - 30, seller_label)

        c.setFillColor(GRAY)
        data_w = _spaced_w("Data", FONT_REG, 7.5, 1.0)
        _draw_spaced("Data", MX + CW - data_w, top - 14, FONT_REG, 7.5, cs=1.0)
        c.setFillColor(NAVY)
        c.setFont(FONT_BOLD, 12)
        c.drawRightString(MX + CW, top - 30, date_str)

        sep_y = page_h - MY - HEADER_H + 10
        c.setStrokeColor(RULE)
        c.setLineWidth(RULE_W)
        c.line(MX, sep_y, MX + CW, sep_y)
        return sep_y - 24

    def _img_placeholder(x, y, sz):
        c.setFillColor(LGRAY)
        c.rect(x, y, sz, sz, fill=1, stroke=0)
        c.setFillColor(GRAY)
        c.setFont(FONT_REG, 7)
        c.drawCentredString(x + sz / 2, y + sz / 2 - 4, "sem imagem")

    def _draw_item(item, y_top, idx):
        img_right = (idx % 2 == 0)

        if img_right:
            txt_x = MX
            txt_w = CW - IMG_SZ - 14
            img_x = MX + CW - IMG_SZ
        else:
            img_x = MX
            txt_x = MX + IMG_SZ + 14
            txt_w = CW - IMG_SZ - 14

        img_y = y_top - (ITEM_H + IMG_SZ) / 2
        first_img = item.images.first()
        if first_img:
            try:
                c.drawImage(ImageReader(first_img.image.path),
                            img_x, img_y, width=IMG_SZ, height=IMG_SZ,
                            preserveAspectRatio=True, mask='auto')
            except Exception:
                _img_placeholder(img_x, img_y, IMG_SZ)
        else:
            _img_placeholder(img_x, img_y, IMG_SZ)

        # padding superior dentro do bloco do item (texto não cola na linha de cima)
        ty = y_top - 16

        qty_str  = f"{item.quantity:02d}"
        qty_font, qty_size = FONT_BOLD, 32
        c.setFillColor(GOLD)
        c.setFont(qty_font, qty_size)
        c.drawString(txt_x, ty - qty_size, qty_str)

        name_x = txt_x + _sw(qty_str, qty_font, qty_size) + 8
        c.setFillColor(NAVY)
        _draw_spaced(item.product_name.upper(), name_x, ty - 20,
                     FONT_BOLD, 11, cs=1.5)

        ty -= qty_size + 12

        if item.description:
            for raw_line in item.description.split('\n'):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                for wline in _wrap(raw_line, FONT_REG, 8.5, txt_w):
                    c.setFillColor(GRAY)
                    _draw_spaced(wline, txt_x, ty, FONT_REG, 8.5, cs=0.5)
                    ty -= 12

        if item.condition_text:
            for wline in _wrap(item.condition_text.strip(), FONT_REG, 8.5, txt_w):
                c.setFillColor(GRAY)
                _draw_spaced(wline, txt_x, ty, FONT_REG, 8.5, cs=0.5)
                ty -= 12

        price_y = y_top - ITEM_H + 26
        # Preço de LISTA ao cliente = valor unitário com o Ajuste de Preço (markup)
        # embutido, sem desconto. Assim a soma dos itens + frete reconcilia com o
        # "Valor sem desconto"; o desconto entra depois, no bloco de total.
        markup_pct = quote.price_increase_percent or Decimal('0')
        markup_mult = Decimal('1') + markup_pct / Decimal('100')
        unit_price = item.unit_value * markup_mult
        qty_label = "valor total" if item.quantity == 1 else "valor unitário"

        def _price_block(x, label, base_unit):
            amt = base_unit * item.quantity if item.quantity == 1 else base_unit
            c.setFillColor(GRAY)
            _draw_spaced(label, x, price_y + 13, FONT_REG, 7.5, cs=1.5)
            c.setFillColor(NAVY)
            c.setFont(FONT_BOLD, 13)
            c.drawString(x, price_y - 1, _fmt_brl(amt))

        if quote.dual_pricing:
            # Dois preços por item: varejo (unit_value) e atacado
            # (unit_value_wholesale, caindo no varejo se em branco). Mesmo markup
            # do varejo, para reconciliar com os totais.
            wholesale_unit = item.unit_value_wholesale
            if wholesale_unit is None:
                wholesale_unit = item.unit_value
            _price_block(txt_x, f"varejo · {qty_label}", unit_price)
            _price_block(txt_x + txt_w / 2, f"atacado · {qty_label}",
                         wholesale_unit * markup_mult)
        else:
            _price_block(txt_x, qty_label, unit_price)

        bot_y = y_top - ITEM_H
        c.setStrokeColor(RULE)
        c.setLineWidth(RULE_W)
        c.line(MX, bot_y + 3, MX + CW, bot_y + 3)

        return bot_y - 5

    def _draw_proposta_especial(y_top):
        # ── Cabeçalho da seção: rótulo + filete divisório ────────────────
        c.setStrokeColor(RULE)
        c.setLineWidth(RULE_W)
        c.line(MX, y_top, MX + CW, y_top)

        ty = y_top - 24

        c.setFillColor(NAVY)
        _draw_spaced("PROPOSTA ESPECIAL", MX, ty, FONT_BOLD, 13, cs=3)
        c.setFillColor(GRAY)
        c.setFont(FONT_ITALIC, 8)
        c.drawRightString(MX + CW, ty + 1, "Orçamento válido por 03 dias")
        ty -= 17

        # Frete por conta da loja (STORE) nunca é cobrado do cliente — a loja
        # arca com o custo e ele fica fora do total —, então é sempre "grátis".
        if quote.freight_responsible == FreightResponsible.STORE:
            c.setFillColor(GRAY)
            c.setFont(FONT_REG, 8.5)
            c.drawString(MX, ty, "Entrega e montagem grátis pela equipe Roselar Móveis.")
            ty -= 13
        elif quote.freight_responsible == FreightResponsible.CUSTOMER:
            c.setFillColor(GRAY)
            c.setFont(FONT_REG, 8.5)
            c.drawString(MX, ty, "Frete por conta do cliente.")
            ty -= 13

        ty -= 12

        # ── Cálculo dos valores ──────────────────────────────────────────
        # Fonte única de verdade: o total ao cliente é EXATAMENTE o snapshot
        # mostrado na tela de detalhe (calculate_rounded_total), já com ajuste
        # de preço, desconto, FRETE e preço final/arredondamento embutidos.
        # Antes o PDF recomputava sem o frete e divergia do total do sistema.
        subtotal   = quote.calculate_subtotal()
        disc_pct   = quote.discount_percent or Decimal('0')
        disc_val   = subtotal * disc_pct / Decimal('100')
        avista     = quote.calculate_rounded_total()   # total real ao cliente (com frete)
        list_price = avista + disc_val                 # "valor sem desconto"

        from core.models import PaymentMethodType
        _pay_names = dict(PaymentMethodType.choices)

        def _method_label(code):
            return _pay_names.get(code, code or "")

        # ── Helpers de linha ─────────────────────────────────────────────
        def _row(label, value, *, lbl_color=NAVY, val_color=NAVY,
                 lbl_font=(FONT_REG, 9.5), val_font=(FONT_BOLD, 11),
                 strike=False):
            nonlocal ty
            c.setFillColor(lbl_color)
            c.setFont(*lbl_font)
            c.drawString(MX, ty, label)
            c.setFillColor(val_color)
            c.setFont(*val_font)
            c.drawRightString(MX + CW, ty, value)
            if strike:
                vw = _sw(value, val_font[0], val_font[1])
                c.setStrokeColor(val_color)
                c.setLineWidth(0.7)
                c.line(MX + CW - vw, ty + 3, MX + CW, ty + 3)
            ty -= 16

        def _subnote(text):
            nonlocal ty
            c.setFillColor(GRAY)
            c.setFont(FONT_REG, 8)
            c.drawString(MX + 12, ty, text)
            ty -= 13

        # rótulo "COMO PAGAR"
        c.setFillColor(GRAY)
        _draw_spaced("COMO PAGAR", MX, ty, FONT_BOLD, 7.5, cs=1.5)
        ty -= 15

        if disc_pct > 0:
            _row("Valor sem desconto", _fmt_brl(list_price),
                 lbl_color=GRAY, val_color=GRAY,
                 val_font=(FONT_REG, 10), strike=True)

        split_active = bool(quote.payment_type_2) and quote.payment_split_amount is not None

        if split_active:
            # Pagamento composto: Entrada (método 1) + Restante (método 2).
            # Ambas as pernas particionam o total real ao cliente (com frete).
            entrada_val  = min(quote.payment_split_amount, avista)
            restante_val = max(Decimal('0'), avista - entrada_val)
            n1 = quote.payment_installments or 1
            n2 = quote.payment_installments_2 or 1
            m1 = _method_label(quote.payment_type)
            m2 = _method_label(quote.payment_type_2)

            _row(f"Entrada no {m1}" if m1 else "Entrada", _fmt_brl(entrada_val))
            if n1 > 1:
                _subnote(f"em {n1}x de {_fmt_brl(entrada_val / Decimal(n1))} sem juros")

            _row(f"Restante no {m2}" if m2 else "Restante", _fmt_brl(restante_val))
            if n2 > 1:
                _subnote(f"em {n2}x de {_fmt_brl(restante_val / Decimal(n2))} sem juros")
        else:
            n  = quote.payment_installments or 1
            m1 = _method_label(quote.payment_type)
            if n > 1:
                parcela = avista / Decimal(n)
                lbl = f"Parcelado no {m1}" if m1 else "Parcelado"
                _row(lbl, f"{n}x de {_fmt_brl(parcela)}")
                _subnote("sem juros")
            else:
                # Sempre mostra a linha à vista — evita a seção "COMO PAGAR"
                # ficar vazia quando não há forma de pagamento selecionada.
                _row(f"À vista no {m1}" if m1 else "À vista", _fmt_brl(avista))

        # ── Barra de total destacada (navy) ──────────────────────────────
        ty -= 6
        bar_h = 42
        bar_y = ty - bar_h
        c.setFillColor(NAVY)
        c.roundRect(MX, bar_y, CW, bar_h, 6, fill=1, stroke=0)

        total_lbl = "Valor do investimento com desconto" if disc_pct > 0 else "Valor do investimento"
        if quote.dual_pricing:
            total_lbl = "Valor do investimento — Varejo"
        c.setFillColor(WHITE)
        c.setFont(FONT_REG, 9.5)
        c.drawString(MX + 16, bar_y + bar_h / 2 - 3, total_lbl)
        c.setFillColor(WHITE)
        c.setFont(FONT_BOLD, 17)
        c.drawRightString(MX + CW - 16, bar_y + bar_h / 2 - 6, _fmt_brl(avista))

        # ── Total de atacado (orçamento atacado + varejo) ────────────────
        if quote.dual_pricing:
            avista_atacado = quote.calculate_rounded_total_wholesale()
            ty = bar_y - 10
            atac_h = 34
            atac_y = ty - atac_h
            c.setStrokeColor(NAVY)
            c.setLineWidth(1)
            c.roundRect(MX, atac_y, CW, atac_h, 6, fill=0, stroke=1)
            c.setFillColor(NAVY)
            c.setFont(FONT_REG, 9.5)
            c.drawString(MX + 16, atac_y + atac_h / 2 - 3, "Valor do investimento — Atacado")
            c.setFont(FONT_BOLD, 15)
            c.drawRightString(MX + CW - 16, atac_y + atac_h / 2 - 5, _fmt_brl(avista_atacado))

    _items_page_bg()
    cur_y = _draw_header()

    for i, item in enumerate(items):
        is_last = (i == len(items) - 1)
        space_needed = ITEM_H + (FOOTER_H if is_last else 0)

        if cur_y - space_needed < MY:
            c.showPage()
            _items_page_bg()
            cur_y = page_h - MY - 15

        cur_y = _draw_item(item, cur_y, i)

    if cur_y - FOOTER_H < MY:
        c.showPage()
        _items_page_bg()
        cur_y = page_h - MY

    _draw_proposta_especial(cur_y - 10)

    try:
        c.save()
    except Exception:
        logger.exception('Error generating client PDF for quote %s', quote.number)
        raise
    pdf = buffer.getvalue()
    buffer.close()

    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = _safe_content_disposition(
        f'proposta_{quote.number}.pdf'
    )
    return response

@login_required
def quote_pdf_supplier(request: HttpRequest, quote_id: int) -> HttpResponse:
    import zipfile as zipfile_mod

    quote = get_object_or_404(
        Quote.objects.select_related("customer", "seller")
                     .prefetch_related("items", "items__supplier"),
        id=quote_id,
    )
    if not _is_staff_or_admin(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")
    if not _is_admin(request.user):
        messages.error(request, "Apenas administradores podem baixar o PDF de fornecedor.")
        return redirect("sales:quote_detail", quote_id=quote.id)

    # GET → exibe formulário com os 3 campos manuais antes de gerar o PDF
    if request.method != "POST":
        return render(request, "sales/quote_pdf_supplier_form.html", {"quote": quote})

    transportadora = request.POST.get("transportadora", "").strip()
    cond_pagamento = request.POST.get("cond_pagamento", "").strip()
    observacoes    = request.POST.get("observacoes", "").strip()

    supplier_prices: dict[int, Decimal] = {}
    for item in quote.items.all():
        if not item.supplier_id:
            continue
        raw = request.POST.get(f"price_{item.id}", "").strip()
        try:
            supplier_prices[item.id] = Decimal(raw.replace(",", "."))
        except Exception:
            messages.error(
                request,
                f'Valor inválido para o produto "{item.product_name}". Preencha todos os preços corretamente.',
            )
            return render(request, "sales/quote_pdf_supplier_form.html", {"quote": quote})

    def _fmt_brl(value) -> str:
        s = f"{float(value):,.2f}"
        return s.replace(',', '\x00').replace('.', ',').replace('\x00', '.')

    NAVY  = colors.HexColor('#0A2640')
    LGRAY = colors.HexColor('#DDDDDD')
    BGROW = colors.HexColor('#F8F9FA')
    MUTED = colors.HexColor('#888888')
    _styles = getSampleStyleSheet()

    def _ps(name, **kw):
        return ParagraphStyle(name, parent=_styles['Normal'], **kw)

    def _make_pdf_for_supplier(supplier, items_for_supplier,
                               _transp=transportadora,
                               _cond=cond_pagamento,
                               _obs=observacoes,
                               _prices=supplier_prices) -> bytes:
        st_title   = _ps(f'{supplier.id}_title',  fontSize=15, fontName='Helvetica-Bold',
                         textColor=NAVY, alignment=TA_CENTER, spaceAfter=2)
        st_sub     = _ps(f'{supplier.id}_sub',    fontSize=9, textColor=MUTED,
                         alignment=TA_CENTER, spaceAfter=0)
        st_section = _ps(f'{supplier.id}_sec',    fontSize=9, fontName='Helvetica-Bold',
                         textColor=NAVY, spaceBefore=8, spaceAfter=4)
        st_normal  = _ps(f'{supplier.id}_normal', fontSize=9, leading=13)
        st_label   = _ps(f'{supplier.id}_label',  fontSize=7, textColor=MUTED, leading=11)
        st_footer  = _ps(f'{supplier.id}_footer', fontSize=7, textColor=MUTED, alignment=TA_CENTER)
        st_th      = _ps(f'{supplier.id}_th',     fontSize=8, fontName='Helvetica-Bold',
                         textColor=colors.white, alignment=TA_CENTER)
        st_td_c    = _ps(f'{supplier.id}_td_c',   fontSize=8, alignment=TA_CENTER)
        st_td_l    = _ps(f'{supplier.id}_td_l',   fontSize=8)
        st_td_g    = _ps(f'{supplier.id}_td_g',   fontSize=7, textColor=colors.HexColor('#666666'))

        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            rightMargin=2*cm, leftMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm,
        )
        els = []

        els.append(Paragraph("ROSELAR MÓVEIS", st_title))
        els.append(Paragraph("PEDIDO DE COMPRA", st_sub))
        els.append(Spacer(1, 0.3*cm))
        els.append(HRFlowable(width="100%", thickness=2, color=NAVY))
        els.append(Spacer(1, 0.4*cm))

        seller_name = quote.seller.get_full_name() or quote.seller.username
        meta_data = [
            [
                Paragraph(f"<b>Orçamento:</b> #{quote.number}", st_normal),
                Paragraph(f"<b>Data:</b> {quote.quote_date.strftime('%d/%m/%Y')}", st_normal),
            ],
            [
                Paragraph(f"<b>Vendedor:</b> {seller_name}", st_normal),
                Paragraph("", st_normal),
            ],
            [
                Paragraph(f"<b>Transportadora:</b> {_transp or '—'}", st_normal),
                Paragraph(f"<b>Cond. Pagamento:</b> {_cond or '—'}", st_normal),
            ],
        ]
        meta_tbl = Table(meta_data, colWidths=[8.5*cm, 8.5*cm])
        meta_tbl.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING',   (0, 0), (-1, -1), 0),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        els.append(meta_tbl)
        els.append(Spacer(1, 0.4*cm))

        supplier_cell = [
            Paragraph("<b>Fornecedor</b>", st_section),
            Paragraph(supplier.name, st_normal),
        ]
        if supplier.phone:
            supplier_cell.append(Paragraph(f"Tel: {supplier.phone}", st_label))
        if supplier.email:
            supplier_cell.append(Paragraph(supplier.email, st_label))

        client_cell = [
            Paragraph("<b>Cliente</b>", st_section),
            Paragraph(quote.customer.name, st_normal),
        ]

        party_tbl = Table([[supplier_cell, client_cell]], colWidths=[8.5*cm, 8.5*cm])
        party_tbl.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('BOX',           (0, 0), (0, 0),   0.5, LGRAY),
            ('BOX',           (1, 0), (1, 0),   0.5, LGRAY),
            ('BACKGROUND',    (0, 0), (0, 0),   BGROW),
            ('BACKGROUND',    (1, 0), (1, 0),   BGROW),
            ('LEFTPADDING',   (0, 0), (-1, -1), 8),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
            ('TOPPADDING',    (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        els.append(party_tbl)
        els.append(Spacer(1, 0.5*cm))

        els.append(Paragraph("ITENS DO PEDIDO", st_section))
        hdr = [
            Paragraph('#',          st_th),
            Paragraph('Produto',    st_th),
            Paragraph('Descrição',  st_th),
            Paragraph('Qtd',        st_th),
            Paragraph('Vlr. Unit.', st_th),
            Paragraph('Total',      st_th),
        ]
        tdata = [hdr]
        subtotal = Decimal('0.00')
        for idx, item in enumerate(items_for_supplier, 1):
            unit = _prices.get(item.id, Decimal('0.00'))
            line = unit * item.quantity
            subtotal += line
            desc = item.description.strip() if item.description else '—'
            tdata.append([
                Paragraph(str(idx),              st_td_c),
                Paragraph(item.product_name,     st_td_l),
                Paragraph(desc,                  st_td_g),
                Paragraph(str(item.quantity),    st_td_c),
                Paragraph(f"R$ {_fmt_brl(unit)}", st_td_c),
                Paragraph(f"R$ {_fmt_brl(line)}", st_td_c),
            ])

        col_w = [0.8*cm, 4.5*cm, 6.5*cm, 1.2*cm, 2.5*cm, 2.5*cm]
        itbl = Table(tdata, colWidths=col_w, repeatRows=1)
        row_styles = [
            ('BACKGROUND',    (0, 0), (-1, 0),  NAVY),
            ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
            ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
            ('FONTSIZE',      (0, 0), (-1, 0),  8),
            ('TOPPADDING',    (0, 0), (-1, 0),  7),
            ('BOTTOMPADDING', (0, 0), (-1, 0),  7),
            ('GRID',          (0, 0), (-1, -1), 0.5, LGRAY),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 1), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
            ('LEFTPADDING',   (0, 1), (-1, -1), 5),
            ('RIGHTPADDING',  (0, 1), (-1, -1), 5),
        ]
        for i in range(1, len(tdata)):
            if i % 2 == 0:
                row_styles.append(('BACKGROUND', (0, i), (-1, i), BGROW))
        itbl.setStyle(TableStyle(row_styles))
        els.append(itbl)
        els.append(Spacer(1, 0.3*cm))

        total_tbl = Table(
            [[Paragraph(f"<b>Subtotal:</b> R$ {_fmt_brl(subtotal)}", st_normal)]],
            colWidths=[17*cm],
        )
        total_tbl.setStyle(TableStyle([
            ('ALIGN',         (0, 0), (-1, -1), 'RIGHT'),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ]))
        els.append(total_tbl)

        if quote.delivery_days_min or quote.delivery_days_max:
            mn = quote.delivery_days_min
            mx = quote.delivery_days_max
            if mn and mx:
                prazo_txt = f"{mn} a {mx} dias"
            elif mn:
                prazo_txt = f"a partir de {mn} dias"
            else:
                prazo_txt = f"até {mx} dias"
            els.append(Spacer(1, 0.2*cm))
            els.append(Paragraph(
                f"<b>Prazo de entrega estimado:</b> {prazo_txt}",
                st_normal,
            ))

        if _obs:
            els.append(Spacer(1, 0.4*cm))
            st_obs_lbl = _ps(f'{supplier.id}_obs_lbl', fontSize=8, fontName='Helvetica-Bold',
                             textColor=NAVY, spaceBefore=4, spaceAfter=3)
            st_obs_txt = _ps(f'{supplier.id}_obs_txt', fontSize=8, leading=12,
                             textColor=colors.HexColor('#333333'))
            obs_tbl = Table(
                [[
                    [Paragraph("OBSERVAÇÕES", st_obs_lbl),
                     Paragraph(_obs, st_obs_txt)]
                ]],
                colWidths=[17*cm],
            )
            obs_tbl.setStyle(TableStyle([
                ('BOX',           (0, 0), (-1, -1), 0.5, LGRAY),
                ('BACKGROUND',    (0, 0), (-1, -1), BGROW),
                ('LEFTPADDING',   (0, 0), (-1, -1), 8),
                ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
                ('TOPPADDING',    (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
            els.append(obs_tbl)

        els.append(Spacer(1, 0.8*cm))
        els.append(HRFlowable(width="100%", thickness=0.5, color=LGRAY))
        els.append(Spacer(1, 0.2*cm))
        els.append(Paragraph(
            f"Gerado em {timezone.localdate().strftime('%d/%m/%Y')} | Roselar Móveis",
            st_footer,
        ))

        doc.build(els)
        data = buf.getvalue()
        buf.close()
        return data

    by_supplier: dict = defaultdict(list)
    items_without_supplier = []
    for item in quote.items.all():
        if item.supplier_id:
            by_supplier[item.supplier_id].append(item)
        else:
            items_without_supplier.append(item)

    if not by_supplier:
        messages.error(request, "Nenhum item com fornecedor cadastrado neste orçamento.")
        return redirect("sales:quote_detail", quote_id=quote.id)

    if items_without_supplier:
        nomes = ", ".join(it.product_name for it in items_without_supplier[:5])
        messages.warning(
            request,
            f"Os seguintes itens não têm fornecedor e foram ignorados: {nomes}.",
        )

    if len(by_supplier) == 1:
        supplier_id, items_list = next(iter(by_supplier.items()))
        supplier = items_list[0].supplier
        pdf_bytes = _make_pdf_for_supplier(supplier, items_list, transportadora, cond_pagamento, observacoes, supplier_prices)
        safe_name = supplier.name.replace(' ', '_').replace('/', '_')
        filename = f"pedido_{quote.number}_{safe_name}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = _safe_content_disposition(filename)
        return response

    zip_buffer = BytesIO()
    with zipfile_mod.ZipFile(zip_buffer, 'w', zipfile_mod.ZIP_DEFLATED) as zf:
        for supplier_id, items_list in by_supplier.items():
            supplier = items_list[0].supplier
            pdf_bytes = _make_pdf_for_supplier(supplier, items_list, transportadora, cond_pagamento, observacoes, supplier_prices)
            safe_name = supplier.name.replace(' ', '_').replace('/', '_')
            zf.writestr(f"pedido_{quote.number}_{safe_name}.pdf", pdf_bytes)

    zip_data = zip_buffer.getvalue()
    zip_buffer.close()

    zip_filename = f"pedidos_{quote.number}.zip"
    response = HttpResponse(zip_data, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{zip_filename}"'
    return response

@login_required
def order_list(request: HttpRequest) -> HttpResponse:
    orders = Order.objects.select_related('quote', 'supplier', 'quote__customer', 'quote__seller').order_by('-created_at')
    if not _can_view_all_orders(request.user):
        orders = orders.filter(quote__seller=request.user)

    search_query = request.GET.get('search', '').strip()
    if search_query:
        orders = orders.filter(
            models.Q(number__icontains=search_query) |
            models.Q(quote__number__icontains=search_query) |
            models.Q(quote__customer__name__icontains=search_query) |
            models.Q(supplier__name__icontains=search_query) |
            models.Q(notes__icontains=search_query)
        )
    
    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        orders = orders.filter(status=status_filter)
    
    supplier_filter = request.GET.get('supplier', '').strip()
    if supplier_filter:
        orders = orders.filter(supplier_id=supplier_filter)
    
    context = {
        'orders': orders,
        'search_query': search_query,
        'status_filter': status_filter,
        'supplier_filter': supplier_filter,
        'is_seller': _is_seller(request.user),
        'can_generate_order_pdf': _can_generate_order_pdf(request.user),
        'can_create_standalone': _is_finance(request.user) or _is_admin(request.user),
    }

    return render(request, 'sales/order_list.html', context)

@login_required
def order_detail(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(
        Order.objects.select_related('quote', 'supplier', 'quote__customer', 'quote__seller'),
        pk=order_id
    )
    if not _can_view_all_orders(request.user) and (
        order.quote is None or order.quote.seller_id != request.user.id
    ):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")

    items = order.items.select_related('quote_item').all()

    total = sum(item.line_total for item in items)

    _can_finance_action = (_is_finance(request.user) or _is_admin(request.user))
    context = {
        'order': order,
        'items': items,
        'total': total,
        'show_sale_values': _can_finance_action,
        'value_breakdown': _build_value_breakdown(order.quote) if (_can_finance_action and order.quote) else None,
        'is_seller': _is_seller(request.user),
        'can_generate_order_pdf': _can_generate_order_pdf(request.user),
        'can_set_delivery': _can_generate_order_pdf(request.user),
        'can_cancel_order': _can_finance_action,
        'can_edit_order': _can_finance_action or (order.quote and order.quote.seller_id == request.user.id),
        'can_approve_order': _can_finance_action and order.is_total_conference and order.status == OrderStatus.PENDING,
        'can_conclude_order': _can_finance_action and order.is_total_conference and order.status == OrderStatus.ONGOING,
        'supplier_orders': (
            order.quote.orders.select_related('supplier')
                               .filter(is_total_conference=False)
                               .order_by('supplier__name')
            if order.is_total_conference else None
        ),
    }

    return render(request, 'sales/order_detail.html', context)

@login_required
@require_http_methods(["POST"])
def order_set_delivery(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(
        Order.objects.select_related('quote', 'quote__customer', 'quote__seller'),
        pk=order_id,
    )
    if not _can_generate_order_pdf(request.user):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")

    if not order.is_total_conference:
        messages.error(request, "A data de entrega deve ser definida no pedido total.")
        return redirect("sales:order_detail", order_id=order.id)

    delivery_str = request.POST.get("delivery_deadline", "").strip()
    try:
        delivery_date = date_type.fromisoformat(delivery_str) if delivery_str else None
    except ValueError:
        delivery_date = None

    if not delivery_date:
        messages.error(request, "Data de entrega inválida.")
        return redirect("sales:order_detail", order_id=order.id)

    quote = order.quote
    seller_name = quote.seller.get_full_name() or quote.seller.username
    customer_name = quote.customer.name

    item_count = order.items.count()
    subtotal = sum(it.line_total for it in order.items.all())
    subtotal_fmt = f"R$ {float(subtotal):,.2f}".replace(',', '\x00').replace('.', ',').replace('\x00', '.')

    Order.objects.filter(quote=quote).update(delivery_deadline=delivery_date)

    transport_info = request.POST.get("transport_info", "").strip()
    if transport_info:
        Order.objects.filter(quote=quote).update(transport_info=transport_info)

    from core.models import AuditLog, AuditAction, Notification, NotificationType

    reminder_title = f"Entrega prevista — {quote.number} | {customer_name}"
    reminder_description = (
        f"Pedido {quote.number}\n"
        f"Cliente: {customer_name}\n"
        f"Vendedor: {seller_name}\n"
        f"Itens: {item_count} | Total: {subtotal_fmt}\n"
        f"Data de entrega definida por: {request.user.get_full_name() or request.user.username}"
    )

    recipients = set()
    recipients.add(quote.seller)
    recipients.add(request.user)

    for recipient in recipients:
        event, _ = CalendarEvent.objects.get_or_create(
            quote=quote,
            order=order,
            event_type=EventType.DELIVERY,
            assigned_to=recipient,
            defaults={
                "title": reminder_title,
                "description": reminder_description,
                "status": EventStatus.PENDING,
                "event_date": delivery_date,
                "customer": quote.customer,
            },
        )
        if event.event_date != delivery_date:
            event.event_date = delivery_date
            event.description = reminder_description
            event.save(update_fields=["event_date", "description"])

        Reminder.objects.get_or_create(
            event=event,
            defaults={
                "remind_date": delivery_date,
                "message": reminder_title,
            },
        )

        Notification.send(
            recipient,
            f"Entrega agendada: {quote.number}",
            NotificationType.DELIVERY_NEAR,
            message=(
                f"Data de entrega definida para {delivery_date.strftime('%d/%m/%Y')}.\n"
                f"Cliente: {customer_name} | {item_count} itens | {subtotal_fmt}"
            ),
            url=f"/sales/orders/{order.id}/",
        )

    AuditLog.log(
        request.user,
        AuditAction.CONVERT_ORDER,
        f"Data de entrega definida para {quote.number}: {delivery_date.strftime('%d/%m/%Y')}",
        obj=quote,
        ip_address=request.META.get('REMOTE_ADDR'),
    )

    messages.success(
        request,
        f"Data de entrega definida: {delivery_date.strftime('%d/%m/%Y')}. "
        f"Lembretes criados para {quote.seller.get_full_name() or quote.seller.username} e para você."
    )
    return redirect("sales:order_detail", order_id=order.id)


@login_required
@require_http_methods(["POST"])
def order_approve(request: HttpRequest, order_id: int) -> HttpResponse:
    """Finance/Admin: aprova pedido aguardando aprovação (PENDING → ONGOING)."""
    order = get_object_or_404(
        Order.objects.select_related('quote', 'quote__customer', 'quote__seller'),
        pk=order_id,
    )
    if not (_is_finance(request.user) or _is_admin(request.user)):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")
    if not order.is_total_conference:
        messages.error(request, "A aprovação deve ser feita no pedido total.")
        return redirect("sales:order_detail", order_id=order.id)
    if order.status != OrderStatus.PENDING:
        messages.error(request, "Este pedido não está aguardando aprovação.")
        return redirect("sales:order_detail", order_id=order.id)

    with transaction.atomic():
        Order.objects.filter(quote=order.quote).update(status=OrderStatus.ONGOING)
        from core.models import AuditLog, AuditAction, Notification, NotificationType
        AuditLog.log(
            request.user,
            AuditAction.CONVERT_ORDER,
            f"Pedido {order.quote.number} aprovado pelo financeiro. Status: Em Andamento.",
            obj=order.quote,
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        Notification.send(
            order.quote.seller,
            f"Pedido aprovado: {order.quote.number}",
            NotificationType.ORDER_CONFIRMED,
            message=(
                f"Seu pedido {order.quote.number} para {order.quote.customer.name} "
                f"foi aprovado pelo financeiro e está em andamento."
            ),
            url=f"/sales/orders/{order.id}/",
        )

    messages.success(request, f"Pedido {order.quote.number} aprovado. Status: Em Andamento.")
    return redirect("sales:order_detail", order_id=order.id)


@login_required
@require_http_methods(["POST"])
def order_conclude(request: HttpRequest, order_id: int) -> HttpResponse:
    """Finance/Admin: conclui pedido entregue (ONGOING → DONE) e dispara pós-venda."""
    order = get_object_or_404(
        Order.objects.select_related('quote', 'quote__customer', 'quote__seller'),
        pk=order_id,
    )
    if not (_is_finance(request.user) or _is_admin(request.user)):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")
    if not order.is_total_conference:
        messages.error(request, "A conclusão deve ser feita no pedido total.")
        return redirect("sales:order_detail", order_id=order.id)
    if order.status != OrderStatus.ONGOING:
        messages.error(request, "Este pedido não está em andamento.")
        return redirect("sales:order_detail", order_id=order.id)

    if not order.delivery_deadline:
        messages.error(request, "Defina a data de entrega antes de concluir o pedido.")
        return redirect("sales:order_detail", order_id=order.id)

    with transaction.atomic():
        Order.objects.filter(quote=order.quote).update(status=OrderStatus.DONE)
        quote = order.quote
        quote.status = QuoteStatus.POS_VENDA
        quote.save(update_fields=["status"])
        # Pós-venda continua sendo venda. Se por algum motivo a comissão não foi
        # apurada no fechamento (venda antiga), apura agora em vez de deixar zero.
        if quote.commission_value is None:
            persist_quote_commission(quote)
        from core.models import AuditLog, AuditAction, Notification, NotificationType
        AuditLog.log(
            request.user,
            AuditAction.CONVERT_ORDER,
            f"Pedido {quote.number} concluído. Orçamento enviado para pós-venda.",
            obj=quote,
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        Notification.send(
            quote.seller,
            f"Pós-venda: {quote.number}",
            NotificationType.GENERAL,
            message=(
                f"O pedido {quote.number} foi entregue ao cliente {quote.customer.name}. "
                f"Realize o acompanhamento pós-venda com o cliente."
            ),
            url=f"/sales/quotes/{quote.id}/",
        )

    seller_name = order.quote.seller.get_full_name() or order.quote.seller.username
    messages.success(
        request,
        f"Pedido {order.quote.number} concluído com sucesso. "
        f"Vendedor {seller_name} notificado para pós-venda.",
    )
    return redirect("sales:order_detail", order_id=order.id)


@login_required
@require_http_methods(["POST"])
def order_cancel(request: HttpRequest, order_id: int) -> HttpResponse:
    """Finance/Admin: cancela pedido removendo o(s) registro(s) de pedido."""
    order = get_object_or_404(
        Order.objects.select_related('quote', 'supplier', 'quote__customer', 'quote__seller'),
        pk=order_id,
    )
    if not (_is_finance(request.user) or _is_admin(request.user)):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")

    quote = order.quote
    order_number = order.number
    is_total = order.is_total_conference
    supplier_name = order.supplier.name if order.supplier else "pedido total"

    # Pedido avulso da loja: sem orçamento vinculado — apenas remove.
    if quote is None:
        with transaction.atomic():
            order.delete()
        from core.models import AuditLog, AuditAction
        AuditLog.log(
            request.user,
            AuditAction.CONVERT_ORDER,
            f"Cancelamento de pedido avulso: {order_number} ({supplier_name}).",
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        messages.success(request, f"Pedido avulso {order_number} cancelado.")
        return redirect("sales:order_list")

    with transaction.atomic():
        if is_total:
            deleted_count, _ = Order.objects.filter(quote=quote).delete()
            if quote.status == QuoteStatus.CONVERTED:
                quote.status = QuoteStatus.APPROVED
                quote.sale_date = None
                quote.save(update_fields=["status", "sale_date"])
                clear_quote_commission(quote)
            removed_label = f"todos os pedidos do orçamento {quote.number}"
        else:
            order.delete()
            deleted_count = 1
            if not Order.objects.filter(quote=quote).exists() and quote.status == QuoteStatus.CONVERTED:
                quote.status = QuoteStatus.APPROVED
                quote.sale_date = None
                quote.save(update_fields=["status", "sale_date"])
                clear_quote_commission(quote)
            removed_label = f"pedido {order_number} ({supplier_name})"

    from core.models import AuditLog, AuditAction, Notification, NotificationType
    AuditLog.log(
        request.user,
        AuditAction.CONVERT_ORDER,
        f"Cancelamento de pedido: {removed_label}. Registros removidos: {deleted_count}.",
        obj=quote,
        ip_address=request.META.get('REMOTE_ADDR'),
    )

    if quote.seller_id != request.user.id:
        Notification.send(
            quote.seller,
            f"Pedido cancelado: {quote.number}",
            NotificationType.GENERAL,
            message=(
                f"O financeiro cancelou {removed_label} do cliente {quote.customer.name}."
            ),
            url=f"/sales/quotes/{quote.id}/",
        )

    messages.success(request, f"Cancelamento realizado com sucesso: {removed_label}.")
    return redirect("sales:order_list")


def generate_next_store_order_number() -> str:
    """Número sequencial para pedidos avulsos da loja (sem orçamento)."""
    with transaction.atomic():
        last = (
            Order.objects.select_for_update()
            .filter(number__startswith="LOJA-")
            .order_by("-id")
            .first()
        )
        try:
            candidate = int(last.number.split("-")[1]) + 1 if last else 1
        except (ValueError, IndexError):
            candidate = Order.objects.filter(number__startswith="LOJA-").count() + 1

        while Order.objects.filter(number=f"LOJA-{candidate:04d}").exists():
            candidate += 1

        return f"LOJA-{candidate:04d}"


@login_required
@require_http_methods(["GET", "POST"])
def order_create_standalone(request: HttpRequest) -> HttpResponse:
    """Pedido de compra avulso da loja: compra de estoque ao fornecedor,
    sem orçamento/venda/vendedor vinculado. Não entra em comissões nem
    métricas de venda (que são baseadas em Quote)."""
    if not (_is_finance(request.user) or _is_admin(request.user)):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")

    if request.method == "POST":
        form = OrderForm(request.POST)
        formset = OrderItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            has_items = any(
                f.cleaned_data and not f.cleaned_data.get("DELETE")
                for f in formset.forms
            )
            if not has_items:
                messages.error(request, "Adicione pelo menos um item ao pedido.")
            else:
                with transaction.atomic():
                    order = form.save(commit=False)
                    order.quote = None
                    order.is_total_conference = False
                    order.number = generate_next_store_order_number()
                    order.save()
                    formset.instance = order
                    formset.save()

                from core.models import AuditLog, AuditAction
                AuditLog.log(
                    request.user,
                    AuditAction.CONVERT_ORDER,
                    f"Pedido avulso da loja {order.number} criado "
                    f"({order.supplier.name if order.supplier else 'sem fornecedor'}).",
                    ip_address=request.META.get('REMOTE_ADDR'),
                )
                messages.success(request, f"Pedido avulso {order.number} criado.")
                return redirect("sales:order_detail", order_id=order.id)
        else:
            messages.error(request, "Corrija os campos inválidos.")
    else:
        form = OrderForm()
        formset = OrderItemFormSet()

    return render(
        request,
        "sales/order_form.html",
        {"form": form, "formset": formset, "order": None, "is_standalone": True},
    )


@login_required
@require_http_methods(["GET", "POST"])
def order_edit(request: HttpRequest, order_id: int) -> HttpResponse:
    """Edição completa do pedido de compra: dados + itens/custos.

    Permissão: financeiro/admin ou o vendedor dono do orçamento.
    """
    order, forbidden = _get_order_or_403(
        request,
        order_id,
    )
    if forbidden:
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")

    if request.method == "POST":
        form = OrderForm(request.POST, instance=order)
        formset = OrderItemFormSet(request.POST, instance=order)

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                form.save()
                formset.save()

                # Data de emissão é uma só para a venda inteira. Um orçamento
                # convertido gera vários pedidos (o total de conferência + um por
                # fornecedor); deixar cada um com a sua data faria a mesma venda
                # ter datas de emissão diferentes e, pior, o mês da venda passaria
                # a depender de qual pedido foi editado por último.
                if "created_at" in form.changed_data and order.quote is not None:
                    siblings = Order.objects.filter(quote=order.quote).exclude(pk=order.pk)
                    updated = siblings.exclude(created_at=order.created_at).update(
                        created_at=order.created_at
                    )

                    new_sale_date = timezone.localtime(order.created_at).date()
                    if (
                        order.quote.status in SOLD_STATUSES
                        and order.quote.sale_date != new_sale_date
                    ):
                        # A data do pedido define o mês da venda nos painéis e no
                        # relatório de comissões.
                        order.quote.sale_date = new_sale_date
                        order.quote.save(update_fields=["sale_date"])
                        messages.info(
                            request,
                            f"Data da venda do orçamento {order.quote.number} "
                            f"atualizada para {new_sale_date.strftime('%d/%m/%Y')}.",
                        )
                    if updated:
                        messages.info(
                            request,
                            f"Data de emissão replicada para os outros {updated} "
                            f"pedido{'s' if updated > 1 else ''} do orçamento "
                            f"{order.quote.number}.",
                        )

            from core.models import AuditLog, AuditAction
            AuditLog.log(
                request.user,
                AuditAction.EDIT_VALUES,
                f"Pedido {order.number} editado ({order.supplier.name if order.supplier else 'pedido total'}).",
                obj=order.quote,
                ip_address=request.META.get('REMOTE_ADDR'),
            )

            messages.success(request, f"Pedido {order.number} atualizado.")
            return redirect("sales:order_detail", order_id=order.id)
        else:
            messages.error(request, "Corrija os campos inválidos.")
    else:
        form = OrderForm(instance=order)
        formset = OrderItemFormSet(instance=order)

    return render(
        request,
        "sales/order_form.html",
        {"form": form, "formset": formset, "order": order},
    )


@login_required
def order_pdf(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(
        Order.objects.select_related('quote', 'supplier', 'quote__customer', 'quote__seller'),
        pk=order_id
    )
    if not _can_generate_order_pdf(request.user):
        messages.error(request, "Seu perfil não pode baixar PDFs de pedidos.")
        return redirect("sales:order_list")
    if not _can_view_all_orders(request.user) and (
        order.quote is None or order.quote.seller_id != request.user.id
    ):
        messages.error(request, "Acesso negado.")
        return redirect("sales:order_list")

    if order.is_total_conference:
        messages.error(request, "Não é possível gerar PDF para pedido de conferência total.")
        return redirect('sales:order_detail', order_id=order.id)

    # GET → exibe formulário com campos pré-preenchidos do pedido
    if request.method != "POST":
        return render(request, "sales/order_pdf_form.html", {"order": order})

    # POST → salva os campos no pedido e gera o PDF
    transportadora = request.POST.get("transportadora", "").strip()
    cond_pagamento = request.POST.get("cond_pagamento", "").strip()
    observacoes    = request.POST.get("observacoes", "").strip()
    order.transport_info          = transportadora
    order.purchase_condition_text = cond_pagamento
    order.notes                   = observacoes
    order.save(update_fields=["transport_info", "purchase_condition_text", "notes"])

    # Read manual prices entered in the form (do NOT use stored purchase_unit_cost)
    items_qs_pre = list(order.items.all())
    manual_prices: dict[int, Decimal] = {}
    for item in items_qs_pre:
        raw = request.POST.get(f"price_{item.id}", "").strip()
        try:
            manual_prices[item.id] = Decimal(raw.replace(",", "."))
        except Exception:
            messages.error(
                request,
                f'Valor inválido para o produto "{item.product_name}". Preencha todos os preços corretamente.',
            )
            return render(request, "sales/order_pdf_form.html", {"order": order})

    def _fmt_brl(value) -> str:
        s = f"{float(value):,.2f}"
        return s.replace(',', '\x00').replace('.', ',').replace('\x00', '.')

    NAVY   = colors.HexColor('#0A2640')
    LGRAY  = colors.HexColor('#DDDDDD')
    BGROW  = colors.HexColor('#F8F9FA')
    MUTED  = colors.HexColor('#888888')

    styles = getSampleStyleSheet()

    def _ps(name, **kw):
        return ParagraphStyle(name, parent=styles['Normal'], **kw)

    st_title    = _ps('od_title',   fontSize=15, fontName='Helvetica-Bold',
                      textColor=NAVY, alignment=TA_CENTER, spaceAfter=2)
    st_sub      = _ps('od_sub',     fontSize=9,  textColor=MUTED,
                      alignment=TA_CENTER, spaceAfter=0)
    st_section  = _ps('od_sec',     fontSize=9,  fontName='Helvetica-Bold',
                      textColor=NAVY, spaceBefore=8, spaceAfter=4)
    st_normal   = _ps('od_normal',  fontSize=9,  leading=13)
    st_label    = _ps('od_label',   fontSize=7,  textColor=MUTED, leading=11)
    st_footer   = _ps('od_footer',  fontSize=7,  textColor=MUTED, alignment=TA_CENTER)
    st_th       = _ps('od_th',      fontSize=8,  fontName='Helvetica-Bold',
                      textColor=colors.white, alignment=TA_CENTER)
    st_td_c     = _ps('od_td_c',    fontSize=8,  alignment=TA_CENTER)
    st_td_l     = _ps('od_td_l',    fontSize=8)
    st_td_g     = _ps('od_td_g',    fontSize=7,  textColor=colors.HexColor('#666666'))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )
    elements = []

    elements.append(Paragraph("ROSELAR MÓVEIS", st_title))
    elements.append(Paragraph("PEDIDO DE COMPRA", st_sub))
    elements.append(Spacer(1, 0.3*cm))
    elements.append(HRFlowable(width="100%", thickness=2, color=NAVY))
    elements.append(Spacer(1, 0.4*cm))

    if order.quote:
        seller_name = order.quote.seller.get_full_name() or order.quote.seller.username
    else:
        seller_name = "Compra da Loja"
    prazo = order.delivery_deadline.strftime('%d/%m/%Y') if order.delivery_deadline else '—'
    meta_data = [
        [
            Paragraph(f"<b>Pedido:</b> #{order.number}", st_normal),
            Paragraph(f"<b>Data:</b> {order.created_at.strftime('%d/%m/%Y')}", st_normal),
        ],
        [
            Paragraph(f"<b>Vendedor:</b> {seller_name}", st_normal),
            Paragraph(f"<b>Prazo de entrega:</b> {prazo}", st_normal),
        ],
        [
            Paragraph(f"<b>Transportadora:</b> {order.transport_info or '—'}", st_normal),
            Paragraph(f"<b>Cond. Pagamento:</b> {order.purchase_condition_text or '—'}", st_normal),
        ],
    ]
    meta_tbl = Table(meta_data, colWidths=[8.5*cm, 8.5*cm])
    meta_tbl.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',   (0, 0), (-1, -1), 0),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(meta_tbl)
    elements.append(Spacer(1, 0.4*cm))

    supplier_cell = []
    if order.supplier:
        supplier_cell.append(Paragraph(f"<b>Fornecedor</b>", st_section))
        supplier_cell.append(Paragraph(order.supplier.name, st_normal))
        if order.supplier.phone:
            supplier_cell.append(Paragraph(f"Tel: {order.supplier.phone}", st_label))
        if order.supplier.email:
            supplier_cell.append(Paragraph(order.supplier.email, st_label))
    else:
        supplier_cell.append(Paragraph("—", st_normal))

    client_cell = [
        Paragraph("<b>Cliente</b>", st_section),
        Paragraph(order.quote.customer.name if order.quote else "Estoque da Loja", st_normal),
    ]

    party_tbl = Table([[supplier_cell, client_cell]], colWidths=[8.5*cm, 8.5*cm])
    party_tbl.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('BOX',           (0, 0), (0, 0),   0.5, LGRAY),
        ('BOX',           (1, 0), (1, 0),   0.5, LGRAY),
        ('BACKGROUND',    (0, 0), (0, 0),   BGROW),
        ('BACKGROUND',    (1, 0), (1, 0),   BGROW),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(party_tbl)
    elements.append(Spacer(1, 0.5*cm))

    elements.append(Paragraph("ITENS DO PEDIDO", st_section))

    items_qs = items_qs_pre  # already fetched above

    hdr = [
        Paragraph('#',          st_th),
        Paragraph('Produto',    st_th),
        Paragraph('Descrição',  st_th),
        Paragraph('Qtd',        st_th),
        Paragraph('Vlr. Unit.', st_th),
        Paragraph('Total',      st_th),
    ]
    table_data = [hdr]

    for idx, item in enumerate(items_qs, 1):
        desc = item.description.strip() if item.description else '—'
        unit = manual_prices.get(item.id, Decimal('0.00'))
        total_item = unit * item.quantity
        table_data.append([
            Paragraph(str(idx),                    st_td_c),
            Paragraph(item.product_name,           st_td_l),
            Paragraph(desc,                        st_td_g),
            Paragraph(str(item.quantity),          st_td_c),
            Paragraph(f"R$ {_fmt_brl(unit)}",      st_td_c),
            Paragraph(f"R$ {_fmt_brl(total_item)}", st_td_c),
        ])

    col_widths = [0.8*cm, 4.5*cm, 6.5*cm, 1.2*cm, 2.5*cm, 2.5*cm]
    items_tbl = Table(table_data, colWidths=col_widths, repeatRows=1)

    row_styles = [
        ('BACKGROUND',    (0, 0), (-1, 0),  NAVY),
        ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, 0),  8),
        ('TOPPADDING',    (0, 0), (-1, 0),  7),
        ('BOTTOMPADDING', (0, 0), (-1, 0),  7),
        ('GRID',          (0, 0), (-1, -1), 0.5, LGRAY),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ('LEFTPADDING',   (0, 1), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 1), (-1, -1), 5),
    ]
    for i in range(1, len(table_data)):
        if i % 2 == 0:
            row_styles.append(('BACKGROUND', (0, i), (-1, i), BGROW))

    items_tbl.setStyle(TableStyle(row_styles))
    elements.append(items_tbl)
    elements.append(Spacer(1, 0.3*cm))

    grand_total = sum(manual_prices.get(it.id, Decimal('0.00')) * it.quantity for it in items_qs)
    total_tbl = Table(
        [[Paragraph(f"<b>Total do pedido:</b> R$ {_fmt_brl(grand_total)}", st_normal)]],
        colWidths=[17*cm],
    )
    total_tbl.setStyle(TableStyle([
        ('ALIGN',         (0, 0), (-1, -1), 'RIGHT'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
    ]))
    elements.append(total_tbl)

    if order.notes:
        elements.append(Spacer(1, 0.4*cm))
        st_obs_lbl = _ps('od_obs_lbl', fontSize=8, fontName='Helvetica-Bold',
                         textColor=NAVY, spaceBefore=4, spaceAfter=3)
        st_obs_txt = _ps('od_obs_txt', fontSize=8, leading=12,
                         textColor=colors.HexColor('#333333'))
        obs_tbl = Table(
            [[
                [Paragraph("OBSERVAÇÕES", st_obs_lbl),
                 Paragraph(order.notes, st_obs_txt)]
            ]],
            colWidths=[17*cm],
        )
        obs_tbl.setStyle(TableStyle([
            ('BOX',           (0, 0), (-1, -1), 0.5, LGRAY),
            ('BACKGROUND',    (0, 0), (-1, -1), BGROW),
            ('LEFTPADDING',   (0, 0), (-1, -1), 8),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
            ('TOPPADDING',    (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(obs_tbl)

    elements.append(Spacer(1, 0.8*cm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=LGRAY))
    elements.append(Spacer(1, 0.2*cm))
    elements.append(Paragraph(
        f"Gerado em {timezone.localdate().strftime('%d/%m/%Y')} | Roselar Móveis",
        st_footer,
    ))

    try:
        doc.build(elements)
    except Exception:
        logger.exception('Erro ao gerar PDF do pedido %s', order.number)
        raise

    pdf = buffer.getvalue()
    buffer.close()

    supplier_name = order.supplier.name if order.supplier else 'sem_fornecedor'
    filename = f'pedido_{order.number}_{supplier_name}.pdf'
    filename = filename.replace(' ', '_').replace('/', '_')
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = _safe_content_disposition(filename)
    return response


@login_required
@require_http_methods(["GET", "POST"])
def quote_simulate_commission(request: HttpRequest, quote_id: int) -> HttpResponse:
    quote = get_object_or_404(
        Quote.objects.select_related('customer', 'seller'), id=quote_id
    )
    if not _can_access_all_quotes(request.user) and quote.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    subtotal = quote.calculate_subtotal()
    # Frete por conta da loja (STORE) não é repassado ao cliente: fica fora do
    # total e da comissão. billable_freight já zera esse caso.
    freight_value = quote.billable_freight

    if request.method == "POST":
        sim_payment_type    = request.POST.get('sim_payment_type', '') or ''
        sim_has_architect   = request.POST.get('sim_has_architect') == '1'
        sim_architect_id    = request.POST.get('sim_architect_id', '') or ''
        try:
            sim_discount = Decimal(request.POST.get('discount_percent', '0') or '0')
        except Exception:
            sim_discount = Decimal('0')
        try:
            price_increase_pct = Decimal(request.POST.get('price_increase_percent', '0') or '0')
        except Exception:
            price_increase_pct = Decimal('0')
        sim_installments    = max(1, int(request.POST.get('sim_installments', '1') or 1))
        sim_payment_type_2 = request.POST.get('sim_payment_type_2', '') or ''
        sim_installments_2 = max(1, int(request.POST.get('sim_installments_2', '1') or 1))
        try:
            price_increase_pct_2 = Decimal(request.POST.get('price_increase_percent_2', '0') or '0')
        except Exception:
            price_increase_pct_2 = Decimal('0')
        try:
            _sa = request.POST.get('sim_split_amount', '') or ''
            sim_split_amount = Decimal(_sa) if _sa else None
            if sim_split_amount is not None and sim_split_amount <= 0:
                sim_split_amount = None
        except Exception:
            sim_split_amount = None
        try:
            _dp = request.POST.get('down_payment_value', '') or ''
            down_payment_value = Decimal(_dp) if _dp else None
            if down_payment_value is not None and down_payment_value <= 0:
                down_payment_value = None
        except Exception:
            down_payment_value = None
    else:
        sim_payment_type    = quote.payment_type or ''
        sim_has_architect   = quote.has_architect
        sim_architect_id    = str(quote.architect_id or '')
        sim_discount        = quote.discount_percent or Decimal("0")
        price_increase_pct  = quote.price_increase_percent or Decimal('0')
        sim_installments    = quote.payment_installments or 1
        sim_payment_type_2  = quote.payment_type_2 or ''
        sim_installments_2  = quote.payment_installments_2 or 1
        sim_split_amount    = quote.payment_split_amount
        price_increase_pct_2 = Decimal('0')
        down_payment_value  = quote.down_payment_value or None

    from core.models import Architect
    selected_architect = None
    if sim_architect_id:
        try:
            selected_architect = Architect.objects.get(pk=int(sim_architect_id))
        except (Architect.DoesNotExist, ValueError, TypeError):
            pass

    ctx = _build_simulation_context(
        subtotal=subtotal,
        freight_value=freight_value,
        sim_payment_type=sim_payment_type,
        sim_has_architect=sim_has_architect,
        sim_discount=sim_discount,
        price_increase_pct=price_increase_pct,
        sim_installments=sim_installments,
        sim_payment_type_2=sim_payment_type_2,
        sim_installments_2=sim_installments_2,
        sim_split_amount=sim_split_amount,
        price_increase_pct_2=price_increase_pct_2,
        down_payment_value=down_payment_value,
    )

    save_session_key = f"quote_sim_saved_{quote.id}"
    quote_actions_unlocked = bool(request.session.get(save_session_key, False))

    if request.method == "POST" and request.POST.get('action') == 'save_conditions':
        if ctx['margin_limit_exceeded']:
            messages.error(request, "Condições bloqueadas. Ajuste o preço antes de salvar.")
            ctx['quote'] = quote
            ctx['quote_actions_unlocked'] = quote_actions_unlocked
            ctx['selected_architect'] = selected_architect
            ctx['sim_architect_id']   = sim_architect_id
            ctx['can_view_commission'] = _can_view_commission(request.user)
            ctx['is_admin'] = _is_admin(request.user)
            return render(request, 'sales/quote_simulation.html', ctx)
        with transaction.atomic():
            quote.discount_percent       = ctx['discount_percent']
            quote.price_increase_percent = ctx['price_increase_pct']
            quote.payment_type           = ctx['sim_payment_type']
            quote.payment_installments   = ctx['sim_installments']
            quote.payment_fee_percent    = ctx['payment_fee_percent']
            if ctx['split_mode']:
                quote.payment_type_2         = ctx['sim_payment_type_2']
                quote.payment_installments_2 = ctx['sim_installments_2']
                quote.payment_fee_percent_2  = ctx['payment_fee_percent_2']
                quote.payment_split_amount   = ctx['split_amount_1']
            else:
                quote.payment_type_2         = ''
                quote.payment_installments_2 = 1
                quote.payment_fee_percent_2  = Decimal("0.00")
                quote.payment_split_amount   = None
            quote.has_architect          = ctx['sim_has_architect']
            if not ctx['sim_has_architect']:
                quote.architect = None
            # A entrada faz parte das condições da venda: sem gravá-la o motor
            # não consegue reproduzir depois a comissão mostrada nesta tela.
            quote.down_payment_value = ctx['down_payment_value'] or Decimal("0.00")
            quote.save()
            # Orçamento já vendido: as condições mudaram, a comissão congelada
            # precisa acompanhar.
            if quote.status in SOLD_STATUSES:
                persist_quote_commission(quote)
        request.session[save_session_key] = True
        messages.success(request, f"Condições do orçamento {quote.number} salvas com sucesso.")
        return redirect("sales:quote_detail", quote_id=quote.id)

    if request.method == "POST" and not request.POST.get('_ajax'):
        return redirect(request.path)

    ctx['quote'] = quote
    ctx['selected_architect'] = selected_architect
    ctx['sim_architect_id']   = sim_architect_id
    ctx['quote_actions_unlocked'] = quote_actions_unlocked
    ctx['can_view_commission'] = _can_view_commission(request.user)
    ctx['is_admin'] = _is_admin(request.user)
    return render(request, 'sales/quote_simulation.html', ctx)

@login_required
@require_http_methods(["GET", "POST"])
def standalone_simulation(request: HttpRequest) -> HttpResponse:
    from core.models import Customer, Architect

    if request.method == "POST":
        try:
            subtotal = Decimal(request.POST.get('sim_subtotal', '0') or '0')
        except Exception:
            subtotal = Decimal('0')
        try:
            freight_value = Decimal(request.POST.get('sim_freight', '0') or '0')
        except Exception:
            freight_value = Decimal('0')
        sim_payment_type  = request.POST.get('sim_payment_type', '') or ''
        sim_has_architect = request.POST.get('sim_has_architect') == '1'
        try:
            sim_discount = Decimal(request.POST.get('discount_percent', '0') or '0')
        except Exception:
            sim_discount = Decimal('0')
        try:
            price_increase_pct = Decimal(request.POST.get('price_increase_percent', '0') or '0')
        except Exception:
            price_increase_pct = Decimal('0')
        sim_installments = max(1, int(request.POST.get('sim_installments', '1') or 1))
        customer_id = request.POST.get('sim_customer_id', '') or ''
        sim_architect_id = request.POST.get('sim_architect_id', '') or ''
        sim_payment_type_2 = request.POST.get('sim_payment_type_2', '') or ''
        sim_installments_2 = max(1, int(request.POST.get('sim_installments_2', '1') or 1))
        try:
            price_increase_pct_2 = Decimal(request.POST.get('price_increase_percent_2', '0') or '0')
        except Exception:
            price_increase_pct_2 = Decimal('0')
        try:
            _sa = request.POST.get('sim_split_amount', '') or ''
            sim_split_amount = Decimal(_sa) if _sa else None
            if sim_split_amount is not None and sim_split_amount <= 0:
                sim_split_amount = None
        except Exception:
            sim_split_amount = None
        try:
            _dp = request.POST.get('down_payment_value', '') or ''
            down_payment_value = Decimal(_dp) if _dp else None
            if down_payment_value is not None and down_payment_value <= 0:
                down_payment_value = None
        except Exception:
            down_payment_value = None
    else:
        subtotal           = Decimal('0')
        freight_value      = Decimal('0')
        sim_payment_type   = ''
        sim_has_architect  = False
        sim_discount       = Decimal('0')
        price_increase_pct = Decimal('0')
        sim_installments   = 1
        customer_id        = ''
        sim_architect_id   = ''
        sim_payment_type_2   = ''
        sim_installments_2   = 1
        sim_split_amount     = None
        price_increase_pct_2 = Decimal('0')
        down_payment_value   = None

    subtotal      = max(Decimal('0'), subtotal)
    freight_value = max(Decimal('0'), freight_value)

    if request.method == "POST" and not request.POST.get('_ajax'):
        return redirect(request.path)

    selected_customer = None
    if customer_id:
        try:
            selected_customer = Customer.objects.get(pk=int(customer_id))
        except (Customer.DoesNotExist, ValueError, TypeError):
            pass

    selected_architect = None
    if sim_architect_id:
        try:
            selected_architect = Architect.objects.get(pk=int(sim_architect_id))
        except (Architect.DoesNotExist, ValueError, TypeError):
            pass

    ctx = _build_simulation_context(
        subtotal=subtotal,
        freight_value=freight_value,
        sim_payment_type=sim_payment_type,
        sim_has_architect=sim_has_architect,
        sim_discount=sim_discount,
        price_increase_pct=price_increase_pct,
        sim_installments=sim_installments,
        sim_payment_type_2=sim_payment_type_2,
        sim_installments_2=sim_installments_2,
        sim_split_amount=sim_split_amount,
        price_increase_pct_2=price_increase_pct_2,
        down_payment_value=down_payment_value,
    )
    ctx['standalone']         = True
    ctx['sim_subtotal']       = subtotal
    ctx['sim_freight']        = freight_value
    ctx['selected_customer']  = selected_customer
    ctx['sim_customer_id']    = customer_id
    ctx['selected_architect'] = selected_architect
    ctx['sim_architect_id']   = sim_architect_id
    ctx['can_view_commission'] = _can_view_commission(request.user)

    return render(request, 'sales/standalone_simulation.html', ctx)

@login_required
@require_http_methods(["POST"])
def quote_duplicate(request, quote_id):
    original = get_object_or_404(Quote, id=quote_id)
    if not _is_staff_or_admin(request.user) and original.seller_id != request.user.id:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")
    from core.models import AuditLog, AuditAction

    with transaction.atomic():
        new_number = generate_next_quote_number()
        new_quote = Quote.objects.create(
            number=new_number,
            customer=original.customer,
            seller=request.user,
            status=QuoteStatus.DRAFT,
            quote_date=timezone.localdate(),
            freight_value=original.freight_value,
            freight_responsible=original.freight_responsible,
            shipping_company=original.shipping_company,
            shipping_payment_method=original.shipping_payment_method,
            discount_percent=original.discount_percent,
            price_increase_percent=original.price_increase_percent,
            has_architect=original.has_architect,
            architect=original.architect,
            payment_type=original.payment_type,
            payment_installments=original.payment_installments,
            payment_fee_percent=original.payment_fee_percent,
            payment_type_2=original.payment_type_2,
            payment_installments_2=original.payment_installments_2,
            payment_fee_percent_2=original.payment_fee_percent_2,
            payment_split_amount=original.payment_split_amount,
            total_override=original.total_override,
            total_rounding_mode=original.total_rounding_mode,
            total_manual_adjustment=original.total_manual_adjustment,
        )

        for item in original.items.all():
            QuoteItem.objects.create(
                quote=new_quote,
                supplier=item.supplier,
                product_name=item.product_name,
                description=item.description,
                quantity=item.quantity,
                unit_value=item.unit_value,
                condition_text=item.condition_text,
                architect_percent=item.architect_percent,
            )

    AuditLog.log(request.user, AuditAction.CREATE_QUOTE,
                 f"Orçamento duplicado: {original.number} → {new_quote.number}", obj=new_quote)

    messages.success(request, f"Orçamento duplicado: {new_quote.number}")
    return redirect("sales:quote_edit", quote_id=new_quote.id)

@login_required
@require_http_methods(["POST"])
def quote_delete(request, quote_id):
    from django.db.models.deletion import ProtectedError

    if not _is_admin(request.user):
        messages.error(request, "Apenas administradores podem excluir orçamentos.")
        return redirect("sales:quote_detail", quote_id=quote_id)

    quote = get_object_or_404(Quote, id=quote_id)
    number = quote.number
    try:
        with transaction.atomic():
            deleted_orders = quote.orders.count()
            quote.orders.all().delete()
            quote.delete()
    except ProtectedError:
        messages.error(
            request,
            "Não foi possível excluir o orçamento por dependências vinculadas.",
        )
        return redirect("sales:quote_detail", quote_id=quote_id)

    from core.models import AuditLog, AuditAction
    AuditLog.log(request.user, AuditAction.DELETE_QUOTE,
                 f"Orçamento excluído: {number} (pedidos removidos: {deleted_orders})", obj=None)
    if deleted_orders:
        messages.success(request, f"Orçamento {number} excluído com sucesso (incluindo {deleted_orders} pedido(s) vinculado(s)).")
    else:
        messages.success(request, f"Orçamento {number} excluído com sucesso.")
    return redirect("sales:quote_list")


@login_required
@require_http_methods(["POST"])
def quotes_bulk_delete(request: HttpRequest) -> HttpResponse:
    from django.db.models.deletion import ProtectedError
    from core.models import AuditLog, AuditAction

    if not _is_admin(request.user):
        messages.error(request, "Apenas administradores podem excluir orçamentos.")
        return redirect("sales:quote_list")

    ids_raw = request.POST.getlist("quote_ids")
    if not ids_raw:
        messages.warning(request, "Nenhum orçamento selecionado.")
        return redirect("sales:quote_list")

    # Validate all values are integers to prevent injection
    try:
        ids = [int(i) for i in ids_raw]
    except (ValueError, TypeError):
        messages.error(request, "Seleção inválida.")
        return redirect("sales:quote_list")

    quotes = Quote.objects.filter(id__in=ids)
    deleted_count = 0
    error_count = 0
    numbers = []

    for quote in quotes:
        try:
            with transaction.atomic():
                number = quote.number
                deleted_orders = quote.orders.count()
                quote.orders.all().delete()
                quote.delete()
                AuditLog.log(
                    request.user, AuditAction.DELETE_QUOTE,
                    f"Orçamento excluído (bulk): {number} (pedidos removidos: {deleted_orders})",
                    obj=None,
                )
                numbers.append(number)
                deleted_count += 1
        except ProtectedError:
            error_count += 1

    if deleted_count:
        messages.success(request, f"{deleted_count} orçamento(s) excluído(s) com sucesso: {', '.join(numbers)}.")
    if error_count:
        messages.error(request, f"{error_count} orçamento(s) não puderam ser excluídos por dependências vinculadas.")

    return redirect("sales:quote_list")


# ── Documentos / Notas Fiscais da venda ───────────────────────────────────────
_ALLOWED_DOC_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp")
_MAX_DOC_SIZE = 15 * 1024 * 1024  # 15 MB


@login_required
@require_http_methods(["GET", "POST"])
def quote_documents(request: HttpRequest, quote_id: int) -> HttpResponse:
    """Lista e upload de documentos (NF compra/cliente/outro) da venda."""
    quote, forbidden = _get_quote_or_403(request, quote_id)
    if forbidden:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    if request.method == "POST":
        doc_type = request.POST.get("doc_type", "").strip()
        valid_types = {c[0] for c in SaleDocumentType.choices}
        if doc_type not in valid_types:
            messages.error(request, "Tipo de documento inválido.")
            return redirect("sales:quote_documents", quote_id=quote.id)

        files = request.FILES.getlist("files")
        if not files:
            messages.error(request, "Selecione ao menos um arquivo.")
            return redirect("sales:quote_documents", quote_id=quote.id)

        supplier_id = request.POST.get("supplier") or None
        supplier = None
        if supplier_id and doc_type == SaleDocumentType.NF_COMPRA:
            from core.models import Supplier
            supplier = Supplier.objects.filter(pk=supplier_id).first()

        description = request.POST.get("description", "").strip()

        created = 0
        for f in files:
            name = (f.name or "").lower()
            if not name.endswith(_ALLOWED_DOC_EXTS):
                messages.warning(request, f"Arquivo ignorado (formato não permitido): {f.name}")
                continue
            if f.size > _MAX_DOC_SIZE:
                messages.warning(request, f"Arquivo ignorado (maior que 15 MB): {f.name}")
                continue
            SaleDocument.objects.create(
                quote=quote,
                doc_type=doc_type,
                supplier=supplier,
                file=f,
                description=description,
                uploaded_by=request.user,
            )
            created += 1

        if created:
            messages.success(request, f"{created} documento(s) anexado(s).")
        return redirect("sales:quote_documents", quote_id=quote.id)

    documents = quote.documents.select_related("supplier", "uploaded_by").all()
    from core.models import Supplier
    context = {
        "quote": quote,
        "nf_compra": [d for d in documents if d.doc_type == SaleDocumentType.NF_COMPRA],
        "nf_cliente": [d for d in documents if d.doc_type == SaleDocumentType.NF_CLIENTE],
        "outros": [d for d in documents if d.doc_type == SaleDocumentType.OUTRO],
        "suppliers": Supplier.objects.order_by("name"),
        "is_admin": _is_admin(request.user),
    }
    return render(request, "sales/quote_documents.html", context)


@login_required
@require_http_methods(["POST"])
def quote_document_delete(request: HttpRequest, quote_id: int, doc_id: int) -> HttpResponse:
    """Remove um documento da venda (arquivo físico + registro)."""
    quote, forbidden = _get_quote_or_403(request, quote_id)
    if forbidden:
        messages.error(request, "Acesso negado.")
        return redirect("sales:quote_list")

    document = get_object_or_404(SaleDocument, pk=doc_id, quote=quote)
    if document.file:
        try:
            document.file.delete(save=False)
        except Exception:
            logger.warning("Falha ao remover arquivo do documento %s.", document.pk, exc_info=True)
    document.delete()

    messages.success(request, "Documento removido.")
    return redirect("sales:quote_documents", quote_id=quote.id)
