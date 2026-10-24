#!/usr/bin/env node
// AI Dashboard «Мои ИИ системы» — HTTP-сервер: статика, API, авторизация.
// Только встроенные модули Node.js (≥ 18), без npm-зависимостей. Контракт: docs/SPEC.md.
'use strict';

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const dns = require('dns');
const net = require('net');
const os = require('os');
const { URL } = require('url');

// ---------------------------------------------------------------------------
// Константы и пути
// ---------------------------------------------------------------------------
const APP_VERSION = '1.1.0';
const SCHEMA_VERSION = 1;

const ROOT = __dirname;
const PUBLIC_DIR = path.join(ROOT, 'public');
// AID_DATA_DIR — только для тестов (scripts/test-api.sh), чтобы не трогать рабочие данные
const DATA_DIR = process.env.AID_DATA_DIR ? path.resolve(process.env.AID_DATA_DIR) : path.join(ROOT, 'data');
const DEFAULT_CONFIG_FILE = path.join(ROOT, 'data', 'config.default.json');
const CONFIG_FILE = path.join(DATA_DIR, 'config.json');
const BACKUP_FILE = CONFIG_FILE + '.bak';
const SESSIONS_FILE = path.join(DATA_DIR, '.sessions.json');
const LOG_FILE = path.join(DATA_DIR, 'server.log');
const ENV_FILE = path.join(DATA_DIR, '.env');

const MAX_BODY = 5 * 1024 * 1024;              // лимит тела запроса — 5 МБ
const SESSION_TTL_MS = 30 * 24 * 3600 * 1000;  // 30 дней
const MAX_SESSIONS = 100;
const COOKIE_NAME = 'aid_session';
const LOGIN_DELAY_MS = 500;
const LOGIN_MAX_FAILS = 10;                    // неудачных попыток за окно
const LOGIN_WINDOW_MS = 15 * 60 * 1000;
const CHECK_TIMEOUT_MS = 5000;

const ACCESS_VALUES = ['app', 'browser', 'api', 'local'];
const API_ACCESS_VALUES = ['unknown', 'yes', 'no'];
const SITE_STATUS = ['ok', 'down', 'unchecked'];
const API_STATUS = ['ok', 'down', 'unchecked', 'n/a'];
const TOOLS_STATUS = ['ok', 'partial', 'unchecked'];
const HANDOFF_TYPES = ['none', 'url', 'clipboard'];
const TOOL_CATEGORIES = ['mail', 'calendar', 'files', 'code', 'browser', 'terminal', 'design', 'tasks', 'notes', 'skill', 'other'];
const CONN_STATUS = ['verified', 'manual', 'available', 'none', 'unknown'];
const CONN_KINDS = ['plugin', 'mcp', 'skill', 'link', 'builtin', ''];
const THEMES = ['auto', 'light', 'dark'];
const LANGUAGES = ['ru', 'de'];
const ICON_RE = /^\/icons\/[a-z0-9_-]+\.svg$/;
const ICONS_DIR = path.join(PUBLIC_DIR, 'icons');
const FORBIDDEN_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
};

// CSP для страницы: только свои скрипты/стили (инлайн-обработчики onclick="" работать не будут)
const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
  "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

