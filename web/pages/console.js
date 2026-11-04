// 直播控制台：画面预览 + 正在说 + 人工插话 + 会话事件。后续阶段在这里加队列、当前产品、实时数据、弹幕流。
import { busy, card, cardHead, fmtTime, h, logList } from '../ui.js';
import { describe, typeBadge, userTags } from '../live_events.js';

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

  const playing = h('div', { class: 'muted' }, '—');
  const speakText = h('textarea', { rows: 3, placeholder: '输入要说的话，回车发送（Shift+回车换行）' });
  const voiceSel = h('select', { title: '音色' });
  const interruptFirst = h('input', { type: 'checkbox' });
  const speakBtn = h('button', { class: 'primary', type: 'button' }, '说');
  const interruptBtn = h('button', { type: 'button' }, '打断');
  const statsBox = h('div', { class: 'row num' });
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
        card(cardHead('现在', interruptBtn), h('div', {}, h('span', { class: 'muted' }, '正在说：'), playing)),
        card('人工插话', speakText,
          h('div', { class: 'row' }, voiceSel, h('label', { class: 'inline' }, interruptFirst, '先打断'),
            h('span', { class: 'grow' }), speakBtn)),
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

  // ---- 说话 / 打断 ----
  const speak = () => busy(speakBtn, async () => {
    const text = speakText.value.trim();
    if (!text) return;
    const r = await app.api.post(`/api/sessions/${app.sid}/speak`, { text, voice: voiceSel.value || null, interrupt: interruptFirst.checked });
    log.add(`已提交 request=${r.request_id}`);
    speakText.value = '';
  });
  speakBtn.onclick = speak;
  speakText.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      speak();
    }
  };
  interruptBtn.onclick = () => busy(interruptBtn, () => app.api.post(`/api/sessions/${app.sid}/interrupt`));

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
    playing.textContent = s.playing_text || '（空闲）';
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
