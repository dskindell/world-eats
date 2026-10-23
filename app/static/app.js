'use strict';

// ---- Text: the strings table (strings-en.js) and lookups ----

// Warns once per key (or other subject), so a gap is visible without flooding the console.
const warned = new Set();
function warnOnce(subject, problem) {
  if (warned.has(subject)) return;
  warned.add(subject);
  console.warn(`World Eats strings: ${problem}: ${subject}`); // i18n-ok console only
}

// Only a plain object is accepted; it's frozen (plural objects too) so nothing can change it later.
function acceptTable(table) {
  if (table === null || typeof table !== 'object' || Object.getPrototypeOf(table) !== Object.prototype) {
    warnOnce('WORLD_EATS_STRINGS', 'table missing, showing keys'); // i18n-ok console only
    return Object.freeze({});
  }
  for (const value of Object.values(table)) {
    if (value !== null && typeof value === 'object') Object.freeze(value);
  }
  return Object.freeze(table);
}

// The one place the table is chosen. There's only English for now.
const STRINGS = acceptTable(self.WORLD_EATS_STRINGS);
const tableString = (key) => (Object.hasOwn(STRINGS, key) && typeof STRINGS[key] === 'string' ? STRINGS[key] : null);
const LANG = tableString('meta.lang') || 'en';

// A bad language tag falls back to English rather than breaking the page.
const PLURALS = (() => {
  try {
    return new Intl.PluralRules(LANG);
  } catch {
    return new Intl.PluralRules('en');
  }
})();
const UPPER_LANG = (() => {
  try {
    'a'.toLocaleUpperCase(LANG);
    return LANG;
  } catch {
    return 'en';
  }
})();

// {name} placeholders and {{ / }} for literal braces, in one pass: inserted
// values are never scanned again. A missing param stays as {name}.
const PLACEHOLDER_RE = /\{\{|\}\}|\{([a-z][a-zA-Z0-9]*)\}/g;

function fillTemplate(key, template, params) {
  return template.replace(PLACEHOLDER_RE, (token, name) => {
    if (token === '{{') return '{';
    if (token === '}}') return '}';
    if (params && Object.hasOwn(params, name)) return String(params[name]);
    warnOnce(key, `no value for {${name}}`); // i18n-ok console only
    return token;
  });
}

// Text for a key (a string entry). The result is plain text: set it with
// textContent, or wrap it as ${esc(t(…))} in an innerHTML template.
function t(key, params) {
  const template = tableString(key);
  if (template === null) {
    warnOnce(key, 'missing string'); // i18n-ok console only
    return key;
  }
  return fillTemplate(key, template, params);
}

// Text for a plural entry, chosen by count ({count} is filled in too).
function tn(key, count, params) {
  const forms = Object.hasOwn(STRINGS, key) ? STRINGS[key] : null;
  if (forms === null || typeof forms !== 'object') {
    warnOnce(key, 'missing plural'); // i18n-ok console only
    return key;
  }
  const form = PLURALS.select(count);
  const template = Object.hasOwn(forms, form) && typeof forms[form] === 'string' ? forms[form] : forms.other;
  if (typeof template !== 'string') {
    warnOnce(key, 'plural without other'); // i18n-ok console only
    return key;
  }
  return fillTemplate(key, template, { ...params, count });
}

// A server error's text from its code: used only if the code is well formed,
// has an entry, and every placeholder in it has an integer param; otherwise
// the server's own English (`fallback`).
const ERROR_CODE_RE = /^[a-z][a-z0-9_]{0,39}$/;

function errorText(code, params, fallback) {
  if (typeof code !== 'string' || !ERROR_CODE_RE.test(code)) return fallback;
  const key = `error.${code}`;
  const template = tableString(key);
  if (template === null) return fallback;
  const values = params !== null && typeof params === 'object' && !Array.isArray(params) ? params : {};
  for (const [, name] of template.matchAll(PLACEHOLDER_RE)) {
    if (name !== undefined && !(Object.hasOwn(values, name) && Number.isInteger(values[name]))) return fallback;
  }
  return fillTemplate(key, template, values);
}

// A country's name from the table, or the server's name if there's no entry.
function countryName(code, fallbackName) {
  if (typeof code !== 'string' || !CODE_RE.test(code)) return fallbackName;
  return tableString(`country.${code}`) ?? fallbackName;
}

// The departure board's short form of a country's name, or null.
function boardShort(code) {
  if (typeof code !== 'string' || !CODE_RE.test(code)) return null;
  return tableString(`board.short.${code}`);
}

// Region names shown in the UI, keyed by the server's region value (the value
// itself is still what's sorted, filtered and kept in the address).
const REGION_NAMES = Object.freeze({
  // The lexer behind the hard-coded-text test counts property-name literals too.
  Africa: () => t('region.africa'),
  Asia: () => t('region.asia'),
  Europe: () => t('region.europe'),
  'North America': () => t('region.north_america'), // i18n-ok server region value
  Oceania: () => t('region.oceania'),
  'South America': () => t('region.south_america'), // i18n-ok server region value
});
const regionName = (region) => (Object.hasOwn(REGION_NAMES, region) ? REGION_NAMES[region]() : region);

// index.html keeps its English as a fallback; elements marked with data-i18n
// (text) or data-i18n-<attribute> get the table's text once, before the first render.
const I18N_ATTRIBUTES = ['placeholder', 'aria-label', 'title'];

function applyPageStrings() {
  document.documentElement.lang = LANG;
  for (const el of document.querySelectorAll('[data-i18n]')) {
    const text = tableString(el.dataset.i18n);
    if (text === null) warnOnce(el.dataset.i18n, 'missing string'); // i18n-ok console only
    else el.textContent = fillTemplate(el.dataset.i18n, text);
  }
  for (const attr of I18N_ATTRIBUTES) {
    for (const el of document.querySelectorAll(`[data-i18n-${attr}]`)) {
      const key = el.getAttribute(`data-i18n-${attr}`);
      const text = tableString(key);
      if (text === null) warnOnce(key, 'missing string'); // i18n-ok console only
      else el.setAttribute(attr, fillTemplate(key, text));
    }
  }
}

// ---- State and small helpers ----

const state = { user: null, setupRequired: false };
let currentView = null;

const $ = (id) => document.getElementById(id);

// The only way a server value may enter innerHTML: element text or a
// double-quoted attribute value, never href/src/style/on* attributes.
// Prefer textContent where practical.
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const badgesHtml = (badges) => badges
  .map(([text, cls]) => `<span class="badge ${cls}">${esc(text)}</span>`) // i18n-ok HTML markup
  .join('');

function textEl(tag, text, className = '') {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg'; // security-lint: ok XML namespace name for createElementNS, never fetched

// ---- Avatars (contract 11a §4): one builder, DOM only ----

const AVATAR_SEED_RE = /^[0-9a-f]{32}$/;
// avatar-pin: a map pin's head, sized by its x/y/width/height attributes (no CSS size).
const AVATAR_SIZES = ['avatar-sm', 'avatar-md', 'avatar-lg', 'avatar-pin'];
const AVATAR_ALL_CELLS = 0x7fff;
const AVATAR_MIN_CELLS = 4;

// The 15 left-half cells (bit r * 3 + c) and the hue, or null for an invalid seed.
function avatarPattern(seed) {
  if (typeof seed !== 'string' || !AVATAR_SEED_RE.test(seed)) return null;
  const b = [];
  for (let i = 0; i < 32; i += 2) b.push(parseInt(seed.slice(i, i + 2), 16));
  let v = (b[0] | (b[1] << 8)) & AVATAR_ALL_CELLS;
  let on = 0;
  for (let i = 0; i < 15; i += 1) on += (v >> i) & 1;
  if (on < AVATAR_MIN_CELLS) v ^= AVATAR_ALL_CELLS;
  const hue = ((b[4] << 8) | b[5]) % 360;
  // Always true here; kept as the contract's guard on the one value built into fill (11a §6.6).
  return Number.isInteger(hue) && hue >= 0 && hue < 360 ? { v, hue } : null;
}

// Decorative: the name next to it is the accessible name. The seed itself
// never enters the DOM, only the derived cells and hue.
function avatarEl(seed, sizeClass) {
  const size = AVATAR_SIZES.includes(sizeClass) ? sizeClass : AVATAR_SIZES[0];
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', `avatar ${size}`);
  svg.setAttribute('viewBox', '0 0 36 36');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const pattern = avatarPattern(seed);
  const disc = document.createElementNS(SVG_NS, 'circle');
  disc.setAttribute('class', pattern ? 'avatar-disc' : 'avatar-disc avatar-empty'); // i18n-ok CSS class names
  disc.setAttribute('cx', '18');
  disc.setAttribute('cy', '18');
  disc.setAttribute('r', '18');
  svg.append(disc);
  if (!pattern) return svg;
  const fill = `hsl(${pattern.hue}, 65%, 45%)`;
  for (let row = 0; row < 5; row += 1) {
    for (let col = 0; col < 5; col += 1) {
      const src = col < 3 ? col : 4 - col;
      if (!((pattern.v >> (row * 3 + src)) & 1)) continue;
      const rect = document.createElementNS(SVG_NS, 'rect');
      rect.setAttribute('x', String(5.5 + col * 5));
      rect.setAttribute('y', String(5.5 + row * 5));
      rect.setAttribute('width', '5');
      rect.setAttribute('height', '5');
      rect.setAttribute('fill', fill);
      rect.setAttribute('shape-rendering', 'crispEdges');
      svg.append(rect);
    }
  }
  return svg;
}

// Pairs each fixed <span class="avatar-slot"> just rendered by innerHTML with
// the same array, by index (one slot per item, contract 11a §6.1). If the
// counts ever differ, every slot gets the placeholder rather than someone else's avatar.
function fillAvatarSlots(container, people, sizeClass) {
  const slots = container.querySelectorAll('.avatar-slot');
  const aligned = slots.length === people.length;
  if (!aligned) console.error('avatar slots and people differ in number'); // i18n-ok developer message
  slots.forEach((slot, i) => {
    const person = aligned ? people[i] : null;
    slot.replaceWith(avatarEl(person ? person.avatar_seed : null, sizeClass));
  });
}

// `code` is the server's error code ('' if none); branches compare codes, never text.
class ApiError extends Error {
  constructor(message, status, code = '') {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// ---- API ----

async function api(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (method === 'POST' || method === 'PATCH') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body ?? {});
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(t('client.network'), 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.ok && data !== null && typeof data === 'object') return data;

  const hasError = !res.ok && data && typeof data.error === 'string';
  const message = hasError ? errorText(data.code, data.params, data.error)
    : t('client.http_status', { status: Number(res.status) });
  const code = hasError && typeof data.code === 'string' ? data.code : '';
  if (res.status === 401 && path !== '/api/login' && path !== '/api/session') {
    onSessionLost();
  } else if (res.status === 403 && data && data.password_change_required === true) {
    onPasswordChangeRequired();
  } else if (res.status === 403 && path.startsWith('/api/admin/') && code === 'admins_only') {
    onAdminLost();
  }
  throw new ApiError(message, res.status, code);
}

// ---- Feedback: inline form errors and toasts (textContent only) ----

function setError(container, message) {
  const el = container.querySelector('.form-error');
  el.textContent = message;
  el.hidden = !message;
}

let toastTimer = 0;
function toast(message, kind = 'ok') {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('toast-error', kind === 'error');
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
    el.textContent = '';
  }, kind === 'error' ? 8000 : 4000);
}

// Wires a form: preventDefault first, clears the old error, disables the
// submit button while the handler runs, and shows any thrown error inline.
function bindForm(form, handler) {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    setError(form, '');
    button.disabled = true;
    try {
      await handler(form);
    } catch (err) {
      if (err instanceof QuietError) return;
      // A dialog closed meanwhile (e.g. by navigation) would keep a stale
      // error for its next opening, so report it as a toast instead.
      const dialog = form.closest('dialog');
      if (dialog && !dialog.open) toast(err.message, 'error');
      else setError(form, err.message);
    } finally {
      button.disabled = false;
    }
  });
}

// A client-side error. `code` (optional) lets it steer the same branches as a server error.
const fail = (message, code = '') => {
  const err = new Error(message);
  err.code = code;
  throw err;
};

// Abandons an action without telling the user, e.g. when the session ended
// and the page has already been cleared and re-rendered.
class QuietError extends Error {}

// ---- Client-side checks mirroring contract §4 (the server is authoritative) ----

// Limits shared by the checks below and their messages (the server's error codes).
const USERNAME_MIN = 3;
const USERNAME_MAX = 32;
const NAME_MIN = 1;
const NAME_MAX = 50;
const PASSWORD_MIN = 12;
const PASSWORD_MAX = 128;
const USERNAME_RE = new RegExp(`^[A-Za-z0-9._-]{${USERNAME_MIN},${USERNAME_MAX}}$`);
// Cc, Cs, Co, Cn, Zl, Zp, and Cf except U+200D (zero-width joiner).
const BAD_NAME_CHAR_RE = /[\p{Cc}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]|(?!\u200D)\p{Cf}/u;
const codePoints = (s) => [...s].length;

function checkUsername(value) {
  if (!USERNAME_RE.test(value)) fail(t('error.username_invalid', { min: USERNAME_MIN, max: USERNAME_MAX }));
}

// Display names and passport names share one rule set, with different messages.
function checkName(value, messages) {
  const name = value.normalize('NFC').trim();
  const n = codePoints(name);
  if (n < NAME_MIN || n > NAME_MAX) fail(messages.length());
  if (BAD_NAME_CHAR_RE.test(name)) fail(messages.chars());
}

const DISPLAY_NAME_MESSAGES = Object.freeze({
  length: () => t('error.display_name_length', { min: NAME_MIN, max: NAME_MAX }),
  chars: () => t('error.display_name_chars'),
});
const PASSPORT_NAME_MESSAGES = Object.freeze({
  length: () => t('error.passport_name_length', { min: NAME_MIN, max: NAME_MAX }),
  chars: () => t('error.passport_name_chars'),
});
const checkDisplayName = (value) => checkName(value, DISPLAY_NAME_MESSAGES);
const checkPassportName = (value) => checkName(value, PASSPORT_NAME_MESSAGES);

function checkNewPassword(value) {
  const n = codePoints(value);
  if (n < PASSWORD_MIN || n > PASSWORD_MAX || /\p{Cs}/u.test(value)) {
    fail(t('error.password_length', { min: PASSWORD_MIN, max: PASSWORD_MAX }));
  }
}

// ---- Views and routing ----

const VIEWS = ['loading', 'setup', 'login', 'force-password', 'passports', 'passport', 'people', 'country',
  'account', 'admin', 'admin-user'];
// 'loading' keeps the top bar while a logged-in route loads (it's hidden with no user).
const TOPBAR_VIEWS = ['loading', 'passports', 'passport', 'people', 'country', 'account', 'admin', 'admin-user'];
// User, passport and meal ids: one pattern for the route table and every id check.
const ID_PATTERN = '[1-9][0-9]{0,9}';
const ID_RE = new RegExp(`^${ID_PATTERN}$`);
const CODE_RE = /^[A-Z]{2}$/;

