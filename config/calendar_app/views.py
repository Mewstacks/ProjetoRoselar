from __future__ import annotations

import calendar
import json
import logging
import re
import unicodedata
from urllib.parse import quote as url_quote
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role, User

from .models import (
    CalendarEvent,
    EventAttachment,
    EventStatus,
    EventTag,
    EventType,
    Reminder,
    ReminderStatus,
    TagColor,
)



def _is_admin(user: User) -> bool:
    """Verifica se o usuário é admin (pode ver todos os calendários)."""
    return user.role == Role.ADMIN or user.is_superuser


def _get_events_qs(user: User):
    """
    Retorna queryset de eventos filtrado por permissão:
    - Admin/Dono: todos os eventos
    - Financeiro: apenas seus próprios eventos (entregas e pagamentos)
    - Vendedor: somente os seus eventos
    """
    qs = CalendarEvent.objects.select_related(
        "assigned_to", "quote", "order", "customer"
    ).prefetch_related("tags", "attachments")
    if _is_admin(user):
        return qs
    base_qs = qs.filter(assigned_to=user)
    if user.role == Role.FINANCE:
        return base_qs.filter(
            event_type__in=[EventType.DELIVERY, EventType.ARCHITECT_PAYMENT, EventType.CUSTOM]
        )
    return base_qs


@login_required
def calendar_view(request: HttpRequest) -> HttpResponse:
    """Página principal do calendário com visualização mensal."""
    today = timezone.localdate()

    # Pegar mês/ano da query string ou usar o atual
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
    except (ValueError, TypeError):
        year, month = today.year, today.month

    # Validar limites
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1

    # Filtro por vendedor (apenas para admins)
    seller_filter = request.GET.get("seller")
    sellers = []
    if _is_admin(request.user):
        sellers = User.objects.filter(is_active=True).order_by("first_name", "username")

    # Construir calendário
    cal = calendar.Calendar(firstweekday=6)  # Domingo primeiro
    month_days = cal.monthdayscalendar(year, month)

    # Buscar eventos do mês
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    events_qs = _get_events_qs(request.user).filter(
        event_date__gte=first_day,
        event_date__lte=last_day,
    ).exclude(status=EventStatus.CANCELED)

    if _is_admin(request.user) and seller_filter:
        try:
            events_qs = events_qs.filter(assigned_to_id=int(seller_filter))
        except (ValueError, TypeError):
            pass

    # Agrupar eventos por dia
    events_by_day: dict[int, list] = {}
    for event in events_qs:
        day = event.event_date.day
        events_by_day.setdefault(day, []).append(event)

    # Construir semanas com dados
    weeks = []
    for week in month_days:
        week_data = []
        for day in week:
            if day == 0:
                week_data.append({"day": 0, "events": [], "is_today": False})
            else:
                current = date(year, month, day)
                day_events = events_by_day.get(day, [])
                week_data.append({
                    "day": day,
                    "events": day_events,
                    "is_today": current == today,
                    "is_past": current < today,
                })
        weeks.append(week_data)

    # Navegação mês anterior / próximo
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year

    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year

    month_name = [
        "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
    ][month]

    # Lembretes não lidos do usuário (para hoje)
    today_reminders = Reminder.objects.filter(
        event__assigned_to=request.user,
        remind_date__lte=today,
        read=False,
        status=ReminderStatus.SCHEDULED,
    ).select_related("event")[:10]

    # Todas as tags disponíveis (para o popover de etiquetas)
    all_tags = EventTag.objects.all()
    tag_colors = TagColor.choices  # [("#61bd4f", "Verde"), ...]

    context = {
        "weeks": weeks,
        "month": month,
        "year": year,
        "month_name": month_name,
        "prev_month": prev_month,
        "prev_year": prev_year,
        "next_month": next_month,
        "next_year": next_year,
        "today": today,
        "sellers": sellers,
        "seller_filter": seller_filter,
        "is_admin": _is_admin(request.user),
        "today_reminders": today_reminders,
        "weekday_names": ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"],
        "all_tags": all_tags,
        "tag_colors": tag_colors,
        "event_types": EventType.choices,
    }
    return render(request, "calendar_app/calendar.html", context)


