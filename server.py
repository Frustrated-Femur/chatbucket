from flask import Flask, send_from_directory, jsonify, request, redirect
from flask_sock import Sock
from werkzeug.utils import secure_filename
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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── tunables ──────────────────────────────────────────────────────────
PAGE_SIZE          = 50    # messages per /history page
DOM_CAP            = 400   # max messages kept in browser DOM

RAM_CAP            = 200   # max lines held in server RAM ring-buffer
YTDLP_SEARCH_PAGE_SIZE = 10
MAX_FILE_SIZE_MB   = 100
MAX_CONTENT_LENGTH = MAX_FILE_SIZE_MB * 1024 * 1024
THORIUM_COOKIES = ('chromium', os.path.expanduser('~/.config/thorium/Default'))
#CHAT_FILE         = "messages/chat.jsonl"
UPLOAD_FOLDER      = "uploads"
GIF_BASE_DIR       = "gifs"
STICKER_BASE_DIR   = "stickers"
SFX_BASE_DIR       = "sfx"


MAX_GIF_SIZE_BYTES     = 15 * 1024 * 1024  # 15 MB
MAX_STICKER_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_SFX_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB

# Sticker allowlists — images + short video clips
STICKER_ALLOWED_IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'}
STICKER_ALLOWED_VIDEOS = {'.mp4', '.webm', '.mov'}
STICKER_ALLOWED_ALL    = STICKER_ALLOWED_IMAGES | STICKER_ALLOWED_VIDEOS
SFX_ALLOWED_EXT    = {'.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac'}

# ── yt-dlp tunables ───────────────────────────────────────────────────
#
# Cookie strategy: three sources, tried in order of preference —
#   1. "browser"     — THORIUM_COOKIES (Thorium's live profile, read via
#                       yt-dlp's chromium-compatible backend). Always
#                       fresh, no manual re-export, but slower — it has to
#                       open + decrypt the SQLite cookie DB via the OS
#                       keyring on every call.
#   2. "cookies.txt" — a manually exported cookies.txt next to this
#                       script (COOKIES_TXT_PATH). Skipped automatically
#                       if the file isn't there — lets browser-decryption
#                       failures (e.g. a keyring mismatch) be worked around
#                       by dropping in a file, no code change needed.
#   3. "none"        — no cookies at all. Still resolves public,
#                       non-age-gated videos, so search/stream degrade
#                       instead of dying outright if both cookie sources
#                       are unavailable.
#
# On a cold start, or the moment the previously-working strategy stops
# working, these are NOT tried one at a time — sequentially waiting out a
# socket_timeout on each dead strategy before reaching a working one adds
# up fast (3 strategies * 10s timeout = up to 30s on a bad day). Instead
# every untried strategy is raced in parallel threads (_cookie_race_pool)
# and whichever succeeds first wins; that choice is cached
# (_working_strategy) so every later request is a single direct call with
# none of the racing overhead, until something actually breaks again.

COOKIES_TXT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

def _build_cookie_strategies():
    strategies = [('browser', {'cookiesfrombrowser': THORIUM_COOKIES})]
    if os.path.exists(COOKIES_TXT_PATH):
        strategies.append(('cookies.txt', {'cookiefile': COOKIES_TXT_PATH}))
    strategies.append(('none', {}))
    return strategies

_COOKIE_STRATEGIES = _build_cookie_strategies()
_working_strategy   = None          # index into _COOKIE_STRATEGIES; None = unresolved
_cookie_lock        = threading.Lock()
# Reused across every race instead of spinning up new threads each time.
_cookie_race_pool = ThreadPoolExecutor(
    max_workers=max(len(_COOKIE_STRATEGIES) * 2, 4),
    thread_name_prefix="ytdlp-cookie",
)