// Ordered hash routes, matched against the part before any '?'; the first
// match wins. Anything unmatched goes to #/. Only routes with `query` keep a
// query part (the passport page's search, sort and filters).
// The country route takes any last segment so a bad code can be reported
// (enterCountry checks it against CODE_RE and the country list).
const ROUTES = [
  { re: /^#\/$/, enter: enterHome },
  { re: /^#\/passports$/, enter: enterPassports },
  { re: new RegExp(`^#/passports/(${ID_PATTERN})$`), query: true, enter: enterPassport },
  { re: new RegExp(`^#/passports/(${ID_PATTERN})/people$`), enter: enterPeople },
  { re: new RegExp(`^#/passports/(${ID_PATTERN})/countries/([^/]*)$`), enter: enterCountry },
  { re: /^#\/account$/, enter: enterAccount },
  { re: /^#\/admin$/, admin: true, enter: enterAdmin },
  { re: new RegExp(`^#/admin/users/(${ID_PATTERN})$`), admin: true, enter: enterAdminUser },
];

// Bumped on every render so a slow route load can tell it's been superseded.
let routeToken = 0;
// Hash changes are ignored until a session has loaded and while one is loading.
let sessionLoaded = false;
let sessionPending = false;

// False until the first real (non-loading) view has been shown.
let contentShown = false;

function showView(name) {
  const previous = currentView;
  currentView = name;
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== name;
  $('topbar').hidden = !(state.user && TOPBAR_VIEWS.includes(name));
  if (previous === name) return;
  const view = $(`view-${name}`);
  view.querySelectorAll('.form-error').forEach((el) => { el.textContent = ''; el.hidden = true; });
  // Move focus to the new heading so keyboard and screen-reader users land
  // in the new view; skipped on first load to avoid stealing focus.
  if (contentShown) view.querySelector('h1')?.focus();
  if (name !== 'loading') contentShown = true;
}

// Splits a hash at the first '?' into the route path and the query (null if none).
function splitHash(hash) {
  const i = hash.indexOf('?');
  return i < 0 ? [hash, null] : [hash.slice(0, i), hash.slice(i + 1)];
}

function render() {
  routeToken += 1;
  clearTimeout(searchTimer);
  // A Log-a-meal intent from the pick board is one-shot: taken by this render
  // whatever the route, and passed on only to the matching country page.
  const intent = mealIntent;
  mealIntent = null;
  stopPick();
  closeMapCard();
  finishMapAnim();
  closeMenu();
  closeDialogs();
  if (state.setupRequired) return showView('setup');
  if (!state.user) return showView('login');
  if (state.user.must_change_password) return showView('force-password');
  updateUserText();
  const [path, query] = splitHash(location.hash);
  const route = ROUTES.find((r) => r.re.test(path));
  if (!route || (route.admin && !state.user.is_admin)) {
    history.replaceState(null, '', '#/');
    return enterHome(null, routeToken);
  }
  if (query !== null && !route.query) history.replaceState(null, '', path);
  const [, param, param2] = path.match(route.re);
  const forCountry = route.enter === enterCountry && intent && intent.id === param && intent.code === param2;
  return route.enter(param, routeToken, param2, query, forCountry ? intent : null);
}

function showLoading() {
  $('loading-text').hidden = false;
  $('load-error').hidden = true;
  $('load-retry').hidden = true;
  showView('loading');
}

// Home is the default passport. Replace (not push) the hash so Back doesn't
// return to #/ and bounce forward again.
async function enterHome(_param, token) {
  showLoading();
  let list;
  try {
    list = await api('GET', '/api/passports');
  } catch (err) {
    if (token === routeToken) showLoadError(err.message);
    return;
  }
  if (token !== routeToken) return;
  const id = String(list.default_passport_id);
  if (!ID_RE.test(id)) {
    history.replaceState(null, '', '#/passports');
    enterPassports(null, token);
    return;
  }
  history.replaceState(null, '', `#/passports/${id}`);
  enterPassport(id, token);
}

function enterAccount(_param, token) {
  fillAccount();
  showView('account');
  loadDefaultSelect(token);
}

// Goes to a hash; renders directly if we're already there (no hashchange).
function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

// Goes to a hash without leaving the current entry in history (used when the
// current page is a passport that no longer exists for this user).
function replaceRoute(hash) {
  history.replaceState(null, '', hash);
  render();
}

function closeDialogs() {
  document.querySelectorAll('dialog[open]').forEach((d) => d.close());
}

function updateUserText() {
  $('menu-name').textContent = state.user.display_name;
  $('menu-avatar').replaceChildren(avatarEl(state.user.avatar_seed, 'avatar-md'));
  $('account-avatar').replaceChildren(avatarEl(state.user.avatar_seed, 'avatar-lg'));
  $('account-username').textContent = state.user.username;
  $('menu-admin').hidden = !state.user.is_admin;
}

function fillAccount() {
  if (currentView === 'account') return;
  $('account-display-name').value = state.user.display_name;
  $('password-form').reset();
}

function goHome(user) {
  state.user = user;
  state.setupRequired = false;
  history.replaceState(null, '', '#/');
  render();
}

// Called whenever this device is no longer logged in: drop anything typed
// (passwords included) and close open overlays.
function clearSessionUi() {
  state.user = null;
  closeMenu();
  closeDialogs();
  document.querySelectorAll('form').forEach((f) => f.reset());
  clearPassportUi();
  dropMap();
}

function onSessionLost() {
  clearSessionUi();
  render();
}

// Re-reads /api/session into state without rendering; false if it failed.
async function refreshSession() {
  try {
    const data = await api('GET', '/api/session');
    state.setupRequired = data.setup_required === true;
    state.user = data.user || null;
    return true;
  } catch {
    return false;
  }
}

async function onPasswordChangeRequired() {
  if (!(await refreshSession()) && state.user) state.user.must_change_password = true;
  render();
}

async function onAdminLost() {
  if (!(await refreshSession()) && state.user) state.user.is_admin = false;
  // "Admins only" can also mean the password must now be changed; the router
  // then shows the forced view, so only toast if admin rights are really gone.
  if (state.user && !state.user.is_admin) toast(t('toast.admin_lost'));
  navigate('#/');
}

function showLoadError(message) {
  showView('loading');
  $('loading-text').hidden = true;
  $('load-error').textContent = message;
  $('load-error').hidden = false;
  $('load-retry').hidden = false;
}

async function loadSession() {
  sessionPending = true;
  showLoading();
  try {
    const data = await api('GET', '/api/session');
    state.setupRequired = data.setup_required === true;
    state.user = data.user || null;
    sessionLoaded = true;
    sessionPending = false;
    render();
  } catch (err) {
    sessionPending = false;
    showLoadError(err.message);
  }
}

// ---- Setup and login ----

bindForm($('setup-form'), async (form) => {
  const username = form.elements.username.value;
  const displayName = form.elements.display_name.value;
  const password = form.elements.password.value;
  checkUsername(username);
  checkDisplayName(displayName);
  checkNewPassword(password);
  try {
    const data = await api('POST', '/api/setup', { username, display_name: displayName, password });
    form.reset();
    goHome(data.user);
  } catch (err) {
    // Someone else finished setup first: switch to login and explain there.
    if (err.status !== 409) throw err;
    state.setupRequired = false;
    form.reset();
    render();
    setError($('login-form'), err.message);
  }
});

bindForm($('login-form'), async (form) => {
  const username = form.elements.username.value;
  const password = form.elements.password.value;
  // Avoid sending an empty attempt, which the server would count as a failure.
  if (!username || !password) fail(t('client.login_empty'));
  try {
    const data = await api('POST', '/api/login', { username, password });
    form.reset();
    goHome(data.user);
  } catch (err) {
    if (err.status !== 409) throw err;
    // No accounts exist (setup required): show the setup view instead.
    state.setupRequired = true;
    form.reset();
    render();
    setError($('setup-form'), err.message);
  }
});

// ---- Top bar menu ----

const menuItems = () => [...$('user-menu').querySelectorAll('[role="menuitem"]:not([hidden])')];

function openMenu() {
  $('user-menu').hidden = false;
  $('menu-button').setAttribute('aria-expanded', 'true');
  menuItems()[0].focus();
}

function closeMenu(returnFocus = false) {
  if ($('user-menu').hidden) return;
  $('user-menu').hidden = true;
  $('menu-button').setAttribute('aria-expanded', 'false');
  if (returnFocus) $('menu-button').focus();
}

$('menu-button').addEventListener('click', () => {
  if ($('user-menu').hidden) openMenu();
  else closeMenu();
});

$('user-menu').addEventListener('keydown', (event) => {
  const items = menuItems();
  const i = items.indexOf(document.activeElement);
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const step = event.key === 'ArrowDown' ? 1 : -1;
    items[(i + step + items.length) % items.length].focus();
  } else if (event.key === 'Tab') {
    closeMenu();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (!$('user-menu').hidden) closeMenu(true);
  else if (mapCardCode !== null && !document.querySelector('dialog[open]')) closeMapCard();
});

document.addEventListener('pointerdown', (event) => {
  if (!event.target.closest('.menu-wrap')) closeMenu();
});

// The menu links navigate by hash; just close the menu.
$('user-menu').querySelectorAll('a').forEach((a) => a.addEventListener('click', () => closeMenu()));

async function logOut(event) {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  try {
    await api('POST', '/api/logout');
    onSessionLost();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    button.disabled = false;
  }
}

$('menu-logout').addEventListener('click', logOut);
$('force-logout').addEventListener('click', logOut);

// ---- Account settings ----

bindForm($('name-form'), async (form) => {
  const displayName = form.elements.display_name.value;
  checkDisplayName(displayName);
  const data = await api('PATCH', '/api/account', { display_name: displayName });
  state.user = data.user;
  updateUserText();
  form.elements.display_name.value = data.user.display_name;
  toast(t('toast.display_name_saved'));
});

bindForm($('avatar-form'), async () => {
  const { user } = await api('POST', '/api/account/avatar', {});
  state.user = user;
  updateUserText();
  toast(t('toast.avatar_saved'));
});

bindForm($('password-form'), async (form) => {
  const current = form.elements.current_password.value;
  const next = form.elements.new_password.value;
  if (!current) fail(t('client.current_password_empty'));
  checkNewPassword(next);
  if (next !== form.elements.confirm_password.value) fail(t('client.passwords_mismatch'));
  await api('POST', '/api/account/password', { current_password: current, new_password: next });
  form.reset();
  toast(t('toast.password_changed_others'));
});

// Bumped whenever a dialog opens a load or closes, so a slow load for a
// dialog that has since closed (or reopened) is ignored.
let dialogToken = 0;

// Wires Cancel, and resets the form on every close (Cancel, Escape, success,
// navigation) so no password lingers.
function setupDialog(dialogId) {
  const dialog = $(dialogId);
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    dialogToken += 1;
    const form = dialog.querySelector('form');
    form.reset();
    setError(form, '');
  });
  return dialog;
}

// onOpen (optional) fills the dialog's text and fields before it opens.
function bindDialog(openButtonId, dialogId, onOpen) {
  const dialog = setupDialog(dialogId);
  $(openButtonId).addEventListener('click', () => {
    if (onOpen) onOpen(dialog);
    dialog.showModal();
  });
}

bindDialog('logout-all-open', 'logout-all-dialog');
bindDialog('delete-open', 'delete-dialog', loadDeletePreview);

bindForm($('default-form'), async () => {
  const select = $('default-passport');
  if (select.disabled || !ID_RE.test(select.value)) fail(t('error.choose_passport'));
  await api('PATCH', '/api/account', { default_passport_id: Number(select.value) });
  toast(t('toast.default_saved'));
});

function defaultOptionText(p) {
  if (p.role !== 'owner') return t('account.default_option_shared', { name: p.name, owner: personLabel(p.owner) });
  return p.is_personal ? t('account.default_option_personal', { name: p.name }) : p.name;
}

async function loadDefaultSelect(token) {
  const select = $('default-passport');
  select.disabled = true;
  select.replaceChildren(new Option(t('common.loading'), ''));
  let list;
  try {
    list = await api('GET', '/api/passports');
  } catch (err) {
    if (token === routeToken) setError($('default-form'), err.message);
    return;
  }
  if (token !== routeToken) return;
  const options = list.passports
    .filter((p) => ID_RE.test(String(p.id)))
    .map((p) => new Option(defaultOptionText(p), String(p.id)));
  select.replaceChildren(...options);
  select.value = String(list.default_passport_id);
  select.disabled = false;
}

function previewLines(preview) {
  const lines = [t('account.delete_preview_personal')];
  for (const h of preview.handed_over) {
    lines.push(t('account.delete_preview_handed_over', { name: h.name, owner: personLabel(h.new_owner) }));
  }
  for (const d of preview.deleted) lines.push(t('account.delete_preview_deleted', { name: d.name }));
  const left = Number(preview.left);
  if (left > 0) lines.push(tn('account.delete_preview_left', left));
  lines.push(t('account.delete_preview_meals'));
  return lines;
}

// The delete button stays disabled until the preview has loaded, so the
// consequences are always on screen before the account can be deleted.
async function loadDeletePreview() {
  const dt = ++dialogToken;
  const box = $('delete-preview');
  box.replaceChildren(textEl('p', t('account.delete_preview_loading'), 'muted'));
  $('delete-submit').disabled = true;
  let preview;
  try {
    preview = await api('GET', '/api/account/delete-preview');
  } catch (err) {
    if (dt === dialogToken) {
      box.replaceChildren();
      setError($('delete-form'), err.message);
    }
    return;
  }
  if (dt !== dialogToken) return;
  box.replaceChildren(...previewLines(preview).map((line) => textEl('p', line)));
  $('delete-submit').disabled = false;
}

bindForm($('logout-all-form'), async () => {
  await api('POST', '/api/account/logout-everywhere');
  onSessionLost();
});

bindForm($('delete-form'), async (form) => {
  const password = form.elements.password.value;
  if (!password) fail(t('client.password_empty'));
  await api('POST', '/api/account/delete', { password });
  clearSessionUi();
  await loadSession();
});

// ---- Passports ----

// The open passport's detail and the user list for the open picker dialog.
// Both are dropped on logout (clearPassportUi).
let passport = null;
let pickUsers = [];
// The open country page's data, and the meal being edited or deleted.
let countryDetail = null;
let mealEdit = null;
let mealDelete = null;
// Next-stop toggles in flight, as `<passport id>:<code>`; their buttons stay disabled.
const nextStopBusy = new Set();
// Bumped by every applied toggle response and every re-read after a 409, so
// an older re-read can't overwrite newer state.
let nextStopGen = 0;

const ROLE_LABELS = Object.freeze({
  owner: () => t('role.owner'),
  participant: () => t('role.participant'),
  viewer: () => t('role.viewer'),
});
const roleLabel = (role) => (typeof role === 'string' && Object.hasOwn(ROLE_LABELS, role) ? ROLE_LABELS[role]() : '');
// Display names aren't unique, so a person is always named with @username too.
const personLabel = (p) => (p.inactive
  ? t('person.label_inactive', { name: p.display_name, username: p.username })
  : t('person.label', { name: p.display_name, username: p.username }));
const personRef = (u) => ({ user_id: u.id, username: u.username });
// People per passport, owner included (the server's limit).
const MAX_PEOPLE = 20;

// API paths are built only from an id that matches the id pattern.
function passportPath(id, suffix = '') {
  const s = String(id);
  if (!ID_RE.test(s)) fail(t('client.passport_not_found'), 'passport_not_found');
  return `/api/passports/${s}${suffix}`;
}

function clearPassportUi() {
  stopPick();
  lastPick = null;
  mealIntent = null;
  passport = null;
  pickUsers = [];
  countryDetail = null;
  mealEdit = null;
  mealDelete = null;
  nextStopBusy.clear();
  dropCountries();
  clearBrowse();
  resetMapUi();
  dialogToken += 1;
  for (const id of ['passport-list', 'participants-list', 'viewers-list', 'new-passport-people',
    'add-person-user', 'default-passport', 'delete-preview', 'country-list', 'country-participants', 'meal-list',
    'country-next-buttons', 'country-next-lines', 'map-card-next', 'pick-next']) {
    $(id).replaceChildren();
  }
  for (const id of ['passport-name', 'passport-owner', 'delete-passport-text', 'passport-confirm-text',
    'passport-progress', 'people-back', 'country-back', 'country-flag', 'country-name', 'country-region',
    'country-status', 'country-avg', 'meal-title', 'delete-meal-text', 'delete-meal-last', 'country-next-hint']) {
    $(id).textContent = '';
  }
  $('meal-form').reset();
  $('delete-meal-form').reset();
}

// ---- Passports: list ----

// inList: the list card's shorter wording for your own passports.
function ownerLine(p, inList) {
  const own = p.role === 'owner';
  if (p.is_personal && own) return inList ? t('passport.owner_personal_short') : t('passport.owner_personal');
  if (p.is_personal) return t('passport.owner_personal_of', { owner: personLabel(p.owner) });
  return own && inList ? t('passport.owner_you') : t('passport.owner_other', { owner: personLabel(p.owner) });
}

function passportCardHtml(p) {
  const badges = [];
  if (p.is_default) badges.push([t('passport.default_badge'), 'badge-accent']);
  const role = roleLabel(p.role);
  if (role) badges.push([role, '']);
  return `<li><button type="button" class="list-card" data-passport-id="${esc(String(p.id))}">` // i18n-ok HTML markup
    + `<span class="list-card-title">${esc(p.name)}</span>` // i18n-ok HTML markup
    + `<span class="list-card-sub"><span class="avatar-slot"></span>${esc(ownerLine(p, true))}</span>` // i18n-ok HTML markup
    + `<span class="badges">${badgesHtml(badges)}</span>` // i18n-ok HTML markup
    + '</button></li>';
}

async function enterPassports(_param, token) {
  showLoading();
  let list;
  try {
    list = await api('GET', '/api/passports');
  } catch (err) {
    if (token === routeToken) showLoadError(err.message);
    return;
  }
  if (token !== routeToken) return;
  $('passport-list').innerHTML = list.passports.map(passportCardHtml).join('');
  fillAvatarSlots($('passport-list'), list.passports.map((p) => p.owner), 'avatar-sm');
  showView('passports');
}

$('passport-list').addEventListener('click', (event) => {
  const card = event.target.closest('[data-passport-id]');
  if (!card) return;
  const id = card.dataset.passportId;
  if (ID_RE.test(id)) location.hash = `#/passports/${id}`;
});

// ---- Passports: new passport dialog ----

// Loads GET /api/users for a picker dialog; ignored if the dialog closed meanwhile.
async function loadPickUsers(form, status, onLoaded) {
  const dt = ++dialogToken;
  status.textContent = t('people.loading');
  status.hidden = false;
  let users;
  try {
    ({ users } = await api('GET', '/api/users'));
  } catch (err) {
    if (dt === dialogToken) {
      status.hidden = true;
      setError(form, err.message);
    }
    return;
  }
  if (dt !== dialogToken) return;
  pickUsers = users.filter((u) => ID_RE.test(String(u.id)));
  status.hidden = true;
  onLoaded(pickUsers);
}

function pickRowHtml(u) {
  const id = esc(String(u.id));
  const notAdded = t('people.not_added');
  const participant = t('role.participant');
  const viewer = t('role.viewer');
  return `<li class="pick-row"><span class="avatar-slot"></span><label for="new-passport-person-${id}">` // i18n-ok HTML markup
    + `<span class="person-name">${esc(u.display_name)}</span>` // i18n-ok HTML markup
    + `<span class="person-username">@${esc(u.username)}</span></label>` // i18n-ok HTML markup
    + `<select id="new-passport-person-${id}" data-user-id="${id}">` // i18n-ok HTML markup
    + `<option value="">${esc(notAdded)}</option>` // i18n-ok HTML markup
    + `<option value="participant">${esc(participant)}</option>` // i18n-ok HTML markup
    + `<option value="viewer">${esc(viewer)}</option>` // i18n-ok HTML markup
    + '</select></li>';
}

bindDialog('new-passport-open', 'new-passport-dialog', () => {
  const form = $('new-passport-form');
  const status = $('new-passport-status');
  $('new-passport-people').replaceChildren();
  loadPickUsers(form, status, (users) => {
    $('new-passport-people').innerHTML = users.map(pickRowHtml).join('');
    fillAvatarSlots($('new-passport-people'), users, 'avatar-sm');
    status.textContent = t('people.nobody_to_add');
    status.hidden = users.length > 0;
  });
});

bindForm($('new-passport-form'), async () => {
  const name = $('new-passport-name').value;
  checkPassportName(name);
  const participants = [];
  const viewers = [];
  for (const select of $('new-passport-people').querySelectorAll('select')) {
    const user = pickUsers.find((u) => String(u.id) === select.dataset.userId);
    if (!user) continue;
    if (select.value === 'participant') participants.push(personRef(user));
    else if (select.value === 'viewer') viewers.push(personRef(user));
  }
  // The owner is one of the people.
  if (participants.length + viewers.length > MAX_PEOPLE - 1) fail(t('error.people_limit', { max: MAX_PEOPLE }));
  const token = routeToken;
  const detail = await api('POST', '/api/passports', { name, participants, viewers });
  $('new-passport-dialog').close();
  toast(t('toast.passport_created'));
  const id = String(detail.passport.id);
  if (token === routeToken && ID_RE.test(id)) location.hash = `#/passports/${id}`;
});

// ---- Countries: the fixed list, fetched once per session ----

let countriesCache = null;
// Region names in the server's order, cached with the countries (filters and region sort).
let regionsCache = null;
// Bumped when the cache is dropped, so a load started before logout can't refill it.
let countriesGen = 0;

function dropCountries() {
  countriesCache = null;
  regionsCache = null;
  countriesGen += 1;
}

const regionList = () => regionsCache || [];

// Resolves to a Map of code -> {code, name, region}; only well-formed codes are kept.
async function getCountries() {
  if (countriesCache) return countriesCache;
  const gen = countriesGen;
  const data = await api('GET', '/api/countries');
  const list = (Array.isArray(data.countries) ? data.countries : [])
    .filter((c) => c && typeof c.code === 'string' && CODE_RE.test(c.code) && typeof c.name === 'string');
  const byCode = new Map(list.map((c) => [c.code, c]));
  const regions = (Array.isArray(data.regions) ? data.regions : []).filter((r) => typeof r === 'string');
  if (gen === countriesGen) {
    countriesCache = byCode;
    regionsCache = [...new Set(regions)];
  }
  return byCode;
}

// Two regional-indicator symbols; built only from a checked code, never server text.
function flagFor(code) {
  if (!CODE_RE.test(code)) return '';
  return String.fromCodePoint(...[...code].map((ch) => 0x1F1E6 + ch.charCodeAt(0) - 65));
}

const DAY_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

// Meal dates are calendar dates with no time zone. Formatting them in UTC
// keeps them from shifting by a day in negative-offset time zones.
function formatDay(iso) {
  if (typeof iso !== 'string' || !DAY_RE.test(iso)) return '';
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
}

// From local date parts: toISOString() would give the UTC date.
function localToday() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Whole stars from STARS_MIN to STARS_MAX (single digits, so one character class).
const STARS_MIN = 1;
const STARS_MAX = 5;
const STARS_RE = new RegExp(`^[${STARS_MIN}-${STARS_MAX}]$`);
const isStars = (r) => Number.isInteger(r) && r >= STARS_MIN && r <= STARS_MAX;

function starsEl(r) {
  const el = textEl('span', '★'.repeat(r) + '☆'.repeat(5 - r), 'stars');
  el.setAttribute('role', 'img');
  el.setAttribute('aria-label', tn('meal.stars_label', r));
  return el;
}

// ---- Passports: loading one passport (passport, people and country pages) ----

// Loads the passport detail together with the page's other requests (thunks
// in `more`). Returns their results, or null if the page was superseded or
// the load failed (the error is already shown).
async function loadPassport(id, token, more = []) {
  // The route regex already enforces this; re-checked because the id ends up in API paths.
  if (!ID_RE.test(id)) {
    replaceRoute('#/passports');
    return null;
  }
  passport = null;
  countryDetail = null;
  showLoading();
  let results;
  try {
    results = await Promise.all([api('GET', passportPath(id)), ...more.map((load) => load())]);
  } catch (err) {
    if (token === routeToken) showPassportLoadError(err, id);
    return null;
  }
  if (token !== routeToken) return null;
  const [detail, ...rest] = results;
  showPassport(detail);
  return rest;
}

function showPassportLoadError(err, id) {
  if (err.status === 404 && err.code === 'country_not_found') {
    toast(t('client.country_not_found'), 'error');
    replaceRoute(`#/passports/${id}`);
  } else if (err.status === 404) {
    toast(t('client.passport_not_found'), 'error');
    replaceRoute('#/passports');
  } else {
    showLoadError(err.message);
  }
}

// The query part of the address holds the search, sort and filters; it's
// validated against this passport and rewritten in canonical form.
async function enterPassport(id, token, _code, query) {
  resetSearch();
  const rest = await loadPassport(id, token, [() => api('GET', passportPath(id, '/countries')), getCountries]);
  if (!rest) return;
  const [summary, countries] = rest;
  browse = parseBrowse(query, id, browseOptions(passport.passport));
  // Zoom and pan are kept when coming back to the same passport, not for another one.
  if (mapPassportId !== id) {
    cancelMapAnim();
    clearMapPick();
    clearPins();
    resetMapView();
    mapPassportId = id;
  }
  showCountryList(summary, countries);
  showView('passport');
  showViewMode();
  updatePickButton();
  const hash = browseHash(browse);
  if (location.hash !== hash) history.replaceState(null, '', hash);
  if (browse.q.trim()) startSearch();
}

async function enterPeople(id, token) {
  if (!(await loadPassport(id, token))) return;
  showView('people');
}

// The code is checked against CODE_RE and the country list before it's used
// in an API path; an unknown one goes back to the passport. `intent` is a
// Log-a-meal request from the pick board (see render()).
async function enterCountry(id, token, code, _query, intent) {
  if (!ID_RE.test(id)) {
    replaceRoute('#/passports');
    return;
  }
  showLoading();
  let countries;
  try {
    countries = await getCountries();
  } catch (err) {
    if (token === routeToken) showLoadError(err.message);
    return;
  }
  if (token !== routeToken) return;
  if (!CODE_RE.test(code) || !countries.has(code)) {
    toast(t('client.country_not_found'), 'error');
    replaceRoute(`#/passports/${id}`);
    return;
  }
  const rest = await loadPassport(id, token, [() => api('GET', passportPath(id, `/countries/${code}`))]);
  if (!rest) return;
  showCountry(code, rest[0], countries);
  // Back returns to the list as it was searched and filtered (rebuilt from validated state).
  $('country-back').setAttribute('href', browse && browse.id === id ? browseHash(browse) : `#/passports/${id}`);
  showView('country');
  if (intent && intentStillValid(intent, token)) openMealDialog(null);
}

// Re-checked against what has just loaded: the role may have changed since the pick.
const intentStillValid = (intent, token) => token === routeToken && passport !== null
  && String(passport.passport.id) === intent.id && countryDetail !== null
  && countryDetail.code === intent.code && canLogMeals();

// ---- Passport page: progress and country list ----

// How a country's status shows, per display state: [list badge text, badge
// class, map class, map card text]. Fixed keys and classes only; the server's
// `status` is looked up, never copied. A null badge means no badge.
const DISPLAY_STATES = Object.freeze({
  not_started: Object.freeze([() => t('status.not_started'), 'badge-status-none', 'status-none',
    () => t('status.not_started')]),
  your_turn: Object.freeze([() => t('status.your_turn'), 'badge-status-your-turn', 'status-your-turn',
    () => t('status.your_turn')]),
  waiting: Object.freeze([() => t('status.waiting'), 'badge-status-waiting', 'status-waiting',
    () => t('status.waiting')]),
  some: Object.freeze([() => t('status.some'), 'badge-status-some', 'status-some', () => t('status.some_participants')]),
  complete: Object.freeze([() => t('status.complete'), 'badge-status-complete', 'status-complete',
    () => t('status.complete')]),
  // Personal passports: done or not.
  done: Object.freeze([() => t('status.done_badge'), 'badge-status-complete', 'status-complete', () => t('status.done')]),
  not_done: Object.freeze([null, '', 'status-none', () => t('status.not_done')]),
});
const SERVER_STATUSES = Object.freeze({ not_started: true, some: true, complete: true });

// The display state of a summary row (or null): participants of shared
// passports (opts.mine) split "some" by their own done_by_me into Your turn /
// Waiting on others; viewers see the three server states; personal passports
// done / not done.
function displayState(s, opts) {
  const status = s && typeof s.status === 'string' && Object.hasOwn(SERVER_STATUSES, s.status) ? s.status : null;
  if (!status) return null;
  if (opts.personal) return status === 'not_started' ? 'not_done' : 'done';
  if (!opts.mine || status !== 'some') return status;
  if (s.done_by_me === false) return 'your_turn';
  if (s.done_by_me === true) return 'waiting';
  return null;
}

const displayEntry = (s, opts) => {
  const key = displayState(s, opts);
  return key ? DISPLAY_STATES[key] : null;
};
const count = (n) => (Number.isInteger(n) && n >= 0 ? n : 0);
// A personal passport has one participant, so any meal means it's done.
const personalDone = (s) => s.status === 'some' || s.status === 'complete';

function progressText(progress, rows, personal) {
  const total = Number.isInteger(progress.total) ? progress.total : rows.length;
  // Counted from the rows so a viewer (mine is null) sees it too.
  if (personal) return t('progress.personal', { done: rows.filter(personalDone).length, total });
  const complete = count(progress.complete);
  if (Number.isInteger(progress.mine)) return t('progress.shared_mine', { complete, mine: count(progress.mine), total });
  return t('progress.shared', { complete, total });
}

function countryRowHtml(s, country, opts, hint) {
  const badges = [];
  const entry = displayEntry(s, opts);
  if (entry && entry[0]) badges.push([entry[0](), entry[1]]);
  return `<li><button type="button" class="country-row" data-code="${esc(country.code)}">` // i18n-ok HTML markup
    + `<span class="flag" aria-hidden="true">${esc(flagFor(country.code))}</span>` // i18n-ok HTML markup
    + `<span class="country-row-name">${esc(countryName(country.code, country.name))}</span>` // i18n-ok HTML markup
    // One fixed, attribute-free slot per row, filled by fillNextStopSlots (contract 11b §7.8).
    + `<span class="badges">${badgesHtml(badges)}<span class="next-stop-slot"></span></span>` // i18n-ok HTML markup
    + (hint ? `<span class="country-row-hint">${esc(hint)}</span>` : '') // i18n-ok HTML markup
    + '</button></li>';
}

// The progress line and the rows from GET .../countries (also after a next-stops conflict).
function setCountryRows(data, countries) {
  const personal = passport.passport.is_personal === true;
  const progress = data.progress && typeof data.progress === 'object' ? data.progress : {};
  const rows = (Array.isArray(data.countries) ? data.countries : [])
    .filter((s) => s && typeof s.code === 'string' && CODE_RE.test(s.code) && countries.has(s.code));
  $('passport-progress').textContent = progressText(progress, rows, personal);
  browseActive = progress.active_participants;
  browseRows = rows.map((s) => rowModel(s, countries.get(s.code), personal, browseActive));
}

function showCountryList(data, countries) {
  setCountryRows(data, countries);
  showBrowseControls();
  applyBrowse();
}

$('country-list').addEventListener('click', (event) => {
  const row = event.target.closest('[data-code]');
  if (row) openCountry(row.dataset.code);
});

// Shared by the list rows and the map card's Open button.
function openCountry(code) {
  if (!passport || !countriesCache) return;
  const id = String(passport.passport.id);
  // Text typed within the debounce would be lost by render(), so record it in
  // the address first: Back and the country page's back link then keep it.
  if (searchTimer) {
    clearTimeout(searchTimer);
    searchTimer = 0;
    if (onBrowsePage()) {
      browse.q = $('browse-q').value;
      writeBrowseAddress();
    }
  }
  if (CODE_RE.test(code) && countriesCache.has(code) && ID_RE.test(id)) {
    location.hash = `#/passports/${id}/countries/${code}`;
  }
}

// ---- Passport page: search, sort and filters (kept in the address) ----

const SORTS = ['name', 'region', 'completed_desc', 'completed_asc', 'rating', 'progress'];
// Status filter values: participants of shared passports, viewers, personal.
const PARTICIPANT_STATUS_KEYS = ['not_started', 'your_turn', 'waiting', 'complete'];
const VIEWER_STATUS_KEYS = ['not_started', 'some', 'complete'];
const PERSONAL_STATUS_KEYS = ['not_started', 'complete'];
const MINE_KEYS = ['done', 'not_done'];
// Next stops filter values (`next=`) per passport kind and role (contract 11b §7.6).
const NEXT_SHARED_PARTICIPANT = Object.freeze(['mine', 'ours', 'anyone']);
const NEXT_SHARED_VIEWER = Object.freeze(['ours', 'anyone']);
const NEXT_PERSONAL_OWNER = Object.freeze(['mine']);
const NEXT_PERSONAL_VIEWER = Object.freeze(['anyone']);
const MAX_QUERY = 200;

// The open passport list's validated state ({id, opts, q, sort, status, mine,
// nextStops, region, stars, view}), its rows, the active participant count
// they were built with, and the search results. Dropped by clearBrowse().
let browse = null;
let browseRows = [];
let browseActive = 0;
let search = { on: false, pending: false, matches: null };
// Bumped by every new search (and on clearing), so older responses are dropped.
let searchToken = 0;
let searchTimer = 0;

// Which parameters apply depends on the passport: personal passports have two
// statuses, and only participants of shared passports have "mine".
function browseOptions(p) {
  const personal = p.is_personal === true;
  const participant = p.role === 'owner' || p.role === 'participant';
  const mine = !personal && participant;
  let nextStops = participant ? NEXT_SHARED_PARTICIPANT : NEXT_SHARED_VIEWER;
  if (personal) nextStops = p.role === 'owner' ? NEXT_PERSONAL_OWNER : NEXT_PERSONAL_VIEWER;
  return {
    personal,
    statuses: personal ? PERSONAL_STATUS_KEYS : (mine ? PARTICIPANT_STATUS_KEYS : VIEWER_STATUS_KEYS),
    mine,
    nextStops,
  };
}

// The status filter from the address: for participants an old "some" means
// Your turn and Waiting on others; anything not valid for the role is dropped.
function statusList(raw, opts) {
  if (raw === null) return [];
  let parts = raw.split(',');
  if (opts.mine) parts = parts.flatMap((v) => (v === 'some' ? ['your_turn', 'waiting'] : [v]));
  return opts.statuses.filter((v) => parts.includes(v));
}

// The allowed values present in a comma-separated list, in the allowed order.
function pickList(raw, allowed) {
  if (raw === null) return [];
  const parts = new Set(raw.split(','));
  return allowed.filter((v) => parts.has(v));
}

// Each parameter is used only if it appears once and is valid; anything
// else is ignored, never an error.
function parseBrowse(query, id, opts) {
  const params = new URLSearchParams(query || '');
  const one = (key) => (params.getAll(key).length === 1 ? params.get(key) : null);
  const q = one('q');
  const sort = one('sort');
  const mine = one('mine');
  const next = one('next');
  const stars = one('stars');
  const view = one('view');
  const pins = one('pins');
  return {
    id,
    opts,
    q: q !== null && codePoints(q) <= MAX_QUERY ? q : '',
    sort: SORTS.includes(sort) ? sort : 'name',
    status: statusList(one('status'), opts),
    mine: opts.mine && MINE_KEYS.includes(mine) ? mine : '',
    nextStops: opts.nextStops.includes(next) ? next : '',
    region: pickList(one('region'), regionList()),
    stars: stars !== null && STARS_RE.test(stars) ? Number(stars) : 0,
    view: view === 'list' ? 'list' : 'map',
    // Only an exact pins=0 turns them off; not a filter (no count, not cleared by Clear).
    pins: pins !== '0',
  };
}

const validQuery = (b) => b.q.trim() !== '' && codePoints(b.q) <= MAX_QUERY;
const filterCount = (b) => [b.status.length, b.mine, b.nextStops, b.region.length, b.stars].filter(Boolean).length;

// The canonical address, built only from validated values; defaults are omitted.
function browseHash(b) {
  const id = String(b.id);
  if (!ID_RE.test(id)) return '#/passports';
  const params = new URLSearchParams();
  if (validQuery(b)) params.set('q', b.q);
  if (b.sort !== 'name') params.set('sort', b.sort);
  if (b.status.length) params.set('status', b.status.join(','));
  if (b.mine) params.set('mine', b.mine);
  if (b.nextStops) params.set('next', b.nextStops);
  if (b.region.length) params.set('region', b.region.join(','));
  if (b.stars) params.set('stars', String(b.stars));
  if (b.view === 'list') params.set('view', 'list');
  if (!b.pins) params.set('pins', '0');
  const query = params.toString();
  return query ? `#/passports/${id}?${query}` : `#/passports/${id}`;
}

const onBrowsePage = () => browse !== null && currentView === 'passport'
  && splitHash(location.hash)[0] === `#/passports/${browse.id}`;

// Records the state in the address without a history entry (and without a hashchange).
function writeBrowseAddress() {
  if (!onBrowsePage()) return;
  const hash = browseHash(browse);
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

// Sort and filter inputs, checked once per load.
function rowModel(s, country, personal, active) {
  const doneCount = Number.isInteger(s.done_count) && s.done_count >= 0 ? s.done_count : 0;
  let progress = Number.isInteger(active) && active > 0 ? doneCount / active : 0;
  if (personal) progress = personalDone(s) ? 1 : 0;
  return {
    s,
    country,
    code: s.code,
    name: country.name,
    region: typeof country.region === 'string' ? country.region : '',
    rating: typeof s.avg_rating === 'number' && Number.isFinite(s.avg_rating) ? s.avg_rating : null,
    completedOn: typeof s.completed_on === 'string' && DAY_RE.test(s.completed_on) ? s.completed_on : null,
    progress,
    nextPeople: nextStopPeople(s.next_stop_user_ids),
  };
}

function nextStopMatches(r, value) {
  if (value === 'mine') return r.s.my_next_stop === true;
  if (value === 'ours') return r.s.shared_next_stop === true;
  return r.nextPeople.length > 0 || r.s.shared_next_stop === true;
}

// Filter values match display states, except on personal passports, where
// the Done / Not done states are filtered as complete / not_started.
const PERSONAL_FILTER_KEYS = Object.freeze({ done: 'complete', not_done: 'not_started' });

function statusMatches(r, status, opts) {
  const key = displayState(r.s, opts);
  if (!key) return false;
  if (opts.personal) return Object.hasOwn(PERSONAL_FILTER_KEYS, key) && PERSONAL_FILTER_KEYS[key] === status;
  return key === status;
}

function rowShown(r, b) {
  if (search.on && !(search.matches && search.matches.has(r.code))) return false;
  if (b.status.length && !b.status.some((st) => statusMatches(r, st, b.opts))) return false;
  if (b.mine && (r.s.done_by_me === true) !== (b.mine === 'done')) return false;
  if (b.nextStops && !nextStopMatches(r, b.nextStops)) return false;
  if (b.region.length && !b.region.includes(r.region)) return false;
  return !b.stars || (r.rating !== null && r.rating >= b.stars);
}

const byName = (a, b) => a.name.localeCompare(b.name, 'en', { sensitivity: 'base' });

// Orders by one value; rows without a value (null) go last in either direction.
function byValue(get, desc) {
  return (a, b) => {
    const x = get(a);
    const y = get(b);
    if (x === null || y === null) return (x === null) - (y === null);
    if (x === y) return 0;
    return (x < y) === desc ? 1 : -1;
  };
}

function sortCompare(sort) {
  const regions = regionList();
  const regionIndex = (r) => (regions.includes(r.region) ? regions.indexOf(r.region) : null);
  switch (sort) {
    case 'region': return byValue(regionIndex, false);
    case 'completed_desc': return byValue((r) => r.completedOn, true);
    case 'completed_asc': return byValue((r) => r.completedOn, false);
    case 'rating': return byValue((r) => r.rating, true);
    case 'progress': return byValue((r) => r.progress, true);
    default: return () => 0;
  }
}

// Region headings are plain list items (not buttons) and carry no data attributes.
// A region missing from the region list sorts last and gets no heading.
function countryListHtml(rows, b) {
  const regions = regionList();
  let html = '';
  let region = null;
  for (const r of rows) {
    if (b.sort === 'region' && r.region !== region) {
      region = r.region;
      if (regions.includes(region)) html += `<li class="region-heading"><h3>${esc(regionName(region))}</h3></li>`; // i18n-ok HTML markup
    }
    const hint = search.on && search.matches ? search.matches.get(r.code) : '';
    html += countryRowHtml(r.s, r.country, b.opts, hint);
  }
  return html;
}

// The rows the list shows, in its order (also the random pick's starting point).
function visibleRows() {
  if (!browse) return [];
  const b = browse;
  const compare = sortCompare(b.sort);
  // Ties fall back to name A-Z.
  return browseRows.filter((r) => rowShown(r, b)).sort((x, y) => compare(x, y) || byName(x, y));
}

function applyBrowse() {
  if (!browse) return;
  const b = browse;
  const rows = visibleRows();
  // The map is the one place the map is coloured; the hidden list isn't built.
  if (b.view === 'map') {
    $('country-list').replaceChildren();
    colourMap(rows);
  } else {
    $('country-list').innerHTML = countryListHtml(rows, b);
    fillNextStopSlots($('country-list'), rows);
  }
  const n = filterCount(b);
  $('filters-open').textContent = n ? t('browse.filters_count', { count: n }) : t('browse.filters');
  $('browse-count-row').hidden = !(b.q.trim() || n);
  $('browse-count').textContent = search.pending ? t('browse.searching')
    : t('browse.showing', { shown: rows.length, total: browseRows.length });
  $('browse-empty').hidden = search.pending || rows.length > 0;
  // A "nothing eligible" message described the list before this change.
  setPickEmpty('');
  updatePickButton();
}

// Sets the bar's controls and labels from the state (personal passports say "Done").
function showBrowseControls() {
  const personal = browse.opts.personal;
  $('browse-q').value = browse.q;
  $('browse-sort').value = browse.sort;
  $('sort-completed-desc').textContent = personal ? t('browse.sort_done_desc') : t('browse.sort_completed_desc');
  $('sort-completed-asc').textContent = personal ? t('browse.sort_done_asc') : t('browse.sort_completed_asc');
  $('pick-open').hidden = !canLogMeals();
  showSearchError('');
}

function showSearchError(message) {
  $('browse-error').textContent = message;
  $('browse-error').hidden = !message;
}

// Stops any pending or in-flight search; its result will be ignored.
function resetSearch() {
  clearTimeout(searchTimer);
  searchTimer = 0;
  searchToken += 1;
  search = { on: false, pending: false, matches: null };
}

function clearBrowse() {
  resetSearch();
  browse = null;
  browseRows = [];
  browseActive = 0;
  $('browse-q').value = '';
  $('browse-sort').value = 'name';
  $('filters-open').textContent = t('browse.filters');
  $('browse-count').textContent = '';
  $('browse-count-row').hidden = true;
  $('browse-empty').hidden = true;
  $('filters-regions').replaceChildren();
  $('pick-open').hidden = true;
  setPickEmpty('');
  showSearchError('');
}

// Hint lines explain a match found in a meal rather than the name. Only string
// fields are used; the prefixes are fixed text.
function hintText(meal) {
  if (!meal || typeof meal !== 'object') return '';
  const str = (v) => (typeof v === 'string' ? v : '');
  const notes = str(meal.notes_excerpt);
  if (notes) return t('search.hint_notes', { notes });
  const dish = str(meal.dish);
  const place = str(meal.place);
  if (dish && place) return t('search.hint_dish_place', { dish, place });
  if (dish) return t('search.hint_dish', { dish });
  return place ? t('search.hint_place', { place }) : '';
}

// code -> hint text ('' for a name match); unknown or malformed codes are ignored.
function parseMatches(data) {
  const matches = new Map();
  for (const m of Array.isArray(data.matches) ? data.matches : []) {
    if (m && typeof m.code === 'string' && CODE_RE.test(m.code) && countriesCache && countriesCache.has(m.code)) {
      matches.set(m.code, hintText(m.meal));
    }
  }
  return matches;
}

// Searches for browse.q. While a request is in flight the previous results stay
// listed (none on first load) and the count line says "Searching…".
async function startSearch() {
  const st = ++searchToken;
  const { q } = browse;
  showSearchError('');
  if (!q.trim() || codePoints(q) > MAX_QUERY) {
    search = { on: false, pending: false, matches: null };
    if (q.trim()) showSearchError(t('error.search_length', { max: MAX_QUERY }));
    applyBrowse();
    return;
  }
  search = { on: true, pending: true, matches: search.on ? search.matches : null };
  applyBrowse();
  const token = routeToken;
  let data;
  try {
    data = await api('POST', passportPath(browse.id, '/search'), { q });
  } catch (err) {
    // A response for a page the user has left is ignored, errors included.
    if (st !== searchToken || token !== routeToken) return;
    if (err.status === 404 && err.code === 'passport_not_found') {
      toast(err.message, 'error');
      replaceRoute('#/passports');
      return;
    }
    search = { on: false, pending: false, matches: null };
    showSearchError(err.message);
    applyBrowse();
    return;
  }
  if (st !== searchToken || token !== routeToken || !browse) return;
  search = { on: true, pending: false, matches: parseMatches(data) };
  applyBrowse();
}

$('browse-q').addEventListener('input', () => {
  clearTimeout(searchTimer);
  const token = routeToken;
  searchTimer = setTimeout(() => {
    searchTimer = 0;
    // A render since typing (cleared timers aside) or another page: do nothing.
    if (token !== routeToken || !onBrowsePage()) return;
    browse.q = $('browse-q').value;
    startSearch();
    writeBrowseAddress();
  }, 300);
  // The list doesn't yet match the box, so no picking until the search has run.
  updatePickButton();
});

$('browse-sort').addEventListener('change', () => {
  const sort = $('browse-sort').value;
  if (!onBrowsePage() || !SORTS.includes(sort)) return;
  browse.sort = sort;
  applyBrowse();
  writeBrowseAddress();
});

$('browse-clear').addEventListener('click', () => {
  if (!onBrowsePage()) return;
  resetSearch();
  Object.assign(browse, { q: '', sort: 'name', status: [], mine: '', nextStops: '', region: [], stars: 0 });
  showBrowseControls();
  applyBrowse();
  writeBrowseAddress();
  $('browse-q').focus();
});

// ---- Passport page: filters dialog (applied live) ----

const filtersDialog = $('filters-dialog');

function regionCheck(region) {
  const label = document.createElement('label');
  label.className = 'check';
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.name = 'region';
  input.value = region;
  input.checked = browse.region.includes(region);
  label.append(input, ` ${regionName(region)}`);
  return label;
}

function syncFilters() {
  const form = $('filters-form');
  const { opts } = browse;
  $('filter-status-none').textContent = opts.personal ? t('status.not_done') : t('status.not_started');
  $('filter-status-complete').textContent = opts.personal ? t('status.done') : t('status.complete');
  for (const box of form.querySelectorAll('input[name="status"]')) {
    box.closest('label').hidden = !opts.statuses.includes(box.value);
    box.checked = browse.status.includes(box.value);
  }
  $('filters-mine').hidden = !opts.mine;
  for (const radio of form.querySelectorAll('input[name="mine"]')) radio.checked = radio.value === browse.mine;
  // Personal passports have one list, so its option names it in full.
  $('filter-next-mine').textContent = opts.personal ? t('filters.next_mine_personal') : t('filters.next_mine');
  $('filter-next-anyone').textContent = opts.personal ? t('filters.next_anyone_personal') : t('filters.next_anyone');
  for (const radio of form.querySelectorAll('input[name="next"]')) {
    radio.closest('label').hidden = radio.value !== '' && !opts.nextStops.includes(radio.value);
    radio.checked = radio.value === browse.nextStops;
  }
  $('filters-region-group').hidden = regionList().length === 0;
  $('filters-regions').replaceChildren(...regionList().map(regionCheck));
  $('filters-stars').value = browse.stars ? String(browse.stars) : '';
}

const checkedValues = (form, name) => new Set([...form.querySelectorAll(`input[name="${name}"]:checked`)]
  .map((input) => input.value));

function readFilters() {
  const form = $('filters-form');
  const status = checkedValues(form, 'status');
  const regions = checkedValues(form, 'region');
  const mine = form.querySelector('input[name="mine"]:checked')?.value;
  const next = form.querySelector('input[name="next"]:checked')?.value;
  const stars = $('filters-stars').value;
  browse.status = browse.opts.statuses.filter((v) => status.has(v));
  browse.mine = browse.opts.mine && MINE_KEYS.includes(mine) ? mine : '';
  browse.nextStops = browse.opts.nextStops.includes(next) ? next : '';
  browse.region = regionList().filter((r) => regions.has(r));
  browse.stars = STARS_RE.test(stars) ? Number(stars) : 0;
}

$('filters-open').addEventListener('click', () => {
  if (!onBrowsePage()) return;
  syncFilters();
  filtersDialog.showModal();
});

$('filters-form').addEventListener('change', () => {
  if (!onBrowsePage()) return;
  readFilters();
  applyBrowse();
  writeBrowseAddress();
});

$('filters-clear').addEventListener('click', () => {
  if (!onBrowsePage()) return;
  Object.assign(browse, { status: [], mine: '', nextStops: '', region: [], stars: 0 });
  syncFilters();
  applyBrowse();
  writeBrowseAddress();
});

$('filters-form').addEventListener('submit', (event) => {
  event.preventDefault();
  filtersDialog.close();
});

// ---- Passport page: world map ----

// The map file is fetched once per page load and validated; the SVG holds only
// svg/g/path/circle elements whose attributes are validated path data, checked
// codes, app-computed numbers and fixed class names. All colour is in CSS.
const MAP_PATH_RE = /^[MLZ0-9.,\-]+$/;
const MAP_MAX_PATH = 200000;
const MAP_MAX_COUNTRIES = 197;
const MAP_MAX_OTHER = 500;
const MAP_MAX_SCALE = 12;
const MAP_STEP = 1.5;
const MAP_DOT_PX = 4;
const MAP_TAP_MS = 300;
const MAP_TAP_PX = 8;
// A dot is only 8 px across, so a tap this close to one counts as a tap on it.
const MAP_DOT_REACH_PX = 14;

const mapSvg = $('map-svg');
// Parsed map.json ({width, height, countries, dots, other}); dropped on logout.
let mapData = null;
let mapBuilt = false;
// Bumped by resetMapUi() (logout); a map.json response from an older generation is discarded.
let mapGen = 0;
// Identifies the load in flight (generation, route and passport), so a stale one doesn't block a new one.
let mapFetchKey = '';
// Coloured shapes ({el, code, base}) and dots ({el, code, x, y}).
let mapShapes = [];
let mapDots = [];
// The visible part of the map in map units ({x, y, w, h}).
let mapView = null;
let mapPassportId = null;
// Pointers currently down on the map (id -> {x, y}) and the gesture they form.
const mapPointers = new Map();
let mapGesture = null;
let mapCardCode = null;
// The pending pointermove frame, and the codes shown by the last colouring
// that wasn't waiting for a search ({id, codes}).
let mapMoveFrame = 0;
let mapShown = null;
// Next stops pins (contract 11c): the layer, its drawn groups ({el, code, x, y},
// southern last), the key of the drawn pin data ('' if none), pin data waiting
// for a usable scale ({key, entries}), and the codes left undimmed.
let mapPinsLayer = null;
let mapPinGroups = [];
let mapPinsKey = '';
let mapPinsWanted = null;
let mapPinsShown = null;

function svgEl(tag, cls) {
  const el = document.createElementNS(SVG_NS, tag);
  if (cls) el.setAttribute('class', cls);
  return el;
}

const isPlainObject = (v) => v !== null && typeof v === 'object' && Object.getPrototypeOf(v) === Object.prototype;
const mapSize = (n) => typeof n === 'number' && Number.isFinite(n) && n >= 1 && n <= 10000;
const mapPath = (d) => typeof d === 'string' && d.length > 0 && d.length <= MAP_MAX_PATH && MAP_PATH_RE.test(d);
const finiteIn = (n, lo, hi) => typeof n === 'number' && Number.isFinite(n) && n >= lo && n <= hi;

// Returns the usable parts of map.json, or null if it can't be drawn. Bad
// entries are skipped; only codes in the country list are kept.
function parseMap(json) {
  if (!isPlainObject(json) || !mapSize(json.width) || !mapSize(json.height)) return null;
  if (!isPlainObject(json.countries) || !countriesCache) return null;
  const { width, height } = json;
  const countries = new Map();
  for (const code of Object.keys(json.countries)) {
    if (countries.size >= MAP_MAX_COUNTRIES) break;
    if (!CODE_RE.test(code) || !countriesCache.has(code)) continue;
    const d = json.countries[code];
    if (mapPath(d)) countries.set(code, d);
  }
  if (countries.size === 0) return null;
  const dots = new Map();
  for (const code of isPlainObject(json.dots) ? Object.keys(json.dots) : []) {
    if (!CODE_RE.test(code) || !countries.has(code)) continue;
    const p = json.dots[code];
    if (Array.isArray(p) && p.length === 2 && finiteIn(p[0], 0, width) && finiteIn(p[1], 0, height)) dots.set(code, [p[0], p[1]]);
  }
  const other = Array.isArray(json.other) ? json.other.slice(0, MAP_MAX_OTHER).filter(mapPath) : [];
  return { width, height, countries, dots, other, anchors: parseAnchors(json.anchors, countries, width, height) };
}

// Pin anchors ({code: [x, y]}, contract 11c §2): a bad or missing entry just means no pin there.
function parseAnchors(raw, countries, width, height) {
  const anchors = new Map();
  for (const code of isPlainObject(raw) ? Object.keys(raw) : []) {
    if (anchors.size >= MAP_MAX_COUNTRIES) break;
    if (!CODE_RE.test(code) || !countries.has(code)) continue;
    const p = raw[code];
    if (Array.isArray(p) && p.length === 2 && finiteIn(p[0], -1, width + 1) && finiteIn(p[1], -1, height + 1)) {
      anchors.set(code, [p[0], p[1]]);
    }
  }
  return anchors;
}

function showMapStatus(message, failed = false) {
  const el = $('map-status');
  el.textContent = message;
  el.hidden = !message;
  el.classList.toggle('form-error', failed);
  $('map-retry').hidden = !failed;
}

// Not api(): its 401 handling is for /api/ requests.
async function loadMap() {
  const gen = mapGen;
  const token = routeToken;
  const id = browse.id;
  const key = `${gen}:${token}:${id}`;
  if (mapFetchKey === key) return;
  mapFetchKey = key;
  showMapStatus(t('map.loading'));
  let json = null;
  try {
    const res = await fetch('/map.json', { credentials: 'same-origin' });
    if (res.ok) json = await res.json();
  } catch {
    json = null;
  }
  if (mapFetchKey === key) mapFetchKey = '';
  // Stale: logged out, another page, or another passport since the request.
  if (gen !== mapGen || token !== routeToken || !browse || browse.id !== id) return;
  const parsed = parseMap(json);
  if (!parsed) {
    showMapStatus(t('map.load_failed'), true);
    return;
  }
  mapData = parsed;
  if (onBrowsePage() && browse.view === 'map') {
    ensureMap();
    applyBrowse();
  }
}

$('map-retry').addEventListener('click', () => {
  if (onBrowsePage() && browse.view === 'map') loadMap();
});

function buildMap() {
  const other = svgEl('g');
  for (const d of mapData.other) {
    const path = svgEl('path', 'map-other');
    path.setAttribute('d', d);
    other.append(path);
  }
  const lands = svgEl('g');
  for (const [code, d] of mapData.countries) {
    const path = svgEl('path', 'map-country');
    path.setAttribute('d', d);
    path.setAttribute('data-code', code);
    lands.append(path);
    mapShapes.push({ el: path, code, base: 'map-country' });
  }
  const dots = svgEl('g');
  for (const [code, [x, y]] of mapData.dots) {
    const dot = svgEl('circle', 'map-dot');
    dot.setAttribute('cx', String(x));
    dot.setAttribute('cy', String(y));
    dot.setAttribute('data-code', code);
    dots.append(dot);
    mapShapes.push({ el: dot, code, base: 'map-dot' });
    mapDots.push({ el: dot, code, x, y });
  }
  // Pins go last, above every country and dot.
  mapPinsLayer = svgEl('g', 'map-pins');
  mapPinsLayer.setAttribute('aria-hidden', 'true');
  mapPinsLayer.classList.toggle('map-pins-off', !!browse && !browse.pins);
  clearPins();
  mapSvg.replaceChildren(other, lands, dots, mapPinsLayer);
  mapBuilt = true;
  resetMapView();
}

// Shows the map (building it, or loading the file first). Colouring is
// applyBrowse()'s job; a pick made before the map was built is zoomed to here.
function ensureMap() {
  if (!mapBuilt && mapData) {
    buildMap();
    jumpToEarlyPick();
  }
  $('map-body').hidden = !mapBuilt;
  if (!mapBuilt) {
    loadMap();
    return;
  }
  showMapStatus('');
  updateDotRadius();
}

// The switch and which of list, Sort and map are shown (also used by the reset).
function setViewUi(map) {
  $('view-list').setAttribute('aria-pressed', String(!map));
  $('view-map').setAttribute('aria-pressed', String(map));
  $('browse-sort-field').hidden = map;
  $('country-list').hidden = map;
  $('map-section').hidden = !map;
}

// The List / Map switch, the controls that differ between the two, and the
// legend. Colouring is left to applyBrowse().
function showViewMode() {
  const map = browse.view === 'map';
  setViewUi(map);
  const { personal, mine } = browse.opts;
  $('legend-none').textContent = personal ? t('status.not_done') : t('status.not_started');
  $('legend-complete').textContent = personal ? t('status.done') : t('status.complete');
  $('legend-some').hidden = personal || mine;
  $('legend-your-turn').hidden = !mine;
  $('legend-waiting').hidden = !mine;
  showPinsUi();
  if (map) ensureMap();
}

$('view-switch').addEventListener('click', (event) => {
  const button = event.target.closest('[data-view]');
  if (!button || !onBrowsePage()) return;
  const view = button.dataset.view === 'map' ? 'map' : 'list';
  if (browse.view === view) return;
  browse.view = view;
  closeMapCard();
  cancelMapAnim();
  clearMapPick();
  resetMapView();
  showViewMode();
  applyBrowse();
  writeBrowseAddress();
});

// Classes from fixed literals only (the display-state table).
function mapClass(base, row, shown, opts, picked) {
  const cls = [base];
  const entry = row ? displayEntry(row.s, opts) : null;
  if (entry) cls.push(entry[2]);
  if (!shown) cls.push('map-dim');
  if (picked) cls.push('map-picked');
  return cls.join(' ');
}

// The codes to leave undimmed: the list's rows, except while a search is in
// flight, when the last settled set for this passport is kept (none dimmed if
// there isn't one) instead of dimming everything.
function mapShownCodes(rows) {
  if (!search.pending) {
    mapShown = { id: browse.id, codes: new Set(rows.map((r) => r.code)) };
    return mapShown.codes;
  }
  if (mapShown && mapShown.id === browse.id) return mapShown.codes;
  return new Set(browseRows.map((r) => r.code));
}

// The picked country's shapes are moved to the end of their group so
// neighbours drawn later don't cover its highlight.
function colourMap(rows) {
  if (!mapBuilt) return;
  if (!browse) {
    clearPins();
    return;
  }
  const byCode = new Map(browseRows.map((r) => [r.code, r]));
  const shown = mapShownCodes(rows);
  const pickedCode = mapPicked && mapPicked.id === browse.id ? mapPicked.code : null;
  const picked = [];
  for (const { el, code, base } of mapShapes) {
    el.setAttribute('class', mapClass(base, byCode.get(code), shown.has(code), browse.opts, code === pickedCode));
    if (code === pickedCode) picked.push(el);
  }
  for (const el of picked) el.parentNode.append(el);
  updatePins(shown);
  mapSvg.setAttribute('aria-label', passport ? t('map.label_named', { name: passport.passport.name }) : t('map.label'));
}

// ---- World map: Next stops pins (contract 11c §3, §7) ----

// Screen pixels inside a pin group (scaled by map units per pixel).
const PIN_HEAD_R = 10;
const PIN_HEAD_Y = -24;
const PIN_HEAD_STEP = 13;
const PIN_ICON = 12;
const PIN_MAX_HEADS = 3;
// A pin whose anchor is closer than this to the top of the frame hangs below
// it instead (head top, rim included, is about 35 px above the anchor).
const PIN_FLIP_PX = 36;
// Flipped: the needles mirrored (lines only) and the heads moved below the
// anchor; avatars and "+N" are moved, never mirrored.
const PIN_FLIP_NEEDLES = 'scale(1 -1)';
const PIN_FLIP_HEADS = `translate(0 ${-2 * PIN_HEAD_Y})`;

const validMapCode = (code) => typeof code === 'string' && CODE_RE.test(code) && countriesCache !== null
  && countriesCache.has(code) && mapData !== null && mapData.countries.has(code);

// Map units per screen pixel, or null when the map isn't laid out.
function pinScale(rect = mapRect()) {
  if (!rect || !mapView) return null;
  const k = mapView.w / rect.width;
  return Number.isFinite(k) && k > 0 ? k : null;
}

const pinTransform = (g, k) => `translate(${g.x} ${g.y}) scale(${k})`;

function clearPins() {
  mapPinGroups = [];
  mapPinsKey = '';
  mapPinsWanted = null;
  if (mapPinsLayer) mapPinsLayer.replaceChildren();
}

// Shared first, then me, then the others in the order given. People come only
// from the row (active participants); personal passports have no shared list.
function pinHeads(r, personal) {
  const me = state.user ? state.user.id : null;
  const heads = [];
  if (!personal && r.s.shared_next_stop === true) heads.push({ shared: true });
  for (const p of r.nextPeople) if (p.id === me) heads.push({ person: p });
  for (const p of r.nextPeople) if (p.id !== me) heads.push({ person: p });
  return heads;
}

// One entry per country with a pin, northern first (so southern pins are drawn over them).
function pinEntries() {
  const entries = [];
  for (const r of browseRows) {
    const at = mapData.anchors.get(r.code);
    if (!at || !validMapCode(r.code)) continue;
    const heads = pinHeads(r, browse.opts.personal);
    if (heads.length) entries.push({ code: r.code, x: at[0], y: at[1], heads });
  }
  return entries.sort((a, b) => a.y - b.y || (a.code < b.code ? -1 : 1));
}

// Identifies what the pins show, so they're rebuilt only when it changes (in memory only).
const headKey = (h) => (h.person ? `${h.person.id}/${h.person.avatar_seed}` : 'shared');
const pinsKey = (entries) => `${browse.id}|${entries.map((e) => `${e.code}:${e.heads.map(headKey).join(',')}`).join(';')}`;

function setNumbers(el, attrs) {
  for (const [name, n] of attrs) el.setAttribute(name, String(n));
}

function pinCircle(cls, hx) {
  const c = svgEl('circle', cls);
  setNumbers(c, [['cx', hx], ['cy', PIN_HEAD_Y], ['r', PIN_HEAD_R]]);
  return c;
}

// A head's elements: the avatar, the shared pin on an accent disc, or "+N"; then the rim.
function pinHead(head, hx) {
  const box = [['x', hx - PIN_HEAD_R], ['y', PIN_HEAD_Y - PIN_HEAD_R], ['width', 2 * PIN_HEAD_R], ['height', 2 * PIN_HEAD_R]];
  const parts = [];
  if (head.person) {
    const avatar = avatarEl(head.person.avatar_seed, 'avatar-pin');
    setNumbers(avatar, box);
    parts.push(avatar);
  } else if (head.shared) {
    const icon = pinEl('map-pin-icon');
    const half = PIN_ICON / 2;
    setNumbers(icon, [['x', hx - half], ['y', PIN_HEAD_Y - half], ['width', PIN_ICON], ['height', PIN_ICON]]);
    parts.push(pinCircle('map-pin-shared', hx), icon);
  } else {
    const text = svgEl('text', 'map-pin-more-text');
    setNumbers(text, [['x', hx], ['y', PIN_HEAD_Y]]);
    text.textContent = t('map.pins_more', { n: head.more });
    parts.push(pinCircle('map-pin-more', hx), text);
  }
  parts.push(pinCircle('map-pin-rim', hx));
  return parts;
}

// One country's pin: shadow, needles, then heads left to right (each overlapping the last).
// Returns the group and its needle and head groups (for flipPin()).
function pinGroup(entry, k) {
  const g = svgEl('g', 'map-pin');
  g.setAttribute('data-code', entry.code);
  g.setAttribute('transform', pinTransform(entry, k));
  const shadow = svgEl('ellipse', 'map-pin-shadow');
  setNumbers(shadow, [['cx', 0], ['cy', 0], ['rx', 4], ['ry', 1.5]]);
  g.append(shadow);
  const all = entry.heads;
  const heads = all.length > PIN_MAX_HEADS ? [all[0], all[1], { more: all.length - 2 }] : all;
  const xs = heads.map((_, i) => (i - (heads.length - 1) / 2) * PIN_HEAD_STEP);
  const needles = svgEl('g');
  for (const hx of xs) {
    const needle = svgEl('line', 'map-pin-needle');
    setNumbers(needle, [['x1', hx], ['y1', PIN_HEAD_Y + 9], ['x2', 0], ['y2', 0]]);
    needles.append(needle);
  }
  const top = svgEl('g');
  heads.forEach((head, i) => top.append(...pinHead(head, xs[i])));
  g.append(needles, top);
  return { el: g, needles, heads: top };
}

// Hangs a pin below its anchor near the top of the frame, and back above
// once there's room. Attributes change only when the state does.
function flipPin(g, rect) {
  const flip = rect.height * ((g.y - mapView.y) / mapView.h) < PIN_FLIP_PX;
  if (flip === g.flipped) return;
  g.flipped = flip;
  g.el.classList.toggle('map-pin-flipped', flip);
  if (flip) {
    g.needles.setAttribute('transform', PIN_FLIP_NEEDLES);
    g.heads.setAttribute('transform', PIN_FLIP_HEADS);
  } else {
    g.needles.removeAttribute('transform');
    g.heads.removeAttribute('transform');
  }
}

// On pan (the zoom goes through scalePins): one comparison per pin.
function flipPins(rect = mapRect()) {
  if (!rect || !mapView) return;
  for (const g of mapPinGroups) flipPin(g, rect);
}

// Draws the waiting pin data, if the map has a usable scale; otherwise the
// layer stays empty until the next updateDotRadius().
function drawPins(rect) {
  const wanted = mapPinsWanted;
  if (!wanted || !mapPinsLayer) return;
  const k = pinScale(rect);
  mapPinGroups = [];
  mapPinsKey = '';
  if (k === null) {
    mapPinsLayer.replaceChildren();
    return;
  }
  mapPinGroups = wanted.entries.map((e) => ({ ...pinGroup(e, k), code: e.code, x: e.x, y: e.y, flipped: false }));
  flipPins(rect);
  mapPinsLayer.replaceChildren(...mapPinGroups.map((g) => g.el));
  mapPinsKey = wanted.key;
  mapPinsWanted = null;
}

// Classes and order only: dimmed with their country; the picked one on top and never dimmed.
function stylePins() {
  if (!mapPinsLayer || !browse) return;
  const pickedCode = mapPicked && mapPicked.id === browse.id ? mapPicked.code : null;
  const shown = mapPinsShown;
  let picked = null;
  for (const g of mapPinGroups) {
    g.el.classList.toggle('map-pin-dim', g.code !== pickedCode && shown !== null && !shown.has(g.code));
    if (g.code === pickedCode) picked = g.el;
  }
  const order = mapPinGroups.map((g) => g.el).filter((el) => el !== picked);
  if (picked) order.push(picked);
  const children = mapPinsLayer.children;
  if (order.some((el, i) => children[i] !== el)) mapPinsLayer.append(...order);
}

// From colourMap(): rebuilt only if the pin data changed; off or no anchors means none.
function updatePins(shown) {
  if (!mapPinsLayer) return;
  mapPinsLayer.classList.toggle('map-pins-off', !browse.pins);
  mapPinsShown = shown;
  if (!browse.pins || mapData.anchors.size === 0) {
    clearPins();
    return;
  }
  const entries = pinEntries();
  const key = pinsKey(entries);
  if (key !== mapPinsKey) {
    mapPinsWanted = { key, entries };
    drawPins();
  }
  stylePins();
}

// With the dot radii: pins keep their screen size at every zoom (and draw if they were waiting).
function scalePins(rect) {
  if (mapPinsWanted) {
    drawPins(rect);
    stylePins();
    return;
  }
  const k = pinScale(rect);
  if (k === null) return;
  const r = rect || mapRect();
  for (const g of mapPinGroups) {
    g.el.setAttribute('transform', pinTransform(g, k));
    flipPin(g, r);
  }
}

function showPinsUi() {
  const on = browse ? browse.pins : true;
  $('map-pins-toggle').setAttribute('aria-pressed', String(on));
  $('legend-pins').hidden = !on;
  if (mapPinsLayer) mapPinsLayer.classList.toggle('map-pins-off', !on);
}

$('map-pins-toggle').addEventListener('click', () => {
  if (!onBrowsePage() || browse.view !== 'map') return;
  browse.pins = !browse.pins;
  showPinsUi();
  applyBrowse();
  writeBrowseAddress();
});

// ---- World map: zoom and pan (by the viewBox) ----

const mapScale = () => mapData.width / mapView.w;
const clampScale = (s) => Math.min(MAP_MAX_SCALE, Math.max(1, s));

function mapRect() {
  const rect = mapSvg.getBoundingClientRect();
  return rect.width >= 1 && rect.height >= 1 ? rect : null;
}

// Keeps dots about MAP_DOT_PX screen pixels in radius at any zoom.
function updateDotRadius(rect = mapRect()) {
  if (!rect || !mapView) return;
  const r = (MAP_DOT_PX * mapView.w) / rect.width;
  if (!Number.isFinite(r) || r <= 0) return;
  for (const { el } of mapDots) el.setAttribute('r', String(r));
  scalePins(rect);
}

// Clamped so the map always fills the frame. Dot radii change only with the zoom.
function setMapView(x, y, w, h, rect) {
  if (!mapData) return;
  const nx = Math.min(Math.max(x, 0), mapData.width - w);
  const ny = Math.min(Math.max(y, 0), mapData.height - h);
  if (![nx, ny, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return;
  const zoomed = !mapView || mapView.w !== w;
  mapView = { x: nx, y: ny, w, h };
  mapSvg.setAttribute('viewBox', `${nx} ${ny} ${w} ${h}`);
  if (zoomed) updateDotRadius(rect);
  else flipPins(rect);
}

function resetMapView() {
  if (mapBuilt) setMapView(0, 0, mapData.width, mapData.height);
}

// Zooms so that the map point (px, py) sits at the fraction (fx, fy) of the frame.
function zoomMapTo(scale, px, py, fx, fy, rect) {
  if (!mapView || ![scale, px, py, fx, fy].every(Number.isFinite)) return;
  const s = clampScale(scale);
  const w = mapData.width / s;
  const h = mapData.height / s;
  setMapView(px - fx * w, py - fy * h, w, h, rect);
}

// A screen point in map units, plus its fraction across the frame (null if the map isn't shown).
function mapPoint(clientX, clientY, rect = mapRect()) {
  if (!rect || !mapView) return null;
  const fx = (clientX - rect.left) / rect.width;
  const fy = (clientY - rect.top) / rect.height;
  return { x: mapView.x + fx * mapView.w, y: mapView.y + fy * mapView.h, fx, fy };
}

function zoomMapBy(factor) {
  if (!mapView) return;
  zoomMapTo(mapScale() * factor, mapView.x + mapView.w / 2, mapView.y + mapView.h / 2, 0.5, 0.5);
}

$('map-zoom-in').addEventListener('click', () => {
  cancelMapAnim();
  zoomMapBy(MAP_STEP);
});
$('map-zoom-out').addEventListener('click', () => {
  cancelMapAnim();
  zoomMapBy(1 / MAP_STEP);
});
$('map-reset').addEventListener('click', () => {
  cancelMapAnim();
  closeMapCard();
  clearMapPick();
  resetMapView();
});

// The page scrolls instead when the zoom can't change (out at 1x, in at 12x).
mapSvg.addEventListener('wheel', (event) => {
  if (!mapBuilt || !mapView) return;
  const unit = event.deltaMode === 1 ? 16 : (event.deltaMode === 2 ? 400 : 1);
  const delta = Math.max(-300, Math.min(300, event.deltaY * unit));
  const scale = clampScale(mapScale() * Math.exp(-delta * 0.002));
  if (!Number.isFinite(scale) || Math.abs(scale - mapScale()) < 1e-9) return;
  const p = mapPoint(event.clientX, event.clientY);
  if (!p) return;
  event.preventDefault();
  cancelMapAnim();
  zoomMapTo(scale, p.x, p.y, p.fx, p.fy);
}, { passive: false });

function panMap(dx, dy, rect) {
  if (!mapView) return;
  const ux = (dx * mapView.w) / rect.width;
  const uy = (dy * mapView.h) / rect.height;
  setMapView(mapView.x - ux, mapView.y - uy, mapView.w, mapView.h, rect);
}

// Two pointers: the map point under their midpoint follows the midpoint.
function startPinch() {
  const [a, b] = [...mapPointers.values()];
  const d = Math.hypot(a.x - b.x, a.y - b.y);
  const p = mapGesture.rect && mapPoint((a.x + b.x) / 2, (a.y + b.y) / 2, mapGesture.rect);
  mapGesture.pinch = d >= 1 && p ? { d, scale: mapScale(), x: p.x, y: p.y } : null;
}

function movePinch(rect) {
  const pinch = mapGesture.pinch;
  if (!pinch) return;
  const [a, b] = [...mapPointers.values()];
  const d = Math.hypot(a.x - b.x, a.y - b.y);
  if (d < 1) return;
  const fx = ((a.x + b.x) / 2 - rect.left) / rect.width;
  const fy = ((a.y + b.y) / 2 - rect.top) / rect.height;
  zoomMapTo((pinch.scale * d) / pinch.d, pinch.x, pinch.y, fx, fy, rect);
}

function clearMapGesture() {
  cancelAnimationFrame(mapMoveFrame);
  mapMoveFrame = 0;
  mapPointers.clear();
  mapGesture = null;
}

// Applies the pointers' latest positions, at most once per frame. `last` is
// where the single-pointer pan has got to.
function applyMapMove() {
  mapMoveFrame = 0;
  const g = mapGesture;
  if (!g || !g.rect || mapPointers.size === 0) return;
  if (mapPointers.size >= 2) {
    movePinch(g.rect);
    return;
  }
  if (g.tap || !g.last) return;
  const [cur] = mapPointers.values();
  panMap(cur.x - g.last.x, cur.y - g.last.y, g.rect);
  g.last = { x: cur.x, y: cur.y };
}

mapSvg.addEventListener('pointerdown', (event) => {
  if (!mapBuilt || (event.pointerType === 'mouse' && event.button !== 0)) return;
  cancelMapAnim();
  // A new gesture: drop pointers whose up/cancel never arrived.
  if (event.isPrimary || event.pointerType === 'mouse') clearMapGesture();
  try {
    mapSvg.setPointerCapture(event.pointerId);
  } catch {
    // Not capturable (pointer already gone): the gesture still works while over the map.
  }
  mapPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (!mapGesture) {
    // The target is recorded now: with capture, later events target the svg.
    // The frame's position is read once per gesture.
    mapGesture = {
      tap: mapPointers.size === 1, x: event.clientX, y: event.clientY, t: event.timeStamp,
      target: event.target, pinch: null, last: null, rect: mapRect(),
    };
  }
  if (mapPointers.size < 2) return;
  mapGesture.tap = false;
  closeMapCard();
  startPinch();
});

mapSvg.addEventListener('pointermove', (event) => {
  const g = mapGesture;
  if (!mapPointers.has(event.pointerId) || !g) return;
  const cur = { x: event.clientX, y: event.clientY };
  mapPointers.set(event.pointerId, cur);
  if (g.tap) {
    if (Math.hypot(cur.x - g.x, cur.y - g.y) < MAP_TAP_PX) return;
    // Drag start: pan from where the pointer went down.
    g.tap = false;
    g.last = { x: g.x, y: g.y };
    closeMapCard();
  }
  if (!mapMoveFrame) mapMoveFrame = requestAnimationFrame(applyMapMove);
});

function endMapPointer(event) {
  if (!mapPointers.has(event.pointerId)) return;
  const g = mapGesture;
  const last = mapPointers.size === 1;
  mapPointers.delete(event.pointerId);
  if (event.type === 'pointerup' && last && g && g.tap && event.timeStamp - g.t < MAP_TAP_MS
    && Math.hypot(event.clientX - g.x, event.clientY - g.y) < MAP_TAP_PX) {
    mapTap(g.target, event.clientX, event.clientY);
  }
  if (mapPointers.size === 0) {
    clearMapGesture();
  } else if (g && mapPointers.size >= 2) {
    startPinch();
  } else if (g) {
    // Pinch to one finger: keep panning from where that finger is now.
    const [rest] = mapPointers.values();
    g.pinch = null;
    g.last = { x: rest.x, y: rest.y };
  }
}

mapSvg.addEventListener('pointerup', endMapPointer);
mapSvg.addEventListener('pointercancel', endMapPointer);
mapSvg.addEventListener('lostpointercapture', endMapPointer);

window.addEventListener('resize', () => {
  if (mapBuilt && browse && browse.view === 'map') updateDotRadius();
});

// The closest dot within reach of a screen point, or null.
function nearestDot(clientX, clientY) {
  const rect = mapRect();
  if (!rect || !mapView) return null;
  let best = null;
  let bestDist = MAP_DOT_REACH_PX;
  for (const dot of mapDots) {
    const sx = rect.left + ((dot.x - mapView.x) / mapView.w) * rect.width;
    const sy = rect.top + ((dot.y - mapView.y) / mapView.h) * rect.height;
    const dist = Math.hypot(sx - clientX, sy - clientY);
    if (dist <= bestDist) {
      best = dot.el;
      bestDist = dist;
    }
  }
  return best;
}

// Only our own country shapes and dots count; anything else (sea, other land)
// closes the card unless a dot is within reach. Where dots overlap, the one
// whose centre is closest wins, not the one drawn last.
function mapTap(target, clientX, clientY) {
  // A pin (the topmost under the pointer) opens its own country.
  const pin = target instanceof Element ? target.closest('.map-pin') : null;
  const pinCode = pin && mapPinsLayer && mapPinsLayer.contains(pin) ? pin.getAttribute('data-code') : null;
  if (validMapCode(pinCode)) {
    openMapCard(pinCode, clientY);
    return;
  }
  let el = target instanceof Element ? target.closest('[data-code]') : null;
  if (el && !(mapSvg.contains(el) && (el.classList.contains('map-country') || el.classList.contains('map-dot')))) {
    el = null;
  }
  if (!el || el.classList.contains('map-dot')) el = nearestDot(clientX, clientY) || el;
  const code = el ? el.getAttribute('data-code') : null;
  if (typeof code === 'string' && CODE_RE.test(code) && countriesCache && countriesCache.has(code)) {
    openMapCard(code, clientY);
  } else {
    closeMapCard();
  }
}

// ---- World map: zoom to the random pick ----

const PICK_ZOOM_MS = 600;
const PICK_ZOOM_MIN = 2;
const PICK_ZOOM_MAX = 8;
// The country's box takes up at most 1 / 1.3 of the view in each direction.
const PICK_ZOOM_PAD = 1.3;

// The highlighted pick ({id, code}), and the running zoom animation's frame,
// target view and map generation. The animation outlives the board (closing
// it doesn't stop the zoom).
let mapPicked = null;
let mapAnimFrame = 0;
let mapAnimTo = null;
let mapAnimGen = 0;

// User gestures, Reset and logout: stop where the map is now.
function cancelMapAnim() {
  cancelAnimationFrame(mapAnimFrame);
  mapAnimFrame = 0;
  mapAnimTo = null;
}

// Leaving the page (render): jump to the target, so coming back shows the
// whole zoom rather than a half-way view.
function finishMapAnim() {
  const to = mapAnimTo;
  const current = mapAnimGen === mapGen && mapBuilt;
  cancelMapAnim();
  if (to && current) setMapView(to.x, to.y, to.w, to.h);
}

// Removes the highlight (classes only; colourMap() leaves it off from now on).
function clearMapPick() {
  mapPicked = null;
  for (const { el } of mapShapes) el.classList.remove('map-picked');
  stylePins();
}

// The bounding box of the largest piece (by box area) of a validated path:
// one piece per M. Null if no piece has a finite box.
function largestPieceBox(d) {
  let best = null;
  for (const piece of d.split('M')) {
    const nums = (piece.match(/-?[0-9.]+/g) || []).map(Number);
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (let i = 0; i + 1 < nums.length; i += 2) {
      minX = Math.min(minX, nums[i]);
      maxX = Math.max(maxX, nums[i]);
      minY = Math.min(minY, nums[i + 1]);
      maxY = Math.max(maxY, nums[i + 1]);
    }
    const box = { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
    if (!Object.values(box).every(Number.isFinite)) continue;
    if (!best || box.width * box.height > best.width * best.height) best = box;
  }
  return best;
}

// The view that fits a country: its dot if it has one, else its largest piece
// (so mainland Portugal, not the Azores; metropolitan France, not French Guiana).
function pickTargetView(code) {
  const dot = mapDots.find((dt) => dt.code === code);
  const d = mapData.countries.get(code);
  let box = null;
  if (dot) box = { x: dot.x, y: dot.y, width: 0, height: 0 };
  else if (typeof d === 'string') box = largestPieceBox(d);
  if (!box) return null;
  const { width, height } = mapData;
  const fit = Math.min(width / (box.width * PICK_ZOOM_PAD), height / (box.height * PICK_ZOOM_PAD));
  const scale = Math.min(PICK_ZOOM_MAX, Math.max(PICK_ZOOM_MIN, Number.isFinite(fit) ? fit : PICK_ZOOM_MAX));
  const w = width / scale;
  const h = height / scale;
  const view = { x: box.x + box.width / 2 - w / 2, y: box.y + box.height / 2 - h / 2, w, h };
  return Object.values(view).every(Number.isFinite) ? view : null;
}

// Moves the view towards `to` over PICK_ZOOM_MS (ease-out). The scale is
// interpolated on a log scale and the centre linearly, so the zoom feels even;
// every step goes through setMapView's clamp, and the last step lands on `to`
// exactly. Stops if the map or the page changed meanwhile.
function animateMapTo(to) {
  cancelMapAnim();
  const { width, height } = mapData;
  const from = { ...mapView };
  const gen = mapGen;
  const token = routeToken;
  const start = performance.now();
  const logFrom = Math.log(width / from.w);
  const logTo = Math.log(width / to.w);
  const lerp = (a, b, e) => a + (b - a) * e;
  mapAnimTo = to;
  mapAnimGen = gen;
  const step = (now) => {
    mapAnimFrame = 0;
    if (gen !== mapGen || token !== routeToken || !mapBuilt) return;
    const k = Math.min(1, (now - start) / PICK_ZOOM_MS);
    if (k >= 1) {
      mapAnimTo = null;
      setMapView(to.x, to.y, to.w, to.h);
      return;
    }
    const e = 1 - (1 - k) ** 3;
    const scale = Math.exp(lerp(logFrom, logTo, e));
    const w = width / scale;
    const h = height / scale;
    const cx = lerp(from.x + from.w / 2, to.x + to.w / 2, e);
    const cy = lerp(from.y + from.h / 2, to.y + to.h / 2, e);
    setMapView(cx - w / 2, cy - h / 2, w, h);
    mapAnimFrame = requestAnimationFrame(step);
  };
  mapAnimFrame = requestAnimationFrame(step);
}

// Called when the pick board settles: in map view, highlight the country and
// zoom to it (at once with reduced motion). List view is left alone. If the
// map isn't built yet, the pick is recorded and shown once it is (ensureMap).
function zoomMapToPick(code) {
  if (!onBrowsePage() || browse.view !== 'map') return;
  if (!CODE_RE.test(code) || !countriesCache || !countriesCache.has(code)) return;
  clearMapPick();
  mapPicked = { id: browse.id, code };
  if (!mapBuilt || !mapView) return;
  closeMapCard();
  for (const { el, code: c } of mapShapes) {
    if (c !== code) continue;
    el.classList.add('map-picked');
    el.parentNode.append(el);
  }
  stylePins();
  const to = pickTargetView(code);
  if (!to) return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    cancelMapAnim();
    setMapView(to.x, to.y, to.w, to.h);
  } else {
    animateMapTo(to);
  }
}

// A pick recorded before the map was built (this passport only): zoom to it at once.
function jumpToEarlyPick() {
  if (!mapPicked || !browse || mapPicked.id !== browse.id) return;
  const to = pickTargetView(mapPicked.code);
  if (to) setMapView(to.x, to.y, to.w, to.h);
}

// ---- World map: the country card ----

function mapCardStatus(row) {
  const entry = row ? displayEntry(row.s, browse.opts) : null;
  return entry ? entry[3]() : '';
}

// Shown at the top of the frame for a tap in its lower half, else at the bottom.
function openMapCard(code, clientY) {
  if (!browse) return;
  const country = countriesCache.get(code);
  const row = browseRows.find((r) => r.code === code);
  const rect = mapRect();
  const top = rect ? clientY - rect.top > rect.height / 2 : false;
  $('map-card-flag').textContent = flagFor(code);
  $('map-card-name').textContent = countryName(code, country.name);
  $('map-card-status').textContent = mapCardStatus(row);
  const card = $('map-card');
  card.classList.toggle('map-card-top', top);
  card.classList.toggle('map-card-bottom', !top);
  card.hidden = false;
  mapCardCode = code;
  fillMapCardNext();
}

// Who has the country listed, from the row only (contract 11c §7.1): names,
// no avatars, no adder; '' when it's on no list.
function mapCardNextText(r) {
  const personal = browse.opts.personal;
  const people = r.nextPeople;
  const shared = !personal && r.s.shared_next_stop === true;
  if (personal && people.length && state.user && people[0].id === state.user.id) return t('nextstops.yours');
  const names = people.map(memberName).join(t('nextstops.name_separator'));
  if (people.length && shared) return t('map.card_next_both', { names });
  if (people.length) return t('map.card_next_people', { names });
  return shared ? t('nextstops.shared') : '';
}

// The card's next-stop line and buttons, from the country's current row.
function fillMapCardNext() {
  const row = mapCardCode !== null ? browseRows.find((r) => r.code === mapCardCode) : null;
  const line = row ? mapCardNextText(row) : '';
  $('map-card-line').textContent = line;
  $('map-card-line').hidden = !line;
  if (row) {
    fillNextStopButtons($('map-card-next'), mapCardCode, rowNextState(row));
  } else {
    $('map-card-next').replaceChildren();
    $('map-card-next').hidden = true;
  }
}

function closeMapCard() {
  mapCardCode = null;
  $('map-card').hidden = true;
  for (const id of ['map-card-flag', 'map-card-name', 'map-card-status', 'map-card-line']) $(id).textContent = '';
  $('map-card-line').hidden = true;
  $('map-card-next').replaceChildren();
  $('map-card-next').hidden = true;
}

$('map-card-open').addEventListener('click', () => {
  if (mapCardCode !== null && onBrowsePage()) openCountry(mapCardCode);
});

// Logout: nothing of the previous colouring, card, zoom or gesture is left.
// The built shapes themselves go in dropMap().
function resetMapUi() {
  mapGen += 1;
  mapFetchKey = '';
  mapPassportId = null;
  mapShown = null;
  mapPicked = null;
  cancelMapAnim();
  clearMapGesture();
  closeMapCard();
  clearPins();
  mapPinsShown = null;
  for (const { el, base } of mapShapes) el.setAttribute('class', base);
  mapSvg.setAttribute('aria-label', t('map.label'));
  resetMapView();
  showMapStatus('');
  setViewUi(false);
  showPinsUi();
}

// The parsed map and every shape go; the svg element itself stays, so its
// listeners are attached only once.
function dropMap() {
  mapData = null;
  mapBuilt = false;
  mapShapes = [];
  mapDots = [];
  mapView = null;
  clearPins();
  mapPinsLayer = null;
  mapPinsShown = null;
  mapSvg.replaceChildren();
  mapSvg.removeAttribute('viewBox');
  $('map-body').hidden = true;
}

// ---- Passport page: random pick (departure board) ----

// Nothing here is saved: the board's country, the previous pick ({id, code},
// so Pick again doesn't repeat it) and a Log-a-meal intent ({id, code}, taken
// by the next render()) live in memory only and are dropped by clearPassportUi().
const pickDialog = $('pick-dialog');
let pickCurrent = null;
let lastPick = null;
let mealIntent = null;
// Bumped by stopPick() and on settling; timer and frame callbacks that
// captured an older value do nothing.
let pickGen = 0;
let pickTimer = 0;
let pickFrame = 0;
let pickTiles = [];
let pickStart = 0;

// The characters a tile flips through, by code point.
const FLAP_CHARS = Array.from(t('board.letters'));
const PICK_TICK_MS = 60;
// The N = rows x cols tiles settle at the evenly spread times
// PICK_FIRST_MS + k * PICK_SPREAD_MS / (N - 1) (400 ms to 2500 ms, whatever the
// country), handed out in random order (stopTimes).
const PICK_FIRST_MS = 400;
const PICK_SPREAD_MS = 2100;
// The board's size comes from its width and the 197 names only, never the pick.
// 12 columns fit down to 320 px (narrower tiles below 375 px, in CSS).
const PICK_MIN_COLS = 12;
const PICK_MAX_COLS = 40;
const PICK_ROWS = 2;
const clampInt = (n, lo, hi) => Math.min(hi, Math.max(lo, Number.isFinite(n) ? Math.floor(n) : lo));

const pickButtons = () => [$('pick-log'), $('pick-country'), $('pick-again')];

function pickBlocked() {
  return !onBrowsePage() || !canLogMeals() || searchTimer !== 0 || search.pending
    || !countriesCache || browseRows.length === 0;
}

function updatePickButton() {
  $('pick-open').disabled = pickBlocked();
}

// Countries listed right now that the caller hasn't done. Strict checks: an
// unexpected value is never eligible.
function pickEligible() {
  if (!browse || !countriesCache) return [];
  const personal = browse.opts.personal;
  return visibleRows()
    .filter((r) => (personal ? r.s.status === 'not_started' : r.s.done_by_me === false))
    .map((r) => r.code)
    .filter((code) => CODE_RE.test(code) && countriesCache.has(code));
}

// Uniform in [0, n): values in the incomplete last block are rejected.
function randomIndex(n) {
  const limit = 2 ** 32 - (2 ** 32 % n);
  const buf = new Uint32Array(1);
  do {
    crypto.getRandomValues(buf);
  } while (buf[0] >= limit);
  return buf[0] % n;
}

// The previous pick is left out first (unless it's the only one), so a
// repeat is impossible without re-drawing.
function drawPick(codes, previous) {
  const pool = codes.length > 1 && codes.includes(previous) ? codes.filter((c) => c !== previous) : codes;
  return pool[randomIndex(pool.length)];
}

function previousPick() {
  return lastPick && browse && lastPick.id === browse.id ? lastPick.code : null;
}

// The status line is cleared first and filled a frame later, so a screen
// reader announces it even when the same message is shown again.
let pickEmptyGen = 0;

function setPickEmpty(message) {
  pickEmptyGen += 1;
  $('pick-empty').textContent = '';
  if (!message) return;
  const gen = pickEmptyGen;
  const token = routeToken;
  requestAnimationFrame(() => {
    if (gen === pickEmptyGen && token === routeToken && onBrowsePage()) $('pick-empty').textContent = message;
  });
}

function showPickEmpty() {
  const active = search.on || filterCount(browse) > 0;
  setPickEmpty(active ? t('pick.empty_filtered') : t('pick.empty_all'));
}

// Synchronous: stops the timer and frame, and empties the board.
function stopPick() {
  clearInterval(pickTimer);
  cancelAnimationFrame(pickFrame);
  pickTimer = 0;
  pickFrame = 0;
  pickGen += 1;
  pickCurrent = null;
  pickTiles = [];
  $('pick-tiles').replaceChildren();
  $('pick-flag').textContent = '';
  $('pick-region').textContent = '';
  $('pick-live').textContent = '';
  $('pick-place').classList.add('is-pending');
  $('pick-board').disabled = false;
  for (const button of pickButtons()) button.disabled = true;
  $('pick-next').replaceChildren();
  $('pick-next').hidden = true;
}

// The board has settled on pickCurrent (its result buttons are about to be, or are, usable).
const pickSettled = () => pickCurrent !== null && pickDialog.open && $('pick-board').disabled;

// The pick result's next-stop buttons, from the picked country's current row.
function fillPickNext() {
  const p = pickCurrent;
  const row = p && browse && p.id === browse.id ? browseRows.find((r) => r.code === p.code) : null;
  if (row) {
    fillNextStopButtons($('pick-next'), p.code, rowNextState(row));
  } else {
    $('pick-next').replaceChildren();
    $('pick-next').hidden = true;
  }
}

// A name as board rows of code points (each at most `cols`): upper-cased,
// words placed left to right with greedy wrap, a word longer than a row
// broken by characters. Used for the board's size and for placing the pick.
function layoutName(name, cols) {
  const words = Array.from(name.normalize('NFC').toLocaleUpperCase(UPPER_LANG).normalize('NFC'))
    .reduce((acc, ch) => {
      if (ch === ' ') acc.push([]);
      else acc[acc.length - 1].push(ch);
      return acc;
    }, [[]])
    .filter((w) => w.length > 0);
  const rows = [];
  let row = [];
  for (const word of words) {
    let rest = word;
    // Each pass places the word, starts a new row, or places `cols` characters.
    while (rest.length > 0) {
      if ((row.length ? row.length + 1 : 0) + rest.length <= cols) {
        if (row.length) row.push(' ');
        row.push(...rest);
        rest = [];
      } else if (row.length) {
        rows.push(row);
        row = [];
      } else {
        rows.push(rest.slice(0, cols));
        rest = rest.slice(cols);
      }
    }
  }
  if (row.length) rows.push(row);
  return rows;
}

// Columns that fit the board's current width (tile and gap measured from CSS
// with a throwaway row). Always PICK_ROWS rows.
function measureBoard() {
  const box = $('pick-tiles');
  const probe = document.createElement('span');
  probe.className = 'pick-board-row';
  const tile = textEl('span', '', 'pick-tile');
  probe.append(tile);
  box.replaceChildren(probe);
  const gap = parseFloat(getComputedStyle(probe).columnGap) || 0;
  const tileWidth = tile.getBoundingClientRect().width;
  const width = box.getBoundingClientRect().width;
  box.replaceChildren();
  const cols = clampInt(tileWidth > 0 ? (width + gap) / (tileWidth + gap) : 0, PICK_MIN_COLS, PICK_MAX_COLS);
  return { cols, rows: PICK_ROWS };
}

// Builds rows x cols identical tiles; the character each one settles on (a
// letter, or a space for a blank) lives only in its JS object. Returns false
// if the name (or its short form) needs more rows than the board has (then
// it settles at once). The board-only short form (strings table, by code) is
// used when the full name doesn't fit; the full name is shown under the board.
function buildTiles(code, name) {
  const { cols, rows } = measureBoard();
  let layout = layoutName(name, cols);
  const short = boardShort(code);
  if (layout.length > rows && short !== null) layout = layoutName(short, cols);
  const box = $('pick-tiles');
  pickTiles = [];
  for (let r = 0; r < rows; r += 1) {
    const rowEl = document.createElement('span');
    rowEl.className = 'pick-board-row';
    for (let c = 0; c < cols; c += 1) {
      const el = textEl('span', '', 'pick-tile');
      rowEl.append(el);
      const ch = (layout[r] && layout[r][c]) || ' ';
      pickTiles.push({ el, ch, stopAt: 0, done: false });
    }
    box.append(rowEl);
  }
  const times = stopTimes(pickTiles.map((tile) => tile.ch));
  pickTiles.forEach((tile, i) => { tile.stopAt = times[i]; });
  return layout.length <= rows;
}

// Stop times for tiles in board order (chars[i] is the tile's final character,
// ' ' for a blank). One real letter (a random \p{L} tile, else any non-blank)
// gets the last time, 2500 ms; the other tiles get evenly spread slots from
// 400 ms to one tick before that, shuffled (Fisher-Yates with the secure
// generator), so blanks settle among the letters and the name visibly
// finishes on its own last tick.
function stopTimes(chars) {
  const n = chars.length;
  const indexes = (test) => chars.flatMap((ch, i) => (test(ch) ? [i] : []));
  const letters = indexes((ch) => /\p{L}/u.test(ch));
  const pool = letters.length ? letters : indexes((ch) => ch !== ' ');
  const last = pool.length ? pool[randomIndex(pool.length)] : n - 1;
  const step = (PICK_SPREAD_MS - PICK_TICK_MS) / (n - 2);
  const slots = Array.from({ length: n - 1 }, (_, k) => PICK_FIRST_MS + k * step);
  for (let i = slots.length - 1; i > 0; i -= 1) {
    const j = randomIndex(i + 1);
    [slots[i], slots[j]] = [slots[j], slots[i]];
  }
  let next = 0;
  return chars.map((_, i) => (i === last ? PICK_FIRST_MS + PICK_SPREAD_MS : slots[next++]));
}

// A settled tile: its letter, or blank.
function settleTile(tile) {
  tile.done = true;
  tile.el.textContent = tile.ch === ' ' ? '' : tile.ch;
  tile.el.className = tile.ch === ' ' ? 'pick-tile pick-tile-blank' : 'pick-tile'; // i18n-ok CSS class names
}

function startPick(code) {
  stopPick();
  clearMapPick();
  const country = countriesCache.get(code);
  pickCurrent = { id: browse.id, code };
  lastPick = { id: browse.id, code };
  const fits = buildTiles(code, countryName(code, country.name));
  if (!fits || matchMedia('(prefers-reduced-motion: reduce)').matches) {
    settlePick();
    return;
  }
  const gen = pickGen;
  pickStart = performance.now();
  pickTimer = setInterval(() => tickPick(gen), PICK_TICK_MS);
  tickPick(gen);
}

// Time-based, so a throttled background tab still settles on time.
function tickPick(gen) {
  if (gen !== pickGen) return;
  if (!pickDialog.open) {
    stopPick();
    return;
  }
  const elapsed = performance.now() - pickStart;
  const random = new Uint8Array(pickTiles.length);
  crypto.getRandomValues(random);
  let left = 0;
  pickTiles.forEach((tile, i) => {
    if (tile.done) return;
    if (elapsed >= tile.stopAt) {
      settleTile(tile);
      return;
    }
    left += 1;
    tile.el.textContent = FLAP_CHARS[random[i] % FLAP_CHARS.length];
    // Two identical animations under different names restart it on every flap.
    tile.el.className = tile.el.className === 'pick-tile flip-a' ? 'pick-tile flip-b' : 'pick-tile flip-a'; // i18n-ok CSS class names
  });
  if (left === 0) settlePick();
}

// Shows the result. The buttons wait a frame, so the key press or tap that
// skipped the animation can't also activate one of them.
function settlePick() {
  if (!pickCurrent) return;
  clearInterval(pickTimer);
  cancelAnimationFrame(pickFrame);
  pickTimer = 0;
  pickGen += 1;
  const gen = pickGen;
  const country = countriesCache && countriesCache.get(pickCurrent.code);
  if (!country) {
    stopPick();
    return;
  }
  for (const tile of pickTiles) settleTile(tile);
  const region = typeof country.region === 'string' ? regionName(country.region) : '';
  const name = countryName(pickCurrent.code, country.name);
  $('pick-flag').textContent = flagFor(pickCurrent.code);
  // The full name, also when the board shows a short form.
  $('pick-region').textContent = region ? t('pick.place', { country: name, region }) : name;
  $('pick-place').classList.remove('is-pending');
  $('pick-live').textContent = region ? t('pick.live_region', { country: name, region })
    : t('pick.live', { country: name });
  $('pick-board').disabled = true;
  zoomMapToPick(pickCurrent.code);
  pickFrame = requestAnimationFrame(() => {
    if (gen !== pickGen || !pickDialog.open) return;
    // Defensive: the board only opens for owners and participants, so canLog
    // is normally true here.
    const canLog = canLogMeals();
    $('pick-log').hidden = !canLog;
    for (const button of pickButtons()) button.disabled = false;
    fillPickNext();
    (canLog ? $('pick-log') : $('pick-country')).focus();
  });
}

$('pick-open').addEventListener('click', () => {
  if (pickBlocked()) return;
  const codes = pickEligible();
  if (!codes.length) {
    showPickEmpty();
    return;
  }
  setPickEmpty('');
  pickDialog.showModal();
  startPick(drawPick(codes, previousPick()));
});

$('pick-board').addEventListener('click', () => {
  if (pickTimer) settlePick();
});

function closePick() {
  stopPick();
  pickDialog.close();
}

$('pick-close').addEventListener('click', closePick);
// Escape: stop now; the dialog then closes by default.
pickDialog.addEventListener('cancel', stopPick);
// May run after a result button has already navigated, so it only stops the
// board and never touches the Log-a-meal intent.
pickDialog.addEventListener('close', stopPick);

// A held key repeats; a repeat must not activate whatever button now has focus.
pickDialog.addEventListener('keydown', (event) => {
  if (event.repeat && (event.key === 'Enter' || event.key === ' ')) event.preventDefault();
}, true);

// The settled pick as a country hash, from checked values only (else null).
// The passport entry in history keeps its browse query, so Back returns to it.
function pickedCountryHash() {
  const p = pickCurrent;
  if (!p || !ID_RE.test(p.id) || !CODE_RE.test(p.code) || !countriesCache || !countriesCache.has(p.code)) return null;
  return { id: p.id, code: p.code, hash: `#/passports/${p.id}/countries/${p.code}` };
}

$('pick-log').addEventListener('click', () => {
  const target = pickedCountryHash();
  if (!target || !canLogMeals() || !onBrowsePage()) return;
  closePick();
  mealIntent = { id: target.id, code: target.code };
  navigate(target.hash);
});

$('pick-country').addEventListener('click', () => {
  const target = pickedCountryHash();
  if (!target || !onBrowsePage()) return;
  closePick();
  navigate(target.hash);
});

$('pick-again').addEventListener('click', () => {
  if (!pickCurrent) return;
  if (pickBlocked()) {
    closePick();
    return;
  }
  const codes = pickEligible();
  if (!codes.length) {
    closePick();
    showPickEmpty();
    return;
  }
  startPick(drawPick(codes, previousPick()));
  $('pick-board').focus();
});

$('people-open').addEventListener('click', () => {
  const id = passport ? String(passport.passport.id) : '';
  if (ID_RE.test(id)) location.hash = `#/passports/${id}/people`;
});

// ---- Country page ----

const canLogMeals = () => passport !== null
  && (passport.passport.role === 'owner' || passport.passport.role === 'participant');

// For an owner or participant of a shared passport, the "some" line says whose
// turn it is. "Me" is the one participant whose integer id is the session
// user's (never matched by name); anything ambiguous gets the general line.
function someStatusText(participants) {
  const role = passport ? passport.passport.role : '';
  if (role !== 'owner' && role !== 'participant') return t('countrypage.status_some');
  const me = participants.filter((p) => p && Number.isInteger(p.id) && state.user && p.id === state.user.id);
  if (me.length !== 1 || typeof me[0].done !== 'boolean') return t('countrypage.status_some');
  return me[0].done ? t('countrypage.status_waiting') : t('countrypage.status_your_turn');
}

function countryStatusText(data, participants, personal) {
  if (personal) {
    const owner = participants[0];
    if (!owner || owner.done !== true) return t('countrypage.status_not_done');
    const date = formatDay(owner.completed_on);
    return date ? t('countrypage.status_done_on', { date }) : t('status.done');
  }
  if (data.status === 'complete') {
    const date = formatDay(data.completed_on);
    return date ? t('countrypage.status_complete_on', { date }) : t('status.complete');
  }
  if (data.status === 'some') return someStatusText(participants);
  if (data.status === 'not_started') return t('status.not_started');
  return '';
}

// A participant's name, marked when their account is disabled.
const memberName = (m) => (m.inactive ? t('person.inactive', { name: m.display_name }) : m.display_name);

function doneText(m) {
  if (m.done !== true) return t('countrypage.person_not_yet');
  const date = formatDay(m.completed_on);
  return date ? t('countrypage.person_done_on', { date }) : t('status.done');
}

function countryPersonHtml(m) {
  const done = m.done === true;
  const name = memberName(m);
  const doneLabel = doneText(m);
  return '<li class="person-row"><span class="avatar-slot"></span><div class="person-text">' // i18n-ok HTML markup
    + `<span class="person-name">${esc(name)}</span>` // i18n-ok HTML markup
    + `<span class="person-username">@${esc(m.username)}</span></div>` // i18n-ok HTML markup
    + `<span class="done-state${done ? ' done-mark' : ' muted'}">${esc(doneLabel)}</span>` // i18n-ok HTML markup
    + '</li>';
}

function mealButton(text, label, action, id) {
  const button = textEl('button', text, 'btn');
  button.type = 'button';
  button.dataset.mealAction = action;
  button.dataset.mealId = id;
  button.setAttribute('aria-label', label);
  return button;
}

// Built with textContent throughout; notes keep their line breaks via CSS.
function mealCard(m) {
  const li = document.createElement('li');
  li.className = 'card meal-card'; // i18n-ok CSS class names
  const date = formatDay(m.eaten_on);
  const head = document.createElement('div');
  head.className = 'meal-head';
  head.append(textEl('span', date, 'meal-date'));
  if (isStars(m.rating)) head.append(starsEl(m.rating));
  li.append(head);
  if (m.user) {
    const author = textEl('p', personLabel(m.user), 'muted meal-author'); // i18n-ok CSS class names
    author.prepend(avatarEl(m.user.avatar_seed, 'avatar-sm'));
    li.append(author);
  }
  if (typeof m.dish === 'string' && m.dish) li.append(textEl('p', m.dish, 'meal-dish'));
  if (typeof m.place === 'string' && m.place) li.append(textEl('p', t('meal.at_place', { place: m.place }), 'muted'));
  if (typeof m.notes === 'string' && m.notes) li.append(textEl('p', m.notes, 'meal-notes'));
  if (m.mine === true) {
    const actions = document.createElement('div');
    actions.className = 'actions';
    const id = String(m.id);
    const editLabel = date ? t('meal.edit_label_dated', { date }) : t('meal.edit_label');
    const deleteLabel = date ? t('meal.delete_label_dated', { date }) : t('meal.delete_label');
    actions.append(
      mealButton(t('meal.edit'), editLabel, 'edit', id),
      mealButton(t('meal.delete'), deleteLabel, 'delete', id),
    );
    li.append(actions);
  }
  return li;
}

function showCountry(code, data, countries) {
  const country = countries.get(code);
  const personal = passport.passport.is_personal === true;
  const participants = Array.isArray(data.participants) ? data.participants : [];
  const meals = (Array.isArray(data.meals) ? data.meals : []).filter((m) => m && ID_RE.test(String(m.id)));
  countryDetail = { code, name: countryName(code, country.name), meals, next: detailNextState(data, participants) };
  $('country-flag').textContent = flagFor(code);
  $('country-name').textContent = countryDetail.name;
  $('country-region').textContent = typeof country.region === 'string' ? regionName(country.region) : '';
  $('country-status').textContent = countryStatusText(data, participants, personal);
  const avg = data.avg_rating;
  const hasAvg = typeof avg === 'number' && Number.isFinite(avg);
  $('country-avg').textContent = hasAvg ? t('countrypage.average', { avg: avg.toFixed(1) }) : '';
  $('country-avg').hidden = !hasAvg;
  $('country-people-card').hidden = personal;
  $('country-participants').innerHTML = personal ? '' : participants.map(countryPersonHtml).join('');
  fillAvatarSlots($('country-participants'), personal ? [] : participants, 'avatar-sm');
  $('log-meal-open').hidden = !canLogMeals();
  showNextStops();
  $('meal-list').replaceChildren(...meals.map(mealCard));
  $('meals-empty').hidden = meals.length > 0;
}

// Re-reads the open country after a change without the loading view, so the
// page keeps its scroll position.
async function refreshCountry(token) {
  const code = countryDetail ? countryDetail.code : '';
  const countries = countriesCache;
  if (!passport || !countries || !CODE_RE.test(code) || !countries.has(code)) {
    render();
    return;
  }
  let data;
  try {
    data = await passportRequest('GET', `/countries/${code}`);
  } catch (err) {
    if (err instanceof QuietError) return;
    // A gone passport has already moved the page on (new routeToken); say why anyway.
    const gone = err.status === 404 && err.code === 'passport_not_found';
    if (gone || token === routeToken) toast(err.message, 'error');
    return;
  }
  if (token !== routeToken) return;
  showCountry(code, data, countries);
  // The Edit/Delete button that opened the dialog was replaced, so focus would
  // fall back to <body>; put it on the meals heading instead.
  if (!document.activeElement || document.activeElement === document.body) $('meals-title').focus();
}

$('meal-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-meal-id]');
  if (!button || !countryDetail) return;
  const id = button.dataset.mealId;
  if (!ID_RE.test(id)) return;
  const meal = countryDetail.meals.find((m) => String(m.id) === id && m.mine === true);
  if (!meal) return;
  if (button.dataset.mealAction === 'edit') openMealDialog(meal);
  else if (button.dataset.mealAction === 'delete') openDeleteMeal(meal);
});

// ---- Next stops (contract 11b): list badges, toggle buttons, country page lines ----

const NEXT_LISTS = ['mine', 'ours'];
// A map pin with a round hole (fixed path data; never from the server).
const PIN_PATH = 'M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z'
  + 'M12 11.5a2.5 2.5 0 1 1 0-5a2.5 2.5 0 1 1 0 5z';

// Summary ids resolved to this passport's active participants by integer id;
// unknown ids and repeats are skipped, and never more than there are participants.
function nextStopPeople(ids) {
  const participants = passport && Array.isArray(passport.participants) ? passport.participants : [];
  const people = [];
  for (const id of Array.isArray(ids) ? ids : []) {
    if (people.length >= participants.length) break;
    if (!Number.isInteger(id) || people.some((p) => p.id === id)) continue;
    const person = participants.find((m) => m.id === id && m.inactive !== true);
    if (person) people.push(person);
  }
  return people;
}

// The country page's people: person objects with an integer id and a name, no repeats.
function detailPeople(list) {
  const max = passport && Array.isArray(passport.participants) ? passport.participants.length : 0;
  const people = [];
  for (const p of Array.isArray(list) ? list : []) {
    if (people.length >= max) break;
    if (!isPlainObject(p) || !Number.isInteger(p.id) || typeof p.display_name !== 'string') continue;
    if (!people.some((q) => q.id === p.id)) people.push(p);
  }
  return people;
}

// cls: 'next-stop-pin' (sized by CSS) or 'map-pin-icon' (sized by attributes on the map).
function pinEl(cls = 'next-stop-pin') {
  const svg = svgEl('svg', cls);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = svgEl('path');
  path.setAttribute('d', PIN_PATH);
  svg.append(path);
  return svg;
}

// A list row's group: avatars of the people with it on their own list, the
// pin if it's on the shared list, and the same as hidden text. Null if neither.
function nextStopBadge(r) {
  const people = r.nextPeople;
  const shared = r.s.shared_next_stop === true;
  if (people.length === 0 && !shared) return null;
  const group = document.createElement('span');
  group.className = 'next-stop-badge';
  for (const p of people) group.append(avatarEl(p.avatar_seed, 'avatar-sm'));
  if (shared) group.append(pinEl());
  const names = people.map(memberName).join(t('nextstops.name_separator'));
  let label = t('nextstops.shared');
  if (people.length && shared) label = t('nextstops.badge_both', { names });
  else if (people.length) label = t('nextstops.badge_people', { names });
  group.append(textEl('span', label, 'visually-hidden'));
  return group;
}

// Pairs each fixed .next-stop-slot just rendered by innerHTML with the same
// rows, by index. On a count mismatch every slot stays empty (hidden by CSS).
function fillNextStopSlots(container, rows) {
  const slots = container.querySelectorAll('.next-stop-slot');
  if (slots.length !== rows.length) {
    console.error('next-stop slots and rows differ in number'); // i18n-ok developer message
    return;
  }
  slots.forEach((slot, i) => {
    const group = nextStopBadge(rows[i]);
    if (group) slot.replaceWith(group);
  });
}

// What the buttons show, from a list row: {mine (null for viewers), ours, and
// whether an add would be refused}.
function rowNextState(r) {
  const s = r.s;
  return {
    mine: typeof s.my_next_stop === 'boolean' ? s.my_next_stop : null,
    ours: s.shared_next_stop === true,
    ...summaryBlocked(s),
  };
}

// Whether adding to each list would be refused, from a country summary.
function summaryBlocked(s) {
  const personal = passport !== null && passport.passport.is_personal === true;
  return {
    mineBlocked: personal ? personalDone(s) : s.done_by_me === true,
    oursBlocked: s.status === 'complete',
  };
}

// The same from the country detail (or a toggle response, with its summary).
function nextFromResponse(data) {
  const personal = passport !== null && passport.passport.is_personal === true;
  const ns = isPlainObject(data.next_stops) ? data.next_stops : {};
  const shared = !personal && isPlainObject(ns.shared) ? ns.shared : null;
  return {
    mine: typeof data.my_next_stop === 'boolean' ? data.my_next_stop : null,
    ours: shared !== null,
    shared,
    people: detailPeople(ns.people),
  };
}

function detailNextState(data, participants) {
  const me = participants.filter((p) => p && Number.isInteger(p.id) && state.user && p.id === state.user.id);
  return {
    ...nextFromResponse(data),
    mineBlocked: me.length === 1 && me[0].done === true,
    oursBlocked: data.status === 'complete',
  };
}

function nextStopButton(list, on, disabled) {
  const personal = passport.passport.is_personal === true;
  let label = t('nextstops.ours');
  if (list === 'mine') label = personal ? t('nextstops.personal') : t('nextstops.mine');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn next-stop-btn'; // i18n-ok CSS class names
  button.dataset.nextList = list;
  button.setAttribute('aria-pressed', String(on));
  button.disabled = disabled;
  const mark = textEl('span', on ? '✓' : '+', 'next-stop-mark');
  mark.setAttribute('aria-hidden', 'true');
  button.append(mark, textEl('span', label));
  return button;
}

// Fills a button row for one country: owners and participants only, and only
// when the server gave a boolean my_next_stop; "Ours" never on personal
// passports. An add that would be refused is disabled while off.
function fillNextStopButtons(container, code, st) {
  const id = passport ? String(passport.passport.id) : '';
  const show = canLogMeals() && typeof st.mine === 'boolean' && ID_RE.test(id) && CODE_RE.test(code);
  container.hidden = !show;
  if (!show) {
    container.replaceChildren();
    return [];
  }
  const busy = nextStopBusy.has(`${id}:${code}`);
  const buttons = [nextStopButton('mine', st.mine, busy || (!st.mine && st.mineBlocked))];
  if (passport.passport.is_personal !== true) {
    buttons.push(nextStopButton('ours', st.ours, busy || (!st.ours && st.oursBlocked)));
  }
  container.replaceChildren(...buttons);
  return buttons;
}

function nextStopPeopleLine(people) {
  const line = document.createElement('div');
  line.className = 'next-stop-line';
  const list = document.createElement('ul');
  list.className = 'next-stop-people';
  for (const p of people) {
    const item = document.createElement('li');
    item.append(avatarEl(p.avatar_seed, 'avatar-sm'), textEl('span', memberName(p)));
    list.append(item);
  }
  line.append(textEl('p', t('nextstops.people')), list);
  return line;
}

// added_by null (or not a person) means no name: the adder has left or been removed.
function nextStopSharedLine(shared) {
  const by = shared.added_by;
  const named = isPlainObject(by) && typeof by.display_name === 'string';
  const line = textEl('p', '', 'next-stop-shared');
  line.append(pinEl(), textEl('span', named ? t('nextstops.shared_by', { name: memberName(by) }) : t('nextstops.shared')));
  return line;
}

function nextStopLines(next, personal) {
  const lines = [];
  if (personal) {
    // The owner reads "your"; a viewer sees whose list it is.
    if (next.mine === true) lines.push(textEl('p', t('nextstops.yours')));
    else if (next.mine === null && next.people.length) lines.push(nextStopPeopleLine(next.people));
    return lines;
  }
  if (next.people.length) lines.push(nextStopPeopleLine(next.people));
  if (next.shared) lines.push(nextStopSharedLine(next.shared));
  if (!lines.length) lines.push(textEl('p', t('nextstops.none'), 'muted'));
  return lines;
}

// The country page's Next stops section: buttons, a hint for a refused add, the lines.
function showNextStops() {
  const { next, code } = countryDetail;
  const personal = passport.passport.is_personal === true;
  const buttons = fillNextStopButtons($('country-next-buttons'), code, next);
  const shown = (list) => buttons.some((b) => b.dataset.nextList === list);
  let hint = '';
  if (shown('ours') && !next.ours && next.oursBlocked) hint = t('nextstops.hint_complete');
  else if (shown('mine') && !next.mine && next.mineBlocked) hint = t('nextstops.hint_done');
  $('country-next-hint').textContent = hint;
  $('country-next-hint').hidden = !hint;
  const lines = nextStopLines(next, personal);
  $('country-next-lines').replaceChildren(...lines);
  $('country-next-lines').hidden = lines.length === 0;
  $('country-next-card').hidden = buttons.length === 0 && lines.length === 0;
}

function nextStopToast(list, on) {
  if (passport && passport.passport.is_personal === true) return on ? t('toast.next_added') : t('toast.next_removed');
  if (list === 'ours') return on ? t('toast.next_ours_added') : t('toast.next_ours_removed');
  return on ? t('toast.next_mine_added') : t('toast.next_mine_removed');
}

// Redraws every open set of buttons for this country (page, map card, pick
// result). If focus was on the row when tapped (refocus), it goes back to the
// toggled list's new button, unless the user has moved it elsewhere meanwhile.
function refreshNextStopUi(code, container, list, refocus) {
  if (currentView === 'country' && countryDetail && countryDetail.code === code) showNextStops();
  if (mapCardCode === code) fillMapCardNext();
  if (pickSettled() && pickCurrent.code === code) fillPickNext();
  const active = document.activeElement;
  // A removed focused button leaves focus on <body>, or on the dialog inside a modal.
  const lost = !active || active === document.body || (active.tagName === 'DIALOG' && active.contains(container));
  if (refocus && NEXT_LISTS.includes(list) && lost && container.isConnected && !container.hidden) {
    container.querySelector(`button[data-next-list="${list}"]:not(:disabled)`)?.focus();
  }
}

// Merges a toggle response (already checked to be for this passport and code)
// into the list row and the country page, then re-applies the browse state.
function applyNextStopResponse(code, data) {
  nextStopGen += 1;
  const id = String(passport.passport.id);
  const i = browseRows.findIndex((r) => r.code === code);
  if (browse && browse.id === id && i >= 0) {
    browseRows[i] = rowModel(data.summary, browseRows[i].country, browse.opts.personal, browseActive);
    if (onBrowsePage()) applyBrowse();
  }
  if (countryDetail && countryDetail.code === code) {
    countryDetail.next = { ...nextFromResponse(data), ...summaryBlocked(data.summary) };
  }
}

// After a 409 the shown state was out of date: re-read the country page or
// the list. The result is dropped if a newer toggle response or re-read came first.
async function reloadNextStops(token, id) {
  const gen = ++nextStopGen;
  const code = currentView === 'country' && countryDetail ? countryDetail.code : null;
  if (code === null && (!onBrowsePage() || browse.id !== id)) return;
  if (code !== null && !(CODE_RE.test(code) && countriesCache && countriesCache.has(code))) return;
  let data;
  try {
    data = await passportRequest('GET', code === null ? '/countries' : `/countries/${code}`);
  } catch (err) {
    if (err instanceof QuietError) return;
    // A gone passport has already moved the page on (new routeToken); say why anyway.
    const gone = err.status === 404 && err.code === 'passport_not_found';
    if (gone || token === routeToken) toast(err.message, 'error');
    return;
  }
  if (gen !== nextStopGen || token !== routeToken || !passport || String(passport.passport.id) !== id) return;
  if (code !== null) {
    if (currentView === 'country' && countryDetail && countryDetail.code === code) {
      showCountry(code, data, countriesCache);
    }
    return;
  }
  if (!onBrowsePage() || browse.id !== id || !countriesCache) return;
  setCountryRows(data, countriesCache);
  applyBrowse();
  if (mapCardCode !== null) fillMapCardNext();
  if (pickSettled()) fillPickNext();
}

// Sets (not toggles) one list for one country: `on` is the opposite of the
// button's pressed state when tapped. The response is used only if the page,
// passport and country are still the ones it was sent for.
async function toggleNextStop(button, code, container) {
  const list = button.dataset.nextList;
  if (button.disabled || !passport || !canLogMeals() || !NEXT_LISTS.includes(list)) return;
  if (list === 'ours' && passport.passport.is_personal === true) return;
  if (!CODE_RE.test(code) || !countriesCache || !countriesCache.has(code)) return;
  const id = String(passport.passport.id);
  const key = `${id}:${code}`;
  if (!ID_RE.test(id) || nextStopBusy.has(key)) return;
  const on = button.getAttribute('aria-pressed') !== 'true';
  const token = routeToken;
  const refocus = container.contains(document.activeElement);
  nextStopBusy.add(key);
  refreshNextStopUi(code, container, list, false);
  let data;
  try {
    data = await passportRequest('POST', `/countries/${code}/next-stops`, { list, on });
  } catch (err) {
    nextStopBusy.delete(key);
    if (err instanceof QuietError) return;
    // Only on the page it was made from; a gone passport has already moved the page on, so say why.
    const gone = err.status === 404 && err.code === 'passport_not_found';
    if (gone || token === routeToken) toast(err.message, 'error');
    refreshNextStopUi(code, container, list, refocus);
    if (err.status === 409 && token === routeToken) reloadNextStops(token, id);
    return;
  }
  nextStopBusy.delete(key);
  if (token === routeToken) toast(nextStopToast(list, on));
  const current = token === routeToken && passport !== null && String(passport.passport.id) === id;
  if (current && isPlainObject(data.summary) && data.summary.code === code) applyNextStopResponse(code, data);
  refreshNextStopUi(code, container, list, refocus);
}

// Each button row knows its country from the page state, never from the DOM.
const NEXT_STOP_ROWS = [
  ['country-next-buttons', () => (currentView === 'country' && countryDetail ? countryDetail.code : null)],
  ['map-card-next', () => (onBrowsePage() ? mapCardCode : null)],
  ['pick-next', () => (onBrowsePage() && pickSettled() ? pickCurrent.code : null)],
];

for (const [containerId, codeOf] of NEXT_STOP_ROWS) {
  const container = $(containerId);
  container.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-next-list]');
    const code = codeOf();
    if (button && container.contains(button) && typeof code === 'string') toggleNextStop(button, code, container);
  });
}