// ---------------------------------------------------------------------------
// Ошибки и утилиты
// ---------------------------------------------------------------------------
class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const bad = (msg) => new HttpError(400, msg);

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const clone = (v) => JSON.parse(JSON.stringify(v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha256 = (s) => crypto.createHash('sha256').update(String(s), 'utf8').digest();
const round2 = (n) => Math.round(n * 100) / 100;
const fmtNum = (n) => String(round2(n)).replace('.', ',');

// Сегодняшняя дата в локальном часовом поясе: YYYY-MM-DD
function todayLocal() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// Атомарная запись: tmp → fsync → rename
function writeFileAtomic(file, data, mode = 0o644) {
  const tmp = `${file}.tmp-${process.pid}-${crypto.randomBytes(4).toString('hex')}`;
  try {
    const fd = fs.openSync(tmp, 'w', mode);
    try {
      fs.writeFileSync(fd, data);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, file);
    try { fs.chmodSync(file, mode); } catch { /* не критично */ }
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch { /* уже нет */ }
    throw e;
  }
}

// Простой парсер .env: KEY=VALUE, комментарии #, кавычки "..." / '...'
function parseEnvFile(file) {
  const out = {};
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch { return out; }
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    let v = m[2];
    const q = v[0];
    if ((q === '"' || q === "'") && v.length >= 2 && v.endsWith(q)) {
      v = v.slice(1, -1);
    } else {
      const hash = v.indexOf(' #');            // комментарий в конце строки
      if (hash >= 0) v = v.slice(0, hash);
      v = v.trim();
    }
    out[m[1]] = v;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Логирование: только метод, путь, код, время. Без тел, cookie, заголовков.
// ---------------------------------------------------------------------------
function log(line) {
  const clean = String(line).replace(/[\x00-\x1f\x7f]/g, ' ');
  try { fs.appendFileSync(LOG_FILE, `${new Date().toISOString()} ${clean}\n`, { mode: 0o600 }); } catch { /* игнор */ }
  if (process.stdout.isTTY) console.log(clean);
}

// ---------------------------------------------------------------------------
// Валидация (белый список полей — всё неизвестное отбрасывается)
// ---------------------------------------------------------------------------
function vStr(v, field, { max = 2000, required = false } = {}) {
  if (v === undefined || v === null) {
    if (required) throw bad(`Поле «${field}» обязательно`);
    return '';
  }
  if (typeof v !== 'string') throw bad(`Поле «${field}» должно быть строкой`);
  if (v.length > max) throw bad(`Поле «${field}» слишком длинное (максимум ${max} символов)`);
  if (required && !v.trim()) throw bad(`Поле «${field}» не может быть пустым`);
  return v.trim();
}

function vStrArr(v, field, { maxItems = 200, maxLen = 500 } = {}) {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw bad(`Поле «${field}» должно быть списком строк`);
  if (v.length > maxItems) throw bad(`Поле «${field}»: слишком много элементов (максимум ${maxItems})`);
  const out = [];
  for (const x of v) {
    if (typeof x !== 'string') throw bad(`Поле «${field}» должно содержать только строки`);
    if (x.length > maxLen) throw bad(`Поле «${field}»: элемент слишком длинный (максимум ${maxLen} символов)`);
    const t = x.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

function vEnum(v, field, allowed, def) {
  if (v === undefined || v === null) {
    if (def === undefined) throw bad(`Поле «${field}» обязательно`);
    return def;
  }
  if (!allowed.includes(v)) throw bad(`Поле «${field}»: недопустимое значение «${String(v).slice(0, 50)}». Допустимо: ${allowed.map((a) => a || '""').join(', ')}`);
  return v;
}

function vEnumArr(v, field, allowed) {
  const arr = vStrArr(v, field, { maxItems: allowed.length + 5, maxLen: 50 });
  for (const x of arr) if (!allowed.includes(x)) throw bad(`Поле «${field}»: недопустимое значение «${x}». Допустимо: ${allowed.join(', ')}`);
  return arr;
}

function vSlug(v, field) {
  if (typeof v !== 'string' || !SLUG_RE.test(v) || FORBIDDEN_KEYS.has(v)) {
    throw bad(`Некорректный ${field} «${String(v ?? '').slice(0, 64)}»: допустимы строчные латинские буквы, цифры, «-» и «_» (до 64 символов)`);
  }
  return v;
}

function vSlugArr(v, field) {
  const arr = vStrArr(v, field, { maxItems: 100, maxLen: 64 });
  for (const x of arr) vSlug(x, `${field}[]`);
  return arr;
}

function vDate(v, field) {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v !== 'string' || !DATE_RE.test(v) || Number.isNaN(Date.parse(v))) {
    throw bad(`Поле «${field}»: ожидается дата в формате ГГГГ-ММ-ДД`);
  }
  return v;
}

function vBool(v, field, def = false) {
  if (v === undefined || v === null) return def;
  if (typeof v !== 'boolean') throw bad(`Поле «${field}» должно быть true или false`);
  return v;
}

function vNum(v, field, def = 0) {
  if (v === undefined || v === null || v === '') return def;
  const n = typeof v === 'string' ? Number(v) : v;
  if (typeof n !== 'number' || !Number.isFinite(n)) throw bad(`Поле «${field}» должно быть числом`);
  return n;
}

function vUrl(v, field, { allowEmpty = true } = {}) {
  const s = vStr(v, field, { max: 2000 });
  if (!s) {
    if (allowEmpty) return '';
    throw bad(`Поле «${field}» обязательно`);
  }
  let u;
  try { u = new URL(s); } catch { throw bad(`Поле «${field}»: некорректная ссылка`); }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') throw bad(`Поле «${field}»: ссылка должна начинаться с http:// или https://`);
  return s;
}

function vObj(v, field) {
  if (v === undefined || v === null) return {};
  if (!isPlainObject(v)) throw bad(`Поле «${field}» должно быть объектом`);
  return v;
}

function normSystem(o) {
  if (!isPlainObject(o)) throw bad('Система: ожидается объект');
  const st = vObj(o.status, 'status');
  const ho = vObj(o.handoff, 'handoff');
  const handoff = {
    type: vEnum(ho.type, 'handoff.type', HANDOFF_TYPES, 'none'),
    template: vStr(ho.template, 'handoff.template', { max: 2000 }),
  };
  if (handoff.type === 'url') {
    if (!handoff.template.includes('{prompt}')) throw bad('Поле «handoff.template»: для передачи через ссылку шаблон должен содержать {prompt}');
    vUrl(handoff.template.split('{prompt}').join('x'), 'handoff.template', { allowEmpty: false });
  }
  return {
    id: vSlug(o.id, 'id'),
    name: vStr(o.name, 'name', { max: 200, required: true }),
    url: vUrl(o.url, 'url'),
    access: vEnumArr(o.access, 'access', ACCESS_VALUES),
    models: vStrArr(o.models, 'models'),
    tasks: vStrArr(o.tasks, 'tasks'),
    subscription: vStr(o.subscription, 'subscription', { max: 500 }),
    apiAccess: vEnum(o.apiAccess, 'apiAccess', API_ACCESS_VALUES, 'unknown'),
    limits: vStr(o.limits, 'limits', { max: 5000 }),
    status: {
      site: vEnum(st.site, 'status.site', SITE_STATUS, 'unchecked'),
      api: vEnum(st.api, 'status.api', API_STATUS, 'unchecked'),
      tools: vEnum(st.tools, 'status.tools', TOOLS_STATUS, 'unchecked'),
    },
    checkedAt: vDate(o.checkedAt, 'checkedAt'),
    handoff,
    notes: vStr(o.notes, 'notes', { max: 5000 }),
    sources: vStrArr(o.sources, 'sources', { maxItems: 20, maxLen: 500 }),
    icon: vIcon(o.icon),
    demo: vBool(o.demo, 'demo'),
  };
}

// Иконка: пусто или /icons/<имя>.svg
function vIcon(v) {
  const s = vStr(v, 'icon', { max: 200 });
  if (s && !ICON_RE.test(s)) throw bad('Поле «icon»: ожидается путь вида /icons/имя.svg (строчные латинские буквы, цифры, «-», «_») или пустая строка');
  return s;
}

function normTool(o) {
  if (!isPlainObject(o)) throw bad('Инструмент: ожидается объект');
  return {
    id: vSlug(o.id, 'id'),
    name: vStr(o.name, 'name', { max: 200, required: true }),
    category: vEnum(o.category, 'category', TOOL_CATEGORIES, 'other'),
    demo: vBool(o.demo, 'demo'),
  };
}

function normConnection(o) {
  if (!isPlainObject(o)) throw bad('Связь: ожидается объект');
  const c = {
    toolId: vSlug(o.toolId, 'toolId'),
    systemId: vSlug(o.systemId, 'systemId'),
    status: vEnum(o.status, 'status', CONN_STATUS),
    kind: vEnum(o.kind ?? '', 'kind', CONN_KINDS, ''),
    method: vStr(o.method, 'method', { max: 1000 }),
    actions: vStrArr(o.actions, 'actions', { maxItems: 50, maxLen: 100 }),
    source: vStr(o.source, 'source', { max: 1000 }),
    checkedAt: vDate(o.checkedAt, 'checkedAt'),
    demo: vBool(o.demo, 'demo'),
  };
  // «Подключено и проверено» — только с датой проверки и источником
  if (c.status === 'verified') {
    if (!c.checkedAt) throw bad('Статус «подключено и проверено» требует дату проверки (checkedAt)');
    if (!c.source) throw bad('Статус «подключено и проверено» требует источник информации (source)');
  }
  return c;
}

function normRule(o) {
  if (!isPlainObject(o)) throw bad('Правило: ожидается объект');
  return {
    id: vSlug(o.id, 'id'),
    name: vStr(o.name, 'name', { max: 200, required: true }),
    keywords: vStrArr(o.keywords, 'keywords', { maxItems: 100, maxLen: 100 }),
    requiredTools: vSlugArr(o.requiredTools, 'requiredTools'),
    capabilities: vStrArr(o.capabilities, 'capabilities', { maxItems: 50, maxLen: 300 }),
    preferredSystems: vSlugArr(o.preferredSystems, 'preferredSystems'),
    promptTemplate: vStr(o.promptTemplate, 'promptTemplate', { max: 10000 }),
    priority: vNum(o.priority, 'priority', 0),
    demo: vBool(o.demo, 'demo'),
  };
}

function normSettings(o) {
  const s = vObj(o, 'settings');
  return {
    theme: vEnum(s.theme, 'settings.theme', THEMES, 'auto'),
    language: vEnum(s.language, 'settings.language', LANGUAGES, 'ru'),
  };
}

// Нормализовать массив с префиксом позиции в сообщении об ошибке
function normList(arr, field, fn, { required }) {
  if (arr === undefined || arr === null) {
    if (required) throw bad(`Поле «${field}» обязательно (список)`);
    return [];
  }
  if (!Array.isArray(arr)) throw bad(`Поле «${field}» должно быть списком`);
  return arr.map((item, i) => {
    try { return fn(item); } catch (e) {
      if (e instanceof HttpError) e.message = `${field}[${i}]: ${e.message}`;
      throw e;
    }
  });
}

function assertUniqueIds(list, field) {
  const seen = new Set();
  for (const x of list) {
    if (seen.has(x.id)) throw bad(`${field}: повторяющийся id «${x.id}»`);
    seen.add(x.id);
  }
}

// Полная проверка конфигурации. required=false — разрешить отсутствующие разделы (для слияния)
function normConfig(o, { required = true } = {}) {
  if (!isPlainObject(o)) throw bad('Конфигурация должна быть объектом JSON');
  const systems = normList(o.systems, 'systems', normSystem, { required });
  const tools = normList(o.tools, 'tools', normTool, { required });
  const rules = normList(o.rules, 'rules', normRule, { required });
  const conns = normList(o.connections, 'connections', normConnection, { required });
  assertUniqueIds(systems, 'systems');
  assertUniqueIds(tools, 'tools');
  assertUniqueIds(rules, 'rules');

  const sysIds = new Set(systems.map((s) => s.id));
  const toolIds = new Set(tools.map((t) => t.id));
  const pairs = new Set();
  const connections = [];
  for (const c of conns) {
    if (c.status === 'unknown') continue;  // отсутствие записи = unknown
    if (!toolIds.has(c.toolId)) throw bad(`connections: инструмент «${c.toolId}» не найден`);
    if (!sysIds.has(c.systemId)) throw bad(`connections: система «${c.systemId}» не найдена`);
    const key = `${c.toolId}|${c.systemId}`;
    if (pairs.has(key)) throw bad(`connections: повторяющаяся связь «${c.toolId}» × «${c.systemId}»`);
    pairs.add(key);
    connections.push(c);
  }
  return {
    version: SCHEMA_VERSION,
    updatedAt: typeof o.updatedAt === 'string' && DATE_RE.test(o.updatedAt) ? o.updatedAt : new Date().toISOString(),
    systems,
    tools,
    connections,
    rules,
    settings: normSettings(o.settings),
  };
}

// ---------------------------------------------------------------------------
// Подбор исполнителя (SPEC §3)
// ---------------------------------------------------------------------------
const CONN_SCORE = { verified: 3, manual: 2, available: 0.5, none: 0, unknown: 0 };
const CONN_STATUS_RU = {
  verified: 'подключено и проверено',
  manual: 'указано вручную',
  available: 'доступно для подключения',
  none: 'не подключено',
  unknown: 'неизвестно',
};

function recommend(task, config) {
  const text = String(task ?? '').trim();
  const lower = text.toLowerCase();
  const rules = Array.isArray(config.rules) ? config.rules : [];
  const systems = Array.isArray(config.systems) ? config.systems : [];
  const toolsById = new Map((config.tools || []).map((t) => [t.id, t]));
  const conns = new Map((config.connections || []).map((c) => [`${c.toolId}|${c.systemId}`, c]));

  // 1. Правило: максимум совпавших ключевых слов, при равенстве — больший priority
  let matched = null;
  let matchedKeywords = [];
  for (const r of rules) {
    const kws = (r.keywords || []).map((k) => String(k).toLowerCase().trim()).filter(Boolean);
    const hits = kws.filter((k) => lower.includes(k));
    if (!hits.length) continue;
    const better = !matched ||
      hits.length > matchedKeywords.length ||
      (hits.length === matchedKeywords.length && (Number(r.priority) || 0) > (Number(matched.priority) || 0));
    if (better) { matched = r; matchedKeywords = hits; }
  }
  let matchType = matched ? 'keywords' : null;
  if (!matched) {
    const def = rules.find((r) => r.id === 'default');
    if (def) { matched = def; matchType = 'default'; }
  }

  const required = matched ? (matched.requiredTools || []) : [];
  const preferred = matched ? (matched.preferredSystems || []) : [];

  // 2. Очки для каждой системы
  const scored = systems.map((s, idx) => {
    let score = 0;
    const reasons = [];
    const haveTools = [];
    const missingTools = [];
    for (const toolId of required) {
      const c = conns.get(`${toolId}|${s.id}`);
      const status = c && CONN_SCORE[c.status] !== undefined ? c.status : 'unknown';
      const pts = CONN_SCORE[status];
      score += pts;
      const tname = toolsById.get(toolId)?.name || toolId;
      // haveTools — все нужные инструменты, кроме «не подключено» (со статусом для иконки)
      if (status === 'none') missingTools.push(toolId);
      else haveTools.push({ toolId, status });
      reasons.push(`${tname}: ${CONN_STATUS_RU[status]}${pts ? ` (+${fmtNum(pts)})` : ''}`);
    }
    if (matched && !required.length) reasons.push('Для задачи не нужны подключённые инструменты');
    const pi = preferred.indexOf(s.id);
    if (pi >= 0) {
      const pts = Math.max(0, 1 - 0.5 * pi);   // первый +1, второй +0,5
      if (pts > 0) {
        score += pts;
        reasons.push(`Приоритетная система в правиле «${matched.name}» (+${fmtNum(pts)})`);
      }
    }
    const site = s.status?.site;
    if (site === 'down') { score -= 5; reasons.push('Сайт недоступен при последней проверке (−5)'); }
    else if (site !== 'ok') reasons.push('Доступность сайта не проверена');
    if (s.limits) reasons.push(`Ограничения: ${s.limits}`);
    return {
      systemId: s.id,
      name: s.name,
      url: s.url || '',
      score: round2(score),
      haveTools,
      missingTools,
      limits: s.limits || '',
      reasons,
      _idx: idx,
      _handoff: s.handoff || { type: 'none', template: '' },
    };
  });

  // 3. Сортировка: очки по убыванию, при равенстве — порядок систем в конфигурации
  scored.sort((a, b) => b.score - a.score || a._idx - b._idx);
  const bestHandoff = scored[0] ? scored[0]._handoff : null;
  const candidates = scored.map(({ _idx, _handoff, ...rest }) => rest);
  const best = candidates[0] || null;
  const alternative = candidates[1] || null;

  // Промпт: подстановка {task}; если шаблона нет или в нём нет {task} — задача добавляется в конец
  const tpl = matched && matched.promptTemplate ? matched.promptTemplate : '{task}';
  const prompt = tpl.includes('{task}') ? tpl.split('{task}').join(text) : `${tpl}\n\n${text}`;

  // Передача задачи: ссылка — только если у лучшей системы handoff.type === "url"
  let handoff = { type: 'none', url: null };
  if (best && bestHandoff) {
    if (bestHandoff.type === 'url' && String(bestHandoff.template || '').includes('{prompt}')) {
      const url = bestHandoff.template.split('{prompt}').join(encodeURIComponent(prompt));
      handoff = /^https?:\/\//i.test(url) ? { type: 'url', url } : { type: 'none', url: null };
    } else if (HANDOFF_TYPES.includes(bestHandoff.type)) {
      handoff = { type: bestHandoff.type, url: null };
    }
  }

  const result = {
    task: text,
    matchedRule: matched ? { id: matched.id, name: matched.name } : null,
    matchType,                      // "keywords" | "default" | null
    matchedKeywords,
    capabilities: matched ? (matched.capabilities || []) : [],
    requiredTools: required,
    candidates,
    best,
    alternative,
    prompt,
    handoff,
  };
  if (matchType !== 'keywords') {
    result.hint = 'Подходящее правило не найдено — уточните задачу или добавьте правило подбора.';
  }
  return result;
}

// ---------------------------------------------------------------------------
// Экспорт для модульных тестов (require('./server.js')) — сервер при этом не стартует
// ---------------------------------------------------------------------------
if (require.main !== module) {
  module.exports = { recommend, normConfig, normSystem, normTool, normConnection, normRule, parseEnvFile, HttpError };
  return;
}

// ===========================================================================
// Ниже — запуск сервера
// ===========================================================================

// --- Настройки из data/.env (переменные окружения процесса, если заданы, важнее .env) ---
const env = parseEnvFile(ENV_FILE);
const pickEnv = (k) => (process.env[k] !== undefined ? process.env[k] : (env[k] || ''));
const PASSWORD = pickEnv('PASSWORD');
const HOST = pickEnv('HOST') || '127.0.0.1';
const PORT = parseInt(pickEnv('PORT') || '8787', 10);
const AUTH_REQUIRED = PASSWORD.length > 0;
const PASSWORD_HASH = sha256(PASSWORD);
// Хеш сессии привязан к паролю: смена PASSWORD отзывает все выданные сессии
const tokenHash = (token) => crypto.createHmac('sha256', PASSWORD_HASH).update(token).digest('hex');
const IS_LOOPBACK = ['127.0.0.1', 'localhost', '::1'].includes(HOST);

// Список через запятую → массив строк в нижнем регистре
const csvEnv = (k) => pickEnv(k).split(',').map((x) => x.trim().toLowerCase()).filter(Boolean);

// Разрешённые значения заголовка Host/Origin (защита от DNS rebinding и CSRF)
const IS_WILDCARD = ['0.0.0.0', '::', '[::]'].includes(HOST);
const ALLOWED_HOSTS = (() => {
  const list = ['localhost', '127.0.0.1', '[::1]'];
  const h = HOST.toLowerCase();
  if (!IS_WILDCARD) list.push(h.includes(':') && !h.startsWith('[') ? `[${h}]` : h);
  else {
    // HOST=0.0.0.0: разрешаем собственные адреса интерфейсов (в т.ч. Tailscale/WireGuard)
    for (const addrs of Object.values(os.networkInterfaces())) {
      for (const a of addrs || []) list.push(a.family === 'IPv6' || a.family === 6 ? `[${a.address.toLowerCase()}]` : a.address);
    }
  }
  for (const e of csvEnv('ALLOWED_HOSTS')) list.push(e);
  return list;
})();

// Разобрать «host[:port]» → { name, port }; IPv6 — в квадратных скобках
function splitHostPort(value) {
  const v = String(value || '').trim().toLowerCase();
  if (!v) return null;
  let m = v.match(/^(\[[0-9a-f:.]+\])(?::(\d{1,5}))?$/);
  if (m) return { name: m[1], port: m[2] || '' };
  m = v.match(/^([a-z0-9.-]+)(?::(\d{1,5}))?$/);
  if (m) return { name: m[1].replace(/\.$/, ''), port: m[2] || '' };
  return null;
}

// Запись списка без порта разрешает любой порт; с портом — только этот порт
function isAllowedHost(value) {
  const hp = splitHostPort(value);
  if (!hp) return false;
  return ALLOWED_HOSTS.some((entry) => {
    const e = splitHostPort(entry);
    return e && e.name === hp.name && (!e.port || e.port === (hp.port || '80'));
  });
}

// Хосты, которым разрешено проверять внутренние адреса (например, локальный сервис)
const CHECK_ALLOW_PRIVATE = new Set(csvEnv('CHECK_ALLOW_PRIVATE_HOSTS').map((e) => {
  const hp = splitHostPort(e);
  return hp ? hp.name.replace(/^\[|\]$/g, '') : e;
}));

if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  console.error(`Ошибка: некорректный PORT в ${ENV_FILE}`);
  process.exit(1);
}
if (!IS_LOOPBACK && !AUTH_REQUIRED) {
  console.error(`Ошибка: HOST=${HOST} доступен по сети, но PASSWORD не задан. Укажите PASSWORD в ${ENV_FILE} или HOST=127.0.0.1.`);
  process.exit(1);
}

fs.mkdirSync(DATA_DIR, { recursive: true });

// --- Конфигурация (в памяти + data/config.json) ---
let config;

function readConfigFile(file) {
  return normConfig(JSON.parse(fs.readFileSync(file, 'utf8')));
}

function loadConfig() {
  if (!fs.existsSync(CONFIG_FILE)) {
    // Первый запуск: создаём config.json из config.default.json
    let def;
    try { def = readConfigFile(DEFAULT_CONFIG_FILE); } catch (e) {
      console.error(`Ошибка: не удалось прочитать ${DEFAULT_CONFIG_FILE}: ${e.message}`);
      process.exit(1);
    }
    def.updatedAt = new Date().toISOString();
    writeFileAtomic(CONFIG_FILE, JSON.stringify(def, null, 2) + '\n', 0o600);
    log(`config.json создан из config.default.json`);
    return def;
  }
  try {
    return readConfigFile(CONFIG_FILE);
  } catch (e) {
    log(`ОШИБКА: config.json повреждён или не проходит проверку: ${e.message}`);
    if (fs.existsSync(BACKUP_FILE)) {
      try {
        const cfg = readConfigFile(BACKUP_FILE);
        const broken = `${CONFIG_FILE}.broken-${Date.now()}`;
        fs.renameSync(CONFIG_FILE, broken);
        writeFileAtomic(CONFIG_FILE, JSON.stringify(cfg, null, 2) + '\n', 0o600);
        log(`Восстановлено из config.json.bak; повреждённый файл сохранён как ${path.basename(broken)}`);
        return cfg;
      } catch (e2) {
        log(`ОШИБКА: config.json.bak тоже не читается: ${e2.message}`);
      }
    }
    console.error(`Ошибка: ${CONFIG_FILE} не читается (${e.message}). Исправьте файл вручную или удалите его, чтобы создать заново из config.default.json.`);
    process.exit(1);
  }
}

// Сохранить новую конфигурацию: полная проверка → резервная копия → атомарная запись
function commit(next) {
  const validated = normConfig(next);
  validated.updatedAt = new Date().toISOString();
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      fs.copyFileSync(CONFIG_FILE, BACKUP_FILE);
      // copyFile переносит права исходника — закрепляем 0600
      const fd = fs.openSync(BACKUP_FILE, 'r+');
      try { fs.fchmodSync(fd, 0o600); } finally { fs.closeSync(fd); }
    }
  } catch { /* не критично */ }
  writeFileAtomic(CONFIG_FILE, JSON.stringify(validated, null, 2) + '\n', 0o600);
  config = validated;
  return validated;
}

// --- Сессии (в памяти + data/.sessions.json; хранится только SHA-256 токена) ---
const sessions = new Map(); // hash(hex) → expiresAt(ms)

function loadSessions() {
  try {
    const data = JSON.parse(fs.readFileSync(SESSIONS_FILE, 'utf8'));
    const now = Date.now();
    for (const [h, exp] of Object.entries(data.sessions || {})) {
      if (/^[a-f0-9]{64}$/.test(h) && typeof exp === 'number' && exp > now) sessions.set(h, exp);
    }
  } catch { /* файла нет или повреждён — начинаем с пустого списка */ }
}

function saveSessions() {
  const obj = { version: 1, sessions: Object.fromEntries(sessions) };
  try { writeFileAtomic(SESSIONS_FILE, JSON.stringify(obj) + '\n', 0o600); } catch (e) { log(`ОШИБКА записи сессий: ${e.message}`); }
}

function cleanupSessions() {
  const now = Date.now();
  let changed = false;
  for (const [h, exp] of sessions) if (exp <= now) { sessions.delete(h); changed = true; }
  if (changed) saveSessions();
}

function getCookie(req, name) {
  const header = req.headers.cookie;
  if (!header) return null;
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    if (part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return null;
}

function sessionHash(req) {
  const token = getCookie(req, COOKIE_NAME);
  if (!token || !/^[A-Za-z0-9_-]{20,128}$/.test(token)) return null;
  return tokenHash(token);
}

function isAuthenticated(req) {
  if (!AUTH_REQUIRED) return true;
  const h = sessionHash(req);
  if (!h) return false;
  const exp = sessions.get(h);
  if (!exp) return false;
  if (exp <= Date.now()) { sessions.delete(h); saveSessions(); return false; }
  return true;
}

function checkPassword(input) {
  if (typeof input !== 'string') return false;
  // Сравнение хешей фиксированной длины — timingSafeEqual без утечки длины пароля
  return crypto.timingSafeEqual(sha256(input), PASSWORD_HASH);
}

// Ограничение перебора: не больше LOGIN_MAX_FAILS неудач за окно с одного адреса
const loginFails = new Map(); // ip → { count, first }
const LOGIN_FAILS_MAX = 1000;
function cleanupLoginFails() {
  const now = Date.now();
  for (const [ip, f] of loginFails) if (now - f.first > LOGIN_WINDOW_MS) loginFails.delete(ip);
}
function loginLocked(ip) {
  const f = loginFails.get(ip);
  if (!f) return 0;
  if (Date.now() - f.first > LOGIN_WINDOW_MS) { loginFails.delete(ip); return 0; }
  return f.count >= LOGIN_MAX_FAILS ? Math.ceil((f.first + LOGIN_WINDOW_MS - Date.now()) / 60000) : 0;
}
function loginFailed(ip) {
  const f = loginFails.get(ip);
  if (!f || Date.now() - f.first > LOGIN_WINDOW_MS) {
    loginFails.delete(ip);
    if (loginFails.size >= LOGIN_FAILS_MAX) cleanupLoginFails();
    // Всё ещё полно — вытесняем самые старые записи
    while (loginFails.size >= LOGIN_FAILS_MAX) loginFails.delete(loginFails.keys().next().value);
    loginFails.set(ip, { count: 1, first: Date.now() });
  } else f.count += 1;
}

// --- HTTP-помощники ---
function sendJson(res, status, obj, headers = {}) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
    ...headers,
  });
  res.end(body);
}

