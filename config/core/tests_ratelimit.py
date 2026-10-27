from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from core import ratelimit


class ClientIpTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_fly_uses_fly_client_ip_and_ignores_forged_xff(self):
        request = self.factory.get(
            "/",
            HTTP_FLY_CLIENT_IP="203.0.113.7",
            HTTP_X_FORWARDED_FOR="9.9.9.9, 203.0.113.7, 172.16.0.2",
        )
        with mock.patch.object(ratelimit, "FLY_APP", "roselar"):
            self.assertEqual(ratelimit.client_ip(request), "203.0.113.7")

    def test_fly_without_header_falls_back_to_xff_offset(self):
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="9.9.9.9, 198.51.100.4")
        with mock.patch.object(ratelimit, "FLY_APP", "roselar"), \
                mock.patch.object(ratelimit, "TRUSTED_PROXY_COUNT", 1):
            self.assertEqual(ratelimit.client_ip(request), "198.51.100.4")

    def test_outside_fly_ignores_fly_client_ip_header(self):
        request = self.factory.get(
            "/",
            HTTP_FLY_CLIENT_IP="1.1.1.1",
            HTTP_X_FORWARDED_FOR="9.9.9.9, 198.51.100.4",
        )
        with mock.patch.object(ratelimit, "FLY_APP", None), \
                mock.patch.object(ratelimit, "TRUSTED_PROXY_COUNT", 1):
            self.assertEqual(ratelimit.client_ip(request), "198.51.100.4")
