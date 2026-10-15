"""Field validation, password hashing, login lockout and client IPs (contract 1a, sections 2, 4 and 6;
admin unlock from contract 1b)."""

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import unicodedata

from config import normalise_ip

USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,32}")
USERNAME_MSG = "Username must be 3-32 characters: letters, digits, . _ -"
DISPLAY_NAME_LENGTH_MSG = "Display name must be 1-50 characters"
DISPLAY_NAME_CHARS_MSG = "Display name contains characters that aren't allowed"
PASSWORD_MSG = "Password must be 12-128 characters"

BLOCKED_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Cn", "Zl", "Zp", "Cf"})
ZERO_WIDTH_JOINER = "\u200d"


class Invalid(ValueError):
    """A client-supplied field failed validation; the message is safe to return."""


class Busy(Exception):
    """The hashing semaphore couldn't be acquired in time."""


# --- Validation (section 4) ---
# Validators for client fields raise Invalid(message) (a 400 with that message);
# handlers call them in the contract's field order. The boolean helpers below are
# for inputs that are never a 400 (login and confirmation passwords).

def valid_username(value):
    return isinstance(value, str) and USERNAME_RE.fullmatch(value) is not None


def require_username(value):
    if not valid_username(value):
        raise Invalid(USERNAME_MSG)
    return value


def clean_display_name(value):
    if not isinstance(value, str):
        raise Invalid(DISPLAY_NAME_LENGTH_MSG)
    name = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(name) <= 50:
        raise Invalid(DISPLAY_NAME_LENGTH_MSG)
    for ch in name:
        if ch != ZERO_WIDTH_JOINER and unicodedata.category(ch) in BLOCKED_CATEGORIES:
            raise Invalid(DISPLAY_NAME_CHARS_MSG)
    return name


def has_surrogate(text):
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def valid_new_password(value):
    return isinstance(value, str) and 12 <= len(value) <= 128 and not has_surrogate(value)


def require_new_password(value):
    if not valid_new_password(value):
        raise Invalid(PASSWORD_MSG)
    return value


def usable_password(value):
    """Login/confirmation passwords: anything else is treated as a wrong password."""
    return isinstance(value, str) and 1 <= len(value) <= 128 and not has_surrogate(value)


# --- Password hashing ---

class PasswordHasher:
    """scrypt with one shared semaphore so at most `slots` hashes run at once.

    Tests may construct one with a smaller n; the stored string carries its own
    parameters, so verification always uses what the hash was made with.
    """

    def __init__(self, n=32768, r=8, p=3, maxmem=67108864, slots=2, timeout=10):
        self.n, self.r, self.p, self.maxmem = n, r, p, maxmem
        self.semaphore = threading.BoundedSemaphore(slots)
        self.timeout = timeout
        self.dummy_hash = None

    def _scrypt(self, password, salt, n, r, p):
        if not self.semaphore.acquire(timeout=self.timeout):
            raise Busy()
        try:
            return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                  maxmem=self.maxmem, dklen=32)
        finally:
            self.semaphore.release()

    def hash(self, password):
        salt = secrets.token_bytes(16)
        key = self._scrypt(password, salt, self.n, self.r, self.p)
        return "scrypt${}${}${}${}${}".format(
            self.n, self.r, self.p,
            base64.b64encode(salt).decode("ascii"), base64.b64encode(key).decode("ascii"))

    def verify(self, password, stored):
        parsed = parse_hash(stored)
        if parsed is None:
            return False
        n, r, p, salt, expected = parsed
        return hmac.compare_digest(self._scrypt(password, salt, n, r, p), expected)

    def init_dummy(self):
        self.dummy_hash = self.hash(secrets.token_urlsafe(24))

    def burn(self, password):
        """Verify against the dummy hash so a missing user or bad input costs the same time."""
        if self.dummy_hash is None:
            raise RuntimeError("init_dummy() must run before burn()")
        self.verify(password if usable_password(password) else "-", self.dummy_hash)
        return False