function sendText(res, status, text) {
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(text);
}

// Чтение тела с лимитом 5 МБ
function readBody(req) {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers['content-length'] || 0);
    if (declared > MAX_BODY) return reject(new HttpError(413, 'Слишком большой запрос (максимум 5 МБ)'));
    const chunks = [];
    let size = 0;
    let done = false;
    req.on('data', (chunk) => {
      if (done) return;
      size += chunk.length;
      if (size > MAX_BODY) {
        done = true;
        reject(new HttpError(413, 'Слишком большой запрос (максимум 5 МБ)'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => { if (!done) { done = true; resolve(Buffer.concat(chunks).toString('utf8')); } });
    req.on('error', (e) => { if (!done) { done = true; reject(e); } });
  });
}

// JSON-тело; ключи __proto__/constructor/prototype запрещены
async function readJson(req, { optional = false } = {}) {
  const text = await readBody(req);
  if (!text.trim()) {
    if (optional) return {};
    throw bad('Пустое тело запроса: ожидается JSON');
  }
  try {
    return JSON.parse(text, (key, value) => {
      if (FORBIDDEN_KEYS.has(key)) throw new HttpError(400, `Недопустимый ключ «${key}» в JSON`);
      return value;
    });
  } catch (e) {
    if (e instanceof HttpError) throw e;
    throw bad('Некорректный JSON в теле запроса');
  }
}

// Защита от CSRF: изменяющий запрос только со своего origin и только как application/json
function checkCsrf(req) {
  const foreign = () => new HttpError(403, 'Запрос с чужого сайта отклонён');
  if (String(req.headers['sec-fetch-site'] || '').toLowerCase() === 'cross-site') throw foreign();
  const origin = req.headers.origin;
  let originOk = false;
  if (origin !== undefined) {
    let u = null;
    try { u = new URL(origin); } catch { /* "null" и мусор */ }
    if (!u || (u.protocol !== 'http:' && u.protocol !== 'https:') || !isAllowedHost(u.host || '')) throw foreign();
    originOk = true;
  }
  // Точный media type: параметры после «;» допустимы, «application/jsonjunk» — нет
  const mediaType = String(req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  const isJson = mediaType === 'application/json';
  const hasBody = Number(req.headers['content-length'] || 0) > 0 || req.headers['transfer-encoding'] !== undefined;
  if (hasBody ? !isJson : !(isJson || originOk)) {
    throw new HttpError(415, 'Ожидается Content-Type: application/json');
  }
}

// --- Защита от SSRF: внутренние адреса запрещены (кроме CHECK_ALLOW_PRIVATE_HOSTS) ---
const BLOCKED_NETS = new net.BlockList();
[
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8], ['169.254.0.0', 16],
  ['172.16.0.0', 12], ['192.168.0.0', 16], ['224.0.0.0', 4], ['240.0.0.0', 4],
].forEach(([a, p]) => BLOCKED_NETS.addSubnet(a, p, 'ipv4'));
[
  ['::', 128], ['::1', 128], ['fe80::', 10], ['fc00::', 7], ['ff00::', 8], ['64:ff9b::', 96],
].forEach(([a, p]) => BLOCKED_NETS.addSubnet(a, p, 'ipv6'));

function isBlockedIp(ip) {
  const fam = net.isIP(ip);
  if (!fam) return true;
  // ::ffff:a.b.c.d BlockList сам сверяет с правилами IPv4
  return BLOCKED_NETS.check(ip, fam === 6 ? 'ipv6' : 'ipv4');
}

const SSRF_MSG = 'Проверка внутренних адресов запрещена; добавьте хост в CHECK_ALLOW_PRIVATE_HOSTS';

// Разрешить имя и проверить ВСЕ адреса; вернуть проверенный адрес для соединения
async function resolveCheckTarget(u) {
  const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase().replace(/\.$/, '');
  const allowPrivate = CHECK_ALLOW_PRIVATE.has(host);
  if (!allowPrivate && (host === 'localhost' || host.endsWith('.localhost'))) throw bad(SSRF_MSG);
  let addrs;
  if (net.isIP(host)) addrs = [{ address: host, family: net.isIP(host) }];
  else {
    try { addrs = await dns.promises.lookup(host, { all: true, verbatim: true }); } catch (e) {
      return { error: `адрес не найден (${e.code || 'DNS'})` };
    }
  }
  if (!addrs.length) return { error: 'адрес не найден' };
  if (!allowPrivate && addrs.some((a) => isBlockedIp(a.address))) throw bad(SSRF_MSG);
  return { address: addrs[0].address, family: addrs[0].family };
}

// --- Проверка доступности сайта (только HEAD, без редиректов, тело не читается) ---
async function checkSite(urlStr) {
  let u;
  try { u = new URL(urlStr); } catch { return { site: 'down', httpStatus: null, detail: 'некорректная ссылка' }; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    return { site: 'down', httpStatus: null, detail: 'поддерживаются только http и https' };
  }
  const target = await resolveCheckTarget(u);   // 400 для запрещённых адресов
  if (target.error) return { site: 'down', httpStatus: null, detail: target.error };
  // Соединение закреплено за проверенным адресом (повторного DNS-запроса нет); SNI/Host — исходное имя
  const pinnedLookup = (hostname, opts, cb) => {
    if (typeof opts === 'function') { cb = opts; opts = {}; }
    if (opts && opts.all) cb(null, [{ address: target.address, family: target.family }]);
    else cb(null, target.address, target.family);
  };
  return new Promise((resolve) => {
    let finished = false;
    const finish = (r) => { if (!finished) { finished = true; clearTimeout(timer); resolve(r); } };
    const lib = u.protocol === 'https:' ? https : http;
    const req = lib.request(u, { method: 'HEAD', timeout: CHECK_TIMEOUT_MS, lookup: pinnedLookup, agent: false, headers: { 'User-Agent': `ai-dashboard/${APP_VERSION} (site check)` } }, (res) => {
      res.resume();
      // Любой ответ < 500 (включая 3xx/403/405) — сервер отвечает, сайт доступен
      const ok = res.statusCode < 500;
      finish({ site: ok ? 'ok' : 'down', httpStatus: res.statusCode, detail: `HTTP ${res.statusCode}` });
      req.destroy();
    });
    const timer = setTimeout(() => { req.destroy(); finish({ site: 'down', httpStatus: null, detail: 'нет ответа за 5 секунд' }); }, CHECK_TIMEOUT_MS);
    req.on('timeout', () => { req.destroy(); finish({ site: 'down', httpStatus: null, detail: 'нет ответа за 5 секунд' }); });
    req.on('error', (e) => finish({ site: 'down', httpStatus: null, detail: `ошибка соединения (${e.code || 'unknown'})` }));
    req.end();
  });
}

// --- Транслитерация для автоматического id ---
const TRANSLIT = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm',
  н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sch',
  ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya', ä: 'ae', ö: 'oe', ü: 'ue', ß: 'ss',
};
function slugify(name) {
  const s = String(name || '').toLowerCase().split('').map((ch) => (TRANSLIT[ch] !== undefined ? TRANSLIT[ch] : ch)).join('')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
  return s || 'item';
}
function uniqueId(base, list) {
  const ids = new Set(list.map((x) => x.id));
  if (!ids.has(base) && !FORBIDDEN_KEYS.has(base)) return base;
  for (let i = 2; ; i++) if (!ids.has(`${base}-${i}`)) return `${base}-${i}`;
}

