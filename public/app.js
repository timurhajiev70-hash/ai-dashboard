/* «Мои ИИ системы» — фронтенд (vanilla JS, ES2020, без зависимостей) */
'use strict';

(function () {
  // ===== Справочники =====
  const TABS = ['systems', 'matrix', 'recommend', 'settings'];

  const CONN_ORDER = ['verified', 'manual', 'available', 'none', 'unknown'];
  const CONN_EMOJI = { verified: '✅', manual: '✍️', available: '➕', none: '⛔', unknown: '❔' };
  const LANGS = ['ru', 'de'];
  const LANG_KEY = 'aid_lang';

  // ===== Локализация =====
  const I18N = { lang: 'ru', dict: {}, ru: {} };

  // t('ключ', {param}) — строка текущего языка → русская → сам ключ
  function t(key, params) {
    let s = I18N.dict[key];
    if (s == null) s = I18N.ru[key];
    if (s == null) s = key;
    if (params) s = String(s).replace(/\{(\w+)\}/g, (m, k) => (params[k] != null ? String(params[k]) : m));
    return s;
  }

  async function fetchLocale(lang) {
    try {
      const res = await fetch(`/locales/${lang}.json`, { credentials: 'same-origin', cache: 'no-cache' });
      if (!res.ok) return null;
      const data = await res.json();
      return data && typeof data === 'object' ? data : null;
    } catch (e) { return null; }
  }

  // Справочники зависят от языка — пересобираются после загрузки локали
  let CONN_STATUS, CONN_KIND, CATEGORY, ACCESS, API_ACCESS, SITE_ST, API_ST, TOOLS_ST, HANDOFF, THEMES;
  function buildMaps() {
    CONN_STATUS = {};
    CONN_ORDER.forEach((k) => {
      CONN_STATUS[k] = { emoji: CONN_EMOJI[k], label: t(`conn.${k}.label`), short: t(`conn.${k}.short`) };
    });
    CONN_KIND = {
      '': t('connKind.empty'), plugin: t('connKind.plugin'), mcp: t('connKind.mcp'), skill: t('connKind.skill'),
      link: t('connKind.link'), builtin: t('connKind.builtin')
    };
    CATEGORY = {};
    ['mail', 'calendar', 'files', 'code', 'browser', 'terminal', 'design', 'tasks', 'notes', 'skill', 'other']
      .forEach((k) => { CATEGORY[k] = t(`category.${k}`); });
    ACCESS = { app: t('access.app'), browser: t('access.browser'), api: t('access.api'), local: t('access.local') };
    API_ACCESS = { yes: t('apiAccess.yes'), no: t('apiAccess.no'), unknown: t('apiAccess.unknown') };
    // Статусы трёх индикаторов: подпись + цветовой класс
    SITE_ST = {
      ok: [t('siteStatus.ok'), 'c-ok'], down: [t('siteStatus.down'), 'c-bad'], unchecked: [t('status.unchecked'), '']
    };
    API_ST = {
      ok: [t('apiStatus.ok'), 'c-ok'], down: [t('apiStatus.down'), 'c-bad'],
      unchecked: [t('status.unchecked'), ''], 'n/a': [t('apiStatus.na'), '']
    };
    TOOLS_ST = {
      ok: [t('toolsStatus.ok'), 'c-ok'], partial: [t('toolsStatus.partial'), 'c-warn'], unchecked: [t('status.unchecked'), '']
    };
    HANDOFF = { none: t('handoff.none'), url: t('handoff.url'), clipboard: t('handoff.clipboard') };
    THEMES = { auto: t('theme.auto'), light: t('theme.light'), dark: t('theme.dark') };
  }
  buildMaps();

  // Статичный текст index.html: data-i18n, data-i18n-placeholder, data-i18n-aria, data-i18n-title
  function applyStaticI18n() {
    document.documentElement.lang = I18N.lang;
    document.title = t('meta.title');
    $$('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
    $$('[data-i18n-placeholder]').forEach((el) => { el.setAttribute('placeholder', t(el.dataset.i18nPlaceholder)); });
    $$('[data-i18n-aria]').forEach((el) => { el.setAttribute('aria-label', t(el.dataset.i18nAria)); });
    $$('[data-i18n-title]').forEach((el) => { el.setAttribute('title', t(el.dataset.i18nTitle)); });
  }

  async function loadLanguage(lang) {
    const want = LANGS.includes(lang) ? lang : 'ru';
    if (!Object.keys(I18N.ru).length) I18N.ru = (await fetchLocale('ru')) || {};
    I18N.dict = want === 'ru' ? I18N.ru : ((await fetchLocale(want)) || {});
    I18N.lang = want;
    buildMaps();
    applyStaticI18n();
  }

  function currentLang() {
    const l = lsGet(LANG_KEY);
    if (LANGS.includes(l)) return l;
    const cl = state.config && state.config.settings && state.config.settings.language;
    return LANGS.includes(cl) ? cl : 'ru';
  }

  const THEME_KEY = 'aid_theme';
  const TAB_KEY = 'aid_tab';

  // ===== Состояние =====
  const state = {
    health: null,
    config: null,
    tab: 'systems',
    search: '',
    matrixStatus: '',
    matrixCategory: '',
    recTask: '',
    recPrompt: '',
    recResult: null,
    recLoading: false,
    showRules: false,
    icons: []
  };

  // ===== Утилиты =====
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // Экранирование пользовательского текста (и для атрибутов)
  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Разрешаем только http(s)-ссылки
  function safeUrl(u) {
    try {
      const url = new URL(String(u || ''), location.href);
      return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
    } catch (e) { return ''; }
  }

  function parseList(str) {
    const out = [];
    String(str || '').split(',').forEach((s) => {
      const t = s.trim();
      if (t && !out.includes(t)) out.push(t);
    });
    return out;
  }

  const joinList = (arr) => (Array.isArray(arr) ? arr.join(', ') : '');

  function fmtDate(d) {
    if (!d) return '';
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d));
    return m ? `${m[3]}.${m[2]}.${m[1]}` : String(d);
  }

  function today() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  function slugify(s) {
    const map = {
      а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y',
      к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f',
      х: 'h', ц: 'c', ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya'
    };
    const t = String(s || '').toLowerCase().split('').map((c) => (c in map ? map[c] : c)).join('');
    return t.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
  }

  // Уникальный id: из введённого, иначе из названия
  function makeId(raw, name, list, prefix) {
    let id = slugify(raw) || slugify(name) || `${prefix}-${Date.now().toString(36)}`;
    const base = id;
    let i = 2;
    while ((list || []).some((x) => x.id === id)) id = `${base}-${i++}`;
    return id;
  }

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* недоступно */ } }

  const cfg = () => state.config || { systems: [], tools: [], connections: [], rules: [], settings: {} };
  const toolById = (id) => cfg().tools.find((t) => t.id === id);
  const systemById = (id) => cfg().systems.find((s) => s.id === id);
  const toolName = (id) => (toolById(id) || {}).name || id;

  function connOf(toolId, systemId) {
    return cfg().connections.find((c) => c.toolId === toolId && c.systemId === systemId) || null;
  }
  const connStatus = (c) => (c && CONN_STATUS[c.status] ? c.status : 'unknown');

  const q = () => state.search.trim().toLowerCase();
  const has = (s, needle) => String(s || '').toLowerCase().includes(needle);

  function matchSystem(s, n) {
    if (!n) return true;
    return has(s.name, n) || (s.models || []).some((m) => has(m, n)) || (s.tasks || []).some((t) => has(t, n));
  }
  const matchTool = (t, n) => !n || has(t.name, n);
  function matchRule(r, n) {
    return !n || has(r.name, n) || (r.keywords || []).some((k) => has(k, n));
  }

  const demoBadge = (item) => (item && item.demo ? `<span class="badge badge-demo" title="${esc(t('demo.title'))}">${esc(t('demo.badge'))}</span>` : '');

  function statusPill(status, withLabel) {
    const st = CONN_STATUS[status] || CONN_STATUS.unknown;
    const key = CONN_STATUS[status] ? status : 'unknown';
    return `<span class="st st-${key}" title="${esc(st.label)}">${st.emoji}${withLabel ? ' ' + esc(st.short) : ''}</span>`;
  }

  function legendHtml() {
    return `<div class="legend" aria-label="${esc(t('legend.aria'))}">` +
      CONN_ORDER.map((k) => `<span>${CONN_STATUS[k].emoji} ${esc(CONN_STATUS[k].label)}</span>`).join('<span aria-hidden="true">·</span>') +
      '</div>';
  }

  function options(map, selected) {
    return Object.keys(map).map((k) =>
      `<option value="${esc(k)}"${k === selected ? ' selected' : ''}>${esc(map[k])}</option>`).join('');
  }

  // ===== Уведомления =====
  function toast(msg, type) {
    const box = $('#toasts');
    const el = document.createElement('div');
    el.className = 'toast' + (type === 'error' ? ' error' : '');
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    el.textContent = msg;
    box.appendChild(el);
    const ttl = type === 'error' ? 6000 : 3000;
    setTimeout(() => { el.classList.add('hide'); setTimeout(() => el.remove(), 300); }, ttl);
    el.addEventListener('click', () => el.remove());
  }
  const toastErr = (e) => toast((e && e.message) || t('error.generic'), 'error');

  // ===== API =====
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  async function api(method, path, body, opts) {
    const o = opts || {};
    const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    // Изменяющие запросы — всегда application/json (сервер строго проверяет CSRF)
    if (method !== 'GET' && method !== 'HEAD') init.headers['Content-Type'] = 'application/json; charset=utf-8';
    if (body !== undefined) init.body = JSON.stringify(body);
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      throw new ApiError(t('error.no_connection'), 0);
    }
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    if (!res.ok) {
      const msg = (data && data.error) || defaultError(res.status);
      if (res.status === 401 && !o.allow401) {
        showLogin(t('error.session_expired'));
      }
      throw new ApiError(msg, res.status);
    }
    return data;
  }

  function defaultError(code) {
    if (code === 400) return t('error.400');
    if (code === 401) return t('error.401');
    if (code === 404) return t('error.404');
    if (code === 429) return t('error.429');
    if (code >= 500) return t('error.5xx');
    return t('error.code', { code });
  }

  async function reloadConfig() {
    state.config = normalizeConfig(await api('GET', '/api/config'));
    render();
  }

  // Подстраховка от неполных данных
  function normalizeConfig(c) {
    const x = c && typeof c === 'object' ? c : {};
    ['systems', 'tools', 'connections', 'rules'].forEach((k) => { if (!Array.isArray(x[k])) x[k] = []; });
    if (!x.settings || typeof x.settings !== 'object') x.settings = { theme: 'auto', language: 'ru' };
    x.systems.forEach((s) => {
      s.status = Object.assign({ site: 'unchecked', api: 'unchecked', tools: 'unchecked' }, s.status || {});
      s.handoff = Object.assign({ type: 'none', template: '' }, s.handoff || {});
      ['access', 'models', 'tasks', 'sources'].forEach((k) => { if (!Array.isArray(s[k])) s[k] = []; });
      if (typeof s.icon !== 'string') s.icon = '';
    });
    x.rules.forEach((r) => {
      ['keywords', 'requiredTools', 'capabilities', 'preferredSystems'].forEach((k) => { if (!Array.isArray(r[k])) r[k] = []; });
    });
    return x;
  }

  // ===== Тема =====
  function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    else root.removeAttribute('data-theme');
  }
  function currentTheme() {
    const t = lsGet(THEME_KEY);
    if (t && THEMES[t]) return t;
    const ct = state.config && state.config.settings && state.config.settings.theme;
    return THEMES[ct] ? ct : 'auto';
  }

  async function setTheme(theme) {
    lsSet(THEME_KEY, theme);
    applyTheme(theme);
    // Сохраняем и в config.json, чтобы тема совпадала на других устройствах
    if (!state.config) return;
    const next = Object.assign({}, state.config, { settings: Object.assign({}, state.config.settings, { theme }) });
    try {
      const saved = await api('PUT', '/api/config', next);
      state.config = normalizeConfig(saved && saved.systems ? saved : next);
      toast(t('toast.theme_saved'));
    } catch (e) {
      if (e.status !== 401) toast(t('toast.theme_not_saved', { message: e.message }), 'error');
    }
  }

  // Язык: localStorage + settings.language в config.json
  async function setLanguage(lang) {
    if (!LANGS.includes(lang)) return;
    lsSet(LANG_KEY, lang);
    await loadLanguage(lang);
    render();
    if (!state.config) return;
    try {
      const saved = await api('PUT', '/api/settings', { language: lang });
      state.config.settings = Object.assign({}, state.config.settings, saved || { language: lang });
      toast(t('toast.language_saved'));
    } catch (e) {
      if (e.status !== 401) toast(t('toast.language_not_saved', { message: e.message }), 'error');
    }
  }

  // ===== Экраны: загрузка / вход / приложение =====
  function showScreen(name) {
    $('#boot').hidden = name !== 'boot';
    $('#login').hidden = name !== 'login';
    $('#app').hidden = name !== 'app';
  }

  function showLogin(msg) {
    closeModal();
    state.config = null;
    showScreen('login');
    const err = $('#login-error');
    if (msg) { err.textContent = msg; err.hidden = false; } else { err.hidden = true; }
    const pw = $('#login-password');
    pw.value = '';
    setTimeout(() => pw.focus(), 30);
  }

  async function boot() {
    showScreen('boot');
    $('#boot-text').textContent = t('boot.loading');
    $('#boot-retry').hidden = true;
    try {
      state.health = await api('GET', '/api/health', undefined, { allow401: true });
    } catch (e) {
      $('#boot-text').textContent = t('boot.server_down');
      $('#boot-retry').hidden = false;
      return;
    }
    try {
      state.config = normalizeConfig(await api('GET', '/api/config', undefined, { allow401: true }));
    } catch (e) {
      if (e.status === 401) { showLogin(); return; }
      $('#boot-text').textContent = t('boot.load_failed', { message: e.message });
      $('#boot-retry').hidden = false;
      return;
    }
    applyTheme(currentTheme());
    // Язык из config.json, если в этом браузере он ещё не выбран
    if (currentLang() !== I18N.lang) await loadLanguage(currentLang());
    loadIcons();
    showScreen('app');
    render();
  }

  // Список доступных иконок (для формы системы)
  async function loadIcons() {
    try {
      const list = await api('GET', '/api/icons', undefined, { allow401: true });
      state.icons = Array.isArray(list) ? list.filter((f) => /^[a-z0-9_-]+\.svg$/.test(f)) : [];
    } catch (e) { state.icons = []; }
  }

  async function onLogin(ev) {
    ev.preventDefault();
    const pw = $('#login-password').value;
    const err = $('#login-error');
    const btn = $('#login-submit');
    err.hidden = true;
    if (!pw) { err.textContent = t('login.enter_password'); err.hidden = false; return; }
    btn.disabled = true;
    btn.textContent = t('buttons.login_progress');
    try {
      await api('POST', '/api/login', { password: pw }, { allow401: true });
      $('#login-password').value = '';
      await boot();
    } catch (e) {
      err.textContent = e.status === 401 ? t('login.wrong_password') : e.message;
      err.hidden = false;
      $('#login-password').select();
    } finally {
      btn.disabled = false;
      btn.textContent = t('buttons.login');
    }
  }

  async function logout() {
    try { await api('POST', '/api/logout', undefined, { allow401: true }); } catch (e) { /* всё равно выходим */ }
    if (state.health && state.health.authRequired) {
      showLogin();
      toast(t('toast.logged_out'));
    } else {
      toast(t('toast.auth_disabled'));
    }
  }

  // ===== Навигация =====
  function setTab(tab) {
    if (!TABS.includes(tab)) tab = 'systems';
    state.tab = tab;
    lsSet(TAB_KEY, tab);
    if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
    render();
  }

  function render() {
    if (!state.config) return;
    $$('.tab').forEach((b) => {
      const on = b.dataset.tab === state.tab;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    });
    TABS.forEach((t) => { $('#view-' + t).hidden = t !== state.tab; });
    const view = $('#view-' + state.tab);
    if (state.tab === 'systems') view.innerHTML = renderSystems();
    else if (state.tab === 'matrix') view.innerHTML = renderMatrix();
    else if (state.tab === 'recommend') view.innerHTML = renderRecommend();
    else view.innerHTML = renderSettings();
  }

  // ===== Вкладка «Системы» =====
  function indicator(label, map, value) {
    const v = map[value] || map.unchecked;
    return `<div class="ind ${v[1]}"><b>${esc(label)}</b><span title="${esc(v[0])}">${esc(v[0])}</span></div>`;
  }

  // Иконка системы: только /icons/<имя>.svg
  function iconHtml(icon, cls) {
    const ok = typeof icon === 'string' && /^\/icons\/[a-z0-9_-]+\.svg$/.test(icon);
    return ok ? `<img class="${cls || 'sys-icon'}" src="${esc(icon)}" alt="" width="28" height="28" aria-hidden="true">` : '';
  }

  // Источники: только http(s), открываются в новой вкладке без доступа к opener
  function sourcesHtml(list) {
    const links = (list || []).map(safeUrl).filter((u) => /^https?:/i.test(u));
    if (!links.length) return '';
    const items = links.map((u) => `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></li>`).join('');
    return `<details class="sources"><summary>${esc(t('systems.sources', { count: links.length }))}</summary><ul>${items}</ul></details>`;
  }

  function systemCard(s) {
    const url = safeUrl(s.url);
    const connected = cfg().connections
      .filter((c) => c.systemId === s.id && (c.status === 'verified' || c.status === 'manual'))
      .map((c) => `<span class="chip">${CONN_STATUS[c.status].emoji} ${esc(toolName(c.toolId))}</span>`);
    const access = (s.access || []).map((a) => `<span class="chip chip-accent">${esc(ACCESS[a] || a)}</span>`).join('');
    const models = (s.models || []).map((m) => `<span class="chip">${esc(m)}</span>`).join('');
    const tasks = (s.tasks || []).map((x) => `<li>${esc(x)}</li>`).join('');

    return `
    <article class="card sys-card" data-system="${esc(s.id)}">
      <div class="sys-head">
        <div style="min-width:0">
          <div class="sys-title">${iconHtml(s.icon)}<h3>${esc(s.name || s.id)}</h3>${demoBadge(s)}</div>
          ${url ? `<div class="sys-url">${esc(url)}</div>` : ''}
        </div>
        ${url ? `<button type="button" class="btn btn-primary btn-sm" data-action="open-url" data-url="${esc(url)}">${esc(t('buttons.open'))}</button>` : ''}
      </div>
      ${access ? `<div class="chips">${access}</div>` : ''}
      ${models ? `<div><div class="sec-label">${esc(t('systems.models'))}</div><div class="chips">${models}</div></div>` : ''}
      ${tasks ? `<div><div class="sec-label">${esc(t('systems.tasks'))}</div><ul class="task-list">${tasks}</ul></div>` : ''}
      <div><div class="sec-label">${esc(t('systems.connected_tools'))}</div>
        ${connected.length ? `<div class="chips">${connected.join('')}</div>` : `<div class="sec-text muted">${esc(t('systems.no_verified_connections'))}</div>`}
      </div>
      ${s.limits ? `<div><div class="sec-label">${esc(t('systems.limits'))}</div><div class="sec-text">${esc(s.limits)}</div></div>` : ''}
      <dl class="kv">
        <dt>${esc(t('systems.subscription'))}</dt><dd>${s.subscription ? esc(s.subscription) : `<span class="muted">${esc(t('systems.not_specified'))}</span>`}</dd>
        <dt>${esc(t('systems.api'))}</dt><dd>${esc(API_ACCESS[s.apiAccess] || API_ACCESS.unknown)}</dd>
        <dt>${esc(t('systems.handoff'))}</dt><dd>${esc(HANDOFF[(s.handoff || {}).type] || HANDOFF.none)}</dd>
      </dl>
      <div class="indicators">
        ${indicator(t('systems.indicator_site'), SITE_ST, s.status.site)}
        ${indicator(t('systems.indicator_api'), API_ST, s.status.api)}
        ${indicator(t('systems.indicator_tools'), TOOLS_ST, s.status.tools)}
      </div>
      <div class="small muted">${esc(t('systems.checked', { date: s.checkedAt ? fmtDate(s.checkedAt) : t('systems.not_checked') }))}</div>
      ${s.notes ? `<div class="sec-text small">${esc(s.notes)}</div>` : ''}
      ${sourcesHtml(s.sources)}
      <div class="card-actions">
        <button type="button" class="btn btn-sm" data-action="edit-system" data-id="${esc(s.id)}">${esc(t('buttons.edit'))}</button>
        <button type="button" class="btn btn-sm" data-action="check-system" data-id="${esc(s.id)}"${url ? '' : ` disabled title="${esc(t('buttons.no_link_title'))}"`}>${esc(t('buttons.check_site'))}</button>
        <button type="button" class="btn btn-sm btn-danger" data-action="delete-system" data-id="${esc(s.id)}">${esc(t('buttons.delete'))}</button>
      </div>
    </article>`;
  }

  function renderSystems() {
    const n = q();
    const list = cfg().systems.filter((s) => matchSystem(s, n));
    const total = cfg().systems.length;
    return `
      <div class="view-head">
        <div><h2>${esc(t('tabs.systems'))}</h2><div class="sub">${esc(n ? t('systems.found', { count: list.length, total }) : t('systems.total', { total }))}</div></div>
        <button type="button" class="btn btn-primary" data-action="new-system">${esc(t('buttons.add_system'))}</button>
      </div>
      ${list.length ? `<div class="grid">${list.map(systemCard).join('')}</div>`
        : `<div class="empty">${esc(n ? t('systems.empty_search') : t('systems.empty'))}</div>`}
      ${legendHtml()}`;
  }

  // ===== Вкладка «Матрица» =====
  function renderMatrix() {
    const n = q();
    const c = cfg();
    // Поиск: если запрос совпадает с инструментами — фильтруем строки; с системами — столбцы
    let tools = c.tools.slice();
    if (n && tools.some((x) => matchTool(x, n))) tools = tools.filter((x) => matchTool(x, n));
    let systems = c.systems.slice();
    if (n && systems.some((s) => matchSystem(s, n))) systems = systems.filter((s) => matchSystem(s, n));

    if (state.matrixCategory) tools = tools.filter((x) => (x.category || 'other') === state.matrixCategory);
    if (state.matrixStatus) {
      tools = tools.filter((x) => systems.some((s) => connStatus(connOf(x.id, s.id)) === state.matrixStatus));
    }

    const catOpts = { '': t('matrix.all_categories') };
    Object.keys(CATEGORY).forEach((k) => { catOpts[k] = CATEGORY[k]; });
    const stOpts = { '': t('matrix.all_statuses') };
    CONN_ORDER.forEach((k) => { stOpts[k] = `${CONN_STATUS[k].emoji} ${CONN_STATUS[k].label}`; });

    let table;
    if (!c.systems.length || !c.tools.length) {
      table = `<div class="empty">${esc((!c.tools.length ? t('matrix.add_tools') : t('matrix.add_systems')) + t('matrix.fill_suffix'))}</div>`;
    } else if (!tools.length) {
      table = `<div class="empty">${esc(t('matrix.no_tools_for_filters'))}</div>`;
    } else {
      const head = systems.map((s) => `<th scope="col">${iconHtml(s.icon, 'sys-icon sys-icon-sm')}${esc(s.name || s.id)} ${demoBadge(s)}</th>`).join('');
      const rows = tools.map((tl) => {
        const cells = systems.map((s) => {
          const conn = connOf(tl.id, s.id);
          const st = connStatus(conn);
          const dim = state.matrixStatus && st !== state.matrixStatus ? ' style="opacity:.35"' : '';
          const sub = conn && conn.kind ? `<small>${esc(CONN_KIND[conn.kind] || conn.kind)}</small>` : '';
          return `<td><button type="button" class="cell-btn" data-action="edit-conn" data-tool="${esc(tl.id)}" data-system="${esc(s.id)}"
            aria-label="${esc(tl.name)} × ${esc(s.name)}: ${esc(CONN_STATUS[st].label)}"${dim}>
            ${statusPill(st, true)}${sub}${conn && conn.demo ? demoBadge(conn) : ''}</button></td>`;
        }).join('');
        return `<tr><th scope="row" class="col-tool"><div class="tool-cell">
            <div class="tool-name"><b>${esc(tl.name || tl.id)}</b><small>${esc(CATEGORY[tl.category] || CATEGORY.other)}</small> ${demoBadge(tl)}</div>
            <button type="button" class="btn btn-icon btn-ghost" data-action="edit-tool" data-id="${esc(tl.id)}" aria-label="${esc(t('matrix.edit_tool_aria', { name: tl.name }))}" title="${esc(t('matrix.edit_title'))}">✎</button>
          </div></th>${cells}</tr>`;
      }).join('');
      table = `<div class="matrix-wrap"><table class="matrix">
        <thead><tr><th scope="col" class="col-tool">${esc(t('matrix.tool_column'))}</th>${head}</tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    }

    return `
      <div class="view-head">
        <div><h2>${esc(t('matrix.title'))}</h2><div class="sub">${esc(t('matrix.subtitle'))}</div></div>
        <button type="button" class="btn btn-primary" data-action="new-tool">${esc(t('buttons.add_tool'))}</button>
      </div>
      <div class="filters">
        <label class="field"><span>${esc(t('matrix.filter_status'))}</span><select id="f-status">${options(stOpts, state.matrixStatus)}</select></label>
        <label class="field"><span>${esc(t('matrix.filter_category'))}</span><select id="f-category">${options(catOpts, state.matrixCategory)}</select></label>
      </div>
      ${table}
      ${legendHtml()}`;
  }

  // ===== Вкладка «Кому дать задачу» =====
  function candidateCard(cand, kind, capabilities) {
    if (!cand) return '';
    const best = kind === 'best';
    const have = (cand.haveTools || []).map((h) =>
      `<span class="chip">${(CONN_STATUS[h.status] || CONN_STATUS.unknown).emoji} ${esc(toolName(h.toolId))}</span>`).join('');
    const missing = (cand.missingTools || []).map((id) => `<span class="chip">⛔ ${esc(toolName(id))}</span>`).join('');
    const reasons = (cand.reasons || []).map((r) => `<li>${esc(r)}</li>`).join('');
    const caps = (capabilities || []).map((x) => `<span class="chip">${esc(x)}</span>`).join('');
    const url = safeUrl(cand.url);
    const score = typeof cand.score === 'number' ? (Math.round(cand.score * 10) / 10) : cand.score;
    return `
      <article class="card ${best ? 'rec-best' : 'rec-alt'}">
        <div class="rec-head">
          <div>
            <div class="rec-kicker">${esc(best ? t('recommend.best') : t('recommend.alternative'))}</div>
            <h3 class="rec-name">${iconHtml((systemById(cand.systemId) || {}).icon)}<span>${esc(cand.name || cand.systemId)}</span></h3>
          </div>
          <div class="btn-row" style="align-items:center">
            ${score != null ? `<span class="score">${esc(t('recommend.score', { score }))}</span>` : ''}
            ${!best && url ? `<button type="button" class="btn btn-sm" data-action="open-url" data-url="${esc(url)}">${esc(t('buttons.open'))}</button>` : ''}
          </div>
        </div>
        <div class="rec-points">
          ${best && caps ? `<div><div class="sec-label">${esc(t('recommend.capabilities'))}</div><div class="chips">${caps}</div></div>` : ''}
          <div><div class="sec-label">${esc(t('recommend.have_access'))}</div>${have ? `<div class="chips">${have}</div>` : `<div class="sec-text muted">${esc(t('recommend.no_verified_tools'))}</div>`}</div>
          ${missing ? `<div><div class="sec-label">${esc(t('recommend.missing'))}</div><div class="chips">${missing}</div></div>` : ''}
          ${cand.limits ? `<div><div class="sec-label">${esc(t('recommend.limits'))}</div><div class="sec-text">${esc(cand.limits)}</div></div>` : ''}
          ${reasons ? `<div><div class="sec-label">${esc(t('recommend.why'))}</div><ul class="reasons">${reasons}</ul></div>` : ''}
        </div>
      </article>`;
  }

  function renderRecResult() {
    const r = state.recResult;
    if (!r) return '';
    const parts = [];
    if (r.matchedRule) {
      // Имя правила — жирным: собираем вокруг экранированного плейсхолдера
      const label = esc(t('recommend.matched_rule', { name: '\u0000' })).split('\u0000');
      parts.push(`<div class="small muted" id="rec-rule" data-rule="${esc(r.matchedRule.id)}">${label[0]}<b>${esc(r.matchedRule.name || r.matchedRule.id)}</b>${label.slice(1).join('')}</div>`);
    } else {
      parts.push(`<div class="notice">${esc(t('recommend.no_rule'))}</div>`);
    }
    if (r.best) parts.push(candidateCard(r.best, 'best', r.capabilities));
    else parts.push(`<div class="empty">${esc(t('recommend.no_systems'))}</div>`);
    if (r.alternative) parts.push(candidateCard(r.alternative, 'alt'));

    const bestUrl = r.best ? safeUrl(r.best.url) : '';
    const canHandoff = !!handoffUrl();
    parts.push(`
      <div class="card prompt-box">
        <label class="field"><span>${esc(t('recommend.prompt_label'))}</span>
          <textarea id="rec-prompt" rows="5">${esc(state.recPrompt)}</textarea>
        </label>
        <div class="btn-row" style="margin-top:10px">
          <button type="button" class="btn" data-action="rec-open"${bestUrl ? '' : ' disabled'}>${esc(t('buttons.open_system'))}</button>
          <button type="button" class="btn" data-action="rec-copy">${esc(t('buttons.copy_prompt'))}</button>
          <button type="button" class="btn btn-primary" data-action="rec-handoff"${canHandoff ? '' : ` disabled title="${esc(t('recommend.handoff_not_configured_title'))}"`}>${esc(t('buttons.handoff'))}</button>
        </div>
      </div>`);
    return `<div class="rec-result">${parts.join('')}</div>`;
  }

  // Ссылка передачи: пересобираем по шаблону, если промпт отредактирован
  function handoffUrl() {
    const r = state.recResult;
    if (!r || !r.handoff || !r.handoff.url) return '';
    const sys = r.best && systemById(r.best.systemId);
    const tpl = sys && sys.handoff && sys.handoff.type === 'url' ? sys.handoff.template : '';
    if (tpl && tpl.includes('{prompt}') && state.recPrompt !== r.prompt) {
      return safeUrl(tpl.split('{prompt}').join(encodeURIComponent(state.recPrompt)));
    }
    return safeUrl(r.handoff.url);
  }

  function ruleRow(r) {
    const tools = (r.requiredTools || []).map((id) => `<span class="chip">${esc(toolName(id))}</span>`).join('');
    const kw = (r.keywords || []).map((k) => `<span class="chip">${esc(k)}</span>`).join('');
    const pref = (r.preferredSystems || []).map((id) => esc((systemById(id) || {}).name || id)).join(' → ');
    return `
      <article class="card rule-row">
        <div class="rule-main">
          <h4>${esc(r.name || r.id)} ${demoBadge(r)} <span class="badge badge-muted">${esc(t('rules.priority', { priority: r.priority != null ? r.priority : 0 }))}</span></h4>
          ${kw ? `<div class="chips">${kw}</div>` : `<div class="rule-meta">${esc(t('rules.no_keywords'))}</div>`}
          ${tools ? `<div class="rule-meta">${esc(t('rules.required_tools'))}</div><div class="chips">${tools}</div>` : ''}
          ${pref ? `<div class="rule-meta">${esc(t('rules.preference', { systems: '' }))}${pref}</div>` : ''}
        </div>
        <div class="btn-row">
          <button type="button" class="btn btn-sm" data-action="edit-rule" data-id="${esc(r.id)}">${esc(t('buttons.change'))}</button>
          <button type="button" class="btn btn-sm btn-danger" data-action="delete-rule" data-id="${esc(r.id)}">${esc(t('buttons.delete'))}</button>
        </div>
      </article>`;
  }

  function renderRecommend() {
    const n = q();
    const rules = cfg().rules.slice()
      .filter((r) => matchRule(r, n))
      .sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0));
    const rulesBlock = state.showRules ? `
      <section style="margin-top:28px">
        <div class="view-head">
          <div><h2>${esc(t('rules.title'))}</h2><div class="sub">${esc(t('rules.subtitle'))}</div></div>
          <button type="button" class="btn btn-primary" data-action="new-rule">${esc(t('buttons.add_rule'))}</button>
        </div>
        ${rules.length ? `<div class="rules-list">${rules.map(ruleRow).join('')}</div>`
          : `<div class="empty">${esc(n ? t('rules.empty_search') : t('rules.empty'))}</div>`}
      </section>` : '';

    return `
      <div class="view-head">
        <div><h2>${esc(t('recommend.title'))}</h2><div class="sub">${esc(t('recommend.subtitle'))}</div></div>
      </div>
      <form class="rec-form card" id="rec-form">
        <label class="field"><span>${esc(t('recommend.task_label'))}</span>
          <textarea id="rec-task" rows="4" placeholder="${esc(t('recommend.task_placeholder'))}">${esc(state.recTask)}</textarea>
        </label>
        <div class="btn-row">
          <button type="button" class="btn btn-ghost" data-action="toggle-rules" aria-expanded="${state.showRules}">
            ${esc(state.showRules ? t('buttons.hide_rules') : t('buttons.show_rules'))}</button>
          <button type="submit" class="btn btn-primary" id="rec-submit"${state.recLoading ? ' disabled' : ''}>${esc(state.recLoading ? t('buttons.recommend_progress') : t('buttons.recommend'))}</button>
        </div>
      </form>
      ${renderRecResult()}
      ${rulesBlock}`;
  }

  async function doRecommend() {
    const task = state.recTask.trim();
    if (!task) { toast(t('toast.describe_task'), 'error'); $('#rec-task') && $('#rec-task').focus(); return; }
    state.recLoading = true;
    render();
    try {
      const r = await api('POST', '/api/recommend', { task });
      state.recResult = r || null;
      state.recPrompt = (r && r.prompt) || task;
    } catch (e) {
      toastErr(e);
    } finally {
      state.recLoading = false;
      render();
    }
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* переходим к запасному способу */ }
    // Запасной способ: выделение + execCommand
    const ta = $('#rec-prompt');
    let tmp = null;
    let el = ta;
    if (!el) {
      tmp = document.createElement('textarea');
      tmp.value = text;
      tmp.style.position = 'fixed';
      tmp.style.opacity = '0';
      document.body.appendChild(tmp);
      el = tmp;
    }
    el.focus();
    el.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    if (tmp) tmp.remove();
    return ok;
  }

  // ===== Вкладка «Настройки» =====
  function renderSettings() {
    const h = state.health || {};
    const theme = currentTheme();
    const c = cfg();
    const locale = I18N.lang === 'de' ? 'de-DE' : 'ru-RU';
    const readme = esc(t('settings.readme_hint', { file: '\u0000' })).split('\u0000').join('<code>README.md</code>');
    return `
      <div class="view-head"><div><h2>${esc(t('settings.title'))}</h2></div></div>
      <div class="settings">
        <section class="card">
          <h3>${esc(t('settings.language_title'))}</h3>
          <div class="radio-row" role="radiogroup" aria-label="${esc(t('settings.language_aria'))}">
            ${LANGS.map((k) => `<label class="check"><input type="radio" name="language" value="${k}"${k === I18N.lang ? ' checked' : ''}> ${esc(t('lang.' + k))}</label>`).join('')}
          </div>
          ${I18N.lang !== 'ru' ? `<p class="muted small" style="margin-top:8px">${esc(t('settings.server_text_note'))}</p>` : ''}
        </section>
        <section class="card">
          <h3>${esc(t('settings.theme_title'))}</h3>
          <div class="radio-row" role="radiogroup" aria-label="${esc(t('settings.theme_aria'))}">
            ${Object.keys(THEMES).map((k) => `<label class="check"><input type="radio" name="theme" value="${k}"${k === theme ? ' checked' : ''}> ${esc(THEMES[k])}</label>`).join('')}
          </div>
        </section>
        <section class="card">
          <h3>${esc(t('settings.export_title'))}</h3>
          <p class="muted small">${esc(t('settings.export_hint'))}</p>
          <div class="btn-row"><a class="btn" href="/api/export" download="config.json">${esc(t('buttons.download_json'))}</a></div>
        </section>
        <section class="card">
          <h3>${esc(t('settings.import_title'))}</h3>
          <form id="import-form" class="form-grid">
            <label class="field"><span>${esc(t('settings.import_file'))}</span><input type="file" id="import-file" accept="application/json,.json"></label>
            <div class="radio-row" role="radiogroup" aria-label="${esc(t('settings.import_mode_aria'))}">
              <label class="check"><input type="radio" name="import-mode" value="replace" checked> ${esc(t('settings.import_replace'))}</label>
              <label class="check"><input type="radio" name="import-mode" value="merge"> ${esc(t('settings.import_merge'))}</label>
            </div>
            <div class="btn-row"><button type="submit" class="btn btn-primary">${esc(t('buttons.import'))}</button></div>
          </form>
        </section>
        <section class="card">
          <h3>${esc(t('settings.session_title'))}</h3>
          <p class="muted small">${esc(h.authRequired ? t('settings.auth_on') : t('settings.auth_off'))}</p>
          <div class="btn-row"><button type="button" class="btn btn-danger" data-action="logout"${h.authRequired ? '' : ' disabled'}>${esc(t('buttons.logout'))}</button></div>
        </section>
        <section class="card">
          <h3>${esc(t('settings.about_title'))}</h3>
          <dl class="kv">
            <dt>${esc(t('settings.version'))}</dt><dd>${esc(h.version != null ? h.version : '—')}</dd>
            <dt>${esc(t('settings.data_updated'))}</dt><dd>${c.updatedAt ? esc(new Date(c.updatedAt).toLocaleString(locale)) : '—'}</dd>
            <dt>${esc(t('settings.records'))}</dt><dd>${esc(t('settings.records_value', { systems: c.systems.length, tools: c.tools.length, connections: c.connections.length, rules: c.rules.length }))}</dd>
          </dl>
          <p class="muted small" style="margin-top:10px">${readme}</p>
        </section>
      </div>`;
  }

  async function doImport(ev) {
    ev.preventDefault();
    const file = $('#import-file').files[0];
    if (!file) { toast(t('toast.choose_file'), 'error'); return; }
    const mode = ($('input[name="import-mode"]:checked') || {}).value || 'replace';
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch (e) {
      toast(t('toast.invalid_json'), 'error');
      return;
    }
    if (mode === 'replace' && !confirm(t('confirm.replace_all'))) return;
    try {
      await api('POST', mode === 'merge' ? '/api/import?merge=1' : '/api/import', data);
      toast(mode === 'merge' ? t('toast.merged') : t('toast.replaced'));
      await reloadConfig();
    } catch (e) { toastErr(e); }
  }

  // ===== Модальное окно =====
  let modalCtx = null;

  function openModal(o) {
    modalCtx = o;
    $('#modal-title').textContent = o.title;
    $('#modal-body').innerHTML = o.body;
    $('#modal-submit').textContent = o.submitLabel || t('buttons.save');
    $('#modal-error').hidden = true;
    // Кнопка удаления (по желанию)
    const foot = $('.modal-foot');
    const old = $('#modal-delete');
    if (old) old.remove();
    if (o.onDelete) {
      const del = document.createElement('button');
      del.type = 'button';
      del.id = 'modal-delete';
      del.className = 'btn btn-danger';
      del.style.marginRight = 'auto';
      del.textContent = o.deleteLabel || t('buttons.delete');
      del.addEventListener('click', async () => {
        try {
          if ((await o.onDelete()) !== false) closeModal();
        } catch (e) { showModalError(e); }
      });
      foot.insertBefore(del, foot.firstChild);
    }
    $('#modal').hidden = false;
    document.body.classList.add('modal-open');
    if (o.onOpen) o.onOpen($('#modal-form'));
    const first = $('#modal-body input:not([type=hidden]):not([readonly]), #modal-body select, #modal-body textarea');
    if (first) setTimeout(() => first.focus(), 30);
  }

  function closeModal() {
    $('#modal').hidden = true;
    document.body.classList.remove('modal-open');
    $('#modal-body').innerHTML = '';
    modalCtx = null;
  }

  function showModalError(e) {
    const el = $('#modal-error');
    el.textContent = (e && e.message) || String(e);
    el.hidden = false;
  }

  async function onModalSubmit(ev) {
    ev.preventDefault();
    if (!modalCtx) return;
    const btn = $('#modal-submit');
    $('#modal-error').hidden = true;
    btn.disabled = true;
    try {
      await modalCtx.onSubmit($('#modal-form'));
      closeModal();
    } catch (e) {
      if (e && e.status === 401) return;
      showModalError(e);
    } finally {
      btn.disabled = false;
    }
  }

  // Чтение полей формы
  const fv = (form, name) => {
    const el = form.elements[name];
    return el ? String(el.value).trim() : '';
  };
  const fchecked = (form, name) => $$(`input[name="${name}"]:checked`, form).map((i) => i.value);

  // ===== Редактирование системы =====
  function systemForm(s) {
    const isNew = !s;
    const x = s || { access: [], models: [], tasks: [], sources: [], icon: '', status: {}, handoff: { type: 'none', template: '' }, apiAccess: 'unknown' };
    const st = x.status || {};
    const ho = x.handoff || {};
    const siteO = { unchecked: t('form.not_checked'), ok: t('form.works'), down: t('form.down') };
    const apiO = { unchecked: t('form.not_checked'), ok: t('form.works'), down: t('form.api_down'), 'n/a': t('form.na') };
    const toolsO = { unchecked: t('form.not_checked'), ok: t('form.tools_ok'), partial: t('form.partial') };
    // Иконки: файлы из /api/icons + текущая (если её файла уже нет)
    const iconO = { '': t('form.icon_none') };
    (state.icons || []).forEach((f) => { iconO['/icons/' + f] = f.replace(/\.svg$/, ''); });
    if (x.icon && !iconO[x.icon]) iconO[x.icon] = x.icon;
    return `
      <div class="form-grid two">
        <label class="field"><span>${esc(t('form.name'))}</span><input type="text" name="name" value="${esc(x.name)}" required maxlength="80"></label>
        <label class="field"><span>${esc(t('form.id'))}</span>
          <input type="text" name="id" value="${esc(x.id)}"${isNew ? ` placeholder="${esc(t('form.id_placeholder'))}"` : ' readonly'} maxlength="40">
          <span class="hint">${esc(isNew ? t('form.id_hint_new') : t('form.id_hint_fixed'))}</span></label>
        <label class="field span-2"><span>${esc(t('form.link'))}</span><input type="url" name="url" value="${esc(x.url)}" placeholder="https://…"></label>
        <label class="field span-2"><span>${esc(t('form.icon'))}</span>
          <span class="icon-pick"><span id="icon-preview">${iconHtml(x.icon)}</span><select name="icon">${options(iconO, x.icon || '')}</select></span></label>
        <fieldset class="span-2"><legend>${esc(t('form.access_methods'))}</legend><div class="check-list">
          ${Object.keys(ACCESS).map((k) => `<label class="check"><input type="checkbox" name="access" value="${k}"${(x.access || []).includes(k) ? ' checked' : ''}> ${esc(ACCESS[k])}</label>`).join('')}
        </div></fieldset>
        <label class="field span-2"><span>${esc(t('form.models'))}</span><input type="text" name="models" value="${esc(joinList(x.models))}"><span class="hint">${esc(t('form.comma_hint'))}</span></label>
        <label class="field span-2"><span>${esc(t('form.main_tasks'))}</span><input type="text" name="tasks" value="${esc(joinList(x.tasks))}"><span class="hint">${esc(t('form.comma_hint'))}</span></label>
        <label class="field"><span>${esc(t('form.subscription'))}</span><input type="text" name="subscription" value="${esc(x.subscription)}" placeholder="${esc(t('form.subscription_placeholder'))}"></label>
        <label class="field"><span>${esc(t('form.api_access'))}</span><select name="apiAccess">${options({ unknown: t('form.unknown'), yes: t('form.yes'), no: t('form.no') }, x.apiAccess || 'unknown')}</select></label>
        <label class="field span-2"><span>${esc(t('form.limits'))}</span><textarea name="limits" rows="2">${esc(x.limits)}</textarea></label>
        <fieldset class="span-2"><legend>${esc(t('form.status'))}</legend><div class="form-grid two">
          <label class="field"><span>${esc(t('form.site'))}</span><select name="st_site">${options(siteO, st.site || 'unchecked')}</select></label>
          <label class="field"><span>API</span><select name="st_api">${options(apiO, st.api || 'unchecked')}</select></label>
          <label class="field"><span>${esc(t('form.tools'))}</span><select name="st_tools">${options(toolsO, st.tools || 'unchecked')}</select></label>
          <label class="field"><span>${esc(t('form.checked_date'))}</span><input type="date" name="checkedAt" value="${esc(x.checkedAt || '')}"></label>
        </div></fieldset>
        <fieldset class="span-2"><legend>${esc(t('form.handoff'))}</legend><div class="form-grid two">
          <label class="field"><span>${esc(t('form.method'))}</span><select name="ho_type">${options(HANDOFF, ho.type || 'none')}</select></label>
          <label class="field"><span>${esc(t('form.url_template'))}</span><input type="text" name="ho_template" value="${esc(ho.template || '')}" placeholder="https://…?q={prompt}">
            <span class="hint">${esc(t('form.url_template_hint'))}</span></label>
        </div></fieldset>
        <label class="field span-2"><span>${esc(t('form.notes'))}</span><textarea name="notes" rows="2">${esc(x.notes)}</textarea></label>
        <label class="field span-2"><span>${esc(t('form.sources'))}</span><textarea name="sources" rows="3" placeholder="https://…">${esc((x.sources || []).join('\n'))}</textarea>
          <span class="hint">${esc(t('form.sources_hint'))}</span></label>
        <label class="check span-2"><input type="checkbox" name="demo"${x.demo ? ' checked' : ''}> ${esc(t('demo.checkbox'))}</label>
      </div>`;
  }

  // Источники из textarea: по одной ссылке на строку, только http(s)
  function parseSources(text) {
    const out = [];
    String(text || '').split(/\r?\n/).forEach((line) => {
      const u = line.trim();
      if (!u) return;
      if (!/^https?:\/\//i.test(u) || !safeUrl(u)) throw new Error(t('error.sources_invalid', { url: u.slice(0, 80) }));
      if (!out.includes(u)) out.push(u);
    });
    if (out.length > 20) throw new Error(t('error.sources_too_many'));
    return out;
  }

  function editSystem(id) {
    const s = id ? systemById(id) : null;
    if (id && !s) { toast(t('toast.system_not_found'), 'error'); return; }
    openModal({
      title: s ? t('form.system_title', { name: s.name || s.id }) : t('form.system_new'),
      body: systemForm(s),
      onOpen: (f) => {
        f.elements.icon.addEventListener('change', () => { $('#icon-preview').innerHTML = iconHtml(f.elements.icon.value); });
      },
      onSubmit: async (f) => {
        const name = fv(f, 'name');
        if (!name) throw new Error(t('error.name_required'));
        const url = fv(f, 'url');
        if (url && !safeUrl(url)) throw new Error(t('error.url_scheme'));
        const hoType = fv(f, 'ho_type');
        const hoTpl = fv(f, 'ho_template');
        if (hoType === 'url' && !hoTpl.includes('{prompt}')) throw new Error(t('error.handoff_template'));
        const data = Object.assign({}, s || {}, {
          id: s ? s.id : makeId(fv(f, 'id'), name, cfg().systems, 'sys'),
          name,
          url,
          access: fchecked(f, 'access'),
          models: parseList(fv(f, 'models')),
          tasks: parseList(fv(f, 'tasks')),
          subscription: fv(f, 'subscription'),
          apiAccess: fv(f, 'apiAccess') || 'unknown',
          limits: fv(f, 'limits'),
          status: { site: fv(f, 'st_site'), api: fv(f, 'st_api'), tools: fv(f, 'st_tools') },
          checkedAt: fv(f, 'checkedAt') || null,
          handoff: { type: hoType || 'none', template: hoTpl },
          notes: fv(f, 'notes'),
          sources: parseSources(f.elements.sources.value),
          icon: fv(f, 'icon'),
          demo: f.elements.demo.checked
        });
        if (s) await api('PUT', '/api/systems/' + encodeURIComponent(s.id), data);
        else await api('POST', '/api/systems', data);
        toast(s ? t('toast.system_saved') : t('toast.system_added'));
        await reloadConfig();
      }
    });
  }

  async function deleteSystem(id) {
    const s = systemById(id);
    if (!s) return;
    if (!confirm(t('confirm.delete_system', { name: s.name || s.id }))) return;
    try {
      await api('DELETE', '/api/systems/' + encodeURIComponent(id));
      toast(t('toast.system_deleted'));
      await reloadConfig();
    } catch (e) { toastErr(e); }
  }

  async function checkSystem(id, btn) {
    if (btn) { btn.disabled = true; btn.textContent = t('buttons.check_progress'); }
    try {
      await api('POST', '/api/check/' + encodeURIComponent(id));
      await reloadConfig();
      const s = systemById(id);
      const st = s && s.status && s.status.site;
      if (st === 'ok') toast(t('toast.site_ok', { name: s.name }));
      else if (st === 'down') toast(t('toast.site_down', { name: s.name }), 'error');
      else toast(t('toast.check_done'));
    } catch (e) {
      toastErr(e);
      if (btn && document.body.contains(btn)) { btn.disabled = false; btn.textContent = t('buttons.check_site'); }
    }
  }

  // ===== Редактирование инструмента =====
  function editTool(id) {
    const tl = id ? toolById(id) : null;
    if (id && !tl) { toast(t('toast.tool_not_found'), 'error'); return; }
    const x = tl || { category: 'other' };
    openModal({
      title: tl ? t('form.tool_title', { name: tl.name || tl.id }) : t('form.tool_new'),
      body: `
        <div class="form-grid two">
          <label class="field"><span>${esc(t('form.name'))}</span><input type="text" name="name" value="${esc(x.name)}" required maxlength="80"></label>
          <label class="field"><span>${esc(t('form.id'))}</span><input type="text" name="id" value="${esc(x.id)}"${tl ? ' readonly' : ` placeholder="${esc(t('form.id_placeholder'))}"`} maxlength="40"></label>
          <label class="field span-2"><span>${esc(t('form.category'))}</span><select name="category">${options(CATEGORY, x.category || 'other')}</select></label>
          <label class="check span-2"><input type="checkbox" name="demo"${x.demo ? ' checked' : ''}> ${esc(t('demo.checkbox'))}</label>
        </div>`,
      onDelete: tl ? async () => {
        if (!confirm(t('confirm.delete_tool', { name: tl.name || tl.id }))) return false;
        await api('DELETE', '/api/tools/' + encodeURIComponent(tl.id));
        toast(t('toast.tool_deleted'));
        await reloadConfig();
        return true;
      } : null,
      onSubmit: async (f) => {
        const name = fv(f, 'name');
        if (!name) throw new Error(t('error.name_required'));
        const data = Object.assign({}, tl || {}, {
          id: tl ? tl.id : makeId(fv(f, 'id'), name, cfg().tools, 'tool'),
          name,
          category: fv(f, 'category') || 'other',
          demo: f.elements.demo.checked
        });
        if (tl) await api('PUT', '/api/tools/' + encodeURIComponent(tl.id), data);
        else await api('POST', '/api/tools', data);
        toast(tl ? t('toast.tool_saved') : t('toast.tool_added'));
        await reloadConfig();
      }
    });
  }

  // ===== Редактирование связи (ячейка матрицы) =====
  function editConnection(toolId, systemId) {
    const tl = toolById(toolId);
    const s = systemById(systemId);
    if (!tl || !s) return;
    const c = connOf(toolId, systemId) || { status: 'unknown', kind: '', method: '', actions: [], source: '', checkedAt: null, demo: false };
    const stOpts = {};
    CONN_ORDER.forEach((k) => { stOpts[k] = `${CONN_STATUS[k].emoji} ${CONN_STATUS[k].label}`; });
    openModal({
      title: `${tl.name} × ${s.name}`,
      body: `
        <div class="form-grid two">
          <label class="field"><span>${esc(t('conn.status'))}</span><select name="status">${options(stOpts, connStatus(c))}</select>
            <span class="hint">${esc(t('conn.unknown_hint'))}</span></label>
          <label class="field"><span>${esc(t('conn.kind'))}</span><select name="kind">${options(CONN_KIND, c.kind || '')}</select></label>
          <label class="field span-2"><span>${esc(t('conn.method'))}</span><input type="text" name="method" value="${esc(c.method)}" placeholder="${esc(t('conn.method_placeholder'))}"></label>
          <label class="field span-2"><span>${esc(t('conn.actions'))}</span><input type="text" name="actions" value="${esc(joinList(c.actions))}" placeholder="read, search, send"><span class="hint">${esc(t('form.comma_hint'))}</span></label>
          <label class="field"><span>${esc(t('conn.source'))}</span><input type="text" name="source" value="${esc(c.source)}" placeholder="${esc(t('conn.source_placeholder'))}"></label>
          <label class="field"><span>${esc(t('form.checked_date'))}</span><input type="date" name="checkedAt" value="${esc(c.checkedAt || '')}"></label>
          <p class="hint span-2 muted small" id="conn-hint">${esc(t('conn.verified_hint'))}</p>
          <label class="check span-2"><input type="checkbox" name="demo"${c.demo ? ' checked' : ''}> ${esc(t('demo.checkbox'))}</label>
        </div>`,
      onOpen: (f) => {
        // При выборе «проверено» подставляем сегодняшнюю дату, если пусто
        f.elements.status.addEventListener('change', () => {
          if (f.elements.status.value === 'verified' && !f.elements.checkedAt.value) f.elements.checkedAt.value = today();
        });
      },
      onSubmit: async (f) => {
        const status = fv(f, 'status') || 'unknown';
        const data = {
          toolId, systemId, status,
          kind: fv(f, 'kind'),
          method: fv(f, 'method'),
          actions: parseList(fv(f, 'actions')),
          source: fv(f, 'source'),
          checkedAt: fv(f, 'checkedAt') || null,
          demo: f.elements.demo.checked
        };
        if (status === 'verified' && (!data.checkedAt || !data.source)) {
          throw new Error(t('error.verified_requires'));
        }
        await api('PUT', '/api/connections', data);
        toast(status === 'unknown' ? t('toast.conn_reset') : t('toast.conn_saved'));
        await reloadConfig();
      }
    });
  }

  // ===== Редактирование правила =====
  function editRule(id) {
    const r = id ? cfg().rules.find((x) => x.id === id) : null;
    if (id && !r) { toast(t('toast.rule_not_found'), 'error'); return; }
    const x = r || { keywords: [], requiredTools: [], capabilities: [], preferredSystems: [], priority: 0, promptTemplate: '{task}' };
    const tools = cfg().tools;
    const sysIds = cfg().systems.map((s) => s.id).join(', ');
    openModal({
      title: r ? t('form.rule_title', { name: r.name || r.id }) : t('form.rule_new'),
      body: `
        <div class="form-grid two">
          <label class="field"><span>${esc(t('form.name'))}</span><input type="text" name="name" value="${esc(x.name)}" required maxlength="80"></label>
          <label class="field"><span>${esc(t('form.id'))}</span><input type="text" name="id" value="${esc(x.id)}"${r ? ' readonly' : ` placeholder="${esc(t('form.id_placeholder'))}"`} maxlength="40"></label>
          <label class="field span-2"><span>${esc(t('form.keywords'))}</span><input type="text" name="keywords" value="${esc(joinList(x.keywords))}" placeholder="${esc(t('form.keywords_placeholder'))}">
            <span class="hint">${esc(t('form.keywords_hint'))}</span></label>
          <fieldset class="span-2"><legend>${esc(t('form.required_tools'))}</legend>
            ${tools.length ? `<div class="check-list">${tools.map((tl) => `<label class="check"><input type="checkbox" name="requiredTools" value="${esc(tl.id)}"${(x.requiredTools || []).includes(tl.id) ? ' checked' : ''}> ${esc(tl.name || tl.id)}</label>`).join('')}</div>`
              : `<p class="muted small">${esc(t('form.no_tools'))}</p>`}
          </fieldset>
          <label class="field span-2"><span>${esc(t('form.capabilities'))}</span><input type="text" name="capabilities" value="${esc(joinList(x.capabilities))}"><span class="hint">${esc(t('form.comma_hint'))}</span></label>
          <label class="field span-2"><span>${esc(t('form.preferred_systems'))}</span><input type="text" name="preferredSystems" value="${esc(joinList(x.preferredSystems))}">
            <span class="hint">${esc(t('form.preferred_hint', { ids: sysIds || '—' }))}</span></label>
          <label class="field span-2"><span>${esc(t('form.prompt_template'))}</span><textarea name="promptTemplate" rows="3">${esc(x.promptTemplate)}</textarea>
            <span class="hint">${esc(t('form.prompt_template_hint'))}</span></label>
          <label class="field"><span>${esc(t('form.priority'))}</span><input type="number" name="priority" value="${esc(x.priority != null ? x.priority : 0)}" step="1"></label>
          <label class="check"><input type="checkbox" name="demo"${x.demo ? ' checked' : ''}> ${esc(t('demo.checkbox'))}</label>
        </div>`,
      onSubmit: async (f) => {
        const name = fv(f, 'name');
        if (!name) throw new Error(t('error.name_required'));
        const pref = parseList(fv(f, 'preferredSystems'));
        const unknown = pref.filter((p) => !systemById(p));
        if (unknown.length) throw new Error(t('error.unknown_systems', { list: unknown.join(', ') }));
        const pr = parseInt(fv(f, 'priority'), 10);
        const data = Object.assign({}, r || {}, {
          id: r ? r.id : makeId(fv(f, 'id'), name, cfg().rules, 'rule'),
          name,
          keywords: parseList(fv(f, 'keywords')),
          requiredTools: fchecked(f, 'requiredTools'),
          capabilities: parseList(fv(f, 'capabilities')),
          preferredSystems: pref,
          promptTemplate: fv(f, 'promptTemplate'),
          priority: Number.isFinite(pr) ? pr : 0,
          demo: f.elements.demo.checked
        });
        if (r) await api('PUT', '/api/rules/' + encodeURIComponent(r.id), data);
        else await api('POST', '/api/rules', data);
        toast(r ? t('toast.rule_saved') : t('toast.rule_added'));
        await reloadConfig();
      }
    });
  }

  async function deleteRule(id) {
    const r = cfg().rules.find((x) => x.id === id);
    if (!r) return;
    if (!confirm(t('confirm.delete_rule', { name: r.name || r.id }))) return;
    try {
      await api('DELETE', '/api/rules/' + encodeURIComponent(id));
      toast(t('toast.rule_deleted'));
      await reloadConfig();
    } catch (e) { toastErr(e); }
  }

  function openUrl(url) {
    const u = safeUrl(url);
    if (!u) { toast(t('toast.invalid_link'), 'error'); return; }
    window.open(u, '_blank', 'noopener,noreferrer');
  }

  // ===== Обработчики событий (делегирование) =====
  function onClick(ev) {
    const tabBtn = ev.target.closest('.tab');
    if (tabBtn) { setTab(tabBtn.dataset.tab); return; }

    if (ev.target.closest('[data-modal-close]')) { closeModal(); return; }

    const el = ev.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const id = el.dataset.id;
    switch (el.dataset.action) {
      case 'open-url': openUrl(el.dataset.url); break;
      case 'new-system': editSystem(null); break;
      case 'edit-system': editSystem(id); break;
      case 'delete-system': deleteSystem(id); break;
      case 'check-system': checkSystem(id, el); break;
      case 'new-tool': editTool(null); break;
      case 'edit-tool': editTool(id); break;
      case 'edit-conn': editConnection(el.dataset.tool, el.dataset.system); break;
      case 'new-rule': editRule(null); break;
      case 'edit-rule': editRule(id); break;
      case 'delete-rule': deleteRule(id); break;
      case 'toggle-rules': state.showRules = !state.showRules; render(); break;
      case 'rec-open': if (state.recResult && state.recResult.best) openUrl(state.recResult.best.url); break;
      case 'rec-copy':
        copyText(state.recPrompt).then((ok) => toast(ok ? t('toast.copied') : t('toast.copy_failed'), ok ? '' : 'error'));
        break;
      case 'rec-handoff': {
        const u = handoffUrl();
        if (u) openUrl(u); else toast(t('toast.handoff_not_configured'), 'error');
        break;
      }
      case 'logout': logout(); break;
      default: break;
    }
  }

  function onInput(ev) {
    const t = ev.target;
    if (t.id === 'search') {
      state.search = t.value;
      clearTimeout(onInput.timer);
      onInput.timer = setTimeout(render, 120);
    } else if (t.id === 'rec-task') {
      state.recTask = t.value;
    } else if (t.id === 'rec-prompt') {
      state.recPrompt = t.value;
      const hb = $('[data-action="rec-handoff"]');
      if (hb) hb.disabled = !handoffUrl();
    }
  }

  function onChange(ev) {
    const t = ev.target;
    if (t.id === 'f-status') { state.matrixStatus = t.value; render(); }
    else if (t.id === 'f-category') { state.matrixCategory = t.value; render(); }
    else if (t.name === 'theme' && t.type === 'radio') setTheme(t.value);
    else if (t.name === 'language' && t.type === 'radio') setLanguage(t.value);
  }

  function onSubmit(ev) {
    const f = ev.target;
    if (f.id === 'login-form') onLogin(ev);
    else if (f.id === 'modal-form') onModalSubmit(ev);
    else if (f.id === 'rec-form') { ev.preventDefault(); doRecommend(); }
    else if (f.id === 'import-form') doImport(ev);
  }

  function onKey(ev) {
    if (ev.key === 'Escape' && !$('#modal').hidden) { closeModal(); return; }
    // Ctrl/Cmd+Enter в поле задачи — подобрать
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey) && ev.target.id === 'rec-task') {
      ev.preventDefault();
      doRecommend();
    }
    // Стрелки по вкладкам
    if ((ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') && ev.target.classList && ev.target.classList.contains('tab')) {
      const i = TABS.indexOf(state.tab);
      const next = TABS[(i + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
      setTab(next);
      const b = $(`.tab[data-tab="${next}"]`);
      if (b) b.focus();
    }
  }

  // ===== Старт =====
  function init() {
    applyTheme(currentTheme());
    const fromHash = location.hash.replace('#', '');
    const saved = lsGet(TAB_KEY);
    state.tab = TABS.includes(fromHash) ? fromHash : (TABS.includes(saved) ? saved : 'systems');

    document.addEventListener('click', onClick);
    document.addEventListener('input', onInput);
    document.addEventListener('change', onChange);
    document.addEventListener('submit', onSubmit);
    document.addEventListener('keydown', onKey);
    window.addEventListener('hashchange', () => {
      const h = location.hash.replace('#', '');
      if (TABS.includes(h) && h !== state.tab) setTab(h);
    });
    $('#boot-retry').addEventListener('click', boot);
    loadLanguage(currentLang()).then(boot, boot);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
