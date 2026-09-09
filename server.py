from flask import Flask, send_from_directory, jsonify, request, redirect
from flask_sock import Sock
try:
    # [PERF] Optional: gzip-compress JSON + static responses. Saves ~70-80%
    # bandwidth on /history and the media-library endpoints. Pure win, no
    # client change needed. Safe no-op when the package isn't installed.
    from flask_compress import Compress
except Exception:  # pragma: no cover
    Compress = None
try:
    # [PERF] Optional: keep-alive HTTP session for the Giphy proxy (connection
    # reuse + TLS session resumption across searches). Falls back to urllib.
    import requests
except Exception:  # pragma: no cover
    requests = None
from werkzeug.utils import secure_filename
import hashlib
import json
import os
import threading
import secrets
from datetime import datetime, timezone
import mimetypes
import re
import subprocess
import shutil
import presence_state
import yt_dlp
import push_notifications
import time
import urllib.parse
import urllib.request   # [PERF] stdlib fallback for the Giphy proxy when requests isn't installed
from concurrent.futures import ThreadPoolExecutor, as_completed
import bisect

# ── tunables ────────────────────────────────────────────────────────────────
PAGE_SIZE          = 100   # [PERF] 50→100: index serves pages in O(log n); fewer round trips
DOM_CAP            = 800   # max messages kept in browser DOM (client mirror)

RAM_CAP            = 100000
YTDLP_SEARCH_PAGE_SIZE = 20
MAX_FILE_SIZE_MB   = 512   # [PERF] 200→512 MB general uploads
MAX_CONTENT_LENGTH = MAX_FILE_SIZE_MB * 1024 * 1024
THORIUM_COOKIES = ('chromium', os.path.expanduser('~/.config/thorium/Default'))
UPLOAD_FOLDER      = "uploads"
GIF_BASE_DIR       = "gifs"
STICKER_BASE_DIR   = "stickers"
SFX_BASE_DIR       = "sfx"

MAX_GIF_SIZE_BYTES     = 50 * 1024 * 1024  # 50 MB (was 15)
MAX_STICKER_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB (was 15)
MAX_SFX_SIZE_BYTES     = 50 * 1024 * 1024  # 50 MB (was 15)

STICKER_ALLOWED_IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'}
STICKER_ALLOWED_VIDEOS = {'.mp4', '.webm', '.mov'}
STICKER_ALLOWED_ALL    = STICKER_ALLOWED_IMAGES | STICKER_ALLOWED_VIDEOS
SFX_ALLOWED_EXT    = {'.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac', '.opus'}

# ── yt-dlp tunables ─────────────────────────────────────────────────────────

COOKIES_TXT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

def _build_cookie_strategies():
    strategies = []
    # 1. Prioritize explicitly provided cookies.txt (Headless VM behavior)
    if os.path.exists(COOKIES_TXT_PATH):
        strategies.append(('cookies.txt', {'cookiefile': COOKIES_TXT_PATH}))

    # 2. Fall back to local browser only if the profile path actually exists (Local Dev)
    if os.path.exists(THORIUM_COOKIES[1]):
        strategies.append(('browser', {'cookiesfrombrowser': THORIUM_COOKIES}))

    # 3. Last resort
    strategies.append(('none', {}))
    return strategies

_COOKIE_STRATEGIES = _build_cookie_strategies()
_working_strategy   = None
_cookie_lock        = threading.Lock()
_cookie_race_pool = ThreadPoolExecutor(
    max_workers=max(len(_COOKIE_STRATEGIES) * 2, 4),
    thread_name_prefix="ytdlp-cookie",
)

# [DEDICATED] The socks5://127.0.0.1:1080 proxy used on the previous
# peer-hosted machine is gone: the Oracle VM has direct public Internet.
# Optionally re-enable an outbound proxy via the CHATBUCKET_YTDLP_PROXY
# environment variable (e.g. "socks5://user:pass@host:port") for regions
# where YouTube blocks direct egress.
_NETWORK_OPTS = {
    'socket_timeout': 10,
    'retries': 3,
    'fragment_retries': 3,
    'extractor_retries': 2,
    'proxy': 'socks5://127.0.0.1:1080',
    'concurrent_fragment_downloads': 4,
}
_YTDLP_PROXY = os.environ.get("CHATBUCKET_YTDLP_PROXY", "").strip()
if _YTDLP_PROXY:
    _NETWORK_OPTS['proxy'] = _YTDLP_PROXY
    print(f"[ytdlp] outbound proxy enabled via CHATBUCKET_YTDLP_PROXY")

def _extract_with(strategy_opts, base_opts, target):
    opts = {**_NETWORK_OPTS, **base_opts, **strategy_opts}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(target, download=False)

def ytdlp_extract(target, base_opts):
    global _working_strategy

    if _working_strategy is not None:
        name, strategy_opts = _COOKIE_STRATEGIES[_working_strategy]
        try:
            return _extract_with(strategy_opts, base_opts, target)
        except Exception as e:
            print(f"[ytdlp] cached strategy '{name}' failed ({e}); re-resolving...")
            with _cookie_lock:
                _working_strategy = None

    futures = {
        _cookie_race_pool.submit(_extract_with, opts, base_opts, target): idx
        for idx, (name, opts) in enumerate(_COOKIE_STRATEGIES)
    }
    first_error = None
    for future in as_completed(futures):
        idx = futures[future]
        try:
            result = future.result()
        except Exception as e:
            first_error = first_error or e
            continue
        with _cookie_lock:
            _working_strategy = idx
        print(f"[ytdlp] using cookie strategy '{_COOKIE_STRATEGIES[idx][0]}'")
        return result

    raise first_error or RuntimeError("All yt-dlp cookie strategies failed")

YTDLP_OPTS = {
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'simulate': True,
}

_STREAM_CACHE      = {}
_stream_cache_lock = threading.Lock()

_STREAM_INFLIGHT      = {}
_stream_inflight_lock = threading.Lock()

STREAM_URL_FALLBACK_TTL = 4 * 60 * 60
STREAM_CACHE_SWEEP_THRESHOLD = 1000  # [PERF] 200→1000: more URLs stay warm in RAM
STREAM_INFLIGHT_WAIT_TIMEOUT = 45

_ytdlp_prefetch_pool = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="ytdlp-prefetch",
)

def _parse_expire_param(url):
    try:
        query  = urllib.parse.urlparse(url).query
        expire = urllib.parse.parse_qs(query).get("expire", [None])[0]
        return int(expire) if expire else None
    except (ValueError, TypeError):
        return None

def _sweep_stream_cache(now):
    dead = [vid for vid, (_, exp) in _STREAM_CACHE.items() if exp <= now]
    for vid in dead:
        del _STREAM_CACHE[vid]