# Merged into every yt-dlp call. concurrent_fragment_downloads is yt-dlp's
# own "parallel connections" knob (-N on the CLI) — it only kicks in if a
# format ever needs multi-fragment (DASH/HLS) download, which doesn't
# happen today since everything here runs with simulate=True and just
# hands the browser a direct URL, but it's here so nothing needs retuning
# if that ever changes. socket_timeout/retries keep one stalled connection
# from hanging a Flask worker thread indefinitely.
_NETWORK_OPTS = {
    'socket_timeout': 10,
    'retries': 3,
    'fragment_retries': 3,
    'extractor_retries': 2,
    'concurrent_fragment_downloads': 4,
}


def _extract_with(strategy_opts, base_opts, target):
    """Run a single extraction attempt with one cookie strategy merged in."""
    opts = {**_NETWORK_OPTS, **base_opts, **strategy_opts}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(target, download=False)


def ytdlp_extract(target, base_opts):
    """
    Extract info for `target` (a URL, or a "ytsearchN:query" pseudo-URL)
    using whichever cookie strategy currently works.

    Fast path: a strategy already proved itself since the last failure —
    call it directly, no threads, no extra overhead.

    Cold-start / recovery path: race every strategy in _COOKIE_STRATEGIES
    on the shared pool at once and keep whichever finishes first
    successfully, caching that choice for next time.
    """
    global _working_strategy

    if _working_strategy is not None:
        name, strategy_opts = _COOKIE_STRATEGIES[_working_strategy]
        try:
            return _extract_with(strategy_opts, base_opts, target)
        except Exception as e:
            print(f"[ytdlp] cached strategy '{name}' failed ({e}); re-resolving...")
            with _cookie_lock:
                _working_strategy = None  # fall through to the race below

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
        # Don't wait on the remaining, slower/losing attempts — they
        # finish on their own in the background pool and their results
        # are simply discarded.
        return result

    raise first_error or RuntimeError("All yt-dlp cookie strategies failed")


# Format-selection profile for actual audio streaming (see
# /api/music/stream below). Cookies are applied per-call by
# ytdlp_extract(), not baked in here.
YTDLP_OPTS = {
    # Prioritize formats that browsers can play natively without server transcoding
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'simulate': True,
}

# ── app setup ─────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
sock = Sock(app)

@app.after_request
def _no_heuristic_cache_for_static(response):
    # [FIX] Flask's built-in /static/<path> handler (serving index.css,
    # index.js, icons, etc.) sets no Cache-Control header at all by default.
    # With nothing explicit, browsers fall back to RFC 7234 heuristic
    # freshness — estimating an acceptable cache lifetime from the file's
    # Last-Modified age — and can reuse a stale cached copy for hours
    # without ever asking the server again. That's what made index.css/
    # index.js edits intermittently invisible on reload: whichever tab
    # happened to have cached a pre-edit copy just kept serving it, while
    # any fresh Incognito window (empty cache) always fetched current.
    # no-cache (not no-store) still lets a conditional GET short-circuit to
    # a fast 304 via the ETag/Last-Modified Werkzeug already sets — this
    # only forces "always ask," not "never cache the body."
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

# Ensure all directories exist at startup
# Ensure all directories exist at startup
for _d in (
    "messages", UPLOAD_FOLDER, os.path.join(UPLOAD_FOLDER, "yt"), 
    GIF_BASE_DIR, STICKER_BASE_DIR, SFX_BASE_DIR, presence_state.PRESENCE_DIR,
    os.path.join(GIF_BASE_DIR, "general"),
    os.path.join(STICKER_BASE_DIR, "general"),
    os.path.join(SFX_BASE_DIR, "general"),
):
    os.makedirs(_d, exist_ok=True)

# One-time migration: chat.jsonl → per-message files
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
                    _sid = re.sub(r'[^a-zA-Z0-9_\-]', '_',
                                  str(_msg.get("id", secrets.token_hex(8))))
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

# One-time cleanup: strip legacy system (join/left) files that were
# previously persisted into messages/ before presence/ existed. These are
# exactly the files responsible for the "can't scroll up / phantom pages"
# bug — /history pagination could return pages made entirely of these
# invisible (display:none) entries. Presence tracking now lives in
# presence/ (see presence_state.py) instead; these files serve no purpose
# and actively poison pagination if left in place. Safe to run every boot —
# it's a no-op once the backlog is cleared.
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
        print(f"[startup] removed {_cleaned} legacy system (join/left) message file(s) from messages/")
