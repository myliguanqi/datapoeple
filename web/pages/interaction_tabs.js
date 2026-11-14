// 弹幕互动页的三个标签：决策日志（事件 → 触发器 → 台词）、触发器表（行内编辑，热更新）、观众（长期记忆）。
import { badge, busy, confirmDialog, fmtDateTime, fmtTime, h, promptDialog, table, toast } from '../ui.js';

const STATUS = {
  queued: ['', '排队'], composing: ['warn', '写词中'], speaking: ['ok', '播报中'], spoken: ['ok', '已说'],
  interrupted: ['warn', '被打断'], dropped: ['', '丢弃'], failed: ['bad', '失败'], timeout: ['warn', '超时'], empty: ['', '无内容'],
};
const SOURCE = { template: '模板', llm: 'LLM', knowledge: '知识库', manual: '人工', script: '讲解稿' };

export function statusBadge(s) {
  const [k, t] = STATUS[s] || ['', s || '—'];
  return badge(t, k);
}

// ---------------------------------------------------------------- 决策日志
export function logTab(pane, app) {
  const rows = new Map();
  const tbody = h('tbody');
  const tbl = h('table', { class: 'tbl' },
    h('colgroup', {}, ...['70px', '18%', '120px', '', '70px', '26%', '90px', '70px'].map((w) => h('col', { style: w ? { width: w } : {} }))),
    h('thead', {}, h('tr', {}, ...['时间', '来源事件', '触发器', '原因', '来源', '最终台词', '耗时', '状态'].map((x) => h('th', {}, x)))), tbody);
  pane.append(h('p', { class: 'hint' }, '每一行是导演的一次决策：哪个事件 → 命中哪个触发器 → 用模板还是 LLM 写的词 → 说了什么（合规命中会标出来）。'),
    h('div', { class: 'scroll-y', style: { maxHeight: '62vh' } }, tbl));
  const render = (e) => {
    const src = e.source || {};
    const tr = h('tr', {},
      h('td', { class: 'num' }, fmtTime(e.ts)),
      h('td', {}, src.nickname ? h('span', {}, h('b', {}, src.nickname), src.content ? ` ${src.content}` : '') : h('span', { class: 'muted' }, e.kind === 'manual' ? '人工插话' : '—')),
      h('td', {}, h('code', {}, e.event), ' ', badge(String(e.priority), e.priority >= 50 ? 'warn' : '')),
      h('td', { class: 'muted' }, e.reason || ''),
      h('td', {}, e.text_source ? badge(SOURCE[e.text_source] || e.text_source, e.text_source === 'llm' || e.text_source === 'knowledge' ? 'accent' : '') : '—'),
      h('td', {}, e.text || h('span', { class: 'muted' }, e.fallback_text || ''),
        (e.compliance_hits || []).length ? h('div', { class: 'hint err-text' }, `合规替换：${e.compliance_hits.join('、')}`) : '',
        e.note ? h('div', { class: 'hint' }, e.note) : ''),
      h('td', { class: 'num muted' }, [e.compose_ms != null ? `写词 ${e.compose_ms}ms` : '', e.latency_ms != null ? `排队 ${Math.round(e.latency_ms / 100) / 10}s` : ''].filter(Boolean).join(' · ')),
      h('td', {}, statusBadge(e.status)));
    const old = rows.get(e.id);
    if (old) old.replaceWith(tr);
    else tbody.prepend(tr);
    rows.set(e.id, tr);
    while (tbody.children.length > 300) {
      const last = tbody.lastChild;
      for (const [k, v] of rows) if (v === last) rows.delete(k);
      last.remove();
    }
  };
  const onAction = (ev) => render(ev.detail.data);
  const onCue = (ev) => {
    const c = ev.detail.data;
    if (!rows.has(c.id)) render({ id: c.id, ts: c.created_at, event: c.event, kind: c.kind, priority: c.priority, reason: c.reason, source: c.source, status: 'queued', fallback_text: c.fallback_text });
  };
  app.events.addEventListener('topic:host.action', onAction);
  app.events.addEventListener('topic:interaction.cue', onCue);
  app.api.get('/api/interaction/log?limit=200').then((items) => {
    for (const e of items.reverse()) render(e);
    if (!items.length) tbody.append(h('tr', {}, h('td', { colSpan: 8, class: 'muted' }, '还没有决策：连接弹幕或在「模拟」页注入事件')));
  }).catch(() => {});
  return () => {
    app.events.removeEventListener('topic:host.action', onAction);
    app.events.removeEventListener('topic:interaction.cue', onCue);
  };
}

