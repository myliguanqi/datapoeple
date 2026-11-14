// 直播场景页 /scene：背景 → 视频层 → 数字人画面（复用 player.js）→ 产品卡片 → 字幕 → AI 标识（常驻，不能关）。
// 场景状态来自服务端：连上画面 WebSocket 后先 GET /api/sessions/{sid}/scene，之后收 {"type":"scene"} 事件切布局、播视频；
// 视频播完回传 {"type":"scene.video_ended", play_id}（服务端也会按时长兜底结束）。断线 2 秒后自动重连。
// URL 参数：session=default、w=&h=（覆盖画布尺寸）、debug=1（显示布局框和状态）、latency=0.3、
//          preview=1（控制台里嵌的预览：静音，不和控制台播放器重复出声）
import { LivePlayer } from './player.js';

const params = new URLSearchParams(location.search);
const sid = params.get('session') || 'default';
const debug = params.get('debug') === '1';
const preview = params.get('preview') === '1';
const $ = (id) => document.getElementById(id);
const stage = $('stage');
const video = $('video');
const dh = $('dh');
const card = $('card');
const subtitle = $('subtitle');
const disclosure = $('disclosure');
const overlay = $('overlay');
const dbg = $('debug');
if (debug) {
  document.body.classList.add('debug');
  dbg.hidden = false;
}

const player = new LivePlayer($('dhCanvas'), { latency: parseFloat(params.get('latency') || '0.3') });
let scene = null;
let playId = null;
let subtitleOn = true;
let W = 720;
let H = 1280;