// ---- Meal dialogs ----

const mealDialog = setupDialog('meal-dialog');
mealDialog.addEventListener('close', () => { mealEdit = null; });

// meal: the meal to edit, or null to log a new one.
function openMealDialog(meal) {
  if (!countryDetail) return;
  mealEdit = meal;
  const el = $('meal-form').elements;
  const today = localToday();
  const date = meal && typeof meal.eaten_on === 'string' && DAY_RE.test(meal.eaten_on) ? meal.eaten_on : today;
  $('meal-title').textContent = meal ? t('meal.edit_title') : t('meal.log_title', { country: countryDetail.name });
  // The server allows up to a day ahead (time zones), so keep a stored later date editable.
  el.eaten_on.max = date > today ? date : today;
  el.eaten_on.value = date;
  el.dish.value = meal && typeof meal.dish === 'string' ? meal.dish : '';
  el.place.value = meal && typeof meal.place === 'string' ? meal.place : '';
  const rating = meal && isStars(meal.rating) ? String(meal.rating) : 'none';
  for (const radio of el.rating) radio.checked = radio.value === rating;
  el.notes.value = meal && typeof meal.notes === 'string' ? meal.notes : '';
  mealDialog.showModal();
}

$('log-meal-open').addEventListener('click', () => openMealDialog(null));