except Exception as _e:
    print(f"[startup] legacy system-message cleanup skipped: {_e}")

clients       = []
_clients_lock = threading.Lock()   # guards add/remove on `clients`
_send_locks   = {}                 # id(ws) -> Lock, serializes writes per socket
_file_lock = threading.Lock()

# id(ws) -> username. Lets multiple sockets per username (e.g. two tabs)
# coexist without one tab's disconnect falsely marking the user offline.
connected_users = {}
_users_lock     = threading.Lock()


# ── disk helpers ──────────────────────────────────────────────────────

def _write_message(msg):
    """Write a single message as its own JSON file."""
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(msg.get("id", secrets.token_hex(8))))
    fpath = os.path.join("messages", f"{safe_id}.json")
    with _file_lock:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(msg, f)

def _write_youtube_meta(msg):
    """
    Archive a copy of every sent YouTube-embed share's metadata under
    uploads/yt/ (that directory is already created at startup — see the
    directory-setup loop above — this is the first thing that writes into it).

    This is NOT read back for chat rendering/history/pagination —
    messages/<id>.json (written by _write_message, above, unchanged)
    remains the sole source of truth for that. This copy exists purely so
    a future media-library page can enumerate every YouTube video ever
    shared without having to scan and type-check every file in messages/,
    the same way it can already enumerate uploads/, gifs/, and stickers/.

    Deliberately non-load-bearing: if this write fails, the chat message
    itself already succeeded via _write_message, so errors here are
    swallowed rather than risking the actual send over a secondary
    archival copy. Matches _write_message's own rigor level (plain write,
    no os.replace atomicity dance) rather than host_state.py's — that
    file is atomic because other processes repeatedly re-read it expecting
    a consistent current value; this file is written once and never read
    back synchronously by anything that would notice a torn write.

    Scope note: only "youtube" (IFrame-embed) shares get archived here,
    not "ytdlp_audio" — that's what was actually asked for ("that folder
    should just have yt send stuff"). Easy one-line change if you want
    ytdlp_audio archived too — see the call site in the websocket handler.
    """
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(msg.get("id", secrets.token_hex(8))))
    fpath = os.path.join(UPLOAD_FOLDER, "yt", f"{safe_id}.json")
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(msg, f)
    except OSError:
        pass

def _read_messages(before=None, after=None, limit=PAGE_SIZE):
    """Read all message files, sort by timestamp, apply filter + limit."""
    msgs = []
    try:
        for fname in os.listdir("messages"):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join("messages", fname), "r", encoding="utf-8") as f:
                    msgs.append(json.load(f))
            except Exception:
                pass
    except Exception:
        return []
    msgs.sort(key=lambda m: m.get("timestamp", ""))
    if before:
        msgs = [m for m in msgs if m.get("timestamp", "") < before]
        return msgs[-limit:]
    if after:
        # Earliest N *after* the cutoff (ascending), not the latest overall —
        # this fills the gap forward from where the client left off instead
        # of jumping straight to the present.
        msgs = [m for m in msgs if m.get("timestamp", "") > after]
        return msgs[:limit]
    return msgs[-limit:]

def _online_usernames():
    with _users_lock:
        return list(set(connected_users.values()))

def _build_system_event(username, event):
    """
    Build a 'joined'/'left' event dict for LIVE broadcast only.

    Deliberately does NOT call _write_message — these are ephemeral,
    exactly like 'typing'/'status' events below, and must never land in
    messages/. Persisting them there was the root cause of the pagination-
    pollution bug (a phone flapping its connection could fill /history
    pages entirely with invisible, display:none system entries). Durable
    join/leave history now lives in presence/ instead — see
    presence_state.py. This function exists only so currently-connected
    clients still see the same live system-message DOM node they always
    did; nothing about that live-only behavior changed.
    """
    now = datetime.now()
    return {
        "id":        f"{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}_{username}_{event}",
        "type":      "system",
        "event":     event,
        "user":      username,
        "timestamp": now.isoformat(),
        "time":      now.strftime("%H:%M:%S"),
    }

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
MAX_BASENAME_LEN = 100  # headroom under ext4/NTFS ~255-char limits once the
                         # timestamp_randomsuffix_ prefix is glued on at the call site


