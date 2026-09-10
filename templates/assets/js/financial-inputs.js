/*
 * Entrada financeira padronizada.
 *
 * Campos com data-financial="money" mostram sempre o valor em reais (1.234,56)
 * enquanto são digitados. Campos data-financial="percent" aceitam vírgula como
 * separador decimal e são normalizados para o servidor no envio do formulário.
 */
(function () {
  'use strict';

  var MONEY = 'money';
  var PERCENT = 'percent';

  function numericValue(value) {
    var raw = String(value == null ? '' : value).trim();
    if (!raw) return null;
    if (raw.indexOf(',') !== -1) raw = raw.replace(/\./g, '').replace(',', '.');
    return /^-?\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : null;
  }

  function moneyFromValue(value) {
    var amount = numericValue(value);
    if (amount === null || !isFinite(amount)) return '';
    return Math.abs(amount).toLocaleString('pt-BR', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function moneyFromDigits(value) {
    var digits = String(value || '').replace(/\D/g, '');
    if (!digits) return '';
    return (Number(digits) / 100).toLocaleString('pt-BR', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function percentFromValue(value) {
    var raw = String(value == null ? '' : value).trim().replace(/%/g, '').replace(/\s/g, '');
    if (!raw) return '';
    // Para porcentagem, ponto e vírgula representam casas decimais; não há milhar.
    raw = raw.replace(/\./g, ',').replace(/[^\d,]/g, '');
    var parts = raw.split(',');
    return parts[0] + (parts.length > 1 ? ',' + parts.slice(1).join('').slice(0, 2) : '');
  }

  function wireValue(input) {
    var type = input && input.dataset ? input.dataset.financial : '';
    var value = String(input && input.value ? input.value : '').trim();
    if (!value) return '';
    if (type === MONEY) {
      var money = numericValue(value);
      return money === null ? value : money.toFixed(2);
    }
    if (type === PERCENT) return value.replace(',', '.');
    return value;
  }

  function initialise(root) {
    (root || document).querySelectorAll('[data-financial]').forEach(function (input) {
      var type = input.dataset.financial;
      input.setAttribute('inputmode', type === MONEY ? 'numeric' : 'decimal');
      input.setAttribute('autocomplete', 'off');
      if (input.value) {
        input.value = type === MONEY ? moneyFromValue(input.value) : percentFromValue(input.value);
      }
    });
  }

  document.addEventListener('input', function (event) {
    var input = event.target;
    if (!input || !input.dataset || !input.dataset.financial) return;
    if (input.dataset.financial === MONEY) input.value = moneyFromDigits(input.value);
    if (input.dataset.financial === PERCENT) input.value = percentFromValue(input.value);
  });

  document.addEventListener('blur', function (event) {
    var input = event.target;
    if (!input || !input.dataset || !input.dataset.financial || !input.value) return;
    if (input.dataset.financial === PERCENT) {
      var value = numericValue(input.value);
      if (value !== null) input.value = value.toLocaleString('pt-BR', { maximumFractionDigits: 2 });
    }
  }, true);

  document.addEventListener('submit', function (event) {
    event.target.querySelectorAll('[data-financial]').forEach(function (input) {
      var displayedValue = input.value;
      input.value = wireValue(input);
      // A serialização do formulário ocorre antes da próxima tarefa. Restaurar o
      // formato visual evita que uma validação local deixe o usuário olhando para
      // um número técnico com ponto decimal.
      window.setTimeout(function () {
        if (document.contains(input)) input.value = displayedValue;
      }, 0);
    });
  }, true);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { initialise(document); });
  } else {
    initialise(document);
  }

  window.RoselarFinancialInputs = {
    initialise: initialise,
    toWireValue: wireValue
  };
}());
