"""Configuration from environment variables (contract 1a, section 2).

load_config() takes any mapping, so tests can pass a plain dict. Invalid
configuration raises ConfigError with a one-line message naming the variable.
"""

import ipaddress
import re
from dataclasses import dataclass

HOSTNAME_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
PORT_RE = re.compile(r"[0-9]{1,5}")
BRACKETED_RE = re.compile(r"\[([^\[\]]*)\](?::(.*))?", re.DOTALL)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    allowed_hosts: tuple  # (lower-cased host, port or None) pairs
    secure_cookies: bool
    trusted_proxies: frozenset  # ipaddress objects
    data_dir: str
    port: int

    @property
    def cookie_name(self):
        return "__Host-we_session" if self.secure_cookies else "we_session"


def parse_port(text):
    if not PORT_RE.fullmatch(text):
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def split_host_port(value):
    """Splits `host`, `host:port`, `[v6]` or `[v6]:port` on the last colon outside brackets.

    Returns (host, port text or None), or None for a malformed bracketed form. Used
    for both WORLD_EATS_ALLOWED_HOSTS entries and request Host headers; each caller
    validates the parts itself.
    """
    if value.startswith("["):
        m = BRACKETED_RE.fullmatch(value)
        if not m or not m.group(1):
            return None
        return "[" + m.group(1) + "]", m.group(2)
    host, sep, port = value.rpartition(":")
    return (host, port) if sep else (value, None)


def normalise_ip(ip):
    """IPv4-mapped IPv6 addresses compare as the IPv4 address they carry."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_host_entry(entry):
    bad = ConfigError(f"WORLD_EATS_ALLOWED_HOSTS: invalid entry {entry!r}")
    if any(c in entry for c in "*/@") or "://" in entry or any(c.isspace() for c in entry):
        raise bad
    parts = split_host_port(entry)
    if parts is None:
        raise bad
    host, port_text = parts
    port = None
    if port_text is not None:
        port = parse_port(port_text)
        if port is None:
            raise bad
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise bad from None
    elif not HOSTNAME_RE.fullmatch(host):
        raise bad
    return host.lower(), port


def load_config(env):
    entries = [e.strip() for e in env.get("WORLD_EATS_ALLOWED_HOSTS", "").split(",")]
    entries = [e for e in entries if e]
    if not entries:
        raise ConfigError("WORLD_EATS_ALLOWED_HOSTS must be set, e.g. WORLD_EATS_ALLOWED_HOSTS=localhost")
    allowed_hosts = tuple(parse_host_entry(e) for e in entries)

    secure = env.get("WORLD_EATS_SECURE_COOKIES", "false")
    if secure.lower() not in ("true", "false"):
        raise ConfigError(f"WORLD_EATS_SECURE_COOKIES must be true or false, not {secure!r}")

    proxies = set()
    for entry in env.get("WORLD_EATS_TRUSTED_PROXIES", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            proxies.add(normalise_ip(ipaddress.ip_address(entry)))
        except ValueError:
            raise ConfigError(f"WORLD_EATS_TRUSTED_PROXIES: invalid address {entry!r}") from None

    port_text = env.get("PORT", "8080")
    port = parse_port(port_text)
    if port is None:
        raise ConfigError(f"PORT: invalid value {port_text!r}")

    return Config(
        allowed_hosts=allowed_hosts,
        secure_cookies=secure.lower() == "true",
        trusted_proxies=frozenset(proxies),
        data_dir=env.get("DATA_DIR", "/data"),
        port=port,
    )
