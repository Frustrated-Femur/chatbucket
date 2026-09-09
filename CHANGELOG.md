# ChatBucket — Dedicated Server Migration (2026-09-06)

## What changed and why

The whole networking architecture flipped from peer-hosted (Tailscale +
Syncthing + arbitration) to a single 24/7 dedicated server on an Oracle
Cloud Ubuntu VM with a public IP. Every piece of ChatBucket application
functionality (chat, uploads, GIFs, stickers, SFX, Giphy proxy, YouTube /
yt-dlp, FCM push, presence) is preserved. See `docs/MIGRATION.md` for the
file-by-file breakdown.

### Architecture — before → after

| Concern | Before | After |
|---|---|---|
| Public port | `front_door.py` binds :5000, proxies to child :5001 | `server.py` binds :5000 directly (or :5000 loopback behind Caddy) |
| Entry point | `main.py` → `front_door.run(name)` (arbitration + supervisor) | `main.py` → `server.app.run(host, port)` |
| Leader election | `arbitration.py` + `host_state.py` + Syncthing | *(gone — VM is always the host)* |
| Client-mode redirect | `doorman.py` / front-door redirect responses | *(gone)* |
| Manager control | `manager_config.py` + loopback :5050 | *(gone — no Rust Manager on the VM)* |
| yt-dlp egress | Hardcoded `socks5://127.0.0.1:1080` | Direct; opt-in via `CHATBUCKET_YTDLP_PROXY` |
| Bind interface | Tailscale IPv4, fallback 0.0.0.0 | `0.0.0.0` by default; loopback behind reverse proxy |
| Firebase creds | Same (env var / repo-relative path) | Same, but the documented location is `/etc/chatbucket/` (outside working dir) |

### Files rewritten
- `main.py` — trivial entrypoint. Reads `CHATBUCKET_HOST` / `CHATBUCKET_PORT`.
- `server.py` — removed the hardcoded SOCKS5 proxy; direct-bind block at
  the bottom; added `_safe_join()` path-traversal guards on the static
  serve routes; ProxyFix middleware when `CHATBUCKET_PROXY_HOPS >= 1`;
  minimal security response headers.

### Files preserved as-is
- `push_notifications.py`, `presence_state.py`, `static/*`
  (except one stale comment updated in `static/index.js`)

### Files moved to `legacy/` (not executed)
`arbitration.py`, `host_state.py`, `doorman.py`, `front_door.py`,
`tcp_proxy.py`, `manager_config.py` — retained for reference per the
migration brief.

### New files
`README.md`, `.env.example`, `.gitignore` (extended),
`systemd/chatbucket.service`, `docs/DEPLOYMENT.md`, `docs/MIGRATION.md`,
`docs/SECURITY.md`, `legacy/README.md`, `requirements.txt` (extended),
`requirements-prod-posix.txt` (versions pinned).

### Runtime data format
Unchanged. Drop existing `messages/ uploads/ gifs/ stickers/ sfx/
presence/ push_tokens/` directories into `/opt/chatbucket/` and restart.

### Security — see `docs/SECURITY.md`
- Path-traversal guards on `/uploads`, `/gifs`, `/stickers`, `/sfx`
- Removed hardcoded SOCKS5 egress
- `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` headers
- systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, etc.)
- Secrets live in `/etc/chatbucket/`, not the source tree

### Still to do before public launch
- **Authentication** (Caddy basicauth / Cloudflare Access / VPN) — no auth in the app
- **Rate limiting** on `/upload` and `/api/music/*` and `/api/giphy/search`
- **HTTPS** via Caddy / nginx — do not run raw `http://<ip>:5000` for real users
- **Separate origin** for `/uploads/` to blunt malicious-upload XSS

---

## Previous release notes

### 2026-08-27 — Server-Heavy Optimization Pass

*(Preserved from the source VM's CHANGELOG.)*

#### GIF button fix
- `.icon.gif` now uses an **inlined data-URI SVG badge** (rounded rect + "GIF")
  instead of the external `/static/icons/gif.svg`, which 404'd on some
  deployments and exposed the cramped text-chip fallback you saw in the
  screenshot. Zero network requests, zero 404 surface. (`index.css`, `index.html`)

#### Server does the heavy lifting
- **Giphy proxy** — new `/api/giphy/search`. The browser used to call
  api.giphy.com directly (cross-origin TLS per search, 100KB+ JSON, API key
  shipped to every client). Now the server holds the key, reuses one
  keep-alive session, caches queries in RAM for 10 min, and slims each result
  to `{preview, full, title}` (~90% less JSON). (`server.py`, `index.js`)
- **GIF library tree cached** — `os.walk` used to run on every panel open.
  Now built once and invalidated only on upload/folder-create. (`server.py`)
- **gzip compression** via flask-compress (optional, graceful no-op if
  missing). `/history` payloads shrink ~75%. (`server.py`)
- **Conditional `/history`** — every page is ETag'd; the client sends
  `If-None-Match` and reuses its cached page on a 304. Scroll-back on an
  idle channel is now effectively free. (`server.py`, `index.js`)

#### Raised limits
| Knob | Was | Now |
|---|---|---|
| History page size | 50 | 100 |
| DOM cap (client) | 400 | 800 |
| General upload | 200 MB | 512 MB |
| GIF / Sticker / SFX | 15 MB | 50 MB |
| Edit window | 15 min | 30 min |
| Stream-URL cache | 200 | 1000 |
| yt-dlp search results | 10 | 20 |
| Search-cache TTL | 5 min | 10 min |

#### Client got lighter
- Giphy thumbnails get `decoding="async"` (off-main-thread decode).
- History responses are cached + conditionally revalidated (see above).
- Bigger PAGE_SIZE/DOM_CAP = fewer round trips, less churn.
