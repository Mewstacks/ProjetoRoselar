/*
 * Rascunho local de formulario.
 *
 * Marque o <form> com data-draft="<nome>". Cada mudanca e guardada no proprio
 * aparelho. Se o envio nunca chega ao servidor — sessao expirada, token CSRF
 * vencido, aba descartada pelo celular, rede caindo — a proxima carga oferece
 * de volta o que foi digitado, em vez de devolver um formulario vazio.
 *
 * Atributos aceitos no <form>:
 *   data-draft           nome do rascunho (obrigatorio)
 *   data-draft-requires  sufixos de campo, separados por virgula; o rascunho so
 *                        e guardado se ao menos um deles tiver conteudo
 *   data-draft-add-row   nome de uma funcao global que cria mais uma linha de
 *                        formset, usada para recriar as linhas do rascunho
 */
(function () {
  'use strict';

  var MAX_AGE = 12 * 60 * 60 * 1000;
  var IGNORED_NAMES = ['csrfmiddlewaretoken', 'submission_token'];

  function keyFor(form) {
    return 'roselar.rascunho.' + form.dataset.draft + ':' + window.location.pathname;
  }

  function storable(el) {
    return el.name
      && el.type !== 'file'
      && el.type !== 'submit'
      && el.type !== 'button'
      && IGNORED_NAMES.indexOf(el.name) === -1
      && !el.hasAttribute('data-financial-wire');
  }

  function snapshot(form) {
    var data = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!storable(el)) return;
      if (el.type === 'checkbox') { data[el.name] = el.checked; return; }
      if (el.type === 'radio') { if (el.checked) data[el.name] = el.value; return; }
      data[el.name] = el.value;
    });
    return data;
  }

  function worthKeeping(form, data) {
    var requires = (form.dataset.draftRequires || '').split(',')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    if (!requires.length) {
      return Object.keys(data).some(function (name) {
        return typeof data[name] === 'string' && data[name].trim();
      });
    }
    return Object.keys(data).some(function (name) {
      var hit = requires.some(function (suffix) {
        return name.slice(-suffix.length) === suffix;
      });
      return hit && String(data[name] || '').trim();
    });
  }

  function clear(key) {
    try { localStorage.removeItem(key); } catch (err) { /* sem armazenamento */ }
  }

  function read(key) {
    try {
      var raw = localStorage.getItem(key);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.data || (Date.now() - parsed.t) > MAX_AGE) {
        clear(key);
        return null;
      }
      return parsed;
    } catch (err) {
      return null;
    }
  }

  function write(form, key) {
    try {
      var data = snapshot(form);
      if (!worthKeeping(form, data)) { clear(key); return; }
      localStorage.setItem(key, JSON.stringify({ t: Date.now(), data: data }));
    } catch (err) {
      // Cota cheia, modo privado ou armazenamento bloqueado: o rascunho e um extra.
    }
  }

  function growFormset(form, data) {
    var addRow = window[form.dataset.draftAddRow || ''];
    if (typeof addRow !== 'function') return;
    var totals = form.querySelectorAll('input[name$="-TOTAL_FORMS"]');
    Array.prototype.forEach.call(totals, function (total) {
      var wanted = parseInt(data[total.name], 10);
      if (isNaN(wanted)) return;
      var guard = 0;
      while (parseInt(total.value, 10) < wanted && guard++ < 80) addRow();
    });
  }

  function apply(form, data) {
    growFormset(form, data);
    Object.keys(data).forEach(function (name) {
      var nodes = form.querySelectorAll('[name="' + name.replace(/"/g, '\\"') + '"]');
      if (!nodes.length) return;
      var value = data[name];
      Array.prototype.forEach.call(nodes, function (el) {
        if (el.type === 'radio') { el.checked = (el.value === value); return; }
        if (el.type === 'checkbox') { el.checked = !!value; return; }
        el.value = value == null ? '' : value;
      });
      var first = nodes[0];
      if (first.tagName === 'SELECT' || first.type === 'checkbox' || first.type === 'radio') {
        Array.prototype.forEach.call(nodes, function (el) {
          el.dispatchEvent(new Event('change', { bubbles: true }));
        });
      }
    });
  }

  // A barra aparece em telas com sistemas de CSS diferentes (Bootstrap numa,
  // classes proprias noutra). Estilo proprio garante que o botao nunca saia
  // invisivel por depender de uma classe que aquela tela nao tem.
  var STYLE_ID = 'roselar-draft-style';
  var CSS = [
    '.rsl-draft{display:flex;flex-wrap:wrap;align-items:center;gap:.75rem;',
    'margin:0 0 1rem;padding:.75rem 1rem;border:1px solid #E3D5A8;border-radius:8px;',
    'background:#FFF8E1;color:#111418;font-size:.875rem;line-height:1.4;}',
    '.rsl-draft__text{flex:1 1 12rem;}',
    '.rsl-draft button{min-height:44px;padding:.5rem 1rem;border-radius:6px;',
    'font:inherit;font-weight:600;cursor:pointer;}',
    '.rsl-draft__restore{background:#111418;color:#fff;border:1px solid #111418;}',
    '.rsl-draft__restore:hover{background:#000;}',
    '.rsl-draft__drop{background:transparent;color:#111418;border:1px solid #111418;}',
    '.rsl-draft__drop:hover{background:rgba(17,20,24,.08);}',
    '.rsl-draft button:focus-visible{outline:2px solid #D32F2F;outline-offset:2px;}',
    // Telas estreitas: o texto ocupa a linha inteira e os dois botoes
    // dividem a linha de baixo, em vez de um sobrar sozinho no canto.
    '@media (max-width:479px){.rsl-draft__text{flex:1 1 100%;}',
    '.rsl-draft button{flex:1 1 0;}}'
  ].join('');

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function offer(form, key, draft) {
    ensureStyle();
    var bar = document.createElement('div');
    bar.className = 'rsl-draft';
    bar.setAttribute('role', 'status');

    var when = new Date(draft.t).toLocaleString('pt-BR', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'
    });
    var text = document.createElement('span');
    text.className = 'rsl-draft__text';
    text.textContent = 'Rascunho não enviado de ' + when + '.';

    var restore = document.createElement('button');
    restore.type = 'button';
    restore.className = 'rsl-draft__restore';
    restore.textContent = 'Restaurar';

    var drop = document.createElement('button');
    drop.type = 'button';
    drop.className = 'rsl-draft__drop';
    drop.textContent = 'Descartar';

    bar.appendChild(text);
    bar.appendChild(restore);
    bar.appendChild(drop);
    form.insertBefore(bar, form.firstChild);

    restore.addEventListener('click', function () {
      apply(form, draft.data);
      bar.remove();
    });
    drop.addEventListener('click', function () {
      clear(key);
      bar.remove();
    });
  }

  function wire(form) {
    if (!form.dataset.draft || form.dataset.draftWired) return;
    form.dataset.draftWired = '1';

    var key = keyFor(form);
    var timer = null;
    ['input', 'change'].forEach(function (evt) {
      form.addEventListener(evt, function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () { write(form, key); }, 600);
      });
    });

    var pending = read(key);
    if (pending) offer(form, key, pending);
  }

  function wireAll() {
    document.querySelectorAll('form[data-draft]').forEach(wire);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireAll);
  } else {
    wireAll();
  }

  window.RoselarFormDraft = {
    // Chamado pela tela de destino quando o envio chegou ao servidor: o
    // rascunho ja cumpriu o papel e nao deve reaparecer.
    clear: function (name, path) {
      clear('roselar.rascunho.' + name + ':' + path);
    }
  };
}());