// Mirrors contract §5; the server is authoritative and its message is shown on error.
const MEAL_TEXT_MAX = 100;
const NOTES_MAX = 2000;
const MEAL_FIRST_YEAR = 1900;
const MEAL_FIRST_DAY = `${MEAL_FIRST_YEAR}-01-01`;
const MEAL_TEXT_MESSAGES = Object.freeze({
  dish: Object.freeze({
    length: () => t('error.dish_length', { max: MEAL_TEXT_MAX }),
    chars: () => t('error.dish_chars'),
  }),
  place: Object.freeze({
    length: () => t('error.place_length', { max: MEAL_TEXT_MAX }),
    chars: () => t('error.place_chars'),
  }),
});

function checkMealText(value, messages) {
  const s = value.normalize('NFC').trim();
  if (codePoints(s) > MEAL_TEXT_MAX) fail(messages.length());
  if (BAD_NAME_CHAR_RE.test(s)) fail(messages.chars());
}

function checkNotes(value) {
  const s = value.replace(/\r\n?/g, '\n').normalize('NFC').trim();
  if (codePoints(s) > NOTES_MAX) fail(t('error.notes_length'));
  if (BAD_NAME_CHAR_RE.test(s.replace(/\n/g, ''))) fail(t('error.notes_chars'));
}