def _safe_upload_name(raw_name, fallback_base="file"):
    """Return a slash-free filename with a non-empty basename."""
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

def _safe_send(ws, data):
    """Serialize all writes to a given socket.

    flask_sock's underlying socket isn't safe for concurrent writers. If two
    threads call .send() on the SAME ws at once — e.g. a client's own thread
    replying to a ping with a pong, while another client's thread is mid-way
    through broadcasting a message to it — the two WebSocket frames can
    interleave on the wire. The browser then sees a corrupted frame
    ("Invalid frame header") and the connection dies with code 1006.
    Routing every send through this client's own lock prevents that.
    """
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


# ── routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    response = send_from_directory("static", "index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route("/health")
def health():
    # Liveness probe for the arbitration/doorman logic (see
    # ChatBucket_Networking_Architecture.md §3). Stays dependency-free —
    # no disk I/O, no message-store reads — so it always returns in
    # single-digit milliseconds regardless of server load. If this route
    # ever does real work, the arbitration timeout logic upstream becomes
    # unreliable, since a slow health check looks identical to a dead host.
    return jsonify({"status": "ok"}), 200

@app.route("/history")
def history():
    before = request.args.get("before")
    after  = request.args.get("after")
    return jsonify(_read_messages(before=before, after=after, limit=PAGE_SIZE))

@app.route("/unread-boundary")
def unread_boundary():
    """
    Presence-based unread boundary for a user: the epoch-ms timestamp of
    their last recorded "left" event (from presence/), or null if they
    have no presence record, or have never left (nothing to catch up on).

    This does NOT search messages/ itself. The client already has the
    exact logic needed — applyUnreadDivider() in app.js walks the rendered
    messages looking for the first one newer than a given timestamp — so
    re-implementing that search here would just be the same "find first
    thing after time T" logic living in two languages. All this route
    hands back is the timestamp; the client supplies the messages side.

    Used as a FALLBACK only: app.js prefers a client-local read-receipt
    (localStorage) when one exists, since that reflects an actual
    confirmed scroll position on this specific device. This value only
    matters on a device/browser with no local record yet — first visit,
    cleared storage, or a different device than last time.
    """
    user = request.args.get("user", "").strip()
    if not user:
        return jsonify({"boundary_ts": None})
    return jsonify({"boundary_ts": presence_state.last_left_epoch_ms(user)})

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    response = send_from_directory(UPLOAD_FOLDER, filename)
    response.headers.add("Accept-Ranges", "bytes")
    # [FIX] Every other asset route (/gifs/, /stickers/, /sfx/) already caches
    # aggressively since uploaded filenames are unique-per-upload and never
    # mutated in place — this route was the one gap, meaning every chat GIF/
    # file got re-fetched from origin on every reload/scrollback instead of
    # hitting browser cache.
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


@app.route("/upload", methods=["POST"])
def upload_file():
    file    = request.files.get("file")
    user    = request.form.get("user", "Unknown")
    caption = request.form.get("caption", "")

    if not file:
        return {"error": "No file"}, 400

    timestamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    random_suffix = secrets.token_hex(4)
    cleaned_base, extension = _safe_upload_name(file.filename)
    filename  = f"{timestamp}_{random_suffix}_{cleaned_base}{extension}"
    filepath  = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    now        = datetime.now()
    msg_id     = request.form.get("id") or \
                 f"{timestamp}_{secrets.token_hex(3)}_{user}"
    is_sticker = request.form.get("isSticker") == "true"

    msg = {
        "id":        msg_id,
        "type":      "file",
        "user":      user,
        "filename":  filename,
        "caption":   caption,
        "isSticker": is_sticker,
        "timestamp": now.isoformat(),
        "time":      now.strftime("%H:%M:%S"),
    }

    reply_to_raw = request.form.get("replyTo")
    if reply_to_raw:
        try:
            msg["replyTo"] = json.loads(reply_to_raw)
        except Exception:
            pass

    _write_message(msg)
    msg_str = json.dumps(msg)
    _broadcast(msg_str)
    return {"success": True, "filename": filename}


