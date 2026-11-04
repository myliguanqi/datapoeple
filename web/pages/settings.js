// 直播间设置：由 GET /api/settings 的 schema 自动生成表单。每项标「即时生效 / 需重启」，改动后底部出现保存条。
import { badge, busy, card, cardHead, confirmDialog, fmtDateTime, h, modal, table, toast } from '../ui.js';
import { cookieText, pasteCookie } from '../live_events.js';

export const title = '直播间设置';
let cleanup = [];

const SOURCE_LABEL = { default: '默认', yaml: '配置文件', runtime: 'Web 修改', cli: '启动参数' };

export async function mount(el, app) {
  let schema = null;
  let group = location.hash.split('?g=')[1] || null;
  const dirty = new Map(); // key -> 新值
  const controls = new Map(); // key -> {row, get, set, invalid}
  const search = h('input', { type: 'search', placeholder: '搜索设置（名称 / 键名 / 说明）' });
  const navEl = h('nav', { class: 'settings-nav' });
  const body = h('div', { class: 'stack' });
  const dirtyText = h('span', {});
  const saveBtn = h('button', { class: 'primary', type: 'button' }, '保存');
  const discardBtn = h('button', { type: 'button' }, '放弃');
  const savebar = h('div', { class: 'savebar', hidden: true }, dirtyText, h('span', { class: 'grow' }), discardBtn, saveBtn);
  const pendingBox = h('div');
  const exportBtn = h('button', { type: 'button' }, '导出');
  const importInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
  const importBtn = h('button', { type: 'button' }, '导入');
  const historyBtn = h('button', { type: 'button' }, '历史版本');
  const resetBtn = h('button', { type: 'button', class: 'danger' }, '恢复默认');

  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '直播间设置'), h('span', { class: 'grow' }),
      exportBtn, importBtn, importInput, historyBtn, resetBtn),
    pendingBox,
    h('div', { class: 'row', style: { marginBottom: '12px' } }, search),
    h('div', { class: 'settings-layout' }, navEl, h('div', { class: 'stack' }, body, savebar)));

  async function load() {
    schema = await app.api.get('/api/settings');
    if (!group || !schema.groups.some((g) => g.id === group)) group = schema.groups.find((g) => countIn(g.id))?.id || schema.groups[0].id;
    render();
  }
  const countIn = (gid) => schema.fields.filter((f) => f.group === gid).length;

  function render() {
    const q = search.value.trim().toLowerCase();
    navEl.replaceChildren(
      ...schema.groups.filter((g) => countIn(g.id) || g.id === 'secrets').map((g) =>
        h('a', { href: '#', class: !q && g.id === group ? 'active' : '', on: { click: (e) => { e.preventDefault(); group = g.id; search.value = ''; render(); } } },
          h('span', {}, g.label), h('span', { class: 'muted num' }, countIn(g.id)))),
      h('a', { href: '#', class: !q && group === 'secrets' ? 'active' : '', on: { click: (e) => { e.preventDefault(); group = 'secrets'; search.value = ''; render(); } } },
        h('span', {}, '密钥与凭证'), h('span', {}, '')));
    controls.clear();
    body.replaceChildren();
    if (!q && group === 'secrets') {
      renderSecrets();
      return;
    }
    const fields = schema.fields.filter((f) => (q ? `${f.label} ${f.key} ${f.help}`.toLowerCase().includes(q) : f.group === group));
    const title = q ? `搜索「${search.value.trim()}」：${fields.length} 项` : schema.groups.find((g) => g.id === group)?.label;
    const list = h('div');
    for (const f of fields) list.append(fieldRow(f));
    body.append(card(title, fields.length ? list : h('p', { class: 'muted' }, q ? '没有匹配的设置项' : '这一组的设置项会在后续阶段加入。')));
    renderPending();
  }

  function renderPending() {
    const keys = schema.pending_restart || [];
    pendingBox.replaceChildren(keys.length
      ? card(null, h('div', { class: 'row' }, badge('需重启', 'warn'),
        h('span', {}, `已保存但需要重启服务才生效：${keys.join('、')}`)))
      : '');
  }

  function fieldRow(f) {
    const has = dirty.has(f.key);
    const value = has ? dirty.get(f.key) : f.value;
    const err = h('div', { class: 'f-err' });
    const ctrl = makeControl(f, value, (v, invalid) => {
      row.classList.toggle('invalid', !!invalid);
      err.textContent = invalid || '';
      if (invalid) return;
      if (JSON.stringify(v) === JSON.stringify(f.value)) dirty.delete(f.key);
      else dirty.set(f.key, v);
      row.classList.toggle('dirty', dirty.has(f.key));
      updateBar();
    });
    const meta = h('div', { class: 'f-meta' },
      f.apply === 'hot' ? badge('即时生效', 'ok') : badge('需重启', 'warn'),
      f.pending_restart ? badge('已保存，待重启', 'warn') : '',
      f.source !== 'default' ? badge(SOURCE_LABEL[f.source] || f.source, f.source === 'runtime' ? 'accent' : '') : '',
      f.locked ? badge(f.source === 'cli' ? '由启动参数指定' : '只读', '') : '',
      f.source === 'runtime' && !f.locked
        ? h('button', { type: 'button', class: 'link small', on: { click: () => resetKey(f.key) } }, '恢复为配置文件/默认值')
        : '');
    const row = h('div', { class: `field${has ? ' dirty' : ''}` },
      h('div', { class: 'f-label' }, f.label, h('span', { class: 'f-key' }, f.key)),
      h('div', { class: 'f-ctrl' }, ctrl.el, f.help ? h('div', { class: 'hint' }, f.help) : '', meta, err));
    controls.set(f.key, { row, err });
    return row;
  }

  function updateBar() {
    savebar.hidden = dirty.size === 0;
    dirtyText.textContent = `未保存 ${dirty.size} 项`;
  }

  saveBtn.onclick = () => busy(saveBtn, async () => {
    try {
      const r = await app.api.put('/api/settings', { changes: Object.fromEntries(dirty) });
      dirty.clear();
      updateBar();
      const parts = [];
      if (r.applied.length) parts.push(`已即时生效 ${r.applied.length} 项`);
      if (r.restart.length) parts.push(`${r.restart.length} 项需重启后生效`);
      toast(parts.join('，') || '没有变化', r.restart.length ? 'warn' : 'ok', 5000);
      await load();
    } catch (e) {
      const errs = e.body && e.body.errors;
      if (!errs) throw e;
      for (const [k, msg] of Object.entries(errs)) {
        const c = controls.get(k);
        if (c) {
          c.row.classList.add('invalid');
          c.err.textContent = msg;
        }
      }
      toast(`有 ${Object.keys(errs).length} 项不合法：${Object.entries(errs).map(([k, m]) => `${k} ${m}`).join('；')}`, 'bad', 6000);
    }
  });
  discardBtn.onclick = () => {
    dirty.clear();
    updateBar();
    render();
  };
  async function resetKey(key) {
    await busy(null, async () => {
      await app.api.post('/api/settings/reset', { keys: [key] });
      dirty.delete(key);
      updateBar();
      toast('已恢复', 'ok');
      await load();
    });
  }
  resetBtn.onclick = async () => {
    if (!(await confirmDialog('清除所有在 Web 上改过的设置，回到配置文件 / 默认值？（会保存一份历史版本，可回滚）', { okText: '恢复默认', danger: true }))) return;
    await busy(resetBtn, async () => {
      const r = await app.api.post('/api/settings/reset', {});
      dirty.clear();
      updateBar();
      toast(`已恢复默认${r.restart.length ? `（${r.restart.length} 项需重启）` : ''}`, 'ok');
      await load();
    });
  };
  exportBtn.onclick = () => busy(exportBtn, async () => {
    const data = await app.api.get('/api/settings/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = h('a', { href: URL.createObjectURL(blob), download: `flashhead-settings-v${data.version}.json` });
    document.body.append(a);
    a.click();
    a.remove();
  });
  importBtn.onclick = () => importInput.click();
  importInput.onchange = () => busy(importBtn, async () => {
    const f = importInput.files[0];
    importInput.value = '';
    if (!f) return;
    const data = JSON.parse(await f.text());
    const overrides = data.overrides || data;
    if (!(await confirmDialog(`导入 ${Object.keys(overrides).length} 项设置，替换当前所有 Web 修改？`, { okText: '导入' }))) return;
    const r = await app.api.post('/api/settings/import', { overrides });
    toast(`已导入（即时生效 ${r.applied.length} 项，需重启 ${r.restart.length} 项）`, 'ok');
    await load();
  });
  historyBtn.onclick = () => busy(historyBtn, async () => {
    const hist = await app.api.get('/api/settings/history');
    const box = h('div', { class: 'scroll-y' }, table([
      { label: '版本', render: (x) => h('span', {}, `v${x.version} `, x.current ? badge('当前', 'accent') : ''), width: '90px' },
      { label: '时间', render: (x) => fmtDateTime(x.ts), width: '150px' },
      { label: '来源', key: 'source', width: '90px' },
      { label: '改动', render: (x) => (x.keys || []).join('、') || '—' },
      { label: '', width: '60px', render: (x) => x.current ? '' : h('button', { type: 'button', class: 'small', on: { click: () => rollback(x.version) } }, '回滚') },
    ], hist, { emptyText: '还没有历史版本' }));
    await modal('设置历史版本（最近 20 份）', box, [{ text: '关闭', result: null }]);
  });
  async function rollback(v) {
    document.querySelector('.modal-mask')?.remove();
    if (!(await confirmDialog(`回滚到 v${v} 时的 Web 设置？`, { okText: '回滚' }))) return;
    await busy(null, async () => {
      const r = await app.api.post(`/api/settings/rollback/${v}`);
      toast(`已回滚到 v${v}（即时生效 ${r.applied.length} 项，需重启 ${r.restart.length} 项）`, 'ok');
      await load();
    });
  }
  search.oninput = () => render();

  async function renderSecrets() {
    const reloadBtn = h('button', { type: 'button' }, '重新加载 .env');
    const tbl = h('div');
    const fill = (items) => tbl.replaceChildren(table([
      { label: '键', render: (s) => h('code', {}, s.key), width: '34%' },
      { label: '说明', key: 'desc' },
      { label: '状态', width: '150px', render: (s) => s.configured
        ? h('span', {}, badge('已配置', 'ok'), ' ', h('span', { class: 'muted' }, s.source === 'env' ? '环境变量' : '.env'))
        : badge('未配置', '') },
      { label: '值', width: '22%', render: (s) => (s.secret ? h('span', { class: 'muted' }, '不显示') : s.value || '—') },
    ], items));
    reloadBtn.onclick = () => busy(reloadBtn, async () => {
      const r = await app.api.post('/api/settings/reload-env');
      fill(r.secrets);
      toast(r.changed.length ? `已重新加载，变化：${r.changed.join('、')}` : '已重新加载，没有变化', 'ok');
    });
    fill(await app.api.get('/api/settings/secrets-status'));
    body.append(card(cardHead('密钥与凭证（.env）', reloadBtn),
      h('p', { class: 'hint' }, '密钥只放在项目根目录的 .env（参考 .env.example），这里只显示是否已配置，不显示内容。改完 .env 点「重新加载」即可，不用重启。'),
      tbl));
    const cookieState = h('span', {});
    const pasteBtn = h('button', { type: 'button' }, '粘贴新 cookie');
    const clearBtn = h('button', { type: 'button', class: 'danger' }, '清除');
    const loadCookie = async () => {
      const st = await app.api.get('/api/danmaku/status');
      cookieState.replaceChildren(st.cookie && st.cookie.configured ? badge('已配置', 'ok') : badge('未配置', ''), ' ', cookieText(st.cookie));
    };
    pasteBtn.onclick = async () => {
      await pasteCookie(app);
      loadCookie();
    };
    clearBtn.onclick = async () => {
      if (!(await confirmDialog('删除本机保存的抖音登录 cookie？之后以游客态连接，收不到礼物。', { okText: '删除', danger: true }))) return;
      await busy(clearBtn, () => app.api.del('/api/danmaku/cookie'));
      loadCookie();
    };
    body.append(card(cardHead('抖音登录 cookie', pasteBtn, clearBtn),
      h('p', { class: 'hint' }, '游客模式收不到礼物。cookie 等同账号密码：只存本机 secrets/（路径可用 .env 的 DOUYIN_COOKIE_FILE 改），网页只写不读。'),
      cookieState));
    loadCookie();
  }

  const onChanged = () => {
    if (!dirty.size) load();
  };
  app.events.addEventListener('topic:settings.changed', onChanged);
  cleanup.push(() => app.events.removeEventListener('topic:settings.changed', onChanged));
  await load();
}

