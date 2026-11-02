// 网页客户端：连接直播会话、发文本/音频、切模型、换形象、离线生成。接口见 docs/live/API.md。
import { LivePlayer } from './player.js';

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(location.search);
// 纯画面模式 /?view=1&session=default ：只显示画面并自动连接，可直接作为 OBS「浏览器源」
const viewOnly = params.get('view') === '1';
if (viewOnly) document.body.classList.add('view-only');
if (params.get('subs') === '0') $('#subtitle').hidden = true;
if (params.get('latency')) $('#latency').value = params.get('latency');
const player = new LivePlayer($('#screen'), { latency: parseFloat($('#latency').value) });
let sid = params.get('session') || 'default';
let sessionStatus = null;

// ---------------------------------------------------------------- 工具
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  const body = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.detail) || body || `HTTP ${r.status}`);
  return body;
}
const postJSON = (path, data) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

function log(text, cls = '') {
  const li = document.createElement('li');
  li.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
  if (cls) li.className = cls;
  const ol = $('#log');
  ol.prepend(li);
  while (ol.children.length > 120) ol.lastChild.remove();
}

function pill(el, text, cls = '') {
  el.textContent = text;
  el.className = `pill ${cls}`;
}

async function guard(btn, fn) {
  btn.disabled = true;
  try {
    await fn();
  } catch (e) {
    log(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- 连接与播放
function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws/sessions/${encodeURIComponent(sid)}`;
}

$('#connectBtn').onclick = async () => {
  if (player.connected) {
    player.disconnect();
    return;
  }
  await player.connect(wsUrl());
};

player.addEventListener('open', () => {
  pill($('#pillConn'), `已连接 ${sid}`, 'ok');
  $('#connectBtn').textContent = '断开';
  $('#overlay').hidden = !player.audioLocked;
  $('#overlay').textContent = '点击页面开始播放（浏览器要求用户操作后才能出声）';
  log(`已连接会话 ${sid}`);
});
document.addEventListener('click', () => {
  if (player.ctx && player.audioLocked) {
    player.ctx.resume().then(() => {
      if (player.connected) $('#overlay').hidden = true;
    });
  }
});
player.addEventListener('close', (e) => {
  pill($('#pillConn'), '未连接', 'bad');
  $('#connectBtn').textContent = '连接';
  $('#overlay').hidden = false;
  $('#overlay').textContent = '已断开，点击「连接」重新接收';
  if (e.detail.reason !== 'client') log(`连接关闭 code=${e.detail.code}`, 'warn');
});
player.addEventListener('error', (e) => log(e.detail.message, 'err'));
player.addEventListener('seg', (e) => {
  $('#subtitle').textContent = e.detail.text || '';
  const b = $('#speakBadge');
  b.textContent = e.detail.seg ? 'SPEAKING' : 'IDLE';
  b.classList.toggle('speaking', !!e.detail.seg);
});
player.addEventListener('stats', (e) => {
  const s = e.detail;
  $('#clientStats').textContent =
    `客户端  ${s.fps} fps · 缓冲 ${s.bufferMs} ms · 补帧 ${s.fillers} · 重同步 ${s.resyncs} · 丢包 ${s.dropped} · 序号跳变 ${s.gaps}`;
});
player.addEventListener('message', (e) => {
  const m = e.detail;
  switch (m.type) {
    case 'hello':
      log(`hello: model=${m.session.model} fps=${m.fps} sr=${m.sample_rate}`);
      break;
    case 'speak_queued':
      log(`排队: ${m.text}`);
      break;
    case 'playback':
      if (m.state === 'start') log(`开口: ${m.text || ''}`);
      break;
    case 'speak_done':
      log(`说完 request=${m.request_id}`);
      break;
    case 'interrupted':
      log(`已打断，丢弃 ${m.dropped_segments.length} 段`, 'warn');
      break;
    case 'model_switching':
      log(`切换模型 ${m.from} → ${m.to} ...`);
      break;
    case 'model':
      log(`模型已切换为 ${m.model}（${m.chunk.chunk_frames} 帧/块）`);
      break;
    case 'avatar':
      log('形象已更换');
      break;
    case 'warning':
      log(m.message, 'warn');
      break;
    case 'error':
      log(`错误(${m.where || ''}): ${m.message}`, 'err');
      break;
    case 'closed':
      log('会话已关闭', 'warn');
      break;
  }
});

$('#latency').oninput = (e) => {
  const v = parseFloat(e.target.value);
  player.setLatency(v);
  $('#latencyVal').textContent = `${v.toFixed(2)}s`;
};
$('#volume').oninput = (e) => player.setVolume(parseFloat(e.target.value));
$('#sessionSelect').onchange = async (e) => {
  sid = e.target.value;
  if (player.connected) await player.connect(wsUrl());
  refreshSession();
};

// ---------------------------------------------------------------- 说话 / 打断 / 推音频
async function speak() {
  const text = $('#speakText').value.trim();
  if (!text) return;
  const body = { text, voice: $('#voiceSelect').value || null, interrupt: $('#interruptFirst').checked };
  const r = await postJSON(`/api/sessions/${sid}/speak`, body);
  log(`已提交 request=${r.request_id}`);
}
$('#speakBtn').onclick = () => guard($('#speakBtn'), speak);
$('#speakText').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    guard($('#speakBtn'), speak);
  }
};
$('#interruptBtn').onclick = () => guard($('#interruptBtn'), () => api(`/api/sessions/${sid}/interrupt`, { method: 'POST' }));

$('#audioBtn').onclick = () =>
  guard($('#audioBtn'), async () => {
    const f = $('#audioFile').files[0];
    if (!f) throw new Error('请选择音频文件');
    const fd = new FormData();
    fd.append('file', f);
    fd.append('text', f.name);
    fd.append('interrupt', $('#interruptFirst').checked);
    const r = await api(`/api/sessions/${sid}/audio`, { method: 'POST', body: fd });
    log(`已推送音频 ${r.seconds}s`);
  });

// ---------------------------------------------------------------- 模型 / 形象
document.querySelectorAll('#modelSeg button').forEach((btn) => {
  btn.onclick = () =>
    guard(btn, async () => {
      $('#modelHint').textContent = '切换中...';
      await postJSON(`/api/sessions/${sid}/model`, { model: btn.dataset.model });
      await refreshSession();
    });
});

$('#avatarBtn').onclick = () =>
  guard($('#avatarBtn'), async () => {
    const f = $('#avatarFile').files[0];
    if (!f) throw new Error('请选择图片');
    const fd = new FormData();
    fd.append('file', f);
    fd.append('use_face_crop', $('#avatarCrop').checked);
    await api(`/api/sessions/${sid}/avatar`, { method: 'POST', body: fd });
  });

// ---------------------------------------------------------------- 离线生成
$('#offBtn').onclick = () =>
  guard($('#offBtn'), async () => {
    const fd = new FormData();
    const img = $('#offImage').files[0];
    const aud = $('#offAudio').files[0];
    if (img) fd.append('image', img);
    if (aud) fd.append('audio', aud);
    fd.append('text', $('#offText').value);
    fd.append('model', $('#offModel').value);
    fd.append('voice', $('#offVoice').value);
    fd.append('use_face_crop', $('#offCrop').checked);
    const job = await api('/api/offline/jobs', { method: 'POST', body: fd });
    log(`离线任务 ${job.job_id} 已提交`);
    pollJob(job.job_id);
  });

async function pollJob(id) {
  $('#offVideo').hidden = true;
  for (;;) {
    const j = await api(`/api/offline/jobs/${id}`);
    $('#offBar').style.width = `${Math.round(j.progress * 100)}%`;
    $('#offMsg').textContent = `${j.status} ${j.message || ''}`;
    if (j.status === 'done') {
      $('#offVideo').src = `${j.video_url}?t=${Date.now()}`;
      $('#offVideo').hidden = false;
      log(`离线任务 ${id} 完成`);
      refreshJobs();
      return;
    }
    if (j.status === 'failed') {
      log(`离线任务 ${id} 失败: ${j.message}`, 'err');
      refreshJobs();
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function refreshJobs() {
  const jobs = await api('/api/offline/jobs');
  const ul = $('#jobList');
  ul.innerHTML = '';
  for (const j of jobs.slice(0, 8)) {
    const li = document.createElement('li');
    li.append(`${j.job_id} · ${j.model} · ${j.status} `);
    if (j.video_url) {
      const a = document.createElement('a');
      a.href = j.video_url;
      a.textContent = '下载';
      li.append(a);
    }
    ul.append(li);
  }
}

// ---------------------------------------------------------------- 会话 / 系统
$('#newSessionBtn').onclick = () =>
  guard($('#newSessionBtn'), async () => {
    const gpu = $('#newGpu').value;
    const s = await postJSON('/api/sessions', { model: $('#newModel').value, gpu_id: gpu === '' ? null : parseInt(gpu, 10) });
    log(`新会话 ${s.session_id} @ GPU${s.gpu_id}`);
    sid = s.session_id;
    await refreshSystem();
    if (player.connected) await player.connect(wsUrl());
  });

$('#closeSessionBtn').onclick = () =>
  guard($('#closeSessionBtn'), async () => {
    if (!confirm(`关闭会话 ${sid}？`)) return;
    await api(`/api/sessions/${sid}`, { method: 'DELETE' });
    player.disconnect();
    await refreshSystem();
  });

async function refreshSession() {
  try {
    const s = await api(`/api/sessions/${sid}`);
    sessionStatus = s;
    pill($('#pillState'), s.state, s.state === 'running' ? 'ok' : 'bad');
    pill($('#pillModel'), `${s.model} · GPU${s.gpu_id}`);
    if (s.rtf != null) pill($('#pillRtf'), `RTF ${s.rtf}`, s.rtf < 0.85 ? 'ok' : s.rtf < 1 ? 'warn' : 'bad');
    document.querySelectorAll('#modelSeg button').forEach((b) => b.classList.toggle('active', b.dataset.model === s.model));
    $('#modelHint').textContent = s.chunk ? `${s.chunk.chunk_frames} 帧/块 · ${s.chunk.chunk_seconds.toFixed(2)}s` : '';
    $('#serverStats').textContent =
      `服务端  生成 ${s.gen_time_ema_s ?? '-'} s/块 · 已缓冲 ${s.buffered_s}s · 待播语音 ${s.speech_pending_s}s · ` +
      `TTS 队列 ${s.tts_queue} · 补帧 ${s.fillers} · 块 ${s.chunks}`;
  } catch (e) {
    sessionStatus = null;
    pill($('#pillState'), '无会话', 'bad');
  }
}

async function refreshSystem() {
  try {
    const sys = await api('/api/system');
    const sel = $('#sessionSelect');
    const ids = sys.sessions.map((s) => s.session_id);
    if (!ids.includes(sid) && ids.length) sid = ids[0];
    sel.innerHTML = '';
    for (const s of sys.sessions) {
      const o = new Option(`${s.session_id} (${s.model}, GPU${s.gpu_id})`, s.session_id);
      sel.append(o);
    }
    if (!ids.length) sel.append(new Option('（暂无会话，模型加载中？）', ''));
    sel.value = sid;
    const rows = $('#workerRows');
    rows.innerHTML = '';
    for (const w of sys.workers) {
      const tr = document.createElement('tr');
      const models = Object.keys((w.info && w.info.models) || {}).join(', ') || '-';
      const cells = [
        `#${w.index}`,
        (w.info && w.info.gpu_name) || `gpu ${w.gpu_id}`,
        w.state + (w.error ? ` (${String(w.error).split('\n')[0].slice(0, 80)})` : ''),
        models,
        `${w.live_sessions.length} / ${w.jobs.length}`,
      ];
      for (const c of cells) {
        const td = document.createElement('td');
        td.textContent = c;
        tr.append(td);
      }
      rows.append(tr);
    }
  } catch (e) {
    log(`获取系统状态失败: ${e.message}`, 'err');
  }
}

async function loadVoices() {
  try {
    const v = await api('/api/tts/voices');
    for (const id of ['#voiceSelect', '#offVoice']) {
      const sel = $(id);
      sel.innerHTML = '';
      for (const it of v.voices) sel.append(new Option(it.name, it.id));
      sel.value = v.default;
    }
  } catch (e) {
    log(`获取音色失败: ${e.message}`, 'warn');
  }
}

if (viewOnly || params.get('autoconnect') === '1') {
  player.connect(wsUrl()).catch((e) => log(e.message, 'err'));
  // 纯画面模式断线自动重连（主动断开 reason=client 不重连）
  player.addEventListener('close', (e) => {
    if (viewOnly && e.detail.reason !== 'client') setTimeout(() => player.connected || player.connect(wsUrl()), 2000);
  });
}
if (!viewOnly) {
  refreshSystem();
  loadVoices();
  refreshJobs();
  setInterval(refreshSession, 1000);
  setInterval(refreshSystem, 5000);
}
