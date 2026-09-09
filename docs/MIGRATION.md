# Migration — from peer-hosted VM to dedicated server

This document explains exactly what changed between the VM snapshot supplied
in `chatbucket-dedicated-server-source.zip` and this dedicated-server
layout, and how to restore your existing runtime data on top of it.

---

## Architectural summary

**Before** (peer-hosted, multi-machine):

```
        ┌───────── Tailscale MagicDNS ─────────┐
        │                                       │
   archlinux.tail…                        win1.tail…
        │                                       │
   main.py ──► front_door.py                main.py ──► front_door.py
   (bind :5000)                            (bind :5000)
       │  arbitrates via                       │  arbitrates via
       │  host-state.json                      │  host-state.json
       │  (Syncthing-shared)                   │  (Syncthing-shared)
       ▼                                       ▼
   spawns server.py                        redirects to
   on 127.0.0.1:5001                       whichever peer is host
   & proxies :5000 → :5001                 (302 → host.tail…:5000)
```

**Now** (dedicated Oracle Cloud VM):

```
   Internet
       │
       ▼  public IP / DNS
   Oracle VM :5000 (or :443 behind Caddy)
       │
   main.py  ─┐
             │  binds directly on 0.0.0.0
   server.py ┘
             │
             ├── HTTP / WebSocket / uploads
             ├── GIFs / stickers / SFX
             ├── Giphy proxy
             ├── YouTube / yt-dlp
             └── FCM push
```

No Tailscale required for normal client connectivity. No Syncthing required
for normal operation. No arbitration, no leader election, no doorman, no
front-door supervisor.

---

## File-by-file changes

### Files kept AS-IS

- `presence_state.py`
- `push_notifications.py`
- `static/*` (except one stale comment in `static/index.js`)
- `CHANGELOG.md`
- `VERSION`

### Files rewritten

| File | Old role | New role |
|---|---|---|
| `main.py` | Detect hostname, hand off to `front_door.run(name)` (which arbitrated, spawned server as child on :5001, and proxied :5000→:5001) | Simple entrypoint — reads `CHATBUCKET_HOST`/`CHATBUCKET_PORT` env vars and calls `server.app.run(host, port)` directly. **No arbitration, no supervisor, no child process.** |
| `server.py` | Ran on `127.0.0.1:5001` because the front door owned :5000; hardcoded `socks5://127.0.0.1:1080` egress for yt-dlp | Runs on `0.0.0.0:5000` directly (or 127.0.0.1 behind a reverse proxy). yt-dlp proxy is now opt-in via `CHATBUCKET_YTDLP_PROXY`. Added path-traversal guards on the static-serve routes, security response headers, and ProxyFix middleware. |
| `requirements.txt` | Bare minimum | Pins core deps + moves the optional-but-recommended `flask-compress` / `requests` in from “optional” |
| `requirements-prod-posix.txt` | `gunicorn`, `gevent` | Same, but versioned + documented |
| `.gitignore` | Runtime + secrets patterns | Same, plus explicitly ignores `state/`, `push_tokens/`, `fcm_service_account.json`, `service-account.json`, `firebase-adminsdk.json`, `.env` |

### Files moved to `legacy/` (preserved, not executed)

Per README_FOR_AI.md’s explicit instruction to *not blindly delete these*:

- `arbitration.py` — leader election, host-state.json read/write
- `host_state.py` — atomic host-state.json writer
- `doorman.py` — Flask redirect app for client-mode machines
- `front_door.py` — persistent supervisor / port-5000 owner / child manager
- `tcp_proxy.py` — byte-blind :5000 → :5001 splice
- `manager_config.py` — Rust Manager local config file

None of these are imported by `main.py` or `server.py` on the dedicated
server. They stay in-tree as reference material in case peer-hosting is ever
revived alongside the dedicated server.

### Files newly added

- `.env.example` — every environment variable the server understands
- `systemd/chatbucket.service` — 24/7 unit with hardening (see below)
- `docs/DEPLOYMENT.md` — full Ubuntu / Oracle Cloud walkthrough
- `docs/SECURITY.md` — security posture + must-fix-before-going-live list
- `docs/MIGRATION.md` — this document
- `README.md` — top-level overview

---

## Restoring runtime data from the VM

The following directories on the old VM contain user data. Copy them
into `/opt/chatbucket/` on the new dedicated server (they are all listed in
`.gitignore`, so nothing here is source-controlled):

```bash
# On the old VM:
sudo tar czf chatbucket-data.tar.gz \
    messages/ uploads/ gifs/ stickers/ sfx/ presence/ push_tokens/

# On the new server:
sudo -u chatbucket tar xzf chatbucket-data.tar.gz -C /opt/chatbucket/
sudo systemctl restart chatbucket
```

No format changes were made — every JSON schema and every filename layout
stays exactly as it was. `server.py`’s startup migration step for the
legacy `messages/chat.jsonl` file is still present, in case you are
migrating from an even older deployment.

### What NOT to migrate

- `state/host-state.json` — no longer used; the dedicated server does not
  arbitrate. Safe to leave behind.
- `manager_config.json` — Rust Manager config; not used on the dedicated
  server.
- `.chatbucket.pid` — front-door pidfile; not used.

### What to migrate separately

- **Firebase service account** — copy `fcm_service_account.json` into
  `/etc/chatbucket/` (NOT `/opt/chatbucket/`) with mode 0640 and
  ownership `chatbucket:chatbucket`. Reference it from the systemd
  environment file via `GOOGLE_APPLICATION_CREDENTIALS`.
- **Giphy API key** — set `GIPHY_API_KEY` in `/etc/chatbucket/chatbucket.env`.
- **yt-dlp cookies.txt** (optional) — place at `/opt/chatbucket/cookies.txt`
  if your original VM used one.

---

## Client-side changes

Effectively none. The frontend has always spoken to `location.origin` — it
does not know or care whether that origin is a Tailnet address or a public
IP. Only one dangling comment referring to the old arbitration.py’s
`SCHEME` note was updated in `static/index.js`.

If you previously bookmarked `http://archlinux.tail…:5000`, update the
bookmark to the new public origin (either `http://<public-ip>:5000` for a
smoke test, or `https://chat.example.com` once you put Caddy in front —
see `docs/DEPLOYMENT.md`).
