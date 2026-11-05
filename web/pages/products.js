// 产品与知识库：左边产品列表（拖动排序、上架开关、讲解中标记、公共资料），右边选中产品的 基本信息 / 讲解稿 / 资料 / 测试。
// 接口：/api/products*、/api/kb/*、/api/host/product/*（见 docs/live/API.md）。kb.ingest、products.changed 经 /ws/console 实时刷新。
import { badge, busy, card, cardHead, confirmDialog, empty, fmtDateTime, h, tabs, toast } from '../ui.js';

export const title = '产品与知识库';
let cleanup = [];
const GLOBAL = '__global__';
const SEL_KEY = 'flashhead.productSel';
const TAB_KEY = 'flashhead.productTab';
const STATUS = { queued: ['warn', '排队'], parsing: ['warn', '解析中'], indexing: ['warn', '建索引'], ready: ['ok', '已入库'], failed: ['bad', '失败'] };
const SRC = { manual: '人工', llm: 'LLM 生成', template: '模板草稿' };

function store(key, v) {
  try {
    if (v === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, v);
  } catch { /* 无 localStorage */ }
  return null;
}

export async function mount(el, app) {
  const on = (target, type, fn) => {
    target.addEventListener(type, fn);
    cleanup.push(() => target.removeEventListener(type, fn));
  };
  let products = [];
  let activeId = null;
  let selected = store(SEL_KEY) || null;
  let scriptDirty = false;

  const kbBox = h('div', { class: 'row' });
  const reindexAll = h('button', { type: 'button', class: 'small' }, '全部重建索引');
  const listEl = h('ol', { class: 'plist' });
  const newBtn = h('button', { type: 'button', class: 'small primary' }, '+ 新建产品');
  const detail = h('div', { class: 'stack' });
  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '产品与知识库'), kbBox, h('span', { class: 'grow' }), reindexAll),
    h('div', { class: 'products-layout' },
      card(cardHead('产品', newBtn), h('p', { class: 'hint' }, '拖动调整轮播顺序；开关 = 是否参与讲品轮播。'), listEl),
      detail));

  // ---------------------------------------------------------------- 知识库概况
  async function refreshKb() {
    try {
      const st = await app.api.get('/api/kb/status');
      kbBox.replaceChildren(badge(`检索 ${st.active_backend}`, st.warning ? 'warn' : 'ok'), h('span', { class: 'muted' }, `文档 ${st.docs} · 切块 ${st.chunks}`),
        st.by_status.failed ? badge(`${st.by_status.failed} 份失败`, 'bad') : '',
        st.stale_docs ? badge(`${st.stale_docs} 份需重建（切块参数改过）`, 'warn') : '',
        st.warning ? h('span', { class: 'hint' }, st.warning) : '');
    } catch { /* ignore */ }
  }
  reindexAll.onclick = async () => {
    if (!(await confirmDialog('按当前切块参数重新解析并索引全部文档？期间检索结果可能不完整。', { okText: '重建' }))) return;
    await busy(reindexAll, () => app.api.post('/api/kb/reindex', {}));
    toast('已排队重建', 'ok');
  };

  // ---------------------------------------------------------------- 产品列表
  async function refreshList() {
    const r = await app.api.get('/api/products');
    products = r.items;
    activeId = r.active_id;
    if (selected && selected !== GLOBAL && !products.some((p) => p.id === selected)) selected = null;
    if (!selected) selected = products[0]?.id || GLOBAL;
    renderList();
    return r;
  }
  function renderList() {
    const items = products.map((p) => {
      const sw = h('input', { type: 'checkbox', checked: p.enabled, title: '参与讲品轮播' });
      sw.onclick = (e) => e.stopPropagation();
      sw.onchange = () => busy(null, async () => {
        await app.api.put(`/api/products/${p.id}`, { enabled: sw.checked });
        await refreshList();
      });
      const confirmed = p.segments.filter((s) => s.confirmed).length;
      const li = h('li', { class: `pitem ${p.id === selected ? 'active' : ''}`, draggable: 'true', dataset: { id: p.id } },
        h('span', { class: 'grip', title: '拖动排序' }, '⋮⋮'),
        h('div', { class: 'grow' }, h('div', { class: 'pname' }, p.name),
          h('div', { class: 'row tight' },
            p.id === activeId ? badge('讲解中', 'accent') : '',
            badge(`讲解稿 ${confirmed}/${p.segments.length}`, p.segments.length && confirmed === p.segments.length ? 'ok' : (p.segments.length ? 'warn' : '')),
            badge(`资料 ${p.docs}`), p.enabled ? '' : badge('已停用'))),
        h('label', { class: 'switch' }, sw, h('span')));
      li.onclick = () => select(p.id);
      li.addEventListener('dragstart', (e) => {
        li.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', p.id);
      });
      li.addEventListener('dragend', () => li.classList.remove('dragging'));
      li.addEventListener('dragover', (e) => {
        e.preventDefault();
        const dragging = listEl.querySelector('.dragging');
        if (!dragging || dragging === li) return;
        const after = e.clientY > li.getBoundingClientRect().top + li.offsetHeight / 2;
        li[after ? 'after' : 'before'](dragging);
      });
      li.addEventListener('drop', async (e) => {
        e.preventDefault();
        const ids = [...listEl.querySelectorAll('li[data-id]')].map((x) => x.dataset.id).filter((x) => x !== GLOBAL);
        await busy(null, () => app.api.put('/api/products/order', { ids }));
        await refreshList();
      });
      return li;
    });
    const g = h('li', { class: `pitem global ${selected === GLOBAL ? 'active' : ''}` },
      h('span', { class: 'grip' }, '📁'), h('div', { class: 'grow' }, h('div', { class: 'pname' }, '公共资料'),
        h('div', { class: 'hint' }, '不属于任何产品，如店铺售后、发货政策（所有产品都能检索到）')));
    g.onclick = () => select(GLOBAL);
    listEl.replaceChildren(...items, g);
    if (!products.length) listEl.prepend(h('li', { class: 'muted pempty' }, '还没有产品：点「新建产品」'));
  }
  async function select(id) {
    if (id === selected) return;
    if (scriptDirty && !(await confirmDialog('讲解稿有未保存的修改，放弃修改？', { okText: '放弃', danger: true }))) return;
    selected = id;
    store(SEL_KEY, id);
    renderList();
    renderDetail();
  }
  newBtn.onclick = () => busy(newBtn, async () => {
    const p = await app.api.post('/api/products', { name: `新产品 ${products.length + 1}` });
    await refreshList();
    scriptDirty = false;
    selected = null;
    await select(p.id);
    toast('已新建，先在「基本信息」改名称和价格', 'ok');
  });

  // ---------------------------------------------------------------- 详情
  let detailCleanup = [];
  function renderDetail() {
    for (const fn of detailCleanup) fn();
    detailCleanup = [];
    scriptDirty = false;
    if (selected === GLOBAL) {
      const pane = h('div');
      detail.replaceChildren(card(cardHead('公共资料'), h('p', { class: 'hint' }, '这里的文档对每个产品的提问都会参与检索。'), pane));
      const t = tabs([{ id: 'docs', label: '资料' }, { id: 'test', label: '测试' }], store(TAB_KEY) === 'test' ? 'test' : 'docs', (id) => {
        store(TAB_KEY, id);
        pane.replaceChildren();
        (id === 'docs' ? docsTab : testTab)(pane, '');
      });
      pane.before(t.el);
      return;
    }
    const p = products.find((x) => x.id === selected);
    if (!p) {
      detail.replaceChildren(empty('选择左边的产品', '或者先新建一个产品，再上传它的资料（html / pdf / md）、生成讲解稿。'));
      return;
    }
    const activateBtn = h('button', { type: 'button', class: p.id === activeId ? 'small' : 'small primary' }, p.id === activeId ? '讲解中' : '设为当前讲解');
    activateBtn.disabled = p.id === activeId;
    const delBtn = h('button', { type: 'button', class: 'small danger' }, '删除');
    const pane = h('div');
    detail.replaceChildren(card(cardHead(p.name, activateBtn, delBtn), pane));
    activateBtn.onclick = () => busy(activateBtn, async () => {
      await app.api.post(`/api/host/product/${p.id}/activate`);
      toast(`当前讲解：${p.name}`, 'ok');
      await refreshList();
      renderDetail();
    });
    delBtn.onclick = async () => {
      if (!(await confirmDialog(`删除产品「${p.name}」？它的讲解稿和 ${p.docs} 份资料会一起删除，不能恢复。`, { okText: '删除', danger: true }))) return;
      await busy(delBtn, () => app.api.del(`/api/products/${p.id}`));
      selected = null;
      await refreshList();
      renderDetail();
    };
    const TABS = [{ id: 'info', label: '基本信息' }, { id: 'script', label: '讲解稿' }, { id: 'docs', label: '资料' }, { id: 'test', label: '测试' }];
    let cur = store(TAB_KEY) || 'info';
    if (!TABS.some((t) => t.id === cur)) cur = 'info';
    let shown = null;
    let restoring = false;
    const t = tabs(TABS, cur, async (id) => {
      if (restoring) {                      // 取消切换：只把标签高亮改回去，不重画（保留未保存的修改）
        restoring = false;
        return;
      }
      if (scriptDirty && shown === 'script' && id !== 'script' && !(await confirmDialog('讲解稿有未保存的修改，放弃修改？', { okText: '放弃', danger: true }))) {
        restoring = true;
        t.set('script');
        return;
      }
      shown = id;
      scriptDirty = false;
      store(TAB_KEY, id);
      for (const fn of detailCleanup) fn();
      detailCleanup = [];
      pane.replaceChildren();
      ({ info: infoTab, script: scriptTab, docs: docsTab, test: testTab })[id](pane, p.id, p);
    });
    pane.before(t.el);
  }

  // ---- 基本信息 ----
  function infoTab(pane, pid, p) {
    const name = h('input', { type: 'text', value: p.name, maxLength: 60 });
    const price = h('input', { type: 'text', value: p.price_text, placeholder: '如：日常价 129 元，直播间到手价 99 元' });
    const points = h('textarea', { rows: 4, value: (p.selling_points || []).join('\n'), placeholder: '一行一个卖点' });
    const dur = h('input', { type: 'number', min: 30, max: 7200, step: 10, value: p.duration_s ?? '', placeholder: '留空 = 默认（直播间设置 → 知识库与讲品）' });
    const notes = h('textarea', { rows: 2, value: p.notes || '', placeholder: '备注（只给自己看，不会说出来）' });
    const save = h('button', { type: 'button', class: 'primary' }, '保存');
    const row = (label, ctrl, help = '') => h('div', { class: 'field' }, h('div', { class: 'f-label' }, label), h('div', { class: 'f-ctrl' }, ctrl, help ? h('div', { class: 'hint' }, help) : ''));
    pane.append(row('名称', name), row('价格 / 机制', price, '讲解稿与话术里的 {price}；只写资料里有的、能核实的价格'),
      row('卖点', points, '生成讲解稿时会用到'), row('讲解时长（秒）', dur, '轮播时这个产品讲多久再换下一个'), row('备注', notes),
      h('div', { class: 'row' }, h('span', { class: 'grow' }), save));
    save.onclick = () => busy(save, async () => {
      await app.api.put(`/api/products/${pid}`, { name: name.value, price_text: price.value, selling_points: points.value,
        duration_s: dur.value === '' ? null : Number(dur.value), notes: notes.value });
      toast('已保存', 'ok');
      await refreshList();
      detail.querySelector('.card > header h3').textContent = name.value;
    });
  }

  // ---- 讲解稿 ----
  function scriptTab(pane, pid, p) {
    let segs = (p.segments || []).map((s) => ({ ...s }));
    const listBox = h('div', { class: 'stack' });
    const genN = h('select', {}, ...[3, 4, 5, 6, 8].map((n) => new Option(`${n} 段`, n)));
    genN.value = '5';
    const genBtn = h('button', { type: 'button' }, '用知识库生成');
    const allBtn = h('button', { type: 'button', class: 'small' }, '全部确认');
    const addBtn = h('button', { type: 'button', class: 'small' }, '+ 添加一段');
    const saveBtn = h('button', { type: 'button', class: 'primary' }, '保存');
    const dirtyHint = h('span', { class: 'hint' });
    const setDirty = (v = true) => {
      scriptDirty = v;
      dirtyHint.textContent = v ? '有未保存的修改' : '';
      saveBtn.disabled = !v;
    };
    pane.append(
      h('p', { class: 'hint' }, '没有弹幕时数字人按顺序逐段讲；有高优先级互动先回应，再从被打断的那段接着讲。可用占位符 {product}（产品名）、{price}（价格）、{anchor}（主播自称）。',
        '生成的段落标「待确认」，勾选确认后才参与轮播（广告法要求人工把关）。'),
      h('div', { class: 'row' }, genN, genBtn, allBtn, addBtn, h('span', { class: 'grow' }), dirtyHint, saveBtn), listBox);
    function render() {
      listBox.replaceChildren(...segs.map((s, i) => {
        const ta = h('textarea', { rows: 3, value: s.text, maxLength: 500 });
        const count = h('span', { class: 'hint num' }, `${s.text.length} 字`);
        ta.oninput = () => {
          s.text = ta.value;
          s.confirmed = false;
          ok.checked = false;
          count.textContent = `${ta.value.length} 字`;
          setDirty();
        };
        const ok = h('input', { type: 'checkbox', checked: !!s.confirmed });
        ok.onchange = () => {
          s.confirmed = ok.checked;
          setDirty();
        };
        const move = (d) => {
          const j = i + d;
          if (j < 0 || j >= segs.length) return;
          [segs[i], segs[j]] = [segs[j], segs[i]];
          setDirty();
          render();
        };
        const up = h('button', { type: 'button', class: 'small', title: '上移' }, '↑');
        const down = h('button', { type: 'button', class: 'small', title: '下移' }, '↓');
        const del = h('button', { type: 'button', class: 'small danger', title: '删除' }, '删除');
        up.onclick = () => move(-1);
        down.onclick = () => move(1);
        del.onclick = () => {
          segs.splice(i, 1);
          setDirty();
          render();
        };
        return h('div', { class: `seg-row ${s.confirmed ? '' : 'pending'}` },
          h('div', { class: 'row tight' }, h('b', { class: 'num' }, `第 ${i + 1} 段`),
            h('label', { class: 'inline' }, ok, s.confirmed ? '已确认' : '待确认'),
            badge(SRC[s.source] || s.source || '人工', s.source === 'llm' ? 'accent' : ''),
            (s.hits || []).length ? badge(`合规已替换：${s.hits.join('、')}`, 'warn') : '',
            h('span', { class: 'grow' }), count, up, down, del),
          ta);
      }));
      if (!segs.length) listBox.append(empty('还没有讲解稿', '先在「资料」页上传这个产品的资料，再点「用知识库生成」；也可以「添加一段」手写。'));
    }
    render();
    setDirty(false);
    addBtn.onclick = () => {
      segs.push({ text: '', confirmed: false, source: 'manual', hits: [] });
      setDirty();
      render();
      listBox.querySelector('.seg-row:last-child textarea')?.focus();
    };
    allBtn.onclick = () => {
      for (const s of segs) s.confirmed = true;
      setDirty();
      render();
    };
    saveBtn.onclick = () => busy(saveBtn, async () => {
      const r = await app.api.put(`/api/products/${pid}/script`, { segments: segs.filter((s) => s.text.trim()) });
      segs = r.segments.map((s) => ({ ...s }));
      setDirty(false);
      render();
      toast(`已保存（${r.playable} 段参与轮播）`, 'ok');
      refreshList();
    });
    genBtn.onclick = async () => {
      if (scriptDirty && !(await confirmDialog('有未保存的修改，生成会覆盖掉待确认的段落（已确认的保留），继续？', { okText: '生成' }))) return;
      await busy(genBtn, async () => {
        genBtn.textContent = '生成中…';
        try {
          const r = await app.api.post(`/api/products/${pid}/script/generate`, { segments: Number(genN.value) });
          segs = r.product.segments.map((s) => ({ ...s }));
          setDirty(false);
          render();
          toast(r.error ? `已生成 ${r.generated} 段（${r.error}）` : `已生成 ${r.generated} 段（${SRC[r.source]}），请逐段检查后确认`, r.error ? 'warn' : 'ok', 7000);
          refreshList();
        } finally {
          genBtn.textContent = '用知识库生成';
        }
      });
    };
  }

  // ---- 资料 ----
  function docsTab(pane, pid) {
    const input = h('input', { type: 'file', multiple: true, accept: '.md,.markdown,.txt,.html,.htm,.pdf' });
    const drop = h('div', { class: 'dropzone' }, h('div', {}, '把 html / pdf / md 文件拖到这里，或 '), input,
      h('div', { class: 'hint' }, '可一次多选；文件名会改成随机 ID 保存；扫描版 PDF 不支持（不做 OCR）'));
    const tbody = h('tbody');
    const chunkBox = h('div');
    pane.append(drop, h('div', { class: 'scroll-y', style: { maxHeight: '50vh' } },
      h('table', { class: 'tbl' }, h('colgroup', {}, ...['', '60px', '60px', '110px', '150px', '170px'].map((w) => h('col', { style: w ? { width: w } : {} }))),
        h('thead', {}, h('tr', {}, ...['文件', '类型', '切块', '状态', '更新时间', ''].map((x) => h('th', {}, x)))), tbody)), chunkBox);
    const send = (files) => busy(null, async () => {
      if (!files.length) return;
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name);
      fd.append('product_id', pid);
      const r = await app.api.upload('/api/kb/docs', fd);
      const bad = r.filter((d) => d.status === 'rejected');
      const dup = r.filter((d) => d.duplicate);
      toast(`已上传 ${r.length - bad.length} 份${dup.length ? `（${dup.length} 份内容重复，沿用已有记录）` : ''}${bad.length ? `；${bad.map((d) => `${d.name}：${d.error}`).join('；')}` : ''}`,
        bad.length ? 'warn' : 'ok', 6000);
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
    async function load() {
      const docs = await app.api.get(`/api/kb/docs?product_id=${encodeURIComponent(pid)}`);
      tbody.replaceChildren(...docs.map((d) => {
        const [k, t] = STATUS[d.status] || ['', d.status];
        const view = h('button', { type: 'button', class: 'small' }, '切块');
        const re = h('button', { type: 'button', class: 'small' }, '重建');
        const del = h('button', { type: 'button', class: 'small danger' }, '删除');
        view.disabled = d.status !== 'ready';
        view.onclick = () => busy(view, async () => {
          const full = await app.api.get(`/api/kb/docs/${d.id}?chunks=true`);
          chunkBox.replaceChildren(card(cardHead(`「${d.name}」的切块（${full.chunk_list.length}）`),
            h('div', { class: 'scroll-y' }, ...full.chunk_list.map((c) => h('div', { class: 'chunk' },
              h('div', { class: 'hint' }, `#${c.seq + 1} ${c.heading}${c.page ? `（第 ${c.page} 页）` : ''} · ${c.text.length} 字`), h('div', {}, c.text))))));
        });
        re.onclick = () => busy(re, () => app.api.post('/api/kb/reindex', { doc_id: d.id }));
        del.onclick = async () => {
          if (!(await confirmDialog(`删除文档「${d.name}」？删除后检索不到它的内容。`, { okText: '删除', danger: true }))) return;
          await busy(del, () => app.api.del(`/api/kb/docs/${d.id}`));
          load();
        };
        return h('tr', { dataset: { id: d.id } },
          h('td', {}, d.name, d.title && d.title !== d.name.replace(/\.[^.]+$/, '') ? h('div', { class: 'hint' }, d.title) : '',
            d.error ? h('div', { class: 'hint err-text' }, d.error) : '', d.stale ? h('div', { class: 'hint' }, '切块参数改过，建议重建') : ''),
          h('td', {}, d.ext.replace('.', '')), h('td', { class: 'num' }, d.status === 'ready' ? d.chunks : '—'),
          h('td', {}, badge(t, k), d.pages ? h('span', { class: 'hint' }, ` ${d.pages} 页`) : ''),
          h('td', { class: 'num muted' }, fmtDateTime(d.updated_at)), h('td', {}, h('div', { class: 'row tight' }, view, re, del)));
      }));
      if (!docs.length) tbody.append(h('tr', {}, h('td', { colSpan: 6, class: 'muted' }, '还没有资料：上传 html / pdf / md')));
    }
    const onIngest = (ev) => {
      const d = ev.detail.data;
      if (d.kind === 'doc' && (d.product_id || '') === pid) load();
    };
    app.events.addEventListener('topic:kb.ingest', onIngest);
    detailCleanup.push(() => app.events.removeEventListener('topic:kb.ingest', onIngest));
    load();
  }

  // ---- 测试 ----
  function testTab(pane, pid) {
    const q = h('input', { type: 'search', placeholder: '输入一个观众可能问的问题，如：这个多少钱 / 多久发货' });
    const searchBtn = h('button', { type: 'button' }, '检索测试');
    const answerBtn = h('button', { type: 'button', class: 'primary' }, '回答测试');
    const speak = h('input', { type: 'checkbox' });
    const nick = h('input', { type: 'text', placeholder: '提问观众昵称（可空）', style: { maxWidth: '180px' } });
    const gender = h('select', {}, new Option('性别未知', 'unknown'), new Option('男', 'male'), new Option('女', 'female'));
    const out = h('div', { class: 'stack' });
    pane.append(h('p', { class: 'hint' }, pid ? '在这个产品的资料 + 公共资料里检索；回答测试走和直播完全一样的路径（检索 → LLM 或资料原句 → 合规过滤）。'
      : '在公共资料里检索。'),
    h('div', { class: 'row' }, q, searchBtn), h('div', { class: 'row' }, nick, gender, h('label', { class: 'inline' }, speak, '让数字人说'),
      h('span', { class: 'grow' }), answerBtn), out);
    const productParam = pid ? `&product_id=${encodeURIComponent(pid)}` : '&product_id=';
    searchBtn.onclick = () => busy(searchBtn, async () => {
      if (!q.value.trim()) return;
      const r = await app.api.get(`/api/kb/search?q=${encodeURIComponent(q.value)}${productParam}`);
      out.replaceChildren(card(cardHead('检索结果', badge(r.enough ? '资料够' : '资料不够（会说「帮您确认」）', r.enough ? 'ok' : 'warn'),
        h('span', { class: 'muted' }, `覆盖度 ${Math.round(r.coverage * 100)}% · ${r.backend} · ${r.ms} ms`)),
      h('div', { class: 'hint' }, `信息点：${r.groups.map((g) => g[0]).join('、') || '（无，泛泛提问）'}；检索词：${r.terms.join(' ')}`),
      ...r.hits.map((x, i) => h('div', { class: 'chunk' },
        h('div', { class: 'hint' }, `[${i + 1}] ${x.doc_name} · ${x.heading}${x.page ? `（第 ${x.page} 页）` : ''} · 分数 ${x.score}`,
          x.matched.length ? ` · 命中 ${x.matched.join('、')}` : ''), h('div', {}, x.text))),
      r.hits.length ? '' : empty('没有命中', '资料里没有相关内容')));
    });
    answerBtn.onclick = () => busy(answerBtn, async () => {
      if (!q.value.trim()) return;
      const r = await app.api.post('/api/kb/answer', { question: q.value, product_id: pid || null, speak: speak.checked,
        nickname: nick.value.trim() || null, gender: gender.value });
      const SOURCE = { template: '模板', llm: 'LLM', knowledge: '知识库', manual: '人工' };
      out.replaceChildren(card(cardHead('最终台词', badge(SOURCE[r.source] || r.source, r.source === 'knowledge' ? 'ok' : 'warn'),
        h('span', { class: 'muted' }, `${r.ms} ms`), r.cue_id ? badge('已交给主持人', 'accent') : ''),
      h('div', { class: 'answer' }, r.text || '（空）'),
      r.hits.length ? h('div', { class: 'hint err-text' }, `合规替换：${r.hits.join('、')}`) : '',
      r.note ? h('div', { class: 'hint' }, r.note) : '',
      r.refs.length ? h('div', { class: 'hint' }, `依据：${r.refs.map((x) => `${x.doc_name} ${x.heading.split(' / ').pop()}${x.page ? ` p${x.page}` : ''}`).join('；')}`) : ''));
    });
    q.onkeydown = (e) => e.key === 'Enter' && searchBtn.click();
  }

  // ---------------------------------------------------------------- 实时刷新
  on(app.events, 'topic:kb.ingest', () => refreshKb());
  on(app.events, 'topic:products.changed', async (ev) => {
    const d = ev.detail.data || {};
    await refreshList();
    if (d.reason === 'active' && !scriptDirty) renderDetail();
  });
  cleanup.push(() => {
    for (const fn of detailCleanup) fn();
  });
  await Promise.all([refreshKb(), refreshList()]);
  renderDetail();
}

export function unmount() {
  for (const fn of cleanup) fn();
  cleanup = [];
}
