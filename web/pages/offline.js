// 离线视频：图片 + 文本（走 TTS）或音频 -> mp4。
import { badge, busy, card, fmtDateTime, h, table, toast } from '../ui.js';

export const title = '离线视频';
let alive = false;

export async function mount(el, app) {
  alive = true;
  const img = h('input', { type: 'file', accept: 'image/*' });
  const aud = h('input', { type: 'file', accept: 'audio/*' });
  const text = h('textarea', { rows: 4, placeholder: '文本（走 TTS）；上传了音频则忽略文本' }, '你好，这是一段由图片和文字生成的数字人视频。');
  const model = h('select', {}, new Option('Lite', 'lite'), new Option('Pro', 'pro'));
  const voice = h('select', { title: '音色' });
  const crop = h('input', { type: 'checkbox' });
  const btn = h('button', { class: 'primary', type: 'button' }, '生成');
  const bar = h('div');
  const msg = h('div', { class: 'hint' });
  const video = h('video', { controls: true, hidden: true });
  const jobsBox = h('div');

  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '离线视频')),
    h('div', { class: 'grid main-side' },
      card('生成视频',
        h('div', { class: 'grid cols-2' },
          h('label', { class: 'stack muted' }, '形象图片（不选用默认形象）', img),
          h('label', { class: 'stack muted' }, '或上传音频', aud)),
        h('div', { class: 'row' }, text),
        h('div', { class: 'row' }, model, voice, h('label', { class: 'inline' }, crop, '人脸裁剪'), h('span', { class: 'grow' }, ''), btn),
        h('div', { class: 'progress' }, bar), msg, video),
      card('最近任务', jobsBox)));

  btn.onclick = () => busy(btn, async () => {
    const fd = new FormData();
    if (img.files[0]) fd.append('image', img.files[0]);
    if (aud.files[0]) fd.append('audio', aud.files[0]);
    fd.append('text', text.value);
    fd.append('model', model.value);
    fd.append('voice', voice.value);
    fd.append('use_face_crop', crop.checked);
    const job = await app.api.upload('/api/offline/jobs', fd);
    toast(`离线任务 ${job.job_id} 已提交`, 'ok');
    poll(job.job_id);
  });

  async function poll(id) {
    video.hidden = true;
    while (alive) {
      const j = await app.api.get(`/api/offline/jobs/${id}`);
      bar.style.width = `${Math.round(j.progress * 100)}%`;
      msg.textContent = `${j.status} ${j.message || ''}`;
      if (j.status === 'done') {
        video.src = `${j.video_url}?t=${Date.now()}`;
        video.hidden = false;
        refreshJobs();
        return;
      }
      if (j.status === 'failed') {
        toast(`离线任务失败: ${j.message}`, 'bad');
        refreshJobs();
        return;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  async function refreshJobs() {
    const jobs = await app.api.get('/api/offline/jobs');
    const kind = { done: 'ok', failed: 'bad' };
    jobsBox.replaceChildren(table([
      { label: '任务', key: 'job_id' },
      { label: '模型', key: 'model', width: '56px' },
      { label: '状态', render: (j) => badge(j.status, kind[j.status] || ''), width: '90px' },
      { label: '时间', render: (j) => fmtDateTime(j.created_at) },
      { label: '', width: '56px', render: (j) => (j.video_url ? h('a', { href: j.video_url }, '下载') : '') },
    ], jobs.slice(0, 12), { emptyText: '还没有任务' }));
  }

  try {
    const v = await app.api.get('/api/tts/voices');
    voice.replaceChildren(...v.voices.map((it) => new Option(it.name, it.id)));
    voice.value = v.default;
  } catch { /* 音色列表拿不到不影响生成 */ }
  model.value = app.system?.default_model || 'lite';
  refreshJobs();
}

export function unmount() {
  alive = false;
}
