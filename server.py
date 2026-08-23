from flask import Flask, send_from_directory, jsonify, request, redirect
from flask_sock import Sock
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
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import bisect

# ── tunables ──────────────────────────────────────────────────────────
PAGE_SIZE          = 50    # messages per /history page
DOM_CAP            = 400   # max messages kept in browser DOM

# RAM-first guidance: the warm message index below keeps every message in
# memory by design (this is a cheap, deliberate RAM-for-speed trade). The
# number is a soft ceiling for log-style sanity, NOT enforced — enforcing it
# would break pagination by dropping messages the client still needs to fetch.
RAM_CAP            = 50000
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

# ── yt-dlp stream URL cache & prefetch ──────────────────────────────────
#
# The slow part of playing a shared yt-dlp track was never the search or
# the message send — it's this: resolving a playable googlevideo URL means
# a real yt-dlp extraction (cookie handling + hitting YouTube's own
# endpoints), which is genuinely network-bound and can take a few real
# seconds. Previously this only ever ran the instant someone tapped play,
# so that latency was always felt live, in the moment someone wanted to
# listen.
#
# Fix: the same resolution now ALSO runs in the background the instant a
# ytdlp_audio message is broadcast (see the websocket handler below), so
# by the time anyone actually taps play, the extraction is either already
# done or well under way. Every caller that needs a stream URL — the real
# play request, the search-result preview player, and this background
# warm-up — funnels through the single _resolve_stream_url() below, so
# there is exactly one resolve path, never two implementations that could
# drift apart.
#
# Caching a signed, EXPIRING URL is only safe because the expiry is
# respected, not assumed away: googlevideo's signed URLs carry their own
# `expire=<epoch>` query param, which is parsed and used as the
# authoritative TTL. A message replayed hours later (long past that
# window) simply falls through to a fresh resolve — identical to
# pre-prefetch behavior, never a stale/dead URL handed to a client.
#
# Deliberately NOT done: speculatively prefetching every search result the
# moment a search returns. Sending a message is a strong, near-certain
# signal someone wants to listen; a track merely appearing in a list of
# 10 search results is not — most search results are never played.
# Warming all of them would multiply extraction volume for little real
# benefit, which is exactly the kind of cost a feature has to justify here
# and doesn't.

_STREAM_CACHE      = {}   # video_id -> (url, expires_at_epoch_seconds)
_stream_cache_lock = threading.Lock()

_STREAM_INFLIGHT      = {}   # video_id -> threading.Event, set() when resolved
_stream_inflight_lock = threading.Lock()

# Signed googlevideo URLs carry their own real expiry (parsed below via
# `expire=`) — this is only the fallback for the rare case that param is
# ever missing/unparseable. Kept comfortably under the real-world window
# on purpose: serving a URL believed valid past its actual expiry fails
# loudly for the listener (a dead link), while re-resolving a technically
# still-valid URL a bit early just costs one extra extraction. The safer
# direction to be wrong in is obvious.
STREAM_URL_FALLBACK_TTL = 4 * 60 * 60  # seconds

# Opportunistic sweep trigger, not a hard cap — expired entries here are
# genuine dead weight (they will 403 if ever served), unlike the
# search/meta caches above where a stale-but-present entry is still fine
# to serve. Checked on write rather than run on a timer: no background
# scheduler exists in this codebase and one isn't worth adding just for
# this.
STREAM_CACHE_SWEEP_THRESHOLD = 200

# How long a caller will wait on someone ELSE'S in-progress resolution
# before giving up on them and resolving independently. See
# _resolve_stream_url()'s docstring for why this exists at all.
STREAM_INFLIGHT_WAIT_TIMEOUT = 45  # seconds

# Bounded on purpose: a burst of shares (someone dumping several links in
# a row) queues behind these 2 workers rather than spawning one thread per
# share. The work itself is network-bound (waiting on sockets, not
# spinning the CPU), so 2 concurrent resolutions is cheap to hold even on
# modest hardware — this just caps how many can be in flight AT ONCE.
_ytdlp_prefetch_pool = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="ytdlp-prefetch",
)


