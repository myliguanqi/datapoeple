// 弹幕事件的展示：用户标签（昵称 性别 财富等级 粉丝团 熟客/金主）、事件文字、画像卡、cookie 粘贴框。
// 数据来自 /ws/console 的 live.event（已节流：like_batch / member_batch）或 /api/danmaku/events。
import { badge, busy, fmtTime, h, modal, promptDialog, toast } from './ui.js';

export const TYPE_LABEL = {
  chat: '弹幕', gift: '礼物', like: '点赞', like_batch: '点赞', member: '进场', member_batch: '进场', follow: '关注',
  share: '分享', fansclub: '粉丝团', room_stat: '在线', rank: '榜单', room_stats: '统计', room_notice: '通知',
  product_change: '商品', stream_end: '下播', system: '系统',
};
export const TYPE_KIND = { gift: 'warn', follow: 'ok', fansclub: 'ok', stream_end: 'bad', chat: 'accent' };
const GENDER = { male: '♂', female: '♀', unknown: '·' };

/** 用户标签：昵称 ♂/♀/· 💎12 🎫5 熟客/金主（tags 来自长期记忆，可选） */
export function userTags(u, { onClick, tags } = {}) {
  if (!u) return h('span', { class: 'muted' }, '—');
  const name = h(onClick ? 'button' : 'span', { class: onClick ? 'link' : '', type: 'button', title: u.user_id || '', on: onClick ? { click: () => onClick(u) } : {} },
    u.nickname || '（无昵称）');
  const parts = [name, ' ', h('span', { class: 'muted', title: '性别' }, GENDER[u.gender] || '·')];
  if (u.wealth_level > 0) parts.push(' ', badge(`💎${u.wealth_level}`, u.wealth_level >= 20 ? 'warn' : ''));
  if (u.fans_club && u.fans_club.level) parts.push(' ', badge(`🎫${u.fans_club.level}`, 'accent'));
  for (const t of tags || []) parts.push(' ', badge(t, 'ok'));
  return h('span', {}, ...parts);
}

export function describe(e) {
  const d = e.data || {};
  switch (e.type) {
    case 'chat': return d.is_emoji ? `${d.content}（表情）` : d.content;
    case 'gift': return `${d.gift_name} ×${d.count}（${d.total_diamonds} 钻）${d.is_final === false ? ' 连击中…' : ''}`;
    case 'like': return `点赞 ×${d.count}`;
    case 'like_batch': return `点赞 ${e.count}（${e.events} 次${e.users && e.users[0] ? `，最猛：${e.users[0].nickname} ${e.users[0].count}` : ''}）`;
    case 'member': return '进入直播间';
    case 'member_batch': return `进场 ×${e.count}：${(e.users || []).map((u) => u.nickname).slice(0, 6).join('、')}${e.count > 6 ? '…' : ''}`;
    case 'follow': return '关注了主播';
    case 'share': return '分享了直播间';
    case 'fansclub': return d.kind === 'upgrade' ? `粉丝团升级 ${d.content || ''}` : `加入粉丝团 ${d.content || ''}`;
    case 'room_stat': return `在线 ${d.online ?? '—'}${d.total_pv ? ` · 累计 ${d.total_pv}` : ''}`;
    case 'rank': return `贡献榜：${(d.ranks || []).slice(0, 5).map((r) => `${r.nickname}(${r.score})`).join(' > ')}`;
    case 'room_stats': return d.display_long || String(d.display_value ?? '');
    case 'room_notice': return d.content;
    case 'product_change': return `商品变更 ${d.toast || ''} ${(d.products || []).length} 件`;
    case 'stream_end': return '直播已结束';
    default: return d.message || JSON.stringify(d).slice(0, 120);
  }
}

export function eventUser(e) {
  return e.user || null;
}

export function typeBadge(t) {
  return badge(TYPE_LABEL[t] || t, TYPE_KIND[t] || '');
}

export function timeOf(e) {
  return fmtTime(e.ts);
}

/** 观众画像卡（来自事件本身；有长期记忆时追加历史统计） */
export function profileCard(u, memory) {
  const rows = [
    ['昵称', u.nickname], ['用户 ID', u.user_id || '（拿不到）'], ['性别', { male: '男', female: '女' }[u.gender] || '未知'],
    ['财富等级', u.wealth_level ? `${u.wealth_level}${u.wealth_name ? `（${u.wealth_name}）` : ''}` : '—'],
    ['累计送出钻石', u.wealth_diamonds || '—'],
    ['粉丝团', u.fans_club ? `${u.fans_club.name || ''} ${u.fans_club.level} 级` : '—'],
    ['用户等级', u.user_level || '—'], ['粉丝数', u.followers ?? '—'], ['认证', u.verified ? '是' : '否'],
  ];
  for (const [k, v] of Object.entries(u.extra || {})) {
    if (k === 'wealth_progress') rows.push(['升级进度', `${v.now}/${v.next}（还差 ${v.need}）`]);
    else rows.push([k, typeof v === 'object' ? JSON.stringify(v) : v]);
  }
  if (memory) {
    rows.push(['——', '长期记忆'], ['累计进场', memory.visits], ['累计送礼', `${memory.gift_count} 个 / ${memory.gift_value} 钻`],
      ['发言次数', memory.chat_count], ['关注过', memory.followed ? '是' : '否'], ['最高财富等级', memory.max_wealth_level ?? '—'],
      ['往场就认识', memory.known_before ? '是' : '否（本场新人）']);
  }
  const body = h('div', {},
    u.avatar_url ? h('img', { src: u.avatar_url, alt: '', referrerpolicy: 'no-referrer', style: { width: '56px', height: '56px', borderRadius: '50%', float: 'right' } }) : '',
    h('dl', { class: 'kv' }, rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, String(v ?? '—'))])));
  return modal(`观众：${u.nickname || u.user_id}`, body, [{ text: '关闭', result: null }]);
}

/** 粘贴抖音登录 cookie（只写不读） */
export async function pasteCookie(app) {
  const raw = await promptDialog('粘贴抖音登录 cookie', {
    label: '游客模式收不到礼物。登录 live.douyin.com 后，在浏览器开发者工具里复制任意请求的「cURL」或请求头里的 cookie 整行粘进来；只保存在本机 secrets/，网页上不会再显示。约 2 个月过期，礼物突然收不到时重新粘贴。',
    multiline: true,
  });
  if (!raw) return null;
  return busy(null, async () => {
    const r = await app.api.put('/api/danmaku/cookie', { cookie: raw });
    toast(`cookie 已保存（${r.length} 字符${r.has_sessionid ? '，含 sessionid' : '，⚠ 没看到 sessionid，可能不是登录态'}）`, r.has_sessionid ? 'ok' : 'warn', 6000);
    return r;
  });
}

export function cookieText(c) {
  if (!c || !c.configured) return '未配置（游客态，收不到礼物）';
  return `已配置（约 ${c.age_days} 天前更新${c.has_sessionid ? '' : '，⚠ 不含 sessionid'}）`;
}