function readMealForm(form) {
  const el = form.elements;
  const eatenOn = el.eaten_on.value;
  if (!DAY_RE.test(eatenOn) || eatenOn < MEAL_FIRST_DAY || eatenOn > el.eaten_on.max) {
    fail(t('error.date_range', { year: MEAL_FIRST_YEAR }));
  }
  checkMealText(el.dish.value, MEAL_TEXT_MESSAGES.dish);
  checkMealText(el.place.value, MEAL_TEXT_MESSAGES.place);
  const rating = form.querySelector('input[name="rating"]:checked')?.value ?? 'none';
  if (rating !== 'none' && !STARS_RE.test(rating)) fail(t('error.rating_range', { min: STARS_MIN, max: STARS_MAX }));
  checkNotes(el.notes.value);
  return {
    eaten_on: eatenOn,
    dish: el.dish.value,
    place: el.place.value,
    rating: rating === 'none' ? null : Number(rating),
    notes: el.notes.value,
  };
}

bindForm($('meal-form'), async (form) => {
  if (!countryDetail) fail(t('client.country_not_found'), 'country_not_found');
  const body = readMealForm(form);
  const { code } = countryDetail;
  const token = routeToken;
  // Closing bumps dialogToken, so a mismatch means the dialog was closed (and
  // maybe reopened) while saving; then it mustn't be closed from here.
  const dt = dialogToken;
  if (mealEdit) {
    const id = String(mealEdit.id);
    if (!ID_RE.test(id)) fail(t('client.meal_not_found'), 'meal_not_found');
    await passportRequest('PATCH', `/meals/${id}`, body);
  } else {
    if (!CODE_RE.test(code)) fail(t('client.country_not_found'), 'country_not_found');
    await passportRequest('POST', `/countries/${code}/meals`, body);
  }
  if (dt === dialogToken) mealDialog.close();
  toast(t('toast.meal_saved'));
  if (token === routeToken) refreshCountry(token);
});

