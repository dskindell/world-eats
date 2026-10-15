# World Eats

Pick a random country to eat from, and keep track of the countries we've done.

Runs on host port 7197.

**Status:** skeleton only. No features have been built yet; they will be added one at a time as they're agreed.

## Layout

- `app/server.py` - Python stdlib HTTP server, no dependencies. Serves `app/static/` and `GET /health`.
- `app/static/` - frontend (vanilla HTML/JS/CSS, no build step)

## Run

```bash
docker compose up -d --build
```
