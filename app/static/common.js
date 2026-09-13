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

function fmtTime(s) {
  return s || '—';
}


/* ─── SSE 客户端 ─── */

function subscribeJob(jobId, handlers = {}) {
  const url = `/api/job/${jobId}/stream?token=${encodeURIComponent(API.token)}`;
  const es = new EventSource(url);

  es.addEventListener('progress', (e) => {
    try { handlers.onProgress && handlers.onProgress(JSON.parse(e.data)); } catch (_) {}
  });

  es.addEventListener('end', (e) => {
    try { handlers.onEnd && handlers.onEnd(JSON.parse(e.data)); } catch (_) {}
    es.close();
  });

  es.addEventListener('error', (e) => {
    let msg = '连接中断';
    try { msg = JSON.parse(e.data).message || msg; } catch (_) {}
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
