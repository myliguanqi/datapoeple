// 人设：模板列表（切换 / 复制 / 编辑 / 删除）与完整编辑器（提示词、弹幕意图、触发器、称呼、节目单叫法、知识库兜底）。
// 接口：/api/personas*；出厂模板只读，复制后编辑。节目单和知识库按人设隔离，视频素材共用。
import { badge, busy, card, cardHead, confirmDialog, empty, fmtTime, h, promptDialog, tabs, toast } from '../ui.js';
import { triggersTab } from './interaction_tabs.js';

export const title = '人设';
let cleanup = [];

const QA_LABEL = { strict: '严格依据资料', context: '资料作参考', off: '不检索' };
const CARD_LABEL = { product: '完整条目卡片', title: '只显示标题', off: '不显示卡片' };
const WINDOWS = [['chat_s', '弹幕窗口'], ['question_s', '提问窗口'], ['gift_s', '礼物窗口'], ['member_s', '进场窗口'], ['follow_s', '关注窗口']];

export async function mount(el, app) {
  cleanup = [];
  const on = (target, type, fn) => {
    target.addEventListener(type, fn);
    cleanup.push(() => target.removeEventListener(type, fn));
  };
  let data = null;
  let editing = null;
  let editorCleanup = [];

  const headLine = h('div', { class: 'row' });
  const cardsBox = h('div', { class: 'grid cols-2' });
  const editorBox = h('div', { class: 'stack' });
  el.append(
    h('div', { class: 'page-head' }, h('h2', {}, '人设'), headLine, h('span', { class: 'grow' }),
      h('a', { href: '#/products' }, '节目与知识库 →')),
    card(cardHead('人设模板', h('span', { class: 'hint' }, '切换只在停播时允许；每个人设有自己的节目单和知识库，视频素材共用')),
      cardsBox),
    editorBox);

  async function load() {
    data = await app.api.get('/api/personas');
    renderHead();
    renderCards();
  }

  function renderHead() {
    const a = data.active;
    const busyText = data.busy;
    headLine.replaceChildren(
      badge(`当前：${a.name}`, 'accent'),
      h('span', { class: 'muted' }, `自称「${a.anchor_name || '主播'}」 · ${a.program?.item_label || '节目'} · ${a.intents?.length || 0} 个意图 · ${Object.keys(a.triggers || {}).length} 个触发器`),
      busyText ? badge(busyText, 'warn') : badge('可以切换', 'ok'));
  }

  function renderCards() {
    cardsBox.replaceChildren(...data.items.map((p) => {
      const active = p.id === data.active_id;
      const use = h('button', { type: 'button', class: active ? 'small' : 'small primary', disabled: active }, active ? '使用中' : '使用');
      use.onclick = () => doActivate(p);
      const dup = h('button', { type: 'button', class: 'small' }, '复制');
      dup.onclick = () => doDuplicate(p);
      const edit = h('button', { type: 'button', class: 'small' }, p.builtin ? '查看' : '编辑');
      edit.onclick = () => openEditor(p.id);
      const del = h('button', { type: 'button', class: 'small danger' }, '删除');
      del.hidden = p.builtin || active;
      del.onclick = () => doDelete(p);
      return card(cardHead(p.name, active ? badge('使用中', 'accent') : (p.builtin ? badge('出厂模板') : badge('我的模板', 'ok'))),
        h('p', { class: 'hint', style: { minHeight: '2.5em' } }, p.description || '（没有描述）'),
        h('div', { class: 'row tight' },
          badge(`${p.intents?.length || 0} 个意图`), badge(`${p.triggers} 个触发器`), badge(p.item_label || '节目'),
          active ? '' : h('span', { class: 'grow' })),
        h('div', { class: 'row tight' }, use, dup, edit, del));
    }));
    if (!data.items.length) cardsBox.append(empty('还没有人设模板'));
  }

  async function doActivate(p) {
    if (!(await confirmDialog(`切换到「${p.name}」？\n节目单和知识库会切到这个人设；切换只在停播（未开直播、弹幕未连接）时允许。`,
      { okText: '切换' }))) return;
    try {
      await app.api.post(`/api/personas/${p.id}/activate`);
      toast(`已切换到「${p.name}」`, 'ok');
      editing = null;
      renderEditor();
      await load();
    } catch (e) {
      toast(e.message, 'bad', 7000);
    }
  }

  async function doDuplicate(p) {
    const name = await promptDialog('复制人设为新模板', { label: '新模板名称（复制后可以随便改）', value: `${p.name}（副本）` });
    if (name === null) return;
    await busy(null, async () => {
      const np = await app.api.post(`/api/personas/${p.id}/duplicate`, { name: name.trim() || undefined });
      toast(`已复制为「${np.name}」${np.program_copied ? '（节目单已复制）' : '（节目单为空）'}；知识库资料不复制`, 'ok', 6000);
      await load();
      await openEditor(np.id);
    });
  }

  async function doDelete(p) {
    if (!(await confirmDialog(`删除人设模板「${p.name}」？它的节目单和知识库资料不会自动删除，但模板本身不能恢复。`,
      { okText: '删除', danger: true }))) return;
    await busy(null, async () => {
      await app.api.del(`/api/personas/${p.id}`);
      toast('已删除', 'ok');
      if (editing === p.id) { editing = null; renderEditor(); }
      await load();
    });
  }

  function renderEditor() {
    for (const fn of editorCleanup) fn();
    editorCleanup = [];
    editorBox.replaceChildren();
    if (!editing) return;
  }

  async function openEditor(pid) {
    editing = pid;
    renderEditor();
    let p;
    try {
      p = await app.api.get(`/api/personas/${pid}`);
    } catch (e) {
      editorBox.replaceChildren(card('编辑人设', h('div', { class: 'empty' }, e.message)));
      return;
    }
    if (editing !== pid) return;
    const readOnly = !!p.builtin;
    const nameBox = h('span', { class: 'muted' }, `「${p.name}」`);
    const head = cardHead(readOnly ? `查看人设：${p.name}` : `编辑人设：${p.name}`,
      p.active ? badge('使用中', 'accent') : '',
      readOnly ? h('button', { type: 'button', class: 'small primary', on: { click: () => doDuplicate(p) } }, '复制为新模板') : '',
      nameBox);
    const pane = h('div', { class: 'stack' });
    const t = tabs([
      { id: 'basic', label: '基本信息' }, { id: 'prompts', label: '提示词' }, { id: 'intents', label: '弹幕意图' },
      { id: 'triggers', label: '触发器' }, { id: 'program', label: '节目单与知识库' },
    ], 'basic', (id) => {
      pane.replaceChildren();
      if (id === 'basic') basicTab(pane, p, readOnly);
      else if (id === 'prompts') promptsTab(pane, p, readOnly);
      else if (id === 'intents') intentsTab(pane, p, readOnly);
      else if (id === 'triggers') triggersTab(pane, app, { base: `/api/personas/${p.id}/triggers`, readOnly, addable: !readOnly });
      else programTab(pane, p, readOnly);
    });
    editorBox.append(card(head, t.el, pane));
  }

  const field = (label, ctrl, help = '') => h('div', { class: 'field' },
    h('div', { class: 'f-label' }, label), h('div', { class: 'f-ctrl' }, ctrl, help ? h('div', { class: 'hint' }, help) : ''));

  async function savePatch(pid, patch, btn) {
    return busy(btn, async () => {
      try {
        await app.api.put(`/api/personas/${pid}`, patch);
      } catch (e) {
        const errs = e.body && e.body.errors;
        toast(errs ? `保存失败：${Object.entries(errs).map(([k, v]) => `${k} ${v}`).join('；')}` : e.message, 'bad', 8000);
        return;
      }
      toast('已保存，立即生效', 'ok');
      await load();
      await openEditor(pid);
    });
  }

  // ---- 基本信息 ----
  function basicTab(pane, p, readOnly) {
    const name = h('input', { type: 'text', value: p.name, maxLength: 30, disabled: readOnly });
    const desc = h('textarea', { rows: 2, value: p.description || '', disabled: readOnly });
    const anchor = h('input', { type: 'text', value: p.anchor_name || '', disabled: readOnly });
    const shop = h('input', { type: 'text', value: p.shop_name || '', disabled: readOnly });
    const honor = (key, label) => ['male', 'female', 'unknown'].map((g) =>
      h('label', { class: 'inline' }, `${label}${g === 'male' ? '（男）' : g === 'female' ? '（女）' : '（未知）'}`,
        h('input', { type: 'text', value: (p[key] || {})[g] || '', style: { width: '6em' }, dataset: { key, g }, disabled: readOnly })));
    const rlim = h('input', { type: 'number', min: 10, max: 300, value: p.limits?.reply_max_chars ?? '', placeholder: '留空 = 全局', style: { width: '8em' }, disabled: readOnly });
    const alim = h('input', { type: 'number', min: 10, max: 300, value: p.limits?.answer_max_chars ?? '', placeholder: '留空 = 全局', style: { width: '8em' }, disabled: readOnly });
    const save = h('button', { type: 'button', class: 'primary' }, '保存基本信息');
    save.hidden = readOnly;
    save.onclick = () => {
      const vals = (key) => Object.fromEntries(['male', 'female', 'unknown'].map((g) =>
        [g, pane.querySelector(`input[data-key="${key}"][data-g="${g}"]`).value.trim()]));
      const patch = {
        name: name.value, description: desc.value, anchor_name: anchor.value, shop_name: shop.value,
        honorific: vals('honorific'), big_honorific: vals('big_honorific'),
        limits: { reply_max_chars: rlim.value === '' ? null : +rlim.value, answer_max_chars: alim.value === '' ? null : +alim.value },
      };
      savePatch(p.id, patch, save);
    };
    pane.append(
      h('div', { class: 'grid cols-2' },
        field('人设名称', name, '只用于网页和日志，不会说出口'),
        field('主播自称', anchor, '话术里的 {anchor} 会替换成它'),
        field('店铺名（可空）', shop, '话术里的 {shop_line} 会替换成「，店铺「xx」」'),
        field('描述', desc, '给自己看的备注')),
      field('观众称呼后缀', h('div', { class: 'row tight' }, ...honor('honorific', '称呼后缀')), '昵称简称后面加的字：小王哥 / 李姐；未知性别默认不加'),
      field('尊称（礼物答谢）', h('div', { class: 'row tight' }, ...honor('big_honorific', '尊称')), '礼物答谢里的「大哥 / 大姐」'),
      field('字数上限', h('div', { class: 'row tight' }, h('label', { class: 'inline' }, '互动台词', rlim),
        h('label', { class: 'inline' }, '知识库回答', alim)), '留空 = 用「直播间设置 → 主持人」里的全局值'),
      h('div', { class: 'row' }, h('span', { class: 'grow' }), save));
  }

  // ---- 提示词 ----
  function promptsTab(pane, p, readOnly) {
    const box = (key, label, help) => {
      const ta = h('textarea', { rows: key === 'prompt' ? 14 : 8, value: p[key] || '', disabled: readOnly, style: { width: '100%', fontFamily: 'monospace' } });
      return { el: field(label, ta, help), ta };
    };
    const prompt = box('prompt', '人设提示词', 'LLM 写词时的系统提示；{anchor} / {shop_line} 会被替换');
    const rag = box('rag_prompt', '知识库回答规则', '回答 qa = strict/context 的意图时，追加在系统提示后面');
    const script = box('script_prompt', '讲解稿生成提示词', '「用知识库生成讲解稿」时的系统提示');
    const save = h('button', { type: 'button', class: 'primary' }, '保存提示词');
    save.hidden = readOnly;
    save.onclick = () => savePatch(p.id, { prompt: prompt.ta.value, rag_prompt: rag.ta.value, script_prompt: script.ta.value }, save);
    pane.append(prompt.el, rag.el, script.el, h('div', { class: 'row' }, h('span', { class: 'grow' }), save));
  }

  // ---- 弹幕意图 ----
  function intentsTab(pane, p, readOnly) {
    let items = (p.intents || []).map((x) => ({ ...x, keywords: [...(x.keywords || [])] }));
    const list = h('div', { class: 'stack' });
    const add = h('button', { type: 'button', class: 'small' }, '+ 添加意图');
    const save = h('button', { type: 'button', class: 'primary' }, '保存意图');
    add.hidden = readOnly;
    save.hidden = readOnly;
    add.onclick = () => {
      items.push({ name: '', label: '', keywords: [], qa: 'off', first_come: false, window: 'chat_s',
        search_suffix: '', enabled: true });
      render();
    };
    function render() {
      list.replaceChildren(...items.map((it, i) => intentRow(it, i)));
      if (!items.length) list.append(empty('还没有弹幕意图', '没有意图的弹幕会走闲聊触发器；需要知识库回答就加一个 qa = 严格依据资料的意图。'));
    }
    function intentRow(it, i) {
      const en = h('input', { type: 'checkbox', checked: it.enabled !== false, disabled: readOnly });
      const name = h('input', { type: 'text', value: it.name || '', placeholder: '英文名，如 product_question', style: { width: '14em' }, disabled: readOnly });
      const label = h('input', { type: 'text', value: it.label || '', placeholder: '中文名', style: { width: '9em' }, disabled: readOnly });
      const kw = h('textarea', { rows: 2, value: (it.keywords || []).join('，'), disabled: readOnly });
      const qa = h('select', { disabled: readOnly }, ...Object.entries(QA_LABEL).map(([v, t]) => new Option(t, v)));
      qa.value = it.qa || 'off';
      const fifo = h('input', { type: 'checkbox', checked: !!it.first_come, disabled: readOnly });
      const win = h('select', { disabled: readOnly }, ...WINDOWS.map(([v, t]) => new Option(t, v)));
      win.value = it.window || 'question_s';
      const suffix = h('input', { type: 'text', value: it.search_suffix || '', placeholder: '检索时追加（可空）', style: { width: '12em' }, disabled: readOnly });
      const up = h('button', { type: 'button', class: 'small', title: '上移（先匹配）' }, '↑');
      const down = h('button', { type: 'button', class: 'small', title: '下移' }, '↓');
      const del = h('button', { type: 'button', class: 'small danger' }, '删除');
      up.hidden = down.hidden = del.hidden = readOnly;
      const move = (d) => {
        const j = i + d;
        if (j < 0 || j >= items.length) return;
        update();
        [items[i], items[j]] = [items[j], items[i]];
        render();
      };
      up.onclick = () => move(-1);
      down.onclick = () => move(1);
      del.onclick = () => { items.splice(i, 1); render(); };
      const update = () => {
        items[i] = {
          ...items[i], enabled: en.checked, name: name.value.trim(), label: label.value.trim(),
          keywords: kw.value.split(/[\n,，]/).map((x) => x.trim()).filter(Boolean), qa: qa.value,
          first_come: fifo.checked, window: win.value, search_suffix: suffix.value.trim(),
        };
      };
      for (const el of [en, name, label, kw, qa, fifo, win, suffix]) {
        el.addEventListener('input', update);
        el.addEventListener('change', update);
      }
      return h('div', { class: 'card', style: { padding: '8px 12px' } },
        h('div', { class: 'row tight' }, h('label', { class: 'switch', title: '启用' }, en, h('span')), name, label,
          h('span', { class: 'grow' }), up, down, del),
        h('div', { class: 'row tight' }, h('span', { class: 'hint' }, '关键词（逗号 / 换行分隔）')),
        kw,
        h('div', { class: 'row tight' }, h('label', { class: 'inline' }, '回答方式', qa),
          h('label', { class: 'inline' }, fifo, '先来先答'), h('label', { class: 'inline' }, '保鲜窗口', win),
          h('label', { class: 'inline' }, '检索追加', suffix)));
    }
    save.onclick = () => savePatch(p.id, { intents: items }, save);
    pane.append(
      h('p', { class: 'hint' }, '按顺序匹配，先命中先算。意图的英文名同时是触发器里的 when（改了名字要同步改触发器）。qa = 严格依据资料时，资料不够会说人设里的兜底话术。'),
      list, h('div', { class: 'row' }, add, h('span', { class: 'grow' }), save));
    render();
  }

  // ---- 节目单与知识库 ----
  function programTab(pane, p, readOnly) {
    const pg = p.program || {};
    const itemLabel = h('input', { type: 'text', value: pg.item_label || '', disabled: readOnly });
    const showPrice = h('input', { type: 'checkbox', checked: !!pg.show_price, disabled: readOnly });
    const cardMode = h('select', { disabled: readOnly }, ...Object.entries(CARD_LABEL).map(([v, t]) => new Option(t, v)));
    cardMode.value = pg.card || 'title';
    const defaultText = h('input', { type: 'text', value: pg.default_item_text || '', disabled: readOnly });
    const closing = h('textarea', { rows: 2, value: pg.closing_line || '', disabled: readOnly });
    const noAnswer = h('textarea', { rows: 2, value: p.knowledge?.no_answer_text || '', disabled: readOnly });
    const save = h('button', { type: 'button', class: 'primary' }, '保存节目单设置');
    save.hidden = readOnly;
    save.onclick = () => savePatch(p.id, {
      program: { item_label: itemLabel.value, show_price: showPrice.checked, card: cardMode.value,
        default_item_text: defaultText.value, closing_line: closing.value },
      knowledge: { no_answer_text: noAnswer.value },
    }, save);
    pane.append(
      p.active ? '' : h('p', { class: 'hint' }, '这是未启用的人设：节目单条目和知识库资料要切换到这个模板后，才能在「节目与知识库」页管理。'),
      h('p', { class: 'hint' }, '节目单条目（带货人设是商品，讲解人设是讲解主题）在「节目与知识库」页管理；每个条目可以关联讲解稿和视频。'),
      h('div', { class: 'grid cols-2' },
        field('条目叫法', itemLabel, '如：商品 / 讲解主题 / 节目'),
        field('场景卡片', cardMode, '讲解人设通常用「只显示标题」，娱乐人设用「不显示卡片」'),
        field('默认条目文案', defaultText, '没有当前条目时，话术里 {product} 的兜底'),
        field('允许显示价格', h('label', { class: 'inline' }, showPrice, '在网页和场景卡片里显示价格')),
        field('结尾引导语', closing, '生成讲解稿时追加在最后；可空。占位符 {product} {price} {anchor}')),
      field('资料不够时的话术', noAnswer, 'qa = 严格依据资料时，资料里没有答案就说这句；{asker} 是提问观众的称呼'),
      h('div', { class: 'row' }, h('a', { href: '#/products' }, '去管理节目单与知识库 →'), h('span', { class: 'grow' }), save));
  }

  on(app.events, 'topic:persona.changed', () => load());
  on(app.events, 'topic:products.changed', () => { if (data) load(); });
  await load();
}

export function unmount() {
  for (const fn of cleanup) fn();
  cleanup = [];
}
