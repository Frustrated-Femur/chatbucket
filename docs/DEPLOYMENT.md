# Deployment — Ubuntu / Oracle Cloud (public Internet)

Target: Ubuntu 22.04 LTS or newer on an Oracle Cloud Compute VM with a
public IPv4 address. The same instructions work on any generic Ubuntu
box; only the “Oracle networking” section is cloud-specific.

---

## 1. Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    ffmpeg openssl \
    ufw
```

- **ffmpeg** is required for the sticker upload → transcode path (`server.py`
  calls it via `subprocess.run(["ffmpeg", ...])`).
- **openssl** is required by `push_notifications.py` to sign the FCM JWT.

Optional but recommended:

```bash
sudo apt-get install -y caddy    # if you want HTTPS termination
```

---

## 2. Service user and layout

```bash
sudo useradd -r -m -d /opt/chatbucket -s /bin/bash chatbucket
sudo mkdir -p /opt/chatbucket
sudo chown chatbucket:chatbucket /opt/chatbucket
```

Deploy the source into `/opt/chatbucket/` (git clone, rsync, or unzip this
package):

```bash
sudo -u chatbucket unzip chatbucket-dedicated.zip -d /opt/chatbucket
```

Then build the virtualenv:

```bash
sudo -u chatbucket bash <<'EOF'
cd /opt/chatbucket
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-prod-posix.txt
EOF
```

---

## 3. Secrets and environment

Secrets live **outside** `/opt/chatbucket/` so a `git clean -fdx` in the
working directory cannot destroy them:

```bash
sudo mkdir -p /etc/chatbucket
sudo cp /opt/chatbucket/.env.example /etc/chatbucket/chatbucket.env
sudo $EDITOR /etc/chatbucket/chatbucket.env       # fill in real values
```

Download your Firebase service-account JSON from the Firebase console
(*Project settings → Service accounts → Generate new private key*) and
place it at `/etc/chatbucket/fcm_service_account.json`, then reference it
from the env file:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/etc/chatbucket/fcm_service_account.json
```

Lock down permissions:

```bash
sudo chown -R chatbucket:chatbucket /etc/chatbucket
sudo chmod 750 /etc/chatbucket
sudo chmod 640 /etc/chatbucket/*
```

---

## 4. systemd service

```bash
sudo cp /opt/chatbucket/systemd/chatbucket.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chatbucket
sudo systemctl status chatbucket
sudo journalctl -u chatbucket -f              # follow logs
```

Restart policy is `always` with a 5-in-60s crash-loop cap and 3s backoff.
Graceful shutdown gives gunicorn 5s to finish requests + 10s systemd
headroom before SIGKILL.

### Updating the service

```bash
cd /opt/chatbucket
sudo -u chatbucket git pull                    # or rsync / unzip a new bundle
sudo -u chatbucket .venv/bin/pip install -r requirements.txt -r requirements-prod-posix.txt
sudo systemctl restart chatbucket
```

---

## 5. Firewall

```bash
sudo ufw allow 22/tcp                          # keep SSH open
sudo ufw allow 5000/tcp                        # ChatBucket (skip if using Caddy on 443)
sudo ufw enable
```

If you put Caddy in front:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
# ChatBucket bound only on 127.0.0.1:5000 — no ufw rule needed for :5000
```

---

## 6. Oracle Cloud networking

Oracle Cloud has **two** firewall layers to open — a common gotcha:

### 6.1 Security list (VCN)

*Oracle console → Networking → Virtual Cloud Networks → your VCN → Security Lists → Ingress Rules → Add*

| Source CIDR | Protocol | Destination Port |
|---|---|---|
| `0.0.0.0/0` | TCP | `5000` (or `443` for HTTPS) |
| `0.0.0.0/0` | TCP | `80` (only if using HTTP→HTTPS redirect) |

### 6.2 iptables on the VM

Oracle Ubuntu images ship with a `REJECT` rule blocking everything above
port 22 in `iptables`. Either replace it with `ufw` (as above) or add an
explicit ACCEPT above the reject:

```bash
sudo iptables -I INPUT -p tcp --dport 5000 -j ACCEPT
sudo netfilter-persistent save
```

Verify from your laptop:

```bash
curl -v http://<public-ip>:5000/health
# expected: HTTP/1.1 200 OK  {"status":"ok"}
```

---

## 7. HTTPS via Caddy (recommended)

Once DNS points a hostname (e.g. `chat.example.com`) at your public IP,
Caddy will auto-provision Let's Encrypt certs. Put ChatBucket behind it:

`/etc/caddy/Caddyfile`:

```caddy
chat.example.com {
    encode zstd gzip

    # WebSocket upgrade works transparently — no special config needed
    reverse_proxy 127.0.0.1:5000 {
        header_up X-Real-IP {remote_host}
        transport http {
            versions 1.1        # flask-sock speaks HTTP/1.1 upgrade only
        }
    }

    # Cap request bodies at 600 MB (server.py caps at 512 MB internally)
    request_body {
        max_size 600MB
    }
}
```

Then in `/etc/chatbucket/chatbucket.env`:

```bash
CHATBUCKET_HOST=127.0.0.1
CHATBUCKET_PORT=5000
CHATBUCKET_PROXY_HOPS=1        # trust one proxy hop
```

Restart both:

```bash
sudo systemctl restart chatbucket
sudo systemctl reload caddy
```

---

## 8. Log rotation

Logs go to the systemd journal. To keep only the last 30 days:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
cat <<EOF | sudo tee /etc/systemd/journald.conf.d/chatbucket.conf
[Journal]
MaxRetentionSec=30day
SystemMaxUse=1G
EOF
sudo systemctl restart systemd-journald
```

---

## 9. Backups

The only things you MUST back up are runtime data (source code is in
git):

```bash
#!/usr/bin/env bash
# /usr/local/bin/chatbucket-backup.sh
set -euo pipefail
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=/var/backups/chatbucket
mkdir -p "$DEST"
sudo -u chatbucket tar czf "$DEST/data-$STAMP.tar.gz" \
    -C /opt/chatbucket \
    messages uploads gifs stickers sfx presence push_tokens
find "$DEST" -type f -mtime +30 -delete
```

Run daily from cron. Copy off-box.

---

## 10. Verification checklist

- [ ] `systemctl status chatbucket` shows `active (running)`
- [ ] `curl http://127.0.0.1:5000/health` returns `{"status":"ok"}`
- [ ] `curl http://<public-ip>:5000/health` (from off-box) returns 200
- [ ] Browser at `http://<public-ip>:5000/` shows the ChatBucket UI
- [ ] WebSocket connects (DevTools → Network → WS tab shows `/ws?user=…` open)
- [ ] Sending a message from browser A appears in browser B in real time
- [ ] Uploading a file works and the file is served back from `/uploads/…`
- [ ] Giphy search returns results (`/api/giphy/search?q=cat`)
- [ ] YouTube search returns results (`/api/music/search?q=lofi`)
- [ ] FCM push arrives on an Android device (only after `GOOGLE_APPLICATION_CREDENTIALS` is set)
- [ ] Server survives `sudo systemctl restart chatbucket` without losing history
