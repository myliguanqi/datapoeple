// Web 外壳：hash 路由、顶栏状态徽章、会话选择、管理口令、/ws/console 连接、共享的画面播放器。
// 页面模块（web/pages/*.js）导出 { title, mount(el, app), unmount?() }。接口见 docs/live/API.md。
import { LivePlayer } from './player.js';
import * as api from './api.js';
import { $, h, pill, promptDialog, toast } from './ui.js';

const params = new URLSearchParams(location.search);

// ---------------------------------------------------------------- 共享画面（控制台页挂载它；切页不断流）
function createStage() {
  const canvas = h('canvas', { width: 512, height: 512 });
  const badgeEl = h('div', { class: 'speak-badge' }, 'IDLE');
  const subtitle = h('div', { class: 'subtitle' });
  const overlay = h('div', { class: 'overlay' }, '点击「连接」开始接收实时画面');
  const el = h('div', { class: 'stage' }, canvas, badgeEl, subtitle, overlay);
  return { el, canvas, badge: badgeEl, subtitle, overlay };
}

const stage = createStage();
if (params.get('subs') === '0') stage.subtitle.hidden = true;
const player = new LivePlayer(stage.canvas, { latency: parseFloat(params.get('latency') || '0.3') });
player.addEventListener('seg', (e) => {
  stage.subtitle.textContent = e.detail.text || '';
  stage.badge.textContent = e.detail.seg ? 'SPEAKING' : 'IDLE';
  stage.badge.classList.toggle('speaking', !!e.detail.seg);
});
document.addEventListener('click', () => {
  if (player.ctx && player.audioLocked) player.ctx.resume().then(() => player.connected && (stage.overlay.hidden = true));
});

function wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}${path}`;
}

// ---------------------------------------------------------------- 纯画面模式 /?view=1&session=default（OBS 浏览器源）
if (params.get('view') === '1') {
  document.body.className = 'view-only';
  document.body.replaceChildren(stage.el);
  stage.el.querySelector('.speak-badge').hidden = true;
  const sid = params.get('session') || 'default';
  const url = wsUrl(`/ws/sessions/${encodeURIComponent(sid)}`);
  player.addEventListener('open', () => {
    stage.overlay.hidden = !player.audioLocked;
    stage.overlay.textContent = '点击页面开始播放（浏览器要求用户操作后才能出声）';
  });
  player.addEventListener('close', (e) => {
    stage.overlay.hidden = false;
    stage.overlay.textContent = '连接断开，正在重连…';
    if (e.detail.reason !== 'client') setTimeout(() => player.connected || player.connect(url), 2000);
  });
  player.connect(url).catch(() => {});
} else {
  startApp();
}

// ---------------------------------------------------------------- 完整控制台
function startApp() {
  const PAGES = {
    console: () => import('./pages/console.js'),
    danmaku: () => import('./pages/danmaku.js'),
    products: () => import('./pages/products.js'),
    media: () => import('./pages/media.js'),
    settings: () => import('./pages/settings.js'),
    system: () => import('./pages/system.js'),
    offline: () => import('./pages/offline.js'),
  };
  const events = new EventTarget();
  const app = {
    api,
    events,
    player,
    stage,
    wsUrl,
    sid: params.get('session') || 'default',
    autoconnect: params.get('autoconnect') === '1', // 完整界面也自动连接画面（控制台页首次打开时）
    system: null,
    setSession(sid) {
      if (!sid || sid === app.sid) return;
      app.sid = sid;
      $('#sessionSelect').value = sid;
      events.dispatchEvent(new CustomEvent('session', { detail: sid }));
    },
    session() {
      return (app.system?.sessions || []).find((s) => s.session_id === app.sid) || null;
    },
    refreshSystem,
  };
  window.__app = app; // 方便在浏览器控制台调试

  // ---- 主题 ----
  const THEME_KEY = 'flashhead.theme';
  const applyTheme = (t) => (t ? document.documentElement.setAttribute('data-theme', t) : document.documentElement.removeAttribute('data-theme'));
  try { applyTheme(localStorage.getItem(THEME_KEY)); } catch { /* 无 localStorage */ }
  $('#themeBtn').onclick = () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
    const next = dark ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
  };

  // ---- 口令 ----
  const askToken = async () => {
    const t = await promptDialog('管理口令', { label: '写操作需要 .env 里的 ADMIN_TOKEN（只存在本浏览器）', password: true, value: api.getToken() });
    return t === null ? null : t.trim();
  };
  api.onTokenRequired(askToken);
  $('#tokenBtn').onclick = async () => {
    const t = await askToken();
    if (t !== null) {
      api.setToken(t);
      toast(t ? '口令已保存' : '已清除口令', 'ok');
    }
  };

  // ---- 路由 ----
  const nav = $('#nav');
  const navSelect = $('#navSelect');
  for (const a of nav.querySelectorAll('a')) navSelect.append(new Option(a.textContent, a.dataset.page));
  navSelect.onchange = () => (location.hash = `#/${navSelect.value}`);
  let current = null;
  let seq = 0;
  async function route() {
    const name = (location.hash.match(/^#\/(\w+)/) || [])[1] || 'console';
    const key = PAGES[name] ? name : 'console';
    const my = ++seq;
    for (const a of nav.querySelectorAll('a')) a.classList.toggle('active', a.dataset.page === key);
    navSelect.value = key;
    const mod = await PAGES[key]();
    if (my !== seq) return;
    try { current?.unmount?.(); } catch (e) { console.error(e); }
    const el = $('#page');
    el.replaceChildren();
    current = mod;
    document.title = `${mod.title || key} · FlashHead 数字人直播`;
    try {
      await mod.mount(el, app);
    } catch (e) {
      console.error(e);
      el.append(h('div', { class: 'empty' }, `页面加载失败：${e.message}`));
    }
  }
  window.addEventListener('hashchange', route);

  // ---- 顶栏状态 ----
  $('#sessionSelect').onchange = (e) => app.setSession(e.target.value);
  async function refreshSystem() {
    try {
      const sys = await api.get('/api/system');
      app.system = sys;
      renderTop(sys);
      events.dispatchEvent(new CustomEvent('system', { detail: sys }));
    } catch (e) {
      pill($('#pillSession'), '服务未响应', 'bad');
    }
    return app.system;
  }
  function renderTop(sys) {
    const sel = $('#sessionSelect');
    const ids = sys.sessions.map((s) => s.session_id);
    if (!ids.includes(app.sid) && ids.length && !ids.includes('default')) app.setSession(ids[0]);
    const want = [...ids, ...(ids.includes(app.sid) ? [] : [app.sid])];
    if ([...sel.options].map((o) => o.value).join() !== want.join()) {
      sel.replaceChildren(...want.map((id) => new Option(ids.includes(id) ? id : `${id}（未就绪）`, id)));
    }
    sel.value = app.sid;
    const s = app.session();
    if (!s) pill($('#pillSession'), ids.length ? '会话不存在' : '无会话（模型加载中？）', 'bad');
    else pill($('#pillSession'), `会话 ${s.session_id} · ${s.model} · ${s.state}`, s.state === 'running' ? 'ok' : 'bad');
    if (s && s.rtf != null) pill($('#pillRtf'), `RTF ${s.rtf}`, `plain ${s.rtf < 0.85 ? 'ok' : s.rtf < 1 ? 'warn' : 'bad'}`);
    else pill($('#pillRtf'), 'RTF —', 'plain');
    const sv = sys.services || {};
    const tts = sv.tts;
    if (tts) {
      const fb = tts.requested && tts.engine !== tts.requested;
      pill($('#pillTts'), `TTS ${tts.engine}${fb ? '（回退）' : ''}`, fb || tts.warning ? 'warn' : 'ok');
      $('#pillTts').title = tts.error || tts.warning || (fb ? `首选 ${tts.requested} 不可用，已回退` : '语音合成正常');
    }
    const llm = sv.llm;
    if (llm) {
      pill($('#pillLlm'), `LLM ${llm.available ? (llm.provider === 'mock' ? 'mock' : '可用') : '不可用'}`, llm.available ? 'ok' : 'warn');
      $('#pillLlm').title = llm.reason || `${llm.provider} ${llm.model || ''}`;
    }
    else pill($('#pillLlm'), 'LLM 未接入', '');
    const dm = sv.danmaku;
    if (dm) {
      const label = { mock: '模拟', replay: '回放' }[dm.source] || (dm.logged_in ? '登录态' : '游客态');
      const map = { connected: ['ok', label], connecting: ['warn', '连接中'], starting: ['warn', '启动中'], stopped: ['', '未连接'],
        error: ['bad', '错误'], backoff: ['bad', '重连中'], disconnected: ['warn', '已断开'], finished: ['', '回放结束'] };
      const [k, t] = map[dm.state] || ['', dm.state];
      pill($('#pillDanmaku'), `弹幕 ${t}`, dm.state === 'connected' && !dm.logged_in && dm.source === 'douyin' ? 'warn' : k);
      $('#pillDanmaku').title = dm.error || `${dm.source || ''} ${dm.live_id || ''}`;
    } else pill($('#pillDanmaku'), '弹幕 未接入', '');
  }

  // ---- /ws/console：设置变更、弹幕事件、决策日志……按 topic 分发给页面 ----
  // app.liveEvents：最近的弹幕事件（节流后的，最多 500 条），各页面共用；新事件派发 'live'
  app.liveEvents = [];
  app.consoleRecent = {};
  app.cueResults = new Map();
  // 实时数据（控制台页显示）：在线人数与 1 分钟前比的变化、累计点赞、本页打开以来的礼物钻石数
  app.liveStats = { online: null, onlineHist: [], likes: null, giftDiamonds: 0, gifts: 0 };
  const trackStats = (d) => {
    const st = app.liveStats;
    if (d.type === 'room_stat' && d.data && d.data.online != null) {
      st.online = d.data.online;
      st.onlineHist.push([d.ts, d.online ?? d.data.online]);
      while (st.onlineHist.length && st.onlineHist[0][0] < d.ts - 60) st.onlineHist.shift();
    } else if (d.type === 'like_batch') st.likes = d.total ?? (st.likes || 0) + d.count;
    else if (d.type === 'like') st.likes = d.data.total ?? (st.likes || 0) + (d.data.count || 1);
    else if (d.type === 'gift' && d.data.is_final !== false) {
      st.giftDiamonds += d.data.total_diamonds || 0;
      st.gifts += 1;
    }
  };
  const pushLive = (data) => {
    trackStats(data);
    app.liveEvents.push(data);
    if (app.liveEvents.length > 500) app.liveEvents.splice(0, app.liveEvents.length - 500);
  };
  let consoleWs = null;
  function connectConsole() {
    consoleWs = new WebSocket(wsUrl('/ws/console'));
    consoleWs.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.topic === 'hello') {
        app.consoleRecent = msg.data.recent || {};
        app.liveEvents = [];
        for (const m of app.consoleRecent['live.event'] || []) pushLive(m.data);
        events.dispatchEvent(new CustomEvent('live-reset'));
      } else if (msg.topic === 'host.action' || msg.topic === 'interaction.cue') {
        const mid = msg.data && msg.data.source && msg.data.source.msg_id;      // 弹幕 → 决策结果（事件流「处理」列）
        if (mid) {
          app.cueResults.set(mid, msg.data);
          if (app.cueResults.size > 1000) app.cueResults.delete(app.cueResults.keys().next().value);
        }
      } else if (msg.topic === 'live.event') {
        pushLive(msg.data);
        events.dispatchEvent(new CustomEvent('live', { detail: msg.data }));
      }
      events.dispatchEvent(new CustomEvent('console', { detail: msg }));
      events.dispatchEvent(new CustomEvent(`topic:${msg.topic}`, { detail: msg }));
      if (msg.topic === 'settings.changed') refreshSystem();
    };
    consoleWs.onclose = () => setTimeout(connectConsole, 3000);
  }
  connectConsole();

  refreshSystem().then(route);
  setInterval(refreshSystem, 2000);
}