def _resolve_stream_url(video_id):
    now = time.time()

    with _stream_cache_lock:
        cached = _STREAM_CACHE.get(video_id)
    if cached and cached[1] > now:
        return cached[0]

    my_event = threading.Event()
    with _stream_inflight_lock:
        other_event = _STREAM_INFLIGHT.get(video_id)
        if other_event is None:
            _STREAM_INFLIGHT[video_id] = my_event

    if other_event is not None:
        if other_event.wait(timeout=STREAM_INFLIGHT_WAIT_TIMEOUT):
            with _stream_cache_lock:
                cached = _STREAM_CACHE.get(video_id)
            if cached and cached[1] > now:
                return cached[0]
        with _stream_inflight_lock:
            _STREAM_INFLIGHT[video_id] = my_event

    try:
        info = ytdlp_extract(f"https://www.youtube.com/watch?v={video_id}", YTDLP_OPTS)
        fresh_url = info.get("url")
        if not fresh_url:
            raise RuntimeError("yt-dlp returned no stream URL")

        expires_at = _parse_expire_param(fresh_url)
        expires_at = (expires_at - 30) if expires_at else (now + STREAM_URL_FALLBACK_TTL)

        with _stream_cache_lock:
            _STREAM_CACHE[video_id] = (fresh_url, expires_at)
            if len(_STREAM_CACHE) > STREAM_CACHE_SWEEP_THRESHOLD:
                _sweep_stream_cache(now)

        return fresh_url
    finally:
        with _stream_inflight_lock:
            if _STREAM_INFLIGHT.get(video_id) is my_event:
                del _STREAM_INFLIGHT[video_id]
        my_event.set()

def _prefetch_ytdlp_audio(video_id):
    try:
        _resolve_stream_url(video_id)
    except Exception as e:
        print(f"[ytdlp-prefetch] {video_id} failed (non-fatal): {e}")

def _maybe_prefetch_ytdlp_audio(video_id):
    if video_id:
        _ytdlp_prefetch_pool.submit(_prefetch_ytdlp_audio, video_id)


# ── app setup ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# [DEDICATED] Behind a reverse proxy (Caddy / nginx / Cloudflare), respect
# the standard forwarded-for headers so request.remote_addr and
# request.is_secure reflect the real client and scheme instead of the
# loopback proxy. When exposing the raw port 5000 directly this is a
# no-op — trust exactly one proxy hop at most.
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    _proxy_hops = int(os.environ.get("CHATBUCKET_PROXY_HOPS", "0"))
    if _proxy_hops > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=_proxy_hops, x_proto=_proxy_hops, x_host=_proxy_hops,
        )
        print(f"[server] ProxyFix enabled for {_proxy_hops} hop(s)")
except Exception:
    pass
sock = Sock(app)
if Compress is not None:
    # [PERF] gzip JSON + CSS/JS. /history payloads shrink ~75%; static sheets
    # compress once and are served from cache. Threshold keeps tiny responses
    # untouched.
    app.config.setdefault('COMPRESS_MIMETYPES', [
        'text/html', 'text/css', 'text/plain', 'application/javascript',
        'application/json', 'image/svg+xml',
    ])
    app.config.setdefault('COMPRESS_LEVEL', 6)
    app.config.setdefault('COMPRESS_MIN_SIZE', 1024)
    Compress(app)

@app.after_request
def _no_heuristic_cache_for_static(response):
    if request.path in ("/static/index.css", "/static/index.js"):
        response.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ── Giphy proxy (server does the heavy lifting) ─────────────────────────────
# The browser used to call api.giphy.com directly: cross-origin TLS per
# search, a 100KB+ JSON payload, and the API key shipped to every client.
# Now the client hits same-origin /api/giphy/search; the server holds the
# key, reuses one keep-alive session, caches identical queries in RAM, and
# slims each result to {preview, full, title} (~90% less JSON, then gzip).
GIPHY_API_KEY      = os.environ.get("GIPHY_API_KEY", "93jPMQfDg5ob5IJXf0Gp0YTWvF0rNSqd")
GIPHY_CACHE_TTL    = 600          # 10 min
GIPHY_CACHE_MAX    = 256          # distinct queries kept warm in RAM
GIPHY_TIMEOUT_S    = 6
_giphy_cache       = {}
_giphy_cache_lock  = threading.Lock()
_giphy_http        = requests.Session() if requests is not None else None
if _giphy_http is not None:
    _giphy_http.headers.update({"User-Agent": "ChatBucket/giphy-proxy"})


def _giphy_fetch(query, limit):
    params = {"q": query, "api_key": GIPHY_API_KEY, "limit": limit, "rating": "pg-13"}
    if _giphy_http is not None:
        return _giphy_http.get(
            "https://api.giphy.com/v1/gifs/search",
            params=params,
            timeout=GIPHY_TIMEOUT_S
        ).json()

    url = (
        "https://api.giphy.com/v1/gifs/search?"
        + urllib.parse.urlencode(params)
    )
    with urllib.request.urlopen(url, timeout=GIPHY_TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


@app.route("/api/giphy/search", methods=["GET"])
def giphy_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"data": []})
    try:
        limit = max(1, min(50, int(request.args.get("limit", 18))))
    except ValueError:
        limit = 18

    key = (query.lower(), limit)
    now = time.time()
    with _giphy_cache_lock:
        hit = _giphy_cache.get(key)
    if hit and now - hit[0] < GIPHY_CACHE_TTL:
        return jsonify(hit[1])

    try:
        raw = _giphy_fetch(query, limit)
    except Exception as e:
        return jsonify({"error": f"giphy upstream: {e}"}), 502

    slim = []
    for item in (raw.get("data") or []):
        images = item.get("images") or {}
        preview = (
            (images.get("fixed_height_small") or {}).get("url")
            or (images.get("fixed_height") or {}).get("url")
            or ""
        )
        full = (
            (images.get("original") or {}).get("url")
            or preview
        )
        if not full:
            continue

        slim.append({
            "preview": preview,
            "full": full,
            "title": item.get("title", "")
        })

    payload = {"data": slim}
    with _giphy_cache_lock:
        if len(_giphy_cache) >= GIPHY_CACHE_MAX:
            _giphy_cache.clear()          # bounded; refills lazily
        _giphy_cache[key] = (now, payload)
    return jsonify(payload)


for _d in (
    "messages",
    UPLOAD_FOLDER,
    os.path.join(UPLOAD_FOLDER, "yt"),
    GIF_BASE_DIR,
    STICKER_BASE_DIR,
    SFX_BASE_DIR,
    presence_state.PRESENCE_DIR,
    os.path.join(GIF_BASE_DIR, "general"),
    os.path.join(STICKER_BASE_DIR, "general"),
    os.path.join(SFX_BASE_DIR, "general"),
):
    os.makedirs(_d, exist_ok=True)


