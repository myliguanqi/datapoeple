// fetch 封装：自动带 X-Admin-Token；写操作返回 401 时弹出口令输入，输入后重试一次。
// 口令存 localStorage（try/catch：隐私模式下可能不可用，此时只在本页内存里保留）。

const TOKEN_KEY = 'flashhead.adminToken';
let memToken = null;

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || memToken || '';
  } catch {
    return memToken || '';
  }
}

export function setToken(t) {
  memToken = t || null;
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 存不了就只放内存 */
  }
}

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

let askToken = null; // 由 app.js 注入：() => Promise<string|null>
export function onTokenRequired(fn) {
  askToken = fn;
}

export async function api(path, opts = {}, retried = false) {
  const headers = new Headers(opts.headers || {});
  const tok = getToken();
  if (tok) headers.set('X-Admin-Token', tok);
  let body = opts.body;
  if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(body);
  }
  const r = await fetch(path, { ...opts, headers, body });
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json().catch(() => null) : await r.text();
  if (r.status === 401 && !retried && askToken) {
    const t = await askToken();
    if (t) {
      setToken(t);
      return api(path, opts, true);
    }
  }
  if (!r.ok) {
    let msg = (data && data.detail) || (typeof data === 'string' && data) || `HTTP ${r.status}`;
    if (typeof msg !== 'string') msg = JSON.stringify(msg);
    throw new ApiError(r.status, msg, data);
  }
  return data;
}

export const get = (p) => api(p);
export const post = (p, data) => api(p, { method: 'POST', body: data === undefined ? {} : data });
export const put = (p, data) => api(p, { method: 'PUT', body: data });
export const del = (p) => api(p, { method: 'DELETE' });
export const upload = (p, formData) => api(p, { method: 'POST', body: formData });
