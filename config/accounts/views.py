from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.conf import settings

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from core.ratelimit import (
    client_ip,
    clear_attempts,
    is_rate_limited,
    register_failure,
)


def csrf_failure_view(request, reason=""):
    """
    Custom CSRF failure handler: delete the stale csrftoken cookie
    and redirect back so the page loads fresh with a valid token.
    """
    response = redirect(request.path or reverse('accounts:login'))
    response.delete_cookie(
        settings.CSRF_COOKIE_NAME,
        path=settings.CSRF_COOKIE_PATH,
        domain=settings.CSRF_COOKIE_DOMAIN,
    )
    messages.warning(request, 'Sessão expirada. Por favor, tente novamente.')
    return response


@ensure_csrf_cookie
def login(request):
    # Redirect if already logged in
    if request.user.is_authenticated:
        return redirect(reverse('core:index'))
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        # Chave por IP+usuário: não pune um escritório inteiro atrás de um NAT,
        # e só conta falhas (logins bem-sucedidos não gastam o orçamento).
        rl_ident = f"{client_ip(request)}:{(username or '').lower()}"
        if is_rate_limited("login", rl_ident, limit=10):
            messages.error(request, 'Muitas tentativas. Aguarde alguns minutos e tente novamente.')
            return render(request, 'accounts/login.html', {'login_failed': True, 'username_value': username})
        user = authenticate(request, username=username, password=password)

        if user is not None:
            clear_attempts("login", rl_ident)
            remember = request.POST.get('remember')
            if not remember:
                # Session expires when the browser closes
                request.session.set_expiry(0)
            else:
                # Session lasts 30 days
                request.session.set_expiry(60 * 60 * 24 * 30)
            auth_login(request, user)
            messages.success(request, f'Bem-vindo de volta, {user.username}!')
            next_url = request.GET.get('next', '')
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = reverse('core:index')
            return redirect(next_url)
        else:
            register_failure("login", rl_ident, window=300)
            messages.error(request, 'Nome de usuário ou senha inválidos.')
            return render(request, 'accounts/login.html', {'login_failed': True, 'username_value': username})

    return render(request, 'accounts/login.html')

@require_http_methods(["GET", "POST"])
def logout(request):
    # CSRF hardening (Fetch Metadata): POST sempre OK (token CSRF).
    # GET só é aceito em navegação same-origin de topo — bloqueia
    # <img src="/accounts/logout/">, fetch e iframes cross-site.
    if request.method == "GET":
        site = request.headers.get("Sec-Fetch-Site")
        mode = request.headers.get("Sec-Fetch-Mode")
        if site in ("cross-site", "same-site") or (mode and mode != "navigate"):
            return redirect('core:index')
        # Não deslogar em prefetch/preload do navegador (Chrome, Firefox, etc.)
        is_prefetch = (
            "prefetch" in (request.headers.get("Sec-Purpose", "").lower())
            or request.headers.get("Purpose", "").lower() == "prefetch"
            or request.headers.get("X-Moz", "").lower() == "prefetch"
            or request.headers.get("X-Purpose", "").lower() in ("preview", "prefetch")
        )
        if is_prefetch:
            return redirect('core:index')
    auth_logout(request)
    messages.info(request, 'Você saiu da sua conta com sucesso.')
    return redirect('accounts:login')


@require_http_methods(["POST"])
def change_password(request):
    """Allows a user to change their own password by confirming current password."""
    rl_ident = client_ip(request)
    if is_rate_limited("change_password", rl_ident, limit=10):
        messages.error(request, 'Muitas tentativas. Aguarde alguns minutos e tente novamente.')
        return redirect('accounts:login')

    username = (request.POST.get('username') or '').strip()
    old_password = request.POST.get('old_password') or ''
    new_password1 = request.POST.get('new_password1') or ''
    new_password2 = request.POST.get('new_password2') or ''

    if not old_password or not new_password1 or not new_password2:
        messages.error(request, 'Preencha todos os campos de senha.')
        return redirect('accounts:login')

    if new_password1 != new_password2:
        messages.error(request, 'A nova senha e a confirmação não conferem.')
        return redirect('accounts:login')

    if old_password == new_password1:
        messages.error(request, 'A nova senha deve ser diferente da senha atual.')
        return redirect('accounts:login')

    # Identifica o usuário e valida a senha atual (conta só falhas no rate limit).
    if request.user.is_authenticated:
        user = request.user
        identity_ok = user.check_password(old_password)
    else:
        if not username:
            messages.error(request, 'Informe seu usuário para alterar a senha.')
            return redirect('accounts:login')
        user = authenticate(request, username=username, password=old_password)
        identity_ok = user is not None

    if not identity_ok:
        register_failure("change_password", rl_ident, window=300)
        messages.error(request, 'Usuário ou senha atual inválidos.')
        return redirect('accounts:login')

    # Aplica as regras de força de senha (AUTH_PASSWORD_VALIDATORS).
    try:
        validate_password(new_password1, user)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect('accounts:login')

    user.set_password(new_password1)
    user.save(update_fields=['password'])
    clear_attempts("change_password", rl_ident)

    if request.user.is_authenticated:
        update_session_auth_hash(request, user)
        messages.success(request, 'Senha alterada com sucesso.')
    else:
        messages.success(request, 'Senha alterada com sucesso. Faça login com a nova senha.')
    return redirect('accounts:login')