@login_required
def upcoming_events(request: HttpRequest) -> HttpResponse:
    """Lista de próximos eventos (7, 15 ou 30 dias)."""
    today = timezone.localdate()

    try:
        days = int(request.GET.get("days", 7))
    except (ValueError, TypeError):
        days = 7
    if days not in (7, 15, 30):
        days = 7

    end_date = today + timedelta(days=days)

    events_qs = _get_events_qs(request.user).filter(
        event_date__gte=today,
        event_date__lte=end_date,
    ).exclude(status=EventStatus.CANCELED)

    # Filtro por vendedor (admins)
    seller_filter = request.GET.get("seller")
    sellers = []
    if _is_admin(request.user):
        sellers = User.objects.filter(is_active=True).order_by("first_name", "username")
        if seller_filter:
            try:
                events_qs = events_qs.filter(assigned_to_id=int(seller_filter))
            except (ValueError, TypeError):
                pass

    # Filtro por tipo
    event_type = request.GET.get("type")
    if event_type and event_type in dict(EventType.choices):
        events_qs = events_qs.filter(event_type=event_type)

    context = {
        "events": events_qs,
        "days": days,
        "today": today,
        "end_date": end_date,
        "sellers": sellers,
        "seller_filter": seller_filter,
        "event_type": event_type,
        "is_admin": _is_admin(request.user),
        "event_types": EventType.choices,
    }
    return render(request, "calendar_app/upcoming.html", context)


@login_required
def overdue_events(request: HttpRequest) -> HttpResponse:
    """Lista de entregas/eventos atrasados."""
    today = timezone.localdate()

    events_qs = _get_events_qs(request.user).filter(
        event_date__lt=today,
        status=EventStatus.PENDING,
    )

    seller_filter = request.GET.get("seller")
    sellers = []
    if _is_admin(request.user):
        sellers = User.objects.filter(is_active=True).order_by("first_name", "username")
        if seller_filter:
            try:
                events_qs = events_qs.filter(assigned_to_id=int(seller_filter))
            except (ValueError, TypeError):
                pass

    context = {
        "events": events_qs,
        "today": today,
        "sellers": sellers,
        "seller_filter": seller_filter,
        "is_admin": _is_admin(request.user),
    }
    return render(request, "calendar_app/overdue.html", context)


@login_required
def event_detail(request: HttpRequest, event_id: int) -> HttpResponse:
    """Detalhes de um evento específico."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    reminders = event.reminders.all()

    context = {
        "event": event,
        "reminders": reminders,
        "is_admin": _is_admin(request.user),
    }
    return render(request, "calendar_app/event_detail.html", context)


@login_required
@require_POST
def event_mark_done(request: HttpRequest, event_id: int) -> HttpResponse:
    """Marca evento como concluído."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    event.mark_done()
    # Dispensar lembretes pendentes
    event.reminders.filter(status=ReminderStatus.SCHEDULED).update(
        status=ReminderStatus.DISMISSED, read=True, read_at=timezone.now()
    )
    return redirect("calendar_app:event_detail", event_id=event.pk)


