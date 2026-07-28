from decimal import Decimal

from django.test import TestCase

from core.models import PaymentTariff


class PaymentTariffFeeTests(TestCase):
    """Tarifa ausente nunca pode virar 0% — isso liberava parcela de graça."""

    def setUp(self):
        PaymentTariff.objects.all().delete()
        PaymentTariff.objects.create(
            payment_type="CREDIT_CARD", installments=1, fee_percent=Decimal("4.00")
        )
        PaymentTariff.objects.create(
            payment_type="CREDIT_CARD", installments=12, fee_percent=Decimal("13.30")
        )
        PaymentTariff.objects.create(
            payment_type="CHEQUE", installments=1, fee_percent=Decimal("0.00")
        )
        PaymentTariff.objects.create(
            payment_type="BOLETO", installments=1, fee_percent=Decimal("0.00")
        )

    def test_fee_cadastrada(self):
        self.assertEqual(
            PaymentTariff.get_fee("CREDIT_CARD", 12), Decimal("13.30")
        )

    def test_fee_ausente_retorna_none_e_nao_zero(self):
        fee = PaymentTariff.get_fee("CREDIT_CARD", 7)
        self.assertIsNone(fee)
        self.assertNotEqual(fee, 0)

    def test_cheque_parcelado_usa_tabela_do_cartao(self):
        # Cheque não tem tabela própria acima de 1x. Antes caía em DoesNotExist
        # e devolvia 0%, cobrando juro nenhum enquanto a tela exibia 13,30%.
        self.assertEqual(PaymentTariff.get_fee("CHEQUE", 12), Decimal("13.30"))

    def test_lookup_type(self):
        self.assertEqual(PaymentTariff.lookup_type("CHEQUE"), "CREDIT_CARD")
        self.assertEqual(PaymentTariff.lookup_type("BOLETO_30"), "BOLETO")
        self.assertEqual(PaymentTariff.lookup_type("CREDIT_CARD"), "CREDIT_CARD")
        self.assertEqual(PaymentTariff.lookup_type("PIX"), "PIX")

    def test_boleto_30_dias_usa_tarifa_de_uma_cobranca(self):
        self.assertEqual(
            PaymentTariff.get_fee("BOLETO_30", 1),
            Decimal("0.00"),
        )