// --- CRUD для systems / tools / rules ---
const COLLECTIONS = {
  systems: { norm: normSystem, label: 'Система', notFound: (id) => `Система «${id}» не найдена` },
  tools: { norm: normTool, label: 'Инструмент', notFound: (id) => `Инструмент «${id}» не найден` },
  rules: { norm: normRule, label: 'Правило', notFound: (id) => `Правило «${id}» не найдено` },
};

function decodeId(raw) {
  let id;
  try { id = decodeURIComponent(raw); } catch { throw bad('Некорректный id в адресе'); }
  return vSlug(id, 'id');
}

function createItem(coll, body) {
  if (!isPlainObject(body)) throw bad('Ожидается объект JSON');
  const next = clone(config);
  const list = next[coll];
  const item = { ...body };
  if (item.id === undefined || item.id === null || item.id === '') item.id = uniqueId(slugify(item.name), list);
  const norm = COLLECTIONS[coll].norm(item);
  if (list.some((x) => x.id === norm.id)) throw bad(`${COLLECTIONS[coll].label} с id «${norm.id}» уже существует`);
  list.push(norm);
  commit(next);
  return norm;
}

function updateItem(coll, id, body) {
  if (!isPlainObject(body)) throw bad('Ожидается объект JSON');
  const next = clone(config);
  const list = next[coll];
  const idx = list.findIndex((x) => x.id === id);
  if (idx < 0) throw new HttpError(404, COLLECTIONS[coll].notFound(id));
  if (body.id !== undefined && body.id !== id) throw bad('Изменение id не поддерживается: создайте новую запись');
  const old = list[idx];
  const merged = { ...old, ...body, id };
  // Вложенные объекты системы обновляются частично
  if (coll === 'systems') {
    if (isPlainObject(body.status)) merged.status = { ...old.status, ...body.status };
    if (isPlainObject(body.handoff)) merged.handoff = { ...old.handoff, ...body.handoff };
  }
  list[idx] = COLLECTIONS[coll].norm(merged);
  commit(next);
  return list[idx];
}