@sock.route("/ws")
def websocket(ws):
    username = (request.args.get("user") or "Unknown").strip() or "Unknown"

    _send_locks[id(ws)] = threading.Lock()
    with _clients_lock:
        clients.append(ws)
    with _users_lock:
        connected_users[id(ws)] = username

    _safe_send(ws, json.dumps({
        "type":         "init",
        "online_users": _online_usernames(),
    }))

    presence_state.write_presence(username, "online")
    _broadcast(json.dumps(_build_system_event(username, "joined")))
    _broadcast(json.dumps({"type": "status", "user": username, "online": True}), exclude=ws)

    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            msg = json.loads(data)

            msg_type = msg.get("type")

            if msg_type == "ping":
                _safe_send(ws, json.dumps({"type": "pong"}))
                continue

            # Ephemeral events — relay live to other clients, NEVER persist.
            # (This was the bug: typing events had no case here, fell through
            # to the write+broadcast path below, and got saved as permanent
            # message files with no "text" field — which then crashed the
            # history renderer on next page load.)
            if msg_type in ("typing", "status"):
                _broadcast(json.dumps(msg), exclude=ws)
                continue

            now = datetime.now()
            if not msg.get("id"):
                msg["id"] = (f"{now.strftime('%Y%m%d_%H%M%S')}_"
                             f"{secrets.token_hex(3)}_{msg.get('user', 'Unknown')}")
            msg["timestamp"] = now.isoformat()
            msg["time"]      = now.strftime("%H:%M:%S")
            _write_message(msg)
            if msg_type == "youtube":
                _write_youtube_meta(msg)
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
            presence_state.write_presence(username, "offline")
            _broadcast(json.dumps(_build_system_event(username, "left")))
            _broadcast(json.dumps({"type": "status", "user": username, "online": False}))


# ── Local GIF Engine Routes ───────────────────────────────────────────

@app.route("/api/gifs", methods=["GET"])
def list_local_gifs():
    """Traverse nested GIF structure and return folder → file lists."""
    tree = {}
    if not os.path.exists(GIF_BASE_DIR):
        return jsonify(tree)
    for root, dirs, files in os.walk(GIF_BASE_DIR):
        rel_folder = os.path.relpath(root, GIF_BASE_DIR)
        if rel_folder == ".":
            continue
        gif_files = [f for f in files if f.lower().endswith(('.gif', '.webp'))]
        tree[rel_folder] = gif_files
    return jsonify(tree)


@app.route("/api/gifs/upload", methods=["POST"])
def upload_local_gif():
    file          = request.files.get("file")
    target_folder = os.path.basename(
        os.path.normpath(request.form.get("folder", "general").strip()))

    if not file:
        return jsonify({"error": "No file payload received."}), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_GIF_SIZE_BYTES:
        return jsonify({
            "error": f"File too heavy ({file_size/(1024*1024):.1f} MB). Max is 15 MB."
        }), 400

    header = file.read(4)
    file.seek(0)
    is_gif  = header.startswith(b'GIF8')
    peek12  = file.read(12)
    file.seek(0)
    is_webp = header.startswith(b'RIFF') and b'WEBP' in peek12

    if not (is_gif or is_webp):
        return jsonify({
            "error": "Validation rejected: Asset must be a .gif or animated .webp"
        }), 400

    dest_dir = os.path.join(GIF_BASE_DIR, target_folder)
    os.makedirs(dest_dir, exist_ok=True)
    safe_base, safe_ext = _safe_upload_name(file.filename, fallback_base="upload")
    if safe_ext not in {".gif", ".webp"}:
        safe_ext = ".webp" if is_webp else ".gif"
    safe_filename = safe_base + safe_ext
    file.save(os.path.join(dest_dir, safe_filename))
    return jsonify({"success": True, "path": f"/gifs/{target_folder}/{safe_filename}"})


