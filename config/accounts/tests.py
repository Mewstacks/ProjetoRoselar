from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class SessionDurabilityTests(TestCase):
    """
    O vendedor perdia o orcamento inteiro quando o navegador do celular
    reiniciava: a sessao morria junto com o processo e o POST caia no login.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="vendedor", password="senha-forte-123")

    def test_login_without_remember_survives_browser_restart(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "vendedor", "password": "senha-forte-123"},
        )
        self.assertEqual(response.status_code, 302)
        # set_expiry(0) devolveria 0 aqui e o cookie morreria ao fechar o app.
        self.assertEqual(self.client.session.get_expiry_age(), 60 * 60 * 12)
        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_login_with_remember_lasts_thirty_days(self):
        self.client.post(
            reverse("accounts:login"),
            {"username": "vendedor", "password": "senha-forte-123", "remember": "on"},
        )
        self.assertEqual(self.client.session.get_expiry_age(), 60 * 60 * 24 * 30)

    def test_csrf_cookie_is_persistent(self):
        # CSRF_COOKIE_AGE = None faz o token morrer ao fechar o navegador e
        # derruba em 403 qualquer formulario aberto antes disso.
        self.assertIsNotNone(settings.CSRF_COOKIE_AGE)
        self.assertGreaterEqual(settings.CSRF_COOKIE_AGE, 60 * 60 * 24 * 30)
