// 产品与知识库（第二阶段 P5 实现）。
import { empty, h } from '../ui.js';

export const title = '产品与知识库';

export function mount(el) {
  el.append(h('div', { class: 'page-head' }, h('h2', {}, '产品与知识库')),
    empty('开发中（P5）', '产品库、讲解稿、文档上传（html/pdf/md）、检索测试与回答测试将在这里。'));
}