// ---------------------------------------------------------------- 控件
function makeControl(f, value, onChange) {
  const disabled = !!f.locked;
  const t = f.type;
  if (t === 'bool') {
    const input = h('input', { type: 'checkbox', checked: !!value, disabled });
    input.onchange = () => onChange(input.checked);
    return { el: h('label', { class: 'switch' }, input, h('span')) };
  }
  if (t === 'int' || t === 'float') {
    const input = h('input', { type: 'number', value: value ?? '', disabled, step: t === 'int' ? 1 : 'any' });
    if (f.min != null) input.min = f.min;
    if (f.max != null) input.max = f.max;
    input.oninput = () => {
      const s = input.value.trim();
      if (s === '') return onChange(null, f.nullable ? null : '不能为空');
      const n = Number(s);
      if (!Number.isFinite(n)) return onChange(null, '应为数字');
      if (t === 'int' && !Number.isInteger(n)) return onChange(null, '应为整数');
      if (f.min != null && n < f.min) return onChange(null, `不能小于 ${f.min}`);
      if (f.max != null && n > f.max) return onChange(null, `不能大于 ${f.max}`);
      onChange(n);
    };
    const range = f.min != null || f.max != null ? h('span', { class: 'hint' }, `范围 ${f.min ?? '-∞'} ~ ${f.max ?? '∞'}`) : '';
    return { el: h('div', { class: 'row' }, input, range) };
  }
  if (t === 'enum') {
    const sel = h('select', { disabled }, ...f.choices.map((c) => new Option(String(c), String(c))));
    sel.value = String(value);
    sel.onchange = () => onChange(f.choices.find((c) => String(c) === sel.value));
    return { el: sel };
  }
  if (t === 'text') {
    const ta = h('textarea', { rows: 8, disabled, value: value ?? '' });
    const count = h('span', { class: 'hint num' });
    const upd = () => (count.textContent = `${ta.value.length} 字${f.max ? ` / ${f.max}` : ''}`);
    ta.oninput = () => {
      upd();
      onChange(ta.value);
    };
    upd();
    return { el: h('div', { class: 'stack', style: { gap: '4px' } }, ta, count) };
  }
  if (t === 'list' && f.item_choices && f.item_choices.length) {
    const set = new Set(value || []);
    const boxes = f.item_choices.map((c) => {
      const cb = h('input', { type: 'checkbox', checked: set.has(c), disabled });
      cb.onchange = () => onChange(f.item_choices.filter((x, i) => boxes[i].checked));
      return cb;
    });
    return { el: h('div', { class: 'row' }, ...f.item_choices.map((c, i) => h('label', { class: 'inline' }, boxes[i], String(c)))) };
  }
  if (t === 'list') {
    const input = h('input', { type: 'text', disabled, value: value == null ? '' : value.join(', '), placeholder: f.nullable ? '留空 = 不限' : '用逗号分隔' });
    input.oninput = () => {
      const s = input.value.trim();
      if (!s) return onChange(f.nullable ? null : [], null);
      const parts = s.split(/[,，\s]+/).filter(Boolean);
      if (f.item_type === 'int') {
        if (parts.some((p) => !/^-?\d+$/.test(p))) return onChange(null, '应为整数列表');
        return onChange(parts.map(Number));
      }
      if (f.item_type === 'float') return onChange(parts.map(Number));
      onChange(parts);
    };
    return { el: input };
  }
  if (t === 'dict') {
    const ta = h('textarea', { rows: 4, disabled, class: 'mono', value: value == null ? '' : JSON.stringify(value, null, 2) });
    ta.style.fontFamily = 'ui-monospace, Consolas, monospace';
    ta.oninput = () => {
      const s = ta.value.trim();
      if (!s) return onChange(f.nullable ? null : {}, f.nullable ? null : null);
      try {
        const v = JSON.parse(s);
        if (typeof v !== 'object' || Array.isArray(v) || v === null) return onChange(null, '应为 JSON 对象');
        onChange(v);
      } catch {
        onChange(null, 'JSON 格式不对');
      }
    };
    return { el: ta };
  }
  const input = h('input', { type: 'text', disabled, value: value ?? '', style: { width: 'min(100%, 480px)' } });
  input.oninput = () => onChange(input.value.trim() === '' && f.nullable ? null : input.value);
  return { el: input };
}

export function unmount() {
  for (const fn of cleanup) fn();
  cleanup = [];
}