@app.route("/gifs/<path:filepath>")
def serve_local_gif(filepath):
    response = send_from_directory(GIF_BASE_DIR, filepath)
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response
@app.route("/api/gifs/create-folder", methods=["POST"])
def create_gif_folder():
    """Immediately create a new gif folder on disk."""
    data = request.get_json(silent=True) or {}
    name = re.sub(r'[^a-zA-Z0-9\-_ ]', '', data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Invalid folder name."}), 400
    os.makedirs(os.path.join(GIF_BASE_DIR, name), exist_ok=True)
    return jsonify({"success": True, "folder": name})

# ── Sticker Routes ────────────────────────────────────────────────────

@app.route("/api/stickers/folders", methods=["GET"])
def list_sticker_folders():
    """Return all folder names inside the stickers directory."""
    if not os.path.exists(STICKER_BASE_DIR):
        return jsonify(["general"])
    folders = [f for f in os.listdir(STICKER_BASE_DIR)
               if os.path.isdir(os.path.join(STICKER_BASE_DIR, f))]
    if "general" not in folders:
        folders.insert(0, "general")
    return jsonify(sorted(folders))


@app.route("/api/stickers/create-folder", methods=["POST"])
def create_sticker_folder():
    """Immediately create a new sticker folder on disk."""
    data = request.get_json(silent=True) or {}
    name = re.sub(r'[^a-zA-Z0-9\-_ ]', '', data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Invalid folder name."}), 400
    os.makedirs(os.path.join(STICKER_BASE_DIR, name), exist_ok=True)
    return jsonify({"success": True, "folder": name})


@app.route("/api/stickers/<folder>", methods=["GET"])
def list_stickers_in_folder(folder):
    """Return all sticker filenames (images + videos) inside a folder."""
    target_dir = os.path.join(STICKER_BASE_DIR, folder)
    if not os.path.exists(target_dir):
        return jsonify([])
    allowed = ('.png', '.webp', '.gif', '.jpg', '.jpeg', '.avif',
               '.mp4', '.webm', '.mov')
    files = [f for f in os.listdir(target_dir) if f.lower().endswith(allowed)]
    return jsonify(sorted(files))


@app.route("/api/stickers/upload", methods=["POST"])
def upload_sticker():
    """
    Upload a sticker (any image or short video).
    - Images are saved as-is.
    - Videos have their audio stripped via ffmpeg before saving.
      If ffmpeg is not found, the file is saved without audio stripping.
    """
    file          = request.files.get("file")
    target_folder = (
        re.sub(r'[^a-zA-Z0-9\-_ ]', '',
               request.form.get("folder", "general").strip()) or "general"
    )

    if not file:
        return jsonify({"error": "No file received."}), 400

    _, ext = _safe_upload_name(file.filename, fallback_base="sticker")
    if ext not in STICKER_ALLOWED_ALL:
        allowed_str = ", ".join(sorted(STICKER_ALLOWED_ALL))
        return jsonify({
            "error": f"Unsupported type '{ext}'. Allowed: {allowed_str}"
        }), 400

    # Size check before doing anything expensive
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_STICKER_SIZE_BYTES:
        return jsonify({
            "error": (f"File too large ({file_size/(1024*1024):.1f} MB). "
                      "Max is 15 MB.")
        }), 400

    dest_dir = os.path.join(STICKER_BASE_DIR, target_folder)
    os.makedirs(dest_dir, exist_ok=True)

    safe_base, _ = _safe_upload_name(file.filename, fallback_base="sticker")
    safe_name = safe_base + ext

    if ext in STICKER_ALLOWED_VIDEOS:
        # Save to a temp file first, then process with ffmpeg
        tmp_path = os.path.join(dest_dir, "tmp_" + safe_name)
        file.save(tmp_path)

        out_ext  = '.webm' if ext == '.webm' else '.mp4'
        out_name = os.path.splitext(safe_name)[0] + out_ext
        out_path = os.path.join(dest_dir, out_name)

        if shutil.which("ffmpeg"):
            try:
                # Fast path: stream-copy video track, drop audio (-an)
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp_path,
                     "-an", "-c:v", "copy", out_path],
                    capture_output=True, timeout=60
                )
                if result.returncode != 0:
                    # Fallback: re-encode, cap width to 512 px, trim to 10 s
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", tmp_path, "-an",
                         "-vf", "scale='min(512,iw)':-2",
                         "-t", "10", out_path],
                        capture_output=True, timeout=120
                    )
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            # ffmpeg unavailable — save without audio stripping
            out_name = os.path.splitext(safe_name)[0] + ext
            out_path = os.path.join(dest_dir, out_name)
            os.rename(tmp_path, out_path)

        return jsonify({
            "success": True,
            "path": f"/stickers/{target_folder}/{out_name}"
        })

    # Image sticker — save directly
    final_path = os.path.join(dest_dir, safe_name)
    file.save(final_path)
    return jsonify({
        "success": True,
        "path": f"/stickers/{target_folder}/{safe_name}"
    })


