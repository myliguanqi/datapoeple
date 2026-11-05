// 页面走查脚本（P7）：在 Edge/Chrome 的 DevTools 协议里对 http://127.0.0.1:8000/ 执行这个函数（例如 chrome-devtools MCP 的
// evaluate_script(sourcePath=本文件)），会在亮 / 暗两套主题下逐页、逐标签切换，返回横向溢出（scrollWidth > innerWidth
// 或有元素超出右边界）的页面和 JS 报错。先把窗口调到 1366×768，再用设备模拟调到 390 宽各跑一遍。node --check 能检查语法。
async () => {
  const w = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = [];
  const errs = [];
  const over = () => document.documentElement.scrollWidth > innerWidth;
  const wide = () => [...document.querySelectorAll('#page *')].filter((e) => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.right > innerWidth + 1 && getComputedStyle(e).position !== 'fixed';
  }).slice(0, 3).map((e) => `${e.tagName}.${e.className}`);
  window.addEventListener('error', (e) => errs.push(String(e.message)));
  for (const theme of ['light', 'dark']) {
    document.documentElement.setAttribute('data-theme', theme);
    for (const pg of ['console', 'danmaku', 'products', 'media', 'settings', 'system', 'offline']) {
      location.hash = `#/${pg}`;
      await w(1300);
      const tabs = [...document.querySelectorAll('#page .tabs button')];
      if (!tabs.length) {
        out.push([theme, pg, over(), wide()]);
        continue;
      }
      for (const t of tabs) {
        t.click();
        await w(600);
        out.push([theme, `${pg}/${t.textContent}`, over(), wide()]);
      }
    }
  }
  return { size: [innerWidth, innerHeight], bad: out.filter((x) => x[2] || x[3].length), checked: out.length, errs };
}