function deleteItem(coll, id) {
  const next = clone(config);
  const before = next[coll].length;
  next[coll] = next[coll].filter((x) => x.id !== id);
  if (next[coll].length === before) throw new HttpError(404, COLLECTIONS[coll].notFound(id));
  // Каскад: связи и ссылки в правилах
  if (coll === 'systems') {
    next.connections = next.connections.filter((c) => c.systemId !== id);
    next.rules.forEach((r) => { r.preferredSystems = r.preferredSystems.filter((s) => s !== id); });
  } else if (coll === 'tools') {
    next.connections = next.connections.filter((c) => c.toolId !== id);
    next.rules.forEach((r) => { r.requiredTools = r.requiredTools.filter((t) => t !== id); });
  }
  commit(next);
}

// Слияние по id (импорт с ?merge=1)
function mergeConfig(base, incoming) {
  const next = clone(base);
  for (const coll of ['systems', 'tools', 'rules']) {
    for (const item of incoming[coll]) {
      const i = next[coll].findIndex((x) => x.id === item.id);
      if (i >= 0) next[coll][i] = item; else next[coll].push(item);
    }
  }
  for (const c of incoming.connections) {
    const i = next.connections.findIndex((x) => x.toolId === c.toolId && x.systemId === c.systemId);
    if (i >= 0) next.connections[i] = c; else next.connections.push(c);
  }
  if (incoming.settings) next.settings = { ...next.settings, ...incoming.settings };
  return next;
}

