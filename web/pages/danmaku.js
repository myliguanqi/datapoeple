// 弹幕互动：连接直播间（抖音 / 模拟 / 回放）、状态、事件流、模拟注入。后续阶段加决策日志、触发器、观众（长期记忆）。
import { badge, busy, card, confirmDialog, fmtDateTime, fmtTime, h, tabs, toast } from '../ui.js';
import { TYPE_LABEL, cookieText, describe, pasteCookie, profileCard, typeBadge, userTags } from '../live_events.js';
import { logTab, statusBadge, triggersTab, viewersTab } from './interaction_tabs.js';

export const title = '弹幕互动';
let cleanup = [];
const TAB_KEY = 'flashhead.danmakuTab';

export async function mount(el, app) {
  const on = (target, type, fn) => {
    target.addEventListener(type, fn);
    cleanup.push(() => target.removeEventListener(type, fn));
  };
  const cfg = await settingsMap(app);

  // ---------------------------------------------------------------- 顶部：连接与状态
  const sourceSel = h('select', {}, new Option('抖音直播间', 'douyin'), new Option('模拟弹幕', 'mock'), new Option('回放录制', 'replay'));
  sourceSel.value = cfg['danmaku.source'];
  const liveId = h('input', { type: 'text', placeholder: '抖音直播间号：live.douyin.com/ 后面那串数字', value: cfg['danmaku.live_id'] || '' });
  const connectBtn = h('button', { type: 'button', class: 'primary' }, '连接');
  const disconnectBtn = h('button', { type: 'button', class: 'danger' }, '断开');
  const cookieBtn = h('button', { type: 'button' }, '粘贴 cookie');
  const recordSw = h('input', { type: 'checkbox', checked: !!cfg['danmaku.record'] });
  const sniffSw = h('input', { type: 'checkbox', checked: !!cfg['danmaku.sniff'] });
  const stateBox = h('div', { class: 'row' });
  const liveIdRow = h('label', { class: 'inline grow' }, liveId);
  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '弹幕互动')),
    card(null,
      h('div', { class: 'row' }, sourceSel, liveIdRow, connectBtn, disconnectBtn),
      stateBox,
      h('div', { class: 'row' }, cookieBtn,
        h('label', { class: 'inline' }, h('span', { class: 'switch' }, recordSw, h('span')), '录制事件'),
        h('label', { class: 'inline', title: '记录首次出现的消息类型、裸解未适配消息（排查用）' }, h('span', { class: 'switch' }, sniffSw, h('span')), '协议探针'),
        h('span', { class: 'hint' }, '只读接入：只接收直播间消息，不发弹幕、不点赞、不私信。'))));
  const syncSource = () => (liveIdRow.hidden = sourceSel.value !== 'douyin');
  sourceSel.onchange = syncSource;
  syncSource();

  connectBtn.onclick = () => busy(connectBtn, async () => {
    const src = sourceSel.value;
    if (src === 'replay') {
      tabCtl.set('sim');
      toast('在「模拟」页选择录制文件后点「回放」', 'warn');
      return;
    }
    await app.api.post('/api/danmaku/connect', { source: src, live_id: src === 'douyin' ? liveId.value.trim() : undefined });
    toast('正在连接…', 'ok');
    refresh();
  });
  disconnectBtn.onclick = async () => {
    if (!(await confirmDialog('断开弹幕连接？互动引擎将收不到新弹幕。', { okText: '断开', danger: true }))) return;
    await busy(disconnectBtn, () => app.api.post('/api/danmaku/disconnect'));
    refresh();
  };
  cookieBtn.onclick = async () => {
    await pasteCookie(app);
    refresh();
  };
  recordSw.onchange = () => busy(null, async () => {
    if (recordSw.checked) toast('录制文件包含观众昵称等信息，注意隐私（提交测试数据前要去标识化）', 'warn', 6000);
    await app.api.put('/api/settings', { changes: { 'danmaku.record': recordSw.checked } });
  });
  sniffSw.onchange = () => busy(null, () => app.api.put('/api/settings', { changes: { 'danmaku.sniff': sniffSw.checked } }));

  function renderState(st) {
    if (!st) return;
    const map = { connected: ['ok', '已连接'], connecting: ['warn', '连接中'], starting: ['warn', '启动中'], backoff: ['bad', '等待重连'],
      stopped: ['', '未连接'], error: ['bad', '错误'], disconnected: ['warn', '已断开'], finished: ['', '回放结束'] };
    const [kind, text] = map[st.state] || ['', st.state];
    const items = [badge(text, kind)];
    if (st.source) items.push(h('span', { class: 'muted' }, `${{ douyin: '抖音', mock: '模拟', replay: '回放' }[st.source] || st.source} ${st.source === 'douyin' ? st.live_id || '' : ''}`));
    if (st.room_id && st.source === 'douyin') items.push(h('span', { class: 'muted' }, `room_id ${st.room_id}`));
    if (st.source === 'douyin') items.push(badge(st.logged_in ? '登录态' : '游客态（收不到礼物）', st.logged_in ? 'ok' : 'warn'));
    items.push(h('span', { class: 'muted' }, `事件 ${st.events || 0}`));
    if (st.last_event_ts) items.push(h('span', { class: 'muted' }, `最近消息 ${fmtTime(st.last_event_ts)}`));
    if (st.restarts) items.push(h('span', { class: 'muted' }, `重连 ${st.restarts} 次`));
    if (st.state === 'backoff' && st.next_retry_in != null) items.push(h('span', { class: 'muted' }, `${st.next_retry_in}s 后重连`));
    items.push(h('span', { class: 'muted' }, `cookie：${cookieText(st.cookie)}`));
    if (st.error) items.push(h('span', { class: 'err-text' }, st.error));
    stateBox.replaceChildren(...items);
    connectBtn.textContent = st.enabled ? '重新连接' : '连接';
  }
  async function refresh() {
    try {
      renderState(await app.api.get('/api/danmaku/status'));
    } catch { /* 下次再试 */ }
  }
  on(app.events, 'topic:danmaku.status', (e) => renderState(e.detail.data));
  const timer = setInterval(refresh, 3000);
  cleanup.push(() => clearInterval(timer));
  refresh();

  // ---------------------------------------------------------------- 标签页
  const pane = h('div');
  const tabItems = [{ id: 'feed', label: '事件流' }, { id: 'log', label: '决策日志' }, { id: 'triggers', label: '触发器' },
    { id: 'viewers', label: '观众' }, { id: 'sim', label: '模拟' }];
  let saved = 'feed';
  try { saved = localStorage.getItem(TAB_KEY) || 'feed'; } catch { /* ignore */ }
  let paneCleanup = null;
  const tabCtl = tabs(tabItems, tabItems.some((t) => t.id === saved) ? saved : 'feed', (id) => {
    try { localStorage.setItem(TAB_KEY, id); } catch { /* ignore */ }
    paneCleanup?.();
    paneCleanup = null;
    pane.replaceChildren();
    if (id === 'feed') paneCleanup = feedTab(pane, app);
    else if (id === 'log') paneCleanup = logTab(pane, app);
    else if (id === 'triggers') paneCleanup = triggersTab(pane, app);
    else if (id === 'viewers') paneCleanup = viewersTab(pane, app);
    else if (id === 'sim') paneCleanup = simTab(pane, app, refresh);
  });
  el.append(card(null, tabCtl.el, pane));
  cleanup.push(() => paneCleanup?.());
}

