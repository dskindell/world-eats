'use strict';

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
  .map(([text, cls]) => `<span class="badge ${cls}">${esc(text)}</span>`)
  .join('');

function textEl(tag, text, className = '') {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
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
    throw new ApiError('Could not reach the server. Check your connection and try again.', 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.ok && data !== null && typeof data === 'object') return data;

  const fallback = `Something went wrong (HTTP ${Number(res.status)})`;
  const message = !res.ok && data && typeof data.error === 'string' ? data.error : fallback;
  if (res.status === 401 && path !== '/api/login' && path !== '/api/session') {
    onSessionLost();
  } else if (res.status === 403 && data && data.password_change_required === true) {
    onPasswordChangeRequired();
  } else if (res.status === 403 && path.startsWith('/api/admin/') && message === 'Admins only') {
    onAdminLost();
  }
  throw new ApiError(message, res.status);
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

const fail = (message) => { throw new Error(message); };

// Abandons an action without telling the user, e.g. when the session ended
// and the page has already been cleared and re-rendered.
class QuietError extends Error {}

// ---- Client-side checks mirroring contract §4 (the server is authoritative) ----

const USERNAME_RE = /^[A-Za-z0-9._-]{3,32}$/;
// Cc, Cs, Co, Cn, Zl, Zp, and Cf except U+200D (zero-width joiner).
const BAD_NAME_CHAR_RE = /[\p{Cc}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]|(?!\u200D)\p{Cf}/u;
const codePoints = (s) => [...s].length;

function checkUsername(value) {
  if (!USERNAME_RE.test(value)) fail('Username must be 3-32 characters: letters, digits, . _ -');
}

// Display names and passport names share one rule set, with different messages.
function checkName(value, label) {
  const name = value.normalize('NFC').trim();
  const n = codePoints(name);
  if (n < 1 || n > 50) fail(`${label} must be 1-50 characters`);
  if (BAD_NAME_CHAR_RE.test(name)) fail(`${label} contains characters that aren't allowed`);
}

const checkDisplayName = (value) => checkName(value, 'Display name');
const checkPassportName = (value) => checkName(value, 'Passport name');

function checkNewPassword(value) {
  const n = codePoints(value);
  if (n < 12 || n > 128 || /\p{Cs}/u.test(value)) fail('Password must be 12-128 characters');
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
  if (state.user && !state.user.is_admin) toast("You're no longer an admin");
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
  if (!username || !password) fail('Enter your username and password');
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
  if (event.key === 'Escape' && !$('user-menu').hidden) closeMenu(true);
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
  toast('Display name saved');
});

bindForm($('password-form'), async (form) => {
  const current = form.elements.current_password.value;
  const next = form.elements.new_password.value;
  if (!current) fail('Enter your current password');
  checkNewPassword(next);
  if (next !== form.elements.confirm_password.value) fail("Passwords don't match");
  await api('POST', '/api/account/password', { current_password: current, new_password: next });
  form.reset();
  toast('Password changed. Other devices were logged out.');
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
  if (select.disabled || !ID_RE.test(select.value)) fail('Choose a passport you belong to');
  await api('PATCH', '/api/account', { default_passport_id: Number(select.value) });
  toast('Default passport saved');
});

function defaultOptionText(p) {
  if (p.role !== 'owner') return `${p.name} (${personLabel(p.owner)})`;
  return p.is_personal ? `${p.name} (personal)` : p.name;
}

async function loadDefaultSelect(token) {
  const select = $('default-passport');
  select.disabled = true;
  select.replaceChildren(new Option('Loading…', ''));
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
  const lines = ['Your personal passport will be deleted.'];
  for (const h of preview.handed_over) lines.push(`Handed over: ${h.name} → ${personLabel(h.new_owner)}`);
  for (const d of preview.deleted) lines.push(`Deleted: ${d.name}`);
  const left = Number(preview.left);
  if (left > 0) lines.push(`You'll be removed from ${left} other ${left === 1 ? 'passport' : 'passports'}.`);
  lines.push('All your meals will be deleted.');
  return lines;
}

// The delete button stays disabled until the preview has loaded, so the
// consequences are always on screen before the account can be deleted.
async function loadDeletePreview() {
  const t = ++dialogToken;
  const box = $('delete-preview');
  box.replaceChildren(textEl('p', 'Checking what happens to your passports…', 'muted'));
  $('delete-submit').disabled = true;
  let preview;
  try {
    preview = await api('GET', '/api/account/delete-preview');
  } catch (err) {
    if (t === dialogToken) {
      box.replaceChildren();
      setError($('delete-form'), err.message);
    }
    return;
  }
  if (t !== dialogToken) return;
  box.replaceChildren(...previewLines(preview).map((line) => textEl('p', line)));
  $('delete-submit').disabled = false;
}

bindForm($('logout-all-form'), async () => {
  await api('POST', '/api/account/logout-everywhere');
  onSessionLost();
});

