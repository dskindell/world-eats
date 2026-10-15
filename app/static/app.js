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

const VIEWS = ['loading', 'setup', 'login', 'force-password', 'home', 'account', 'admin', 'admin-user'];
const TOPBAR_VIEWS = ['home', 'account', 'admin', 'admin-user'];
// User ids: one pattern for both the route table and every id check.
const ID_PATTERN = '[1-9][0-9]{0,9}';
const ID_RE = new RegExp(`^${ID_PATTERN}$`);

// Ordered hash routes; the first match wins. Anything unmatched goes to #/.
const ROUTES = [
  { re: /^#\/$/, enter: enterHome },
  { re: /^#\/account$/, enter: enterAccount },
  { re: /^#\/admin$/, admin: true, enter: enterAdmin },
  { re: new RegExp(`^#/admin/users/(${ID_PATTERN})$`), admin: true, enter: enterAdminUser },
];

// Bumped on every render so a slow route load can tell it's been superseded.
let routeToken = 0;
// Hash changes are ignored until a session has loaded and while one is loading.
let sessionLoaded = false;
let sessionPending = false;

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
  if (previous && previous !== 'loading') view.querySelector('h1')?.focus();
}

function render() {
  routeToken += 1;
  closeMenu();
  closeDialogs();
  if (state.setupRequired) return showView('setup');
  if (!state.user) return showView('login');
  if (state.user.must_change_password) return showView('force-password');
  updateUserText();
  const hash = location.hash;
  const route = ROUTES.find((r) => r.re.test(hash));
  if (!route || (route.admin && !state.user.is_admin)) return enterHome();
  return route.enter(hash.match(route.re)[1], routeToken);
}

function enterHome() {
  if (location.hash !== '#/') history.replaceState(null, '', '#/');
  showView('home');
}

function enterAccount() {
  fillAccount();
  showView('account');
}

// Goes to a hash; renders directly if we're already there (no hashchange).
function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function closeDialogs() {
  document.querySelectorAll('dialog[open]').forEach((d) => d.close());
}

function updateUserText() {
  $('menu-name').textContent = state.user.display_name;
  $('home-name').textContent = state.user.display_name;
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
  showView('loading');
  $('loading-text').hidden = false;
  $('load-error').hidden = true;
  $('load-retry').hidden = true;
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

// Wires Cancel, and resets the form on every close (Cancel, Escape, success,
// navigation) so no password lingers.
function setupDialog(dialogId) {
  const dialog = $(dialogId);
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
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
  const badges = userBadges(u)
    .map(([text, cls]) => `<span class="badge ${cls}">${esc(text)}</span>`)
    .join('');
  return `<li><button type="button" class="user-card" data-user-id="${esc(String(u.id))}">`
    + `<span class="user-card-name">${esc(u.display_name)}</span>`
    + `<span class="user-card-username">@${esc(u.username)}</span>`
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
    enterHome();
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
    text: (n) => `Disable ${n}? They'll be logged out everywhere and won't be able to log in. Nothing is deleted.`,
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
  $('admin-delete-text').textContent = `Permanently delete ${adminUser.display_name}'s account? This can't be undone.`;
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
