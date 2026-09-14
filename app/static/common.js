/* ═══ 共享脚本：API 封装、提示、主题 ═══ */

const API = {
  token: localStorage.getItem('bsum_token') || '',

  setToken(t) {
    this.token = t || '';
    if (t) localStorage.setItem('bsum_token', t);
    else localStorage.removeItem('bsum_token');
  },

  headers(extra = {}) {
    const h = { ...extra };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  },

  async request(path, options = {}) {
    const opts = {
      ...options,
      headers: this.headers(options.headers || {}),
    };
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }

    const resp = await fetch(path, opts);

    if (resp.status === 401) {
      API.setToken('');
      if (!location.pathname.startsWith('/login')) {
        location.href = '/login?next=' + encodeURIComponent(location.pathname);
      }
      throw new Error('登录已失效');
    }

    const type = resp.headers.get('content-type') || '';
    let data;
    if (type.includes('application/json')) {
      data = await resp.json();
    } else {
      data = await resp.text();
    }

    if (!resp.ok) {
      const msg = (data && data.message) || `请求失败 (${resp.status})`;
      const err = new Error(msg);
      err.code = data && data.code;
      err.status = resp.status;
      throw err;
    }
    return data;
  },

  get(p) { return this.request(p); },
  post(p, b) { return this.request(p, { method: 'POST', body: b }); },
  put(p, b) { return this.request(p, { method: 'PUT', body: b }); },
  del(p) { return this.request(p, { method: 'DELETE' }); },
};


/* ─── 提示 ─── */

function toast(message, type = 'info', duration = 3000) {
  let wrap = document.querySelector('.toast-wrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }

  const el = document.createElement('div');
  el.className = `toast ${type === 'ok' || type === 'success' ? 'ok' : type === 'err' || type === 'error' ? 'err' : ''}`;
  el.textContent = message;
  wrap.appendChild(el);

  setTimeout(() => {
    el.style.transition = 'opacity .2s, transform .2s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(16px)';
    setTimeout(() => el.remove(), 220);
  }, duration);
}

const ok = (m) => toast(m, 'ok');
const err = (m) => toast(m, 'err');


/* ─── 主题 ─── */

const Theme = {
  get() { return localStorage.getItem('bsum_theme') || 'light'; },
  set(t) {
    localStorage.setItem('bsum_theme', t);
    document.documentElement.setAttribute('data-theme', t);
  },
  toggle() {
    this.set(this.get() === 'dark' ? 'light' : 'dark');
    document.querySelectorAll('[data-theme-icon]').forEach(el => {
      el.textContent = this.get() === 'dark' ? '☀' : '☾';
    });
  },
  init() {
    document.documentElement.setAttribute('data-theme', this.get());
  },
};
Theme.init();


/* ─── 格式化 ─── */

function fmtDuration(sec) {
  sec = parseInt(sec) || 0;
  if (sec <= 0) return '0 分钟';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return `${h} 小时 ${m} 分`;
  const s = sec % 60;
  return m > 0 ? (s ? `${m} 分 ${s} 秒` : `${m} 分钟`) : `${s} 秒`;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}


/* ─── 封面图 ─── */

/**
 * 封面 <img>：优先加载 COS 压缩后的缩略图，失败自动退回原图。
 *
 * 服务端会同时给出 thumbnail_small（COS 实时压缩，比原图省 7~8 成流量）
 * 和 thumbnail（原图）。若存储桶没开通图片处理，或以后换成不回源处理的
 * CDN 域名，缩略图地址会返回 4xx，这时必须退回原图 —— 否则封面直接空掉。
 * onerror 对 HTTP 4xx/5xx 是有效的，所以这个兜底能覆盖上述情况。
 */
function thumbTag(small, full, attrs = 'alt="" referrerpolicy="no-referrer"') {
  const src = escapeHtml(small || full || '');
  if (!src) return '';
  return `<img src="${src}" data-full="${escapeHtml(full || '')}" `
    + `onerror="thumbFallback(this)" ${attrs}>`;
}

/** 缩略图加载失败 → 换原图；原图同样失败就留空，避免来回递归 */
function thumbFallback(el) {
  el.onerror = null;
  const full = el.getAttribute('data-full');
  if (full && full !== el.getAttribute('src')) el.src = full;
}

function fmtTime(s) {
  return s || '—';
}


/* ─── Markdown 渲染 ─── */

// marked 是 UMD 包，由 /static/vendor/marked.min.js 用 <script> 标签引入，
// 加载完成后自行挂到 window.marked。
//
// ⚠️ 不要改回 `import('.../marked.min.js')`：UMD 文件没有 ES Module 导出，
// 动态 import 得到的是空模块命名空间（{}），`window.marked = m.marked || m`
// 会把 UMD 已经正确设置好的全局对象覆盖成 {}，
// 之后每次调用都抛 "window.marked.parse is not a function"，
// 表现就是「任务显示完成但正文空白」「点开内容报错」。

/** 兜底渲染：marked 未就绪时按纯文本分段，至少保证内容可见 */
function mdFallback(text) {
  return '<p>' + escapeHtml(text)
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>') + '</p>';
}

function md(text) {
  if (text == null || text === '') return '';
  const s = String(text);
  const m = window.marked;
  return (m && typeof m.parse === 'function') ? m.parse(s) : mdFallback(s);
}


/* ─── 剪贴板 ─── */

/**
 * 复制文本，返回是否成功。
 *
 * ⚠️ Clipboard API 只在**安全上下文**（HTTPS / localhost）暴露。本站点是 HTTP
 * 部署，`navigator.clipboard` 是 undefined，直接调
 * `navigator.clipboard.writeText(...)` 会同步抛 TypeError —— 它连 Promise 都
 * 没返回，后面的 .catch() 根本执行不到，用户看到的就是「点了没反应」。
 * 所以先判存在，再回退到 execCommand 方案（HTTP 下可用）。
 */
async function copyText(text) {
  const s = text == null ? '' : String(text);
  if (!s) return false;

  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(s);
      return true;
    } catch (_) { /* 用户拒绝授权等 → 走兜底 */ }
  }
  return legacyCopy(s);
}

