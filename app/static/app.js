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
  if (res.status === 401 && path !== '/api/login' && path !== '/api/session') onSessionLost();
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
      setError(form, err.message);
    } finally {
      button.disabled = false;
    }
  });
}

const fail = (message) => { throw new Error(message); };

// ---- Client-side checks mirroring contract §4 (the server is authoritative) ----

const USERNAME_RE = /^[A-Za-z0-9._-]{3,32}$/;
// Cc, Cs, Co, Cn, Zl, Zp, and Cf except U+200D (zero-width joiner).
const BAD_NAME_CHAR_RE = /[\p{Cc}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]|(?!\u200D)\p{Cf}/u;
const codePoints = (s) => [...s].length;

function checkUsername(value) {
  if (!USERNAME_RE.test(value)) fail('Username must be 3-32 characters: letters, digits, . _ -');
}

function checkDisplayName(value) {
  const name = value.normalize('NFC').trim();
  const n = codePoints(name);
  if (n < 1 || n > 50) fail('Display name must be 1-50 characters');
  if (BAD_NAME_CHAR_RE.test(name)) fail("Display name contains characters that aren't allowed");
}

function checkNewPassword(value) {
  const n = codePoints(value);
  if (n < 12 || n > 128 || /\p{Cs}/u.test(value)) fail('Password must be 12-128 characters');
}

// ---- Views and routing ----

const VIEWS = ['loading', 'setup', 'login', 'home', 'account'];

function showView(name) {
  const previous = currentView;
  currentView = name;
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== name;
  $('topbar').hidden = !(state.user && (name === 'home' || name === 'account'));
  if (previous === name) return;
  const view = $(`view-${name}`);
  view.querySelectorAll('.form-error').forEach((el) => { el.textContent = ''; el.hidden = true; });
  // Move focus to the new heading so keyboard and screen-reader users land
  // in the new view; skipped on first load to avoid stealing focus.
  if (previous && previous !== 'loading') view.querySelector('h1')?.focus();
}

function render() {
  closeMenu();
  if (state.setupRequired) return showView('setup');
  if (!state.user) return showView('login');
  updateUserText();
  if (location.hash === '#/account') {
    fillAccount();
    return showView('account');
  }
  if (location.hash !== '#/') history.replaceState(null, '', '#/');
  return showView('home');
}

function updateUserText() {
  $('menu-name').textContent = state.user.display_name;
  $('home-name').textContent = state.user.display_name;
  $('account-username').textContent = state.user.username;
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
  document.querySelectorAll('dialog[open]').forEach((d) => d.close());
  document.querySelectorAll('form').forEach((f) => f.reset());
}

function onSessionLost() {
  clearSessionUi();
  render();
}

async function loadSession() {
  showView('loading');
  $('loading-text').hidden = false;
  $('load-error').hidden = true;
  $('load-retry').hidden = true;
  try {
    const data = await api('GET', '/api/session');
    state.setupRequired = data.setup_required === true;
    state.user = data.user || null;
    render();
  } catch (err) {
    $('loading-text').hidden = true;
    $('load-error').textContent = err.message;
    $('load-error').hidden = false;
    $('load-retry').hidden = false;
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

const menuItems = () => [...$('user-menu').querySelectorAll('[role="menuitem"]')];

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

// The "Account settings" link navigates by hash; just close the menu.
$('user-menu').querySelector('a').addEventListener('click', () => closeMenu());

$('menu-logout').addEventListener('click', async (event) => {
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
});

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

function bindDialog(openButtonId, dialogId) {
  const dialog = $(dialogId);
  $(openButtonId).addEventListener('click', () => dialog.showModal());
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
  // Reset on every close (Cancel, Escape, success) so no password lingers.
  dialog.addEventListener('close', () => {
    const form = dialog.querySelector('form');
    form.reset();
    setError(form, '');
  });
}

bindDialog('logout-all-open', 'logout-all-dialog');
bindDialog('delete-open', 'delete-dialog');

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

// ---- Start ----

window.addEventListener('hashchange', () => {
  if (currentView !== 'loading') render();
});

$('load-retry').addEventListener('click', loadSession);

loadSession();