// --- Маршруты API ---
const routes = [];
const route = (method, pattern, handler) => routes.push({ method, pattern, handler });

route('GET', /^\/api\/health$/, (req, res) => {
  sendJson(res, 200, { ok: true, version: APP_VERSION, authRequired: AUTH_REQUIRED, authenticated: isAuthenticated(req) });
});

route('POST', /^\/api\/login$/, async (req, res) => {
  const body = await readJson(req, { optional: true });
  if (!AUTH_REQUIRED) return sendJson(res, 200, { ok: true, authRequired: false });
  const ip = req.socket.remoteAddress || '';
  const wait = loginLocked(ip);
  if (wait) return sendJson(res, 429, { error: `Слишком много неудачных попыток. Попробуйте через ${wait} мин.` });
  if (!checkPassword(isPlainObject(body) ? body.password : undefined)) {
    loginFailed(ip);
    await sleep(LOGIN_DELAY_MS);
    return sendJson(res, 401, { error: 'Неверный пароль' });
  }
  loginFails.delete(ip);
  const token = crypto.randomBytes(32).toString('base64url');
  sessions.set(tokenHash(token), Date.now() + SESSION_TTL_MS);
  // Ограничиваем число сессий — удаляем самые старые
  while (sessions.size > MAX_SESSIONS) sessions.delete(sessions.keys().next().value);
  saveSessions();
  sendJson(res, 200, { ok: true }, {
    'Set-Cookie': `${COOKIE_NAME}=${token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${SESSION_TTL_MS / 1000}`,
  });
});