def _parse_expire_param(url):
    """
    Pull the `expire=<epoch>` query param googlevideo signs into its own
    stream URLs, when present. This is the authoritative expiry — far
    more precise than any guessed TTL, and self-correcting if Google ever
    changes the real window, since nothing here hardcodes what that
    window is. Returns None if the param is absent or unparseable, so the
    caller falls back to STREAM_URL_FALLBACK_TTL instead of trusting a
    made-up number as if it were real.
    """
    try:
        query  = urllib.parse.urlparse(url).query
        expire = urllib.parse.parse_qs(query).get("expire", [None])[0]
        return int(expire) if expire else None
    except (ValueError, TypeError):
        return None


def _sweep_stream_cache(now):
    """Drop expired entries. Caller already holds _stream_cache_lock."""
    dead = [vid for vid, (_, exp) in _STREAM_CACHE.items() if exp <= now]
    for vid in dead:
        del _STREAM_CACHE[vid]


def _resolve_stream_url(video_id):
    """
    Return a playable googlevideo URL for `video_id`, resolving via yt-dlp
    only when nothing usable is already cached. See the module comment
    above this section for why every stream-URL caller shares this one
    function instead of each doing its own extraction.

    Concurrency: resolutions for the SAME video_id are deduplicated via
    _STREAM_INFLIGHT. If one is already in progress — most commonly, the
    background prefetch started a moment before someone actually tapped
    play — this call waits on THAT resolution instead of starting a
    redundant second extraction. Two concurrent extractions of one video
    is pure waste (2x cookie handling, 2x network round-trip) for zero
    benefit, since both would land on an equally-fresh URL.

    A waiter that times out (STREAM_INFLIGHT_WAIT_TIMEOUT) does not wait
    forever on a resolver that might be wedged: it takes over and resolves
    independently instead. In the rare case several waiters time out
    around the same moment, more than one may end up resolving in
    parallel — accepted as-is, the same way this codebase accepts other
    narrow, low-probability duplicate-work edges elsewhere (see
    presence_state.py's accepted write-race gap) rather than adding
    machinery to close a gap this unlikely. The alternative — silently
    waiting forever on a resolver that never finishes — is the worse
    failure mode: it would permanently lock out every future play attempt
    for that video until the process restarts.
    """
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
            # They finished but left nothing usable (their attempt
            # failed) — fall through and resolve independently below,
            # same as if we'd simply timed out waiting on them.
        with _stream_inflight_lock:
            _STREAM_INFLIGHT[video_id] = my_event

    try:
        info = ytdlp_extract(f"https://www.youtube.com/watch?v={video_id}", YTDLP_OPTS)
        fresh_url = info.get("url")
        if not fresh_url:
            raise RuntimeError("yt-dlp returned no stream URL")

        expires_at = _parse_expire_param(fresh_url)
        # Small safety margin so a URL is never handed out at the very
        # edge of its real expiry window.
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
    """
    Background warm-up entry point, submitted to _ytdlp_prefetch_pool the
    instant a ytdlp_audio message is broadcast (see the websocket
    handler). Best-effort only: nobody is waiting on this call's result,
    so a failure here is swallowed rather than surfaced anywhere — the
    eventual real play request just resolves fresh instead, exactly like
    every play did before this feature existed. It must never let an
    exception escape into the executor silently either; logging it here
    keeps a real, repeated failure visible without anyone needing to
    actively watch for it.
    """
    try:
        _resolve_stream_url(video_id)
    except Exception as e:
        print(f"[ytdlp-prefetch] {video_id} failed (non-fatal): {e}")


def _maybe_prefetch_ytdlp_audio(video_id):
    """Fire-and-forget trigger, called from the websocket handler the
    moment a ytdlp_audio message is persisted/broadcast. No-ops silently
    on a malformed message with no videoId rather than raising into the
    websocket loop over what would just be missing warm-up, not a real
    send failure."""
    if video_id:
        _ytdlp_prefetch_pool.submit(_prefetch_ytdlp_audio, video_id)


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
    if request.path in ("/static/index.css", "/static/index.js"):
        # [FIX] index.css/index.js are the two files under active
        # development and the ones a stale copy visibly breaks (wrong
        # borders, missing icons). no-store is stronger than no-cache:
        # the browser is told not to keep a reusable copy of the body at
        # all, so there's no local entry left to (mis)revalidate against —
        # every load is a full network fetch, full stop. Left off the rest
        # of /static/ (icons, uploads, sfx, etc.) since those change rarely
        # and the no-cache + ETag revalidation below is enough for them.
        response.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
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

