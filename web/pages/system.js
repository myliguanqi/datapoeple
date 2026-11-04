// 数字人与系统：会话管理、Lite/Pro 切换、换形象、推送音频、worker 表、各服务状态。
import { badge, busy, card, cardHead, confirmDialog, h, table, toast } from '../ui.js';

export const title = '数字人与系统';
let cleanup = [];

export async function mount(el, app) {
  const sessBox = h('div');
  const workerBox = h('div');
  const svcBox = h('div', { class: 'grid cols-3' });
  const modelSeg = h('div', { class: 'seg' },
    ...['lite', 'pro'].map((m) => h('button', { type: 'button', dataset: { model: m } }, m === 'lite' ? 'Lite' : 'Pro')));
  const modelHint = h('span', { class: 'hint' });
  const avatarFile = h('input', { type: 'file', accept: 'image/*' });
  const avatarCrop = h('input', { type: 'checkbox' });
  const avatarBtn = h('button', { type: 'button' }, '更换形象');
  const audioFile = h('input', { type: 'file', accept: 'audio/*' });
  const audioInterrupt = h('input', { type: 'checkbox' });
  const audioBtn = h('button', { type: 'button' }, '推送');
  const newModel = h('select', {}, new Option('Lite', 'lite'), new Option('Pro', 'pro'));
  const newGpu = h('input', { type: 'number', min: 0, placeholder: 'GPU(可空)', style: { width: '7em' } });
  const newBtn = h('button', { type: 'button' }, '新建会话');
  const closeBtn = h('button', { type: 'button', class: 'danger' }, '关闭当前会话');
  const refreshBtn = h('button', { type: 'button', class: 'small' }, '刷新显存');
  const reloadEnvBtn = h('button', { type: 'button', class: 'small' }, '重新加载 .env');

  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '数字人与系统'), h('span', { class: 'grow' }),
      h('span', { class: 'hint' }, '改了 .env 里的密钥/地址：'), reloadEnvBtn),
    h('div', { class: 'stack' },
      svcBox,
      h('div', { class: 'grid cols-2' },
        card(`模型与形象（当前会话）`,
          h('div', { class: 'row' }, modelSeg, modelHint),
          h('div', { class: 'row' }, avatarFile, h('label', { class: 'inline' }, avatarCrop, '人脸裁剪'), avatarBtn),
          h('p', { class: 'hint' }, '参考图：正脸、嘴自然闭合、光线均匀的半身/头肩照效果最好。')),
        card('推送音频',
          h('p', { class: 'hint' }, '外部 TTS / 录音直接驱动口型（wav、mp3 等，服务端用 ffmpeg 解码）。'),
          h('div', { class: 'row' }, audioFile, h('label', { class: 'inline' }, audioInterrupt, '先打断'), audioBtn))),
      card(cardHead('直播会话', newModel, newGpu, newBtn, closeBtn), sessBox),
      card(cardHead('显卡 worker', refreshBtn), workerBox)));

  for (const b of modelSeg.querySelectorAll('button')) {
    b.onclick = () => busy(b, async () => {
      modelHint.textContent = '切换中…';
      await app.api.post(`/api/sessions/${app.sid}/model`, { model: b.dataset.model });
      await app.refreshSystem();
      toast(`已切换到 ${b.dataset.model}`, 'ok');
    });
  }
  avatarBtn.onclick = () => busy(avatarBtn, async () => {
    const f = avatarFile.files[0];
    if (!f) throw new Error('请选择图片');
    const fd = new FormData();
    fd.append('file', f);
    fd.append('use_face_crop', avatarCrop.checked);
    await app.api.upload(`/api/sessions/${app.sid}/avatar`, fd);
    toast('形象已更换', 'ok');
  });
  audioBtn.onclick = () => busy(audioBtn, async () => {
    const f = audioFile.files[0];
    if (!f) throw new Error('请选择音频文件');
    const fd = new FormData();
    fd.append('file', f);
    fd.append('text', f.name);
    fd.append('interrupt', audioInterrupt.checked);
    const r = await app.api.upload(`/api/sessions/${app.sid}/audio`, fd);
    toast(`已推送音频 ${r.seconds}s`, 'ok');
  });
  newBtn.onclick = () => busy(newBtn, async () => {
    const gpu = newGpu.value;
    const s = await app.api.post('/api/sessions', { model: newModel.value, gpu_id: gpu === '' ? null : parseInt(gpu, 10) });
    toast(`新会话 ${s.session_id} @ GPU${s.gpu_id}`, 'ok');
    await app.refreshSystem();
    app.setSession(s.session_id);
  });
  closeBtn.onclick = async () => {
    if (!(await confirmDialog(`关闭会话「${app.sid}」？正在看这一路画面的客户端会断开。`, { okText: '关闭会话', danger: true }))) return;
    await busy(closeBtn, async () => {
      await app.api.del(`/api/sessions/${app.sid}`);
      app.player.disconnect();
      await app.refreshSystem();
    });
  };
  reloadEnvBtn.onclick = () => busy(reloadEnvBtn, async () => {
    const r = await app.api.post('/api/settings/reload-env');
    toast(r.changed.length ? `已重新加载，变化：${r.changed.join('、')}` : '已重新加载，没有变化', 'ok');
    setTimeout(app.refreshSystem, 300);
  });
  refreshBtn.onclick = () => busy(refreshBtn, async () => {
    const sys = await app.api.get('/api/system?refresh=true');
    renderWorkers(sys);
  });

  function svcCard(name, s, rows) {
    if (!s) return card(name, h('p', { class: 'muted' }, '未接入'));
    return card(name, h('dl', { class: 'kv' }, rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v ?? '—')])));
  }
  function renderServices(sys) {
    const sv = sys.services || {};
    const t = sv.tts;
    const l = sv.llm;
    const d = sv.danmaku;
    svcBox.replaceChildren(
      svcCard('语音合成（TTS）', t, t && [
        ['引擎', h('span', {}, t.engine, ' ', t.engine !== t.requested ? badge(`首选 ${t.requested} 不可用`, 'warn') : badge('正常', 'ok'))],
        ['回退顺序', (t.chain || []).join(' → ')],
        ['调用 / 失败 / 回退', `${t.calls} / ${t.failures} / ${t.fallbacks ?? 0}`],
        ['最近耗时', t.last_ms != null ? `${t.last_ms} ms（${t.last_engine || ''}）` : '—'],
        ['缓存', t.cache ? `${t.cache.enabled ? '开' : '关'} · 命中 ${t.cache.hits} / 未命中 ${t.cache.miss}` : '—'],
        ['今日计费字数', Object.entries(t.today_chars || {}).map(([k, v]) => `${k} ${v}`).join('，') + (t.daily_max_chars ? `（上限 ${t.daily_max_chars}）` : '') || '—'],
        ['提示', t.warning || t.error || t.last_error || '—'],
      ]),
      svcCard('大模型（LLM）', l, l && [
        ['状态', l.available ? badge('可用', 'ok') : badge(l.reason || '不可用', 'warn')],
        ['接口 / 模型', `${l.provider} / ${l.model || '—'}`],
        ['调用 / 失败', `${l.calls} / ${l.failures}`],
        ['最近耗时', l.last_ms != null ? `${l.last_ms} ms${l.last_ttft_ms != null ? `（首字 ${l.last_ttft_ms} ms）` : ''}` : '—'],
        ['今日调用', `${l.today_calls ?? 0}${l.daily_max_calls ? ` / 上限 ${l.daily_max_calls}` : ''}`],
        ['最近错误', l.last_error || '—'],
      ]),
      svcCard('弹幕子进程', d, d && [
        ['状态', `${d.state}${d.logged_in ? '（登录态）' : d.source === 'douyin' && d.state === 'connected' ? '（游客态）' : ''}`],
        ['来源 / 房间', `${d.source || '—'} / ${d.live_id || '—'}`],
        ['事件数', d.events ?? '—'],
        ['重启次数', d.restarts ?? '—'],
        ['错误', d.error || '—'],
      ]));
  }
  function renderSessions(sys) {
    sessBox.replaceChildren(table([
      { label: '会话', render: (s) => h('span', {}, s.session_id, s.session_id === app.sid ? ' ' : '', s.session_id === app.sid ? badge('当前', 'accent') : '') },
      { label: '模型', key: 'model', width: '60px' },
      { label: 'GPU', key: 'gpu_id', width: '50px' },
      { label: '状态', render: (s) => badge(s.state, s.state === 'running' ? 'ok' : 'bad'), width: '80px' },
      { label: 'RTF', key: 'rtf', width: '60px' },
      { label: '', width: '70px', render: (s) => s.session_id === app.sid ? '' : h('button', { class: 'small', type: 'button', on: { click: () => app.setSession(s.session_id) } }, '切换') },
    ], sys.sessions, { emptyText: '暂无会话（模型加载中？）' }));
    const s = app.session();
    for (const b of modelSeg.querySelectorAll('button')) b.classList.toggle('active', !!s && b.dataset.model === s.model);
    modelHint.textContent = s && s.chunk ? `${s.chunk.chunk_frames} 帧/块 · ${s.chunk.chunk_seconds.toFixed(2)}s` : '';
  }
  function renderWorkers(sys) {
    workerBox.replaceChildren(table([
      { label: 'worker', render: (w) => `#${w.index}`, width: '60px' },
      { label: '显卡', render: (w) => (w.info && w.info.gpu_name) || `gpu ${w.gpu_id}` },
      { label: '状态', render: (w) => h('span', {}, badge(w.state, w.state === 'ready' ? 'ok' : 'bad'), w.error ? h('div', { class: 'hint err-text' }, String(w.error).split('\n')[0].slice(0, 160)) : '') },
      { label: '模型', render: (w) => Object.keys((w.info && w.info.models) || {}).join(', ') || '—' },
      { label: 'attention / compile', render: (w) => `${w.info?.attention ?? '—'} / ${w.info?.compile ?? '—'}` },
      { label: '直播/离线', render: (w) => `${w.live_sessions.length} / ${w.jobs.length}`, width: '80px' },
    ], sys.workers));
  }
  const render = (sys) => {
    if (!sys) return;
    renderServices(sys);
    renderSessions(sys);
    renderWorkers(sys);
  };
  const onSys = (e) => render(e.detail);
  app.events.addEventListener('system', onSys);
  cleanup.push(() => app.events.removeEventListener('system', onSys));
  render(app.system || (await app.refreshSystem()));
}

export function unmount() {
  for (const fn of cleanup) fn();
  cleanup = [];
}
