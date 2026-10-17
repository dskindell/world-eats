# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue. Use GitHub's private vulnerability reporting for this repository: open the **Security** tab and choose **Report a vulnerability** (a private security advisory).

Include what you found, how to reproduce it, and the version or commit you tested. This is a small hobby project maintained on a best-effort basis, so there are no response-time guarantees.

## Supported versions

Only the latest commit on the default branch is supported. There are no versioned releases.

## In scope

- The server in `app/` (authentication, sessions, authorisation between users and passports, input handling, the HTTP layer).
- The frontend in `app/static/` (for example XSS or CSRF).
- The shipped `docker-compose.yml` and `app/Dockerfile`.

## Out of scope

- Problems that need you to run the app contrary to the README, for example exposing it before completing first-visit setup, or serving it over plain HTTP on an untrusted network.
- The documented trade-offs in the README's [security notes](README.md#security-notes) and [login lockout](README.md#login-lockout) sections (cookies are shared across ports on one host, usernames are visible to logged-in users, admins can reset passwords).
- Vulnerabilities in your reverse proxy, Docker, or the host.
- Denial of service by someone who already has an account and unlimited requests.

## Security design in brief

Taken from the code in `app/`:

- **No third-party code.** Python standard library only, and no outside requests at runtime, so there are no dependencies to patch.
- **Host and origin checks.** `WORLD_EATS_ALLOWED_HOSTS` is required; requests with any other `Host` get `421`. Every `POST`/`PATCH` must carry an `Origin` matching the request host and `Content-Type: application/json`, and bodies are capped at 16 KiB.
- **Response headers.** A strict Content-Security-Policy (`default-src 'self'`, no inline scripts or styles, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cross-Origin-Opener-Policy: same-origin`.
- **Passwords.** Hashed with scrypt (`hashlib.scrypt`, per-password random salt, N=32768, r=8, p=3) and compared in constant time. Passwords are 12-128 characters.
- **Sessions.** Random 256-bit tokens; only a SHA-256 hash is stored in the database. The cookie is `HttpOnly`, `SameSite=Lax`, and `__Host-` prefixed with `Secure` when `WORLD_EATS_SECURE_COOKIES=true`. Sessions expire after 30 days, and are ended by password change (other devices), admin reset or disable, and account deletion.
- **Lockout.** Five failed logins within 15 minutes lock the username, and the same limit applies per client IP. `X-Forwarded-For` is trusted only from addresses in `WORLD_EATS_TRUSTED_PROXIES`.
- **Data.** SQL uses bound parameters. SQLite runs with `foreign_keys=ON` and `secure_delete=ON`, so deleted rows are overwritten in the database file (backups and the `-wal` file can still hold older copies).
- **Container.** Runs as a non-root user, with a read-only root filesystem, all capabilities dropped and `no-new-privileges` in the shipped compose file.