// ---------------------------------------------------------------- 布局
function place(el, r) {
  Object.assign(el.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
}

function fit() {
  const s = Math.min(innerWidth / W, innerHeight / H);
  stage.style.width = `${W}px`;
  stage.style.height = `${H}px`;
  stage.style.transform = `scale(${s})`;
}
addEventListener('resize', fit);

function apply(sc) {
  if (!sc) return;
  scene = sc;
  const c = sc.canvas;
  W = parseInt(params.get('w'), 10) || c.width;
  H = parseInt(params.get('h'), 10) || c.height;
  const kx = W / c.width;
  const ky = H / c.height;
  const scale = (r) => ({ x: r.x * kx, y: r.y * ky, w: r.w * kx, h: r.h * ky });
  fit();
  const bg = c.background;
  stage.style.backgroundImage = bg && bg.image ? `url("${bg.image}")` : '';
  stage.style.backgroundColor = bg && bg.color ? bg.color : '';
  if (bg && bg.color) stage.style.backgroundImage = 'none';
  if (!bg) stage.style.removeProperty('background-image');

  const full = { x: 0, y: 0, w: W, h: H };
  const lay = sc.layout;
  dh.classList.toggle('pip', lay === 'video_pip');
  dh.hidden = lay === 'video_full';
  if (lay === 'video_pip') place(dh, scale(c.pip_rect));
  else if (lay === 'split') place(dh, { x: 0, y: H / 2, w: W, h: H / 2 });
  else place(dh, scale(c.dh_rect));
  place(video, lay === 'split' ? { x: 0, y: 0, w: W, h: H / 2 } : full);

  const p = sc.card.product;
  const showCard = sc.card.enabled && p && lay !== 'video_full';
  card.hidden = !showCard;
  let cardRect = scale(c.card_rect);
  if (lay === 'split') {                    // 分屏：卡片放到视频那一半的底部（紧凑样式），别挡住下半屏数字人的脸
    const ch = Math.min(cardRect.h, 170);
    cardRect = { x: cardRect.x, y: H / 2 - ch - 16, w: cardRect.w, h: ch };
  }
  if (showCard) {
    place(card, cardRect);
    card.style.height = 'auto';
    card.style.maxHeight = `${cardRect.h}px`;
    card.classList.toggle('compact', sc.card.style === 'compact' || lay === 'split');
    const mode = sc.card.mode || 'product';              // product = 完整条目；title = 只显示名称；off 由服务端关掉卡片
    const price = card.querySelector('.price');
    const list = card.querySelector('ul');
    card.querySelector('.name').textContent = p.name;
    price.textContent = p.price_text || '';
    price.hidden = mode !== 'product' || !p.price_text;
    price.style.display = price.hidden ? 'none' : '';
    list.replaceChildren(...(p.selling_points || []).map((t) => Object.assign(document.createElement('li'), { textContent: t })));
    list.hidden = mode !== 'product' || !(p.selling_points || []).length;
    list.style.display = list.hidden ? 'none' : '';
  }
  subtitleOn = sc.subtitle.enabled;
  subtitle.hidden = !subtitleOn;
  const subBottom = showCard && lay !== 'split' ? cardRect.y - 24 : H - 90;
  Object.assign(subtitle.style, { top: 'auto', bottom: `${H - subBottom}px` });
  disclosure.textContent = sc.disclosure || '本直播间为 AI 数字人直播';     // 常驻显示，不能关闭
  applyVideo(sc);
  renderDebug();
}

function applyVideo(sc) {
  const v = sc.video;
  if (!v) {
    if (playId) {
      video.pause();
      video.removeAttribute('src');
      video.load();
      playId = null;
    }
    video.style.display = 'none';
    player.setVolume(preview ? 0 : 1);
    return;
  }
  video.style.display = 'block';
  video.muted = preview || v.audio === 'mute';
  video.loop = !!v.loop;
  player.setVolume(preview ? 0 : (v.audio === 'video' && v.has_audio ? sc.ducking : 1));
  if (v.play_id === playId) return;
  playId = v.play_id;
  const offset = Math.max(0, (sc.ts || 0) - (v.started_at || 0));       // 页面晚打开 / 重连：从服务端已经播到的位置接上
  video.src = v.url;
  video.onloadedmetadata = () => {
    if (offset > 0.5 && (!video.duration || offset < video.duration - 0.5)) video.currentTime = offset;
  };
  video.play().catch(() => {                                             // 没有用户手势时带声音的自动播放会被拦：先静音播
    video.muted = true;
    video.play().catch(() => {});
    if (!preview) showOverlay('点击页面恢复视频声音');
  });
}

video.addEventListener('ended', () => {
  if (scene && scene.video && !scene.video.loop && playId === scene.video.play_id) player.send({ type: 'scene.video_ended', play_id: playId });
});

function renderDebug() {
  if (!debug || !scene) return;
  const v = scene.video;
  dbg.textContent = `session ${sid}  v${scene.version}  ${scene.reason}\nlayout ${scene.layout}  ${W}×${H}\n`
    + (v ? `video ${v.name} ${v.mode}/${v.audio} play_id=${v.play_id}` : 'video —')
    + `\nws ${player.connected ? 'open' : 'closed'}  ${player.stats.fps} fps  ${player.stats.bufferMs} ms`;
}
setInterval(renderDebug, 1000);

// ---------------------------------------------------------------- 连接
function showOverlay(text) {
  overlay.textContent = text;
  overlay.hidden = false;
}
overlay.addEventListener('click', () => {
  if (player.ctx) player.ctx.resume();
  if (scene && scene.video && !preview && scene.video.audio === 'video') video.muted = false;
  if (player.connected) overlay.hidden = true;
});

async function loadScene() {
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/scene`);
    if (r.ok) apply(await r.json());
  } catch { /* 下次重连再取 */ }
}

const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/sessions/${encodeURIComponent(sid)}`;
player.addEventListener('open', () => {
  if (preview && player.gain) player.setVolume(0);
  // 画面按音频时钟出帧：AudioContext 没被允许启动时画面也不动，预览同样要点一下
  if (player.audioLocked) showOverlay(preview ? '点击开始预览' : '点击页面开始播放（浏览器要求用户操作后才能出声；OBS 浏览器源不需要）');
  else overlay.hidden = true;
  loadScene();
});
player.addEventListener('close', (e) => {
  showOverlay('连接断开，正在重连…');
  if (e.detail.reason !== 'client') setTimeout(() => player.connected || player.connect(wsUrl).catch(() => {}), 2000);
});
player.addEventListener('message', (e) => {
  const m = e.detail;
  if (m.type === 'scene') apply(m.scene);
  else if (m.type === 'closed') showOverlay('会话已关闭');
});
player.addEventListener('seg', (e) => {
  subtitle.textContent = subtitleOn ? (e.detail.text || '') : '';
});

loadScene();
player.connect(wsUrl).catch(() => setTimeout(() => player.connect(wsUrl).catch(() => {}), 2000));
window.__scene = { player, get scene() { return scene; }, apply };      // 调试 / 截图验证用