function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-9999px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);

  const sel = document.getSelection();
  const prev = (sel && sel.rangeCount) ? sel.getRangeAt(0) : null;

  ta.select();
  ta.setSelectionRange(0, ta.value.length);   // iOS Safari 需要

  let done = false;
  try { done = document.execCommand('copy'); } catch (_) { done = false; }

  ta.remove();
  if (prev && sel) { sel.removeAllRanges(); sel.addRange(prev); }
  return done;
}


/* ─── SSE 客户端 ─── */

function subscribeJob(jobId, handlers = {}) {
  const url = `/api/job/${jobId}/stream?token=${encodeURIComponent(API.token)}`;
  const es = new EventSource(url);

  es.addEventListener('progress', (e) => {
    try { handlers.onProgress && handlers.onProgress(JSON.parse(e.data)); } catch (_) {}
  });

  // AI 总结正文增量（服务端边生成边追加，e.data.text 是已生成的全文）
  es.addEventListener('summary', (e) => {
    try { handlers.onSummary && handlers.onSummary(JSON.parse(e.data)); } catch (_) {}
  });

  es.addEventListener('end', (e) => {
    try { handlers.onEnd && handlers.onEnd(JSON.parse(e.data)); } catch (_) {}
    es.close();
  });

  es.addEventListener('error', (e) => {
    // 服务端主动推的 error 事件会带 data；浏览器自身的网络错误则没有。
    // 之前一律兜底成「连接中断」，把真实原因盖掉了，排障时很难受。
    let msg = '';
    if (e && typeof e.data === 'string' && e.data) {
      try { msg = JSON.parse(e.data).message || ''; } catch (_) {}
    }
    if (!msg) {
      // 没有服务端消息 → 是传输层断了。给出可操作的提示而不是一句「连接中断」。
      msg = '与服务器的连接中断。任务可能仍在后台执行，'
          + '可刷新页面到「我的内容」查看结果，或用 API 查询：'
          + `/api/job/${jobId}`;
    }
    handlers.onError && handlers.onError(msg);
    es.close();
  });

  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) return;
    // 网络抖动时浏览器会自动重连，不主动关闭
  };

  return es;
}


/* ─── 登出 ─── */

async function logout() {
  API.setToken('');
  location.href = '/login';
}