def _msg_id_to_path(msg_id):
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(msg_id or secrets.token_hex(8)))
    return os.path.join("messages", f"{safe_id}.json")


# ── warm in-memory message index ──────────────────────────────────────
# The plan's #1 server win: stop doing a full directory scan + parse + sort
# on EVERY /history request. We keep the entire message set in RAM (the
# stated RAM-for-speed trade — a few hundred KB even for huge histories),
# maintained incrementally on every write, and revalidated cheaply against
# the directory mtime so externally-synced files (Syncthing) can't leave us
# serving a stale view.
#
#   _messages       — list of message dicts, ascending by timestamp string
#   _ts_keys        — parallel list of `timestamp` strings for O(log n) bisect
#   _messages_by_id — id -> message dict
#
# All access goes through _index_lock (an RLock so the helpers can nest).
_messages       = []
_ts_keys        = []
_messages_by_id = {}
_index_lock     = threading.RLock()
_index_loaded   = False
_dir_mtime_ns   = 0


def _load_index_locked():
    """Full cold-start read. Exactly one directory scan per process (plus one
    more each time the directory mtime tells us an external change landed)."""
    global _messages, _ts_keys, _messages_by_id
    msgs = []
    try:
        for fname in os.listdir("messages"):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join("messages", fname), "r", encoding="utf-8") as f:
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
    """Cheap call-site: (re)build the index only if cold or externally stale."""
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
    """Insert/replace a message in the warm index, keeping _ts_keys sorted."""
    mid = msg.get("id")
    if not mid:
        return
    with _index_lock:
        if mid in _messages_by_id:
            _messages[:] = [m for m in _messages if m.get("id") != mid]
            _ts_keys[:]  = [m.get("timestamp", "") for m in _messages]
        _messages_by_id[mid] = msg
        ts = msg.get("timestamp", "")
        # Fast path: live messages are (almost) always the newest — O(1) append.
        if not _ts_keys or _ts_keys[-1] <= ts:
            _messages.append(msg)
            _ts_keys.append(ts)
        else:
            i = bisect.bisect_left(_ts_keys, ts)
            _messages.insert(i, msg)
            _ts_keys.insert(i, ts)


def _write_message(msg):
    """Write a single message as its own JSON file and keep the warm index
    current in the same pass."""
    global _dir_mtime_ns
    fpath = _msg_id_to_path(msg.get("id"))
    with _file_lock:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(msg, f)
    # Our own writes deliberately bypass dir-mtime revalidation: the index is
    # updated to match the moment the file is committed.
    _index_upsert(msg)
    try:
        _dir_mtime_ns = os.stat("messages").st_mtime_ns
    except OSError:
        pass


def _read_message_by_id(msg_id):
    """Read a single message by id from the warm index, falling back to disk
    (and auto-absorbing the file into the index) when a Syncthing/out-of-band
    file landed without a directory-scan yet. Never raises."""
    if not msg_id:
        return None
    _ensure_index()
    with _index_lock:
        m = _messages_by_id.get(msg_id)
        if m is not None:
            return m
    try:
        with open(_msg_id_to_path(msg_id), "r", encoding="utf-8") as f:
            m = json.load(f)
        if m and m.get("id") == msg_id:
            _index_upsert(m)
            return m
    except Exception:
        pass
    return None


EDIT_WINDOW_SECONDS = 15 * 60  # requested range was 15-20 min; 15 is the default


