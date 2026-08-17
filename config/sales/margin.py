"""Motor de Margem — fonte única de verdade para margem, custos e comissão.

Extraído de `sales/views.py` para que o cálculo possa ser reaproveitado fora da
camada de views (fechamento de venda, migrações de dados e relatórios) sem
importar o módulo de views e sem duplicar a regra — a duplicação era exatamente
o que fazia o Relatório de Comissões divergir do simulador.
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_CEILING


def _run_simulation(
    subtotal: Decimal,
    freight_value: Decimal,
    discount_pct: Decimal,
    markup_pct: Decimal,
    down_payment: Decimal,
    has_architect: bool,
    payment_methods: list[dict],
    total_override: Decimal | None = None,
) -> dict:
    """Motor de Margem Unificado.

    Recebe freight_value JÁ com markup por dentro (calculado em _build_simulation_context).
    Comissão interpolada linearmente: [2%, 5%] para PIX/Dinheiro, [2%, 4%] para cartão e demais.
    Status: VERMELHO se MLD<0, AMARELO se 0≤MLD<2, VERDE se MLD≥2.

    `total_override` é o "Preço Final ao Cliente" digitado no orçamento. Quando
    presente, ele É o valor da venda: o preço dos produtos deixa de ser derivado
    de acréscimo/desconto e passa a ser o override menos o frete. O restante do
    motor (juros, arquiteto, comissão, entrada) continua igual, agora sobre o
    valor que o cliente realmente paga.
    """
    from decimal import ROUND_HALF_UP
    subtotal      = Decimal(str(subtotal or 0))
    freight_value = Decimal(str(freight_value or 0))
    discount_pct  = Decimal(str(discount_pct or 0))
    markup_pct    = Decimal(str(markup_pct or 0))
    down_payment  = Decimal(str(down_payment or 0))
    if total_override is not None:
        total_override = max(Decimal('0'), Decimal(str(total_override)))

    if subtotal <= 0:
        return {
            "status": "NEUTRO",
            "controls_blocked": False,
            "totals": {
                "subtotal": Decimal('0'), "adj_subtotal": Decimal('0'),
                "freight": freight_value, "total_before_discount": freight_value,
                "discount_value": Decimal('0'), "final_total": freight_value,
                "down_payment": Decimal('0'), "financed": Decimal('0'),
            },
            "costs": {"bank_interest": Decimal('0'), "architect": Decimal('0'), "margin_balance": Decimal('0')},
            "seller": {"commission_pct": Decimal('0'), "commission_value": Decimal('0'), "sacrifice_active": False},
            "main_method": None,
            "max_parcelas": 1,
        }

    # 1. Valores Base (freight já chega com markup por dentro)
    if total_override is not None:
        # Preço final digitado manda: o produto vale o que sobra do total depois
        # do frete. Acréscimo e desconto viram apenas informação de tela.
        valor_total_venda       = total_override
        valor_produtos_ajustado = valor_total_venda - freight_value
    else:
        valor_produtos_ajustado = subtotal * (
            Decimal('1') + (markup_pct / Decimal('100')) - (discount_pct / Decimal('100'))
        )
        valor_total_venda = valor_produtos_ajustado + freight_value

    entrada_efetiva   = min(max(Decimal('0'), down_payment), max(Decimal('0'), valor_total_venda))
    valor_a_financiar = max(Decimal('0'), valor_total_venda - entrada_efetiva)

    # 2. Custo Arquiteto
    custo_arquiteto = Decimal('0')
    if has_architect:
        base_arquiteto  = valor_produtos_ajustado * (Decimal('1') - Decimal('0.12'))
        custo_arquiteto = base_arquiteto * Decimal('0.05')

    # 3. Juros Ponderados + isolamento do juro do frete
    juros_totais_banco = Decimal('0')
    juros_so_do_frete  = Decimal('0')
    metodo_principal   = None
    max_parcelas       = 1
    maior_valor        = Decimal('-1')

    # Determina metodo_principal pela maior perna (independente de ter financiamento)
    # Importante: não pode cair em 'PIX' só porque valor_a_financiar=0 (entrada total),
    # pois isso daria teto de comissão errado (5% em vez de 4% para cartão).
    if payment_methods:
        for metodo in payment_methods:
            metodo_value = Decimal(str(metodo.get('value') or 0))
            metodo_inst  = int(metodo.get('installments') or 1)
            if metodo_value > maior_valor:
                maior_valor      = metodo_value
                metodo_principal = metodo.get('type')
                max_parcelas     = metodo_inst

    if payment_methods and valor_a_financiar > 0:
        for metodo in payment_methods:
            metodo_value = Decimal(str(metodo.get('value') or 0))
            metodo_fee   = Decimal(str(metodo.get('fee_pct') or 0))

            proporcao = (metodo_value / valor_total_venda) if valor_total_venda > 0 else Decimal('0')
            valor_real_financiado_neste_metodo = valor_a_financiar * proporcao
            juros_metodo        = valor_real_financiado_neste_metodo * (metodo_fee / Decimal('100'))
            juros_totais_banco += juros_metodo

            # O juro do frete já foi coberto pelo markup por dentro — isola para não penalizar a margem
            if valor_total_venda > 0:
                proporcao_frete    = freight_value / valor_total_venda
                juros_so_do_frete += juros_metodo * proporcao_frete
    elif not payment_methods:
        metodo_principal = 'PIX'
        max_parcelas     = 1

    # 4. Motor de Margem
    queima_desconto = subtotal * (discount_pct / Decimal('100'))

    # Margem bruta = preço de venda dos produtos menos o custo (88% do subtotal,
    # já que 12% é o budget da loja). Escrita nesta forma — e não como
    # budget + acréscimo − desconto — porque é algebricamente idêntica quando o
    # preço vem de % e continua correta quando ele vem do preço final digitado.
    margem_bruta        = valor_produtos_ajustado - (subtotal * Decimal('0.88'))
    custos_operacionais = (juros_totais_banco - juros_so_do_frete) + custo_arquiteto
    lucro_sobra         = margem_bruta - custos_operacionais
    mld_pct = (lucro_sobra / subtotal) * Decimal('100') if subtotal > Decimal('0') else Decimal('0')

    # 5. Comissão por tipo de pagamento (conforme LOGICA_SIMULADOR.txt)
    #    PIX / CASH (Dinheiro):         dinâmico, clamp(mld, 2%, 5%)
    #    Débito:                        4% fixo
    #    Boleto à vista (1x):           4% fixo (máximo)
    #    Boleto parcelado (2x+):        dinâmico, clamp(mld, 2%, 4%)
    #    Crédito 1x–6x:                 3% fixo
    #    Crédito 7x+:                   dinâmico, clamp(mld, 2%, 4%)
    #    Cheque / outros:               dinâmico, clamp(mld, 2%, 4%)
    sacrificio_ativo = False
    _AVISTA_5   = {'CASH', 'PIX'}        # único teto 5%
    _DEBIT_COMM = {'DEBIT_CARD'}

    if metodo_principal in _AVISTA_5:
        comissao_final = max(Decimal('2'), min(mld_pct, Decimal('5')))
    elif metodo_principal in _DEBIT_COMM:
        comissao_final = Decimal('4')
    elif metodo_principal in {'BOLETO', 'BOLETO_30'}:
        if max_parcelas == 1:
            # boleto à vista = máximo da faixa
            comissao_final = Decimal('4')
        else:
            # boleto parcelado: dinâmico, teto 4%
            comissao_final = max(Decimal('2'), min(mld_pct, Decimal('4')))
    elif metodo_principal == 'CREDIT_CARD':
        if max_parcelas >= 7:
            # 7x+: dinâmico, teto 4%
            comissao_final = max(Decimal('2'), min(mld_pct, Decimal('4')))
        else:
            # 1x–6x: fixo 3%
            comissao_final = Decimal('3')
    else:
        # Cheque, sem forma selecionada, etc.: dinâmico, teto 4%
        comissao_final = max(Decimal('2'), min(mld_pct, Decimal('4')))

    comissao_final = comissao_final.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if mld_pct < Decimal('0'):
        status_simulacao = "VERMELHO"
    elif mld_pct < Decimal('2'):
        status_simulacao = "AMARELO"
        sacrificio_ativo = True
    else:
        status_simulacao = "VERDE"

    return {
        "status": status_simulacao,
        "controls_blocked": status_simulacao == "VERMELHO",
        "totals": {
            "subtotal":              subtotal,
            "adj_subtotal":          valor_produtos_ajustado,
            "freight":               freight_value,
            "total_before_discount": valor_produtos_ajustado + freight_value,
            "discount_value":        queima_desconto,
            "final_total":           valor_total_venda,
            "down_payment":          entrada_efetiva,
            "financed":              valor_a_financiar,
        },
        "costs": {
            "bank_interest":  juros_totais_banco,
            "architect":      custo_arquiteto,
            "margin_balance": lucro_sobra,
        },
        "seller": {
            "commission_pct":   comissao_final,
            "commission_value": valor_produtos_ajustado * (comissao_final / Decimal('100')),
            "sacrifice_active": sacrificio_ativo,
        },
        "main_method": metodo_principal,
        "max_parcelas": max_parcelas,
    }


def _build_simulation_context(
    *,
    subtotal: Decimal,
    freight_value: Decimal,
    sim_payment_type: str,
    sim_has_architect: bool,
    sim_discount: Decimal,
    price_increase_pct: Decimal,
    sim_installments: int,
    sim_payment_type_2: str = '',
    sim_installments_2: int = 1,
    sim_split_amount: Decimal | None = None,
    price_increase_pct_2: Decimal = Decimal("0"),
    down_payment_value: Decimal | None = None,
    total_override: Decimal | None = None,
) -> dict:
    """Wrapper que organiza os inputs do request e injeta no Motor de Margem.

    Toda a lógica complexa de target_mode e cálculos reversos foi removida.
    O motor (`_run_simulation`) é a única fonte de verdade para margem,
    custos e comissão.
    """
    from core.models import (
        PaymentTariff,
        PaymentMethodType,
        payment_condition_label,
        payment_description,
    )

    MAX_DISCOUNT_ABSOLUTE = Decimal("30")
    MAX_PRICE_INCREASE    = Decimal("30")
    MARGIN_BASE      = Decimal("12")
    COMMISSION_FLOOR = Decimal("2")
    ARQUITETO_PCT    = Decimal("5")  # 5% sobre valor ajustado líquido da margem de 12%

    # Higienização de Inputs
    subtotal      = max(Decimal("0"), Decimal(str(subtotal or 0)))
    freight_value = max(Decimal("0"), Decimal(str(freight_value or 0)))
    sim_discount         = max(Decimal("0"), min(Decimal(str(sim_discount or 0)), MAX_DISCOUNT_ABSOLUTE))
    price_increase_pct   = max(Decimal("0"), min(Decimal(str(price_increase_pct or 0)), MAX_PRICE_INCREASE))
    price_increase_pct_2 = max(Decimal("0"), min(Decimal(str(price_increase_pct_2 or 0)), MAX_PRICE_INCREASE))
    sim_installments   = max(1, min(int(sim_installments or 1), 18))
    sim_installments_2 = max(1, min(int(sim_installments_2 or 1), 18))
    if total_override is not None:
        total_override = max(Decimal("0"), Decimal(str(total_override)))

    split_mode = bool(sim_payment_type_2)

    # ---- Taxas antecipadas (necessárias para calcular o markup do frete) ----
    # Tarifa ausente (None) = parcelamento não cadastrado. Bloqueia em vez de
    # assumir 0%, que liberaria a venda sem cobrar o custo do banco.
    tariff_missing = False

    def _fee_for(payment_type: str, installments: int) -> Decimal:
        nonlocal tariff_missing
        fee = PaymentTariff.get_fee(payment_type, installments)
        if fee is None:
            tariff_missing = True
            return Decimal("0")
        return Decimal(str(fee))

    fee_1 = Decimal("0")
    fee_2 = Decimal("0")
    if sim_payment_type:
        fee_1 = _fee_for(sim_payment_type, sim_installments)
    if split_mode and sim_payment_type_2:
        fee_2 = _fee_for(sim_payment_type_2, sim_installments_2)

    # ---- Markup por dentro no Frete ANTES de dividir o valor entre pernas ----
    # Usa a maior taxa para garantir que a pior perna ainda cobre o frete exato.
    taxa_maxima        = max(fee_1, fee_2)
    taxa_decimal_frete = taxa_maxima / Decimal("100")
    if taxa_decimal_frete < Decimal("1") and freight_value > 0:
        freight_cobrado = freight_value / (Decimal("1") - taxa_decimal_frete)
    else:
        freight_cobrado = freight_value

    # ---- Total temporário baseado no frete JÁ com markup ----
    # Com preço final digitado o total não depende mais do acréscimo: qualquer
    # `pi` devolve o mesmo valor, o que também zera as sugestões de acréscimo
    # (subir % não muda um preço que já está fixado).
    def _total_for_markup(pi: Decimal) -> Decimal:
        if total_override is not None:
            return total_override
        adj = subtotal * (Decimal("1") + pi / Decimal("100") - sim_discount / Decimal("100"))
        return max(Decimal("0"), adj + freight_cobrado)

    # ---- Construção da lista de métodos de pagamento para o Motor ----
    def _methods_for_total(total: Decimal) -> list[dict]:
        if split_mode and sim_split_amount and sim_payment_type:
            leg_1 = min(Decimal(str(sim_split_amount)), total)
            leg_2 = max(Decimal("0"), total - leg_1)
            methods = [{
                'type': sim_payment_type, 'installments': sim_installments,
                'fee_pct': fee_1, 'value': leg_1,
            }]
            if leg_2 > 0:
                methods.append({
                    'type': sim_payment_type_2, 'installments': sim_installments_2,
                    'fee_pct': fee_2, 'value': leg_2,
                })
            return methods
        if sim_payment_type:
            return [{
                'type': sim_payment_type, 'installments': sim_installments,
                'fee_pct': fee_1, 'value': total,
            }]
        return []

    valor_temporario_total = _total_for_markup(price_increase_pct)
    payment_methods = _methods_for_total(valor_temporario_total)

    if split_mode and sim_split_amount and sim_payment_type:
        valor_leg_1 = min(Decimal(str(sim_split_amount)), valor_temporario_total)
        valor_leg_2 = max(Decimal("0"), valor_temporario_total - valor_leg_1)
    else:
        valor_leg_1 = valor_temporario_total
        valor_leg_2 = Decimal("0")

    # ---- Higienização da entrada (down payment) HONESTA ----
    dp_input = max(Decimal("0"), Decimal(str(down_payment_value or 0)))

    dp_min_value = Decimal("0")
    if split_mode:
        # No modo Entrada Financiada a perna 1 já foi embutida nos payment_methods
        # com a sua respectiva taxa. Passar um down_payment aqui faria o motor
        # descontar o valor duas vezes (uma como abatimento à vista, outra como taxa).
        dp_capped = Decimal("0")
    else:
        # Usa EXATAMENTE o que o cara digitou (dp_input), sem forçar mínimo nenhum.
        dp_capped = min(dp_input, valor_temporario_total)

    # ---- Executa o Motor Centralizado (passa frete com markup) ----
    resultado = _run_simulation(
        subtotal=subtotal,
        freight_value=freight_cobrado,
        discount_pct=sim_discount,
        markup_pct=price_increase_pct,
        down_payment=dp_capped,
        has_architect=sim_has_architect,
        payment_methods=payment_methods,
        total_override=total_override,
    )

    # ---- Entrada mínima para desbloquear (MLD >= 0) ----
    # Calcula a taxa efetiva ponderada pelos métodos de pagamento e resolve
    # a equação: financed_max = margem_fixa * 100 / taxa_efetiva.
    dp_to_unlock = Decimal("0")
    if resultado['controls_blocked'] and payment_methods and valor_temporario_total > 0:
        taxa_efetiva = sum(
            (Decimal(str(m['value'])) / valor_temporario_total) * Decimal(str(m['fee_pct']))
            for m in payment_methods
        )
        if taxa_efetiva > 0:
            # Mesma álgebra do motor: preço dos produtos = total − frete, e a
            # margem bruta é esse preço menos 88% do subtotal. Vale com e sem
            # preço final digitado.
            _adj = valor_temporario_total - freight_cobrado
            _arquiteto = (_adj * (Decimal("1") - Decimal("0.12"))) * Decimal("0.05") if sim_has_architect else Decimal("0")
            _margem_fixa = _adj - (subtotal * Decimal("0.88")) - _arquiteto
            if _margem_fixa > 0 and _adj > 0:
                # Após o fix de juros_so_do_frete, só o juro proporcional aos produtos
                # pesa na margem da loja. Escala a taxa efetiva pelo fator produto/total.
                _taxa_produto = taxa_efetiva * (_adj / valor_temporario_total)
                if _taxa_produto > 0:
                    _financed_max = _margem_fixa * Decimal("100") / _taxa_produto
                    dp_to_unlock = max(Decimal("0"), valor_temporario_total - _financed_max)
                    dp_to_unlock = dp_to_unlock.quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    # ---- Valores derivados para os templates ----
    adj_subtotal      = resultado['totals']['adj_subtotal']
    # As linhas "Ajuste de Preço" e "Total antes do desconto" descrevem como o
    # preço FOI formado por percentual; com preço final digitado elas continuam
    # mostrando essa formação e a diferença até o valor cobrado aparece numa
    # linha própria (override_adjust_value).
    adj_pre_override  = subtotal * (
        Decimal("1") + price_increase_pct / Decimal("100") - sim_discount / Decimal("100")
    )
    total_before_disc = adj_pre_override + freight_cobrado
    discount_value    = resultado['totals']['discount_value']
    final_total       = resultado['totals']['final_total']
    down_payment_used = resultado['totals']['down_payment']
    financed_value    = resultado['totals']['financed']
    payment_fee_value = resultado['costs']['bank_interest']
    architect_value   = resultado['costs']['architect']

    # Valores por perna (para o painel de split).
    if split_mode:
        split_amount_1 = valor_leg_1
        split_amount_2 = valor_leg_2
        prop_1 = (valor_leg_1 / valor_temporario_total) if valor_temporario_total > 0 else Decimal("0")
        prop_2 = (valor_leg_2 / valor_temporario_total) if valor_temporario_total > 0 else Decimal("0")
        fin_leg_1 = financed_value * prop_1
        fin_leg_2 = financed_value * prop_2
        payment_fee_value_1 = fin_leg_1 * (fee_1 / Decimal("100"))
        payment_fee_value_2 = fin_leg_2 * (fee_2 / Decimal("100"))
        installment_value_1 = (
            split_amount_1 / Decimal(sim_installments) if sim_installments > 1 else split_amount_1
        )
        installment_value_2 = (
            split_amount_2 / Decimal(sim_installments_2) if sim_installments_2 > 1 else split_amount_2
        )
    else:
        split_amount_1 = final_total
        split_amount_2 = Decimal("0")
        payment_fee_value_1 = payment_fee_value
        payment_fee_value_2 = Decimal("0")
        installment_value_1 = (
            financed_value / Decimal(sim_installments) if sim_installments > 1 else financed_value
        )
        installment_value_2 = Decimal("0")

    installment_value = installment_value_1 if not split_mode else Decimal("0")

    # Base real da comissão do arquiteto: subtotal ajustado menos a margem da loja (12%).
    # NÃO subtrair discount_value — adj_subtotal já veio com o desconto aplicado pelo motor.
    valor_avista = adj_subtotal * (Decimal("1") - Decimal("0.12"))

    # ---- Descrições amigáveis ----
    if sim_payment_type:
        desc1 = payment_description(sim_payment_type, sim_installments)
    else:
        desc1 = ""
    desc2 = ""
    if split_mode:
        desc2 = payment_description(sim_payment_type_2, sim_installments_2)
        sim_payment_description = f"{desc1} + {desc2}" if desc1 else desc2
    else:
        sim_payment_description = desc1 if desc1 else "Não definido"

    # ---- tariffs_by_type_json para o JS do painel ----
    payment_type_choices = list(PaymentMethodType.choices)
    max_inst_map = {
        'CASH': 1, 'PIX': 1, 'DEBIT_CARD': 1, 'CREDIT_CARD': 18, 'CHEQUE': 12, 'BOLETO': 4,
        'BOLETO_30': 1,
    }
    tariffs_by_type: dict[str, list] = {}
    for pt_val, _pt_lbl in payment_type_choices:
        max_inst = max_inst_map.get(pt_val, 1)
        tariff_lookup = PaymentTariff.lookup_type(pt_val)
        # Só oferece parcelas com tarifa cadastrada — ausência não é 0%.
        options = [
            {
                'installments': t.installments,
                'fee': float(t.fee_percent),
                'label': payment_condition_label(pt_val, t.installments),
            }
            for t in PaymentTariff.objects.filter(
                payment_type=tariff_lookup, installments__lte=max_inst
            ).order_by('installments')
        ]
        tariffs_by_type[pt_val] = options

    # ---- Status e flags do template ----
    status = resultado['status']
    controls_blocked = resultado['controls_blocked'] or tariff_missing

    _AVISTA_TYPES = {'PIX', 'CASH', 'DEBIT_CARD', 'CHEQUE', 'BOLETO', 'BOLETO_30'}
    split_m1_avista  = split_mode and sim_payment_type   in _AVISTA_TYPES
    split_m2_avista  = split_mode and sim_payment_type_2 in _AVISTA_TYPES
    split_both_cards = split_mode and not split_m1_avista and not split_m2_avista

    seller_commission_percent = resultado['seller']['commission_pct']
    seller_commission_value   = resultado['seller']['commission_value']
    sacrifice_active          = resultado['seller']['sacrifice_active']

    # Teto real de comissão depende do método principal da venda.
    # PIX/CASH → 5%, Débito → 4%, Boleto (todos) → 4%, Crédito 1x-6x → 3%, Crédito 7x+ → 4%, outros → 4%
    _AVISTA_COMM_5 = {'PIX', 'CASH'}
    _main_method_for_comm = resultado.get('main_method') or sim_payment_type or ''
    _main_inst = resultado.get('max_parcelas') or sim_installments or 1
    if _main_method_for_comm in _AVISTA_COMM_5:
        commission_max_actual = Decimal('5')
    elif _main_method_for_comm == 'CREDIT_CARD' and _main_inst < 7:
        commission_max_actual = Decimal('3')
    else:
        commission_max_actual = Decimal('4')

    # ---- Sugestões de acréscimo ----
    # Restaura o que a "reforma testuaria" (6b8c4a2) tinha fixado em zero: o
    # template já mostra "Adicione no mínimo +X%", mas o motor devolvia sempre 0
    # e todas as mensagens caíam no texto genérico.
    #
    # `_run_simulation` é puro, então em vez de derivar uma fórmula fechada por
    # modo (split, entrada, arquiteto) varremos o acréscimo em passos de 0,1% e
    # perguntamos ao próprio motor. Vale para todos os modos, sem duplicar regra.
    _PI_STEP = Decimal("0.1")

    def _mld_pct_para(pi: Decimal, solo: dict | None = None) -> Decimal:
        """MLD% com acréscimo `pi`. `solo` avalia um método cobrindo a venda toda."""
        if subtotal <= 0:
            return Decimal("0")
        total = _total_for_markup(pi)
        if solo is None:
            methods = _methods_for_total(total)
        else:
            methods = [dict(solo, value=total)] if total > 0 else []
        r = _run_simulation(
            subtotal=subtotal,
            freight_value=freight_cobrado,
            discount_pct=sim_discount,
            markup_pct=pi,
            down_payment=Decimal("0") if split_mode else min(dp_input, total),
            has_architect=sim_has_architect,
            payment_methods=methods,
            total_override=total_override,
        )
        return (r['costs']['margin_balance'] / subtotal) * Decimal("100")

    def _acrescimo_para(alvo_mld: Decimal, solo: dict | None = None) -> Decimal:
        """Menor acréscimo ADICIONAL que leva o MLD ao alvo. 0 se nem +30% resolve."""
        if subtotal <= 0:
            return Decimal("0")
        pi = price_increase_pct
        while pi <= MAX_PRICE_INCREASE:
            if _mld_pct_para(pi, solo) >= alvo_mld:
                return pi - price_increase_pct
            pi += _PI_STEP
        return Decimal("0")

    min_increase_to_unblock = Decimal("0")
    suggested_increase = Decimal("0")
    suggestion_is_opportunity = False

    _pode_sugerir = bool(payment_methods) and not tariff_missing and subtotal > 0
    if _pode_sugerir:
        if resultado['controls_blocked']:
            # VERMELHO: quanto falta para a margem parar de ser negativa.
            min_increase_to_unblock = _acrescimo_para(Decimal("0"))
            suggested_increase = min_increase_to_unblock
        elif sacrifice_active:
            # AMARELO: comissão presa no piso; sugere o acréscimo que devolve o verde.
            suggested_increase = _acrescimo_para(Decimal("2"))
        elif (
            not split_mode
            and sim_payment_type in {'CREDIT_CARD', 'BOLETO'}
            and sim_installments >= 7
            and seller_commission_percent < commission_max_actual
        ):
            # VERDE com comissão abaixo do teto: oportunidade, não problema.
            suggested_increase = _acrescimo_para(commission_max_actual)
            suggestion_is_opportunity = suggested_increase > 0

    # Cada perna do split avaliada isoladamente: "esse método, sozinho, se paga?"
    margin_exceeded_1 = False
    margin_exceeded_2 = False
    suggested_increase_1 = Decimal("0")
    suggested_increase_2 = Decimal("0")
    if split_mode and _pode_sugerir:
        _solo_1 = {'type': sim_payment_type, 'installments': sim_installments, 'fee_pct': fee_1}
        margin_exceeded_1 = _mld_pct_para(price_increase_pct, _solo_1) < Decimal("0")
        if margin_exceeded_1:
            suggested_increase_1 = _acrescimo_para(Decimal("0"), _solo_1)
        if valor_leg_2 > 0:
            _solo_2 = {'type': sim_payment_type_2, 'installments': sim_installments_2, 'fee_pct': fee_2}
            margin_exceeded_2 = _mld_pct_para(price_increase_pct, _solo_2) < Decimal("0")
            if margin_exceeded_2:
                suggested_increase_2 = _acrescimo_para(Decimal("0"), _solo_2)

    any_method_over_margin = (
        split_mode
        and not resultado['controls_blocked']
        and (margin_exceeded_1 or margin_exceeded_2)
    )

    blended_fee_pct = (
        (payment_fee_value / financed_value * Decimal("100"))
        if financed_value > 0 else Decimal("0")
    )

    return {
        # Inputs devolvidos para a tela
        'subtotal':                 subtotal,
        'freight_value':            freight_cobrado,  # frete com markup embutido
        'discount_percent':         sim_discount,
        'price_increase_pct':       price_increase_pct,
        'price_increase_pct_2':     price_increase_pct_2,
        'sim_has_architect':        sim_has_architect,
        'sim_payment_type':         sim_payment_type,
        'sim_installments':         sim_installments,
        'sim_payment_type_2':       sim_payment_type_2,
        'sim_installments_2':       sim_installments_2,
        'sim_split_amount':         sim_split_amount,
        'split_mode':               split_mode,
        'split_m1_avista':          split_m1_avista,
        'split_m2_avista':          split_m2_avista,
        'split_both_cards':         split_both_cards,
        'down_payment_value':       down_payment_used,
        'dp_min_value':             dp_min_value,
        'dp_to_unlock':             dp_to_unlock,

        # Totais calculados
        'adj_subtotal':             adj_subtotal,
        'price_increase_value':     adj_pre_override - subtotal,
        'total_before_discount':    total_before_disc,
        'total_override_active':    total_override is not None,
        'total_override_value':     total_override,
        'override_adjust_value':    final_total - total_before_disc,
        'discount_value':           discount_value,
        'total_after_discount':     final_total,
        'final_total':               final_total,
        'financed_value':           financed_value,
        'valor_avista':             valor_avista,

        # Custos / taxas
        'payment_fee_percent':       fee_1,
        'payment_fee_percent_2':     fee_2,
        'payment_fee_value':         payment_fee_value,
        'payment_fee_value_2':       payment_fee_value_2,
        'blended_fee_pct':           blended_fee_pct,

        # Split / parcelas
        'split_amount_1':            split_amount_1,
        'split_amount_2':            split_amount_2,
        'installment_value':         installment_value,
        'installment_value_1':       installment_value_1,
        'installment_value_2':       installment_value_2,
        'sim_payment_desc_1':        desc1,
        'sim_payment_desc_2':        desc2,
        'sim_payment_description':   sim_payment_description,

        # Vendedor / Arquiteto
        'seller_commission_percent':   seller_commission_percent,
        'seller_commission_value':     seller_commission_value,
        'original_commission_percent': commission_max_actual,
        'commission_floor':            COMMISSION_FLOOR,
        'commission_max':              commission_max_actual,
        'commission_reduced':          sacrifice_active,
        'architect_percent':           ARQUITETO_PCT,
        'architect_commission_value':  architect_value,

        # Status / margem
        'controls_blocked':        controls_blocked,
        'margin_limit_exceeded':   controls_blocked,
        'margin_exceeded':         controls_blocked,
        'margin_exceeded_1':       margin_exceeded_1,
        'margin_exceeded_2':       margin_exceeded_2,
        'any_method_over_margin':  any_method_over_margin,
        'margin_balance':          resultado['costs']['margin_balance'],
        'margin_base':             MARGIN_BASE,

        # Sugestões
        'suggested_increase':       suggested_increase.quantize(_PI_STEP, rounding=ROUND_CEILING),
        'suggested_increase_1':     suggested_increase_1.quantize(_PI_STEP, rounding=ROUND_CEILING),
        'suggested_increase_2':     suggested_increase_2.quantize(_PI_STEP, rounding=ROUND_CEILING),
        'suggestion_is_opportunity': suggestion_is_opportunity,
        'min_increase_to_unblock':  min_increase_to_unblock.quantize(_PI_STEP, rounding=ROUND_CEILING),

        # Target (segue desativado no novo motor)
        'target_mode':              False,
        'target_final_input':       Decimal("0"),
        'target_installment_mode':  False,
        'target_installment_input': Decimal("0"),

        # UI
        'max_discount_allowed':    MAX_DISCOUNT_ABSOLUTE,
        'payment_type_choices':    payment_type_choices,
        'tariffs_by_type_json':    json.dumps(tariffs_by_type),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Persistência da comissão da venda
#
# A comissão precisa ser CONGELADA no fechamento da venda. Reconstruí-la depois
# a partir do banco é impossível com fidelidade: o motor depende da entrada
# (down_payment) e da composição das pernas de pagamento, e qualquer tentativa de
# aproximar isso num relatório produz um número diferente do que o vendedor viu
# no simulador. Por isso o cálculo acontece uma vez, aqui, e os relatórios apenas
# somam o que foi gravado.
# ──────────────────────────────────────────────────────────────────────────────

COMMISSION_FIELDS = (
    "commission_pct",
    "commission_value",
    "commission_calculated_at",
    "commission_source",
)


def simulate_quote(quote) -> dict:
    """Roda o motor de margem com os parâmetros já persistidos no orçamento.

    Espelha exatamente o caminho GET de `quote_simulate_commission`, que é a tela
    que o vendedor enxerga — mesmo subtotal, mesmo frete faturável, mesmas pernas
    de pagamento, mesma entrada.
    """
    from sales.models import PriceTier

    # Migrações históricas 0028/0029 usam o modelo real antes de campos mais
    # novos existirem fisicamente. Ler de __dict__ mantém o backfill compatível
    # com instalações do zero sem disparar uma consulta ao campo ainda ausente.
    selected_tier = (
        quote.__dict__.get("selected_price_tier", PriceTier.RETAIL)
        if quote.dual_pricing
        else PriceTier.RETAIL
    )
    return _build_simulation_context(
        subtotal=quote.calculate_subtotal_for_tier(selected_tier),
        # Frete por conta da loja (STORE) não é repassado ao cliente: fica fora
        # do total e, portanto, fora da base de comissão.
        freight_value=quote.billable_freight,
        sim_payment_type=quote.payment_type or "",
        sim_has_architect=quote.has_architect,
        # Atacado já é uma tabela reduzida; o desconto comercial pertence
        # exclusivamente à alternativa de varejo.
        sim_discount=(
            Decimal("0")
            if selected_tier == PriceTier.WHOLESALE
            else (quote.discount_percent or Decimal("0"))
        ),
        price_increase_pct=quote.price_increase_percent or Decimal("0"),
        sim_installments=quote.payment_installments or 1,
        sim_payment_type_2=quote.payment_type_2 or "",
        sim_installments_2=quote.payment_installments_2 or 1,
        sim_split_amount=quote.payment_split_amount,
        price_increase_pct_2=Decimal("0"),
        down_payment_value=quote.down_payment_value or None,
        # O preço final digitado se refere só ao varejo (mesma regra de
        # Quote.calculate_rounded_total_wholesale, que o ignora).
        total_override=(
            quote.__dict__.get("total_override")
            if selected_tier == PriceTier.RETAIL
            else None
        ),
    )


def persist_quote_commission(quote, source: str = "ENGINE") -> dict:
    """Apura e grava a comissão do orçamento. Devolve os valores gravados.

    Grava via queryset `.update()` — mesmo padrão de `_refresh_quote_snapshot` —
    para não disparar o post_save do Quote e recalcular o snapshot à toa. A
    instância em memória é atualizada junto para o chamador não ficar com dados
    velhos.
    """
    from django.utils import timezone
    from sales.models import Quote

    ctx = simulate_quote(quote)
    values = {
        "commission_pct": Decimal(str(ctx["seller_commission_percent"])).quantize(Decimal("0.01")),
        "commission_value": Decimal(str(ctx["seller_commission_value"])).quantize(Decimal("0.01")),
        "commission_calculated_at": timezone.now(),
        "commission_source": source,
    }
    Quote.objects.filter(pk=quote.pk).update(**values)
    for field, value in values.items():
        setattr(quote, field, value)
    return values


def clear_quote_commission(quote) -> None:
    """Zera a comissão gravada — usado quando a venda é revertida.

    Deixar o valor antigo para trás faria a venda revertida continuar aparecendo
    no relatório de comissões.
    """
    from sales.models import Quote

    values = {
        "commission_pct": None,
        "commission_value": None,
        "commission_calculated_at": None,
        "commission_source": "",
    }
    Quote.objects.filter(pk=quote.pk).update(**values)
    for field, value in values.items():
        setattr(quote, field, value)
