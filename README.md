# World Eats

Pick a random country to eat from, and keep track of the countries we've done.

Runs on host port 7197. Set `WORLD_EATS_ALLOWED_HOSTS` to the hostname you use to reach it.

**Status:** being built one feature at a time. So far: accounts and login (first-visit admin setup, login/logout, account settings).

## Layout

- `app/server.py` - Python stdlib HTTP server + SQLite, no dependencies.
- `app/static/` - frontend (vanilla HTML/JS/CSS, no build step).
- `tests/` - `unittest` suite (stdlib only).
- Data: one SQLite database in `/data` inside the container, `./data` next to the compose file on the host (gitignored).

## Run

```bash
mkdir -m 700 data      # owned by you; compose runs the app as UID/GID 1000 to match
docker compose up -d --build
```

If your user isn't UID 1000 (`id -u`), change `user:` in `docker-compose.yml` to match. Without a `user:` line the image runs as UID 10001, and `./data` must then be owned by 10001.

Then open the app and create the first account on the setup page. **Complete setup before exposing the app to anyone else:** until an account exists, whoever opens the app first becomes admin.

## Configuration

Environment variables (set in `docker-compose.yml`):

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WORLD_EATS_ALLOWED_HOSTS` | yes | - | Comma-separated hostnames the app answers to, e.g. `localhost` or `eats.example.com,192.0.2.10:7197`. An entry without a port matches any port. No wildcards. Requests for any other host get `421`. The app won't start without it. |
| `WORLD_EATS_SECURE_COOKIES` | no | `false` | Set `true` when the app is served over HTTPS. Don't set it on plain HTTP: browsers will refuse the cookie and login won't work. |
| `WORLD_EATS_TRUSTED_PROXIES` | no | empty | Comma-separated IP addresses of your reverse proxy. Only then does the app read `X-Forwarded-For` to get the visitor's real IP for the login lockout. |
| `DATA_DIR` | no | `/data` | Where the database lives. |
| `PORT` | no | `8080` | Port inside the container. |

## Behind a reverse proxy

The app serves plain HTTP; put your own reverse proxy in front for HTTPS.

- The proxy must pass the original `Host` header through (nginx: `proxy_set_header Host $host;`), and that host must be in `WORLD_EATS_ALLOWED_HOSTS`. Otherwise every request gets `421`, or every change gets `403`.
- Set `WORLD_EATS_TRUSTED_PROXIES` to the proxy's IP. Without it, every visitor appears to come from the proxy, so five wrong passwords from anyone lock out logins for everyone for 15 minutes.
- Set `WORLD_EATS_SECURE_COOKIES=true`.

## Security notes

- **Give World Eats its own hostname.** Browsers don't keep cookies separate by port, so if you reach the app by IP address and port (e.g. `http://192.0.2.10:7197`), every other web service on that IP receives your World Eats login cookie. A compromised service there could use it to act as you, including as an admin. Use a dedicated hostname (and HTTPS) before you rely on the app, and before you create a second admin.
- **Deleting an account removes its data from the database file** (the app overwrites deleted content), but **backups made before the deletion still contain it**.
- **Every logged-in user can see every username** (to add people to passports), so anyone with an account can lock another account out for 15 minutes by typing wrong passwords. An admin can use Unlock.
- **Admins can reset any password, which gives them access to that account.** The user will notice, because they're logged out everywhere and their password stops working. (Planned: when self-service password recovery is added, admin resets will be removed.)

## If no admin can log in

Setup never reopens once an account exists, so if every admin is locked out (forgotten password, or a new admin's temporary password expired), reset one from the server. Replace `NAME` with the username. The command makes that account an enabled admin, logs it out everywhere, and sets a temporary password you type (12-128 characters, not shown) that must be changed at the next login:

```bash
docker exec -it world-eats python -c "
import getpass, sqlite3, time, auth
h = auth.PasswordHasher().hash(auth.require_new_password(getpass.getpass('Temporary password: ')))
c = sqlite3.connect('/data/world-eats.db')
n = c.execute('UPDATE users SET password_hash = ?, must_change_password = 1, temp_password_set_at = ?, disabled = 0, is_admin = 1 WHERE username = ? COLLATE NOCASE', (h, int(time.time()), 'NAME')).rowcount
c.execute('DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ? COLLATE NOCASE)', ('NAME',))
c.commit(); print('updated' if n else 'no such user')
"
```

If the admin is only locked out by too many wrong passwords, wait 15 minutes or restart the container instead.

## Login lockout

Five wrong passwords for a username within 15 minutes lock that username for 15 minutes; the same limit applies per client IP. This is a deliberate trade-off: anyone who can reach the login page can keep a known username locked by typing wrong passwords. Wrong passwords typed in account settings (changing your password, deleting your account) count toward the same per-username limit. The lockout resets when the container restarts.

## Backup

Everything is in the one SQLite file. Don't copy it with `cp` while the app is running (the copy can be inconsistent). Use SQLite's online backup instead:

```bash
docker exec world-eats python -c "import sqlite3; sqlite3.connect('/data/world-eats.db').backup(sqlite3.connect('/data/backup.db'))"
```

This writes `backup.db` next to the database (`./data/backup.db` on the host); copy that file to your backup location.

## Tests

```bash
docker run --rm -v "$PWD":/src:ro -w /src python:3.12-slim python -m unittest discover -s tests -t . -v
```