async function settingsMap(app) {
  const s = await app.api.get('/api/settings');
  return Object.fromEntries(s.fields.map((f) => [f.key, f.value]));
}

// ---------------------------------------------------------------- 事件流
const FILTERS = ['chat', 'gift', 'like', 'member', 'follow', 'share', 'fansclub', 'room_stat', 'rank', 'other'];
function feedTab(pane, app) {
  const enabled = new Set(['chat', 'gift', 'like', 'member', 'follow', 'share', 'fansclub', 'other']);
  const resultCells = new Map();               // msg_id -> 「处理」单元格
  const results = app.cueResults || (app.cueResults = new Map());
  let paused = false;
  const chips = h('div', { class: 'row' }, ...FILTERS.map((t) => {
    const cb = h('input', { type: 'checkbox', checked: enabled.has(t) });
    cb.onchange = () => {
      if (cb.checked) enabled.add(t);
      else enabled.delete(t);
      render();
    };
    return h('label', { class: 'inline' }, cb, t === 'other' ? '其他' : TYPE_LABEL[t]);
  }));
  const pauseBtn = h('button', { type: 'button', class: 'small' }, '暂停滚动');
  const count = h('span', { class: 'hint' });
  pauseBtn.onclick = () => {
    paused = !paused;
    pauseBtn.textContent = paused ? '继续滚动' : '暂停滚动';
    if (!paused) render();
  };
  const tbody = h('tbody');
  const tbl = h('table', { class: 'tbl' },
    h('colgroup', {}, h('col', { style: { width: '72px' } }), h('col', { style: { width: '60px' } }), h('col', { style: { width: '34%' } }), h('col'), h('col', { style: { width: '120px' } })),
    h('thead', {}, h('tr', {}, ...['时间', '类型', '观众', '内容', '处理'].map((x) => h('th', {}, x)))), tbody);
  pane.append(h('div', { class: 'row' }, chips, h('span', { class: 'grow' }), count, pauseBtn), h('div', { class: 'scroll-y', style: { maxHeight: '60vh' } }, tbl));

  const base = (t) => (t.endsWith('_batch') ? t.slice(0, -6) : t);
  const visible = (e) => enabled.has(FILTERS.includes(base(e.type)) ? base(e.type) : 'other');
  const row = (e) => h('tr', {},
    h('td', { class: 'num' }, fmtTime(e.ts)), h('td', {}, typeBadge(e.type)),
    h('td', {}, e.user ? userTags(e.user, { onClick: (u) => showProfile(app, u), tags: e._tags }) : h('span', { class: 'muted' }, e.type === 'member_batch' ? `${e.count} 人` : '—')),
    h('td', {}, describe(e)), resultCell(e));
  function resultCell(e) {
    const td = h('td', { class: 'muted' });
    const mid = e.msg_id;
    if (mid) {
      resultCells.set(mid, td);
      fillResult(td, results.get(mid));
    }
    return td;
  }
  function fillResult(td, r) {
    if (!td || !r) return;
    td.replaceChildren(h('code', {}, r.event), ' ', statusBadge(r.status));
    td.title = r.text || r.fallback_text || '';
  }
  const onAction = (ev) => {
    const d = ev.detail.data;
    const mid = d.source && d.source.msg_id;
    if (!mid) return;
    const r = results.get(mid);                 // app.js 已按"第一条直接响应"合并好
    if (r && r.id === d.id) fillResult(resultCells.get(mid), r);
  };
  app.events.addEventListener('topic:host.action', onAction);
  app.events.addEventListener('topic:interaction.cue', onAction);
  function render() {
    const items = app.liveEvents.filter(visible).slice(-300).reverse();
    tbody.replaceChildren(...items.map(row));
    if (!items.length) tbody.append(h('tr', {}, h('td', { colSpan: 5, class: 'muted' }, '还没有弹幕事件：上方连接直播间，或在「模拟」页注入 / 开始模拟弹幕')));
    count.textContent = `${app.liveEvents.length} 条（显示 ${items.length}）`;
  }
  const onLive = (ev) => {
    if (paused || !visible(ev.detail)) return;
    tbody.prepend(row(ev.detail));
    if (tbody.querySelector('td[colspan]')) render();
    while (tbody.children.length > 300) tbody.lastChild.remove();
    count.textContent = `${app.liveEvents.length} 条`;
  };
  app.events.addEventListener('live', onLive);
  app.events.addEventListener('live-reset', render);
  render();
  return () => {
    app.events.removeEventListener('live', onLive);
    app.events.removeEventListener('live-reset', render);
    app.events.removeEventListener('topic:host.action', onAction);
    app.events.removeEventListener('topic:interaction.cue', onAction);
  };
}