// ---------------------------------------------------------------- 触发器表
export function triggersTab(pane, app, { base = '/api/interaction/triggers', readOnly = false, addable = false } = {}) {
  const box = h('div');
  const addBtn = h('button', { type: 'button', class: 'small primary' }, '+ 添加触发器');
  let ro = readOnly;
  const reload = async () => {
    if (base === '/api/interaction/triggers') {           // 出厂模板只读：先复制再改
      try {
        const d = await app.api.get('/api/personas');
        ro = !!d.active?.builtin;
      } catch { /* 用传入的 readOnly */ }
    }
    const trs = await app.api.get(base);
    box.replaceChildren(h('div', { class: 'stack' }, ...trs.map((t) => triggerRow(t, app, reload, base, ro))));
    addBtn.hidden = !addable || ro;
  };
  addBtn.onclick = async () => {
    const name = await promptDialog('添加触发器', { label: '触发器名（小写字母/数字/下划线，和意图名同名时由该意图触发）' });
    if (!name || !name.trim()) return;
    await busy(addBtn, async () => {
      await app.api.post(base, { name: name.trim(), when: 'always', priority: 0, enabled: true, lines: [] });
      toast('已添加触发器，请补 when 和话术', 'ok');
      reload();
    });
  };
  pane.append(h('p', { class: 'hint' }, '按优先级从高到低。改完点该行的「保存」立即生效（写进当前人设模板）。',
    ' 优先级 ≥ 紧急优先级（默认 24）的不受发声间隔限制；≥ 打断讲品优先级（默认 50）的会打断讲品。话术里的 {占位符} 见 docs/live/PERSONAS.md。'
    + (readOnly ? ' 出厂模板只读：先「复制为新模板」再改。' : '')), addBtn, box);
  reload();
  return null;
}

function triggerRow(t, app, reload, base = '/api/interaction/triggers', readOnly = false) {
  const en = h('input', { type: 'checkbox', checked: t.enabled, disabled: readOnly });
  const when = h('input', { type: 'text', value: t.when || '', style: { width: '11em' }, disabled: readOnly,
    title: '平台信号，或当前人设某个意图的英文名；always = 恒真兜底' });
  const pri = h('input', { type: 'number', min: 0, max: 100, value: t.priority, style: { width: '5em' }, disabled: readOnly });
  const cd = h('input', { type: 'number', min: 0, step: 1, value: t.cooldown_s, style: { width: '5em' }, disabled: readOnly });
  const emo = h('input', { type: 'text', value: t.emotion || '', style: { width: '7em' }, disabled: readOnly });
  const energy = h('input', { type: 'number', min: 0, max: 1, step: 0.05, value: t.target_energy, style: { width: '5em' }, disabled: readOnly });
  const stale = h('input', { type: 'number', min: 0, value: t.max_stale_s ?? '', placeholder: '默认', style: { width: '5em' }, disabled: readOnly });
  const role = h('select', { disabled: readOnly }, new Option('（不合并）', ''), new Option('who 点名', 'who'), new Option('what 场面', 'what'));
  role.value = t.role || '';
  const mergeClause = h('input', { type: 'text', value: t.merge_clause || '', placeholder: '合并子句（可空）', style: { width: '14em' }, disabled: readOnly });
  const mergeWith = h('input', { type: 'text', value: (t.merge_with || []).join('，'), placeholder: '可合并的触发器名', style: { width: '13em' }, disabled: readOnly });
  const llm = h('input', { type: 'checkbox', checked: t.use_llm, disabled: readOnly });
  const lines = h('textarea', { rows: Math.min(5, Math.max(2, t.lines.length)), value: t.lines.join('\n'), disabled: readOnly });
  const directive = h('input', { type: 'text', value: t.directive || '', style: { width: '100%' }, disabled: readOnly });
  const save = h('button', { type: 'button', class: 'primary small' }, '保存');
  const reset = h('button', { type: 'button', class: 'small' }, '恢复默认');
  const del = h('button', { type: 'button', class: 'small danger' }, '删除');
  save.hidden = readOnly;
  reset.hidden = readOnly;
  del.hidden = readOnly;
  save.onclick = () => busy(save, async () => {
    await app.api.put(`${base}/${t.name}`, {
      enabled: en.checked, when: when.value.trim(), priority: +pri.value, cooldown_s: +cd.value, emotion: emo.value,
      target_energy: +energy.value, max_stale_s: stale.value === '' ? null : +stale.value, use_llm: llm.checked,
      role: role.value || '', merge_clause: mergeClause.value || null,
      merge_with: mergeWith.value.split(/[，,]/).map((x) => x.trim()).filter(Boolean),
      directive: directive.value, lines: lines.value.split('\n').map((x) => x.trim()).filter(Boolean),
    });
    toast(`触发器 ${t.name} 已保存，立即生效`, 'ok');
    reload();
  });
  reset.onclick = async () => {
    if (!(await confirmDialog(`把触发器 ${t.name} 恢复成复制来源（出厂模板）里的值？`, { okText: '恢复默认' }))) return;
    await busy(reset, () => app.api.post(`${base}/${t.name}/reset`));
    reload();
  };
  del.onclick = async () => {
    if (!(await confirmDialog(`删除触发器 ${t.name}？`, { okText: '删除', danger: true }))) return;
    await busy(del, () => app.api.del(`${base}/${t.name}`));
    reload();
  };
  return h('div', { class: 'card', style: { padding: '8px 12px' } },
    h('div', { class: 'row' },
      h('label', { class: 'switch', title: '启用' }, en, h('span')), h('b', {}, t.name),
      h('label', { class: 'inline' }, 'when', when),
      (t.overridden || []).length ? badge(`已改：${t.overridden.join('、')}`, 'accent') : '',
      t.last_fired ? h('span', { class: 'hint' }, `上次触发 ${fmtTime(t.last_fired)}`) : '',
      h('span', { class: 'grow' }),
      h('label', { class: 'inline' }, '优先级', pri), h('label', { class: 'inline' }, '冷却(秒)', cd), del, save, reset),
    h('div', { class: 'row' },
      h('label', { class: 'inline' }, '情绪', emo), h('label', { class: 'inline' }, '能量', energy),
      h('label', { class: 'inline' }, '保鲜(秒)', stale),
      h('label', { class: 'inline' }, '合并角色', role), h('label', { class: 'inline' }, '合并子句', mergeClause),
      h('label', { class: 'inline' }, '合并对象', mergeWith),
      h('label', { class: 'inline' }, h('span', { class: 'switch' }, llm, h('span')), 'LLM 写词')),
    h('div', { class: 'row' }, h('span', { class: 'hint' }, '指令（给 LLM）'), directive),
    h('div', { class: 'row' }, h('div', { class: 'stack', style: { flex: 1, gap: '2px' } }, h('span', { class: 'hint' }, '兜底话术（一行一条，随机挑）'), lines)));
}

