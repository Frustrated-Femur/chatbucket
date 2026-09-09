# ChatBucket — Dedicated Server Edition

This is the migration of ChatBucket from a Tailscale / Syncthing / peer-hosted
architecture to a **single dedicated 24/7 server** (Oracle Cloud Ubuntu VM
with a public IP). Clients connect directly to this VM over the public
Internet — no arbitration, no host election, no doorman, no front-door
supervisor.

All existing ChatBucket functionality is preserved:

- Flask + flask-sock WebSocket chat
- Uploads (max 512 MB)
- GIFs / stickers / SFX libraries
- Giphy proxy (server-side, keeps API key server-only)
- YouTube playback + yt-dlp audio search / streaming
- FCM push notifications
- Presence tracking + unread-boundary API

The old multi-host code is **preserved but not executed** under `legacy/`
(see `docs/MIGRATION.md`).

---

## Quick start

```bash
# 1. System prerequisites (Ubuntu 22.04+)
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip ffmpeg openssl

# 2. Clone + venv
sudo useradd -r -m -d /opt/chatbucket -s /bin/bash chatbucket
sudo -u chatbucket bash <<'EOF'
cd /opt/chatbucket
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-prod-posix.txt
EOF

# 3. Secrets (outside working directory)
sudo mkdir -p /etc/chatbucket
sudo cp .env.example /etc/chatbucket/chatbucket.env
sudo $EDITOR /etc/chatbucket/chatbucket.env         # fill in values
sudo cp fcm_service_account.json /etc/chatbucket/   # from Firebase console
sudo chown -R chatbucket:chatbucket /etc/chatbucket
sudo chmod 750 /etc/chatbucket
sudo chmod 640 /etc/chatbucket/*

# 4. systemd unit
sudo cp systemd/chatbucket.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chatbucket
sudo systemctl status chatbucket
```

Then open port 5000 in the Oracle security list / iptables — see
`docs/DEPLOYMENT.md`.

---

## Repository layout

```
chatbucket-dedicated/
├── main.py                  # Entry point (dev / manual run)
├── server.py                # The Flask app — WebSocket, uploads, GIFs, YT, FCM
├── presence_state.py        # Per-user online/offline persistence
├── push_notifications.py    # FCM HTTP v1 push delivery
├── requirements.txt         # Runtime deps
├── requirements-prod-posix.txt # + gunicorn + gevent
├── .env.example             # Copy to /etc/chatbucket/chatbucket.env
├── .gitignore
├── static/                  # Frontend (HTML/CSS/JS + icons)
├── systemd/
│   └── chatbucket.service   # 24/7 unit file
├── docs/
│   ├── DEPLOYMENT.md        # Full production deployment guide
│   ├── MIGRATION.md         # From peer-hosted VM state to this layout
│   └── SECURITY.md          # Security posture + threat model
└── legacy/                  # Old multi-host code (not executed)
    ├── arbitration.py
    ├── host_state.py
    ├── doorman.py
    ├── front_door.py
    ├── tcp_proxy.py
    └── manager_config.py
```

## Runtime data (created on first run, not in source)

```
messages/       one JSON file per message
uploads/        user-uploaded files
gifs/           GIF library (folder-per-collection)
stickers/       sticker library
sfx/            audio sound-effect library
presence/       per-user presence + unread-boundary state
push_tokens/    registered FCM tokens
```

To restore an existing deployment: just drop these directories into
`/opt/chatbucket/` and restart the service. No schema changes.

---

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable | Default | Purpose |
|---|---|---|
| `CHATBUCKET_HOST` | `0.0.0.0` | Bind address |
| `CHATBUCKET_PORT` | `5000` | Bind port |
| `CHATBUCKET_PROXY_HOPS` | `0` | Set to `1` when Caddy/nginx is in front |
| `CHATBUCKET_YTDLP_PROXY` | *(unset)* | Optional outbound SOCKS5/HTTP proxy for yt-dlp |
| `GIPHY_API_KEY` | *(demo key)* | Your Giphy API key |
| `FIREBASE_SERVICE_ACCOUNT_KEY` | *(unset)* | FCM service account (inline JSON) |
| `GOOGLE_APPLICATION_CREDENTIALS` | *(unset)* | FCM service account (path) |

---

## Development / local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py                        # Werkzeug dev server on :5000
# or:
gunicorn -k gevent -w 1 --threads 8 -b 127.0.0.1:5000 server:app
```

The Werkzeug dev server (what `main.py` starts) is fine for local testing
but **must not** face the public Internet. Use the systemd unit + gunicorn
for the real deployment.

---

## Documentation

- **`docs/DEPLOYMENT.md`** — full step-by-step Ubuntu / Oracle Cloud deployment
- **`docs/MIGRATION.md`** — what changed vs the peer-hosted VM, and how to
  restore the existing message / upload data
- **`docs/SECURITY.md`** — hardening checklist and things you MUST fix before
  going live
