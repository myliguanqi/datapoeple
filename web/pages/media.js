// 视频素材：上传（拖拽多文件）、网格卡片（封面、时长、分辨率、有无声音、关联产品）、预览、立即播放（全屏 / 画中画 / 分屏，原声 / 静音）、
// 关联产品、删除（被产品或背景引用时二次确认）、图片设为直播画面背景；顶部显示当前会话「正在播放」与停止。
import { badge, busy, card, cardHead, confirmDialog, empty, h, modal, toast } from '../ui.js';

export const title = '视频素材';
let cleanup = [];
const MODE = { full: '全屏', pip: '画中画', split: '分屏' };

function dur(s) {
  if (s == null) return '—';
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s % 60)).padStart(2, '0')}`;
}

export async function mount(el, app) {
  const on = (target, type, fn) => {
    target.addEventListener(type, fn);
    cleanup.push(() => target.removeEventListener(type, fn));
  };
  const nowBox = h('div', { class: 'row' });
  const input = h('input', { type: 'file', multiple: true, accept: '.mp4,.webm,.mov,.m4v,.png,.jpg,.jpeg,.webp' });
  const drop = h('div', { class: 'dropzone' }, h('div', {}, '把 mp4 / webm 视频或背景图片拖到这里，或 '), input,
    h('div', { class: 'hint' }, '浏览器能直接播放 mp4（H.264 + AAC）和 webm；mov 不一定能播。文件名会改成随机 ID 保存。'));
  const grid = h('div', { class: 'media-grid' });
  const filter = h('select', {}, new Option('全部', ''), new Option('视频', 'video'), new Option('图片', 'image'));
  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '视频素材'), h('span', { class: 'grow' }),
      h('a', { href: `/scene?session=${encodeURIComponent(app.sid)}`, target: '_blank' }, '打开直播画面（OBS 用）')),
    card(cardHead(`正在播放（会话 ${app.sid}）`), nowBox),
    card(cardHead('素材库', filter), drop, grid));

  let products = [];
  let items = [];
  const defaults = { mode: 'pip', audio: 'video' };

  // ---------------------------------------------------------------- 正在播放
  async function refreshScene() {
    try {
      renderNow(await app.api.get(`/api/sessions/${encodeURIComponent(app.sid)}/scene`));
    } catch { /* ignore */ }
  }
  function renderNow(sc) {
    const v = sc.video;
    if (!v) {
      nowBox.replaceChildren(h('span', { class: 'muted' }, `没有在播视频 · 布局 ${sc.layout === 'dh_with_card' ? '数字人 + 产品卡片' : '数字人'}`));
      return;
    }
    const stop = h('button', { type: 'button', class: 'danger' }, '停止');
    stop.onclick = () => busy(stop, () => app.api.post(`/api/sessions/${encodeURIComponent(app.sid)}/scene/video/stop`));
    nowBox.replaceChildren(badge('播放中', 'ok'), h('b', {}, v.name), h('span', { class: 'muted' },
      `${MODE[v.mode]} · ${v.audio === 'video' ? (v.has_audio ? '原声（讲品暂停）' : '原声（视频无音轨）') : '静音'}${v.loop ? ' · 循环' : ''} · ${dur(v.duration_s)}`),
    h('span', { class: 'grow' }), stop);
  }

  // ---------------------------------------------------------------- 上传
  const send = (files) => busy(null, async () => {
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.name);
    toast(`上传中（${files.length} 个文件）…`);
    const r = await app.api.upload('/api/media', fd);
    const bad = r.filter((d) => d.status === 'rejected');
    toast(`已上传 ${r.length - bad.length} 个${bad.length ? `；${bad.map((d) => `${d.name}：${d.error}`).join('；')}` : ''}`, bad.length ? 'warn' : 'ok', 6000);
    input.value = '';
    load();
  });
  input.onchange = () => send([...input.files]);
  drop.addEventListener('dragover', (e) => {
    e.preventDefault();
    drop.classList.add('over');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    send([...e.dataTransfer.files]);
  });

  // ---------------------------------------------------------------- 网格
  async function load() {
    const [m, p, st] = await Promise.all([app.api.get('/api/media'), app.api.get('/api/products').catch(() => ({ items: [] })),
      app.api.get('/api/settings').catch(() => ({ fields: [] }))]);
    items = m;
    products = p.items;
    for (const f of st.fields) {                     // 「立即播放」默认用设置里的视频布局 / 声音
      if (f.key === 'scene.video.default_mode') defaults.mode = f.value;
      if (f.key === 'scene.video.default_audio') defaults.audio = f.value;
    }
    render();
  }
  filter.onchange = render;
  function render() {
    const list = items.filter((it) => !filter.value || it.kind === filter.value);
    grid.replaceChildren(...list.map(tile));
    if (!list.length) grid.append(empty('还没有素材', '上传产品视频（以后可以用来投流承接），或一张背景图。'));
  }
  function tile(it) {
    const names = it.product_ids.map((id) => products.find((p) => p.id === id)?.name).filter(Boolean);
    const thumb = it.kind === 'image'
      ? h('img', { src: it.url, alt: it.name, loading: 'lazy' })
      : (it.cover_url ? h('img', { src: it.cover_url, alt: it.name, loading: 'lazy' }) : h('div', { class: 'noimg' }, '无封面'));
    const head = h('div', { class: 'thumb' }, thumb, it.kind === 'video' ? h('span', { class: 'dur num' }, dur(it.duration_s)) : '');
    head.onclick = () => previewDialog(it);
    const meta = h('div', { class: 'hint' }, `${it.width}×${it.height}`, it.kind === 'video' ? ` · ${it.has_audio ? '有声' : '无声'}` : ' · 图片',
      ` · ${(it.size / 1048576).toFixed(1)} MB`);
    const tags = h('div', { class: 'row tight' }, ...names.map((n) => badge(n, 'accent')), it.is_background ? badge('当前背景', 'ok') : '');
    const del = h('button', { type: 'button', class: 'small danger' }, '删除');
    del.onclick = () => remove(it, del);
    let actions;
    if (it.kind === 'video') {
      const mode = h('select', { title: '布局' }, ...Object.entries(MODE).map(([k, v]) => new Option(v, k)));
      const audio = h('select', { title: '声音' }, new Option('原声', 'video'), new Option('静音', 'mute'));
      mode.value = defaults.mode;
      audio.value = defaults.audio;
      const play = h('button', { type: 'button', class: 'small primary' }, '立即播放');
      play.onclick = () => busy(play, async () => {
        await app.api.post(`/api/sessions/${encodeURIComponent(app.sid)}/scene/video`, { media_id: it.id, mode: mode.value, audio: audio.value });
        toast(`正在播放「${it.name}」（${MODE[mode.value]}）`, 'ok');
      });
      const link = h('button', { type: 'button', class: 'small' }, '关联产品');
      link.onclick = () => linkDialog(it);
      actions = h('div', { class: 'row tight' }, mode, audio, play, link, h('span', { class: 'grow' }), del);
    } else {
      const bg = h('button', { type: 'button', class: 'small' }, it.is_background ? '取消背景' : '设为背景');
      bg.onclick = () => busy(bg, async () => {
        await app.api.put('/api/settings', { changes: { 'scene.background': it.is_background ? '' : `media:${it.id}` } });
        toast(it.is_background ? '已恢复默认背景' : '已设为直播画面背景', 'ok');
        load();
      });
      actions = h('div', { class: 'row tight' }, bg, h('span', { class: 'grow' }), del);
    }
    return h('div', { class: 'media-tile' }, head, h('div', { class: 'mname', title: it.name }, it.name), meta, tags, actions);
  }

  function previewDialog(it) {
    const body = it.kind === 'video' ? h('video', { src: it.url, controls: true, autoplay: true, style: { maxHeight: '60vh' } })
      : h('img', { src: it.url, style: { maxWidth: '100%', maxHeight: '60vh', display: 'block', margin: '0 auto' } });
    modal(`预览：${it.name}`, body, [{ text: '关闭', result: null }]).then(() => body.pause?.());
  }

  async function linkDialog(it) {
    const boxes = products.map((p) => {
      const cb = h('input', { type: 'checkbox', checked: it.product_ids.includes(p.id), value: p.id });
      return h('label', { class: 'inline', style: { display: 'flex' } }, cb, p.name);
    });
    const body = h('div', { class: 'stack' }, h('div', { class: 'hint' }, '关联后，讲品时可按「讲品中自动插播视频」设置自动播放这个视频。'),
      ...(boxes.length ? boxes : [h('div', { class: 'muted' }, '还没有产品')]));
    const ok = await modal(`「${it.name}」关联到哪些产品`, body, [{ text: '取消', result: null },
      { text: '保存', class: 'primary', value: () => boxes.map((b) => b.querySelector('input')).filter((x) => x.checked).map((x) => x.value) }]);
    if (ok === null || ok === undefined) return;
    await busy(null, () => app.api.put(`/api/media/${it.id}`, { product_ids: ok }));
    toast('已保存关联', 'ok');
    load();
  }

  async function remove(it, btn) {
    if (!(await confirmDialog(`删除素材「${it.name}」？文件会一起删除，不能恢复。`, { okText: '删除', danger: true }))) return;
    try {
      await app.api.del(`/api/media/${it.id}`);
    } catch (e) {
      if (e.status !== 409) {
        toast(e.message, 'bad', 5000);
        return;
      }
      if (!(await confirmDialog(`${e.message.replace(/；确认删除.*$/, '')}。仍然删除并解除关联？`, { okText: '仍然删除', danger: true }))) return;
      await busy(btn, () => app.api.del(`/api/media/${it.id}?force=true`));
    }
    toast('已删除', 'ok');
    load();
  }

  on(app.events, 'topic:scene.changed', (ev) => {
    if (ev.detail.data.session_id === app.sid) renderNow(ev.detail.data.scene);
  });
  on(app.events, 'topic:media.changed', () => load());
  on(app.events, 'session', () => refreshScene());
  await Promise.all([load(), refreshScene()]);
}

export function unmount() {
  for (const fn of cleanup) fn();
  cleanup = [];
}