// ---------------------------------------------------------------- 观众（长期记忆）
export function viewersTab(pane, app) {
  const q = h('input', { type: 'search', placeholder: '搜昵称或用户 ID' });
  const stats = h('span', { class: 'hint' });
  const box = h('div');
  const load = async () => {
    const r = await app.api.get(`/api/viewers?limit=200&q=${encodeURIComponent(q.value.trim())}`);
    stats.textContent = `共 ${r.stats.users} 位（送过礼 ${r.stats.donors}，开播前就认识 ${r.stats.known_at_boot}）`;
    box.replaceChildren(table([
      { label: '昵称', render: (v) => h('span', {}, v.name || '—', ' ', h('span', { class: 'muted' }, { male: '♂', female: '♀' }[v.gender] || '·')) },
      { label: '标签', width: '110px', render: (v) => h('span', {}, v.known_before && v.gift_value >= 50 ? badge('金主', 'warn') : '', ' ',
        v.known_before && v.visits >= 3 ? badge('熟客', 'ok') : '', v.max_wealth_level ? badge(`💎${v.max_wealth_level}`) : '', v.fans_level ? badge(`🎫${v.fans_level}`, 'accent') : '') },
      { label: '进场', key: 'visits', width: '52px' },
      { label: '送礼', width: '100px', render: (v) => `${v.gift_count} 个 / ${v.gift_value} 钻` },
      { label: '发言', key: 'chat_count', width: '52px' },
      { label: '最近提问', render: (v) => v.last_question || '—' },
      { label: '最近互动', width: '140px', render: (v) => fmtDateTime(v.last_seen) },
      { label: '', width: '64px', render: (v) => h('button', { type: 'button', class: 'small danger', on: { click: () => del(v) } }, '删除') },
    ], r.items, { emptyText: '还没有建档的观众（发过弹幕 / 送过礼 / 关注过才会记住；纯进场不记）' }));
  };
  const del = async (v) => {
    if (!(await confirmDialog(`删除观众「${v.name || v.user_id}」的长期记忆记录？`, { okText: '删除', danger: true }))) return;
    await busy(null, () => app.api.del(`/api/viewers/${encodeURIComponent(v.user_id)}`));
    load();
  };
  q.oninput = () => {
    clearTimeout(q._t);
    q._t = setTimeout(load, 250);
  };
  pane.append(h('div', { class: 'row' }, q, stats), h('p', { class: 'hint' }, '只存昵称、性别、财富等级、粉丝团等级、进场/送礼/发言统计和最近一次提问；头像、城市、签名不落盘。'), box);
  load();
  return null;
}
