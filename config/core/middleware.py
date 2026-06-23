from django.shortcuts import redirect
from django.urls import reverse


# URL prefixes that are always public (no login required)
PUBLIC_PREFIXES = [
    '/accounts/login/',
    '/accounts/logout/',
    '/accounts/change-password/',  # troca de senha pela tela de login (identidade validada pela senha atual)
    '/admin/',
    '/static/',
    '/health/',
]


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            path = request.path_info
            if not any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
                login_url = reverse('accounts:login')
                return redirect(f'{login_url}?next={path}')
        return self.get_response(request)