const deleteMealDialog = setupDialog('delete-meal-dialog');
deleteMealDialog.addEventListener('close', () => { mealDelete = null; });

function openDeleteMeal(meal) {
  mealDelete = meal;
  const date = formatDay(meal.eaten_on);
  const last = countryDetail.meals.filter((m) => m.mine === true).length === 1;
  $('delete-meal-text').textContent = date ? t('meal.delete_text_dated', { date }) : t('meal.delete_text');
  $('delete-meal-last').textContent = last ? t('meal.delete_last', { country: countryDetail.name }) : '';
  $('delete-meal-last').hidden = !last;
  deleteMealDialog.showModal();
}

bindForm($('delete-meal-form'), async () => {
  if (!mealDelete) fail(t('client.meal_not_found'), 'meal_not_found');
  const id = String(mealDelete.id);
  if (!ID_RE.test(id)) fail(t('client.meal_not_found'), 'meal_not_found');
  const token = routeToken;
  const dt = dialogToken;
  // The response's still_done isn't needed: the country page is re-fetched below.
  await passportRequest('POST', `/meals/${id}/delete`);
  if (dt === dialogToken) deleteMealDialog.close();
  toast(t('toast.meal_deleted'));
  if (token === routeToken) refreshCountry(token);
});

function personRowHtml(m, removable) {
  const badges = [];
  if (m.is_owner) badges.push([t('role.owner'), 'badge-accent']);
  if (m.id === state.user.id) badges.push([t('people.you'), '']);
  const name = memberName(m);
  const remove = t('people.remove_label', { name: m.display_name, username: m.username });
  const removeText = t('people.remove');
  return '<li class="person-row"><span class="avatar-slot"></span><div class="person-text">' // i18n-ok HTML markup
    + `<span class="person-name">${esc(name)}</span>` // i18n-ok HTML markup
    + `<span class="person-username">@${esc(m.username)}</span>` // i18n-ok HTML markup
    + (badges.length ? `<span class="badges">${badgesHtml(badges)}</span>` : '') // i18n-ok HTML markup
    + '</div>'
    + (removable
      ? `<button type="button" class="btn" data-remove-user-id="${esc(String(m.id))}" aria-label="${esc(remove)}">${esc(removeText)}</button>` // i18n-ok HTML markup
      : '')
    + '</li>';
}