def _any_other_message_references_file(filename, exclude_id):
    """
    True if some message OTHER than exclude_id still points at this exact
    uploaded filename.

    Needed because upload dedup (see /upload) means multiple messages can
    legitimately share one physical file — e.g. the same gif sent 3 times
    now writes 1 file backing 3 messages. Without this check, deleting any
    ONE of those messages would remove the file out from under the other
    two, silently 404-ing an image/gif/video that's still visible in
    someone else's un-deleted message. Scans messages/ directly rather
    than trusting any cached count, same "don't guess" discipline as the
    rest of this codebase's disk-state handling.
    """
    _ensure_index()
    with _index_lock:
        for other in _messages:
            if str(other.get("id")) == str(exclude_id):
                continue
            if (other.get("type") == "file"
                    and not other.get("deleted")
                    and other.get("filename") == filename):
                return True
    return False


def _delete_uploaded_file_for(msg):
    """Best-effort removal of the underlying uploaded file when a 'file'
    message is deleted. Never fatal — an already-gone file, or a filename
    that's actually a remote URL (defensive; the current upload flow
    always uploads first, so this shouldn't happen), just means there's
    nothing local left to clean up.

    Dedup-safe: only actually removes the file from disk when no other
    live message still references it (see _any_other_message_references_file
    above) — otherwise this would be a data-loss bug for every message
    still sharing a deduped file with the one being deleted.
    """
    if msg.get("type") != "file":
        return
    filename = msg.get("filename") or ""
    if not filename or filename.startswith("http"):
        return
    if _any_other_message_references_file(filename, exclude_id=msg.get("id")):
        return
    fpath = os.path.join(UPLOAD_FOLDER, filename)
    try:
        if os.path.isfile(fpath):
            os.remove(fpath)
    except OSError:
        pass

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
    """Answer a /history page from the warm in-memory index via bisect —
    O(log n) to locate the boundary, O(limit) to copy the slice. No directory
    scan, no per-file JSON parse, no full sort on every request.

    (Syncthing/out-of-band files are handled by _ensure_index's cheap mtime
    revalidation: when the directory mtime changes, we reload once, then serve
    from RAM again.)
    """
    _ensure_index()
    with _index_lock:
        msgs = _messages
        keys = _ts_keys
        if before:
            i = bisect.bisect_left(keys, before)
            return msgs[:i][-limit:]
        if after:
            # Earliest N *after* the cutoff (ascending), not the latest overall —
            # this fills the gap forward from where the client left off instead
            # of jumping straight to the present.
            i = bisect.bisect_right(keys, after)
            return msgs[i:i + limit]
        return msgs[-limit:]

# Warm the index once at boot (runs at import under both gunicorn and the
# threaded __main__ server), so the very first /history is already a RAM hit
# and never pays the cold full-scan+parse+sort cost.
try:
    _ensure_index()
    print(f"[startup] message index warmed: {len(_messages)} messages in RAM")
except Exception as _e:
    print(f"[startup] message index warm-up skipped: {_e}")

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

CONTENT_HASH_LEN = 12  # hex chars — plenty of collision safety for a
                        # few-thousand-file friend-group history, short
                        # enough to keep filenames readable