async function showProfile(app, u) {
  let mem = null;
  if (u.user_id) {
    try { mem = await app.api.get(`/api/viewers/${encodeURIComponent(u.user_id)}`); } catch { /* 没建档 */ }
  }
  profileCard(u, mem);
}

// ---------------------------------------------------------------- 模拟
function simTab(pane, app, refreshState) {
  const sim = (body, btn) => busy(btn, () => app.api.post('/api/danmaku/simulate', body));
  const quick = [
    ['普通进场', { type: 'member', nickname: '路过的小李', gender: 'male' }],
    ['高等级进场', { type: 'member', nickname: '钻石大哥', gender: 'male', wealth_level: 30, fans_level: 8 }],
    ['弹幕', { type: 'chat', nickname: '李姐', gender: 'female', wealth_level: 3, content: '主播今天好漂亮' }],
    ['产品提问', { type: 'chat', nickname: '王阿姨', gender: 'female', wealth_level: 6 }],
    ['购买意向', { type: 'chat', nickname: '小张', gender: 'male', content: '几号链接，我要了' }],
    ['小礼物', { type: 'gift', nickname: '李姐', gender: 'female', wealth_level: 3, gift_name: '玫瑰', unit_diamonds: 1, count: 3 }],
    ['大礼物', { type: 'gift', nickname: '钻石大哥', gender: 'male', wealth_level: 30, gift_name: '浪漫花束', unit_diamonds: 299, count: 1 }],
    ['关注', { type: 'follow', nickname: '新朋友', gender: 'female' }],
    ['点赞 ×10', { type: 'like', nickname: '点赞狂魔', count: 10 }],
    ['人数猛涨 +30', { type: 'room_stat', online_delta: 30 }],
    ['人数下滑 -10', { type: 'room_stat', online_delta: -10 }],
  ];
  const quickRow = h('div', { class: 'row' }, ...quick.map(([label, body]) => {
    const b = h('button', { type: 'button' }, label);
    b.onclick = () => sim(body, b);
    return b;
  }));

  const f = {
    type: h('select', {}, ...['chat', 'gift', 'member', 'follow', 'like', 'share', 'fansclub', 'room_stat', 'stream_end'].map((t) => new Option(TYPE_LABEL[t], t))),
    nickname: h('input', { type: 'text', placeholder: '昵称', value: '测试观众' }),
    gender: h('select', {}, new Option('女', 'female'), new Option('男', 'male'), new Option('未知', 'unknown')),
    wealth: h('input', { type: 'number', min: 0, max: 80, value: 0, style: { width: '6em' }, title: '财富等级' }),
    fans: h('input', { type: 'number', min: 0, max: 30, value: 0, style: { width: '6em' }, title: '粉丝团等级' }),
    content: h('input', { type: 'text', placeholder: '弹幕内容（留空 = 随机产品提问）' }),
    gift: h('input', { type: 'text', value: '玫瑰', style: { width: '8em' }, title: '礼物名' }),
    unit: h('input', { type: 'number', min: 0, value: 1, style: { width: '6em' }, title: '单价（钻）' }),
    count: h('input', { type: 'number', min: 1, value: 1, style: { width: '6em' }, title: '数量 / 点赞数' }),
    online: h('input', { type: 'number', min: 0, style: { width: '7em' }, placeholder: '在线人数' }),
  };
  const sendBtn = h('button', { type: 'button', class: 'primary' }, '注入');
  sendBtn.onclick = () => sim({
    type: f.type.value, nickname: f.nickname.value, gender: f.gender.value, wealth_level: +f.wealth.value || 0,
    fans_level: +f.fans.value || 0, content: f.content.value || null, gift_name: f.gift.value || '玫瑰',
    unit_diamonds: +f.unit.value || 0, count: +f.count.value || 1, online: f.online.value === '' ? null : +f.online.value,
  }, sendBtn);

  const rate = h('input', { type: 'number', min: 1, max: 1200, value: 30, style: { width: '6em' } });
  const mockStart = h('button', { type: 'button', class: 'primary' }, '开始模拟弹幕');
  const mockStop = h('button', { type: 'button' }, '停止');
  mockStart.onclick = () => busy(mockStart, async () => {
    await app.api.post('/api/danmaku/mock/start', { rate_per_min: +rate.value || 30 });
    refreshState();
  });
  mockStop.onclick = () => busy(mockStop, async () => {
    await app.api.post('/api/danmaku/mock/stop');
    refreshState();
  });

  const recSel = h('select', {});
  const speed = h('input', { type: 'number', min: 0.1, max: 50, step: 0.1, value: 1, style: { width: '6em' } });
  const loop = h('input', { type: 'checkbox' });
  const replayBtn = h('button', { type: 'button' }, '回放');
  replayBtn.onclick = () => busy(replayBtn, async () => {
    if (!recSel.value) throw new Error('还没有录制文件：先打开「录制事件」录一段');
    await app.api.post('/api/danmaku/replay', { file: recSel.value, speed: +speed.value || 1, loop: loop.checked });
    refreshState();
  });
  app.api.get('/api/danmaku/recordings').then((files) => {
    recSel.replaceChildren(...files.map((x) => new Option(`${x.name}（${Math.round(x.size / 1024)} KB · ${fmtDateTime(x.mtime)}）`, x.path)));
    if (!files.length) recSel.append(new Option('（还没有录制文件）', ''));
  }).catch(() => {});

  pane.append(
    h('div', { class: 'stack' },
      h('div', {}, h('div', { class: 'hint' }, '快捷注入（不经过弹幕子进程，直接进入互动引擎）'), quickRow),
      h('div', {}, h('div', { class: 'hint' }, '自定义事件'),
        h('div', { class: 'row' }, f.type, f.nickname, f.gender, h('label', { class: 'inline' }, '💎', f.wealth), h('label', { class: 'inline' }, '🎫', f.fans)),
        h('div', { class: 'row' }, f.content, h('label', { class: 'inline' }, '礼物', f.gift), h('label', { class: 'inline' }, '单价', f.unit),
          h('label', { class: 'inline' }, '数量', f.count), f.online, sendBtn)),
      h('div', { class: 'grid cols-2' },
        card('模拟弹幕源（子进程）', h('div', { class: 'row' }, h('label', { class: 'inline' }, '每分钟', rate, '条'), mockStart, mockStop),
          h('p', { class: 'hint' }, '比例、产品提问占比、性别比例、财富等级分布在「直播间设置 → 直播间与弹幕」里调。')),
        card('回放录制', h('div', { class: 'row' }, recSel), h('div', { class: 'row' }, h('label', { class: 'inline' }, '倍速', speed),
          h('label', { class: 'inline' }, loop, '循环'), replayBtn)))));
  return null;
}