function showPassport(detail) {
  passport = detail;
  const p = detail.passport;
  const owner = p.role === 'owner';
  $('passport-name').textContent = p.name;
  $('passport-owner').textContent = ownerLine(p, false);
  $('passport-owner').prepend(avatarEl(p.owner ? p.owner.avatar_seed : null, 'avatar-sm'));
  const id = String(p.id);
  for (const linkId of ['people-back', 'country-back']) {
    $(linkId).textContent = p.name;
    $(linkId).setAttribute('href', ID_RE.test(id) ? `#/passports/${id}` : '#/passports');
  }
  $('passport-default').hidden = !p.is_default;
  $('make-default').hidden = p.is_default;
  $('participants-list').innerHTML = detail.participants.map((m) => personRowHtml(m, owner && !m.is_owner)).join('');
  fillAvatarSlots($('participants-list'), detail.participants, 'avatar-sm');
  $('viewers-list').innerHTML = detail.viewers.map((m) => personRowHtml(m, owner)).join('');
  fillAvatarSlots($('viewers-list'), detail.viewers, 'avatar-sm');
  $('viewers-list').hidden = detail.viewers.length === 0;
  $('viewers-empty').hidden = detail.viewers.length > 0;
  $('personal-note').hidden = !p.is_personal;
  $('add-person-open').hidden = !owner;
  $('passport-manage').hidden = !owner;
  $('passport-danger').hidden = owner && p.is_personal;
  $('delete-passport-open').hidden = !owner;
  $('leave-open').hidden = owner;
}