@app.route("/stickers/<folder>/<path:filepath>")
def serve_local_sticker(folder, filepath):
    response = send_from_directory(os.path.join(STICKER_BASE_DIR, folder), filepath)
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response
# ── Local SFX (Audio) Routes ──────────────────────────────────────────

@app.route("/api/sfx/folders", methods=["GET"])
def list_sfx_folders():
    """Return all folder names inside the sfx directory."""
    if not os.path.exists(SFX_BASE_DIR):
        return jsonify(["general"])
    folders = [f for f in os.listdir(SFX_BASE_DIR)
               if os.path.isdir(os.path.join(SFX_BASE_DIR, f))]
    if "general" not in folders:
        folders.insert(0, "general")
    return jsonify(sorted(folders))


@app.route("/api/sfx/create-folder", methods=["POST"])
def create_sfx_folder():
    """Immediately create a new sfx folder on disk."""
    data = request.get_json(silent=True) or {}
    name = re.sub(r'[^a-zA-Z0-9\-_ ]', '', data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Invalid folder name."}), 400
    os.makedirs(os.path.join(SFX_BASE_DIR, name), exist_ok=True)
    return jsonify({"success": True, "folder": name})


@app.route("/api/sfx/<folder>", methods=["GET"])
def list_sfx_in_folder(folder):
    """Return all audio filenames inside a folder."""
    target_dir = os.path.join(SFX_BASE_DIR, folder)
    if not os.path.exists(target_dir):
        return jsonify([])
    files = [f for f in os.listdir(target_dir) if f.lower().endswith(tuple(SFX_ALLOWED_EXT))]
    return jsonify(sorted(files))


@app.route("/api/sfx/upload", methods=["POST"])
def upload_sfx():
    """Upload a local audio file (SFX)."""
    file          = request.files.get("file")
    target_folder = (
        re.sub(r'[^a-zA-Z0-9\-_ ]', '',
               request.form.get("folder", "general").strip()) or "general"
    )

    if not file:
        return jsonify({"error": "No file received."}), 400

    _, ext = _safe_upload_name(file.filename, fallback_base="audio")
    if ext not in SFX_ALLOWED_EXT:
        allowed_str = ", ".join(sorted(SFX_ALLOWED_EXT))
        return jsonify({
            "error": f"Unsupported audio type '{ext}'. Allowed: {allowed_str}"
        }), 400

    # Size check before saving
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_SFX_SIZE_BYTES:
        return jsonify({
            "error": (f"File too large ({file_size/(1024*1024):.1f} MB). "
                      "Max is 15 MB.")
        }), 400

    dest_dir = os.path.join(SFX_BASE_DIR, target_folder)
    os.makedirs(dest_dir, exist_ok=True)

    safe_base, _ = _safe_upload_name(file.filename, fallback_base="audio")
    safe_name = safe_base + ext
    final_path = os.path.join(dest_dir, safe_name)
    
    file.save(final_path)
    return jsonify({
        "success": True,
        "path": f"/sfx/{target_folder}/{safe_name}"
    })


@app.route("/sfx/<folder>/<path:filepath>")
def serve_local_sfx(folder, filepath):
    response = send_from_directory(os.path.join(SFX_BASE_DIR, folder), filepath)
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response

# ── YouTube Music Routes ──────────────────────────────────────────────
_SEARCH_CACHE      = {}
_search_cache_lock = threading.Lock()
SEARCH_CACHE_TTL   = 300
YTDLP_SEARCH_PAGE_SIZE = 10

_META_CACHE      = {}
_meta_cache_lock = threading.Lock()
META_CACHE_TTL   = 3600  # titles/thumbnails barely change; cache longer than search


@app.route("/api/music/search", methods=["GET"])
def search_yt():
    query = request.args.get("q", "").strip().lower()
    if not query:
        return jsonify([])

    page_arg = request.args.get("page")
    limit_arg = request.args.get("limit")
    paged_request = page_arg is not None or limit_arg is not None

    try:
        page = int(page_arg) if page_arg is not None else 1
        limit = int(limit_arg) if limit_arg is not None else YTDLP_SEARCH_PAGE_SIZE
    except ValueError:
        return jsonify({"error": "Invalid page or limit."}), 400

    page = max(1, page)
    limit = max(1, min(50, limit))

    now = time.time()
    cache_key = (query, page, limit)
    with _search_cache_lock:
        cached = _SEARCH_CACHE.get(cache_key)
    if cached and now - cached[0] < SEARCH_CACHE_TTL:
        payload = cached[1]
        return jsonify(payload if paged_request else payload["items"])

    try:
        # Ask yt-dlp for one extra item so we can tell whether another page exists.
        # page=1, limit=10 -> request 11
        # page=2, limit=10 -> request 21
        info = ytdlp_extract(
            f"ytsearch{page * limit + 1}:{query}",
            {'extract_flat': True, 'quiet': True}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    entries = info.get('entries', []) or []
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit

    page_entries = entries[start_idx:end_idx]
    results = []
    for entry in page_entries:
        thumb = entry.get("thumbnails", [{}])[0].get("url") if entry.get("thumbnails") else ""
        results.append({
            "id": entry.get("id"),
            "title": entry.get("title", "Unknown Title"),
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
        _SEARCH_CACHE[cache_key] = (now, payload)

    return jsonify(payload if paged_request else results)


@app.route("/api/music/stage", methods=["POST"])
def stage_yt_audio():
    data = request.get_json(silent=True) or {}
    video_id = data.get("id")
    if not video_id:
        return jsonify({"error": "No ID provided"}), 400

    now = time.time()
    with _meta_cache_lock:
        cached = _META_CACHE.get(video_id)
    if cached and now - cached[0] < META_CACHE_TTL:
        return jsonify({"success": True, "metadata": cached[1]})

    try:
        # Fast metadata extraction. video_id is a bare 11-char YouTube ID
        # (that's what search returns), so it has to become a real URL
        # before yt-dlp can resolve it — passing the bare ID straight
        # through was silently relying on undefined behavior.
        info = ytdlp_extract(
            f"https://www.youtube.com/watch?v={video_id}",
            {'quiet': True, 'simulate': True},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    metadata = {
        "yt_id": video_id,
        "title": info.get("title", "Unknown Title"),
        "thumbnail": info.get("thumbnail", "")
    }

    with _meta_cache_lock:
        _META_CACHE[video_id] = (now, metadata)
    return jsonify({"success": True, "metadata": metadata})


@app.route("/api/music/stream/<video_id>")
def stream_yt(video_id):
    # This runs exactly when the user clicks 'Play' on an audio element.
    # It fetches a brand new, cryptographically signed streaming URL — this
    # one is deliberately never cached (unlike search/stage above): these
    # URLs expire, so every play has to resolve a fresh one.
    try:
        info = ytdlp_extract(f"https://www.youtube.com/watch?v={video_id}", YTDLP_OPTS)
        fresh_url = info.get("url")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not fresh_url:
        return jsonify({"error": "Audio stream not found"}), 404

    return redirect(fresh_url)


# ── HTTP error handlers ───────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File exceeds maximum allowed size."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)