route('POST', /^\/api\/logout$/, (req, res) => {
  const h = sessionHash(req);
  if (h && sessions.delete(h)) saveSessions();
  sendJson(res, 200, { ok: true }, { 'Set-Cookie': `${COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0` });
});

route('GET', /^\/api\/config$/, (req, res) => sendJson(res, 200, config));

route('PUT', /^\/api\/config$/, async (req, res) => {
  const body = await readJson(req);
  sendJson(res, 200, commit(normConfig(body)));
});

route('GET', /^\/api\/settings$/, (req, res) => sendJson(res, 200, config.settings));

route('PUT', /^\/api\/settings$/, async (req, res) => {
  const body = await readJson(req);
  const next = clone(config);
  next.settings = normSettings({ ...next.settings, ...vObj(body, 'settings') });
  sendJson(res, 200, commit(next).settings);
});

for (const coll of Object.keys(COLLECTIONS)) {
  route('GET', new RegExp(`^/api/${coll}$`), (req, res) => sendJson(res, 200, config[coll]));
  route('POST', new RegExp(`^/api/${coll}$`), async (req, res) => {
    sendJson(res, 201, createItem(coll, await readJson(req)));
  });
  route('GET', new RegExp(`^/api/${coll}/([^/]+)$`), (req, res, m) => {
    const id = decodeId(m[1]);
    const item = config[coll].find((x) => x.id === id);
    if (!item) throw new HttpError(404, COLLECTIONS[coll].notFound(id));
    sendJson(res, 200, item);
  });
  route('PUT', new RegExp(`^/api/${coll}/([^/]+)$`), async (req, res, m) => {
    const id = decodeId(m[1]);
    sendJson(res, 200, updateItem(coll, id, await readJson(req)));
  });
  route('DELETE', new RegExp(`^/api/${coll}/([^/]+)$`), (req, res, m) => {
    deleteItem(coll, decodeId(m[1]));
    sendJson(res, 200, { ok: true });
  });
}

route('GET', /^\/api\/connections$/, (req, res) => sendJson(res, 200, config.connections));

route('PUT', /^\/api\/connections$/, async (req, res) => {
  const c = normConnection(await readJson(req));
  if (!config.tools.some((t) => t.id === c.toolId)) throw bad(`Инструмент «${c.toolId}» не найден`);
  if (!config.systems.some((s) => s.id === c.systemId)) throw bad(`Система «${c.systemId}» не найдена`);
  const next = clone(config);
  const i = next.connections.findIndex((x) => x.toolId === c.toolId && x.systemId === c.systemId);
  if (c.status === 'unknown') {
    if (i >= 0) next.connections.splice(i, 1);   // unknown = записи нет
  } else if (i >= 0) {
    next.connections[i] = c;
  } else {
    next.connections.push(c);
  }
  commit(next);
  sendJson(res, 200, { ok: true, connection: c.status === 'unknown' ? null : c });
});

route('POST', /^\/api\/recommend$/, async (req, res) => {
  const body = await readJson(req);
  const task = isPlainObject(body) ? vStr(body.task, 'task', { max: 5000, required: true }) : '';
  if (!task) throw bad('Опишите задачу');
  sendJson(res, 200, recommend(task, config));
});

route('GET', /^\/api\/export$/, (req, res) => {
  // config.json не содержит секретов: пароль хранится только в data/.env
  const body = JSON.stringify(normConfig(config), null, 2) + '\n';
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Disposition': 'attachment; filename="config.json"',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
});

route('POST', /^\/api\/import$/, async (req, res, m, url) => {
  const body = await readJson(req);
  const merge = ['1', 'true', 'yes'].includes(url.searchParams.get('merge') || '');
  const incoming = normConfig(body, { required: !merge });
  const result = commit(merge ? mergeConfig(config, incoming) : incoming);
  sendJson(res, 200, { ok: true, merged: merge, config: result });
});

route('POST', /^\/api\/check\/([^/]+)$/, async (req, res, m) => {
  const id = decodeId(m[1]);
  const sys = config.systems.find((s) => s.id === id);
  if (!sys) throw new HttpError(404, `Система «${id}» не найдена`);
  if (!sys.url) throw bad('У системы не указана ссылка');
  const r = await checkSite(sys.url);
  // Конфигурация могла измениться за время проверки — берём свежую копию
  const next = clone(config);
  const target = next.systems.find((s) => s.id === id);
  if (!target) throw new HttpError(404, `Система «${id}» не найдена`);
  target.status.site = r.site;
  target.checkedAt = todayLocal();
  const saved = commit(next).systems.find((s) => s.id === id);
  sendJson(res, 200, { ok: true, systemId: id, site: r.site, httpStatus: r.httpStatus, detail: r.detail, checkedAt: saved.checkedAt, system: saved });
});

