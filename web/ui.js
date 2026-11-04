// 公共组件：元素构造、卡片、徽章、表格、标签页、空状态、toast、确认框、输入框、异步按钮。

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k in el && typeof v !== 'string') el[k] = v;
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function card(title, ...children) {
  let head = null;
  if (title instanceof HTMLElement && title.tagName === 'HEADER') head = title;   // cardHead(...) 的结果
  else if (title) head = h('header', {}, typeof title === 'string' ? h('h3', {}, title) : title);
  return h('section', { class: 'card' }, head, ...children);
}

/** 带右侧操作的卡片标题：cardHead('标题', btn1, btn2) */
export function cardHead(title, ...tools) {
  return h('header', {}, h('h3', {}, title), h('span', { class: 'grow' }), ...tools);
}

export function badge(text, kind = '') {
  return h('span', { class: `badge ${kind}` }, text);
}

export function pill(el, text, kind = '') {
  el.textContent = text;
  el.className = `pill ${kind}`;
}

export function empty(title, text = '') {
  return h('div', { class: 'empty' }, h('h4', {}, title), text ? h('div', {}, text) : null);
}

/** columns: [{key, label, width, render(row) -> Node|string}] */
export function table(columns, rows, { emptyText = '暂无数据' } = {}) {
  const colgroup = h('colgroup', {}, columns.map((c) => h('col', { style: c.width ? { width: c.width } : {} })));
  const thead = h('thead', {}, h('tr', {}, columns.map((c) => h('th', {}, c.label))));
  const tbody = h('tbody');
  if (!rows.length) tbody.append(h('tr', {}, h('td', { colSpan: columns.length, class: 'muted' }, emptyText)));
  for (const r of rows) {
    tbody.append(h('tr', {}, columns.map((c) => h('td', {}, c.render ? c.render(r) : fmt(r[c.key])))));
  }
  return h('table', { class: 'tbl' }, colgroup, thead, tbody);
}

function fmt(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** tabs([{id, label}], active, onChange) -> {el, set(id)} */
export function tabs(items, active, onChange) {
  const el = h('div', { class: 'tabs', role: 'tablist' });
  const btns = new Map();
  for (const it of items) {
    const b = h('button', { type: 'button', on: { click: () => set(it.id) } }, it.label);
    btns.set(it.id, b);
    el.append(b);
  }
  function set(id) {
    for (const [k, b] of btns) b.classList.toggle('active', k === id);
    onChange(id);
  }
  set(active);
  return { el, set };
}

// ---------------------------------------------------------------- toast / 对话框
export function toast(msg, kind = '', ms = 3500) {
  let box = document.getElementById('toasts');
  if (!box) {
    box = h('div', { id: 'toasts', 'aria-live': 'polite' });
    document.body.append(box);
  }
  const t = h('div', { class: `toast ${kind}` }, msg);
  box.append(t);
  setTimeout(() => t.remove(), ms);
}

export function modal(title, body, buttons = []) {
  return new Promise((resolve) => {
    const close = (v) => {
      mask.remove();
      document.removeEventListener('keydown', onKey);
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') close(null);
    };
    const footer = h('footer', {}, buttons.map((b) =>
      h('button', { type: 'button', class: b.class || '', on: { click: () => close(b.value ? b.value() : b.result) } }, b.text)));
    const mask = h('div', { class: 'modal-mask', on: { click: (e) => e.target === mask && close(null) } },
      h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' }, h('header', {}, title), h('div', { class: 'body' }, body), footer));
    document.body.append(mask);
    document.addEventListener('keydown', onKey);
    const first = mask.querySelector('input, textarea, select') || footer.lastChild;
    first?.focus();
  });
}

export function confirmDialog(message, { okText = '确定', danger = false, title = '请确认' } = {}) {
  return modal(title, h('div', {}, message), [
    { text: '取消', result: false },
    { text: okText, class: danger ? 'danger solid' : 'primary', result: true },
  ]).then((v) => v === true);
}

export function promptDialog(title, { label = '', placeholder = '', password = false, multiline = false, value = '' } = {}) {
  const input = multiline
    ? h('textarea', { rows: 6, placeholder, value })
    : h('input', { type: password ? 'password' : 'text', placeholder, value, style: { width: '100%' } });
  const body = h('div', { class: 'stack' }, label ? h('div', { class: 'muted' }, label) : null, input);
  if (!multiline) input.addEventListener('keydown', (e) => e.key === 'Enter' && body.closest('.modal').querySelector('footer .primary').click());
  return modal(title, body, [
    { text: '取消', result: null },
    { text: '确定', class: 'primary', value: () => input.value },
  ]);
}

/** 异步按钮：进行中禁用，出错 toast */
export async function busy(btn, fn) {
  if (btn) btn.disabled = true;
  try {
    return await fn();
  } catch (e) {
    toast(e.message || String(e), 'bad', 5000);
    return undefined;
  } finally {
    if (btn) btn.disabled = false;
  }
}

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

export function fmtDateTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

export function logList(max = 200) {
  const el = h('ol', { class: 'log' });
  return {
    el,
    add(text, cls = '') {
      el.prepend(h('li', { class: cls }, `${new Date().toLocaleTimeString('zh-CN', { hour12: false })}  ${text}`));
      while (el.children.length > max) el.lastChild.remove();
    },
  };
}