@login_required
@require_POST
def event_mark_canceled(request: HttpRequest, event_id: int) -> HttpResponse:
    """Marca evento como cancelado."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    event.mark_canceled()
    event.reminders.filter(status=ReminderStatus.SCHEDULED).update(
        status=ReminderStatus.DISMISSED, read=True, read_at=timezone.now()
    )
    return redirect("calendar_app:event_detail", event_id=event.pk)


@login_required
@require_POST
def reminder_dismiss(request: HttpRequest, reminder_id: int) -> JsonResponse:
    """Dispensa um lembrete via AJAX."""
    reminder = get_object_or_404(Reminder, pk=reminder_id, event__assigned_to=request.user)
    reminder.dismiss()
    return JsonResponse({"success": True})


@login_required
@require_POST
def reminder_mark_read(request: HttpRequest, reminder_id: int) -> JsonResponse:
    """Marca lembrete como lido via AJAX."""
    reminder = get_object_or_404(Reminder, pk=reminder_id, event__assigned_to=request.user)
    reminder.mark_as_read()
    return JsonResponse({"success": True})


@login_required
def create_event(request: HttpRequest) -> HttpResponse:
    """Criar evento personalizado."""
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        event_date_str = request.POST.get("event_date", "")
        event_type = request.POST.get("event_type", EventType.CUSTOM)

        if not title or not event_date_str:
            return render(request, "calendar_app/create_event.html", {
                "error": "Título e data são obrigatórios.",
                "event_types": EventType.choices,
                "is_admin": _is_admin(request.user),
                "selected_reminders": request.POST.getlist("reminders"),
            })

        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            return render(request, "calendar_app/create_event.html", {
                "error": "Data inválida.",
                "event_types": EventType.choices,
                "is_admin": _is_admin(request.user),
                "selected_reminders": request.POST.getlist("reminders"),
            })

        event = CalendarEvent.objects.create(
            title=title,
            description=description,
            event_type=event_type,
            event_date=event_date,
            assigned_to=request.user,
        )

        # Criar lembretes escolhidos pelo vendedor
        reminder_days = request.POST.getlist("reminders")  # ['7','3','1','0', etc.]
        # Lembrete personalizado
        if request.POST.get("reminder_custom_check"):
            custom_days = request.POST.get("reminder_custom_days", "")
            if custom_days.isdigit() and int(custom_days) > 0:
                reminder_days.append(custom_days)

        # Se nenhum lembrete selecionado, usar padrão (3, 1, 0)
        if not reminder_days:
            reminder_days = ["3", "1", "0"]

        # Mapping for human-readable messages
        _msg_map = {
            0: "HOJE", 1: "amanhã", 3: "em 3 dias", 7: "em 7 dias",
            14: "em 14 dias", 30: "em 30 dias",
        }

        for d in reminder_days:
            try:
                days_before = int(d)
            except (ValueError, TypeError):
                continue
            remind_date = event_date - timedelta(days=days_before)
            if remind_date >= timezone.localdate():
                msg = _msg_map.get(days_before, f"em {days_before} dias")
                Reminder.objects.create(
                    event=event,
                    remind_date=remind_date,
                    message=f"{title} - {msg}",
                )

        return redirect("calendar_app:event_detail", event_id=event.pk)

    context = {
        "event_types": EventType.choices,
        "is_admin": _is_admin(request.user),
        "selected_reminders": [],
    }
    return render(request, "calendar_app/create_event.html", context)


@login_required
def reminders_api(request: HttpRequest) -> JsonResponse:
    """API para buscar lembretes não lidos (para badge no navbar)."""
    today = timezone.localdate()
    count = Reminder.objects.filter(
        event__assigned_to=request.user,
        remind_date__lte=today,
        read=False,
        status=ReminderStatus.SCHEDULED,
    ).count()

    reminders = Reminder.objects.filter(
        event__assigned_to=request.user,
        remind_date__lte=today,
        read=False,
        status=ReminderStatus.SCHEDULED,
    ).select_related("event")[:5]

    data = {
        "count": count,
        "reminders": [
            {
                "id": r.id,
                "message": r.message or r.event.title,
                "event_id": r.event_id,
                "event_date": r.event.event_date.strftime("%d/%m/%Y"),
                "event_type": r.event.get_event_type_display(),
            }
            for r in reminders
        ],
    }
    return JsonResponse(data)


# ---------------------------------------------------------------------------
# API JSON — para popup inline no calendário (estilo Trello)
# ---------------------------------------------------------------------------

def _event_to_dict(event: CalendarEvent, include_admin: bool = False) -> dict:
    """Serializa um CalendarEvent para JSON."""
    attachments = [
        {
            "id": a.id,
            "filename": a.filename,
            "content_type": a.content_type,
            "file_size": a.file_size,
            "file_size_display": a.file_size_display,
            "uploaded_at": a.uploaded_at.strftime("%d/%m/%Y %H:%M"),
        }
        for a in event.attachments.all()
    ]
    tags = [
        {
            "id": t.id,
            "name": t.name,
            "color": t.color,
            "text_color": t.text_color,
        }
        for t in event.tags.all()
    ]
    data = {
        "id": event.pk,
        "title": event.title,
        "description": event.description,
        "event_type": event.event_type,
        "event_type_display": event.get_event_type_display(),
        "status": event.status,
        "status_display": event.get_status_display(),
        "event_date": event.event_date.isoformat(),
        "event_date_display": event.event_date.strftime("%d/%m/%Y"),
        "event_time": event.event_time.strftime("%H:%M") if event.event_time else "",
        "is_overdue": event.is_overdue,
        "customer": event.customer.name if event.customer else "",
        "customer_id": event.customer_id,
        "quote_number": event.quote.number if event.quote else "",
        "order_number": event.order.number if event.order else "",
        "seller": event.assigned_to.get_full_name() or event.assigned_to.username,
        "seller_id": event.assigned_to_id,
        "attachments": attachments,
        "tags": tags,
    }
    return data


@login_required
def api_event_detail(request: HttpRequest, event_id: int) -> JsonResponse:
    """GET: retorna JSON completo do evento para o popup."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    return JsonResponse(_event_to_dict(event, include_admin=_is_admin(request.user)))