_OLD_CHAT = "messages/chat.jsonl"
if os.path.exists(_OLD_CHAT):
    try:
        with open(_OLD_CHAT, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _msg = json.loads(_line)
                    _sid = re.sub(
                        r'[^a-zA-Z0-9_\-]',
                        '_',
                        str(_msg.get("id", secrets.token_hex(8)))
                    )
                    _fp = os.path.join("messages", f"{_sid}.json")
                    if not os.path.exists(_fp):
                        with open(_fp, "w", encoding="utf-8") as _out:
                            json.dump(_msg, _out)
                except Exception:
                    pass

        os.rename(_OLD_CHAT, _OLD_CHAT + ".migrated")
        print("[startup] chat.jsonl migrated to per-message files")
    except Exception as _e:
        print(f"[startup] migration failed: {_e}")


_cleaned = 0
try:
    for _fname in os.listdir("messages"):
        if not _fname.endswith(".json"):
            continue

        _fpath = os.path.join("messages", _fname)

        try:
            with open(_fpath, "r", encoding="utf-8") as _f:
                _m = json.load(_f)

            if _m.get("type") == "system":
                os.remove(_fpath)
                _cleaned += 1

        except Exception:
            pass

    if _cleaned:
        print(
            f"[startup] removed {_cleaned} legacy system "
            f"(join/left) message file(s) from messages/"
        )

except Exception as _e:
    print(f"[startup] legacy system-message cleanup skipped: {_e}")


clients       = []
_clients_lock = threading.Lock()
_send_locks   = {}
_file_lock = threading.Lock()

connected_users = {}
_users_lock     = threading.Lock()


# ── disk helpers ─────────────────────────────────────────────────────────────

def _msg_id_to_path(msg_id):
    safe_id = re.sub(
        r'[^a-zA-Z0-9_\-]',
        '_',
        str(msg_id or secrets.token_hex(8))
    )
    return os.path.join("messages", f"{safe_id}.json")


# ── warm in-memory message index ────────────────────────────────────────────

_messages       = []
_ts_keys        = []
_messages_by_id = {}
_index_lock     = threading.RLock()
_index_loaded   = False
_dir_mtime_ns   = 0


def _load_index_locked():
    global _messages, _ts_keys, _messages_by_id

    msgs = []

    try:
        for fname in os.listdir("messages"):
            if not fname.endswith(".json"):
                continue

            try:
                with open(
                    os.path.join("messages", fname),
                    "r",
                    encoding="utf-8"
                ) as f:
                    msgs.append(json.load(f))
            except Exception:
                continue

    except Exception:
        pass

    msgs.sort(key=lambda m: m.get("timestamp", ""))
    _messages = msgs
    _ts_keys = [m.get("timestamp", "") for m in msgs]

    _messages_by_id = {}
    for m in msgs:
        mid = m.get("id")
        if mid:
            _messages_by_id[mid] = m


def _ensure_index():
    global _index_loaded, _dir_mtime_ns

    with _index_lock:
        stale = False

        try:
            st = os.stat("messages")
            stale = (st.st_mtime_ns != _dir_mtime_ns)
        except OSError:
            stale = not _index_loaded

        if not _index_loaded or stale:
            _load_index_locked()

            try:
                _dir_mtime_ns = os.stat("messages").st_mtime_ns
            except OSError:
                _dir_mtime_ns = 0

            _index_loaded = True


def _index_upsert(msg):
    mid = msg.get("id")
    if not mid:
        return

    with _index_lock:
        if mid in _messages_by_id:
            _messages[:] = [
                m for m in _messages if m.get("id") != mid
            ]
            _ts_keys[:] = [
                m.get("timestamp", "") for m in _messages
            ]

        _messages_by_id[mid] = msg
        ts = msg.get("timestamp", "")

        if not _ts_keys or _ts_keys[-1] <= ts:
            _messages.append(msg)
            _ts_keys.append(ts)
        else:
            i = bisect.bisect_left(_ts_keys, ts)
            _messages.insert(i, msg)
            _ts_keys.insert(i, ts)


def _write_message(msg):
    global _dir_mtime_ns

    fpath = _msg_id_to_path(msg.get("id"))

    with _file_lock:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(msg, f)

    _index_upsert(msg)

    try:
        _dir_mtime_ns = os.stat("messages").st_mtime_ns
    except OSError:
        pass


def _read_message_by_id(msg_id):
    if not msg_id:
        return None

    _ensure_index()

    with _index_lock:
        m = _messages_by_id.get(msg_id)
        if m is not None:
            return m

    try:
        with open(
            _msg_id_to_path(msg_id),
            "r",
            encoding="utf-8"
        ) as f:
            m = json.load(f)

        if m and m.get("id") == msg_id:
            _index_upsert(m)
            return m

    except Exception:
        pass

    return None


EDIT_WINDOW_SECONDS = 30 * 60  # [PERF] 15→30 min edit window


def _any_other_message_references_file(filename, exclude_id):
    _ensure_index()

    with _index_lock:
        for other in _messages:
            if str(other.get("id")) == str(exclude_id):
                continue

            if (
                other.get("type") == "file"
                and not other.get("deleted")
                and other.get("filename") == filename
            ):
                return True

    return False


def _delete_uploaded_file_for(msg):
    if msg.get("type") != "file":
        return

    filename = msg.get("filename") or ""

    if not filename or filename.startswith("http"):
        return

    if _any_other_message_references_file(
        filename,
        exclude_id=msg.get("id")
    ):
        return

    fpath = os.path.join(UPLOAD_FOLDER, filename)

    try:
        if os.path.isfile(fpath):
            os.remove(fpath)
    except OSError:
        pass


def _write_youtube_meta(msg):
    safe_id = re.sub(
        r'[^a-zA-Z0-9_\-]',
        '_',
        str(msg.get("id", secrets.token_hex(8)))
    )
    fpath = os.path.join(
        UPLOAD_FOLDER,
        "yt",
        f"{safe_id}.json"
    )

    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(msg, f)
    except OSError:
        pass


def _read_messages(before=None, after=None, limit=PAGE_SIZE):
    _ensure_index()

    with _index_lock:
        msgs = _messages
        keys = _ts_keys

        if before:
            i = bisect.bisect_left(keys, before)
            return msgs[:i][-limit:]

        if after:
            i = bisect.bisect_right(keys, after)
            return msgs[i:i + limit]

        return msgs[-limit:]


try:
    _ensure_index()
    print(f"[startup] message index warmed: {len(_messages)} messages in RAM")
except Exception as _e:
    print(f"[startup] message index warm-up skipped: {_e}")


def _online_usernames():
    with _users_lock:
        return list(set(connected_users.values()))


def _build_system_event(username, event):
    now = datetime.now(timezone.utc)

    return {
        "id": f"{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}_{username}_{event}",
        "type": "system",
        "event": event,
        "user": username,
        "timestamp": now.isoformat(),
        "time": now.strftime("%H:%M:%S"),
    }


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

MAX_BASENAME_LEN = 100


def _safe_upload_name(raw_name, fallback_base="file"):
    raw_leaf = os.path.basename((raw_name or "").replace("\\", "/"))
    raw_base, raw_ext = os.path.splitext(raw_leaf)

    base = secure_filename(raw_base)

    if not base or base.upper() in _WINDOWS_RESERVED_NAMES:
        base = fallback_base

    base = base[:MAX_BASENAME_LEN]

    ext = secure_filename(raw_ext).lower()

    if ext and not ext.startswith("."):
        ext = "." + ext

    return base, ext


CONTENT_HASH_LEN = 12


def _content_hash(file_storage, chunk_size=1024 * 1024):
    file_storage.stream.seek(0)

    h = hashlib.sha256()

    while True:
        chunk = file_storage.stream.read(chunk_size)

        if not chunk:
            break

        h.update(chunk)

    file_storage.stream.seek(0)

    return h.hexdigest()[:CONTENT_HASH_LEN]


def _safe_send(ws, data):
    lock = _send_locks.get(id(ws))

    if lock is None:
        ws.send(data)
        return

    with lock:
        ws.send(data)


def _broadcast(msg_str, exclude=None):
    dead = []

    for client in list(clients):
        if client is exclude:
            continue

        try:
            _safe_send(client, msg_str)
        except Exception:
            dead.append(client)

    if dead:
        with _clients_lock:
            for d in dead:
                if d in clients:
                    clients.remove(d)

                _send_locks.pop(id(d), None)


# ── routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    response = send_from_directory("static", "index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.after_request
def _security_headers(response):
    # [DEDICATED] Minimal hardening headers for a public-Internet deployment.
    # Kept conservative so they don't break the existing frontend / uploads;
    # tighten CSP later once you know the deck of asset origins you need.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.route("/history")
def history():
    before = request.args.get("before")
    after  = request.args.get("after")

    msgs = _read_messages(
        before=before,
        after=after,
        limit=PAGE_SIZE
    )

    # [PERF] Conditional request: the whole history lives in a warm RAM index,
    # so building this page costs the server ~nothing. The client sends the
    # ETag of its last copy via If-None-Match; when nothing changed we answer
    # 304 and the browser reuses its cached (already-parsed) page instead of
    # re-downloading + re-parsing the same JSON. Weak etag is fine — the body
    # is byte-identical for identical content.
    body = json.dumps(msgs)

    etag = 'W/"%x-%s"' % (
        len(body),
        hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    )

    if request.headers.get("If-None-Match") == etag:
        resp = app.response_class(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, no-cache"
        return resp

    resp = app.response_class(
        response=body,
        status=200,
        mimetype="application/json"
    )

    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, no-cache"

    return resp


@app.route("/history/v2")
def history_v2():
    """Cursor-based incremental sync feed for the Android background-sync path.

    GET /history/v2?after=<iso-timestamp>&limit=<n>
    -> {"messages": [...ascending...], "next_after": <iso>, "has_more": bool}

    The FCM wake-up handler and the app-start catch-up page FORWARD through this
    endpoint using `next_after` as the cursor, so an arbitrary backlog of
    messages that arrived while the app was killed is fully retrieved. Unlike
    /history there is deliberately NO ETag/304 caching here — background syncs
    must always see fresh data.
    """
    after = request.args.get("after")
    try:
        limit = int(request.args.get("limit", PAGE_SIZE))
    except (TypeError, ValueError):
        limit = PAGE_SIZE
    limit = max(1, min(limit, 500))

    msgs = _read_messages(after=after, limit=limit + 1)
    has_more = len(msgs) > limit
    msgs = msgs[:limit]

    next_after = msgs[-1].get("timestamp") if msgs else after

    resp = jsonify({
        "messages": msgs,
        "next_after": next_after,
        "has_more": has_more,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/send-message", methods=["POST"])
def api_send_message():
    """REST message-send used when no websocket is available (e.g. the Android
    notification quick-reply cold-starts the app process).

    Behaves exactly like the websocket message path: a client-supplied id is
    honoured, the message is persisted, broadcast to every live websocket
    client, and pushed via FCM — so peers update in real time and the sender
    never sees a duplicate of their own quick-reply.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    user = (data.get("user") or "Unknown").strip() or "Unknown"

    if not text:
        return jsonify({"success": False, "error": "Missing text"}), 400

    now = datetime.now(timezone.utc)

    msg = {
        "id": data.get("id") or (
            f"{now.strftime('%Y%m%d_%H%M%S')}_"
            f"{secrets.token_hex(3)}_"
            f"{user}"
        ),
        "user": user,
        "text": text,
        "timestamp": now.isoformat(),
        "time": now.strftime("%H:%M:%S"),
    }

    reply_to = data.get("replyTo")
    if isinstance(reply_to, dict):
        msg["replyTo"] = reply_to

    _write_message(msg)
    push_notifications.notify_new_message(msg)
    _broadcast(json.dumps(msg))

    return jsonify({"success": True, "id": msg["id"]})


@app.route("/unread-boundary")
def unread_boundary():
    user = request.args.get("user", "").strip()

    if not user:
        return jsonify({"boundary_ts": None})

    return jsonify({
        "boundary_ts": presence_state.last_left_epoch_ms(user)
    })


# ── [ANDROID-PERF] lightweight delta sync ────────────────────────────────────
# The Android client is local-first: it keeps its own SQLite replica and only
# needs "what changed since my cursor?" on reconnect / cold-open. Walking the
# warm RAM index and returning only rows after a timestamp costs O(log n) +
# O(k) (k = changed rows), versus re-pulling whole history pages. This is the
# single biggest server-side win for mobile smoothness + battery.
#
#   GET /api/messages/since?after=<ISO-timestamp>&limit=<n>
#     -> {"messages":[...], "latest": "<iso>", "hasMore": bool}
# `after` is EXCLUSIVE. Pass the newest timestamp you already hold; rows are
# returned oldest->newest so the client can append in order and advance its
# cursor to `latest`. Bounded by `limit` (default 200, max 500).
@app.route("/api/messages/since")
def messages_since():
    after = (request.args.get("after") or "").strip()
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(500, limit))

    _ensure_index()

    with _index_lock:
        # First index strictly greater than `after` (timestamps sortable ISO).
        start = bisect.bisect_right(_ts_keys, after)
        page = _messages[start:start + limit]
        has_more = (start + limit) < len(_messages)
        latest = page[-1].get("timestamp", after) if page else after

    return jsonify({
        "messages": page,
        "latest": latest,
        "hasMore": has_more,
    })


@app.route("/register-push", methods=["POST"])
@app.route("/api/push/register", methods=["POST"])
def register_push():
    """Register or update an FCM token for background Android push notifications."""

    data = (
        request.get_json(silent=True)
        or request.form.to_dict()
    )

    user = (data.get("user") or "").strip()
    token = (data.get("token") or "").strip()
    device_id = (data.get("device_id") or "").strip()
    platform = (data.get("platform") or "android").strip()

    if not user or not token:
        return jsonify({
            "success": False,
            "error": "Missing user or token"
        }), 400

    ok = push_notifications.register_token(
        user=user,
        token=token,
        device_id=device_id,
        platform=platform,
    )

    return jsonify({"success": ok})


@app.route("/unregister-push", methods=["POST"])
@app.route("/api/push/unregister", methods=["POST"])
def unregister_push():
    """Unregister an FCM token."""

    data = (
        request.get_json(silent=True)
        or request.form.to_dict()
    )

    token = (data.get("token") or "").strip()

    if not token:
        return jsonify({
            "success": False,
            "error": "Missing token"
        }), 400

    ok = push_notifications.unregister_token(token)

    return jsonify({"success": ok})


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    if _safe_join(UPLOAD_FOLDER, filename) is None:
        return jsonify({"error": "invalid path"}), 400
    response = send_from_directory(UPLOAD_FOLDER, filename)
    response.headers.add("Accept-Ranges", "bytes")
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


@app.route("/upload", methods=["POST"])
def upload_file():
    file    = request.files.get("file")
    user    = request.form.get("user", "Unknown")
    caption = request.form.get("caption", "")

    if not file:
        return {"error": "No file"}, 400

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    cleaned_base, extension = _safe_upload_name(file.filename)

    content_hash = _content_hash(file)
    filename = f"{content_hash}_{cleaned_base}{extension}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)

    if not os.path.exists(filepath):
        file.save(filepath)

    now = datetime.now(timezone.utc)

    msg_id = request.form.get("id") or \
             f"{timestamp}_{secrets.token_hex(3)}_{user}"

    is_sticker = request.form.get("isSticker") == "true"

    msg = {
        "id": msg_id,
        "type": "file",
        "user": user,
        "filename": filename,
        "caption": caption,
        "isSticker": is_sticker,
        "timestamp": now.isoformat(),
        "time": now.strftime("%H:%M:%S"),
    }

    reply_to_raw = request.form.get("replyTo")

    if reply_to_raw:
        try:
            msg["replyTo"] = json.loads(reply_to_raw)
        except Exception:
            pass

    _write_message(msg)
    push_notifications.notify_new_message(msg)

    msg_str = json.dumps(msg)
    _broadcast(msg_str)

    return {
        "success": True,
        "filename": filename
    }


@sock.route("/ws")
def websocket(ws):
    username = (
        (request.args.get("user") or "Unknown").strip()
        or "Unknown"
    )

    _send_locks[id(ws)] = threading.Lock()

    with _clients_lock:
        clients.append(ws)

    with _users_lock:
        connected_users[id(ws)] = username

    _safe_send(ws, json.dumps({
        "type": "init",
        "online_users": _online_usernames(),
    }))

    presence_state.write_presence(username, "online")

    _broadcast(
        json.dumps(
            _build_system_event(username, "joined")
        )
    )

    _broadcast(
        json.dumps({
            "type": "status",
            "user": username,
            "online": True
        }),
        exclude=ws
    )

    try:
        while True:
            data = ws.receive()

            if data is None:
                break

            msg = json.loads(data)
            msg_type = msg.get("type")

            if msg_type == "ping":
                _safe_send(
                    ws,
                    json.dumps({"type": "pong"})
                )
                continue

            if msg_type in ("typing", "status"):
                _broadcast(
                    json.dumps(msg),
                    exclude=ws
                )
                continue

            if msg_type == "delete":
                target_id = msg.get("id")
                target = (
                    _read_message_by_id(target_id)
                    if target_id
                    else None
                )

                if target is None:
                    _safe_send(
                        ws,
                        json.dumps({
                            "type": "delete_result",
                            "id": target_id,
                            "ok": False,
                            "reason": "not_found",
                        })
                    )
                    continue

                if target.get("user") != username:
                    _safe_send(
                        ws,
                        json.dumps({
                            "type": "delete_result",
                            "id": target_id,
                            "ok": False,
                            "reason": "not_owner",
                        })
                    )
                    continue

                if target.get("deleted"):
                    _broadcast(
                        json.dumps({
                            "type": "delete",
                            "id": target_id
                        })
                    )

                    _safe_send(
                        ws,
                        json.dumps({
                            "type": "delete_result",
                            "id": target_id,
                            "ok": True,
                            "already": True,
                        })
                    )
                    continue

                _delete_uploaded_file_for(target)

                tombstone = {
                    "id": target_id,
                    "type": target.get("type", "text"),
                    "user": target["user"],
                    "timestamp": target["timestamp"],
                    "time": target.get("time", ""),
                    "deleted": True,
                }

                if target.get("replyTo"):
                    tombstone["replyTo"] = target["replyTo"]

                _write_message(tombstone)

                _broadcast(
                    json.dumps({
                        "type": "delete",
                        "id": target_id
                    })
                )

                _safe_send(
                    ws,
                    json.dumps({
                        "type": "delete_result",
                        "id": target_id,
                        "ok": True,
                    })
                )
                continue

            if msg_type == "edit":
                target_id = msg.get("id")
                new_text = (msg.get("text") or "").strip()

                target = (
                    _read_message_by_id(target_id)
                    if target_id
                    else None
                )

                if (
                    target is None
                    or target.get("deleted")
                    or target.get("user") != username
                ):
                    _safe_send(
                        ws,
                        json.dumps({
                            "type": "edit_result",
                            "id": target_id,
                            "ok": False,
                            "reason": "rejected",
                        })
                    )
                    continue

                msg_kind = target.get("type")

                if target.get("isSticker") or msg_kind in (
                    "youtube",
                    "ytdlp_audio"
                ):
                    continue

                field = (
                    "caption"
                    if msg_kind == "file"
                    else "text"
                )

                if not new_text and field == "text":
                    continue

                try:
                    sent_at = datetime.fromisoformat(
                        target["timestamp"]
                    )
                except (
                    KeyError,
                    ValueError,
                    TypeError
                ):
                    continue

                if (
                    datetime.now(timezone.utc) - sent_at
                ).total_seconds() > EDIT_WINDOW_SECONDS:
                    _safe_send(
                        ws,
                        json.dumps({
                            "type": "edit_rejected",
                            "id": target_id,
                            "reason": "expired",
                        })
                    )
                    continue

                if target.get(field, "") == new_text:
                    continue

                target[field] = new_text
                target["edited"] = True

                _write_message(target)

                _broadcast(
                    json.dumps({
                        "type": "edit",
                        "id": target_id,
                        "field": field,
                        "value": new_text,
                    })
                )
                continue

            now = datetime.now(timezone.utc)

            if not msg.get("id"):
                msg["id"] = (
                    f"{now.strftime('%Y%m%d_%H%M%S')}_"
                    f"{secrets.token_hex(3)}_"
                    f"{msg.get('user', 'Unknown')}"
                )

            msg["timestamp"] = now.isoformat()
            msg["time"]      = now.strftime("%H:%M:%S")

            _write_message(msg)
            push_notifications.notify_new_message(msg)

            if msg_type == "youtube":
                _write_youtube_meta(msg)

            elif msg_type == "ytdlp_audio":
                _maybe_prefetch_ytdlp_audio(
                    msg.get("videoId")
                )

            _broadcast(json.dumps(msg))

    finally:
        with _clients_lock:
            if ws in clients:
                clients.remove(ws)

        _send_locks.pop(id(ws), None)

        with _users_lock:
            connected_users.pop(id(ws), None)
            still_online = username in connected_users.values()

        if not still_online:
            presence_state.write_presence(
                username,
                "offline"
            )

            _broadcast(
                json.dumps(
                    _build_system_event(username, "left")
                )
            )

            _broadcast(
                json.dumps({
                    "type": "status",
                    "user": username,
                    "online": False
                })
            )


# ── Local GIF Engine Routes ────────────────────────────────────────────────

# [PERF] GIF folder tree cache. The client used to trigger a full os.walk of
# the GIF library on EVERY panel open; for a large library that's hundreds of
# stat calls on the request thread. The tree changes only when an upload or
# folder-create lands, so we build it once and bump a version counter on any
# mutation. Panel opens become a dict lookup.
_gif_tree_cache      = None
_gif_tree_version    = 0
_gif_tree_built_for  = -1
_gif_tree_lock       = threading.Lock()


def _bump_gif_tree():
    global _gif_tree_version

    with _gif_tree_lock:
        _gif_tree_version += 1


@app.route("/api/gifs", methods=["GET"])
def list_local_gifs():
    global _gif_tree_cache, _gif_tree_built_for

    with _gif_tree_lock:
        if (
            _gif_tree_cache is not None
            and _gif_tree_built_for == _gif_tree_version
        ):
            return jsonify(_gif_tree_cache)

    tree = {}

    if os.path.exists(GIF_BASE_DIR):
        for root, dirs, files in os.walk(GIF_BASE_DIR):
            rel_folder = os.path.relpath(
                root,
                GIF_BASE_DIR
            )

            if rel_folder == ".":
                continue

            gif_files = [
                f for f in files
                if f.lower().endswith((".gif", ".webp"))
            ]

            tree[rel_folder] = sorted(gif_files)

    with _gif_tree_lock:
        _gif_tree_cache = tree
        _gif_tree_built_for = _gif_tree_version

    return jsonify(tree)


@app.route("/api/gifs/upload", methods=["POST"])
def upload_local_gif():
    file = request.files.get("file")

    target_folder = os.path.basename(
        os.path.normpath(
            request.form.get(
                "folder",
                "general"
            ).strip()
        )
    )

    if not file:
        return jsonify({
            "error": "No file payload received."
        }), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_GIF_SIZE_BYTES:
        return jsonify({
            "error": (
                f"File too heavy "
                f"({file_size/(1024*1024):.1f} MB). "
                "Max is 15 MB."
            )
        }), 400

    header = file.read(4)
    file.seek(0)

    is_gif = header.startswith(b'GIF8')

    peek12 = file.read(12)
    file.seek(0)

    is_webp = (
        header.startswith(b'RIFF')
        and b'WEBP' in peek12
    )

    if not (is_gif or is_webp):
        return jsonify({
            "error": (
                "Validation rejected: Asset must be a "
                ".gif or animated .webp"
            )
        }), 400

    dest_dir = os.path.join(
        GIF_BASE_DIR,
        target_folder
    )
    os.makedirs(dest_dir, exist_ok=True)

    safe_base, safe_ext = _safe_upload_name(
        file.filename,
        fallback_base="upload"
    )

    if safe_ext not in {".gif", ".webp"}:
        safe_ext = (
            ".webp" if is_webp else ".gif"
        )

    safe_filename = safe_base + safe_ext

    file.save(
        os.path.join(
            dest_dir,
            safe_filename
        )
    )

    _bump_gif_tree()

    return jsonify({
        "success": True,
        "path": (
            f"/gifs/{target_folder}/"
            f"{safe_filename}"
        )
    })


def _safe_join(base_dir, user_path):
    """Path-traversal guard for static-serve routes.

    send_from_directory in modern Werkzeug already rejects escapes, but we
    double-check here so a future Werkzeug regression cannot silently open
    a hole. Returns the absolute path or None if the request tries to
    escape base_dir.
    """
    base_abs = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base_abs, user_path))
    if not (target == base_abs or target.startswith(base_abs + os.sep)):
        return None
    return target


@app.route("/gifs/<path:filepath>")
def serve_local_gif(filepath):
    if _safe_join(GIF_BASE_DIR, filepath) is None:
        return jsonify({"error": "invalid path"}), 400
    response = send_from_directory(
        GIF_BASE_DIR,
        filepath
    )

    response.headers[
        "Cache-Control"
    ] = "public, max-age=604800, immutable"

    return response


@app.route("/api/gifs/create-folder", methods=["POST"])
def create_gif_folder():
    data = request.get_json(
        silent=True
    ) or {}

    name = re.sub(
        r'[^a-zA-Z0-9\-_ ]',
        '',
        data.get("name", "")
    ).strip()

    if not name:
        return jsonify({
            "error": "Invalid folder name."
        }), 400

    os.makedirs(
        os.path.join(
            GIF_BASE_DIR,
            name
        ),
        exist_ok=True
    )

    _bump_gif_tree()

    return jsonify({
        "success": True,
        "folder": name
    })


# ── Sticker Routes ─────────────────────────────────────────────────────────

@app.route("/api/stickers/folders", methods=["GET"])
def list_sticker_folders():
    if not os.path.exists(STICKER_BASE_DIR):
        return jsonify(["general"])

    folders = [
        f
        for f in os.listdir(STICKER_BASE_DIR)
        if os.path.isdir(
            os.path.join(
                STICKER_BASE_DIR,
                f
            )
        )
    ]

    if "general" not in folders:
        folders.insert(0, "general")

    return jsonify(sorted(folders))


@app.route("/api/stickers/create-folder", methods=["POST"])
def create_sticker_folder():
    data = request.get_json(
        silent=True
    ) or {}

    name = re.sub(
        r'[^a-zA-Z0-9\-_ ]',
        '',
        data.get("name", "")
    ).strip()

    if not name:
        return jsonify({
            "error": "Invalid folder name."
        }), 400

    os.makedirs(
        os.path.join(
            STICKER_BASE_DIR,
            name
        ),
        exist_ok=True
    )

    return jsonify({
        "success": True,
        "folder": name
    })


@app.route("/api/stickers/<folder>", methods=["GET"])
def list_stickers_in_folder(folder):
    target_dir = os.path.join(
        STICKER_BASE_DIR,
        folder
    )

    if not os.path.exists(target_dir):
        return jsonify([])

    allowed = (
        '.png',
        '.webp',
        '.gif',
        '.jpg',
        '.jpeg',
        '.avif',
        '.mp4',
        '.webm',
        '.mov'
    )

    files = [
        f
        for f in os.listdir(target_dir)
        if f.lower().endswith(allowed)
    ]

    return jsonify(sorted(files))


@app.route("/api/stickers/upload", methods=["POST"])
def upload_sticker():
    file = request.files.get("file")

    target_folder = (
        re.sub(
            r'[^a-zA-Z0-9\-_ ]',
            '',
            request.form.get(
                "folder",
                "general"
            ).strip()
        ) or "general"
    )

    if not file:
        return jsonify({
            "error": "No file received."
        }), 400

    _, ext = _safe_upload_name(
        file.filename,
        fallback_base="sticker"
    )

    if ext not in STICKER_ALLOWED_ALL:
        allowed_str = ", ".join(
            sorted(STICKER_ALLOWED_ALL)
        )

        return jsonify({
            "error": (
                f"Unsupported type '{ext}'. "
                f"Allowed: {allowed_str}"
            )
        }), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_STICKER_SIZE_BYTES:
        return jsonify({
            "error": (
                f"File too large "
                f"({file_size/(1024*1024):.1f} MB). "
                "Max is 15 MB."
            )
        }), 400

    dest_dir = os.path.join(
        STICKER_BASE_DIR,
        target_folder
    )
    os.makedirs(dest_dir, exist_ok=True)

    safe_base, _ = _safe_upload_name(
        file.filename,
        fallback_base="sticker"
    )

    safe_name = safe_base + ext

    if ext in STICKER_ALLOWED_VIDEOS:
        tmp_path = os.path.join(
            dest_dir,
            "tmp_" + safe_name
        )

        file.save(tmp_path)

        out_ext = (
            '.webm'
            if ext == '.webm'
            else '.mp4'
        )

        out_name = (
            os.path.splitext(safe_name)[0]
            + out_ext
        )

        out_path = os.path.join(
            dest_dir,
            out_name
        )

        if shutil.which("ffmpeg"):
            try:
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        tmp_path,
                        "-an",
                        "-c:v",
                        "copy",
                        out_path
                    ],
                    capture_output=True,
                    timeout=60
                )

                if result.returncode != 0:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            tmp_path,
                            "-an",
                            "-vf",
                            "scale='min(512,iw)':-2",
                            "-t",
                            "10",
                            out_path
                        ],
                        capture_output=True,
                        timeout=120
                    )

            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        else:
            out_name = (
                os.path.splitext(safe_name)[0]
                + ext
            )

            out_path = os.path.join(
                dest_dir,
                out_name
            )

            os.rename(
                tmp_path,
                out_path
            )

        return jsonify({
            "success": True,
            "path": (
                f"/stickers/{target_folder}/"
                f"{out_name}"
            )
        })

    final_path = os.path.join(
        dest_dir,
        safe_name
    )

    file.save(final_path)

    return jsonify({
        "success": True,
        "path": (
            f"/stickers/{target_folder}/"
            f"{safe_name}"
        )
    })


@app.route("/stickers/<folder>/<path:filepath>")
def serve_local_sticker(folder, filepath):
    # Clamp folder to a single leaf (no traversal via ../).
    folder = os.path.basename(folder or "")
    if not folder or _safe_join(os.path.join(STICKER_BASE_DIR, folder),
                                filepath) is None:
        return jsonify({"error": "invalid path"}), 400
    response = send_from_directory(
        os.path.join(
            STICKER_BASE_DIR,
            folder
        ),
        filepath
    )

    response.headers[
        "Cache-Control"
    ] = "public, max-age=604800, immutable"

    return response


# ── Local SFX (Audio) Routes ────────────────────────────────────────────────

@app.route("/api/sfx/folders", methods=["GET"])
def list_sfx_folders():
    if not os.path.exists(SFX_BASE_DIR):
        return jsonify(["general"])

    folders = [
        f
        for f in os.listdir(SFX_BASE_DIR)
        if os.path.isdir(
            os.path.join(
                SFX_BASE_DIR,
                f
            )
        )
    ]

    if "general" not in folders:
        folders.insert(0, "general")

    return jsonify(sorted(folders))


@app.route("/api/sfx/create-folder", methods=["POST"])
def create_sfx_folder():
    data = request.get_json(
        silent=True
    ) or {}

    name = re.sub(
        r'[^a-zA-Z0-9\-_ ]',
        '',
        data.get("name", "")
    ).strip()

    if not name:
        return jsonify({
            "error": "Invalid folder name."
        }), 400

    os.makedirs(
        os.path.join(
            SFX_BASE_DIR,
            name
        ),
        exist_ok=True
    )

    return jsonify({
        "success": True,
        "folder": name
    })


@app.route("/api/sfx/<folder>", methods=["GET"])
def list_sfx_in_folder(folder):
    target_dir = os.path.join(
        SFX_BASE_DIR,
        folder
    )

    if not os.path.exists(target_dir):
        return jsonify([])

    files = [
        f
        for f in os.listdir(target_dir)
        if f.lower().endswith(
            tuple(SFX_ALLOWED_EXT)
        )
    ]

    return jsonify(sorted(files))


@app.route("/api/sfx/upload", methods=["POST"])
def upload_sfx():
    file = request.files.get("file")

    target_folder = (
        re.sub(
            r'[^a-zA-Z0-9\-_ ]',
            '',
            request.form.get(
                "folder",
                "general"
            ).strip()
        ) or "general"
    )

    if not file:
        return jsonify({
            "error": "No file received."
        }), 400

    _, ext = _safe_upload_name(
        file.filename,
        fallback_base="audio"
    )

    if ext not in SFX_ALLOWED_EXT:
        allowed_str = ", ".join(
            sorted(SFX_ALLOWED_EXT)
        )

        return jsonify({
            "error": (
                f"Unsupported audio type '{ext}'. "
                f"Allowed: {allowed_str}"
            )
        }), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_SFX_SIZE_BYTES:
        return jsonify({
            "error": (
                f"File too large "
                f"({file_size/(1024*1024):.1f} MB). "
                "Max is 15 MB."
            )
        }), 400

    dest_dir = os.path.join(
        SFX_BASE_DIR,
        target_folder
    )

    os.makedirs(
        dest_dir,
        exist_ok=True
    )

    safe_base, _ = _safe_upload_name(
        file.filename,
        fallback_base="audio"
    )

    safe_name = safe_base + ext

    final_path = os.path.join(
        dest_dir,
        safe_name
    )

    file.save(final_path)

    return jsonify({
        "success": True,
        "path": (
            f"/sfx/{target_folder}/"
            f"{safe_name}"
        )
    })


@app.route("/sfx/<folder>/<path:filepath>")
def serve_local_sfx(folder, filepath):
    folder = os.path.basename(folder or "")
    if not folder or _safe_join(os.path.join(SFX_BASE_DIR, folder),
                                filepath) is None:
        return jsonify({"error": "invalid path"}), 400
    response = send_from_directory(
        os.path.join(
            SFX_BASE_DIR,
            folder
        ),
        filepath
    )

    response.headers[
        "Cache-Control"
    ] = "public, max-age=604800, immutable"

    return response


# ── YouTube Music Routes ────────────────────────────────────────────────────

_SEARCH_CACHE      = {}
_search_cache_lock = threading.Lock()
SEARCH_CACHE_TTL   = 600            # [PERF] 5→10 min
SEARCH_CACHE_MAX   = 256            # [PERF] bound the RAM map
YTDLP_SEARCH_PAGE_SIZE = 20         # [PERF] 10→20 results per yt-dlp search

_META_CACHE      = {}
_meta_cache_lock = threading.Lock()
META_CACHE_TTL   = 3600
META_CACHE_MAX   = 512              # [PERF] bound the RAM map


@app.route("/api/music/search", methods=["GET"])
def search_yt():
    query = request.args.get(
        "q",
        ""
    ).strip().lower()

    if not query:
        return jsonify([])

    page_arg = request.args.get("page")
    limit_arg = request.args.get("limit")
    paged_request = (
        page_arg is not None
        or limit_arg is not None
    )

    try:
        page = (
            int(page_arg)
            if page_arg is not None
            else 1
        )

        limit = (
            int(limit_arg)
            if limit_arg is not None
            else YTDLP_SEARCH_PAGE_SIZE
        )

    except ValueError:
        return jsonify({
            "error": "Invalid page or limit."
        }), 400

    page = max(1, page)
    limit = max(1, min(50, limit))

    now = time.time()
    cache_key = (
        query,
        page,
        limit
    )

    with _search_cache_lock:
        cached = _SEARCH_CACHE.get(cache_key)

    if cached and now - cached[0] < SEARCH_CACHE_TTL:
        payload = cached[1]

        return jsonify(
            payload
            if paged_request
            else payload["items"]
        )

    try:
        info = ytdlp_extract(
            f"ytsearch{page * limit + 1}:{query}",
            {
                'extract_flat': True,
                'quiet': True
            }
        )

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

    entries = info.get(
        'entries',
        []
    ) or []

    start_idx = (page - 1) * limit
    end_idx = start_idx + limit

    page_entries = entries[
        start_idx:end_idx
    ]

    results = []

    for entry in page_entries:
        thumb = (
            entry.get("thumbnails", [{}])[0].get("url")
            if entry.get("thumbnails")
            else ""
        )

        results.append({
            "id": entry.get("id"),
            "title": entry.get(
                "title",
                "Unknown Title"
            ),
            "duration": entry.get("duration"),
            "thumbnail": thumb
        })

    payload = {
        "items": results,
        "page": page,
        "limit": limit,
        "hasMore": len(entries) > end_idx,
    }

    with _search_cache_lock:
        if len(_SEARCH_CACHE) >= SEARCH_CACHE_MAX:
            _SEARCH_CACHE.clear()

        _SEARCH_CACHE[
            cache_key
        ] = (now, payload)

    return jsonify(
        payload
        if paged_request
        else results
    )


@app.route("/api/music/stage", methods=["POST"])
def stage_yt_audio():
    data = request.get_json(
        silent=True
    ) or {}

    video_id = data.get("id")

    if not video_id:
        return jsonify({
            "error": "No ID provided"
        }), 400

    now = time.time()

    with _meta_cache_lock:
        cached = _META_CACHE.get(video_id)

    if cached and now - cached[0] < META_CACHE_TTL:
        return jsonify({
            "success": True,
            "metadata": cached[1]
        })

    try:
        info = ytdlp_extract(
            f"https://www.youtube.com/watch?v={video_id}",
            {
                'quiet': True,
                'simulate': True
            }
        )

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

    metadata = {
        "yt_id": video_id,
        "title": info.get(
            "title",
            "Unknown Title"
        ),
        "thumbnail": info.get(
            "thumbnail",
            ""
        )
    }

    with _meta_cache_lock:
        if len(_META_CACHE) >= META_CACHE_MAX:
            _META_CACHE.clear()

        _META_CACHE[
            video_id
        ] = (now, metadata)

    return jsonify({
        "success": True,
        "metadata": metadata
    })


@app.route("/api/music/stream/<video_id>")
def stream_yt(video_id):
    try:
        fresh_url = _resolve_stream_url(video_id)

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

    if not fresh_url:
        return jsonify({
            "error": "Audio stream not found"
        }), 404

    return redirect(fresh_url)


# ── HTTP error handlers ─────────────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        "error": "File exceeds maximum allowed size."
    }), 413


if __name__ == "__main__":
    # [DEDICATED] Bind directly on the public Internet interface.
    #
    # This is the Werkzeug threaded dev server: convenient for a smoke test,
    # but NOT suitable for the public Internet on its own. For the 24/7
    # deployment, run under gunicorn + gevent instead (see
    # systemd/chatbucket.service and requirements-prod-posix.txt):
    #
    #   pip install gunicorn gevent flask-compress requests
    #   gunicorn -k gevent -w 1 --threads 16 -b 0.0.0.0:5000 \
    #            --graceful-timeout 5 server:app
    #
    # The bind address / port can be overridden via environment variables
    # so the same code path works behind a reverse proxy (Caddy/nginx on
    # 127.0.0.1) without editing source.
    _host = os.environ.get("CHATBUCKET_HOST", "0.0.0.0")
    try:
        _port = int(os.environ.get("CHATBUCKET_PORT", "5000"))
    except ValueError:
        _port = 5000
    print(f"[server] direct run on {_host}:{_port} (Werkzeug threaded); "
          f"use gunicorn for production")
    app.run(host=_host, port=_port, threaded=True)
