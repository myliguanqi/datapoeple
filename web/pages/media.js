// 视频素材（第二阶段 P6 实现）。
import { empty, h } from '../ui.js';

export const title = '视频素材';

export function mount(el) {
  el.append(h('div', { class: 'page-head' }, h('h2', {}, '视频素材')),
    empty('开发中（P6）', '视频上传、预览、关联产品，以及在直播画面里全屏 / 画中画播放将在这里。'));
}