@login_required
@require_POST
def api_event_update(request: HttpRequest, event_id: int) -> JsonResponse:
    """POST: atualiza campos do evento via AJAX (title, description, event_date, event_type, event_time)."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON inválido"}, status=400)

    changed = []
    if "title" in body and body["title"].strip():
        event.title = body["title"].strip()
        changed.append("title")
    if "description" in body:
        event.description = body["description"].strip()
        changed.append("description")
    if "event_date" in body:
        try:
            event.event_date = date.fromisoformat(body["event_date"])
            changed.append("event_date")
        except (ValueError, TypeError):
            return JsonResponse({"error": "Data inválida"}, status=400)
    if "event_type" in body and body["event_type"] in dict(EventType.choices):
        event.event_type = body["event_type"]
        changed.append("event_type")
    if "event_time" in body:
        val = body["event_time"].strip()
        if val:
            try:
                parts = val.split(":")
                from datetime import time as dt_time
                event.event_time = dt_time(int(parts[0]), int(parts[1]))
                changed.append("event_time")
            except (ValueError, IndexError):
                pass
        else:
            event.event_time = None
            changed.append("event_time")

    if changed:
        changed.append("updated_at")
        event.save(update_fields=changed)

    return JsonResponse({"success": True, "event": _event_to_dict(event, _is_admin(request.user))})


@login_required
@require_POST
def api_event_create(request: HttpRequest) -> JsonResponse:
    """POST: cria evento via AJAX e retorna o JSON do evento criado."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON inválido"}, status=400)

    title = body.get("title", "").strip()
    event_date_str = body.get("event_date", "")
    if not title or not event_date_str:
        return JsonResponse({"error": "Título e data são obrigatórios."}, status=400)

    try:
        event_date = date.fromisoformat(event_date_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Data inválida."}, status=400)

    # Resolve optional customer FK
    customer = None
    customer_id = body.get("customer_id")
    if customer_id:
        from core.models import Customer
        try:
            customer = Customer.objects.get(pk=int(customer_id))
        except (Customer.DoesNotExist, ValueError, TypeError):
            pass

    event_type = body.get("event_type", EventType.CUSTOM)
    if event_type not in dict(EventType.choices):
        event_type = EventType.CUSTOM

    event = CalendarEvent.objects.create(
        title=title,
        description=body.get("description", "").strip(),
        event_type=event_type,
        event_date=event_date,
        assigned_to=request.user,
        customer=customer,
    )

    # Lembretes: usa escolha do usuário ou padrão (3, 1, 0)
    reminder_days = body.get("reminders", [3, 1, 0])
    _msg_map = {
        0: "HOJE", 1: "amanhã", 3: "em 3 dias", 7: "em 7 dias",
        14: "em 14 dias", 30: "em 30 dias",
    }
    for d in reminder_days:
        try:
            days_before = int(d)
        except (ValueError, TypeError):
            continue
        remind_date = event_date - timedelta(days=days_before)
        if remind_date >= timezone.localdate():
            msg = _msg_map.get(days_before, f"em {days_before} dias")
            Reminder.objects.create(
                event=event,
                remind_date=remind_date,
                message=f"{title} - {msg}",
            )

    return JsonResponse({"success": True, "event": _event_to_dict(event, _is_admin(request.user))})


@login_required
@require_POST
def api_event_done(request: HttpRequest, event_id: int) -> JsonResponse:
    """POST: marca evento como concluído via AJAX."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    event.mark_done()
    event.reminders.filter(status=ReminderStatus.SCHEDULED).update(
        status=ReminderStatus.DISMISSED, read=True, read_at=timezone.now()
    )
    return JsonResponse({"success": True, "event": _event_to_dict(event, _is_admin(request.user))})


@login_required
@require_POST
def api_event_cancel(request: HttpRequest, event_id: int) -> JsonResponse:
    """POST: marca evento como cancelado via AJAX."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    event.mark_canceled()
    event.reminders.filter(status=ReminderStatus.SCHEDULED).update(
        status=ReminderStatus.DISMISSED, read=True, read_at=timezone.now()
    )
    return JsonResponse({"success": True, "event": _event_to_dict(event, _is_admin(request.user))})


@login_required
@require_POST
def api_event_delete(request: HttpRequest, event_id: int) -> JsonResponse:
    """POST: exclui evento via AJAX."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    event.delete()
    return JsonResponse({"success": True})


@login_required
@require_POST
def api_attachment_upload(request: HttpRequest, event_id: int) -> JsonResponse:
    """POST multipart: faz upload de um arquivo para o evento."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)

    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"error": "Nenhum arquivo enviado."}, status=400)

    # Limitar tamanho (10 MB)
    if uploaded.size > 10 * 1024 * 1024:
        return JsonResponse({"error": "Arquivo muito grande (máx. 10 MB)."}, status=400)

    # Allowlist de extensões — bloqueia HTML/SVG/JS e outros executáveis no navegador
    ALLOWED_EXTS = {
        ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".doc", ".docx", ".xls", ".xlsx", ".txt", ".csv",
    }
    import os as _os
    ext = _os.path.splitext(uploaded.name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return JsonResponse(
            {"error": "Tipo de arquivo não permitido. Use PDF, imagem, Word, Excel, TXT ou CSV."},
            status=400,
        )
    # Nunca confiar no content-type do cliente para tipos perigosos
    safe_ct = uploaded.content_type or "application/octet-stream"
    if "html" in safe_ct or "svg" in safe_ct or "script" in safe_ct:
        safe_ct = "application/octet-stream"

    attachment = EventAttachment.objects.create(
        event=event,
        filename=uploaded.name,
        content_type=safe_ct,
        file_data=uploaded.read(),
        file_size=uploaded.size,
        uploaded_by=request.user,
    )

    return JsonResponse({
        "success": True,
        "attachment": {
            "id": attachment.id,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "file_size": attachment.file_size,
            "file_size_display": attachment.file_size_display,
            "uploaded_at": attachment.uploaded_at.strftime("%d/%m/%Y %H:%M"),
        },
    })


@login_required
def api_attachment_download(request: HttpRequest, attachment_id: int) -> HttpResponse:
    """GET: faz download de um anexo (retorna bytes com Content-Disposition)."""
    attachment = get_object_or_404(EventAttachment, pk=attachment_id)
    # Verificar permissão
    event = attachment.event
    if not _is_admin(request.user) and event.assigned_to != request.user:
        return JsonResponse({"error": "Sem permissão."}, status=403)

    fn = attachment.filename
    nfkd = unicodedata.normalize('NFKD', fn)
    ascii_name = nfkd.encode('ascii', 'ignore').decode('ascii')
    ascii_name = re.sub(r'[^\w.\-]', '_', ascii_name) or 'download'
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{url_quote(fn)}"
    )
    response = HttpResponse(attachment.file_data, content_type=attachment.content_type)
    response["Content-Disposition"] = disposition
    return response


