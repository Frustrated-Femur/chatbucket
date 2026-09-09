# Security posture

This document lists everything that used to be OK because ChatBucket was
running on a small trusted Tailscale mesh, but that becomes a real threat
on the public Internet, and what has (and has not yet) been mitigated in
this dedicated-server migration.

---

## Threat model

- The server is on the public Internet with a static IP.
- Anonymous attackers can hit any exposed endpoint.
- The user base is small and trusted, but there is **no authentication**
  in the current code — anyone with the URL can chat and upload.
- Uploaded files, GIFs, stickers, and SFX are served straight back with
  aggressive caching.
- The server runs `ffmpeg`, `openssl`, and `yt-dlp` subprocesses on behalf
  of requests.

---

## Already mitigated in this migration

### 1. Path traversal on static-serve routes

`/uploads/<path>`, `/gifs/<path>`, `/stickers/<folder>/<path>`, and
`/sfx/<folder>/<path>` now call `_safe_join()` to reject any request
where the resolved absolute path escapes the base directory. Werkzeug's
`send_from_directory` already does this, but the belt-and-braces check
survives a future Werkzeug regression.

### 2. Removed hardcoded egress proxy

`server.py` used to force yt-dlp through `socks5://127.0.0.1:1080` —
useful on the original VM, catastrophic anywhere without that proxy
(every YouTube request would hang). Now controlled by
`CHATBUCKET_YTDLP_PROXY` (unset = direct egress).

### 3. Trust-boundary headers

`ProxyFix` middleware is applied when `CHATBUCKET_PROXY_HOPS >= 1` so
`request.remote_addr` reflects the real client instead of the reverse
proxy (Caddy/nginx).

### 4. Basic security response headers

`X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
`X-Frame-Options: SAMEORIGIN` are added to every response.

### 5. systemd hardening

`chatbucket.service` runs as a dedicated non-privileged user with
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, restricted
address families, and a bounded set of writable paths.

### 6. Secrets separated from source

Firebase service account and Giphy API key live in `/etc/chatbucket/`,
not in the source tree. The systemd unit reads them via `EnvironmentFile=`.

---

## Not yet mitigated — YOU MUST HANDLE THESE BEFORE PUBLIC LAUNCH

### 7. No authentication ⚠️ HIGH

There is currently **no login system**. Anyone who knows the URL can:

- Read the entire message history
- Send messages as any username
- Upload files up to 512 MB
- Consume yt-dlp resources for arbitrary YouTube URLs
- Register FCM tokens under any username

**Options** (pick one before going live):

- **Reverse proxy Basic Auth** (fastest — Caddy `basicauth` directive)
- **Cloudflare Access / Zero Trust** (email-based, no code changes)
- **Wireguard / Tailscale in front of :5000** (VPN back to trusted network)
- **Application-level auth** (biggest change — not in scope of this migration)

Until this is fixed, **do not** point a real domain at the server without
also enabling Basic Auth or Cloudflare Access.

### 8. No rate limiting ⚠️ MEDIUM

The following endpoints are unlimited and expensive:

- `/upload` — 512 MB per request, one request per connection
- `/api/gifs/upload`, `/api/stickers/upload`, `/api/sfx/upload` — 50 MB each
- `/api/music/search`, `/api/music/stage`, `/api/music/stream/…` — kicks off
  yt-dlp subprocesses
- `/api/giphy/search` — proxies to api.giphy.com and eats your key's quota
- `/ws` — connection count and message rate

**Mitigation**: put a rate limiter in front. Caddy has
[`caddy-ratelimit`](https://github.com/mholt/caddy-ratelimit), nginx has
`limit_req_zone`, Cloudflare has WAF rate rules.

### 9. yt-dlp abuse potential ⚠️ MEDIUM

Anyone who can reach `/api/music/*` can make the server download from
YouTube on their behalf. YouTube's anti-bot may block your IP if abused,
and yt-dlp itself has occasional CVEs.

**Mitigation**:

- Combine with auth (item 7) so only known users can hit these endpoints.
- Pin `yt-dlp` versions and update on a schedule.
- Consider running yt-dlp in a nsjail/systemd sandbox.

### 10. Malicious uploads ⚠️ MEDIUM

Files uploaded to `/uploads/` are stored with a content-hash prefix but
**no MIME sniffing or virus scan**. They are served back with the
extension the uploader chose, so a `.html` upload becomes an active page
on your origin.

**Mitigations already partly in place**:

- Sticker uploads validate GIF/WebP magic bytes.
- `X-Content-Type-Options: nosniff` prevents MIME sniffing.
- Uploads are cached `immutable` but on the SAME origin as the app.

**Still to do**:

- Serve `/uploads/` from a separate origin (e.g. `uploads.example.com`) so
  a malicious `.html` cannot read the main-origin cookie.
- Optionally reject dangerous extensions server-side
  (`.html`, `.svg`, `.xhtml`, `.js`, `.wasm`).

### 11. Subprocess execution ⚠️ LOW

`server.py` calls `ffmpeg` on sticker video uploads. The arguments are
built from server-controlled paths, not user input, so this is safe as
written — but if you ever start passing raw user filenames into an
`ffmpeg` argv, escape them.

### 12. TLS ⚠️ MEDIUM

If you expose `http://<public-ip>:5000/` directly, everything (including
FCM tokens, usernames, and message content) is in cleartext. The
`docs/DEPLOYMENT.md` Caddy config solves this cleanly — do that before
you invite anyone.

### 13. CORS / CSRF

The current app trusts same-origin implicitly. There are no `POST` forms
with cookie-based auth (no cookies exist), so classical CSRF is not
exploitable, but once you add auth (item 7) revisit this.

---

## Quick-look checklist before going live

- [ ] Auth in front of the app (Caddy basicauth, Cloudflare Access, or VPN)
- [ ] HTTPS via Caddy or nginx (no plain HTTP on the public port)
- [ ] Rate limiting on `/upload`, `/api/music/*`, `/api/giphy/search`
- [ ] Backups scheduled (`docs/DEPLOYMENT.md` §9)
- [ ] Firebase service account stored in `/etc/chatbucket/`, mode 640
- [ ] `/etc/chatbucket/chatbucket.env` set with real values
- [ ] `ufw` (or Oracle security list + iptables) allows only 22 + 443
- [ ] `sudo systemctl status chatbucket` = `active (running)`
- [ ] Log retention configured (`docs/DEPLOYMENT.md` §8)