bindForm($('delete-form'), async (form) => {
  const password = form.elements.password.value;
  if (!password) fail('Enter your password');
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

const ROLE_LABELS = { owner: 'Owner', participant: 'Participant', viewer: 'Viewer' };
// Display names aren't unique, so a person is always named with @username too.
const personLabel = (p) => `${p.display_name}${p.inactive ? ' (inactive)' : ''} @${p.username}`;
const personRef = (u) => ({ user_id: u.id, username: u.username });

// API paths are built only from an id that matches the id pattern.
function passportPath(id, suffix = '') {
  const s = String(id);
  if (!ID_RE.test(s)) fail('Passport not found');
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
  dropCountries();
  clearBrowse();
  dialogToken += 1;
  for (const id of ['passport-list', 'participants-list', 'viewers-list', 'new-passport-people',
    'add-person-user', 'default-passport', 'delete-preview', 'country-list', 'country-participants', 'meal-list']) {
    $(id).replaceChildren();
  }
  for (const id of ['passport-name', 'passport-owner', 'delete-passport-text', 'passport-confirm-text',
    'passport-progress', 'people-back', 'country-back', 'country-flag', 'country-name', 'country-region',
    'country-status', 'country-avg', 'meal-title', 'delete-meal-text', 'delete-meal-last']) {
    $(id).textContent = '';
  }
  $('meal-form').reset();
  $('delete-meal-form').reset();
}

// ---- Passports: list ----

// inList: the list card's shorter wording for your own passports.
function ownerLine(p, inList) {
  const own = p.role === 'owner';
  if (p.is_personal && own) return inList ? 'Personal' : 'Personal passport';
  if (p.is_personal) return `Personal passport of ${personLabel(p.owner)}`;
  return own && inList ? 'Owned by you' : `Owned by ${personLabel(p.owner)}`;
}

function passportCardHtml(p) {
  const badges = [];
  if (p.is_default) badges.push(['Default', 'badge-accent']);
  if (ROLE_LABELS[p.role]) badges.push([ROLE_LABELS[p.role], '']);
  return `<li><button type="button" class="list-card" data-passport-id="${esc(String(p.id))}">`
    + `<span class="list-card-title">${esc(p.name)}</span>`
    + `<span class="list-card-sub">${esc(ownerLine(p, true))}</span>`
    + `<span class="badges">${badgesHtml(badges)}</span>`
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
  const t = ++dialogToken;
  status.textContent = 'Loading people…';
  status.hidden = false;
  let users;
  try {
    ({ users } = await api('GET', '/api/users'));
  } catch (err) {
    if (t === dialogToken) {
      status.hidden = true;
      setError(form, err.message);
    }
    return;
  }
  if (t !== dialogToken) return;
  pickUsers = users.filter((u) => ID_RE.test(String(u.id)));
  status.hidden = true;
  onLoaded(pickUsers);
}

function pickRowHtml(u) {
  const id = esc(String(u.id));
  return `<li class="pick-row"><label for="new-passport-person-${id}">`
    + `<span class="person-name">${esc(u.display_name)}</span>`
    + `<span class="person-username">@${esc(u.username)}</span></label>`
    + `<select id="new-passport-person-${id}" data-user-id="${id}">`
    + '<option value="">Not added</option>'
    + '<option value="participant">Participant</option>'
    + '<option value="viewer">Viewer</option>'
    + '</select></li>';
}

bindDialog('new-passport-open', 'new-passport-dialog', () => {
  const form = $('new-passport-form');
  const status = $('new-passport-status');
  $('new-passport-people').replaceChildren();
  loadPickUsers(form, status, (users) => {
    $('new-passport-people').innerHTML = users.map(pickRowHtml).join('');
    status.textContent = "There's nobody else to add yet.";
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
  if (participants.length + viewers.length > 19) fail('A passport can have at most 20 people');
  const token = routeToken;
  const detail = await api('POST', '/api/passports', { name, participants, viewers });
  $('new-passport-dialog').close();
  toast('Passport created');
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

const isStars = (r) => Number.isInteger(r) && r >= 1 && r <= 5;

function starsEl(r) {
  const el = textEl('span', '★'.repeat(r) + '☆'.repeat(5 - r), 'stars');
  el.setAttribute('role', 'img');
  el.setAttribute('aria-label', `${r} out of 5 stars`);
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
  if (err.status === 404 && err.message === 'Country not found') {
    toast('Country not found', 'error');
    replaceRoute(`#/passports/${id}`);
  } else if (err.status === 404) {
    toast('Passport not found', 'error');
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
  showCountryList(summary, countries);
  showView('passport');
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
    toast('Country not found', 'error');
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

// Fixed labels and classes; `status` itself never reaches the HTML.
const STATUS_BADGES = {
  not_started: ['Not started', 'badge-status-none'],
  some: ['Some', 'badge-status-some'],
  complete: ['Fully complete', 'badge-status-complete'],
};
const statusBadge = (status) => (typeof status === 'string' && Object.hasOwn(STATUS_BADGES, status)
  ? STATUS_BADGES[status] : null);
const count = (n) => (Number.isInteger(n) && n >= 0 ? n : 0);
// A personal passport has one participant, so any meal means it's done.
const personalDone = (s) => s.status === 'some' || s.status === 'complete';

function progressText(progress, rows, personal) {
  const total = Number.isInteger(progress.total) ? progress.total : rows.length;
  // Counted from the rows so a viewer (mine is null) sees it too.
  if (personal) return `Done: ${rows.filter(personalDone).length} / ${total}`;
  const mine = Number.isInteger(progress.mine) ? ` · You: ${count(progress.mine)} / ${total}` : '';
  return `Fully complete: ${count(progress.complete)} / ${total}${mine}`;
}

function countryRowHtml(s, country, personal, hint) {
  const badges = [];
  if (personal) {
    if (personalDone(s)) badges.push(['✓ Done', 'badge-status-complete']);
  } else {
    const status = statusBadge(s.status);
    if (status) badges.push(status);
    if (s.done_by_me === true) badges.push(['✓ You', '']);
  }
  return `<li><button type="button" class="country-row" data-code="${esc(country.code)}">`
    + `<span class="flag" aria-hidden="true">${esc(flagFor(country.code))}</span>`
    + `<span class="country-row-name">${esc(country.name)}</span>`
    + `<span class="badges">${badgesHtml(badges)}</span>`
    + (hint ? `<span class="country-row-hint">${esc(hint)}</span>` : '')
    + '</button></li>';
}

function showCountryList(data, countries) {
  const personal = passport.passport.is_personal === true;
  const progress = data.progress && typeof data.progress === 'object' ? data.progress : {};
  const rows = (Array.isArray(data.countries) ? data.countries : [])
    .filter((s) => s && typeof s.code === 'string' && CODE_RE.test(s.code) && countries.has(s.code));
  $('passport-progress').textContent = progressText(progress, rows, personal);
  const active = progress.active_participants;
  browseRows = rows.map((s) => rowModel(s, countries.get(s.code), personal, active));
  showBrowseControls();
  applyBrowse();
}

$('country-list').addEventListener('click', (event) => {
  const row = event.target.closest('[data-code]');
  if (!row || !passport || !countriesCache) return;
  const code = row.dataset.code;
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
});

// ---- Passport page: search, sort and filters (kept in the address) ----

const SORTS = ['name', 'region', 'completed_desc', 'completed_asc', 'rating', 'progress'];
const STATUS_KEYS = ['not_started', 'some', 'complete'];
const PERSONAL_STATUS_KEYS = ['not_started', 'complete'];
const MINE_KEYS = ['done', 'not_done'];
const STARS_RE = /^[1-5]$/;
const MAX_QUERY = 200;

// The open passport list's validated state ({id, opts, q, sort, status, mine,
// region, stars}), its rows, and the search results. Dropped by clearBrowse().
let browse = null;
let browseRows = [];
let search = { on: false, pending: false, matches: null };
// Bumped by every new search (and on clearing), so older responses are dropped.
let searchToken = 0;
let searchTimer = 0;

// Which parameters apply depends on the passport: personal passports have two
// statuses, and only participants of shared passports have "mine".
function browseOptions(p) {
  const personal = p.is_personal === true;
  return {
    personal,
    statuses: personal ? PERSONAL_STATUS_KEYS : STATUS_KEYS,
    mine: !personal && (p.role === 'owner' || p.role === 'participant'),
  };
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
  const stars = one('stars');
  return {
    id,
    opts,
    q: q !== null && codePoints(q) <= MAX_QUERY ? q : '',
    sort: SORTS.includes(sort) ? sort : 'name',
    status: pickList(one('status'), opts.statuses),
    mine: opts.mine && MINE_KEYS.includes(mine) ? mine : '',
    region: pickList(one('region'), regionList()),
    stars: stars !== null && STARS_RE.test(stars) ? Number(stars) : 0,
  };
}

const validQuery = (b) => b.q.trim() !== '' && codePoints(b.q) <= MAX_QUERY;
const filterCount = (b) => [b.status.length, b.mine, b.region.length, b.stars].filter(Boolean).length;

// The canonical address, built only from validated values; defaults are omitted.
function browseHash(b) {
  const id = String(b.id);
  if (!ID_RE.test(id)) return '#/passports';
  const params = new URLSearchParams();
  if (validQuery(b)) params.set('q', b.q);
  if (b.sort !== 'name') params.set('sort', b.sort);
  if (b.status.length) params.set('status', b.status.join(','));
  if (b.mine) params.set('mine', b.mine);
  if (b.region.length) params.set('region', b.region.join(','));
  if (b.stars) params.set('stars', String(b.stars));
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
  };
}

// On a personal passport "Done" is any status other than not started.
function statusMatches(r, status, personal) {
  if (personal && status === 'complete') return personalDone(r.s);
  return r.s.status === status;
}

function rowShown(r, b) {
  if (search.on && !(search.matches && search.matches.has(r.code))) return false;
  if (b.status.length && !b.status.some((st) => statusMatches(r, st, b.opts.personal))) return false;
  if (b.mine && (r.s.done_by_me === true) !== (b.mine === 'done')) return false;
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
      if (regions.includes(region)) html += `<li class="region-heading"><h3>${esc(region)}</h3></li>`;
    }
    const hint = search.on && search.matches ? search.matches.get(r.code) : '';
    html += countryRowHtml(r.s, r.country, b.opts.personal, hint);
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
  $('country-list').innerHTML = countryListHtml(rows, b);
  const n = filterCount(b);
  $('filters-open').textContent = n ? `Filters (${n})` : 'Filters';
  $('browse-count-row').hidden = !(b.q.trim() || n);
  $('browse-count').textContent = search.pending ? 'Searching…' : `Showing ${rows.length} of ${browseRows.length}`;
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
  $('sort-completed-desc').textContent = personal ? 'Done (newest)' : 'Completed (newest)';
  $('sort-completed-asc').textContent = personal ? 'Done (oldest)' : 'Completed (oldest)';
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
  $('browse-q').value = '';
  $('browse-sort').value = 'name';
  $('filters-open').textContent = 'Filters';
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
  if (notes) return `Meal notes: ${notes}`;
  const dish = str(meal.dish);
  const place = str(meal.place);
  if (dish && place) return `Meal: ${dish} at ${place}`;
  if (dish) return `Meal: ${dish}`;
  return place ? `Meal: at ${place}` : '';
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
  const t = ++searchToken;
  const { q } = browse;
  showSearchError('');
  if (!q.trim() || codePoints(q) > MAX_QUERY) {
    search = { on: false, pending: false, matches: null };
    if (q.trim()) showSearchError('Search can be up to 200 characters');
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
    if (t !== searchToken || token !== routeToken) return;
    if (err.status === 404 && err.message === 'Passport not found') {
      toast(err.message, 'error');
      replaceRoute('#/passports');
      return;
    }
    search = { on: false, pending: false, matches: null };
    showSearchError(err.message);
    applyBrowse();
    return;
  }
  if (t !== searchToken || token !== routeToken || !browse) return;
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
  Object.assign(browse, { q: '', sort: 'name', status: [], mine: '', region: [], stars: 0 });
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
  label.append(input, ` ${region}`);
  return label;
}

function syncFilters() {
  const form = $('filters-form');
  const { opts } = browse;
  $('filter-status-none').textContent = opts.personal ? 'Not done' : 'Not started';
  $('filter-status-complete').textContent = opts.personal ? 'Done' : 'Fully complete';
  $('filter-status-some-row').hidden = opts.personal;
  for (const box of form.querySelectorAll('input[name="status"]')) box.checked = browse.status.includes(box.value);
  $('filters-mine').hidden = !opts.mine;
  for (const radio of form.querySelectorAll('input[name="mine"]')) radio.checked = radio.value === browse.mine;
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
  const stars = $('filters-stars').value;
  browse.status = browse.opts.statuses.filter((v) => status.has(v));
  browse.mine = browse.opts.mine && MINE_KEYS.includes(mine) ? mine : '';
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
  Object.assign(browse, { status: [], mine: '', region: [], stars: 0 });
  syncFilters();
  applyBrowse();
  writeBrowseAddress();
});

$('filters-form').addEventListener('submit', (event) => {
  event.preventDefault();
  filtersDialog.close();
});

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

const FLAP_CHARS = ' ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const PICK_TICK_MS = 60;
// Tile i stops at PICK_FIRST_MS + i * PICK_SPREAD_MS / (length - 1): the first
// at 400 ms, the last at 2500 ms (spaces count as positions).
const PICK_FIRST_MS = 400;
const PICK_SPREAD_MS = 2100;

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
  setPickEmpty(active
    ? "You've done every country that matches. Clear filters to pick from all."
    : "You've done them all!");
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
}

// One tile per code point; each word is kept together (a word wider than the
// board wraps inside itself). A space is a blank tile ending its word, so a
// line never starts with a gap.
function buildTiles(name) {
  const chars = Array.from(name.normalize('NFC').toLocaleUpperCase('en'));
  const box = $('pick-tiles');
  const step = chars.length > 1 ? PICK_SPREAD_MS / (chars.length - 1) : 0;
  let word = null;
  pickTiles = chars.map((ch, i) => {
    if (!word) {
      word = document.createElement('span');
      word.className = 'pick-word';
      box.append(word);
    }
    const el = textEl('span', ch === ' ' ? '' : ch, ch === ' ' ? 'pick-tile pick-tile-space' : 'pick-tile');
    word.append(el);
    if (ch === ' ') word = null;
    return { el, ch, stopAt: PICK_FIRST_MS + i * step, done: ch === ' ' };
  });
}

function startPick(code) {
  stopPick();
  const country = countriesCache.get(code);
  pickCurrent = { id: browse.id, code };
  lastPick = { id: browse.id, code };
  buildTiles(country.name);
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
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
  pickTiles.forEach((t, i) => {
    if (t.done) return;
    if (elapsed >= t.stopAt) {
      t.done = true;
      t.el.textContent = t.ch;
      t.el.className = 'pick-tile';
      return;
    }
    left += 1;
    t.el.textContent = FLAP_CHARS[random[i] % FLAP_CHARS.length];
    // Two identical animations under different names restart it on every flap.
    t.el.className = t.el.className === 'pick-tile flip-a' ? 'pick-tile flip-b' : 'pick-tile flip-a';
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
  for (const t of pickTiles) {
    t.el.textContent = t.ch === ' ' ? '' : t.ch;
    t.el.className = t.ch === ' ' ? 'pick-tile pick-tile-space' : 'pick-tile';
  }
  const region = typeof country.region === 'string' ? country.region : '';
  $('pick-flag').textContent = flagFor(pickCurrent.code);
  $('pick-region').textContent = region;
  $('pick-place').classList.remove('is-pending');
  $('pick-live').textContent = region ? `Next stop: ${country.name}, ${region}` : `Next stop: ${country.name}`;
  $('pick-board').disabled = true;
  pickFrame = requestAnimationFrame(() => {
    if (gen !== pickGen || !pickDialog.open) return;
    // Defensive: the board only opens for owners and participants, so canLog
    // is normally true here.
    const canLog = canLogMeals();
    $('pick-log').hidden = !canLog;
    for (const button of pickButtons()) button.disabled = false;
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

function countryStatusText(data, participants, personal) {
  if (personal) {
    const owner = participants[0];
    if (!owner || owner.done !== true) return 'Not done yet';
    const date = formatDay(owner.completed_on);
    return date ? `Done on ${date}` : 'Done';
  }
  if (data.status === 'complete') {
    const date = formatDay(data.completed_on);
    return date ? `Fully complete on ${date}` : 'Fully complete';
  }
  if (data.status === 'some') return 'Some participants have eaten here';
  if (data.status === 'not_started') return 'Not started';
  return '';
}

function countryPersonHtml(m) {
  const done = m.done === true;
  const doneText = done ? `Done ${formatDay(m.completed_on)}`.trim() : 'Not yet';
  return '<li class="person-row"><div class="person-text">'
    + `<span class="person-name">${esc(m.display_name)}${m.inactive ? ' (inactive)' : ''}</span>`
    + `<span class="person-username">@${esc(m.username)}</span></div>`
    + `<span class="done-state${done ? ' done-mark' : ' muted'}">${esc(doneText)}</span>`
    + '</li>';
}

function mealButton(label, action, id, date) {
  const button = textEl('button', label, 'btn');
  button.type = 'button';
  button.dataset.mealAction = action;
  button.dataset.mealId = id;
  button.setAttribute('aria-label', date ? `${label} meal from ${date}` : `${label} meal`);
  return button;
}

// Built with textContent throughout; notes keep their line breaks via CSS.
function mealCard(m) {
  const li = document.createElement('li');
  li.className = 'card meal-card';
  const date = formatDay(m.eaten_on);
  const head = document.createElement('div');
  head.className = 'meal-head';
  head.append(textEl('span', date, 'meal-date'));
  if (isStars(m.rating)) head.append(starsEl(m.rating));
  li.append(head);
  if (m.user) li.append(textEl('p', personLabel(m.user), 'muted'));
  if (typeof m.dish === 'string' && m.dish) li.append(textEl('p', m.dish, 'meal-dish'));
  if (typeof m.place === 'string' && m.place) li.append(textEl('p', `at ${m.place}`, 'muted'));
  if (typeof m.notes === 'string' && m.notes) li.append(textEl('p', m.notes, 'meal-notes'));
  if (m.mine === true) {
    const actions = document.createElement('div');
    actions.className = 'actions';
    const id = String(m.id);
    actions.append(mealButton('Edit', 'edit', id, date), mealButton('Delete', 'delete', id, date));
    li.append(actions);
  }
  return li;
}

function showCountry(code, data, countries) {
  const country = countries.get(code);
  const personal = passport.passport.is_personal === true;
  const participants = Array.isArray(data.participants) ? data.participants : [];
  const meals = (Array.isArray(data.meals) ? data.meals : []).filter((m) => m && ID_RE.test(String(m.id)));
  countryDetail = { code, name: country.name, meals };
  $('country-flag').textContent = flagFor(code);
  $('country-name').textContent = country.name;
  $('country-region').textContent = typeof country.region === 'string' ? country.region : '';
  $('country-status').textContent = countryStatusText(data, participants, personal);
  const avg = data.avg_rating;
  const hasAvg = typeof avg === 'number' && Number.isFinite(avg);
  $('country-avg').textContent = hasAvg ? `Average ★ ${avg.toFixed(1)}` : '';
  $('country-avg').hidden = !hasAvg;
  $('country-people-card').hidden = personal;
  $('country-participants').innerHTML = personal ? '' : participants.map(countryPersonHtml).join('');
  $('log-meal-open').hidden = !canLogMeals();
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
    const gone = err.status === 404 && err.message === 'Passport not found';
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
  $('meal-title').textContent = meal ? 'Edit meal' : `Log a meal — ${countryDetail.name}`;
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
function checkMealText(value, label) {
  const s = value.normalize('NFC').trim();
  if (codePoints(s) > 100) fail(`${label} can be up to 100 characters`);
  if (BAD_NAME_CHAR_RE.test(s)) fail(`${label} contains characters that aren't allowed`);
}

function checkNotes(value) {
  const s = value.replace(/\r\n?/g, '\n').normalize('NFC').trim();
  if (codePoints(s) > 2000) fail('Notes can be up to 2,000 characters');
  if (BAD_NAME_CHAR_RE.test(s.replace(/\n/g, ''))) fail("Notes contain characters that aren't allowed");
}

function readMealForm(form) {
  const el = form.elements;
  const eatenOn = el.eaten_on.value;
  if (!DAY_RE.test(eatenOn) || eatenOn < '1900-01-01' || eatenOn > el.eaten_on.max) {
    fail('Choose a date between 1900 and today');
  }
  checkMealText(el.dish.value, '"What you ate"');
  checkMealText(el.place.value, '"Where"');
  const rating = form.querySelector('input[name="rating"]:checked')?.value ?? 'none';
  if (rating !== 'none' && !/^[1-5]$/.test(rating)) fail('Stars must be 1 to 5');
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
  if (!countryDetail) fail('Country not found');
  const body = readMealForm(form);
  const { code } = countryDetail;
  const token = routeToken;
  // Closing bumps dialogToken, so a mismatch means the dialog was closed (and
  // maybe reopened) while saving; then it mustn't be closed from here.
  const dt = dialogToken;
  if (mealEdit) {
    const id = String(mealEdit.id);
    if (!ID_RE.test(id)) fail('Meal not found');
    await passportRequest('PATCH', `/meals/${id}`, body);
  } else {
    if (!CODE_RE.test(code)) fail('Country not found');
    await passportRequest('POST', `/countries/${code}/meals`, body);
  }
  if (dt === dialogToken) mealDialog.close();
  toast('Meal saved');
  if (token === routeToken) refreshCountry(token);
});

const deleteMealDialog = setupDialog('delete-meal-dialog');
deleteMealDialog.addEventListener('close', () => { mealDelete = null; });

function openDeleteMeal(meal) {
  mealDelete = meal;
  const date = formatDay(meal.eaten_on);
  const last = countryDetail.meals.filter((m) => m.mine === true).length === 1;
  $('delete-meal-text').textContent = date ? `Delete this meal from ${date}?` : 'Delete this meal?';
  $('delete-meal-last').textContent = last ? `${countryDetail.name} will no longer count as done for you.` : '';
  $('delete-meal-last').hidden = !last;
  deleteMealDialog.showModal();
}

bindForm($('delete-meal-form'), async () => {
  if (!mealDelete) fail('Meal not found');
  const id = String(mealDelete.id);
  if (!ID_RE.test(id)) fail('Meal not found');
  const token = routeToken;
  const dt = dialogToken;
  // The response's still_done isn't needed: the country page is re-fetched below.
  await passportRequest('POST', `/meals/${id}/delete`);
  if (dt === dialogToken) deleteMealDialog.close();
  toast('Meal deleted');
  if (token === routeToken) refreshCountry(token);
});

function personRowHtml(m, removable) {
  const badges = [];
  if (m.is_owner) badges.push(['Owner', 'badge-accent']);
  if (m.id === state.user.id) badges.push(['You', '']);
  const remove = `Remove ${m.display_name} @${m.username}`;
  return '<li class="person-row"><div class="person-text">'
    + `<span class="person-name">${esc(m.display_name)}${m.inactive ? ' (inactive)' : ''}</span>`
    + `<span class="person-username">@${esc(m.username)}</span>`
    + (badges.length ? `<span class="badges">${badgesHtml(badges)}</span>` : '')
    + '</div>'
    + (removable
      ? `<button type="button" class="btn" data-remove-user-id="${esc(String(m.id))}" aria-label="${esc(remove)}">Remove</button>`
      : '')
    + '</li>';
}

function showPassport(detail) {
  passport = detail;
  const p = detail.passport;
  const owner = p.role === 'owner';
  $('passport-name').textContent = p.name;
  $('passport-owner').textContent = ownerLine(p, false);
  const id = String(p.id);
  for (const linkId of ['people-back', 'country-back']) {
    $(linkId).textContent = p.name;
    $(linkId).setAttribute('href', ID_RE.test(id) ? `#/passports/${id}` : '#/passports');
  }
  $('passport-default').hidden = !p.is_default;
  $('make-default').hidden = p.is_default;
  $('participants-list').innerHTML = detail.participants.map((m) => personRowHtml(m, owner && !m.is_owner)).join('');
  $('viewers-list').innerHTML = detail.viewers.map((m) => personRowHtml(m, owner)).join('');
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

// These errors mean the page is out of date (ownership, membership, role or
// meals changed elsewhere); the current page is re-fetched.
const STALE_ERRORS = [
  [403, 'Only the owner can do that'],
  [404, 'Not in this passport'],
  [409, "The owner can't leave. Delete the passport instead."],
  [404, 'Meal not found'],
  [403, 'Only participants can log meals'],
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
      if (err.status === 404 && err.message === 'Passport not found') {
        closeDialogs();
        replaceRoute('#/passports');
      } else if (STALE_ERRORS.some(([status, message]) => err.status === status && err.message === message)) {
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
    if (!ID_RE.test(String(id))) fail('Passport not found');
    await api('PATCH', '/api/account', { default_passport_id: id });
    if (token === routeToken) {
      passport.passport.is_default = true;
      showPassport(passport);
    }
    toast('Default passport set');
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
      status.textContent = 'Everyone is already in this passport.';
      status.hidden = false;
      return;
    }
    $('add-person-user').replaceChildren(...candidates.map((u) => new Option(personLabel(u), String(u.id))));
    $('add-person-fields').hidden = false;
    $('add-person-roles').hidden = false;
    $('add-person-submit').disabled = false;
  });
});

bindForm($('add-person-form'), async (form) => {
  const user = pickUsers.find((u) => String(u.id) === $('add-person-user').value);
  if (!user) fail('Choose people from the list');
  const role = form.querySelector('input[name="role"]:checked')?.value;
  if (role !== 'participant' && role !== 'viewer') fail('Role must be participant or viewer');
  const token = routeToken;
  const detail = await passportRequest('POST', '/members', { ...personRef(user), role });
  $('add-person-dialog').close();
  showPassportIfCurrent(token, detail);
  toast(`${user.display_name} added`);
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
    ? `Remove ${personLabel(member)}? Their meals in this passport will be deleted.`
    : `Remove ${personLabel(member)}? They'll no longer see this passport.`;
  openPassportConfirm('Remove person', text, 'Remove', async () => {
    const token = routeToken;
    const detail = await passportRequest('POST', `/members/${uid}/remove`);
    passportConfirmDialog.close();
    showPassportIfCurrent(token, detail);
    toast('Removed');
  });
});

$('leave-open').addEventListener('click', () => {
  if (!passport) return;
  const { name, role } = passport.passport;
  const text = role === 'viewer'
    ? `Leave ${name}? You'll no longer see it.`
    : `Leave ${name}? Your meals in it will be deleted.`;
  openPassportConfirm('Leave passport', text, 'Leave', async () => {
    const token = routeToken;
    await passportRequest('POST', '/leave');
    passportConfirmDialog.close();
    if (token === routeToken) replaceRoute('#/');
    toast(`You left ${name}`);
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
  toast('Passport renamed');
});

const typedNameMatches = () => passport !== null
  && $('delete-passport-confirm').value.normalize('NFC').trim() === passport.passport.name;

bindDialog('delete-passport-open', 'delete-passport-dialog', () => {
  $('delete-passport-text').textContent = `Permanently delete ${passport.passport.name}? `
    + "Everyone's meals in it will be deleted. This can't be undone.";
  $('delete-passport-submit').disabled = true;
});

$('delete-passport-confirm').addEventListener('input', () => {
  $('delete-passport-submit').disabled = !typedNameMatches();
});

bindForm($('delete-passport-form'), async () => {
  if (!typedNameMatches()) fail('Type the passport name to confirm');
  const token = routeToken;
  await passportRequest('POST', '/delete', { confirm_name: $('delete-passport-confirm').value });
  $('delete-passport-dialog').close();
  if (token === routeToken) replaceRoute('#/');
  toast('Passport deleted');
});

// ---- Forced password change (temporary password set by an admin) ----

bindForm($('force-password-form'), async (form) => {
  const current = form.elements.current_password.value;
  const next = form.elements.new_password.value;
  if (!current) fail('Enter your temporary password');
  checkNewPassword(next);
  if (next !== form.elements.confirm_password.value) fail("Passwords don't match");
  await api('POST', '/api/account/password', { current_password: current, new_password: next });
  form.reset();
  // The change succeeded even if this re-fetch fails, so clear the flag locally then.
  if (!(await refreshSession()) && state.user) state.user.must_change_password = false;
  history.replaceState(null, '', '#/');
  render();
  toast('Password changed');
});

// ---- Admin: user list ----

function userBadges(u) {
  const badges = [];
  if (u.id === state.user.id) badges.push(['You', '']);
  if (u.is_admin) badges.push(['Admin', 'badge-accent']);
  if (u.disabled) badges.push(['Disabled', 'badge-warn']);
  if (u.locked) badges.push(['Locked', 'badge-warn']);
  // An expired temporary password implies "must change", so show only the stronger badge.
  if (u.temp_password_expired) badges.push(['Temporary password expired', 'badge-warn']);
  else if (u.must_change_password) badges.push(['Must change password', '']);
  return badges;
}

function userCardHtml(u) {
  const badges = badgesHtml(userBadges(u));
  return `<li><button type="button" class="list-card" data-user-id="${esc(String(u.id))}">`
    + `<span class="list-card-title">${esc(u.display_name)}</span>`
    + `<span class="list-card-sub">@${esc(u.username)}</span>`
    + (badges ? `<span class="badges">${badges}</span>` : '')
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
  toast(`User ${data.user.display_name} created`);
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

const yesNo = (v) => (v ? 'Yes' : 'No');

function formatDate(seconds) {
  return typeof seconds === 'number' ? new Date(seconds * 1000).toLocaleDateString() : 'Never';
}

// API paths are built only from an id that matches the id pattern.
function adminUserPath(suffix = '') {
  const id = String(adminUser.id);
  if (!ID_RE.test(id)) fail('Invalid user id');
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
    toast('User not found', 'error');
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
  $('au-username').textContent = u.username;
  $('au-role').textContent = u.is_admin ? 'Admin' : 'User';
  $('au-status').textContent = u.disabled ? 'Disabled' : 'Active';
  $('au-locked').textContent = yesNo(u.locked);
  $('au-must-change').textContent = u.temp_password_expired
    ? 'Yes, temporary password expired' : yesNo(u.must_change_password);
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
  toast('Display name saved');
});

bindDialog('au-reset', 'reset-dialog', () => {
  $('reset-text').textContent = `Set a new temporary password for ${adminUser.display_name}. `
    + "They'll be logged out everywhere and asked to choose a new password when they log in.";
});

bindForm($('reset-form'), async (form) => {
  const password = form.elements.new_password.value;
  checkNewPassword(password);
  const token = routeToken;
  const { user } = await api('POST', adminUserPath('/password'), { new_password: password });
  form.reset();
  $('reset-dialog').close();
  showAdminUserIfCurrent(token, user);
  toast('Password reset');
});

$('au-unlock').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  const token = routeToken;
  try {
    const { user } = await api('POST', adminUserPath('/unlock'));
    showAdminUserIfCurrent(token, user);
    toast('Unlocked');
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

// Disable, Enable, Make admin and Remove admin share one confirmation dialog.
const CONFIRM_ACTIONS = {
  'au-disable': {
    title: 'Disable account', button: 'Disable', path: '/disable', done: 'Disabled',
    text: (n) => `Disable ${n}? They'll be logged out everywhere and won't be able to log in. Nothing is deleted. `
      + 'Passports they own will be handed over to another active participant, if there is one.',
  },
  'au-enable': {
    title: 'Re-enable account', button: 'Enable', path: '/enable', done: 'Enabled',
    text: (n) => `Re-enable ${n}? They'll be able to log in again with their existing password.`,
  },
  'au-promote': {
    title: 'Make admin', button: 'Make admin', path: '/promote', done: 'Now an admin',
    text: (n) => `Make ${n} an admin? Admins can create, disable and delete accounts and reset passwords.`,
  },
  'au-demote': {
    title: 'Remove admin', button: 'Remove admin', path: '/demote', done: 'No longer an admin',
    text: (n, self) => (self
      ? "Remove your own admin rights? You'll lose access to this screen."
      : `Remove admin rights from ${n}?`),
  },
};

let confirmAction = null;
const confirmDialog = setupDialog('confirm-dialog');
confirmDialog.addEventListener('close', () => { confirmAction = null; });

for (const [buttonId, action] of Object.entries(CONFIRM_ACTIONS)) {
  $(buttonId).addEventListener('click', () => {
    confirmAction = action;
    $('confirm-title').textContent = action.title;
    $('confirm-text').textContent = action.text(adminUser.display_name, adminUser.id === state.user.id);
    $('confirm-submit').textContent = action.button;
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
    toast("You're no longer an admin");
    return;
  }
  showAdminUserIfCurrent(token, user);
  toast(action.done);
});

const deleteMatches = () => $('admin-delete-confirm').value.toLowerCase() === adminUser.username.toLowerCase();

bindDialog('au-delete', 'admin-delete-dialog', () => {
  $('admin-delete-text').textContent = `Permanently delete ${adminUser.display_name}'s account? `
    + "Their passports will be handed over to another participant or deleted, and they'll be removed "
    + "from passports shared with them, along with their meals. This can't be undone.";
  $('admin-delete-label').textContent = `Type ${adminUser.username} to confirm`;
  $('admin-delete-submit').disabled = true;
});

$('admin-delete-confirm').addEventListener('input', () => {
  $('admin-delete-submit').disabled = !deleteMatches();
});

bindForm($('admin-delete-form'), async (form) => {
  if (!deleteMatches()) fail('Type the username to confirm');
  const token = routeToken;
  await api('POST', adminUserPath('/delete'), { confirm_username: form.elements.confirm_username.value });
  $('admin-delete-dialog').close();
  if (token === routeToken) navigate('#/admin');
  toast('Account deleted');
});

// ---- Start ----

window.addEventListener('hashchange', () => {
  if (sessionLoaded && !sessionPending) render();
});

$('load-retry').addEventListener('click', loadSession);

loadSession();