def _content_hash(file_storage, chunk_size=1024 * 1024):
    """
    Hash an uploaded file's bytes, streamed in chunks so a 100MB video
    doesn't get pulled into memory whole just to name it.

    Leaves file_storage's stream positioned at 0 afterward — .save()
    (or a second read) still works normally after this returns.
    """
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cleaned_base, extension = _safe_upload_name(file.filename)

    # Content-addressed filename: identical bytes always hash to the same
    # name, so re-sending a gif/photo/song reuses the file already on disk
    # instead of writing (and later Syncthing-replicating to every peer) a
    # second full copy of something byte-for-byte identical to one we
    # already have.
    content_hash = _content_hash(file)
    filename = f"{content_hash}_{cleaned_base}{extension}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
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

            if msg_type == "delete":
                target_id = msg.get("id")
                target = _read_message_by_id(target_id) if target_id else None
                if target is None:
                    # Unknown id — tell the sender so a stale/racing client
                    # (or a genuinely dropped write) doesn't just hang.
                    _safe_send(ws, json.dumps({
                        "type": "delete_result", "id": target_id,
                        "ok": False, "reason": "not_found",
                    }))
                    continue
                if target.get("user") != username:
                    _safe_send(ws, json.dumps({
                        "type": "delete_result", "id": target_id,
                        "ok": False, "reason": "not_owner",
                    }))
                    continue
                if target.get("deleted"):
                    # Idempotent: already deleted. Re-broadcast the delete so a
                    # client that missed the first broadcast still converges,
                    # and confirm success to the sender.
                    _broadcast(json.dumps({"type": "delete", "id": target_id}))
                    _safe_send(ws, json.dumps({
                        "type": "delete_result", "id": target_id,
                        "ok": True, "already": True,
                    }))
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
                # Preserve the reply thread so "Message deleted" never breaks
                # a reply-quote pointing at this message from another bubble.
                if target.get("replyTo"):
                    tombstone["replyTo"] = target["replyTo"]
                _write_message(tombstone)
                _broadcast(json.dumps({"type": "delete", "id": target_id}))
                _safe_send(ws, json.dumps({
                    "type": "delete_result", "id": target_id, "ok": True,
                }))
                continue

            if msg_type == "edit":
                target_id = msg.get("id")
                new_text = (msg.get("text") or "").strip()
                target = _read_message_by_id(target_id) if target_id else None

                if target is None or target.get("deleted") or target.get("user") != username:
                    _safe_send(ws, json.dumps({
                        "type": "edit_result", "id": target_id,
                        "ok": False, "reason": "rejected",
                    }))
                    continue

                # Editable content types only — stickers/YouTube/ytdlp shares
                # have no free-text field the UI ever lets you touch.
                msg_kind = target.get("type")
                if target.get("isSticker") or msg_kind in ("youtube", "ytdlp_audio"):
                    continue

                field = "caption" if msg_kind == "file" else "text"
                if not new_text and field == "text":
                    continue  # empty text isn't a valid edit — delete instead

                try:
                    sent_at = datetime.fromisoformat(target["timestamp"])
                except (KeyError, ValueError, TypeError):
                    continue
                if (datetime.now() - sent_at).total_seconds() > EDIT_WINDOW_SECONDS:
                    _safe_send(ws, json.dumps({
                        "type": "edit_rejected", "id": target_id, "reason": "expired",
                    }))
                    continue

                if target.get(field, "") == new_text:
                    continue  # no-op edit — nothing changed, nothing to broadcast

                target[field] = new_text
                target["edited"] = True
                _write_message(target)
                _broadcast(json.dumps({
                    "type": "edit", "id": target_id, "field": field, "value": new_text,
                }))
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
            elif msg_type == "ytdlp_audio":
                # Start resolving the real stream URL in the background
                # right now, instead of waiting for someone to tap play.
                # Fire-and-forget: see _maybe_prefetch_ytdlp_audio() and
                # _resolve_stream_url() above for the full reasoning.
                _maybe_prefetch_ytdlp_audio(msg.get("videoId"))
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
    # This is where a play tap (or a search-result preview) lands. As of
    # the prefetch feature above, this is USUALLY a near-instant cache
    # hit rather than a live extraction — see _resolve_stream_url()'s
    # docstring for why caching a signed, expiring URL is safe here: the
    # URL's own expiry is parsed and respected, never assumed away.
    try:
        fresh_url = _resolve_stream_url(video_id)
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
    # Under the front-door architecture, server.py runs as a SUPERVISED
    # CHILD of front_door.py and binds LOOPBACK ONLY. Nothing outside
    # this machine may reach the app server directly — the front door
    # (on port 5000, tailnet-facing) is the only path in.
    #
    # threaded=True is retained deliberately: flask_sock's WebSocket
    # transport documents Werkzeug's threaded dev server as one of the
    # two servers (alongside gunicorn+gevent) that support the
    # socket-hijack path WS upgrade needs. This path runs when gunicorn
    # isn't available (Windows, or POSIX without gunicorn installed);
    # front_door.py's _spawn_child_command() picks between gunicorn
    # (bound to 127.0.0.1:5001) and this __main__ block accordingly.
    app.run(host="127.0.0.1", port=5001, threaded=True)