// 直播控制台：画面预览 + 正在说 / 下一句 / 队列 + 人工插话 + 当前产品（切换、跳段）+ 实时数据 + 弹幕流 + 会话事件。
import { busy, card, cardHead, fmtTime, h, logList } from '../ui.js';
import { describe, typeBadge, userTags } from '../live_events.js';
import { statusBadge } from './interaction_tabs.js';

export const title = '直播控制台';
let cleanup = [];

export async function mount(el, app) {
  const { player, stage } = app;
  const log = logList(150);
  const connectBtn = h('button', { class: 'primary', type: 'button' }, player.connected ? '断开' : '连接');
  const latency = h('input', { type: 'range', min: 0.1, max: 1.5, step: 0.05, value: player.latency });
  const latencyVal = h('span', { class: 'num' }, `${player.latency.toFixed(2)}s`);
  const volume = h('input', { type: 'range', min: 0, max: 1.5, step: 0.05, value: player.gain ? player.gain.gain.value : 1 });
  const clientStats = h('div', { class: 'stats' }, '—');
  const serverStats = h('div', { class: 'stats' }, '—');

  const playing = h('div', {}, '—');
  const playingMeta = h('div', { class: 'hint' });
  const nextLine = h('div', { class: 'hint' });
  const pauseIaBtn = h('button', { type: 'button', class: 'small' }, '暂停互动');
  const pauseScBtn = h('button', { type: 'button', class: 'small' }, '暂停讲品');
  const speakText = h('textarea', { rows: 3, placeholder: '输入要说的话，回车发送（Shift+回车换行）' });
  const voiceSel = h('select', { title: '音色' });
  const interruptFirst = h('input', { type: 'checkbox' });
  const speakBtn = h('button', { class: 'primary', type: 'button' }, '说');
  const interruptBtn = h('button', { type: 'button' }, '打断');
  const statsBox = h('div', { class: 'row num' });
  const productSel = h('select', { title: '当前讲解的产品' });
  const prevBtn = h('button', { type: 'button', class: 'small' }, '上一个');
  const nextBtn = h('button', { type: 'button', class: 'small' }, '下一个');
  const skipBtn = h('button', { type: 'button', class: 'small' }, '下一段');
  const scriptLine = h('div', { class: 'hint' });
  const feedBody = h('tbody');

  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '直播控制台'), h('span', { class: 'grow' }),
      h('a', { href: `/?view=1&session=${encodeURIComponent(app.sid)}`, target: '_blank' }, '纯画面（OBS）')),
    h('div', { class: 'grid main-side' },
      h('div', { class: 'stack' },
        card(null, stage.el,
          h('div', { class: 'row' }, connectBtn,
            h('label', { class: 'inline' }, '缓冲', latency, latencyVal),
            h('label', { class: 'inline' }, '音量', volume)),
          clientStats, serverStats)),
      h('div', { class: 'stack' },
        card(cardHead('现在', pauseIaBtn, pauseScBtn, interruptBtn), h('div', {}, h('span', { class: 'muted' }, '正在说：'), playing),
          playingMeta, nextLine),
        card('人工插话', speakText,
          h('div', { class: 'row' }, voiceSel, h('label', { class: 'inline' }, interruptFirst, '先打断'),
            h('span', { class: 'grow' }), speakBtn)),
        card(cardHead('当前产品', h('a', { href: '#/products' }, '产品与知识库 →')),
          h('div', { class: 'row' }, productSel, prevBtn, nextBtn, skipBtn), scriptLine),
        card(cardHead('实时数据', h('a', { href: '#/danmaku' }, '弹幕互动 →')), statsBox),
        card('会话事件', log.el))),
    card(cardHead('弹幕流（最近 50 条，完整版在「弹幕互动」页）'),
      h('div', { class: 'scroll-y', style: { maxHeight: '280px' } },
        h('table', { class: 'tbl' }, h('colgroup', {}, h('col', { style: { width: '72px' } }), h('col', { style: { width: '60px' } }),
          h('col', { style: { width: '36%' } }), h('col')), feedBody))));

  // ---- 播放器 ----
  const url = () => app.wsUrl(`/ws/sessions/${encodeURIComponent(app.sid)}`);
  connectBtn.onclick = async () => {
    if (player.connected) player.disconnect();
    else await player.connect(url());
  };
  const on = (target, type, fn) => {
    target.addEventListener(type, fn);
    cleanup.push(() => target.removeEventListener(type, fn));
  };
  on(player, 'open', () => {
    connectBtn.textContent = '断开';
    stage.overlay.hidden = !player.audioLocked;
    stage.overlay.textContent = '点击页面开始播放（浏览器要求用户操作后才能出声）';
    log.add(`已连接会话 ${app.sid}`);
  });
  on(player, 'close', (e) => {
    connectBtn.textContent = '连接';
    stage.overlay.hidden = false;
    stage.overlay.textContent = '已断开，点击「连接」重新接收';
    if (e.detail.reason !== 'client') log.add(`连接关闭 code=${e.detail.code}`, 'warn');
  });
  on(player, 'error', (e) => log.add(e.detail.message, 'err'));
  on(player, 'stats', (e) => {
    const s = e.detail;
    clientStats.textContent = `客户端 ${s.fps} fps · 缓冲 ${s.bufferMs} ms · 补帧 ${s.fillers} · 重同步 ${s.resyncs} · 丢包 ${s.dropped} · 序号跳变 ${s.gaps}`;
  });
  on(player, 'message', (e) => {
    const m = e.detail;
    const map = {
      hello: () => log.add(`hello: model=${m.session.model} fps=${m.fps} sr=${m.sample_rate}${m.can_write ? '' : '（只读）'}`),
      speak_queued: () => log.add(`排队: ${m.text}`),
      playback: () => m.state === 'start' && log.add(`开口: ${m.text || ''}`),
      speak_done: () => log.add(`说完 request=${m.request_id}`),
      interrupted: () => log.add(`已打断，丢弃 ${m.dropped_segments.length} 段`, 'warn'),
      model_switching: () => log.add(`切换模型 ${m.from} → ${m.to} …`),
      model: () => log.add(`模型已切换为 ${m.model}（${m.chunk.chunk_frames} 帧/块）`),
      avatar: () => log.add('形象已更换'),
      warning: () => log.add(m.message, 'warn'),
      error: () => log.add(`错误(${m.where || ''}): ${m.message}`, 'err'),
      closed: () => log.add('会话已关闭', 'warn'),
    };
    map[m.type]?.();
  });
  on(app.events, 'session', async () => {
    if (player.connected) await player.connect(url());
  });
  latency.oninput = () => {
    player.setLatency(parseFloat(latency.value));
    latencyVal.textContent = `${parseFloat(latency.value).toFixed(2)}s`;
  };
  volume.oninput = () => player.setVolume(parseFloat(volume.value));

  // ---- 说话 / 打断：主持人在管这一路会话时走主持人（进决策日志、过合规、优先级最高），否则直接让会话说 ----
  let hostSt = null;
  const viaHost = () => hostSt && hostSt.enabled && hostSt.session_ok && hostSt.session_id === app.sid;
  const speak = () => busy(speakBtn, async () => {
    const text = speakText.value.trim();
    if (!text) return;
    if (viaHost()) {
      await app.api.post('/api/host/say', { text, interrupt: interruptFirst.checked });
      log.add('已交给主持人（人工插话，优先级最高）');
    } else {
      const r = await app.api.post(`/api/sessions/${app.sid}/speak`, { text, voice: voiceSel.value || null, interrupt: interruptFirst.checked });
      log.add(`已提交 request=${r.request_id}`);
    }
    speakText.value = '';
  });
  speakBtn.onclick = speak;
  speakText.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      speak();
    }
  };
  interruptBtn.onclick = () => busy(interruptBtn, () => app.api.post(viaHost() ? '/api/host/interrupt' : `/api/sessions/${app.sid}/interrupt`));
  pauseIaBtn.onclick = () => busy(pauseIaBtn, async () => {
    await app.api.post(hostSt && hostSt.pause_interaction ? '/api/interaction/resume' : '/api/interaction/pause');
    refreshHost();
  });
  pauseScBtn.onclick = () => busy(pauseScBtn, async () => {
    await app.api.post(hostSt && hostSt.pause_script ? '/api/host/script/resume' : '/api/host/script/pause');
    refreshHost();
  });
  const SOURCE = { template: '模板', llm: 'LLM', knowledge: '知识库', manual: '人工', script: '讲解稿' };
  function renderHost(st) {
    hostSt = st;
    pauseIaBtn.textContent = st.pause_interaction ? '恢复互动' : '暂停互动';
    pauseIaBtn.classList.toggle('danger', st.pause_interaction);
    pauseScBtn.textContent = st.pause_script ? '恢复讲品' : '暂停讲品';
    pauseScBtn.classList.toggle('danger', st.pause_script);
    const cur = st.current;
    if (cur) {
      playing.textContent = cur.text || '（写词中…）';
      playingMeta.replaceChildren(h('code', {}, cur.event), ' ', cur.reason || '', cur.text_source ? ` · ${SOURCE[cur.text_source] || cur.text_source}` : '',
        cur.interruptible ? ' · 可被打断' : '');
    } else {
      const s = app.session();
      playing.textContent = (s && s.playing_text) || '（空闲）';
      playingMeta.textContent = st.enabled ? (st.session_ok ? '主持人待命' : `主持人等待会话「${st.session_id}」`) : '主持人已关闭（直播间设置 → 主播人设）';
    }
    const q = st.queue || [];
    const sc = st.script;
    const scriptNext = !q[0] && sc && st.script_active && !(cur && cur.kind === 'product')
      ? h('span', {}, ` · 下一句：讲品「${sc.product_name}」第 ${sc.segment}/${sc.segments} 段`) : '';
    nextLine.replaceChildren(h('span', {}, `队列 ${st.queue_size} 条`), q[0] ? h('span', {}, ' · 下一句：', h('code', {}, q[0].event), ` ${q[0].text || ''}`) : scriptNext);
    renderScript(st);
  }
  function renderScript(st) {
    const sc = st.script;
    if (!sc) {
      scriptLine.textContent = '';
      return;
    }
    const opts = [...productSel.options].map((o) => o.value).join();
    const want = (sc.rotation || []).map((p) => p.id);
    if (sc.product_id && !want.includes(sc.product_id)) want.unshift(sc.product_id);
    if (opts !== want.join() || productSel.options.length === 0) loadProducts();
    if (document.activeElement !== productSel && sc.product_id) productSel.value = sc.product_id;
    skipBtn.disabled = !st.script_active;
    if (sc.inactive_reason) scriptLine.textContent = sc.inactive_reason;
    else {
      scriptLine.textContent = `${st.pause_script ? '讲品已暂停 · ' : ''}${sc.speaking ? '正在讲' : '下一段'}第 ${sc.segment}/${sc.segments} 段 · 已讲 ${Math.round(sc.elapsed_s)}s / ${Math.round(sc.duration_s)}s 后换下一个产品`
        + (sc.next_text ? ` · ${sc.next_text}…` : '');
    }
  }
  let productsLoading = null;
  function loadProducts() {
    if (productsLoading) return productsLoading;
    productsLoading = app.api.get('/api/products').then((r) => {
      productSel.replaceChildren(...r.items.map((p) => new Option(`${p.name}${p.enabled ? '' : '（已停用）'}${p.playable ? '' : '（无确认稿）'}`, p.id)));
      if (!r.items.length) productSel.append(new Option('还没有产品', ''));
      if (r.active_id) productSel.value = r.active_id;
    }).catch(() => {}).finally(() => setTimeout(() => (productsLoading = null), 3000));
    return productsLoading;
  }
  productSel.onchange = () => productSel.value && busy(productSel, () => app.api.post(`/api/host/product/${productSel.value}/activate`).then(refreshHost));
  prevBtn.onclick = () => busy(prevBtn, () => app.api.post('/api/host/product/prev').then(refreshHost));
  nextBtn.onclick = () => busy(nextBtn, () => app.api.post('/api/host/product/next').then(refreshHost));
  skipBtn.onclick = () => busy(skipBtn, () => app.api.post('/api/host/script/next').then(refreshHost));
  on(app.events, 'topic:products.changed', () => {
    productsLoading = null;
    loadProducts();
  });
  loadProducts();
  async function refreshHost() {
    try {
      renderHost(await app.api.get('/api/host/status'));
    } catch { /* 服务端没有主持人 */ }
  }
  const hostTimer = setInterval(refreshHost, 1000);
  cleanup.push(() => clearInterval(hostTimer));
  on(app.events, 'topic:host.action', refreshHost);
  refreshHost();

  // ---- 实时数据 + 弹幕流 ----
  const feedRow = (e) => h('tr', {}, h('td', { class: 'num' }, fmtTime(e.ts)), h('td', {}, typeBadge(e.type)),
    h('td', {}, e.user ? userTags(e.user, { tags: e._tags }) : ''), h('td', {}, describe(e)));
  const shown = (e) => !['room_stat', 'rank', 'room_stats'].includes(e.type);
  function renderFeed() {
    const items = app.liveEvents.filter(shown).slice(-50).reverse();
    feedBody.replaceChildren(...items.map(feedRow));
    if (!items.length) feedBody.append(h('tr', {}, h('td', { colSpan: 4, class: 'muted' }, '还没有弹幕：到「弹幕互动」页连接直播间或开始模拟弹幕')));
  }
  function renderStats() {
    const st = app.liveStats;
    const first = st.onlineHist[0];
    const delta = first && st.online != null ? st.online - first[1] : 0;
    const item = (label, value, extra = '') => h('span', { class: 'inline' }, label, h('b', {}, value ?? '—'), extra);
    statsBox.replaceChildren(
      item('在线', st.online, delta ? h('span', { class: delta > 0 ? 'badge ok' : 'badge bad' }, `${delta > 0 ? '▲' : '▼'}${Math.abs(delta)}`) : ''),
      item('点赞', st.likes != null ? (st.likes >= 10000 ? `${(st.likes / 10000).toFixed(1)}万` : st.likes) : null),
      item('礼物', `${st.gifts} 笔 / ${st.giftDiamonds} 钻`));
  }
  on(app.events, 'live', (e) => {
    renderStats();
    if (!shown(e.detail)) return;
    if (feedBody.querySelector('td[colspan]')) feedBody.replaceChildren();
    feedBody.prepend(feedRow(e.detail));
    while (feedBody.children.length > 50) feedBody.lastChild.remove();
  });
  on(app.events, 'live-reset', () => {
    renderFeed();
    renderStats();
  });
  renderFeed();
  renderStats();

  // ---- 状态 ----
  on(app.events, 'system', () => render());
  function render() {
    const s = app.session();
    if (!s) {
      serverStats.textContent = '会话不可用';
      playing.textContent = '—';
      return;
    }
    if (!hostSt || !hostSt.current) playing.textContent = s.playing_text || '（空闲）';
    serverStats.textContent = `服务端 生成 ${s.gen_time_ema_s ?? '-'} s/块 · 已缓冲 ${s.buffered_s}s · 待播语音 ${s.speech_pending_s}s · TTS 队列 ${s.tts_queue} · 补帧 ${s.fillers} · 块 ${s.chunks}`;
  }
  render();
  if (app.autoconnect && !player.connected) {
    app.autoconnect = false;
    player.connect(url()).catch((e) => log.add(e.message, 'err'));
  }

  try {
    const v = await app.api.get('/api/tts/voices');
    voiceSel.replaceChildren(...v.voices.map((it) => new Option(it.name, it.id)));
    voiceSel.value = v.default;
  } catch (e) {
    log.add(`获取音色失败: ${e.message}`, 'warn');
  }
}

export function unmount() {
  for (const fn of cleanup) fn();
  cleanup = [];
}