@login_required
@require_POST
def api_attachment_delete(request: HttpRequest, attachment_id: int) -> JsonResponse:
    """POST: exclui um anexo."""
    attachment = get_object_or_404(EventAttachment, pk=attachment_id)
    event = attachment.event
    if not _is_admin(request.user) and event.assigned_to != request.user:
        return JsonResponse({"error": "Sem permissão."}, status=403)
    attachment.delete()
    return JsonResponse({"success": True})


# ---------------------------------------------------------------------------
# Tags API — CRUD de etiquetas (estilo Trello)
# ---------------------------------------------------------------------------

@login_required
def api_tags_list(request: HttpRequest) -> JsonResponse:
    """GET: lista todas as tags disponíveis."""
    tags = EventTag.objects.all()
    data = [
        {
            "id": t.id,
            "name": t.name,
            "color": t.color,
            "text_color": t.text_color,
        }
        for t in tags
    ]
    return JsonResponse({"tags": data})


@login_required
@require_POST
def api_tag_create(request: HttpRequest) -> JsonResponse:
    """POST: cria uma nova tag."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON inválido"}, status=400)

    name = body.get("name", "").strip()
    color = body.get("color", TagColor.GREEN)

    if not name:
        return JsonResponse({"error": "Nome obrigatório."}, status=400)

    if color not in dict(TagColor.choices):
        color = TagColor.GREEN

    tag = EventTag.objects.create(
        name=name,
        color=color,
        created_by=request.user,
    )

    return JsonResponse({
        "success": True,
        "tag": {
            "id": tag.id,
            "name": tag.name,
            "color": tag.color,
            "text_color": tag.text_color,
        },
    })


@login_required
@require_POST
def api_tag_update(request: HttpRequest, tag_id: int) -> JsonResponse:
    """POST: atualiza nome/cor de uma tag."""
    tag = get_object_or_404(EventTag, pk=tag_id)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON inválido"}, status=400)

    if "name" in body and body["name"].strip():
        tag.name = body["name"].strip()
    if "color" in body and body["color"] in dict(TagColor.choices):
        tag.color = body["color"]

    tag.save()

    return JsonResponse({
        "success": True,
        "tag": {
            "id": tag.id,
            "name": tag.name,
            "color": tag.color,
            "text_color": tag.text_color,
        },
    })


@login_required
@require_POST
def api_tag_delete(request: HttpRequest, tag_id: int) -> JsonResponse:
    """POST: exclui uma tag (remove de todos os eventos)."""
    tag = get_object_or_404(EventTag, pk=tag_id)
    tag.delete()
    return JsonResponse({"success": True})


@login_required
@require_POST
def api_event_tag_toggle(request: HttpRequest, event_id: int, tag_id: int) -> JsonResponse:
    """POST: adiciona ou remove uma tag de um evento (toggle)."""
    qs = _get_events_qs(request.user)
    event = get_object_or_404(qs, pk=event_id)
    tag = get_object_or_404(EventTag, pk=tag_id)

    if tag in event.tags.all():
        event.tags.remove(tag)
        action = "removed"
    else:
        event.tags.add(tag)
        action = "added"

    return JsonResponse({
        "success": True,
        "action": action,
        "event": _event_to_dict(event, _is_admin(request.user)),
    })