// Список SVG-иконок из public/icons/
route('GET', /^\/api\/icons$/, (req, res) => {
  let files = [];
  try { files = fs.readdirSync(ICONS_DIR).filter((f) => /^[a-z0-9_-]+\.svg$/.test(f)).sort(); } catch { /* каталога нет */ }
  sendJson(res, 200, files);
});

const PUBLIC_API = new Set(['/api/health', '/api/login']);
const MUTATING = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);

async function handleApi(req, res, url) {
  const p = url.pathname;
  if (MUTATING.has(req.method)) checkCsrf(req);
  if (!PUBLIC_API.has(p) && !isAuthenticated(req)) {
    // Выход без действующей сессии — ничего не делаем, просто чистим cookie
    if (p === '/api/logout' && req.method === 'POST') {
      return sendJson(res, 200, { ok: true }, { 'Set-Cookie': `${COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0` });
    }
    throw new HttpError(401, 'Требуется вход');
  }
  let pathMatched = false;
  for (const r of routes) {
    const m = p.match(r.pattern);
    if (!m) continue;
    pathMatched = true;
    if (r.method === req.method || (req.method === 'HEAD' && r.method === 'GET')) return r.handler(req, res, m, url);
  }
  if (pathMatched) throw new HttpError(405, 'Метод не поддерживается');
  throw new HttpError(404, 'Не найдено');
}

// --- Статика из public/ (защита от выхода за пределы каталога) ---
let PUBLIC_REAL = PUBLIC_DIR;
try { PUBLIC_REAL = fs.realpathSync(PUBLIC_DIR); } catch { /* каталога ещё нет */ }

function serveStatic(req, res, url) {
  if (req.method !== 'GET' && req.method !== 'HEAD') return sendText(res, 405, 'Метод не поддерживается');
  let rel;
  try { rel = decodeURIComponent(url.pathname); } catch { return sendText(res, 400, 'Некорректный адрес'); }
  if (rel.includes('\0') || rel.includes('\\')) return sendText(res, 404, 'Не найдено');
  if (rel === '/' || rel === '') rel = '/index.html';
  const segments = rel.split('/').filter(Boolean);
  // Запрещены «..», скрытые файлы и каталоги (.env, .sessions.json и т.п.)
  if (!segments.length || segments.some((s) => s.startsWith('.'))) return sendText(res, 404, 'Не найдено');
  const file = path.join(PUBLIC_DIR, ...segments);
  if (!file.startsWith(PUBLIC_DIR + path.sep)) return sendText(res, 404, 'Не найдено');
  let real;
  let stat;
  try {
    real = fs.realpathSync(file);
    stat = fs.statSync(real);
  } catch { return sendText(res, 404, 'Не найдено'); }
  if (!real.startsWith(PUBLIC_REAL + path.sep) || !stat.isFile()) return sendText(res, 404, 'Не найдено');
  const ext = path.extname(real).toLowerCase();
  const headers = {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Content-Length': stat.size,
    'Cache-Control': 'no-cache',
  };
  if (ext === '.html') headers['Content-Security-Policy'] = CSP;
  if (ext === '.svg') headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'";
  res.writeHead(200, headers);
  if (req.method === 'HEAD') return res.end();
  fs.createReadStream(real).on('error', () => res.destroy()).pipe(res);
}

// --- Главный обработчик ---
async function handler(req, res) {
  const started = process.hrtime.bigint();
  let url;
  try { url = new URL(req.url || '/', 'http://localhost'); } catch { url = new URL('http://localhost/'); }
  res.on('finish', () => {
    const ms = Number((process.hrtime.bigint() - started) / 1000000n);
    log(`${req.method} ${url.pathname.slice(0, 300)} ${res.statusCode} ${ms}ms`);
  });

  // Общие заголовки безопасности
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Frame-Options', 'DENY');

  const isApi = url.pathname === '/api' || url.pathname.startsWith('/api/');
  // Защита от DNS rebinding: только разрешённые значения Host
  if (!isAllowedHost(req.headers.host)) {
    const msg = 'Неизвестный адрес сервера (Host). Добавьте его в ALLOWED_HOSTS в data/.env';
    if (isApi) sendJson(res, 421, { error: msg }); else sendText(res, 421, msg);
    return;
  }
  try {
    if (isApi) await handleApi(req, res, url);
    else serveStatic(req, res, url);
  } catch (e) {
    const status = e instanceof HttpError ? e.status : 500;
    if (status === 500) log(`ОШИБКА 500: ${e && e.stack ? e.stack : e}`);
    const message = status === 500 ? 'Внутренняя ошибка сервера' : e.message;
    if (res.headersSent) { res.destroy(); return; }
    const extra = status === 413 ? { Connection: 'close' } : {};
    if (isApi) sendJson(res, status, { error: message }, extra);
    else sendText(res, status, message);
    if (status === 413) res.on('finish', () => req.destroy());
  }
}

// --- Запуск ---
config = loadConfig();
loadSessions();
cleanupSessions();
setInterval(cleanupSessions, 3600 * 1000).unref();
setInterval(cleanupLoginFails, 60 * 1000).unref();

// Права файлов: config.json и .bak — только владелец; .env не должен читаться группой/другими
for (const f of [CONFIG_FILE, BACKUP_FILE]) {
  try { if ((fs.statSync(f).mode & 0o077) !== 0) fs.chmodSync(f, 0o600); } catch { /* файла нет */ }
}
try {
  const mode = fs.statSync(ENV_FILE).mode & 0o777;
  if (mode & 0o077) log(`ВНИМАНИЕ: ${ENV_FILE} доступен группе/другим (права ${mode.toString(8)}). Выполните: chmod 600 ${ENV_FILE}`);
} catch { /* .env нет */ }

const server = http.createServer((req, res) => { handler(req, res); });
server.requestTimeout = 30000;
server.headersTimeout = 15000;

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') console.error(`Ошибка: порт ${PORT} уже занят. Возможно, сервер уже запущен (scripts/stop.sh) или измените PORT в data/.env.`);
  else console.error(`Ошибка сервера: ${e.message}`);
  log(`ОШИБКА запуска: ${e.code || e.message}`);
  process.exit(1);
});

server.listen(PORT, HOST, () => {
  log(`Сервер запущен: http://${HOST.includes(':') ? `[${HOST}]` : HOST}:${PORT} (версия ${APP_VERSION}, вход по паролю: ${AUTH_REQUIRED ? 'да' : 'нет'}, pid ${process.pid})`);
});

function shutdown(sig) {
  log(`Остановка (${sig})`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('unhandledRejection', (e) => log(`ОШИБКА (unhandledRejection): ${e && e.stack ? e.stack : e}`));
process.on('uncaughtException', (e) => {
  log(`ОШИБКА (uncaughtException): ${e && e.stack ? e.stack : e}`);
  console.error('Критическая ошибка, сервер остановлен. Подробности в data/server.log');
  process.exit(1);
});
