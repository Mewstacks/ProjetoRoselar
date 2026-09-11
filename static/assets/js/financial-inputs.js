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
      // Sem autocomplete="off": ele desliga tambem a restauracao do valor no
      // botao "voltar" do navegador, e so os campos de dinheiro voltavam
      // vazios enquanto o resto da linha voltava preenchido.
      // https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Turning_off_form_autocompletion
      input.removeAttribute('autocomplete');
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

  // O servidor recebe o valor técnico ("7900.00"); o vendedor continua vendo
  // o que digitou ("7.900,00"). Antes o envio sobrescrevia o próprio campo e
  // tentava devolver o formato visual num setTimeout — que nunca roda quando a
  // página navega. Voltar pelo botão "voltar" trazia o campo com o número
  // técnico, ou vazio. Agora o campo visível nunca é alterado: ele sai do envio
  // (disabled) e um hidden de mesmo nome leva o valor técnico no lugar dele.
  function unmute(root) {
    (root || document).querySelectorAll('[data-financial-muted]').forEach(function (input) {
      input.disabled = false;
      input.removeAttribute('data-financial-muted');
    });
    (root || document).querySelectorAll('[data-financial-wire]').forEach(function (node) {
      node.remove();
    });
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || form.tagName !== 'FORM') return;
    unmute(form);
    form.querySelectorAll('[data-financial]').forEach(function (input) {
      if (!input.name || input.disabled) return;
      var wire = document.createElement('input');
      wire.type = 'hidden';
      wire.name = input.name;
      wire.value = wireValue(input);
      wire.setAttribute('data-financial-wire', '');
      input.insertAdjacentElement('afterend', wire);
      input.disabled = true;
      input.setAttribute('data-financial-muted', '');
    });
    // Validação local que cancela o envio devolve o campo ao usuário: a
    // serialização do formulário já aconteceu quando esta tarefa roda.
    window.setTimeout(function () { unmute(form); }, 0);
  }, true);

  // Página restaurada do cache volta com os campos desligados do envio
  // anterior. Religar aqui evita um formulário que parece travado.
  window.addEventListener('pageshow', function () {
    unmute(document);
    initialise(document);
  });

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