// After an awaited action, redraw only if no render happened meanwhile.
function showPassportIfCurrent(token, detail) {
  if (token === routeToken) showPassport(detail);
}

// These errors (status, code) mean the page is out of date (ownership,
// membership, role or meals changed elsewhere); the current page is re-fetched.
const STALE_ERRORS = [
  [403, 'owner_only'],
  [404, 'not_member'],
  [409, 'owner_leave'],
  [404, 'meal_not_found'],
  [403, 'participants_only'],
];

// A request on the open passport. A stale page is re-fetched; a passport that
// has gone sends the user to the list. The error is rethrown, and bindForm
// toasts it because the dialog is closed by then.
async function passportRequest(method, suffix, body) {
  // Cleared by logout (clearPassportUi) while a dialog was submitting.
  if (!passport) throw new QuietError();
  const token = routeToken;
  try {
    return await api(method, passportPath(passport.passport.id, suffix), body);
  } catch (err) {
    if (token === routeToken) {
      if (err.status === 404 && err.code === 'passport_not_found') {
        closeDialogs();
        replaceRoute('#/passports');
      } else if (STALE_ERRORS.some(([status, code]) => err.status === status && err.code === code)) {
        closeDialogs();
        render();
      }
    }
    throw err;
  }
}

$('make-default').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  if (button.disabled || !passport) return;
  button.disabled = true;
  const token = routeToken;
  const id = passport.passport.id;
  try {
    if (!ID_RE.test(String(id))) fail(t('client.passport_not_found'), 'passport_not_found');
    await api('PATCH', '/api/account', { default_passport_id: id });
    if (token === routeToken) {
      passport.passport.is_default = true;
      showPassport(passport);
    }
    toast(t('toast.default_set'));
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

// ---- Passports: add person ----

bindDialog('add-person-open', 'add-person-dialog', () => {
  const personal = passport.passport.is_personal;
  const status = $('add-person-status');
  $('add-person-participant').disabled = personal;
  $('add-person-participant').checked = !personal;
  $('add-person-viewer').checked = personal;
  $('add-person-user').replaceChildren();
  $('add-person-fields').hidden = true;
  $('add-person-roles').hidden = true;
  $('add-person-submit').disabled = true;
  loadPickUsers($('add-person-form'), status, (users) => {
    const members = new Set([...passport.participants, ...passport.viewers].map((m) => m.id));
    const candidates = users.filter((u) => !members.has(u.id));
    if (!candidates.length) {
      status.textContent = t('people.everyone_added');
      status.hidden = false;
      return;
    }
    $('add-person-user').replaceChildren(...candidates.map((u) => new Option(personLabel(u), String(u.id))));
    showAddPersonAvatar();
    $('add-person-fields').hidden = false;
    $('add-person-roles').hidden = false;
    $('add-person-submit').disabled = false;
  });
});

// A <select> option can't hold an SVG, so the chosen person's avatar sits beside it.
function showAddPersonAvatar() {
  const user = pickUsers.find((u) => String(u.id) === $('add-person-user').value);
  $('add-person-avatar').replaceChildren(avatarEl(user ? user.avatar_seed : null, 'avatar-sm'));
}

$('add-person-user').addEventListener('change', showAddPersonAvatar);

bindForm($('add-person-form'), async (form) => {
  const user = pickUsers.find((u) => String(u.id) === $('add-person-user').value);
  if (!user) fail(t('error.choose_people'));
  const role = form.querySelector('input[name="role"]:checked')?.value;
  if (role !== 'participant' && role !== 'viewer') fail(t('error.role_invalid'));
  const token = routeToken;
  const detail = await passportRequest('POST', '/members', { ...personRef(user), role });
  $('add-person-dialog').close();
  showPassportIfCurrent(token, detail);
  toast(t('toast.person_added', { name: user.display_name }));
});

// ---- Passports: remove person and leave (one shared confirmation dialog) ----

let passportConfirm = null;
const passportConfirmDialog = setupDialog('passport-confirm-dialog');
passportConfirmDialog.addEventListener('close', () => { passportConfirm = null; });

function openPassportConfirm(title, text, button, run) {
  passportConfirm = run;
  $('passport-confirm-title').textContent = title;
  $('passport-confirm-text').textContent = text;
  $('passport-confirm-submit').textContent = button;
  passportConfirmDialog.showModal();
}

bindForm($('passport-confirm-form'), async () => {
  if (passportConfirm) await passportConfirm();
});

$('people-card').addEventListener('click', (event) => {
  const button = event.target.closest('[data-remove-user-id]');
  if (!button || !passport) return;
  const uid = button.dataset.removeUserId;
  if (!ID_RE.test(uid)) return;
  const participant = passport.participants.find((m) => String(m.id) === uid);
  const member = participant || passport.viewers.find((m) => String(m.id) === uid);
  if (!member) return;
  const text = participant
    ? t('people.remove_participant_text', { person: personLabel(member) })
    : t('people.remove_viewer_text', { person: personLabel(member) });
  openPassportConfirm(t('people.remove_title'), text, t('people.remove'), async () => {
    const token = routeToken;
    const detail = await passportRequest('POST', `/members/${uid}/remove`);
    passportConfirmDialog.close();
    showPassportIfCurrent(token, detail);
    toast(t('toast.removed'));
  });
});

$('leave-open').addEventListener('click', () => {
  if (!passport) return;
  const { name, role } = passport.passport;
  const text = role === 'viewer'
    ? t('people.leave_viewer_text', { name })
    : t('people.leave_participant_text', { name });
  openPassportConfirm(t('people.leave_passport'), text, t('people.leave'), async () => {
    const token = routeToken;
    await passportRequest('POST', '/leave');
    passportConfirmDialog.close();
    if (token === routeToken) replaceRoute('#/');
    toast(t('toast.left', { name }));
  });
});

// ---- Passports: rename and delete ----

bindDialog('rename-open', 'rename-dialog', () => {
  $('rename-name').value = passport.passport.name;
});

bindForm($('rename-form'), async () => {
  const name = $('rename-name').value;
  checkPassportName(name);
  const token = routeToken;
  const detail = await passportRequest('PATCH', '', { name });
  $('rename-dialog').close();
  showPassportIfCurrent(token, detail);
  toast(t('toast.passport_renamed'));
});

const typedNameMatches = () => passport !== null
  && $('delete-passport-confirm').value.normalize('NFC').trim() === passport.passport.name;

bindDialog('delete-passport-open', 'delete-passport-dialog', () => {
  $('delete-passport-text').textContent = t('people.delete_text', { name: passport.passport.name });
  $('delete-passport-submit').disabled = true;
});

$('delete-passport-confirm').addEventListener('input', () => {
  $('delete-passport-submit').disabled = !typedNameMatches();
});

bindForm($('delete-passport-form'), async () => {
  if (!typedNameMatches()) fail(t('error.confirm_passport_name'));
  const token = routeToken;
  await passportRequest('POST', '/delete', { confirm_name: $('delete-passport-confirm').value });
  $('delete-passport-dialog').close();
  if (token === routeToken) replaceRoute('#/');
  toast(t('toast.passport_deleted'));
});

// ---- Forced password change (temporary password set by an admin) ----

bindForm($('force-password-form'), async (form) => {
  const current = form.elements.current_password.value;
  const next = form.elements.new_password.value;
  if (!current) fail(t('client.temp_password_empty'));
  checkNewPassword(next);
  if (next !== form.elements.confirm_password.value) fail(t('client.passwords_mismatch'));
  await api('POST', '/api/account/password', { current_password: current, new_password: next });
  form.reset();
  // The change succeeded even if this re-fetch fails, so clear the flag locally then.
  if (!(await refreshSession()) && state.user) state.user.must_change_password = false;
  history.replaceState(null, '', '#/');
  render();
  toast(t('toast.password_changed'));
});

// ---- Admin: user list ----

function userBadges(u) {
  const badges = [];
  if (u.id === state.user.id) badges.push([t('people.you'), '']);
  if (u.is_admin) badges.push([t('admin.badge_admin'), 'badge-accent']);
  if (u.disabled) badges.push([t('admin.badge_disabled'), 'badge-warn']);
  if (u.locked) badges.push([t('admin.badge_locked'), 'badge-warn']);
  // An expired temporary password implies "must change", so show only the stronger badge.
  if (u.temp_password_expired) badges.push([t('admin.badge_temp_expired'), 'badge-warn']);
  else if (u.must_change_password) badges.push([t('admin.badge_must_change'), '']);
  return badges;
}

function userCardHtml(u) {
  const badges = badgesHtml(userBadges(u));
  return `<li><button type="button" class="list-card" data-user-id="${esc(String(u.id))}">` // i18n-ok HTML markup
    + `<span class="list-card-title"><span class="avatar-slot"></span>${esc(u.display_name)}</span>` // i18n-ok HTML markup
    + `<span class="list-card-sub">@${esc(u.username)}</span>` // i18n-ok HTML markup
    + (badges ? `<span class="badges">${badges}</span>` : '') // i18n-ok HTML markup
    + '</button></li>';
}

// _param is unused: #/admin has no path parameter (every route gets one argument slot).
async function enterAdmin(_param, token) {
  let users;
  try {
    ({ users } = await api('GET', '/api/admin/users'));
  } catch (err) {
    if (token === routeToken) showLoadError(err.message);
    return;
  }
  if (token !== routeToken) return;
  $('admin-users').innerHTML = users.map(userCardHtml).join('');
  fillAvatarSlots($('admin-users'), users, 'avatar-sm');
  showView('admin');
}

$('admin-users').addEventListener('click', (event) => {
  const card = event.target.closest('[data-user-id]');
  if (!card) return;
  const id = card.dataset.userId;
  if (ID_RE.test(id)) location.hash = `#/admin/users/${id}`;
});

bindDialog('add-user-open', 'add-user-dialog');

bindForm($('add-user-form'), async (form) => {
  const username = form.elements.username.value;
  const displayName = form.elements.display_name.value;
  const password = form.elements.password.value;
  checkUsername(username);
  checkDisplayName(displayName);
  checkNewPassword(password);
  const token = routeToken;
  const data = await api('POST', '/api/admin/users', {
    username, display_name: displayName, password, is_admin: form.elements.is_admin.checked,
  });
  form.reset();
  $('add-user-dialog').close();
  toast(t('toast.user_created', { name: data.user.display_name }));
  // Refresh the list only if the admin is still on it.
  if (token === routeToken) enterAdmin(null, token);
});

// Fills a temporary-password input; 224 = 4 * 56, so rejecting bytes >= 224
// keeps every character equally likely.
const TEMP_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';

function generatePassword(length = 16) {
  const bytes = new Uint8Array(32);
  let out = '';
  while (out.length < length) {
    crypto.getRandomValues(bytes);
    for (const b of bytes) {
      if (b < 224 && out.length < length) out += TEMP_ALPHABET[b % TEMP_ALPHABET.length];
    }
  }
  return out;
}

document.querySelectorAll('[data-generate]').forEach((button) => {
  button.addEventListener('click', () => {
    $(button.dataset.generate).value = generatePassword();
  });
});

// ---- Admin: one user ----

let adminUser = null;

const yesNo = (v) => (v ? t('common.yes') : t('common.no'));

function formatDate(seconds) {
  return typeof seconds === 'number' ? new Date(seconds * 1000).toLocaleDateString() : t('admin.never');
}

// API paths are built only from an id that matches the id pattern.
function adminUserPath(suffix = '') {
  const id = String(adminUser.id);
  if (!ID_RE.test(id)) fail(t('client.invalid_user_id'));
  return `/api/admin/users/${id}${suffix}`;
}

async function enterAdminUser(id, token) {
  // The route regex already enforces this; re-checked because the id ends up
  // in API paths (defence in depth at a security boundary).
  if (!ID_RE.test(id)) {
    history.replaceState(null, '', '#/');
    enterHome(null, token);
    return;
  }
  let users;
  try {
    ({ users } = await api('GET', '/api/admin/users'));
  } catch (err) {
    if (token === routeToken) showLoadError(err.message);
    return;
  }
  if (token !== routeToken) return;
  const user = users.find((u) => String(u.id) === id);
  if (!user) {
    toast(t('client.user_not_found'), 'error');
    navigate('#/admin');
    return;
  }
  showAdminUser(user);
  showView('admin-user');
}

function showAdminUser(u) {
  adminUser = u;
  const self = u.id === state.user.id;
  $('admin-user-title').textContent = u.display_name;
  $('admin-user-title').prepend(avatarEl(u.avatar_seed, 'avatar-md'));
  $('au-username').textContent = u.username;
  $('au-role').textContent = u.is_admin ? t('admin.role_admin') : t('admin.role_user');
  $('au-status').textContent = u.disabled ? t('admin.status_disabled') : t('admin.status_active');
  $('au-locked').textContent = yesNo(u.locked);
  $('au-must-change').textContent = u.temp_password_expired
    ? t('admin.must_change_expired') : yesNo(u.must_change_password);
  $('au-created').textContent = formatDate(u.created_at);
  $('au-last-active').textContent = formatDate(u.last_active_at);

  $('au-reset').hidden = self;
  $('au-reset-self').hidden = !self;
  $('au-unlock').hidden = !u.locked;
  $('au-disable').hidden = self || u.disabled;
  $('au-enable').hidden = !u.disabled;
  $('au-promote').hidden = u.is_admin;
  $('au-demote').hidden = !u.is_admin;
  $('au-delete').hidden = self;
  $('au-delete-self').hidden = !self;
}

// After an awaited action, redraw only if no render happened meanwhile, so a
// late response for one user can't overwrite another user's view (or another view).
function showAdminUserIfCurrent(token, user) {
  if (token === routeToken) showAdminUser(user);
}

bindDialog('au-edit-name', 'edit-name-dialog', () => {
  $('edit-display-name').value = adminUser.display_name;
});

bindForm($('edit-name-form'), async (form) => {
  const displayName = form.elements.display_name.value;
  checkDisplayName(displayName);
  const token = routeToken;
  const { user } = await api('PATCH', adminUserPath(), { display_name: displayName });
  if (user.id === state.user.id) {
    state.user.display_name = user.display_name;
    updateUserText();
  }
  $('edit-name-dialog').close();
  showAdminUserIfCurrent(token, user);
  toast(t('toast.display_name_saved'));
});

bindDialog('au-reset', 'reset-dialog', () => {
  $('reset-text').textContent = t('admin.reset_text', { name: adminUser.display_name });
});

bindForm($('reset-form'), async (form) => {
  const password = form.elements.new_password.value;
  checkNewPassword(password);
  const token = routeToken;
  const { user } = await api('POST', adminUserPath('/password'), { new_password: password });
  form.reset();
  $('reset-dialog').close();
  showAdminUserIfCurrent(token, user);
  toast(t('toast.password_reset'));
});

$('au-unlock').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  const token = routeToken;
  try {
    const { user } = await api('POST', adminUserPath('/unlock'));
    showAdminUserIfCurrent(token, user);
    toast(t('toast.unlocked'));
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

// Disable, Enable, Make admin and Remove admin share one confirmation dialog.
const CONFIRM_ACTIONS = {
  'au-disable': {
    title: () => t('admin.disable_title'),
    button: () => t('admin.disable'),
    path: '/disable',
    done: () => t('toast.disabled'),
    text: (name) => t('admin.disable_text', { name }),
  },
  'au-enable': {
    title: () => t('admin.enable_title'),
    button: () => t('admin.enable'),
    path: '/enable',
    done: () => t('toast.enabled'),
    text: (name) => t('admin.enable_text', { name }),
  },
  'au-promote': {
    title: () => t('admin.promote'),
    button: () => t('admin.promote'),
    path: '/promote',
    done: () => t('toast.promoted'),
    text: (name) => t('admin.promote_text', { name }),
  },
  'au-demote': {
    title: () => t('admin.demote'),
    button: () => t('admin.demote'),
    path: '/demote',
    done: () => t('toast.demoted'),
    text: (name, self) => (self ? t('admin.demote_self_text') : t('admin.demote_text', { name })),
  },
};

let confirmAction = null;
const confirmDialog = setupDialog('confirm-dialog');
confirmDialog.addEventListener('close', () => { confirmAction = null; });

for (const [buttonId, action] of Object.entries(CONFIRM_ACTIONS)) {
  $(buttonId).addEventListener('click', () => {
    confirmAction = action;
    $('confirm-title').textContent = action.title();
    $('confirm-text').textContent = action.text(adminUser.display_name, adminUser.id === state.user.id);
    $('confirm-submit').textContent = action.button();
    confirmDialog.showModal();
  });
}

bindForm($('confirm-form'), async () => {
  const action = confirmAction;
  if (!action) return;
  const self = adminUser.id === state.user.id;
  const token = routeToken;
  const { user } = await api('POST', adminUserPath(action.path));
  confirmDialog.close();
  if (action.path === '/demote' && self) {
    if (!(await refreshSession()) && state.user) state.user.is_admin = false;
    // Go home if still here; if the admin moved on, re-render where they are
    // (the router moves them off any admin route).
    if (token === routeToken) navigate('#/');
    else render();
    toast(t('toast.admin_lost'));
    return;
  }
  showAdminUserIfCurrent(token, user);
  toast(action.done());
});

const deleteMatches = () => $('admin-delete-confirm').value.toLowerCase() === adminUser.username.toLowerCase();

bindDialog('au-delete', 'admin-delete-dialog', () => {
  $('admin-delete-text').textContent = t('admin.delete_text', { name: adminUser.display_name });
  $('admin-delete-label').textContent = t('admin.delete_label', { username: adminUser.username });
  $('admin-delete-submit').disabled = true;
});

$('admin-delete-confirm').addEventListener('input', () => {
  $('admin-delete-submit').disabled = !deleteMatches();
});

bindForm($('admin-delete-form'), async (form) => {
  if (!deleteMatches()) fail(t('error.confirm_username'));
  const token = routeToken;
  await api('POST', adminUserPath('/delete'), { confirm_username: form.elements.confirm_username.value });
  $('admin-delete-dialog').close();
  if (token === routeToken) navigate('#/admin');
  toast(t('toast.account_deleted'));
});

// ---- Start ----

window.addEventListener('hashchange', () => {
  if (sessionLoaded && !sessionPending) render();
});

$('load-retry').addEventListener('click', loadSession);

applyPageStrings();
loadSession();