def parse_hash(stored):
    parts = stored.split("$") if isinstance(stored, str) else []
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        key = base64.b64decode(parts[5], validate=True)
    except ValueError:
        return None
    if min(n, r, p) < 1 or not salt or len(key) != 32:
        return None
    return n, r, p, salt, key


# --- Client IP (section 2) and lockout keys (section 6) ---

def client_ip(peer, forwarded_for, trusted):
    """The client address: the peer, or the rightmost untrusted X-Forwarded-For entry
    when the peer is a trusted proxy. `forwarded_for` is the list of XFF header values."""
    peer_ip = normalise_ip(ipaddress.ip_address(peer))
    if peer_ip not in trusted:
        return peer_ip
    for entry in reversed(",".join(forwarded_for).split(",")):
        try:
            ip = normalise_ip(ipaddress.ip_address(entry.strip()))
        except ValueError:
            return peer_ip
        if ip not in trusted:
            return ip
    return peer_ip


def ip_key(ip):
    ip = normalise_ip(ip)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# --- Lockout ---

FAILED, SUCCEEDED, NO_RESULT = "failed", "succeeded", "no-result"


class _Entry:
    __slots__ = ("failures", "in_flight", "last")

    def __init__(self, now):
        self.failures = []
        self.in_flight = 0
        self.last = now


class Lockout:
    """Per-username and per-IP failure counts, guarded by one lock.

    reserve() is the atomic check-and-reserve; every successful reserve() must be
    paired with finish() in a `finally`. Either key may be None (not tracked).
    """

    def __init__(self, clock, limit=5, window=900, max_keys=10000):
        self.clock = clock
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.lock = threading.Lock()
        self.users = {}
        self.ips = {}

    def _pairs(self, user_key, ip):
        return [(m, k) for m, k in ((self.users, user_key), (self.ips, ip)) if k is not None]

    def _prune(self, table, now):
        for key in list(table):
            entry = table[key]
            entry.failures = [t for t in entry.failures if now - t < self.window]
            if not entry.failures and entry.in_flight == 0:
                del table[key]

    def _add(self, table, key, now):
        if len(table) >= self.max_keys:
            idle = [k for k, e in table.items() if e.in_flight == 0]
            if idle:
                del table[min(idle, key=lambda k: table[k].last)]
        entry = table[key] = _Entry(now)
        return entry

    def reserve(self, user_key, ip):
        now = self.clock()
        with self.lock:
            pairs = self._pairs(user_key, ip)
            for table, _ in pairs:
                self._prune(table, now)
            for table, key in pairs:
                entry = table.get(key)
                if entry and len(entry.failures) + entry.in_flight >= self.limit:
                    return False
            for table, key in pairs:
                entry = table.get(key) or self._add(table, key, now)
                entry.in_flight += 1
                entry.last = now
            return True

    def finish(self, user_key, ip, outcome):
        now = self.clock()
        with self.lock:
            for table, key in self._pairs(user_key, ip):
                entry = table.get(key)
                if entry is None:
                    continue
                entry.in_flight = max(0, entry.in_flight - 1)
                entry.last = now
                if outcome == FAILED:
                    entry.failures.append(now)
                    del entry.failures[:-self.limit]
                elif outcome == SUCCEEDED and table is self.users:
                    entry.failures.clear()

    def is_locked(self, user_key):
        """True if the username has `limit` failures in the window (in-flight attempts don't count)."""
        now = self.clock()
        with self.lock:
            entry = self.users.get(user_key)
            if entry is None:
                return False
            return sum(1 for t in entry.failures if now - t < self.window) >= self.limit

    def clear_user(self, user_key):
        """Admin unlock, reset or delete: drops the username's failures; in-flight attempts are kept."""
        with self.lock:
            entry = self.users.get(user_key)
            if entry is None:
                return
            entry.failures.clear()
            if entry.in_flight == 0:
                del self.users[user_key]
