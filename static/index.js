// ─────────────────────────────────────────────────────────────────────────────
//  ChatBucket — client
//  Quality-pass revision. Behaviour is preserved except where noted in comments
//  tagged [FIX] (correctness), [SEC] (security), [PERF] (performance), or
//  [REFACTOR] (structure/readability).
// ─────────────────────────────────────────────────────────────────────────────

// ── DOM helpers ───────────────────────────────────────────────────────────────
// [REFACTOR] Centralised element lookups + safe HTML escaping.
// A single $ / $$ shim removes the endless `document.getElementById(...)`
// repetition and makes the code far easier to skim.
const $  = (id) => document.getElementById(id);
const $$ = (sel, root = document) => root.querySelector(sel);

// [SEC] All user-supplied strings that ever land in `innerHTML` must go through
// this. Previously `msg.user`, `msg.text`, `msg.filename`, `msg.replyTo.*` were
// interpolated raw into template strings — a straightforward XSS vector.
function escapeHTML(str) {
    if (str == null) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// [SEC] URLs interpolated into src/href attributes need extra care — reject
// anything that isn't http(s) or a same-origin relative path so a hostile
// filename can't smuggle `javascript:` or `data:` URLs into a live element.
function safeMediaURL(u) {
    if (!u) return "";
    const s = String(u);
    if (/^https?:\/\//i.test(s)) return s;
    if (s.startsWith("/") && !s.startsWith("//")) return s;
    return "";
}

// ── state ─────────────────────────────────────────────────────────────────────
let username        = localStorage.getItem("username");
let socket          = null;
let attachmentFiles = [];
let notifySound     = null;
let replyingTo      = null;   // { id, user, text, mediaSrc, mediaTag }
let dragCounter     = 0;

// pagination / DOM-cap state
let oldestTimestamp  = null;
let isLoadingOlder   = false;
let noMoreOlder      = false;
let newestTimestamp  = null;
let isLoadingNewer   = false;
let noMoreNewer      = true;   // true = DOM already reaches the live tail
const PAGE_SIZE      = 50;
// [PERF] Reduced from 400 → 150. Fewer DOM nodes = less layout/paint cost.
const DOM_CAP        = 150;

let lastRenderedUser  = null;
// [DEAD-CODE-REMOVED] `firstRenderedUser` was only ever assigned to, never read.
// It was a leftover from an earlier user-grouping design that got refactored
// out but the write-only variable stayed. Removed here so future readers
// don't spend time tracing what it means.
// [FIX] Single source of truth for date-separator bookkeeping, shared by
// loadHistory, loadOlderMessages, loadNewerMessages, and appendMessage.
// These used to each maintain their own local copy of "what date did we
// last render" (or, in appendMessage's case, no such tracking at all) —
// that's what let the four paths drift out of sync with each other and
// produce duplicate/missing/stale date separators.
let lastRenderedDateKey  = null; // calendar day of the newest (bottom-most) rendered message
let firstRenderedDateKey = null; // calendar day of the oldest (top-most) rendered message
let imgObserver     = null;

// GIF System Persistent Trackers
let currentActiveFolder = localStorage.getItem("last_gif_folder") || "general";
let currentGifTab       = "local";
let localGifCache       = {};

// ── Music panel state ────────────────────────────────────────────────────
// Three tabs: local (sfx folders, mirrors GIF/sticker local mode exactly),
// youtube (IFrame embed — full library, video+ads), ytdlp (audio-only via
// this project's own /api/music/* backend — no ads, fewer results, slower).
let currentMusicTab        = "local";
let currentActiveMusicFolder = localStorage.getItem("last_music_folder") || "general";
let localMusicCache        = {};

// YouTube Data API v3 — used ONLY for search (official, sanctioned API,
// distinct from the yt-dlp tab). Client-side like the existing Giphy key
// above — same trust model: visible to anyone with this codebase, fine
// for a 3-person trusted group, not fine to treat as a real secret.
const YOUTUBE_API_KEY = "AIzaSyCPz4ygint6Nj5jBTm2oC8BNOF7EGkk360";

// [PERF] Track currently-playing videos in a Set so we avoid
// a full querySelectorAll on every play event.
const _playingVideos = new Set();

// [QoL] Exponential backoff: reconnect attempts since last clean connect.
let _reconnectAttempts = 0;
// [QoL] Original page title — preserved so unread badge can be prepended/stripped cleanly.
const _originalTitle = document.title;

// ── presence & unread state ──────────────────────────────────────────────────
// Per-user typing timers: { username → setTimeout id }
const typingTimers    = {};
// Debounce guard so we don't spam the server with typing events on every keystroke.
let _typingDebounce   = null;
// Known users: { username: { online: bool, typing: bool } }
const _knownUsers     = {};

// ── UNREAD SUBSYSTEM ─────────────────────────────────────────────────────────
// [REFACTOR] The old design mixed together three separate concerns:
//   1. A tab-title badge counter (`unreadCount`).
//   2. A "scroll to bottom" button counter (also `unreadCount`).
//   3. A per-user "first unread message" divider tied to server "left" events.
// They shared state, updated inconsistently, and never guarded against the
// user's own messages. The redesign gives each concern a single owner:
//
//   `Unread.lastReadTs`     — client-side high-water mark (ms epoch).
//                             Persisted in localStorage per-username so it
//                             survives refresh / reconnect / DOM eviction.
//   `Unread.pendingBelow`   — number of messages arrived while the user was
//                             scrolled up (drives the scroll-down button and
//                             the tab title badge).
//   `Unread.boundaryTs`     — the moment we froze at the start of the session;
//                             used exactly once, to place the "N unread" divider
//                             on first render. Cleared after use.
//
// Invariants (enforced in one place — `Unread.markMessage()`):
//   • A message counts as unread only if `msg.user !== username`.
//   • A message counts as unread only if it is newer than `lastReadTs`.
//   • System messages never count as unread.
const Unread = {
    lastReadTs:    0,        // ms since epoch of the newest message the user has seen
    boundaryTs:    0,        // frozen snapshot of lastReadTs at session start
    pendingBelow:  0,        // messages arrived while the user is scrolled up
    _dividerPlaced: false,   // guard so we only auto-scroll to divider once per session

    storageKey() {
        return "chatbucket:lastReadTs:" + (username || "_");
    },

    load() {
        const raw = localStorage.getItem(this.storageKey());
        const n = raw ? parseInt(raw, 10) : 0;
        this.lastReadTs = Number.isFinite(n) ? n : 0;
        this.boundaryTs = this.lastReadTs;  // freeze for divider placement
        this._dividerPlaced = false;
        this.pendingBelow = 0;
    },

    save() {
        try { localStorage.setItem(this.storageKey(), String(this.lastReadTs)); }
        catch (_) { /* private-mode / quota — non-fatal */ }
    },

    // Convert a message timestamp string to a comparable number.
    tsOf(msg) {
        if (!msg || !msg.timestamp) return 0;
        const t = Date.parse(msg.timestamp);
        return Number.isFinite(t) ? t : 0;
    },

    // [FIX] Central place that decides "is this unread FOR ME?"
    // Fixes both the "own messages marked unread" bug and race conditions
    // where duplicate/late-arriving messages inflated the count.
    isUnread(msg) {
        if (!msg || msg.type === "system") return false;
        if (msg.user === username) return false;
        return this.tsOf(msg) > this.lastReadTs;
    },

    // Called when a live message arrives while user is scrolled up.
    countPending(msg) {
        if (!this.isUnread(msg)) return;
        this.pendingBelow++;
        showScrollBtn();
        updateTabTitle();
    },

    // Called when the user is confirmed to have seen everything up to `ts`
    // (they scrolled to the bottom, focused the window near the bottom, or
    // sent a message themselves).
    markSeenUpTo(ts) {
        if (ts && ts > this.lastReadTs) {
            this.lastReadTs = ts;
            this.save();
        }
        this.pendingBelow = 0;
        hideScrollBtn();
        updateTabTitle();
    },

    // Called when the user sends their own message — advance the high-water
    // mark so their own message is not treated as unread on next reload.
    markOwnMessage() {
        const now = Date.now();
        if (now > this.lastReadTs) {
            this.lastReadTs = now;
            this.save();
        }
    },
};

// [QoL] Media-viewer zoom level, pan offset (scroll/pinch to zoom, drag/2-finger to pan).
let _viewerScale = 1;
let _viewerTransX = 0;
let _viewerTransY = 0;
// Suppresses the close-on-click that fires after a drag-to-pan gesture.
let _viewerDragOccurred = false;

// ── boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    if (username) {
        Unread.load();
        showChat();
    }
});

// ── helpers ───────────────────────────────────────────────────────────────────
function playNotificationSound() {
    if (!notifySound) return;
    notifySound.currentTime = 0;
    notifySound.play().catch(() => {});
}

// [SEC] `text` is escaped *before* the URL-to-anchor pass. Splitting the string
// on the regex and building an array of DOM-string fragments guarantees we
// never re-inject unescaped user content, even for edge-case URLs.
function formatMessage(text) {
    if (!text) return "";
    const urlRegex = /(https?:\/\/[^\s<]+)/g;
    let out = "";
    let last = 0;
    let m;
    while ((m = urlRegex.exec(text)) !== null) {
        out += escapeHTML(text.slice(last, m.index));
        const safeUrl = escapeHTML(m[0]);
        out += `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="chat-link">${safeUrl}</a>`;
        last = m.index + m[0].length;
    }
    out += escapeHTML(text.slice(last));
    return out;
}

function userColor(name) {
    const colors = ["#58a6ff","#3fb950","#d29922","#bc8cff","#ff7b72","#f78166"];
    let hash = 0;
    for (let i = 0; i < name.length; i++) hash += name.charCodeAt(i);
    return colors[hash % colors.length];
}

function genId() {
    return Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 9);
}

function isNearBottom(el, threshold = 120) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
}
// Snapshot "at bottom" before a layout change that resizes #messages
// (e.g. a docked panel opening/closing), then re-pin to bottom after —
// so panel open/close never itself alters scroll position or the
// user's at-bottom state, matching behavior when the panel is closed.
function preserveScrollAcrossResize(el, mutate) {
    const wasAtBottom = isNearBottom(el);
    mutate();
    if (wasAtBottom) {
        requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    }
}

// [FIX] Re-applies the correct scroll target as each still-loading image/video in
// the just-rendered page settles. Needed because the initial scroll-to-bottom
// (or scroll-to-divider) in loadHistory() is computed synchronously, before
// lazy images have loaded and grown the layout — landing the view above
// where it should be by however much height was still missing at that instant.
//
// PREVIOUS BUG (scroll-lock symptom): this used to call
// `divider.scrollIntoView({block:"start"})` on EVERY image load event forever
// after render, which meant that whenever the user tried to scroll away from
// the divider, the next lazy image finishing loading would yank them right
// back. It felt like the divider was "blocking" scroll.
//
// FIX: two changes.
//   1. Time-bound the settle window (SETTLE_MS). After the window closes,
//      lazy images continue to load but no longer trigger scrollIntoView.
//   2. Never re-scroll to the divider on subsequent settle events — only on
//      the FIRST settle. After that, growing content above the viewport is
//      absorbed by the scroll-compensation path already living in the lazy
//      image observer (which nudges scrollTop to keep the visible content
//      steady rather than yanking to a target).
const SETTLE_MS = 2500;
function settleScrollAfterRender(messagesEl) {
    const startedAt = performance.now();
    let dividerHandled = false;
    const rescroll = () => {
        // Time-bounded: after SETTLE_MS, do nothing. Layout can still shift
        // as later images load, but the scroll compensation observer handles
        // that without yanking to a fixed target.
        if (performance.now() - startedAt > SETTLE_MS) return;

        const divider = $("unread-divider");
        if (divider && !dividerHandled) {
            divider.scrollIntoView({ block: "start" });
            dividerHandled = true;   // one-shot: never re-scroll to divider again
            return;
        }
        // No divider (or already handled) — only re-pin to bottom if we were
        // already effectively there. Threshold widened to 400 so a user who
        // tapped once to scroll a bit doesn't get dragged back.
        if (!divider && isNearBottom(messagesEl, 400)) {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    };

    // Only attach listeners to media that hasn't loaded yet. Static already-
    // complete images used to get listeners too — harmless but wasteful.
    messagesEl.querySelectorAll("img").forEach(img => {
        if (img.dataset.src || !img.complete) {
            img.addEventListener("load",  rescroll, { once: true });
            img.addEventListener("error", rescroll, { once: true });
        }
    });
    messagesEl.querySelectorAll("video").forEach(vid => {
        if (vid.readyState < 1) {
            vid.addEventListener("loadedmetadata", rescroll, { once: true });
        }
    });
}

// ── QoL helpers ───────────────────────────────────────────────────────────────

// [REBOOT] All runtime-injected styles were moved into style.css so the design
// tokens (--surface-*, --border-*, etc.) apply uniformly. Kept as a no-op
// so anything that still calls injectQoLStyles() (external tooling, other
// scripts) stays a valid symbol.
function injectQoLStyles() { /* moved to style.css */ }

// [REFACTOR] Replaces inline `onclick="copyMsgText(this)"` — the delegated
// click listener in showChat() now dispatches copy actions from data-action.
// [FIX] navigator.clipboard requires a secure context — HTTPS, or
// http://localhost/127.0.0.1 specifically. ChatBucket is currently served
// over plain http:// on the tailnet (see arbitration.py's SCHEME note: TLS
// via `tailscale cert` isn't wired into server.py yet), so on every real
// bookmark anyone actually uses (§5 of the architecture doc) navigator.
// clipboard is undefined. The old code called .writeText straight off
// that undefined, which throws synchronously — before any Promise exists
// — so the chained .catch() never even attached. That's the actual bug:
// every click threw silently, nothing ever happened.
//
// Verified against 5 scenarios (http deploy, http + execCommand
// unsupported, future https, https + permission-denied, focus
// restoration) in a jsdom harness before shipping — caught one real bug
// in the process, see legacyCopyToClipboard's [FIX] comment below.
async function copyTextToClipboard(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (_) {
            // e.g. permission denied — fall through and try the legacy path too
        }
    }
    return legacyCopyToClipboard(text);
}

// Legacy fallback: select an offscreen textarea's contents and run the
// browser's built-in copy command. Deprecated but still implemented
// everywhere that matters here (Win10/11 Chromium/Firefox, Arch) — and
// unlike navigator.clipboard, it has no secure-context requirement, which
// is exactly why it's the path that actually runs on this project's
// current http:// deployment. Must execute synchronously inside the click
// that triggered it — execCommand requires an active user gesture.
function legacyCopyToClipboard(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    // In-flow (not display:none — some browsers refuse to .select() a
    // display:none node) but pushed off-screen and non-interactive.
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.left = "-9999px";
    ta.style.opacity = "0";
    ta.setAttribute("readonly", "");
    document.body.appendChild(ta);

    const priorFocus = document.activeElement;
    ta.focus();   // [FIX] .select() alone does NOT reliably move focus —
                  // verified empirically in a jsdom harness, not assumed —
                  // and execCommand('copy') copies the *focused* control's
                  // selection. Without this the copy can silently grab
                  // whatever was focused before instead of `ta`.
    ta.select();
    ta.setSelectionRange(0, text.length); // belt-and-suspenders for older WebKit

    let ok = false;
    try {
        ok = document.execCommand("copy");
    } catch (_) {
        ok = false;
    }

    document.body.removeChild(ta);
    // Restore focus to wherever it was — typically the message input — so
    // copying doesn't steal focus away from an in-progress draft.
    if (priorFocus && typeof priorFocus.focus === "function") priorFocus.focus();

    return ok;
}

// [FIX] Split out so the "copied" state stays visible for its full window
// even if the pointer leaves the bubble mid-flight — .copy-btn is
// opacity:0 outside :hover, so without the .copied override the checkmark
// could vanish the instant the mouse moves, before it's been seen at all.
// WeakMap-keyed so a rapid double-copy of the SAME message extends the
// window instead of two timers racing to revert it early; same pattern as
// AnimatedMedia's posterCanvas WeakMap elsewhere in this file.
const _copiedIndicatorTimers = new WeakMap();
function showCopiedIndicator(btn) {
    const iconEl = btn.querySelector(".icon");
    if (!iconEl) return;

    clearTimeout(_copiedIndicatorTimers.get(btn));

    btn.classList.add("copied");
    btn.setAttribute("aria-label", "Copied!");
    iconEl.classList.remove("copy");
    iconEl.classList.add("check");

    const t = setTimeout(() => {
        btn.classList.remove("copied");
        btn.setAttribute("aria-label", "Copy message");
        iconEl.classList.remove("check");
        iconEl.classList.add("copy");
    }, 1500);
    _copiedIndicatorTimers.set(btn, t);
}

// [REFACTOR] Replaces inline `onclick="copyMsgText(this)"` — the delegated
// click listener in showChat() dispatches copy actions from .copy-btn.
function copyMessageText(msgEl) {
    const text = msgEl?.dataset.previewText || "";
    if (!text) return;
    const btn = msgEl.querySelector(".copy-btn");

    copyTextToClipboard(text).then(ok => {
        if (!btn) return;
        if (ok) showCopiedIndicator(btn);
        else showToast("Couldn't copy message to clipboard.");
    });
}
// Update <title> to reflect unread count; strip badge when count is zero.
function updateTabTitle() {
    document.title = Unread.pendingBelow > 0
        ? `(${Unread.pendingBelow}) ${_originalTitle}`
        : _originalTitle;
}

// Prompt for desktop notification permission (no-op if already decided).
function requestNotifPermission() {
    if ("Notification" in window && Notification.permission === "default") {
        Notification.requestPermission();
    }
}

// Fire a desktop notification for an incoming message.
// Uses tag:"chatbucket" so notifications replace each other instead of stacking.
function showDesktopNotif(msg) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    // [REBOOT] These emoji stay: they sit inside the OS-level desktop notification
    // body — rendered by the operating system (native emoji font), not by our app UI.
    // The reboot requirement targets in-app icon spots; this string never touches the DOM.
    const body = (msg.text
        || (msg.filename ? "📎 " + msg.filename.replace(/^\d{8}_\d{6}_/, "") : "📷 Media")
    ).slice(0, 120);
    const n = new Notification(msg.user, { body, tag: "chatbucket", silent: true });
    n.onclick = () => { window.focus(); n.close(); };
    setTimeout(() => n.close(), 5000);
}

function joinChat() {
    const name = $("username").value.trim();
    if (!name) return;
    localStorage.setItem("username", name);
    username = name;
    Unread.load();
    showChat();
}

function showToast(message) {
    let container = $("toast-container");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);

    // Force reflow to guarantee the transition triggers.
    void toast.offsetHeight;
    toast.classList.add("show");

    setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 200);
    }, 3000);
}

/* ── yt-dlp resolving indicator ────────────────────────────────────────── */
/* Tiny non-intrusive status filament:
   - shows only while /api/music/stream/... is resolving
   - works for ytdlp preview playback and sent yt-dlp audio bubbles
   - no layout shift, no modal noise, no annoying center-screen sermon
*/
/* ── yt-dlp resolving indicator ────────────────────────────────────────── */
let _ytdlpStatusHideTimer  = null;
let _ytdlpFailureHideTimer = null;
// The <audio> element the pill is currently representing. Needed because
// this is a single shared pill serving potentially many audio elements
// (sent bubbles, and any number of search-preview cards) — without this,
// an OLD element's late pause/ended/error event (fired asynchronously,
// e.g. because a newer preview just interrupted it) could hide or
// overwrite the pill's state for whichever element is now actually
// relevant. Every show/hide call below checks this before acting.
let _ytdlpStatusOwner = null;

function ensureYtdlpStatusPill() {
    let el = $("ytdlp-status-pill");
    if (el) return el;

    if (!$("ytdlp-status-pill-style")) {
        const style = document.createElement("style");
        style.id = "ytdlp-status-pill-style";
        style.textContent = `
#ytdlp-status-pill {
    position: fixed;
    right: 14px;
    bottom: calc(84px + env(safe-area-inset-bottom, 0px));
    z-index: 12000;
    pointer-events: none;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 12px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.10);
    background: rgba(8,8,8,0.74);
    backdrop-filter: blur(10px);
    box-shadow: 0 10px 30px rgba(0,0,0,0.45);
    color: var(--text);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    opacity: 0;
    transform: translateY(8px) scale(0.98);
    transition: opacity var(--dur-med) ease, transform var(--dur-med) ease;
}
#ytdlp-status-pill.show {
    opacity: 1;
    transform: translateY(0) scale(1);
}
#ytdlp-status-pill .ytdlp-status-spinner {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    border: 2px solid var(--border-2);
    border-top-color: var(--text);
    animation: ytdlp-status-spin 0.75s linear infinite;
    flex: 0 0 auto;
}
#ytdlp-status-pill .ytdlp-status-text {
    white-space: nowrap;
    max-width: 42vw;
    overflow: hidden;
    text-overflow: ellipsis;
}
@keyframes ytdlp-status-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
    #ytdlp-status-pill .ytdlp-status-spinner { animation: none; opacity: 0.75; }
}
/* [FIX] Failure state — a resolve error used to just hide the pill, which
   looked identical to "it loaded fine." This makes "it's not going to
   play" as visible as "it's loading" already was. */
#ytdlp-status-pill.is-error {
    border-color: rgba(255,107,107,0.35);
    background: rgba(26,14,14,0.82);
}
#ytdlp-status-pill.is-error .ytdlp-status-text { color: var(--danger); }
#ytdlp-status-pill.is-error .ytdlp-status-spinner {
    animation: none;
    border: none;
    background: var(--danger);
    -webkit-mask-image: url(/static/icons/warning.svg);
    mask-image: url(/static/icons/warning.svg);
    -webkit-mask-repeat: no-repeat;
    mask-repeat: no-repeat;
    -webkit-mask-position: center;
    mask-position: center;
    -webkit-mask-size: contain;
    mask-size: contain;
}
        `.trim();
        document.head.appendChild(style);
    }

    el = document.createElement("div");
    el.id = "ytdlp-status-pill";
    el.innerHTML = `
        <span class="ytdlp-status-spinner" aria-hidden="true"></span>
        <span class="ytdlp-status-text">Resolving audio…</span>
    `;
    document.body.appendChild(el);
    return el;
}

function showYtdlpStatus(message = "Resolving audio…") {
    const pill = ensureYtdlpStatusPill();
    const text = pill.querySelector(".ytdlp-status-text");
    if (text) text.textContent = message;
    pill.classList.add("show");
}

function hideYtdlpStatus() {
    const pill = $("ytdlp-status-pill");
    if (!pill) return;
    pill.classList.remove("show", "is-error");
    clearTimeout(_ytdlpStatusHideTimer);
    _ytdlpStatusHideTimer = null;
    clearTimeout(_ytdlpFailureHideTimer);
    _ytdlpFailureHideTimer = null;
}

// [FIX] Companion to showYtdlpStatus for the failure path. Reuses the same
// pill (no second UI element to design/place) but swaps in error styling
// and auto-dismisses after a few seconds rather than lingering — long
// enough to register, short enough to stay out of the way.
function showYtdlpFailure(message = "Couldn't load audio") {
    clearTimeout(_ytdlpStatusHideTimer);
    _ytdlpStatusHideTimer = null;
    const pill = ensureYtdlpStatusPill();
    const text = pill.querySelector(".ytdlp-status-text");
    if (text) text.textContent = message;
    pill.classList.add("show", "is-error");
    clearTimeout(_ytdlpFailureHideTimer);
    _ytdlpFailureHideTimer = setTimeout(() => {
        pill.classList.remove("is-error");
        hideYtdlpStatus();
    }, 3000);
}

function scheduleYtdlpStatus(ownerEl, message) {
    _ytdlpStatusOwner = ownerEl;
    clearTimeout(_ytdlpStatusHideTimer);
    _ytdlpStatusHideTimer = setTimeout(() => {
        // Still the relevant element 120ms later? Show. If ownership moved
        // on in the meantime (a newer preview started), this stale show
        // request is simply dropped.
        if (_ytdlpStatusOwner === ownerEl) showYtdlpStatus(message);
    }, 120);
}

// Every hide/failure event from an <audio> element must check it's still
// the element the pill is actually representing before acting — an old,
// interrupted element's pause/ended/error firing late must not clear or
// overwrite a DIFFERENT element's currently-showing status.
function hideYtdlpStatusFor(ownerEl) {
    if (_ytdlpStatusOwner !== ownerEl) return;
    hideYtdlpStatus();
}

function showYtdlpFailureFor(ownerEl) {
    if (_ytdlpStatusOwner !== ownerEl) return;
    showYtdlpFailure();
}

function bindYtdlpAudioIndicator(audioEl) {
    if (!audioEl || audioEl.dataset.ytdlpIndicatorBound === "1") return;
    audioEl.dataset.ytdlpIndicatorBound = "1";

    const label = audioEl.dataset.ytdlpStatusLabel || "Resolving audio…";

    audioEl.addEventListener("play",    () => scheduleYtdlpStatus(audioEl, label));
    audioEl.addEventListener("waiting", () => scheduleYtdlpStatus(audioEl, label));
    audioEl.addEventListener("stalled", () => scheduleYtdlpStatus(audioEl, label));

    audioEl.addEventListener("playing",        () => hideYtdlpStatusFor(audioEl));
    audioEl.addEventListener("canplay",        () => hideYtdlpStatusFor(audioEl));
    audioEl.addEventListener("canplaythrough", () => hideYtdlpStatusFor(audioEl));
    audioEl.addEventListener("ended",          () => hideYtdlpStatusFor(audioEl));
    audioEl.addEventListener("pause",          () => hideYtdlpStatusFor(audioEl));
    audioEl.addEventListener("error",          () => showYtdlpFailureFor(audioEl));
}

function wireYtdlpAudioIndicators(root) {
    (root || document).querySelectorAll("audio[data-ytdlp-status-label]").forEach(bindYtdlpAudioIndicator);
}

// [REFACTOR] Single entry point for both audio-element enhancements a
// rendered message can need — the resolving/failure pill above, and the
// now-playing/exclusivity wiring below (see NowPlaying near the YouTube
// player section). Every render path that can produce a ytdlp_audio or
// local-audio bubble calls this ONE function rather than remembering to
// call two separately, which is exactly how the pagination paths
// (loadOlderMessages/loadNewerMessages) previously ended up silently
// missing the resolving-pill wiring in the first place — they called
// neither. One call site per render path now covers both concerns.
function wireAudioEnhancements(root) {
    wireYtdlpAudioIndicators(root);
    wireNowPlayingAudio(root);
}

// ── scroll-to-bottom button ───────────────────────────────────────────────────
function showScrollBtn() {
    if (Unread.pendingBelow <= 0) { hideScrollBtn(); return; }
    let btn = $("scroll-to-bottom-btn");
    if (!btn) {
        btn = document.createElement("button");
        btn.id = "scroll-to-bottom-btn";
        // [REBOOT] All positioning and look come from style.css — the JS just
        // toggles display and swaps the label.
        btn.addEventListener("click", () => {
            const messagesEl = $("messages");
            messagesEl.scrollTop = messagesEl.scrollHeight;
            Unread.markSeenUpTo(Date.now());
        });
        document.body.appendChild(btn);
    }
    // [REBOOT] SVG arrow icon + label. Count value is a safe number so no escaping needed here,
    // but static class strings are the ONLY dynamic HTML we inject.
    const n = Unread.pendingBelow;
    btn.innerHTML =
        `<span class="icon arrow-down sm"></span>` +
        `<span>${n} new message${n !== 1 ? "s" : ""}</span>`;
    btn.style.display = "flex";
}

function hideScrollBtn() {
    const btn = $("scroll-to-bottom-btn");
    if (btn) btn.style.display = "none";
}

// ── system messages ───────────────────────────────────────────────────────────
function buildSystemMessageEl(msg) {
    const div = document.createElement("div");
    div.className = "system-message";
    div.dataset.msgid = msg.id || "";
    div.dataset.ts    = msg.timestamp || "";
    const verb = msg.event === "joined" ? "has joined" : "has left";
    // textContent is XSS-safe by construction.
    div.textContent = `${msg.user} ${verb}`;
    // Hidden from chat view — kept in DOM in case any feature ever needs to reference it.
    div.style.display = "none";
    return div;
}

// ── unread divider ────────────────────────────────────────────────────────────
// [FIX] Rewritten (again) to eliminate "won't let me scroll away" and "have
// to refresh to make it disappear."
//
// Previous behavior:
//   • Divider was placed once per session via boundaryTs.
//   • It was only removed when the user scrolled PAST it.
//   • On short histories that fit in one viewport, the user had nothing to
//     scroll past — so the divider stuck around forever until refresh.
//   • settleScrollAfterRender used to call scrollIntoView on every subsequent
//     lazy-image load, which yanked the user back whenever they tried to move.
//     (Neutered separately in settleScrollAfterRender — one-shot dividerHandled.)
//
// New behavior — retirement triggers, any one is enough:
//   (a) user scrolls past it → immediate (handled in onMessagesScroll).
//   (b) it has been fully visible in the viewport for UNREAD_VIEWED_MS.
//   (c) UNREAD_VIEWED_MAX_MS elapse since first placement (safety net for
//       tiny histories where it never becomes "fully visible" via IO).
//   (d) window regains focus AND the divider is currently on-screen.
//
// Retirement is a soft fade via .unread-divider-fading; node dropped after
// the transition. `_dividerPlaced` stays true forever after retirement, so
// nothing ever revives it this session.
const UNREAD_VIEWED_MS     = 4000;   // fully-visible "you have clearly seen it" grace
const UNREAD_VIEWED_MAX_MS = 12000;  // absolute cap regardless of visibility
let _unreadRetireTimer  = null;
let _unreadObserver     = null;
let _unreadHardCapTimer = null;

function retireUnreadDivider() {
    const divider = $("unread-divider");
    // Always clean up ambient state even if the node is gone already — stops
    // stray timers/observers from running.
    if (_unreadObserver) { _unreadObserver.disconnect(); _unreadObserver = null; }
    clearTimeout(_unreadRetireTimer);   _unreadRetireTimer  = null;
    clearTimeout(_unreadHardCapTimer);  _unreadHardCapTimer = null;
    Unread._dividerPlaced = true;       // never place again this session
    if (!divider) return;

    divider.classList.add("unread-divider-fading");
    let dropped = false;
    const drop = () => {
        if (dropped) return;
        dropped = true;
        divider.remove();
    };
    // Fallback drop in case transitionend never fires (backgrounded tab, no
    // matching transition property, etc.). One-shot listener + one-shot timer.
    divider.addEventListener("transitionend", drop, { once: true });
    setTimeout(drop, 800);
}

function applyUnreadDivider() {
    // Idempotent — remove any stale divider AND any live observers/timers
    // before (re)placing.
    $("unread-divider")?.remove();
    if (_unreadObserver) { _unreadObserver.disconnect(); _unreadObserver = null; }
    clearTimeout(_unreadRetireTimer);   _unreadRetireTimer  = null;
    clearTimeout(_unreadHardCapTimer);  _unreadHardCapTimer = null;

    // Divider is placed at most once per session.
    if (Unread._dividerPlaced || !Unread.boundaryTs) return;

    const messagesEl = $("messages");
    if (!messagesEl) return;

    // Find the first REGULAR message strictly newer than the boundary that
    // is also NOT authored by the current user. Own messages never count.
    const msgs = messagesEl.querySelectorAll(".message");
    let firstUnread = null;
    let unreadCount = 0;
    for (const el of msgs) {
        const ts = Date.parse(el.dataset.ts || "");
        const user = el.dataset.user || "";
        if (!Number.isFinite(ts) || ts <= Unread.boundaryTs) continue;
        if (user === username) continue;
        if (!firstUnread) firstUnread = el;
        unreadCount++;
    }

    if (!firstUnread || unreadCount === 0) return;

    const divider = document.createElement("div");
    divider.id        = "unread-divider";
    divider.className = "unread-divider";
    divider.innerHTML = `<span>${unreadCount} unread message${unreadCount !== 1 ? "s" : ""}</span>`;
    messagesEl.insertBefore(divider, firstUnread);

    // One-time anchor. The one-shot dividerHandled flag in
    // settleScrollAfterRender means later lazy loads no longer re-scroll here.
    divider.scrollIntoView({ block: "start" });
    Unread._dividerPlaced = true;

    // Retirement path (b): fully visible for a while means the user saw it.
    // IntersectionObserver measures real visibility — works even on tiny
    // chats where the user never scrolls because everything fits at once.
    _unreadObserver = new IntersectionObserver(entries => {
        for (const entry of entries) {
            if (entry.isIntersecting && entry.intersectionRatio >= 0.9) {
                if (_unreadRetireTimer) continue;
                _unreadRetireTimer = setTimeout(retireUnreadDivider, UNREAD_VIEWED_MS);
            } else {
                // Left the viewport before the grace timer fired — cancel it.
                // Scroll-past retirement (path a) is immediate, handled below.
                clearTimeout(_unreadRetireTimer);
                _unreadRetireTimer = null;
            }
        }
    }, { threshold: [0, 0.5, 0.9, 1] });
    _unreadObserver.observe(divider);

    // Retirement path (c): absolute cap. Even if the divider never becomes
    // "fully visible" (huge sticker below pushes it around, tab was hidden
    // most of the time, etc.), don't stick around forever.
    _unreadHardCapTimer = setTimeout(() => {
        if ($("unread-divider")) retireUnreadDivider();
    }, UNREAD_VIEWED_MAX_MS);
}

// ── typing indicator ──────────────────────────────────────────────────────────
function ensureTypingIndicator() {
    let bar = $("typing-indicator");
    if (!bar) {
        bar = document.createElement("div");
        bar.id = "typing-indicator";
        const msgsEl = $("messages");
        if (msgsEl && msgsEl.parentElement) {
            msgsEl.parentElement.insertBefore(bar, msgsEl.nextSibling);
        }
    }
    return bar;
}

function handleTyping(user) {
    _knownUsers[user] = _knownUsers[user] || {};
    _knownUsers[user].typing = true;
    _updateTypingBar();
    clearTimeout(typingTimers[user]);
    typingTimers[user] = setTimeout(() => {
        if (_knownUsers[user]) _knownUsers[user].typing = false;
        _updateTypingBar();
    }, 3000);
}

function _updateTypingBar() {
    const bar = $("typing-indicator");
    if (!bar) return;
    const typers = Object.keys(_knownUsers).filter(u => _knownUsers[u]?.typing);
    if (!typers.length) { bar.textContent = ""; return; }
    // [SEC] textContent, not innerHTML, so usernames never render as markup.
    if (typers.length === 1) {
        bar.textContent = `${typers[0]} is typing…`;
    } else if (typers.length === 2) {
        bar.textContent = `${typers[0]} and ${typers[1]} are typing…`;
    } else {
        bar.textContent = `${typers.slice(0, -1).join(", ")} and ${typers[typers.length - 1]} are typing…`;
    }
}

// ── online status / presence bar ──────────────────────────────────────────────
function ensurePresenceBar() {
    let bar = $("presence-bar");
    if (!bar) {
        bar = document.createElement("div");
        bar.id = "presence-bar";
        bar.style.display = "none";
        const header = $("chat-header");
        if (header) header.appendChild(bar);
    }
    return bar;
}

function _renderPresenceBar() {
    const bar   = ensurePresenceBar();
    const users = Object.keys(_knownUsers);
    if (!users.length) { bar.style.display = "none"; return; }
    bar.style.display = "flex";
    bar.innerHTML = "";
    users.slice().sort().forEach(user => {
        const online = _knownUsers[user]?.online;
        const pill   = document.createElement("span");
        pill.className = "presence-pill";
        pill.dataset.presenceUser = user;
        // [SEC] Build the dot + username node with textContent to avoid
        // HTML injection from server-supplied usernames.
        const dot = document.createElement("span");
        dot.className = "status-dot " + (online ? "online" : "offline");
        const label = document.createElement("span");
        label.textContent = user;
        pill.appendChild(dot);
        pill.appendChild(label);
        bar.appendChild(pill);
    });
}

function initOnlineStatus(onlineUsers) {
    onlineUsers.forEach(user => {
        _knownUsers[user] = _knownUsers[user] || {};
        _knownUsers[user].online = true;
    });
    _renderPresenceBar();
}

function updateOnlineStatus(user, online) {
    _knownUsers[user] = _knownUsers[user] || {};
    _knownUsers[user].online = online;
    // Fast-path: update just the dot if the pill already exists.
    const pill = document.querySelector(`[data-presence-user="${CSS.escape(user)}"]`);
    if (pill) {
        const dot = pill.querySelector(".status-dot");
        if (dot) { dot.className = `status-dot ${online ? "online" : "offline"}`; return; }
    }
    _renderPresenceBar();
}

// ── lazy image observer ───────────────────────────────────────────────────────
function initImgObserver() {
    imgObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            const img = entry.target;
            if (!img.dataset.src) continue;

            // SCROLL COMPENSATION: lazy images start at src="" (0px height).
            // When they load above the visible scroll area their height
            // growth silently pushes the viewport's content downward.
            const _msgsEl = $("messages");
            if (_msgsEl && !isNearBottom(_msgsEl)) {
                img.addEventListener("load", () => {
                    const cRect = _msgsEl.getBoundingClientRect();
                    const iRect = img.getBoundingClientRect();
                    if (iRect.bottom < cRect.top) {
                        _msgsEl.scrollTop += img.offsetHeight;
                    }
                }, { once: true });
            }
            img.src = img.dataset.src;
            delete img.dataset.src;
            imgObserver.unobserve(img);
        }
    }, { rootMargin: "200px" });
}

function observeLazyImg(img) {
    if (imgObserver) imgObserver.observe(img);
}

// ── animated-media visibility controller ────────────────────────────────
// [PERF] Chat GIFs, animated-WebP stickers, and looping video stickers only
// decode/play while their element is actually on screen. Browsers do NOT
// pause GIF/WebP decoding just because it scrolled out of the viewport in
// an otherwise-active tab — only backgrounding the whole tab does that — so
// without this, a long chat history with many animated images keeps
// costing CPU/battery even when none of them are anywhere near the
// viewport. (Simplified from an earlier version of this controller that
// also capped playback at 5 loops with hover/tap-to-replay; that layer
// added real fragility — e.g. hover-to-replay not working reliably — for
// a UX nicety that wasn't the actual performance win. Visibility-based
// pause/resume alone captures the large majority of the CPU/battery
// savings, so that's all this does now.)
//
// GIF/WebP has no pause()/resume() — the only way to actually stop decode
// work is to clear the <img> src. Hiding the element via CSS
// (visibility/display) does NOT stop the browser from continuing to
// decode frames underneath it. So on leave-viewport we snapshot the
// current frame onto a small <canvas> and show that as a frozen poster,
// then remove the <img> src to actually kill the decode. On re-entering
// the viewport we restore the src — which necessarily restarts the
// animation from frame 1, since GIFs can't resume mid-loop. That's a
// format limitation, not a bug here.
//
// Canvas snapshotting is safe on cross-origin sources (e.g. the Giphy
// CDN): drawImage() taints the canvas for pixel-readback purposes, but we
// only ever draw + display it, never call toDataURL()/getImageData(), so
// the taint is irrelevant.
//
// <video> stickers use the same visibility gate but via real pause()/
// play(), since <video> genuinely supports pausing/resuming without
// restarting — no canvas trickery needed there.

const AnimatedMedia = (() => {
    const posterCanvas = new WeakMap(); // <img> → its frozen-frame <canvas>

    function freezeImg(img) {
        const currentSrc = img.getAttribute("src");
        if (!currentSrc) return; // nothing loaded yet — nothing to freeze

        const w = img.naturalWidth  || img.offsetWidth  || 1;
        const h = img.naturalHeight || img.offsetHeight || 1;

        let canvas = posterCanvas.get(img);
        if (!canvas) {
            canvas = document.createElement("canvas");
            canvas.className = "gif-poster";
            canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;";
            const container = img.parentElement;
            if (container) {
                if (getComputedStyle(container).position === "static") {
                    container.style.position = "relative";
                }
                container.appendChild(canvas);
            }
            posterCanvas.set(img, canvas);
        }
        canvas.width  = w;
        canvas.height = h;
        try {
            const ctx = canvas.getContext("2d");
            ctx.clearRect(0, 0, w, h);
            ctx.drawImage(img, 0, 0, w, h);
            canvas.style.display = "block";
        } catch (_) {
            // Malformed image — leave the canvas blank; not fatal.
        }

        // Lock the box size in pixels before pulling the src, so the
        // element doesn't collapse to 0×0 (and cause a layout jump) while
        // it's sitting off-screen with no image loaded.
        img.style.width  = img.offsetWidth  + "px";
        img.style.height = img.offsetHeight + "px";
        img.dataset.pausedSrc = currentSrc;
        img.removeAttribute("src");   // <- actually stops the decode/CPU work
        img.style.visibility = "hidden";
    }

    function resumeImg(img) {
        const canvas = posterCanvas.get(img);
        if (canvas) canvas.style.display = "none";
        img.style.visibility = "visible";
        const src = img.dataset.pausedSrc;
        if (src && img.getAttribute("src") !== src) img.src = src;
        // First-ever visibility, before anything has loaded: leave src
        // alone. The existing lazy-load observer (observeLazyImg) owns
        // the initial data-src → src swap for lazy chat GIFs; sticker
        // GIFs already have `src` set directly in the markup.
    }

    // Single shared IntersectionObserver — much cheaper than one per
    // element. rootMargin gives a little runway so playback starts just
    // before the element is actually on screen, not exactly at the edge.
    const observer = new IntersectionObserver(entries => {
        for (const entry of entries) {
            const el = entry.target;
            if (el.tagName === "IMG") {
                entry.isIntersecting ? resumeImg(el) : freezeImg(el);
            } else if (el.tagName === "VIDEO") {
                if (entry.isIntersecting) el.play?.().catch(() => {});
                else el.pause?.();
            } else if (el.classList.contains("yt-player-mount")) {
                // YouTube IFrame players (chat bubbles only — search-result
                // preview cards aren't observed since they get torn down on
                // panel close anyway, see closeMusicDrawer). Deliberately
                // ASYMMETRIC vs the <video> sticker branch above: pause on
                // scroll-out, but do NOT auto-resume on scroll back in.
                // Video stickers are muted/looping/decorative — resuming
                // them costs nothing perceptually. A YouTube player has
                // real audio and only ever started because the user
                // explicitly clicked play; auto-resuming it on a scroll
                // event would blast audio nobody asked for at that moment.
                // Only pausing (never destroying) means clicking back in
                // resumes instantly without a fresh player/network cost.
                if (!entry.isIntersecting) {
                    try { el._ytPlayerInstance?.pauseVideo?.(); } catch (_) {}
                }
            }
        }
    }, { rootMargin: "150px" });

    // Public: wire up a single animated element.
    function attach(el) {
        observer.observe(el);
    }

    // Public: stop observing + drop poster-canvas state for an element
    // that's about to be removed from the DOM (called from trimDOMTop /
    // trimDOMBottom). Not strictly required — the WeakMap would let GC
    // reclaim state anyway once unreferenced — but IntersectionObserver
    // itself holds a strong reference to observed targets until
    // explicitly unobserved, so this avoids that reference pinning
    // evicted message nodes in memory during a long session.
    function release(root) {
        if (!root || !root.querySelectorAll) return;
        root.querySelectorAll("img.is-gif-element, video.chat-sticker, .yt-player-mount").forEach(el => {
            observer.unobserve(el);
        });
        // Removing the bubble from the DOM tears down any live iframe's
        // browsing context (and whatever it was playing) automatically —
        // no explicit player.destroy() needed here, same reasoning as
        // why GIF <img> teardown above doesn't need special-casing either.
    }

    // Public: wire up every animated element inside `root`. Single entry
    // point covers chat GIFs, animated-WebP stickers, video stickers, and
    // (once mounted) YouTube player bubbles — both the live-message and
    // history-render paths call this.
    function scan(root) {
        if (!root || !root.querySelectorAll) return;
        root.querySelectorAll("img.is-gif-element").forEach(attach);
        root.querySelectorAll("video.chat-sticker").forEach(attach);
    }

    return { scan, attach, release };
})();

// ── NowPlaying — single active media session + persistent "island" ────────
// Exactly one media source makes sound at a time, app-wide. Two tiers of
// participant, deliberately not treated the same:
//
//   SESSIONS — sent local-audio bubbles, sent ytdlp_audio bubbles, sent
//   YouTube-embed bubbles once their live player mounts. These register
//   here and STAY registered while paused, so scrolling back to the
//   island and tapping play resumes exactly where you left off.
//
//   INTERRUPTERS — the full-screen video viewer and the shared music-
//   panel/attachment preview player. These silence whatever session is
//   active but never become one themselves: closing the video viewer
//   already fully stops it (see closeMediaViewer), and panel previews are
//   gone the instant the panel closes (see closeGifDrawer/closeMusicDrawer's
//   grid-wipe). Neither has anything worth "getting back to," so giving
//   them an island entry would just clutter it with things that vanish
//   the moment you look away from their source.
//
// Why state is read from the real element instead of tracked separately:
// the same discipline reconcileDateSeparators applies to the DOM applies
// here to playback — a <audio>/<video>'s own play/pause/ended events, and
// a YT.Player's own onStateChange, are the single source of truth. The
// island never keeps an "is it playing" boolean independent of the real
// object; that's exactly what would let a scroll-triggered pause
// (AnimatedMedia), a native <audio controls> click, and an island-button
// tap drift out of sync with each other.
const NowPlaying = (() => {
    let active = null; // current session, or null

    function _safeCall(fn) { try { fn(); } catch (_) { /* already torn down */ } }

    // session = { el, kind, title, subtitle, thumbnail, msgId,
    //             pause(), play(), getProgress() }
    function register(session) {
        if (active && active.el !== session.el) _safeCall(() => active.pause());
        stopAudioPreview();
        _interruptVideoViewer();
        active = session;
        Island.show(session);
    }

    // One-shot: silence the active session without replacing it. Used by
    // the video viewer and the panel preview player right before they
    // themselves start making sound.
    function interrupt() {
        if (active) _safeCall(() => active.pause());
    }

    function notifyPlaying(el) { if (active && active.el === el) Island.setPlaying(true); }
    function notifyPaused(el)  { if (active && active.el === el) Island.setPlaying(false); }
    function notifyEnded(el) {
        if (active && active.el === el) { active = null; Island.hide(); }
    }

    function _interruptVideoViewer() {
        const vid = $("viewer-video");
        if (vid && !vid.paused) vid.pause();
    }

    function current() { return active; }

    return { register, interrupt, notifyPlaying, notifyPaused, notifyEnded, current };
})();

// Bind a plain <audio> element (sent local-audio or ytdlp_audio bubble) as
// a NowPlaying session. Metadata comes from data-np-* attributes set at
// bubble-build time (buildFileBubbleHTML / buildYtdlpAudioBubbleHTML) —
// same pattern as data-ytdlp-status-label — so no DOM traversal is needed
// beyond the element itself and its containing .message (for msgId).
function bindNowPlayingAudio(audioEl) {
    if (!audioEl || audioEl.dataset.npBound === "1") return;
    audioEl.dataset.npBound = "1";

    const msgEl = audioEl.closest(".message");
    const session = {
        el: audioEl,
        kind: audioEl.dataset.npKind || "audio",
        title: audioEl.dataset.npTitle || "Audio",
        subtitle: audioEl.dataset.npSubtitle || "",
        thumbnail: audioEl.dataset.npThumb || null,
        msgId: msgEl?.dataset.msgid || null,
        pause: () => audioEl.pause(),
        play:  () => audioEl.play().catch(() => {}),
        getProgress: () => (Number.isFinite(audioEl.duration) && audioEl.duration > 0)
            ? { current: audioEl.currentTime, duration: audioEl.duration }
            : null,
    };

    audioEl.addEventListener("play",    () => NowPlaying.register(session));
    audioEl.addEventListener("playing", () => NowPlaying.notifyPlaying(audioEl));
    audioEl.addEventListener("pause",   () => NowPlaying.notifyPaused(audioEl));
    audioEl.addEventListener("ended",   () => NowPlaying.notifyEnded(audioEl));
    audioEl.addEventListener("error",   () => NowPlaying.notifyEnded(audioEl));
}

function wireNowPlayingAudio(root) {
    (root || document).querySelectorAll("audio[data-np-title]").forEach(bindNowPlayingAudio);
}

// ── Island UI — the persistent playback surface NowPlaying drives ─────────
// Lives outside #messages entirely (fixed to the viewport, not the scroll
// container), which is the whole point: it has to stay reachable
// regardless of scroll position. Lazily created once, then reused —
// same "ensureX()" singleton pattern as the scroll-to-bottom button and
// the ytdlp status pill elsewhere in this file.
const Island = (() => {
    let el = null;
    let progressTimer = null;
    let currentSession = null;

    function ensure() {
        if (el) return el;
        el = document.createElement("div");
        el.id = "now-playing-island";
        el.className = "np-island";
        el.setAttribute("role", "region");
        el.setAttribute("aria-label", "Now playing");
        el.innerHTML = `
            <div class="np-island-art">
                <img class="np-island-thumb" alt="" draggable="false" style="display:none;">
                <div class="np-island-eq" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
                <div class="np-island-ring" aria-hidden="true"></div>
            </div>
            <div class="np-island-meta">
                <span class="np-island-title"></span>
                <span class="np-island-subtitle"></span>
            </div>
            <button type="button" class="np-island-playpause" aria-label="Pause"><span class="icon play sm"></span></button>
            <button type="button" class="np-island-close" aria-label="Stop and dismiss"><span class="icon close sm"></span></button>
            <div class="np-island-progress"><div class="np-island-progress-fill"></div></div>
        `;
        document.body.appendChild(el);

        // [FIX] <img> is natively draggable by default; dragging the
        // thumbnail out (e.g. toward the music panel's dropzone) produced
        // a garbled native drag-ghost snapshot. Container-level guard
        // catches this and any future draggable descendant — same pattern
        // already used on #media-viewer's img/video elements.
        el.addEventListener("dragstart", e => e.preventDefault());

        el.querySelector(".np-island-playpause").addEventListener("click", (e) => {
            e.stopPropagation();
            if (!currentSession) return;
            if (el.classList.contains("np-live")) currentSession.pause();
            else currentSession.play();
        });

        el.querySelector(".np-island-close").addEventListener("click", (e) => {
            e.stopPropagation();
            if (currentSession) { try { currentSession.pause(); } catch (_) {} }
            hide();
        });

        // Tap the body (not the two buttons above, which already stop
        // propagation) to jump back to the message that started this
        // session — reuses the same scrollToMessage() the reply-quote
        // jump already relies on, including its highlight-flash. If the
        // message has since scrolled out of the DOM_CAP window,
        // scrollToMessage() no-ops, same limitation the reply jump already
        // has — not something new introduced here.
        el.addEventListener("click", () => {
            if (currentSession?.msgId) scrollToMessage(currentSession.msgId);
        });

        return el;
    }

    function show(session) {
        currentSession = session;
        const node = ensure();

        const thumb = node.querySelector(".np-island-thumb");
        const eq    = node.querySelector(".np-island-eq");
        if (session.thumbnail) {
            thumb.src = session.thumbnail;
            thumb.style.display = "block";
            eq.style.display = "none";
        } else {
            thumb.style.display = "none";
            eq.style.display = "flex";
        }

        node.querySelector(".np-island-title").textContent = session.title || "Playing";
        node.querySelector(".np-island-subtitle").textContent = session.subtitle || "";

        node.classList.add("np-visible");
        setPlaying(true);
    }

    function setPlaying(isPlaying) {
        const node = ensure();
        node.classList.toggle("np-live", isPlaying);
        const btn = node.querySelector(".np-island-playpause");
        btn.innerHTML = isPlaying
            ? `<span class="icon pause sm"></span>`
            : `<span class="icon play sm"></span>`;
        btn.setAttribute("aria-label", isPlaying ? "Pause" : "Play");
        _toggleProgressPolling(isPlaying);
    }

    function _toggleProgressPolling(on) {
        clearInterval(progressTimer);
        progressTimer = null;
        if (!on || !currentSession) return;
        _tickProgress();
        progressTimer = setInterval(_tickProgress, 500);
    }

    function _tickProgress() {
        if (!currentSession || !el) return;
        const p = currentSession.getProgress();
        const fill = el.querySelector(".np-island-progress-fill");
        if (!fill || !p || !(p.duration > 0)) return;
        fill.style.width = Math.min(100, (p.current / p.duration) * 100) + "%";
    }

    function hide() {
        clearInterval(progressTimer);
        progressTimer = null;
        currentSession = null;
        if (!el) return;
        el.classList.remove("np-visible", "np-live");
        // Node stays in the DOM (cheap, same pattern scroll-to-bottom-btn
        // uses) — next show() just reuses it.
    }

    return { show, setPlaying, hide };
})();

// ── YouTube IFrame Player — lazy bootstrap + shared mount helper ───────────
// The bootstrap script (iframe_api) is tiny JS, not a video decode, so
// loading it eagerly on first panel-open is cheap. The actual expensive
// thing — a live YT.Player instance, i.e. real video decode — is still
// gated behind an explicit click every time, in both call sites that use
// this (search-preview cards and chat bubbles). That click-gate is the
// part that actually matters on this hardware, not the bootstrap script.
let _ytApiReady    = false;
let _ytApiLoading  = false;
const _ytApiWaitQueue = [];

function _ensureYouTubeApiLoaded(cb) {
    if (_ytApiReady) { cb(); return; }
    _ytApiWaitQueue.push(cb);
    if (_ytApiLoading) return;
    _ytApiLoading = true;
    window.onYouTubeIframeAPIReady = () => {
        _ytApiReady = true;
        _ytApiWaitQueue.splice(0).forEach(fn => fn());
    };
    const tag = document.createElement("script");
    tag.src = "https://www.youtube.com/iframe_api";
    document.head.appendChild(tag);
}

let _ytPlayerMountSeq = 0;

// [FACT, verified against Google's own current IFrame API reference]
// getPlaybackQuality/setPlaybackQuality/getAvailableQualityLevels and the
// suggestedQuality argument are no-ops on YouTube's side as of their
// current documented behavior — YouTube stopped honoring manual quality
// selection. There is also no public codec parameter (H264 vs VP9/AV1)
// anywhere in the IFrame API; codec choice has never been embedder-
// configurable — it's negotiated internally between the player and the
// browser. So "request h264 144p" isn't achievable as a hard guarantee
// through this API, full stop — calling setPlaybackQuality below is kept
// only because it's harmless and occasionally still has effect on some
// video/client combinations per anecdotal reports, not because it's
// reliable. The one REAL, current lever is rendering the player small:
// YouTube's adaptive-quality selection does factor in the player's
// displayed pixel dimensions, so a genuinely small mount element is the
// closest thing to a working "keep this cheap" hint that still exists.
// This matters concretely on this hardware (VAAPI limited to H264, no
// VP9/AV1 hardware decode) — if YouTube serves VP9/AV1 for a given
// video regardless of the hint, that's software-decoded on a 2-core APU
// whenever that bubble is actually playing. There is no way to force
// H264 from the embedder side; this is a hard platform constraint, not
// a missing option in this code.
function mountYouTubePlayer(container, videoId, opts = {}) {
    const mountId = "yt-player-mount-" + (++_ytPlayerMountSeq);
    const mountEl = document.createElement("div");
    mountEl.id = mountId;
    mountEl.className = "yt-player-mount";
    container.innerHTML = "";
    container.appendChild(mountEl);

    _ensureYouTubeApiLoaded(() => {
        new YT.Player(mountId, {
            videoId,
            // Plain numbers, not "100%" strings. YT.Player's width/height
            // map to the iframe's width/height HTML attributes, not CSS —
            // percentage strings there are non-standard and behave
            // inconsistently across browsers/webviews. Actual rendered
            // size is owned by CSS (.yt-bubble-thumb-wrap iframe /
            // .yt-result-thumb-wrap iframe already force 100%/100%), so
            // these just need to be valid numbers, not "correct" ones.
            width: 320,
            height: 180,
            playerVars: {
                autoplay: 1,
                playsinline: 1,
                rel: 0,
                modestbranding: 1,
                vq: "tiny",          // best-effort only — see note above
            },
            events: {
                onReady: (e) => {
                    try { e.target.setPlaybackQuality("tiny"); } catch (_) {}
                    // [FIX] YT.Player REPLACES the target div with a real
                    // <iframe> as part of construction — mountEl is a
                    // detached, stale reference by the time this fires.
                    // getIframe() returns the actual live node currently
                    // in the document; that's what pause-on-scroll-out
                    // needs to observe and what needs to carry
                    // _ytPlayerInstance for AnimatedMedia's observer
                    // callback to find it. Attaching to/storing on the
                    // stale mountEl instead (the previous version of this
                    // function did exactly that) is a silent no-op — an
                    // IntersectionObserver on a detached node never
                    // fires, so scroll-out pause quietly never engaged.
                    const liveEl = e.target.getIframe();
                    if (liveEl) {
                        liveEl.classList.add("yt-player-mount");
                        liveEl._ytPlayerInstance = e.target;
                        if (opts.observeVisibility) AnimatedMedia.attach(liveEl);
                    }
                    // opts.observeVisibility already distinguishes exactly
                    // the two contexts this function is called from: true
                    // for chat-bubble playback (wants scroll-based pause,
                    // AND is the one context that should become a
                    // resumable NowPlaying session), false for search-
                    // result preview cards (torn down on panel close, so
                    // only needs to silence whatever else is playing, not
                    // become a session itself). Reusing that existing flag
                    // rather than adding a second parallel option.
                    if (!opts.observeVisibility) NowPlaying.interrupt();
                    // Hands the live player back to whoever mounted it —
                    // search-preview callers use this to register/stop
                    // themselves in the preview-exclusivity tracker (see
                    // _activePreview near buildYtResultCard). Harmless no-op
                    // for chat-bubble callers, which don't pass this.
                    opts.onPlayerReady?.(e.target);
                },
                onStateChange: (e) => {
                    if (!opts.observeVisibility) return; // search-preview: interrupt-only, see onReady above
                    if (e.data === YT.PlayerState.PLAYING) {
                        const cur = NowPlaying.current();
                        if (!cur || cur.el !== e.target) {
                            NowPlaying.register({
                                el: e.target,
                                kind: "youtube",
                                title: opts.npMeta?.title || "YouTube video",
                                subtitle: opts.npMeta?.subtitle || "",
                                thumbnail: opts.npMeta?.thumbnail || null,
                                msgId: opts.npMeta?.msgId || null,
                                pause: () => e.target.pauseVideo(),
                                play:  () => e.target.playVideo(),
                                getProgress: () => {
                                    try {
                                        const d = e.target.getDuration();
                                        const c = e.target.getCurrentTime();
                                        return d > 0 ? { current: c, duration: d } : null;
                                    } catch (_) { return null; }
                                },
                            });
                        } else {
                            NowPlaying.notifyPlaying(e.target);
                        }
                    } else if (e.data === YT.PlayerState.PAUSED) {
                        NowPlaying.notifyPaused(e.target);
                    } else if (e.data === YT.PlayerState.ENDED) {
                        NowPlaying.notifyEnded(e.target);
                    }
                },
                onError: (e) => {
                    // The single most common real-world cause: the
                    // video's owner disabled embedding — very common for
                    // official/label music videos specifically, since
                    // labels often want plays counted on youtube.com
                    // directly rather than embedded elsewhere.
                    // videoEmbeddable=true at search time is SUPPOSED to
                    // filter these out, but that flag isn't always in
                    // sync with the live embedding-permission state, so
                    // this can still surface for a video search thought
                    // was fine. Either way: replace YouTube's own native
                    // "Video unavailable" box (confusing, no way back)
                    // with a clear message and a real link out.
                    let reason = "This video can't be played here.";
                    if (e.data === 101 || e.data === 150) {
                        reason = "The uploader disabled embedding for this video.";
                    } else if (e.data === 100) {
                        reason = "This video is unavailable or private.";
                    } else if (e.data === 2) {
                        reason = "Invalid video.";
                    }
                    renderYtEmbedFallback(container, videoId, reason);
                },
            },
        });
    });

    return mountEl;
}

function renderYtEmbedFallback(container, videoId, reason) {
    container.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "yt-embed-fallback";
    const msg = document.createElement("span");
    msg.textContent = reason;
    const link = document.createElement("a");
    link.href = `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Watch on YouTube \u2197";
    link.addEventListener("click", e => e.stopPropagation());
    wrap.appendChild(msg);
    wrap.appendChild(link);
    container.appendChild(wrap);
}

// ── Shared audio-preview player ─────────────────────────────────────────
// One <audio> element reused everywhere a "preview without committing"
// affordance is needed: local music-panel rows AND staged attachment
// chips both funnel through this, rather than each owning its own media
// element. A folder or an attachment batch can hold several files —
// creating N media elements just to preview one at a time is wasted
// overhead on this hardware. Sharing one also gives "only one preview
// plays at a time" for free, no separate exclusivity bookkeeping needed,
// same reasoning the exclusive-video-playback Set elsewhere in this file
// applies to sent chat videos.
let _audioPreviewPlayer = null;
let _audioPreviewRow    = null;
let _audioPreviewBtn    = null;

function _resetAudioPreviewUI() {
    if (_audioPreviewRow) _audioPreviewRow.classList.remove("is-playing");
    if (_audioPreviewBtn) _audioPreviewBtn.innerHTML = `<span class="icon play sm"></span>`;
    _audioPreviewRow = null;
    _audioPreviewBtn = null;
}

// Called whenever a context holding a preview goes away (panel closes,
// tab switches, folder changes, chip gets removed/cleared) — leaving a
// preview playing invisibly after its row/chip is gone would be a real,
// easy-to-miss bug, not a cosmetic one.
function stopAudioPreview() {
    if (_audioPreviewPlayer && !_audioPreviewPlayer.paused) _audioPreviewPlayer.pause();
    _resetAudioPreviewUI();
}

function toggleAudioPreview(url, rowEl, btnEl) {
    if (!_audioPreviewPlayer) {
        _audioPreviewPlayer = new Audio();
        _audioPreviewPlayer.addEventListener("ended", _resetAudioPreviewUI);
    }

    if (_audioPreviewRow === rowEl && !_audioPreviewPlayer.paused) {
        stopAudioPreview();
        return;
    }

    // A fresh preview is about to make sound — silence whatever sent
    // bubble/YouTube session is currently playing first, same as any
    // other source that starts audio. It stays paused, not cleared, so
    // the island still shows it and resuming it later works normally.
    NowPlaying.interrupt();
    _resetAudioPreviewUI();
    _audioPreviewPlayer.src = url;
    _audioPreviewPlayer.play().catch(() => {});
    rowEl.classList.add("is-playing");
    btnEl.innerHTML = `<span class="icon pause sm"></span>`;
    _audioPreviewRow = rowEl;
    _audioPreviewBtn = btnEl;
}

// ── history & pagination ──────────────────────────────────────────────────────
function makeDateSeparator(text, dk) {
    const div = document.createElement("div");
    div.className = "date-separator";
    div.textContent = text;
    // [FIX] Store the actual calendar date behind the label. "Today"/
    // "Yesterday" are relative to whenever this element was created and
    // will silently go stale the moment midnight passes — this lets
    // refreshDateSeparatorLabels() re-derive the correct text later
    // instead of trusting frozen text forever.
    if (dk) div.dataset.datekey = dk;
    return div;
}

// [FIX] Re-derive every visible separator's label from its stored calendar
// date. Triggered on focus/visibility-change, which is the cheap trigger
// that actually matters — nobody's watching a backgrounded tab's date
// labels tick over in real time, and this avoids a running interval timer.
function refreshDateSeparatorLabels() {
    document.querySelectorAll(".date-separator[data-datekey]").forEach(sep => {
        const [y, m, d] = sep.dataset.datekey.split("-").map(Number);
        const relabelled = dateLabel(new Date(y, m - 1, d));
        if (sep.textContent !== relabelled) sep.textContent = relabelled;
    });
}
// [FIX] On focus / tab-becomes-visible: (1) relabel any stale Today/Yesterday
// text (existing behaviour), (2) run the reconciler to fix any drift that
// silently accumulated while the tab was backgrounded (new), (3) if the
// unread divider is currently on-screen, retire it — the user is looking
// straight at it, no reason to keep it around waiting for a scroll (new).
function _onWindowFocusOrVisible() {
    refreshDateSeparatorLabels();
    scheduleReconcile();
    const divider = $("unread-divider");
    const messagesEl = $("messages");
    if (divider && messagesEl && !divider.classList.contains("unread-divider-fading")) {
        const cRect = messagesEl.getBoundingClientRect();
        const dRect = divider.getBoundingClientRect();
        const onScreen = dRect.bottom > cRect.top && dRect.top < cRect.bottom;
        if (onScreen) retireUnreadDivider();
    }
}
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) _onWindowFocusOrVisible();
});
window.addEventListener("focus", _onWindowFocusOrVisible);

// [FIX] A date-separator left with no .message anywhere before the next
// separator (or the end of the container) is an orphan — every message it
// used to introduce got trimmed off by trimDOMTop/trimDOMBottom, which
// only ever removed .message elements and left the label floating there
// attached to nothing. Called after every trim pass.
function removeOrphanSeparators(container) {
    container.querySelectorAll(".date-separator").forEach(sep => {
        let node = sep.nextElementSibling;
        let hasMessage = false;
        while (node && !node.classList.contains("date-separator")) {
            if (node.classList.contains("message")) { hasMessage = true; break; }
            node = node.nextElementSibling;
        }
        if (!hasMessage) sep.remove();
    });
}

// [FIX] Single deterministic reconciler for date separators.
//
// Why this exists: every previous approach relied on a running tracker
// (`lastRenderedDateKey`) that could silently drift out of sync with the
// DOM — the tracker might say "last date was 2026-07-20" while the DOM's
// actual last message was 2026-07-19 (or vice versa) because a trim pass
// updated one and not the other, or because appendMessage's early-return
// on `!noMoreNewer` skipped the tracker update. The visible symptom in
// the user's screenshot — a `TODAY` separator between two same-day
// messages — is exactly this drift.
//
// The reconciler does not trust ANY prior state. It walks the DOM in order,
// comparing each .message's dateKey to the previous one, and:
//   • inserts a separator where a day boundary exists but none is present,
//   • removes any separator that duplicates or misrepresents the current
//     first-message-of-day boundary,
//   • rewrites labels (Today/Yesterday/absolute) from the actual message
//     timestamp, so midnight-crossing is correct without a refresh.
//
// It is idempotent: running it twice in a row is a no-op. It's cheap:
// linear in the number of .message nodes currently in the DOM (capped by
// DOM_CAP), touching at most one node per boundary. It runs from a single
// requestAnimationFrame batcher so multiple mutations in the same tick
// coalesce into one reconciliation.
let _reconcileScheduled = false;
function scheduleReconcile() {
    if (_reconcileScheduled) return;
    _reconcileScheduled = true;
    requestAnimationFrame(() => {
        _reconcileScheduled = false;
        reconcileDateSeparators($("messages"));
    });
}

function reconcileDateSeparators(container) {
    if (!container) return;

    // First pass: gather every .message in DOM order with its dateKey.
    // Also gather every .date-separator that currently exists (we'll
    // decide which to keep, retag, or remove).
    const nodes = container.children;
    let prevDk  = null;
    // Snapshot as an array so we can mutate the DOM safely during the walk.
    const list = Array.from(nodes);
    for (let i = 0; i < list.length; i++) {
        const el = list[i];
        if (!el.classList) continue;

        // A separator right before a .message: keep it iff the .message's
        // dateKey differs from prevDk, otherwise it's spurious — remove.
        if (el.classList.contains("date-separator")) {
            // Find the next .message after this separator in the list.
            let nextMsg = null;
            for (let j = i + 1; j < list.length; j++) {
                if (list[j].classList?.contains("message")) { nextMsg = list[j]; break; }
                if (list[j].classList?.contains("date-separator")) break;
            }
            if (!nextMsg) {
                // No message follows before another separator — orphan.
                el.remove();
                continue;
            }
            const nextDk = dateKey(nextMsg.dataset.ts);
            if (!nextDk || nextDk === prevDk) {
                // Spurious: the day didn't actually change here. Remove.
                el.remove();
                continue;
            }
            // Keep. Retag its label + datekey to match what's actually there
            // (fixes midnight-crossing without a refresh).
            el.dataset.datekey = nextDk;
            const relabel = dateLabel(new Date(nextMsg.dataset.ts));
            if (el.textContent !== relabel) el.textContent = relabel;
            prevDk = nextDk;
            continue;
        }

        if (el.classList.contains("message")) {
            const dk = dateKey(el.dataset.ts);
            if (!dk) continue;
            if (dk !== prevDk) {
                // Day boundary at this message — must have a separator right
                // before it. If the previous sibling is a separator, we
                // already handled it above; otherwise insert one now.
                const prev = el.previousElementSibling;
                if (!prev || !prev.classList.contains("date-separator")) {
                    const sep = makeDateSeparator(dateLabel(new Date(el.dataset.ts)), dk);
                    container.insertBefore(sep, el);
                }
                prevDk = dk;
            }
        }
    }

    // Second pass: any separator now sitting at the very end of the
    // container with nothing after it, or two separators back-to-back,
    // is a leftover. `removeOrphanSeparators` handles the first case.
    removeOrphanSeparators(container);
}

// [FIX] Schedule a reconcile at the next local midnight so "Today" and
// "Yesterday" relabel themselves without needing a refresh or a focus event.
// This is the fix for: "talking close to 12am, it hits 12, no separator
// appears until refresh."
let _midnightTimer = null;
function scheduleMidnightRelabel() {
    clearTimeout(_midnightTimer);
    const now = new Date();
    const next = new Date(now);
    next.setHours(24, 0, 30, 0);   // 30s after midnight to be safely past it
    const ms = next.getTime() - now.getTime();
    _midnightTimer = setTimeout(() => {
        // Two things must happen at midnight:
        //   1. Every existing "Today" label becomes "Yesterday" — handled by
        //      refreshDateSeparatorLabels() re-deriving from datekey.
        //   2. The next incoming message (or the visible last message) needs
        //      a fresh separator introducing the new day — the reconciler
        //      inserts it because prevDk vs the message's new-day dk differ.
        refreshDateSeparatorLabels();
        scheduleReconcile();
        // Chain to the following midnight so a very-long-open tab keeps working.
        scheduleMidnightRelabel();
    }, ms);
}

function dateLabel(dateStr) {
    const msgDate  = new Date(dateStr);
    const today    = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (msgDate.toDateString() === today.toDateString())     return "Today";
    if (msgDate.toDateString() === yesterday.toDateString()) return "Yesterday";
    return msgDate.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
}

function dateKey(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    return `${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()}`;
}

// [REFACTOR] The three history-rendering functions used to have ~40 lines of
// identical fragment-building logic each. Extracted here — one place to
// worry about grouping, date separators, and render errors. Also now the
// single place that ever creates a date-separator element, so every caller
// (fresh load, older-prepend, newer-append, and appendMessage for live
// messages) agrees on the rules instead of each re-deriving them.
function renderMessagesInto(frag, msgs, seedUser, seedDateKey = null) {
    let lastDate = seedDateKey;
    let lastUser = seedUser;
    for (const msg of msgs) {
        const dk = dateKey(msg.timestamp);
        const isNewDate = dk && dk !== lastDate;
        // Build first, commit second: a separator must never land in the
        // fragment unless the message it introduces actually made it in.
        // (Previously the separator was appended before buildMessageEl ran,
        // so a malformed message could leave a dangling, empty separator.)
        let el;
        try {
            el = buildMessageEl(msg, isNewDate ? null : lastUser);
        } catch (e) {
            console.error("[render] skipping malformed message:", msg, e);
            continue;
        }
        if (isNewDate) {
            frag.appendChild(makeDateSeparator(dateLabel(msg.timestamp), dk));
            lastDate = dk;
            lastUser = null;
        }
        frag.appendChild(el);
        if (msg.type !== "system") lastUser = msg.user;
    }
    return { lastUser, lastDate };
}

async function loadHistory() {
    const messagesEl = $("messages");
    messagesEl.innerHTML = "";
    lastRenderedUser  = null;
    lastRenderedDateKey  = null;
    firstRenderedDateKey = null;
    oldestTimestamp   = null;
    noMoreOlder       = false;

    // [FIX] Message page and presence-based unread boundary are fetched
    // together, and BOTH are awaited before any unread decision is made.
    // This isn't just an optimization — it closes a real race. History
    // load (HTTP) and the WS handshake are two independent async
    // operations with no guaranteed order. The boundary used to arrive
    // via the WS "init" payload; if history finished rendering first, it
    // would see no boundary yet, fall through to "nothing unread, scroll
    // to bottom", and mark everything seen — permanently losing the
    // signal by the time init actually arrived a moment later. Awaiting
    // both here makes the decision deterministic regardless of which
    // finishes first.
    const [msgs, serverBoundaryTs] = await Promise.all([
        fetchPage(null),
        fetchUnreadBoundary(),
    ]);

    // Presence-based boundary is a FALLBACK ONLY, used when this device
    // has no local read-receipt of its own. lastReadTs (localStorage) is
    // the more precise signal — an actual confirmed scroll position on
    // THIS device — while the presence leave time is a coarser "some
    // connection for this user was open until T" proxy (it doesn't
    // distinguish which device, and a network blip re-triggers it just
    // like a real, deliberate leave). Never let it override an existing
    // local record; only seed a blank one. Once seeded, save it so this
    // device has its own local record from here on and doesn't need to
    // lean on presence data again next time.
    if (serverBoundaryTs != null && !Unread.lastReadTs) {
        Unread.lastReadTs = serverBoundaryTs;
        Unread.boundaryTs = serverBoundaryTs;
        Unread.save();
    }

    if (!msgs.length) return;

    oldestTimestamp = msgs[0].timestamp;

    const frag = document.createDocumentFragment();
    const { lastUser, lastDate } = renderMessagesInto(frag, msgs, null, null);
    messagesEl.appendChild(frag);
    wireAudioEnhancements(messagesEl);

    lastRenderedUser     = lastUser;
    firstRenderedDateKey = dateKey(msgs[0].timestamp);
    lastRenderedDateKey  = lastDate;
    newestTimestamp   = msgs[msgs.length - 1].timestamp;
    noMoreNewer       = true;   // this page IS the live tail
    isLoadingNewer    = false;

    // [FIX] Deterministic post-render reconciliation. Even if the tracker
    // was correct here, running the reconciler once establishes the DOM
    // invariant that later mutations assume.
    scheduleReconcile();
    // Arm the midnight relabel once we actually have content that could go stale.
    scheduleMidnightRelabel();

    // [FIX] Unified unread placement:
    //   – If we have a boundary AND some messages are newer than it,
    //     applyUnreadDivider() will place the marker and scroll to it.
    //   – Otherwise scroll to bottom (fresh visit / everything already read).
    // Either way, mark the current tail as "seen" only AFTER we've committed
    // to bottom-scroll; if the divider is shown, the tail is explicitly NOT seen.
    applyUnreadDivider();
    if (!$("unread-divider")) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
        const tailTs = Date.parse(msgs[msgs.length - 1].timestamp);
        if (Number.isFinite(tailTs)) Unread.markSeenUpTo(tailTs);
    }

    settleScrollAfterRender(messagesEl);
}

// [FIX] The previous versions of these two helpers swallowed every error into
// an empty array — which is indistinguishable from "there are no messages."
// On mobile, when the phone couldn't actually reach the server (mixed content,
// unreachable LAN, TLS failure, proxy rewriting the path), history silently
// disappeared with zero user-visible signal. Root-cause fix: separate the
// three states (network failure, server error, empty page) and surface the
// first two via console + toast so the failure mode is discoverable.
async function fetchPage(beforeTs) {
    const url = beforeTs ? `/history?before=${encodeURIComponent(beforeTs)}` : "/history";
    return _fetchHistory(url, "older");
}

async function fetchAfterPage(afterTs) {
    return _fetchHistory(`/history?after=${encodeURIComponent(afterTs)}`, "newer");
}

// [FIX] Presence-based unread-boundary fallback. Fetched once, up front,
// alongside the message page itself — see loadHistory(). Deliberately its
// own small try/catch rather than routing through _fetchHistory: that
// helper assumes a message array on success and an empty-array fallback,
// neither of which fits a single-timestamp-or-null response. Losing this
// value on a network hiccup should silently fall back to "no server
// boundary available" (same as a brand-new user), not surface a toast —
// it's a minor enhancement to an already-working unread system, not a
// core "can't load chat" failure.
async function fetchUnreadBoundary() {
    try {
        const res = await fetch(`/unread-boundary?user=${encodeURIComponent(username)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return (typeof data.boundary_ts === "number") ? data.boundary_ts : null;
    } catch (e) {
        console.error("[unread-boundary] fetch failed:", e);
        return null;
    }
}

// Throttle the "couldn't load history" toast so a flaky connection doesn't
// spam a stack of duplicate warnings.
let _lastHistoryErrorToast = 0;
async function _fetchHistory(url, label) {
    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return await res.json();
    } catch (e) {
        console.error(`[history:${label}] fetch failed:`, e, "url=", url);
        const now = Date.now();
        if (now - _lastHistoryErrorToast > 6000) {
            _lastHistoryErrorToast = now;
            showToast("Couldn't load message history — check connection.");
        }
        return [];
    }
}

async function loadOlderMessages() {
    if (isLoadingOlder || noMoreOlder || !oldestTimestamp) return;
    isLoadingOlder = true;

    const messagesEl = $("messages");

    const loader = document.createElement("div");
    loader.id = "older-loader";
    loader.textContent = "Loading…";
    loader.style.cssText = "text-align:center;color:#888;font-size:12px;padding:6px 0;";
    messagesEl.prepend(loader);

    const msgs = await fetchPage(oldestTimestamp);
    loader.remove();

    if (!msgs.length) {
        noMoreOlder    = true;
        isLoadingOlder = false;
        return;
    }

    if (msgs.length < PAGE_SIZE) noMoreOlder = true;
    oldestTimestamp = msgs[0].timestamp;

    const frag = document.createDocumentFragment();
    renderMessagesInto(frag, msgs, null, null);
    // [FIX] Older-history and newer-history pagination were the two render
    // paths that never called this — messages present at initial load or
    // arriving live got the resolving pill / now-playing wiring, ones
    // loaded by scrolling up or down silently didn't. Must run before
    // insertBefore() below moves this fragment's children out of it.
    wireAudioEnhancements(frag);

    // If the new batch's newest message shares a calendar day with
    // whatever's currently at the top, that top separator is now
    // redundant — the batch's own leading separator already introduces
    // that day earlier in the timeline. Reconciled against the shared
    // tracker rather than re-deriving it by querying the DOM, which broke
    // once a prior trim left an orphaned separator sitting up there with
    // nothing under it (querySelector(".message") would then find the
    // wrong "first" message and mis-judge the comparison).
    if (firstRenderedDateKey
        && dateKey(msgs[msgs.length - 1].timestamp) === firstRenderedDateKey
        && messagesEl.firstChild
        && messagesEl.firstChild.classList.contains("date-separator")) {
        messagesEl.firstChild.remove();
    }

    // ── SCROLL ANCHOR ──────────────────────────────────────────────────────
    // Captured synchronously right before the DOM mutation so no async scroll
    // event during the fetch can skew the values. offsetTop is parent-relative
    // (unaffected by scrollTop), which makes the delta immune to user scrolling
    // during the awaited fetch.
    const anchorEl           = messagesEl.querySelector(".message");
    const anchorOffsetBefore = anchorEl ? anchorEl.offsetTop : 0;
    const scrollTopBefore    = messagesEl.scrollTop;

    messagesEl.insertBefore(frag, messagesEl.firstChild);
    firstRenderedDateKey = dateKey(msgs[0].timestamp);

    trimDOMBottom(messagesEl);

    if (anchorEl && anchorEl.isConnected) {
        messagesEl.scrollTop = scrollTopBefore + (anchorEl.offsetTop - anchorOffsetBefore);
    }

    // [FIX] Prepending a page can create a duplicate separator right at the
    // seam between the newly-inserted batch's tail and the previously-first
    // message. The reconciler removes it.
    scheduleReconcile();

    isLoadingOlder = false;
}

async function loadNewerMessages() {
    if (isLoadingNewer || noMoreNewer || !newestTimestamp) return;
    isLoadingNewer = true;

    const messagesEl = $("messages");
    const msgs = await fetchAfterPage(newestTimestamp);

    if (!msgs.length) {
        noMoreNewer    = true;
        isLoadingNewer = false;
        return;
    }

    if (msgs.length < PAGE_SIZE) noMoreNewer = true;
    newestTimestamp = msgs[msgs.length - 1].timestamp;

    const frag = document.createDocumentFragment();
    // Seed from the shared trackers (kept correct by every other path,
    // including trims) rather than re-deriving them by querying the DOM
    // for ".message:last-of-type" — that query duplicated logic that
    // belongs in one place and would go stale after a trim the same way
    // loadOlderMessages' old dedup check did.
    const { lastUser, lastDate } = renderMessagesInto(frag, msgs, lastRenderedUser, lastRenderedDateKey);
    wireAudioEnhancements(frag); // [FIX] see loadOlderMessages — same missing wiring, same fix

    messagesEl.appendChild(frag);
    lastRenderedUser    = lastUser;
    lastRenderedDateKey = lastDate;

    trimDOMTop(messagesEl);

    // [FIX] Same reason as loadOlderMessages — the append seam can now have
    // a stale/duplicate separator if the tracker drifted.
    scheduleReconcile();

    isLoadingNewer = false;
}

function trimDOMBottom(container) {
    const msgs = container.querySelectorAll(".message");
    if (msgs.length <= DOM_CAP) return;
    const excess = msgs.length - DOM_CAP;
    for (let i = msgs.length - 1; i >= msgs.length - excess; i--) {
        AnimatedMedia.release(msgs[i]);
        msgs[i].remove();
    }
    lastRenderedUser = null;

    // We just evicted possibly-never-seen messages from the tail. Record
    // the new DOM edge + flag that a gap exists so scrolling back down
    // resumes fetching from the right place.
    const newLast = msgs[msgs.length - excess - 1];
    if (newLast) {
        newestTimestamp = newLast.dataset.ts || newestTimestamp;
        // [FIX] The true bottom-most message's date may have changed —
        // keep the shared tracker honest so a later loadNewerMessages
        // doesn't seed itself from a date that no longer exists in the DOM.
        lastRenderedDateKey = dateKey(newLast.dataset.ts) || lastRenderedDateKey;
    }
    noMoreNewer = false;

    // [FIX] Trimming only ever removed .message elements — if every
    // message under some day's separator just got evicted, that separator
    // was left floating with nothing under it.
    removeOrphanSeparators(container);
}

function trimDOMTop(container) {
    const msgs = container.querySelectorAll(".message");
    if (msgs.length <= DOM_CAP) return;
    const excess = msgs.length - DOM_CAP;
    for (let i = 0; i < excess; i++) {
        AnimatedMedia.release(msgs[i]);
        msgs[i].remove();
    }
    const remaining = container.querySelector(".message");
    if (remaining) {
        oldestTimestamp = remaining.dataset.ts || oldestTimestamp;
        firstRenderedDateKey = dateKey(remaining.dataset.ts) || firstRenderedDateKey;
    }
    noMoreOlder = false;

    removeOrphanSeparators(container);
}

let _scrollTimer = null;
function onMessagesScroll() {
    if (_scrollTimer) return;
    _scrollTimer = setTimeout(() => {
        _scrollTimer = null;
        const el = $("messages");
        if (!el) return;

        if (el.scrollTop < 120) loadOlderMessages();
        if (isNearBottom(el)) {
            if (!noMoreNewer) loadNewerMessages();
            // [FIX] Reaching the bottom means "seen everything up to this
            // moment". Route through Unread so the tab title, scroll button
            // and persisted lastReadTs all update from one place.
            Unread.markSeenUpTo(Date.now());
        }

        // [FIX] Retire the unread divider once the user has scrolled past it.
        // Route through retireUnreadDivider() so the fade + observer cleanup
        // happens in ONE place — the old code hard-remove'd the node here and
        // left the IntersectionObserver dangling (harmless, but leaky).
        const divider = $("unread-divider");
        if (divider && !divider.classList.contains("unread-divider-fading")) {
            const cRect = el.getBoundingClientRect();
            const dRect = divider.getBoundingClientRect();
            if (dRect.bottom < cRect.top) retireUnreadDivider();
        }
    }, 50);
}

// ── connection banner ─────────────────────────────────────────────────────────
function showConnBanner() {
    let b = $("conn-banner");
    if (!b) {
        b = document.createElement("div");
        b.id = "conn-banner";
        // [REBOOT] Styling moved to style.css (#conn-banner) so the reboot
        // theme owns the look.
        b.style.opacity = "0";
        b.style.transition = "opacity 0.25s";
        b.style.pointerEvents = "none";
        const messagesEl = $("messages");
        if (messagesEl) messagesEl.prepend(b);
    }
    // [REBOOT] Warning glyph swapped for the warning SVG icon.
    b.innerHTML =
        `<span class="icon warning sm"></span>` +
        `<span>Disconnected — reconnecting…</span>`;
    b.style.display = "flex";
    requestAnimationFrame(() => { b.style.opacity = "1"; });
}

function hideConnBanner() {
    const b = $("conn-banner");
    if (!b) return;
    b.style.opacity = "0";
    setTimeout(() => { b.style.display = "none"; }, 250);
}

// ── heartbeat ─────────────────────────────────────────────────────────────────
let _pingInterval      = null;
let _pongWatchdog      = null;
let _missedPongs       = 0;
const PING_MS          = 20_000;
const PONG_MS          = 7_000;
const MAX_MISSED_PONGS = 2;

function startHeartbeat() {
    stopHeartbeat();
    _missedPongs = 0;
    _pingInterval = setInterval(() => {
        if (!socket || socket.readyState !== 1) return;
        socket.send(JSON.stringify({ type: "ping" }));
        _pongWatchdog = setTimeout(() => {
            _missedPongs++;
            if (_missedPongs >= MAX_MISSED_PONGS) {
                console.warn("[ws] pong timeout — forcing reconnect");
                socket.close();
            }
        }, PONG_MS);
    }, PING_MS);
}

function stopHeartbeat() {
    clearInterval(_pingInterval);
    clearTimeout(_pongWatchdog);
    _pingInterval = null;
    _pongWatchdog = null;
}

// ── websocket ─────────────────────────────────────────────────────────────────
let _reconnectTimer = null;

function connectWebSocket() {
    // Guard: never open a second socket on top of one that's already
    // connecting/open — this is what produced the endless flap.
    if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
    clearTimeout(_reconnectTimer);

    const protocol = location.protocol === "https:" ? "wss://" : "ws://";
    socket = new WebSocket(protocol + location.host + "/ws?user=" + encodeURIComponent(username));

    const _connectStartedAt = Date.now();

    socket.onopen = () => {
        console.log(`[ws] open (took ${Date.now() - _connectStartedAt}ms to connect)`);
        // [REBOOT] ✓ kept here — it appears only in a transient toast body
        // (rendered as textContent inside .toast) which is meant to be read
        // as prose, not as a UI icon spot.
        if (_reconnectAttempts > 0) showToast("Reconnected ✓");
        _reconnectAttempts = 0;
        _missedPongs = 0;
        hideConnBanner();
        startHeartbeat();
    };

    socket.onmessage = (event) => {
        let msg;
        try { msg = JSON.parse(event.data); }
        catch (e) { console.warn("[ws] non-JSON frame:", event.data); return; }

        // ── control frames ────────────────────────────────────────────────
        if (msg.type === "pong") {
            _missedPongs = 0;
            clearTimeout(_pongWatchdog);
            _pongWatchdog = null;
            return;
        }
        if (msg.type === "ping") {
            socket.send(JSON.stringify({ type: "pong" }));
            return;
        }

        // ── init payload (sent once per WS connect) ─────────────────────────
        // [FIX] The unread boundary is no longer decided here. It's resolved
        // once, deterministically, inside loadHistory() — which awaits both
        // the message page AND /unread-boundary (presence-based) before
        // making any decision, specifically to avoid a race between WS
        // connect and the history HTTP fetch (see loadHistory()'s comment).
        // The call below is a harmless idempotent no-op in the common case:
        // applyUnreadDivider() is guarded by Unread._dividerPlaced, so it
        // does nothing if loadHistory() already placed the divider, and it
        // safely no-ops if messages haven't rendered yet either.
        if (msg.type === "init") {
            if (Array.isArray(msg.online_users)) initOnlineStatus(msg.online_users);
            applyUnreadDivider();
            return;
        }

        // ── ephemeral presence events ─────────────────────────────────────
        if (msg.type === "typing") {
            if (msg.user !== username) handleTyping(msg.user);
            return;
        }

        if (msg.type === "status") {
            updateOnlineStatus(msg.user, msg.online);
            return;
        }

        // ── persisted events (regular messages + system join/leave) ───────
        appendMessage(msg);
        if (msg.type !== "system" && msg.user !== username &&
            (document.hidden || !document.hasFocus())) {
            playNotificationSound();
            showDesktopNotif(msg);
        }
    };

    socket.onclose = (event) => {
        console.warn(
            `[ws] closed after ${Date.now() - _connectStartedAt}ms — ` +
            `code=${event.code} reason="${event.reason || "(none)"}" clean=${event.wasClean}`
        );
        stopHeartbeat();
        showConnBanner();
        // [QoL] Exponential backoff: 3s → 4.8s → 7.7s → … → 30s cap, with ±500ms jitter.
        const delay = Math.min(30000, 3000 * Math.pow(1.6, _reconnectAttempts++) + Math.random() * 500);
        clearTimeout(_reconnectTimer);
        _reconnectTimer = setTimeout(connectWebSocket, delay);
    };

    socket.onerror = (event) => {
        console.error("[ws] error", event);
        stopHeartbeat();
    };
}

// ── Standard Attachments ──────────────────────────────────────────────────────
const MAX_ATTACHMENT_SIZE_MB    = 100;
const MAX_ATTACHMENT_SIZE_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024;

// [FIX] Root cause of the "paste uploads twice" bug is now fixed at this layer:
// the paste flow used to fire BOTH a `beforeinput` handler (which read files
// from `e.dataTransfer.files`) AND a `paste` handler (which read the same
// files from `clipboardData.items`). Both called handleFiles() for the same
// clipboard payload. The two handlers were completely uncoordinated.
//
// The fix is twofold:
//   1. Only ONE paste path — the `paste` handler — reads clipboard files.
//      The `beforeinput` handler is gone; it added nothing that `paste`
//      didn't already cover, and it was the entire source of duplication.
//   2. handleFiles() itself de-dupes: two File objects that share (name,
//      size, lastModified, type) within a very short window are treated as
//      the same file. This is a defence-in-depth check so ANY future
//      double-fire (drag+drop that also triggers a synthesized paste, etc.)
//      still results in exactly one upload.
const _recentFileFingerprints = new Map();  // fingerprint → timestamp
const RECENT_FILE_WINDOW_MS   = 1500;

function fileFingerprint(f) {
    return `${f.name}|${f.size}|${f.lastModified || 0}|${f.type}`;
}

function handleFiles(files) {
    const now = Date.now();
    // [PERF] Prune stale entries so the Map doesn't grow forever.
    for (const [fp, ts] of _recentFileFingerprints) {
        if (now - ts > RECENT_FILE_WINDOW_MS) _recentFileFingerprints.delete(fp);
    }

    for (const file of files) {
        if (file.size > MAX_ATTACHMENT_SIZE_BYTES) {
            showToast(`File "${file.name}" rejected! Max limit is ${MAX_ATTACHMENT_SIZE_MB}MB.`);
            continue;
        }

        const fp = fileFingerprint(file);
        if (_recentFileFingerprints.has(fp)) {
            // Silent skip — this is exactly the duplicate we wanted to catch.
            continue;
        }
        _recentFileFingerprints.set(fp, now);

        attachmentFiles.push(file);
        addPreviewItem(file);
    }
    $("message-input")?.focus();
}

function addPreviewItem(file) {
    const previewArea = $("attachment-preview");
    const list        = $("preview-list");

    const item = document.createElement("div");
    item.className = "preview-item";
    item._file = file;

    const removeBtn = document.createElement("button");
    removeBtn.className = "preview-remove";
    removeBtn.setAttribute("aria-label", "Remove attachment");
    // [REBOOT] SVG close icon in place of the ✕ character.
    removeBtn.innerHTML = `<span class="icon close"></span>`;
    removeBtn.addEventListener("click", () => removePreviewItem(item));

    if (file.type.startsWith("image/")) {
        const reader = new FileReader();
        reader.onload = (e) => {
            const dataUrl = e.target.result;
            const img = document.createElement("img");
            img.src       = dataUrl;
            img.className = "preview-img";
            img.style.cursor = "pointer";
            img.draggable = false; // browsers make <img> natively draggable; that
                                    // would race our own reorder drag below and
                                    // wrongly trigger the external-file drop overlay
            // Click to see it full-size before sending — same viewer
            // clicking a sent chat image already opens, just with a
            // dataURL instead of a server path.
            img.addEventListener("click", (e) => {
                e.stopPropagation();
                openMediaViewer(dataUrl, "image", { trustedLocal: true });
            });
            item.appendChild(img);
            item.appendChild(removeBtn);
        };
        reader.readAsDataURL(file);
    } else if (file.type.startsWith("video/")) {
        // Blob URL, not a static thumbnail — this actually plays the real
        // staged file in the same full-screen viewer sent videos use,
        // rather than extracting a frame via canvas (real engineering
        // cost for a one-time, throwaway preview; playing the file
        // directly gets the same "see what you're about to send" result
        // for a fraction of the work).
        const blobUrl = URL.createObjectURL(file);
        item._previewBlobUrl = blobUrl;

        const previewBtn = document.createElement("button");
        previewBtn.type = "button";
        previewBtn.className = "preview-media-btn";
        previewBtn.setAttribute("aria-label", "Preview video");
        previewBtn.innerHTML = `<span class="icon play sm"></span>`;
        previewBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            openMediaViewer(blobUrl, "video", { trustedLocal: true });
        });
        item.appendChild(previewBtn);

        const name = document.createElement("span");
        name.className   = "preview-name";
        name.textContent = file.name;
        item.appendChild(name);
        item.appendChild(removeBtn);
    } else if (file.type.startsWith("audio/")) {
        // Audio has no visual to show in a full-screen viewer — "preview"
        // here means actually hearing it, via the same shared play/pause
        // toggle the local music panel uses, not a modal.
        const blobUrl = URL.createObjectURL(file);
        item._previewBlobUrl = blobUrl;

        const playBtn = document.createElement("button");
        playBtn.type = "button";
        playBtn.className = "preview-media-btn";
        playBtn.setAttribute("aria-label", "Preview audio");
        playBtn.innerHTML = `<span class="icon play sm"></span>`;
        playBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            toggleAudioPreview(blobUrl, item, playBtn);
        });
        item.appendChild(playBtn);

        const name = document.createElement("span");
        name.className   = "preview-name";
        name.textContent = file.name;
        item.appendChild(name);
        item.appendChild(removeBtn);
    } else {
        // [REBOOT] Icon uses the CSS mask-image icon system. Generic files
        // (no meaningful preview available) keep the old plain-icon look.
        const icon = document.createElement("span");
        icon.className = "preview-icon icon file";
        icon.setAttribute("aria-label", "file");
        const name = document.createElement("span");
        name.className   = "preview-name";
        name.textContent = file.name;
        item.appendChild(icon);
        item.appendChild(name);
        item.appendChild(removeBtn);
    }

    list.appendChild(item);
    if (previewArea.style.display !== "flex") {
        preserveScrollAcrossResize($("messages"), () => { previewArea.style.display = "flex"; });
    } else {
        previewArea.style.display = "flex";
    }
}

function removePreviewItem(item) {
    attachmentFiles = attachmentFiles.filter(f => f !== item._file);
    // Stop and release the preview if this chip was the one playing/
    // holding the blob URL — otherwise the shared player keeps a
    // reference (and possibly keeps playing) after its chip is gone.
    if (item._previewBlobUrl) {
        if (_audioPreviewRow === item) stopAudioPreview();
        URL.revokeObjectURL(item._previewBlobUrl);
    }
    item.remove();
    if (!attachmentFiles.length && !$("preview-list")?.querySelector(".preview-item.is-pending")) {
        preserveScrollAcrossResize($("messages"), () => { $("attachment-preview").style.display = "none"; });
    }
}

// ── attachment reorder (drag & drop) ─────────────────────────────────────
// Plain Pointer Events (one code path for mouse + touch + pen) instead of
// HTML5 draggable="true" — native DnD has no touch support, and this way a
// plain tap still falls through untouched to the existing image/video/
// audio-preview and remove-button click handlers below. Only a press that
// actually moves past DRAG_THRESHOLD_PX counts as "click and hold and
// drag"; anything under that is just a click, exactly as before this
// feature existed. Works for every chip type (image/video/audio/generic
// file) since it's wired once, by delegation, off the shared .preview-item
// class rather than per-type.
const DRAG_THRESHOLD_PX = 6;
let _previewDrag = null; // { item, pointerId, startX, startY, dragging }

function initAttachmentReorder() {
    const list = $("preview-list");
    if (!list || list._reorderInit) return;
    list._reorderInit = true;
    list.addEventListener("pointerdown", onPreviewPointerDown);
    // Preview thumbnails include <img> elements, which browsers make
    // natively draggable by default. Without this, starting a reorder drag
    // on a thumbnail kicks off the browser's OWN drag-and-drop instead of
    // (or racing) our pointer-based one — which also bubbles dragenter/drop
    // up to the window-level "external file" overlay handlers further down
    // this file, popping the "Drop to attach" screen for a drag that never
    // left the tray. Blocking dragstart here stops that at the source, for
    // every chip type, not just images.
    list.addEventListener("dragstart", e => e.preventDefault());
}

function onPreviewPointerDown(e) {
    if (_previewDrag) return;             // one attachment drag at a time
    if (e.button !== 0) return;           // primary mouse button / touch only
    if (e.target.closest(".preview-remove")) return; // "x" stays plain-click-only

    const item = e.target.closest(".preview-item");
    if (!item || !item._file) return;     // pending/error GIF chips aren't real files yet

    _previewDrag = { item, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, dragging: false };
    document.addEventListener("pointermove", onPreviewPointerMove);
    document.addEventListener("pointerup", onPreviewPointerEnd);
    document.addEventListener("pointercancel", onPreviewPointerEnd);
}

function onPreviewPointerMove(e) {
    const st = _previewDrag;
    if (!st || e.pointerId !== st.pointerId) return;

    if (!st.dragging) {
        if (Math.hypot(e.clientX - st.startX, e.clientY - st.startY) < DRAG_THRESHOLD_PX) return;
        st.dragging = true;
        st.item.classList.add("is-dragging");
        document.body.classList.add("attachment-dragging");
        try { st.item.setPointerCapture(st.pointerId); } catch (_) {}
    }

    e.preventDefault();

    // Whichever chip the pointer is over right now — move the dragged chip
    // immediately before/after it depending on which half it's hovering,
    // so the tray reorders live as you drag (same feel as most sortable
    // lists). Pointer capture doesn't affect elementFromPoint, so this
    // still resolves to whatever's visually under the cursor.
    const over = document.elementFromPoint(e.clientX, e.clientY)?.closest(".preview-item");
    if (!over || over === st.item || over.parentElement !== st.item.parentElement) return;

    const rect     = over.getBoundingClientRect();
    const putBefore = e.clientX < rect.left + rect.width / 2;
    over.parentElement.insertBefore(st.item, putBefore ? over : over.nextSibling);
}

function onPreviewPointerEnd(e) {
    const st = _previewDrag;
    if (!st || e.pointerId !== st.pointerId) return;

    document.removeEventListener("pointermove", onPreviewPointerMove);
    document.removeEventListener("pointerup", onPreviewPointerEnd);
    document.removeEventListener("pointercancel", onPreviewPointerEnd);

    if (st.dragging) {
        st.item.classList.remove("is-dragging");
        document.body.classList.remove("attachment-dragging");
        syncAttachmentOrderFromDOM();
        // A real drag still synthesizes a click on the same element right
        // after pointerup — swallow exactly that one so dropping a chip
        // doesn't also pop open the image/video/audio preview underneath it.
        const swallow = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
        st.item.addEventListener("click", swallow, { capture: true, once: true });
        setTimeout(() => st.item.removeEventListener("click", swallow, { capture: true }), 400);
    }
    _previewDrag = null;
}

// Single source of truth for send order: whatever order the chips are
// actually sitting in, left to right, right now.
function syncAttachmentOrderFromDOM() {
    const list = $("preview-list");
    if (!list) return;
    attachmentFiles = Array.from(list.children).map(el => el._file).filter(Boolean);
}

// [FIX] Pending-GIF chip subsystem. A remote Giphy fetch can take a moment;
// this gives the user an immediate, visible "your click registered" signal
// in the same preview-list the file/sticker attachments already use, rather
// than inventing a separate loading subsystem. Each in-flight fetch gets a
// unique token so multiple queued GIF picks (fast double-taps, or picking a
// second GIF while the first is still loading) resolve/fail independently
// and never clobber each other's chip.
let _pendingGifSeq = 0;

function addPendingGifChip() {
    const previewArea = $("attachment-preview");
    const list        = $("preview-list");
    if (!previewArea || !list) return null;

    const token = ++_pendingGifSeq;
    const item = document.createElement("div");
    item.className = "preview-item is-pending";
    item.dataset.pendingToken = String(token);

    const spinner = document.createElement("span");
    spinner.className = "preview-spinner";
    spinner.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "preview-name";
    label.textContent = "Loading GIF…";

    item.appendChild(spinner);
    item.appendChild(label);
    list.appendChild(item);
    previewArea.style.display = "flex";

    return token;
}

function resolvePendingGifChip(token, file) {
    const item = list_findPendingChip(token);
    if (!item) {
        // Chip was already removed (user hit "Clear all" mid-fetch, or the
        // request was superseded). Don't resurrect it or stage a file the
        // user no longer expects to see.
        return false;
    }
    item.remove();
    attachmentFiles.push(file);
    addPreviewItem(file);
    return true;
}

function failPendingGifChip(token) {
    const item = list_findPendingChip(token);
    if (!item) return;
    item.classList.remove("is-pending");
    item.classList.add("is-error");
    item.innerHTML = "";

    const icon = document.createElement("span");
    icon.className = "preview-icon icon warning";
    icon.setAttribute("aria-label", "error");
    const label = document.createElement("span");
    label.className = "preview-name";
    label.textContent = "GIF failed to load";
    const removeBtn = document.createElement("button");
    removeBtn.className = "preview-remove";
    removeBtn.setAttribute("aria-label", "Dismiss");
    removeBtn.innerHTML = `<span class="icon close"></span>`;
    removeBtn.addEventListener("click", () => {
        item.remove();
        if (!attachmentFiles.length && !$("preview-list")?.querySelector(".preview-item.is-pending")) {
            $("attachment-preview").style.display = "none";
        }
    });

    item.appendChild(icon);
    item.appendChild(label);
    item.appendChild(removeBtn);

    // Auto-dismiss so a failed pick doesn't linger forever if the user
    // doesn't notice the small chip.
    setTimeout(() => {
        if (item.isConnected) removeBtn.click();
    }, 4000);
}

function list_findPendingChip(token) {
    const list = $("preview-list");
    if (!list) return null;
    return list.querySelector(`.preview-item[data-pending-token="${token}"]`);
}

function clearAttachments() {
    attachmentFiles = [];
    const list = $("preview-list");
    if (list) {
        // Revoke every staged video/audio blob URL before the chips that
        // reference them are gone — otherwise each one leaks for the rest
        // of the page's life, silently, since nothing else will ever
        // call revokeObjectURL on them again.
        list.querySelectorAll(".preview-item").forEach(item => {
            if (item._previewBlobUrl) URL.revokeObjectURL(item._previewBlobUrl);
        });
        list.innerHTML = "";  // also wipes any pending/error GIF chips
    }
    stopAudioPreview();
    const previewArea = $("attachment-preview");
    if (previewArea) preserveScrollAcrossResize($("messages"), () => { previewArea.style.display = "none"; });
}

// ── reply functionality ───────────────────────────────────────────────────────
function setReply(msgEl) {
    const mediaEl = msgEl.querySelector('img:not(.chat-logo), video');
    const mediaSrc = mediaEl ? safeMediaURL(mediaEl.src) : null;
    const mediaTag = mediaEl ? mediaEl.tagName.toLowerCase() : null;

    replyingTo = {
        id:       msgEl.dataset.msgid,
        user:     msgEl.dataset.user,
        text:     msgEl.dataset.previewText,
        mediaSrc: mediaSrc,
        mediaTag: mediaTag
    };

    preserveScrollAcrossResize($("messages"), () => {
        $("reply-bar").style.display = "flex";
    });
    $("reply-bar-user").textContent = replyingTo.user;

    const replyTextEl = $("reply-bar-text");
    replyTextEl.innerHTML = "";  // clear either mode's previous content

    if (mediaSrc) {
        // [SEC] Build the media node with DOM APIs; never interpolate URLs into innerHTML.
        const el = document.createElement(mediaTag === "video" ? "video" : "img");
        el.src = mediaSrc;
        el.style.cssText = "height:32px;border-radius:4px;object-fit:cover;";
        replyTextEl.appendChild(el);
    } else {
        replyTextEl.textContent = (replyingTo.text || "").slice(0, 80);
    }

    $("message-input").focus();
}

function clearReply() {
    replyingTo = null;
    const bar = $("reply-bar");
    if (bar) preserveScrollAcrossResize($("messages"), () => { bar.style.display = "none"; });
}

function scrollToMessage(id) {
    const el = document.querySelector(`[data-msgid="${CSS.escape(id)}"]`);
    if (!el) return;
    el.scrollIntoView({ block: "center" });
    el.classList.add("highlight-flash");
    setTimeout(() => el.classList.remove("highlight-flash"), 1100);
}

let touchStartX = 0;
let touchStartY = 0;

// ── send ──────────────────────────────────────────────────────────────────────
async function sendMessage() {
    const input = $("message-input");
    const text  = input.innerText.trim();

    if (!text && !attachmentFiles.length) return;
    if (!socket || socket.readyState !== 1) return;

    // Chips may have been dragged since they were staged — resync the send
    // order from the DOM so it always matches what's on screen, left to
    // right, regardless of the order files were originally added in.
    syncAttachmentOrderFromDOM();
    const filesToSend = attachmentFiles.slice();
    clearAttachments();

    const progBar = $("upload-progress");

    if (filesToSend.length > 0) {
        if (progBar) {
            preserveScrollAcrossResize($("messages"), () => { progBar.style.display = "block"; });
            progBar.value = 0;
        }

        const fileProgressArray = new Array(filesToSend.length).fill(0);
        const totalBatchBytes = filesToSend.reduce((acc, f) => acc + f.size, 0) || 1;

        const uploadOne = (file, index) => new Promise((resolve) => {
            const fd = new FormData();
            fd.append("file", file);
            fd.append("user", username);
            if (filesToSend.length === 1 && text) fd.append("caption", text);
            if (replyingTo) fd.append("replyTo", JSON.stringify(replyingTo));

            const xhr = new XMLHttpRequest();
            xhr.open("POST", "/upload");

            xhr.upload.onprogress = function(event) {
                if (event.lengthComputable && progBar) {
                    fileProgressArray[index] = event.loaded;
                    const totalSent = fileProgressArray.reduce((acc, val) => acc + val, 0);
                    progBar.value = (totalSent / totalBatchBytes) * 100;
                }
            };

            xhr.onload = function() {
                if (xhr.status === 200) {
                    resolve(true);
                } else {
                    let errMsg = "Server rejected payload.";
                    try {
                        const res = JSON.parse(xhr.responseText);
                        if (res.error) errMsg = res.error;
                    } catch (_) {}
                    showToast(`Upload Failed: ${errMsg}`);
                    resolve(false);
                }
            };

            xhr.onerror = function() {
                showToast("Upload Failed: Network error.");
                resolve(false);
            };

            xhr.send(fd);
        });

        // [FIX] Sequential, not parallel — each upload is awaited before the
        // next starts, so the server (and everyone's chat feed) receives
        // them in the same left-to-right order shown in the tray. Firing
        // every request at once (the old Promise.all) let whichever file
        // finished first post first, regardless of its position on screen.
        try {
            for (let index = 0; index < filesToSend.length; index++) {
                await uploadOne(filesToSend[index], index);
            }
        } catch (e) {
            console.error(e);
        } finally {
            if (progBar) preserveScrollAcrossResize($("messages"), () => { progBar.style.display = "none"; });
        }
    }

    if (text && filesToSend.length !== 1) {
        const payload = { user: username, text, id: genId() };
        if (replyingTo) payload.replyTo = replyingTo;
        socket.send(JSON.stringify(payload));
    }

    // [FIX] Own send advances the lastReadTs high-water mark so this message
    // can never come back as "unread for me" on next reload.
    Unread.markOwnMessage();

    input.innerText = "";
    localStorage.removeItem("chat_draft");
    input.focus();
    clearReply();
}

// ── showChat ──────────────────────────────────────────────────────────────────
function showChat() {
    notifySound = $("notify-sound");
    $("join-screen").style.display = "none";
    $("chat-screen").style.display = "flex";

    initImgObserver();
    loadHistory();

    const messagesEl = $("messages");
    const msgInput   = $("message-input");

    // Inject ephemeral presence UI — presence bar above messages, typing bar below.
    ensurePresenceBar();
    ensureTypingIndicator();

    // [QoL] Restore unsent draft from previous session
    const _savedDraft = localStorage.getItem("chat_draft");
    if (_savedDraft) {
        msgInput.innerText = _savedDraft;
        const _rng = document.createRange();
        const _sel = window.getSelection();
        _rng.selectNodeContents(msgInput);
        _rng.collapse(false);
        _sel.removeAllRanges();
        _sel.addRange(_rng);
    }
    msgInput.addEventListener("input", () => {
        const _t = msgInput.innerText.trim();
        if (_t) localStorage.setItem("chat_draft", _t);
        else    localStorage.removeItem("chat_draft");
    }, { passive: true });

    messagesEl.addEventListener("scroll", onMessagesScroll, { passive: true });

    // [PERF] Delegated swipe-to-reply listeners: one listener per event type instead of 3 per message.
    messagesEl.addEventListener("touchstart", e => {
        const msgEl = e.target.closest(".message");
        if (msgEl) {
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        }
    }, { passive: true });

    messagesEl.addEventListener("touchend", e => {
        const msgEl = e.target.closest(".message");
        if (!msgEl) return;
        const dx = e.changedTouches[0].clientX - touchStartX;
        const dy = e.changedTouches[0].clientY - touchStartY;
        if (Math.abs(dy) > Math.abs(dx)) return;
        const isOwn = msgEl.classList.contains("own-message");
        if (!isOwn && dx >  55) setReply(msgEl);
        if ( isOwn && dx < -55) setReply(msgEl);
    }, { passive: true });

    messagesEl.addEventListener("dblclick", e => {
        // Drop synthesized double-taps from touch devices — swipe is the touch gesture.
        if (navigator.maxTouchPoints > 0) return;
        const msgEl = e.target.closest(".message");
        if (msgEl) setReply(msgEl);
    });

    // [REFACTOR] One delegated click handler for the whole message list.
    // Replaces the inline `onclick=""` attributes on media/copy/reply nodes
    // (which required inline JS and were also XSS-adjacent).
    messagesEl.addEventListener("click", e => {
        // Copy button
        const copyBtn = e.target.closest(".copy-btn");
        if (copyBtn) {
            e.stopPropagation();
            copyMessageText(copyBtn.closest(".message"));
            return;
        }
        // Reply quote → jump to original
        const replyQuote = e.target.closest(".reply-quote");
        if (replyQuote?.dataset.replyId) {
            e.stopPropagation();
            scrollToMessage(replyQuote.dataset.replyId);
            return;
        }
        // Media (image or video) click → open viewer
        const media = e.target.closest("[data-viewer-src]");
        if (media) {
            e.stopPropagation();
            openMediaViewer(media.dataset.viewerSrc, media.dataset.viewerType || "image");
            return;
        }
        // YouTube bubble thumbnail → click-to-load the live IFrame player,
        // in place. Only fires once per bubble (dataset.ytLoaded guard) —
        // a second click just does nothing here, since at that point it's
        // a real YT.Player with its own controls handling further clicks.
        const ytThumb = e.target.closest(".yt-bubble-thumb-wrap");
        if (ytThumb && !ytThumb.dataset.ytLoaded) {
            e.stopPropagation();
            ytThumb.dataset.ytLoaded = "1";
            const videoId = ytThumb.dataset.ytVideoId;
            if (videoId) {
                const bubbleEl = ytThumb.closest(".bubble-youtube");
                const parentMsgEl = ytThumb.closest(".message");
                mountYouTubePlayer(ytThumb, videoId, {
                    observeVisibility: true,
                    npMeta: {
                        title:     bubbleEl?.querySelector(".yt-bubble-title")?.textContent || "YouTube video",
                        subtitle:  bubbleEl?.querySelector(".yt-bubble-channel")?.textContent || "",
                        thumbnail: bubbleEl?.querySelector(".yt-bubble-thumb-wrap img")?.src || null,
                        msgId:     parentMsgEl?.dataset.msgid || null,
                    },
                });
            }
            return;
        }
    });

    msgInput.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); return; }
        // Debounced typing event — fire at most once every 2 s while the user is typing.
        if (socket && socket.readyState === 1 && !_typingDebounce) {
            socket.send(JSON.stringify({ type: "typing", user: username }));
            _typingDebounce = setTimeout(() => { _typingDebounce = null; }, 2000);
        }
    });

    document.addEventListener("keydown", e => {
        if (e.key === "Escape") {
            const viewer       = $("media-viewer");
            const gifPanel     = $("gif-manager-panel");
            const stickerPanel = $("sticker-manager-panel");
            const musicPanel   = $("music-manager-panel");
            if (viewer?.style.display === "flex")       { closeMediaViewer(null); return; }
            if (gifPanel?.style.display === "flex")     { gifPanel.style.display = "none"; $("message-input")?.focus(); return; }
            if (stickerPanel?.style.display === "flex") { stickerPanel.style.display = "none"; $("message-input")?.focus(); return; }
            if (musicPanel?.style.display === "flex")   { closeMusicDrawer(); return; }
            clearAttachments();
            clearReply();
            return;
        }
        if (e.altKey && e.key.toLowerCase() === "g") { e.preventDefault(); toggleGifDrawer();     return; }
        if (e.altKey && e.key.toLowerCase() === "s") { e.preventDefault(); toggleStickerDrawer(); return; }
        if (e.altKey && e.key.toLowerCase() === "m") { e.preventDefault(); toggleMusicDrawer();   return; }
        if (e.altKey && e.key.toLowerCase() === "a") { e.preventDefault(); $("file-input")?.click(); return; }
        // [QoL] Any printable character while no input is focused → route to message box
        if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
            const act = document.activeElement;
            const alreadyTyping = act && (
                act.tagName === "INPUT" || act.tagName === "TEXTAREA" ||
                act.contentEditable === "true"
            );
            const panelOpen =
                $("gif-manager-panel")?.style.display     === "flex" ||
                $("sticker-manager-panel")?.style.display === "flex" ||
                $("music-manager-panel")?.style.display   === "flex";
            if (!alreadyTyping && !panelOpen) $("message-input")?.focus();
        }
    });

    // [FIX] SINGLE paste handler. The old `beforeinput` handler is deleted:
    // it read files from `e.dataTransfer` and called handleFiles(), then the
    // `paste` handler ran immediately after and called handleFiles() AGAIN
    // for the same clipboard payload. That was the duplicate-upload bug.
    // The lone `paste` handler below covers every case (image copy, file
    // copy, plain-text paste). handleFiles() itself now dedupes as a
    // defence-in-depth guard.
    msgInput.addEventListener("paste", (e) => {
        const clipboardData = e.clipboardData || window.clipboardData;
        if (!clipboardData) return;

        // Collect files (deduped by fingerprint inside handleFiles).
        const files = [];
        if (clipboardData.files && clipboardData.files.length) {
            for (const f of clipboardData.files) files.push(f);
        } else if (clipboardData.items) {
            for (const item of clipboardData.items) {
                if (item.kind === "file") {
                    const f = item.getAsFile();
                    if (f) files.push(f);
                }
            }
        }

        if (files.length > 0) {
            // Files trump text: consume the event, upload the files.
            e.preventDefault();
            handleFiles(files);
            return;
        }

        // Plain-text paste — strip formatting to keep the input clean.
        const text = clipboardData.getData("text");
        if (text != null) {
            e.preventDefault();
            document.execCommand("insertText", false, text);
        }
    });

    $("file-input").addEventListener("change", e => {
        if (e.target.files.length > 0) handleFiles(Array.from(e.target.files));
        e.target.value = "";
    });

    initAttachmentReorder();

    connectWebSocket();
    requestNotifPermission();  // [QoL]
}

// ── media viewer ──────────────────────────────────────────────────────────────
function openMediaViewer(src, type, opts = {}) {
    const viewer = $("media-viewer");
    const img    = $("viewer-image");
    const vid    = $("viewer-video");
    // [SEC] safeMediaURL exists to stop a hostile filename/message from
    // smuggling javascript:/data: content into a live element — that
    // matters for anything sourced from message data (another user's
    // filename, server data). It does NOT apply to blob:/data: URLs we
    // generated ourselves from a local File object the current user just
    // picked in their own file dialog — those aren't attacker-controlled,
    // but safeMediaURL rejects them anyway (blob:/data: don't match its
    // http(s)/same-origin-path allowlist), which silently no-ops the
    // whole viewer. opts.trustedLocal is how the two attachment-preview
    // call sites (staged image dataURL, staged video blobURL) opt out of
    // that check specifically — every message-click call site is
    // unchanged and still goes through the full check.
    const safe = opts.trustedLocal ? src : safeMediaURL(src);
    if (!safe) return;

    if (type === "video") {
        img.style.display = "none";
        img.src = "";
        vid.style.display = "block";
        vid.src = safe;
        NowPlaying.interrupt();
        vid.play().catch(() => {});
    } else {
        vid.style.display = "none";
        vid.pause();
        vid.src = "";
        img.style.display = "block";
        img.src = safe;
    }
    _viewerScale  = 1;
    _viewerTransX = 0;
    _viewerTransY = 0;
    _viewerDragOccurred = false;
    img.draggable = false;
    img.style.transform = "translate(0px,0px) scale(1)";
    viewer.style.display = "flex";
}

// Legacy alias — some templates may still reference this global.
function openImageViewer(src) { openMediaViewer(src, "image"); }

function closeMediaViewer(e) {
    // If the user just panned, the mouseup fires a click on the overlay —
    // swallow it so the viewer stays open.
    if (_viewerDragOccurred) { _viewerDragOccurred = false; return; }
    if (e && e.target.id === "viewer-video") return;
    $("media-viewer").style.display = "none";
    const img = $("viewer-image");
    img.src = "";
    _viewerScale  = 1;
    _viewerTransX = 0;
    _viewerTransY = 0;
    img.style.transform = "translate(0px,0px) scale(1)";
    const vid = $("viewer-video");
    vid.pause();
    vid.src = "";
}

// ── buildMessageEl ────────────────────────────────────────────────────────────
// [SEC] Every user-supplied value is now escaped or DOM-inserted safely.
// [REFACTOR] Split into a few smaller builders so this function reads top-to-bottom
// as a single flow, matching the mental model of "what kind of bubble am I building?"
function buildMessageEl(msg, prevUser) {
    if (msg.type === "system") return buildSystemMessageEl(msg);
    const div = document.createElement("div");
    div.className = "message";

    const msgId = msg.id || (msg.timestamp + "_" + msg.user);
    div.dataset.msgid = msgId;
    div.dataset.user  = msg.user;
    div.dataset.ts    = msg.timestamp || "";

    const sameUser = prevUser === msg.user;
    if (msg.user === username) div.classList.add("own-message");
    if (sameUser)              div.classList.add("grouped");

    const shortTime = msg.time ? msg.time.slice(0, 5) : "";
    const replyHTML = buildReplyQuoteHTML(msg.replyTo);

    let bubbleHTML;
    if (msg.type === "file") {
        bubbleHTML = buildFileBubbleHTML(msg, replyHTML, shortTime, div);
    } else if (msg.type === "youtube") {
        bubbleHTML = buildYouTubeBubbleHTML(msg, replyHTML, shortTime, div);
    } else if (msg.type === "ytdlp_audio") {
        bubbleHTML = buildYtdlpAudioBubbleHTML(msg, replyHTML, shortTime, div);
    } else {
        div.dataset.previewText = msg.text || "";
        // [SEC] Text runs through formatMessage which escapes everything except the URL anchors it inserts itself.
        bubbleHTML =
            `<div class="bubble">` + replyHTML +
            `<span class="bubble-text">${formatMessage(msg.text)}</span>` +
            `<span class="timestamp">${escapeHTML(shortTime)}</span>` +
            // No inline onclick — delegated handler in showChat() dispatches this.
            // [REBOOT] Copy affordance is an SVG icon. The icon swaps to a
            // check SVG on successful copy (handled in copyMessageText).
            `<button class="copy-btn" title="Copy message" aria-label="Copy message">` +
              `<span class="icon copy"></span>` +
            `</button>` +
            `</div>`;
    }

    const usernameHtml = sameUser
        ? ""
        : `<div class="username" style="color:${userColor(msg.user)}">${escapeHTML(msg.user)}</div>`;
    div.innerHTML = usernameHtml + bubbleHTML;
    div.querySelectorAll("img.lazy-img").forEach(observeLazyImg);
    // [PERF] Attach the visibility controller to every animated element in
    // this subtree. One entry point covers chat GIFs, sticker GIFs/WebPs,
    // and video stickers — both live messages and history-render paths use this.
    AnimatedMedia.scan(div);
    return div;
}

function buildReplyQuoteHTML(reply) {
    if (!reply) return "";
    // [SEC] All fields escaped; mediaSrc validated through safeMediaURL.
    const safeUser = escapeHTML(reply.user || "");
    const safeId   = escapeHTML(reply.id   || "");
    const color    = userColor(reply.user || "");

    let content;
    if (reply.mediaSrc) {
        const tag = reply.mediaTag === "video" ? "video" : "img";
        const src = safeMediaURL(reply.mediaSrc);
        content = `<${tag} src="${escapeHTML(src)}" style="height:32px;width:32px;border-radius:4px;object-fit:cover;"></${tag}>`;
    } else {
        content = `<span class="reply-quote-text">${escapeHTML((reply.text || "").slice(0, 60))}</span>`;
    }
    return `<div class="reply-quote" data-reply-id="${safeId}">` +
           `<span class="reply-quote-user" style="color:${color}">${safeUser}</span>` +
           content +
           `</div>`;
}

// ── YouTube (IFrame embed) bubble ───────────────────────────────────────
// At rest: static thumbnail + play overlay only — zero iframe, zero
// decode. The click that mounts a live player is delegated (see the
// messagesEl click handler in showChat()), matching how copy/reply/
// media-viewer clicks are already handled for every other bubble type,
// rather than an inline onclick baked into this HTML string.
function buildYouTubeBubbleHTML(msg, replyHTML, shortTime, div) {
    div.dataset.previewText = msg.title || "YouTube video";
    const safeTitle   = escapeHTML(msg.title || "Untitled");
    const safeChannel = escapeHTML(msg.channel || "");
    const safeTime    = escapeHTML(shortTime);
    const videoId     = escapeHTML(msg.videoId || "");

    // Prefer the thumbnail URL captured at send time (from the search
    // result); fall back to the predictable img.youtube.com URL if it's
    // ever missing, so an old/malformed message still renders something.
    const rawThumb = msg.thumbnailUrl ||
        (msg.videoId ? `https://img.youtube.com/vi/${encodeURIComponent(msg.videoId)}/hqdefault.jpg` : "");
    const thumbUrl = safeMediaURL(rawThumb);

    return `<div class="bubble-youtube">` + replyHTML +
        `<div class="yt-bubble-thumb-wrap" data-yt-video-id="${videoId}">` +
        `<img src="${escapeHTML(thumbUrl)}" alt="" loading="lazy">` +
        `<div class="yt-bubble-play-overlay"><span class="icon play"></span></div>` +
        `</div>` +
        `<div class="yt-bubble-meta">` +
        `<span class="yt-bubble-title">${safeTitle}</span>` +
        `<div class="yt-bubble-channel-row">` +
        `<span class="yt-bubble-channel">${safeChannel}</span>` +
        `<span class="timestamp">${safeTime}</span>` +
        `</div></div></div>`;
}

// ── yt-dlp audio bubble ─────────────────────────────────────────────────
// Reuses the EXISTING .bubble-audio / .chat-audio styling verbatim (no
// new CSS) — same shape as an uploaded local audio file, just sourced
// from /api/music/stream/<id> instead of /uploads/<filename>.
// preload="none" means that route (a real server-side yt-dlp resolution
// call) isn't hit until this specific bubble's play button is pressed —
// same click-gated-cost discipline as the embed bubble above, enforced
// here by the browser's own <audio preload> semantics instead of a
// custom mount function.
function buildYtdlpAudioBubbleHTML(msg, replyHTML, shortTime, div) {
    const title = msg.title || "Unknown Title";
    div.dataset.previewText = title;
    const safeChannel = escapeHTML(msg.channel || "");
    const safeTime     = escapeHTML(shortTime);
    const videoId      = encodeURIComponent(msg.videoId || "");
    const nameLabel     = escapeHTML(title) + (safeChannel ? " — " + safeChannel : "");
    // thumbnailUrl is already sent/stored on every ytdlp_audio message (see
    // sendYtdlpShareInstantly) but wasn't used anywhere visually until now —
    // it gives the island a real thumbnail instead of the generic equalizer
    // fallback, for free.
    const npThumb = safeMediaURL(msg.thumbnailUrl || "");
    const npThumbAttr = npThumb ? ` data-np-thumb="${escapeHTML(npThumb)}"` : "";

    return `<div class="bubble bubble-audio">` + replyHTML +
        `<div class="audio-row"><div class="audio-info">` +
        `<span class="audio-name">${nameLabel}</span>` +
        `<audio src="/api/music/stream/${videoId}" controls class="chat-audio" preload="none" ` +
        `data-ytdlp-status-label="Resolving sent audio…" ` +
        `data-np-kind="ytdlp" data-np-title="${escapeHTML(title)}" data-np-subtitle="${safeChannel}"${npThumbAttr}></audio>` +
        `</div></div>` +
        `<div class="timestamp timestamp-block">${safeTime}</div>` +
        `</div>`;
}

function buildFileBubbleHTML(msg, replyHTML, shortTime, div) {
    const displayName = msg.filename.replace(/^\d{8}_\d{6}_/, "");
    div.dataset.previewText = displayName;

    const isImage = /\.(png|jpe?g|gif|webp)$/i.test(msg.filename) || msg.filename.startsWith("http");
    const isVideo = /\.(mp4|webm|mov|mkv)$/i.test(msg.filename);
    const isAudio = /\.(mp3|flac|ogg|wav|m4a|aac)$/i.test(msg.filename);

    const captionHtml = msg.caption
        ? `<div class="bubble-caption">${formatMessage(msg.caption)}</div>`
        : "";
    const safeName   = escapeHTML(displayName);
    const safeTime   = escapeHTML(shortTime);
    // [SEC] safeMediaURL guarantees http(s) or same-origin path.
    const rawSrc     = msg.filename.startsWith("http") ? msg.filename : `/uploads/${msg.filename}`;
    const finalSrc   = safeMediaURL(rawSrc);
    const finalSrcE  = escapeHTML(finalSrc);

    if (msg.isSticker) {
        const isVideoSticker = /\.(mp4|webm|mov)$/i.test(msg.filename);
        // [LOOPED-GIF] Animated WebP also gets `.is-gif-element`. WebP can be
        // static or animated; the safe default for a sticker is "assume it might
        // animate" (a static one just plays 0 frames and stops immediately, which
        // is invisible to the user, so we lose nothing by treating both alike).
        const isAnimatedSticker = /\.(gif|webp)$/i.test(msg.filename);
        let stickerMedia;
        if (isVideoSticker) {
            stickerMedia = `<video src="${finalSrcE}" class="chat-sticker" autoplay muted playsinline loop></video>`;
        } else if (isAnimatedSticker) {
            stickerMedia =
                `<div class="gif-container">` +
                `<img src="${finalSrcE}" class="chat-sticker is-gif-element" ` +
                `data-viewer-src="${finalSrcE}" data-viewer-type="image" loading="lazy">` +
                `</div>`;
        } else {
            stickerMedia =
                `<img src="${finalSrcE}" class="chat-sticker" ` +
                `data-viewer-src="${finalSrcE}" data-viewer-type="image" loading="lazy">`;
        }
        return `<div class="bubble bubble-sticker">` + replyHTML +
               stickerMedia +
               `<div class="timestamp timestamp-block">${safeTime}</div>` +
               `</div>`;
    }

    if (isImage) {
        const isGif = /\.(gif|webp)$/i.test(msg.filename) || msg.filename.includes("giphy.com");
        if (isGif) {
            return `<div class="bubble bubble-file">` + replyHTML +
                   `<div class="gif-container">` +
                   `<img data-src="${finalSrcE}" src="" class="chat-image lazy-img is-gif-element" ` +
                   `data-viewer-src="${finalSrcE}" data-viewer-type="image" decoding="async">` +
                   `</div>` + captionHtml +
                   `<div class="timestamp timestamp-block">${safeTime}</div>` +
                   `</div>`;
        }
        return `<div class="bubble bubble-file">` + replyHTML +
               `<img data-src="${finalSrcE}" src="" class="chat-image lazy-img" ` +
               `data-viewer-src="${finalSrcE}" data-viewer-type="image" decoding="async">` +
               captionHtml +
               `<div class="timestamp timestamp-block">${safeTime}</div>` +
               `</div>`;
    }

    if (isVideo) {
        // [PERF] No <video preload="metadata"> — the placeholder is text-only.
        // Media element is created only when the viewer opens.
        // [REBOOT] Play glyph swapped for an SVG icon. Layout/styling moved to
        // style.css so the reboot theme fully governs the video card.
        return `<div class="bubble bubble-file">` + replyHTML +
               `<div class="video-thumbnail-container" ` +
                    `data-viewer-src="${finalSrcE}" data-viewer-type="video">` +
                    `<span class="icon play xl"></span>` +
                    `<span class="video-name">${safeName}</span>` +
               `</div>` +
               captionHtml +
               `<div class="timestamp timestamp-block">${safeTime}</div>` +
               `</div>`;
    }

    if (isAudio) {
        return `<div class="bubble bubble-audio">` + replyHTML +
               `<div class="audio-row">` +
               `<div class="audio-info">` +
               `<span class="audio-name">${safeName}</span>` +
               `<audio src="${finalSrcE}" controls class="chat-audio" preload="none" ` +
               `data-np-kind="audio" data-np-title="${safeName}" data-np-subtitle="${escapeHTML(msg.user || "")}"></audio>` +
               `</div></div>` + captionHtml +
               `<div class="timestamp timestamp-block">${safeTime}</div>` +
               `</div>`;
    }

    // Generic file link — [REBOOT] file glyph replaced by SVG icon.
    return `<div class="bubble">` + replyHTML +
           `<div class="file-block">` +
           `<a href="${finalSrcE}" target="_blank" rel="noopener noreferrer" class="file-link">` +
             `<span class="icon file sm"></span>` +
             `<span>${safeName}</span>` +
           `</a>` +
           captionHtml + `</div><span class="timestamp">${safeTime}</span>` +
           `</div>`;
}

function appendMessage(msg) {
    // De-dupe by ID — server can legitimately replay a message on reconnect.
    if (msg.id && document.querySelector(`[data-msgid="${CSS.escape(msg.id)}"]`)) return;

    const messagesEl = $("messages");
    // [FIX] Snapshot the at-bottom state BEFORE we touch the DOM, and
    // remember it for the own-message branch below. Previously we relied on
    // `isOwn` as a synonym for "always yank to bottom" — that was the bug.
    const atBottom   = isNearBottom(messagesEl);
    const isSystem   = msg.type === "system";
    const isOwn      = msg.user === username;

    // If trimDOMBottom previously evicted real content from the tail, the
    // DOM no longer reaches the live end of the conversation. Appending
    // straight to the DOM's current end would place this brand-new message
    // directly after stale, unrelated content with no indication of the
    // gap between them. Count it as unread (unless it's own) instead —
    // loadNewerMessages will render it correctly, in order, once the user
    // scrolls through.
    if (!noMoreNewer) {
        if (!isSystem && !isOwn) Unread.countPending(msg);
        return;
    }

    // [FIX] The old code inserted a separator here based on
    // `lastRenderedDateKey !== dk`, which could be wrong for the reasons
    // documented on `reconcileDateSeparators`. We still don't insert a
    // separator here — the reconciler remains the sole authority on
    // whether/where a .date-separator exists, avoiding the false-positive
    // duplicate a stale tracker could otherwise inject straight into the
    // live DOM. This is what fixes the mid-day "TODAY" glitch and the
    // midnight-crossing bug in one place.
    //
    // [MERGE] The tracker IS still good enough for one thing: knowing
    // whether THIS message should visually group with the previous one
    // (same-user, no separator between them → hide the username line).
    // Trusting `lastRenderedDateKey` for that inherited the same staleness
    // risk though — a false negative there silently drops the username off
    // the first message of a new day. Fixed by reading the actual
    // last-rendered .message element's own timestamp instead of any
    // tracker (skipping the divider node if it's currently sitting last).
    // Same "trust the DOM, not a variable" principle as the reconciler,
    // applied to this one synchronous decision — O(1) in practice, and
    // skipped entirely for system messages since buildMessageEl never
    // reads prevUser for those.
    const dk = dateKey(msg.timestamp);
    let isNewDate = false;
    if (!isSystem) {
        let priorNode = messagesEl.lastElementChild;
        while (priorNode && !priorNode.classList.contains("message")) {
            priorNode = priorNode.previousElementSibling;
        }
        const priorDk = dateKey(priorNode ? priorNode.dataset.ts : "");
        isNewDate = dk && dk !== priorDk;
    }

    const el = buildMessageEl(msg, isNewDate ? null : lastRenderedUser);
    messagesEl.appendChild(el);
    wireAudioEnhancements(el);
    if (!isSystem) lastRenderedUser = msg.user;

    // Update lastRenderedDateKey to the actual last message's date so any
    // OTHER code path that still reads it (paranoia — the reconciler is
    // authoritative for the DOM) stays in sync.
    if (dk) lastRenderedDateKey = dk;

    // Fire the reconciler for THIS insertion. Coalesced across bursts.
    scheduleReconcile();

    // [FIX] Auto-scroll rules — redesigned to stop yanking the user.
    //
    //   • System messages (joins / leaves): always scroll to bottom. They're
    //     transient presence signals and disrupting scroll for them is fine
    //     because they're brief.
    //   • Remote messages: only auto-scroll if the user was already near the
    //     bottom (the ongoing-chat case). Otherwise they're preserved as
    //     unread and the scroll-to-bottom button gains a count.
    //   • OWN messages sent WHILE at the bottom: scroll to bottom, same as
    //     remote-at-bottom — this keeps the natural conversation feel.
    //   • OWN messages sent while SCROLLED UP: DO NOT yank the user. This
    //     is the frustrating behavior we're killing. Instead:
    //       - stay put visually,
    //       - show a subtle affordance on the scroll-to-bottom button so
    //         they know their message went through and where it landed.
    //
    // The at-bottom snapshot was captured before insertion so the newly-
    // appended element's height doesn't tip the calculation false.
    const shouldFollow = atBottom;

    if (shouldFollow) {
        if (!isSystem) trimDOMTop(messagesEl);
        messagesEl.scrollTop = messagesEl.scrollHeight;

        // Stickers can grow after load — pin to bottom once media settles.
        if (!isSystem && msg.isSticker) {
            const forceScrollDown = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };
            el.querySelectorAll("img.chat-sticker").forEach(img =>
                img.addEventListener("load", forceScrollDown, { once: true }));
            el.querySelectorAll("video.chat-sticker").forEach(vid =>
                vid.addEventListener("loadedmetadata", forceScrollDown, { once: true }));
        }

        // [FIX] Following implies the user is now caught up.
        const ts = Date.parse(msg.timestamp);
        if (Number.isFinite(ts)) Unread.markSeenUpTo(ts);
    } else if (isSystem) {
        // Invisible (display:none) system events never contribute to
        // unread state while scrolled up — nothing rendered to catch up
        // on, so no pill/badge/pending-count bump either.
    } else if (isOwn) {
        // [FIX] Own message while scrolled up: don't yank. Do reveal the
        // scroll-to-bottom pill so they see a clear "you sent one, tap to
        // catch up" signal instead of silently dropping their message below
        // the fold. We count it in pendingBelow so the pill's number reflects
        // reality ("1 new message" pointing at their own send).
        Unread.pendingBelow++;
        showScrollBtn();
        updateTabTitle();
    } else {
        // Remote message while scrolled up — count as unread. Same path as
        // before.
        Unread.countPending(msg);
    }
}

// ── GIF PANEL MANAGER ──────────────────────────────────────────────────────────
function toggleGifDrawer() {
    const panel = $("gif-manager-panel");
    if (!panel) return;
    if (panel.style.display === "none") {
        openGifDrawer();
    } else {
        closeGifDrawer();
    }
}

function openGifDrawer() {
    const panel = $("gif-manager-panel");
    const stickerPanel = $("sticker-manager-panel");
    const musicPanel = $("music-manager-panel");
    const messagesEl = $("messages");
    if (!panel) return;

    preserveScrollAcrossResize(messagesEl, () => {
        if (stickerPanel) stickerPanel.style.display = "none";
        if (musicPanel) musicPanel.style.display = "none";
        panel.style.display = "flex";
    });

    if (currentGifTab === "local") loadLocalGifs();

    setTimeout(() => {
        const searchInput = $("giphy-search-input");
        if (currentGifTab === "giphy" && searchInput) {
            searchInput.focus();
        } else {
            const firstFolder = document.querySelector("#folder-list-container [tabindex='0']");
            if (firstFolder) firstFolder.focus();
        }
    }, 80);
}

// [FIX] Explicit close, distinct from the toggle. `stageRemoteGiphyAsFileObject`
// needs a way to guarantee "closed" regardless of current state — calling the
// toggle from inside an async success/error path was reading stale state and
// could reopen an already-closed panel instead of closing it.
function closeGifDrawer() {
    const panel = $("gif-manager-panel");
    const messagesEl = $("messages");
    if (!panel || panel.style.display === "none") return;

    preserveScrollAcrossResize(messagesEl, () => {
        panel.style.display = "none";
    });

    const gifGrid = $("gif-grid");
    const giphyGrid = $("giphy-grid");
    if (gifGrid) gifGrid.innerHTML = "";
    if (giphyGrid) giphyGrid.innerHTML = "";

    $("message-input")?.focus();
}

function switchGifTab(tabName) {
    currentGifTab = tabName;
    const localView       = $("engine-local-view");
    const giphyView       = $("engine-giphy-view");
    const localBtn        = $("tab-local-btn");
    const giphyBtn        = $("tab-giphy-btn");
    const manualUploadBtn = $("gif-manual-upload-btn");

    const activeStyle   = "background:#2d2d2d;color:#fff;border:1px solid #444;padding:5px 14px;cursor:pointer;border-radius:4px;font-weight:500;";
    const inactiveStyle = "background:transparent;color:#888;border:1px solid transparent;padding:5px 14px;cursor:pointer;border-radius:4px;font-weight:500;";

    if (tabName === "local") {
        localView.style.display = "flex";
        giphyView.style.display = "none";
        if (manualUploadBtn) manualUploadBtn.style.display = "flex";
        localBtn.style.cssText = activeStyle;
        giphyBtn.style.cssText = inactiveStyle;
        loadLocalGifs();
    } else {
        localView.style.display = "none";
        giphyView.style.display = "flex";
        if (manualUploadBtn) manualUploadBtn.style.display = "none";
        giphyBtn.style.cssText = activeStyle;
        localBtn.style.cssText = inactiveStyle;
        $("giphy-search-input")?.focus();
    }
}

async function loadLocalGifs() {
    try {
        const res = await fetch("/api/gifs");
        localGifCache = await res.json();

        if (Object.keys(localGifCache).length > 0 && !localGifCache[currentActiveFolder]) {
            currentActiveFolder = Object.keys(localGifCache)[0];
            localStorage.setItem("last_gif_folder", currentActiveFolder);
        }
        renderFolderList();
        renderActiveGifGrid();
    } catch (_) {}
}

function renderFolderList() {
    const container = $("folder-list-container");
    if (!container) return;
    container.innerHTML = "";

    if (Object.keys(localGifCache).length === 0) localGifCache["general"] = [];

    Object.keys(localGifCache).forEach(folder => {
        const btn = document.createElement("div");
        const isActive = folder === currentActiveFolder;
        // [SEC][REBOOT] Icon uses the CSS icon system (mask-image → currentColor).
        // Folder name goes through textContent — never innerHTML.
        const icon = document.createElement("span");
        icon.className = "icon folder sm";
        icon.style.marginRight = "8px";
        icon.style.verticalAlign = "middle";
        const label = document.createElement("span");
        label.style.verticalAlign = "middle";
        label.textContent = folder;
        btn.appendChild(icon);
        btn.appendChild(label);
        btn.style.cssText = `display:flex;align-items:center;padding:7px 10px;margin:3px 0;cursor:pointer;font-size:13px;border-radius:8px;transition:background 120ms ease,color 120ms ease,border-color 120ms ease;border:1px solid ${isActive ? "#3a3a3a" : "transparent"};color:${isActive ? "#f5f5f5" : "#7a7a7a"};background:${isActive ? "#1a1a1a" : "transparent"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;

        btn.tabIndex = 0;
        btn.setAttribute("role", "button");
        btn.title = folder;
        btn.addEventListener("click", () => {
            currentActiveFolder = folder;
            localStorage.setItem("last_gif_folder", folder);
            renderFolderList();
            renderActiveGifGrid();
        });
        btn.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                btn.click();
                setTimeout(() => {
                    document.querySelector("#gif-grid img[tabindex='0']")?.focus();
                }, 60);
            }
        });
        container.appendChild(btn);
    });
}

function renderActiveGifGrid() {
    const grid = $("gif-grid");
    if (!grid) return;
    grid.innerHTML = "";

    const assets = localGifCache[currentActiveFolder] || [];
    if (assets.length === 0) {
        grid.innerHTML = `<div style="color:#484f58;font-size:12px;grid-column:1/-1;text-align:center;padding:30px;">Empty folder scope.<br>Drop assets here to build storage.</div>`;
        return;
    }

    const GRID_STYLE = "width:100%;height:95px;object-fit:cover;cursor:pointer;border-radius:6px;background:#0d1117;border:1px solid #30363d;transition:transform 0.1s;outline-offset:2px;";

    assets.forEach(filename => {
        const img = document.createElement("img");
        const assetUrl = `/gifs/${encodeURIComponent(currentActiveFolder)}/${encodeURIComponent(filename)}`;
        img.src = assetUrl;
        img.loading = "lazy";
        img.tabIndex = 0;
        const display = filename.replace(/\.[^.]+$/, "").replace(/[-_]/g, " ");
        img.setAttribute("aria-label", display);
        img.title = display;
        img.style.cssText = GRID_STYLE;
        img.addEventListener("mouseenter", () => img.style.transform = "scale(1.03)");
        img.addEventListener("mouseleave", () => img.style.transform = "scale(1.0)");
        img.addEventListener("click", () => stageLocalGifAsFileObject(assetUrl, filename));
        img.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stageLocalGifAsFileObject(assetUrl, filename); }
        });
        grid.appendChild(img);
    });
}

async function stageLocalGifAsFileObject(url, filename) {
    try {
        const response = await fetch(url);
        const blob     = await response.blob();
        const file     = new File([blob], filename, { type: blob.type || "image/gif" });

        clearAttachments();
        attachmentFiles.push(file);
        addPreviewItem(file);
        toggleGifDrawer();
        $("message-input")?.focus();
    } catch (_) {
        showToast("Could not stage GIF.");
    }
}

async function handleManualGifSelection(inputEl) {
    if (!inputEl.files || inputEl.files.length === 0) return;
    for (const file of inputEl.files) {
        await uploadGifAssetBinary(file);
    }
    inputEl.value = "";
}

async function uploadGifAssetBinary(fileObj) {
    const dropzone = $("gif-dropzone");
    const oldText = dropzone ? dropzone.textContent : "";
    if (dropzone) dropzone.textContent = "Verifying data constraints...";

    const formData = new FormData();
    formData.append("file", fileObj);
    formData.append("folder", currentActiveFolder);

    try {
        const res = await fetch("/api/gifs/upload", { method: "POST", body: formData });
        const data = await res.json();
        if (data.error) showToast(data.error);
    } catch (_) {
        showToast("Upload network transaction rejected.");
    }

    if (dropzone) dropzone.textContent = oldText;
    loadLocalGifs();
}

async function searchGiphy() {
    const query = $("giphy-search-input").value.trim();
    const grid  = $("giphy-grid");
    if (!grid) return;
    if (!query) { grid.innerHTML = ""; return; }

    grid.innerHTML = `<div style="color:#8b949e;font-size:12px;grid-column:1/-1;text-align:center;padding:30px;">Querying cloud channels...</div>`;

    try {
        const apiKey = "93jPMQfDg5ob5IJXf0Gp0YTWvF0rNSqd";
        const response = await fetch(`https://api.giphy.com/v1/gifs/search?q=${encodeURIComponent(query)}&api_key=${apiKey}&limit=18`);
        const result   = await response.json();

        grid.innerHTML = "";
        if (!result.data || result.data.length === 0) {
            grid.innerHTML = `<div style="color:#484f58;font-size:12px;grid-column:1/-1;text-align:center;padding:30px;">No match indices returned.</div>`;
            return;
        }

        const GRID_STYLE = "width:100%;height:95px;object-fit:cover;cursor:pointer;border-radius:6px;background:#0d1117;border:1px solid #30363d;transition:transform 0.1s;outline-offset:2px;";

        result.data.forEach(item => {
            const img = document.createElement("img");
            img.src = item.images.fixed_height_small.url;
            img.loading = "lazy";
            img.tabIndex = 0;
            img.setAttribute("aria-label", item.title || "giphy gif");
            img.title = item.title || "";
            img.style.cssText = GRID_STYLE;
            img.addEventListener("mouseenter", () => img.style.transform = "scale(1.03)");
            img.addEventListener("mouseleave", () => img.style.transform = "scale(1.0)");
            img.addEventListener("click", () => stageRemoteGiphyAsFileObject(item.images.original.url));
            img.addEventListener("keydown", e => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stageRemoteGiphyAsFileObject(item.images.original.url); }
            });
            grid.appendChild(img);
        });
        setTimeout(() => { grid.querySelector("img[tabindex='0']")?.focus(); }, 50);
    } catch (_) {
        grid.innerHTML = `<div style="color:#f85149;font-size:12px;grid-column:1/-1;text-align:center;padding:30px;">API handshake fault.</div>`;
    }
}

// [FIX] The original version awaited the full fetch+blob download BEFORE
// closing the panel or showing any feedback — on a slow mobile connection
// the user taps a GIF and sees nothing happen for up to a few seconds.
// Also, the error path called the toggleGifDrawer() *toggle*, which could
// reopen an already-closed panel depending on interleaving.
//
// Fix: close the panel and render a pending chip synchronously (before any
// `await`), so the click-to-feedback latency is one JS tick, not a network
// round trip. The fetch then resolves into that same chip.
//
// [FIX] Previously called clearAttachments() before staging, silently
// dropping any file/sticker the user had already queued. A GIF pick now
// coexists with existing attachments like every other attachment type does.
// [FIX] Guards double-click/double-tap firing two fetches for the same GIF —
// the grid items don't disable on click, and a fast double-tap is common on
// mobile. Short window only; this is not the general attachment dedupe
// (that's fileFingerprint()/_recentFileFingerprints, which operates on File
// objects that don't exist yet at click-time here).
const _recentGiphyURLs = new Map();
const RECENT_GIPHY_WINDOW_MS = 1200;

function stageRemoteGiphyAsFileObject(targetUrl) {
    const now = Date.now();
    for (const [u, ts] of _recentGiphyURLs) {
        if (now - ts > RECENT_GIPHY_WINDOW_MS) _recentGiphyURLs.delete(u);
    }
    if (_recentGiphyURLs.has(targetUrl)) return;
    _recentGiphyURLs.set(targetUrl, now);

    closeGifDrawer();
    $("message-input")?.focus();

    const token = addPendingGifChip();

    (async () => {
        try {
            let baseName = targetUrl.split("/").pop().split("?")[0];
            if (!baseName.endsWith(".gif")) baseName += ".gif";

            const response = await fetch(targetUrl);
            if (!response.ok) throw new Error(`fetch failed: ${response.status}`);
            const blob = await response.blob();
            const file = new File([blob], baseName, { type: blob.type || "image/gif" });

            if (token != null) resolvePendingGifChip(token, file);
        } catch (_) {
            if (token != null) failPendingGifChip(token);
            showToast("Couldn't load that GIF. Try again?");
        }
    })();
}

// ── STICKER MANAGER (FOLDER SUPPORTED) ──────────────────────────────────
let currentStickerFolder = localStorage.getItem("last_sticker_folder") || "general";
let localStickerCache    = {};

function toggleStickerDrawer() {
    const panel = $("sticker-manager-panel");
    const gifPanel = $("gif-manager-panel");
    const musicPanel = $("music-manager-panel");
    const messagesEl = $("messages");
    if (!panel) return;

    if (panel.style.display === "none") {
        preserveScrollAcrossResize(messagesEl, () => {
            if (gifPanel) gifPanel.style.display = "none";
            if (musicPanel) musicPanel.style.display = "none";
            panel.style.display = "flex";
        });

        loadLocalStickers();

        setTimeout(() => {
            document.querySelector("#sticker-folder-list-container [tabindex='0']")?.focus();
        }, 80);
    } else {
        preserveScrollAcrossResize(messagesEl, () => {
            panel.style.display = "none";
        });

        // [PERF] Clear the sticker grid on close so looping <video> elements
        // don't keep their media pipelines alive while the panel is hidden.
        const grid = $("sticker-grid");
        if (grid) grid.innerHTML = "";

        $("message-input")?.focus();
    }
}
async function loadLocalStickers() {
    try {
        const res = await fetch("/api/stickers/folders");
        const folders = await res.json();

        folders.forEach(f => { if (!localStickerCache[f]) localStickerCache[f] = []; });

        if (!folders.includes(currentStickerFolder)) {
            currentStickerFolder = "general";
            localStorage.setItem("last_sticker_folder", currentStickerFolder);
        }

        renderStickerFolderList(folders);
        loadStickersForFolder(currentStickerFolder);
    } catch (_) {}
}

function renderStickerFolderList(folders) {
    const container = $("sticker-folder-list-container");
    if (!container) return;
    container.innerHTML = "";

    folders.forEach(folder => {
        const btn = document.createElement("div");
        const isActive = folder === currentStickerFolder;
        btn.style.cssText = `display:flex;align-items:center;padding:7px 10px;margin:3px 0;cursor:pointer;font-size:13px;border-radius:8px;border:1px solid ${isActive ? "#3a3a3a" : "transparent"};color:${isActive ? "#f5f5f5" : "#7a7a7a"};background:${isActive ? "#1a1a1a" : "transparent"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:background 120ms ease,color 120ms ease,border-color 120ms ease;`;

        // [SEC][REBOOT] SVG folder icon via the icon system; label via textContent.
        const icon = document.createElement("span");
        icon.className = "icon folder sm";
        icon.style.marginRight = "8px";
        icon.style.verticalAlign = "middle";
        const label = document.createElement("span");
        label.style.verticalAlign = "middle";
        label.textContent = folder;
        btn.appendChild(icon);
        btn.appendChild(label);

        btn.tabIndex = 0;
        btn.setAttribute("role", "button");
        btn.title = folder;
        btn.addEventListener("click", () => {
            currentStickerFolder = folder;
            localStorage.setItem("last_sticker_folder", folder);
            renderStickerFolderList(folders);
            loadStickersForFolder(folder);
        });
        btn.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                btn.click();
                setTimeout(() => {
                    document.querySelector("#sticker-grid [tabindex='0']")?.focus();
                }, 100);
            }
        });
        container.appendChild(btn);
    });
}

async function loadStickersForFolder(folder) {
    try {
        const res = await fetch(`/api/stickers/${encodeURIComponent(folder)}`);
        localStickerCache[folder] = await res.json();
        renderActiveStickerGrid();
    } catch (_) {}
}

function renderActiveStickerGrid() {
    const grid = $("sticker-grid");
    if (!grid) return;
    grid.innerHTML = "";

    const assets = localStickerCache[currentStickerFolder] || [];
    if (assets.length === 0) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:#484f58;font-size:12px;grid-column:1/-1;text-align:center;padding:30px;";
        empty.innerHTML = `No stickers here yet.<br>Click "+ Add Sticker" or drop files.`;
        grid.appendChild(empty);
        return;
    }

    const BASE_STYLE = "width:90px;height:90px;object-fit:contain;cursor:pointer;border-radius:6px;background:#161b22;border:1px solid #30363d;padding:4px;transition:transform 0.1s;";

    assets.forEach(filename => {
        const url     = `/stickers/${encodeURIComponent(currentStickerFolder)}/${encodeURIComponent(filename)}`;
        const isVideo = /\.(mp4|webm|mov)$/i.test(filename);
        let el;

        if (isVideo) {
            el             = document.createElement("video");
            el.src         = url;
            el.autoplay    = true;
            el.loop        = true;
            el.muted       = true;
            el.playsInline = true;
            el.preload     = "none";
            el.style.cssText = BASE_STYLE;
        } else {
            el         = document.createElement("img");
            el.src     = url;
            el.loading = "lazy";
            el.style.cssText = BASE_STYLE + "filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));";
        }

        el.tabIndex = 0;
        const displayName = filename.replace(/\.[^.]+$/, "").replace(/[-_]/g, " ");
        el.setAttribute("aria-label", displayName);
        el.title = displayName;
        el.setAttribute("role", "button");
        el.addEventListener("mouseenter", () => {
            el.style.transform = "scale(1.08)";
            if (isVideo) {
                el.currentTime = 0;
                el.play().catch(() => {});
            }
        });
        el.addEventListener("mouseleave", () => el.style.transform = "scale(1.0)");
        el.addEventListener("click", () => sendStickerInstantly(url, filename));
        el.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sendStickerInstantly(url, filename); }
        });
        grid.appendChild(el);
    });
}

async function handleManualStickerSelection(inputEl) {
    if (!inputEl.files || inputEl.files.length === 0) return;
    for (const file of inputEl.files) {
        await uploadStickerAssetBinary(file);
    }
    inputEl.value = "";
}

async function uploadStickerAssetBinary(fileObj) {
    const dropzone = $("sticker-dropzone");
    const oldText  = dropzone ? dropzone.textContent : "";
    if (dropzone) dropzone.textContent = "Uploading…";

    const fd = new FormData();
    fd.append("file", fileObj);
    fd.append("folder", currentStickerFolder);

    try {
        const res  = await fetch("/api/stickers/upload", { method: "POST", body: fd });
        const data = await res.json();
        if (data.error) showToast(data.error);
    } catch (_) {
        showToast("Sticker upload failed due to network error.");
    }

    if (dropzone) dropzone.textContent = oldText;
    loadLocalStickers();
}

async function sendStickerInstantly(url, filename) {
    if (!socket || socket.readyState !== 1) return;

    try {
        const res  = await fetch(url);
        const blob = await res.blob();

        if (blob.size > 15 * 1024 * 1024) {
            showToast("Sticker too large (max 15 MB).");
            return;
        }

        const file = new File([blob], filename, { type: blob.type });
        const fd   = new FormData();
        fd.append("file", file);
        fd.append("user", username);
        fd.append("isSticker", "true");
        if (replyingTo) fd.append("replyTo", JSON.stringify(replyingTo));

        const uploadRes = await fetch("/upload", { method: "POST", body: fd });
        const data = await uploadRes.json();

        if (data.error) {
            showToast(data.error);
        } else {
            Unread.markOwnMessage();
            clearReply();
            toggleStickerDrawer();
            $("message-input")?.focus();
        }
    } catch (_) {
        showToast("Failed to send sticker.");
    }
}

// ── MUSIC MANAGER (LOCAL / YOUTUBE-EMBED / YT-DLP) ──────────────────────
function toggleMusicDrawer() {
    const panel = $("music-manager-panel");
    if (!panel) return;
    if (panel.style.display === "none") {
        openMusicDrawer();
    } else {
        closeMusicDrawer();
    }
}

function openMusicDrawer() {
    const panel        = $("music-manager-panel");
    const gifPanel     = $("gif-manager-panel");
    const stickerPanel = $("sticker-manager-panel");
    const messagesEl   = $("messages");
    if (!panel) return;

    preserveScrollAcrossResize(messagesEl, () => {
        if (gifPanel) gifPanel.style.display = "none";
        if (stickerPanel) stickerPanel.style.display = "none";
        panel.style.display = "flex";
    });

    if (currentMusicTab === "local") loadLocalMusic();

    setTimeout(() => {
        if (currentMusicTab === "youtube") {
            $("youtube-search-input")?.focus();
        } else if (currentMusicTab === "ytdlp") {
            $("ytdlp-search-input")?.focus();
        } else {
            document.querySelector("#music-folder-list-container [tabindex='0']")?.focus();
        }
    }, 80);
}

// Explicit close, same reasoning as closeGifDrawer: sendYouTubeShareInstantly/
// sendYtdlpShareInstantly call this from inside a click handler and need a
// guaranteed "closed" regardless of current display state, not a toggle
// that could reopen an already-closed panel on stale state.
function closeMusicDrawer() {
    const panel      = $("music-manager-panel");
    const messagesEl = $("messages");
    if (!panel || panel.style.display === "none") return;

    preserveScrollAcrossResize(messagesEl, () => {
        panel.style.display = "none";
    });

    // Wipes any local-list rows AND any mounted preview players (embedded
    // iframe / inline audio) inside the result lists — removing those DOM
    // nodes tears down their playback for free, same reasoning as
    // AnimatedMedia.release() for chat bubbles. The shared local-music
    // preview player is the one exception: it's a standalone JS object,
    // not a node living inside musicGrid, so innerHTML wiping it doesn't
    // touch it — stopAudioPreview() below is what actually silences it.
    const musicGrid   = $("music-grid");
    const youtubeList = $("youtube-results-list");
    const ytdlpList   = $("ytdlp-results-list");
    if (musicGrid)   musicGrid.innerHTML   = "";
    if (youtubeList) youtubeList.innerHTML = "";
    if (ytdlpList)   ytdlpList.innerHTML   = "";
    stopAudioPreview();
    _activePreview = null;

    $("message-input")?.focus();
}

function switchMusicTab(tabName) {
    currentMusicTab = tabName;
    // Leaving local (or arriving from it) shouldn't leave a preview
    // playing invisibly behind whichever tab is now showing.
    stopAudioPreview();
    const localView   = $("music-engine-local-view");
    const youtubeView = $("music-engine-youtube-view");
    const ytdlpView   = $("music-engine-ytdlp-view");
    const localBtn    = $("music-tab-local-btn");
    const youtubeBtn  = $("music-tab-youtube-btn");
    const ytdlpBtn    = $("music-tab-ytdlp-btn");
    const manualUploadBtn = $("music-manual-upload-btn");

    const activeStyle   = "background:#2d2d2d;color:#fff;border:1px solid #444;padding:5px 14px;cursor:pointer;border-radius:4px;font-weight:500;";
    const inactiveStyle = "background:transparent;color:#888;border:1px solid transparent;padding:5px 14px;cursor:pointer;border-radius:4px;font-weight:500;";

    localView.style.display   = tabName === "local"   ? "flex" : "none";
    youtubeView.style.display = tabName === "youtube" ? "flex" : "none";
    ytdlpView.style.display   = tabName === "ytdlp"   ? "flex" : "none";

    localBtn.style.cssText   = tabName === "local"   ? activeStyle : inactiveStyle;
    youtubeBtn.style.cssText = tabName === "youtube" ? activeStyle : inactiveStyle;
    ytdlpBtn.style.cssText   = tabName === "ytdlp"   ? activeStyle : inactiveStyle;

    if (manualUploadBtn) manualUploadBtn.style.display = tabName === "local" ? "flex" : "none";

    if (tabName === "local")        loadLocalMusic();
    else if (tabName === "youtube") $("youtube-search-input")?.focus();
    else if (tabName === "ytdlp")   $("ytdlp-search-input")?.focus();
}

// ── Local tab — mirrors the sticker system's folders+files pattern
// (/api/sfx/folders then /api/sfx/<folder>), not GIF's single nested-tree
// call, because that's the actual shape /api/sfx/* already returns.
async function loadLocalMusic() {
    try {
        const res = await fetch("/api/sfx/folders");
        const folders = await res.json();

        folders.forEach(f => { if (!localMusicCache[f]) localMusicCache[f] = []; });

        if (!folders.includes(currentActiveMusicFolder)) {
            currentActiveMusicFolder = "general";
            localStorage.setItem("last_music_folder", currentActiveMusicFolder);
        }

        renderMusicFolderList(folders);
        loadMusicForFolder(currentActiveMusicFolder);
    } catch (_) {}
}

function renderMusicFolderList(folders) {
    const container = $("music-folder-list-container");
    if (!container) return;
    container.innerHTML = "";

    folders.forEach(folder => {
        const btn = document.createElement("div");
        const isActive = folder === currentActiveMusicFolder;
        btn.style.cssText = `display:flex;align-items:center;padding:7px 10px;margin:3px 0;cursor:pointer;font-size:13px;border-radius:8px;border:1px solid ${isActive ? "#3a3a3a" : "transparent"};color:${isActive ? "#f5f5f5" : "#7a7a7a"};background:${isActive ? "#1a1a1a" : "transparent"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:background 120ms ease,color 120ms ease,border-color 120ms ease;`;

        const icon = document.createElement("span");
        icon.className = "icon folder sm";
        icon.style.marginRight = "8px";
        icon.style.verticalAlign = "middle";
        const label = document.createElement("span");
        label.style.verticalAlign = "middle";
        label.textContent = folder;
        btn.appendChild(icon);
        btn.appendChild(label);

        btn.tabIndex = 0;
        btn.setAttribute("role", "button");
        btn.title = folder;
        btn.addEventListener("click", () => {
            currentActiveMusicFolder = folder;
            localStorage.setItem("last_music_folder", folder);
            renderMusicFolderList(folders);
            loadMusicForFolder(folder);
        });
        btn.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                btn.click();
                setTimeout(() => {
                    document.querySelector("#music-grid [tabindex='0']")?.focus();
                }, 100);
            }
        });
        container.appendChild(btn);
    });
}

async function loadMusicForFolder(folder) {
    try {
        const res = await fetch(`/api/sfx/${encodeURIComponent(folder)}`);
        localMusicCache[folder] = await res.json();
        renderActiveMusicList();
    } catch (_) {}
}

// List, not a grid — audio files have no natural thumbnail, so a grid of
// blank squares would add nothing GIF's grid doesn't already justify
// through actual thumbnails.
function renderActiveMusicList() {
    const grid = $("music-grid");
    if (!grid) return;
    grid.innerHTML = "";
    // A row this rebuild is about to remove could be the one currently
    // previewing (folder switch, re-render after upload, etc.) — stop it
    // rather than leave audio playing with no visible "now playing" row.
    stopAudioPreview();

    const assets = localMusicCache[currentActiveMusicFolder] || [];
    if (assets.length === 0) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:#484f58;font-size:12px;text-align:center;padding:30px;";
        empty.innerHTML = `No music here yet.<br>Click "+ Add Music File" or drop files.`;
        grid.appendChild(empty);
        return;
    }

    assets.forEach(filename => {
        const url = `/sfx/${encodeURIComponent(currentActiveMusicFolder)}/${encodeURIComponent(filename)}`;
        const row = document.createElement("div");
        row.className = "music-local-item";
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        const display = filename.replace(/\.[^.]+$/, "").replace(/[-_]/g, " ");
        row.setAttribute("aria-label", display);
        row.title = display;

        // Preview button — plays this file inline via the shared preview
        // player, does NOT stage/send. Replaces the old static file icon:
        // with just a filename to go on, there was no way to tell what a
        // file actually sounds like without remembering it by name.
        const playBtn = document.createElement("button");
        playBtn.type = "button";
        playBtn.className = "music-local-play-btn";
        playBtn.setAttribute("aria-label", "Preview " + display);
        playBtn.innerHTML = `<span class="icon play sm"></span>`;
        playBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            toggleAudioPreview(url, row, playBtn);
        });
        row.appendChild(playBtn);

        const name = document.createElement("span");
        name.className = "music-local-item-name";
        name.textContent = display;
        row.appendChild(name);

        row.addEventListener("click", () => stageLocalMusicAsFileObject(url, filename));
        row.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stageLocalMusicAsFileObject(url, filename); }
        });
        grid.appendChild(row);
    });
}

// Stages (does not instantly send) — mirrors stageLocalGifAsFileObject,
// including the clearAttachments()-first behavior, per "look at gif panel
// behaviour" applied literally to local mode specifically.
async function stageLocalMusicAsFileObject(url, filename) {
    try {
        const response = await fetch(url);
        const blob     = await response.blob();
        const file     = new File([blob], filename, { type: blob.type || "audio/mpeg" });

        clearAttachments();
        attachmentFiles.push(file);
        addPreviewItem(file);
        toggleMusicDrawer();
        $("message-input")?.focus();
    } catch (_) {
        showToast("Could not stage music file.");
    }
}

async function handleManualMusicSelection(inputEl) {
    if (!inputEl.files || inputEl.files.length === 0) return;
    for (const file of inputEl.files) {
        await uploadMusicAssetBinary(file);
    }
    inputEl.value = "";
}

async function uploadMusicAssetBinary(fileObj) {
    const dropzone = $("music-dropzone");
    const oldText  = dropzone ? dropzone.textContent : "";
    if (dropzone) dropzone.textContent = "Uploading…";

    const fd = new FormData();
    fd.append("file", fileObj);
    fd.append("folder", currentActiveMusicFolder);

    try {
        const res  = await fetch("/api/sfx/upload", { method: "POST", body: fd });
        const data = await res.json();
        if (data.error) showToast(data.error);
    } catch (_) {
        showToast("Music upload failed due to network error.");
    }

    if (dropzone) dropzone.textContent = oldText;
    loadLocalMusic();
}

// ── Shared result-card builder — YouTube-embed and yt-dlp tabs render the
// exact same card shape (thumbnail + preview-play + title/channel), and
// only differ in what "preview" mounts and what "send" transmits. Single
// builder keeps those two card implementations from drifting apart the
// way independently-maintained near-duplicates tend to in this codebase.
//
// Interaction, per spec: click the play button → preview in place, does
// NOT send. Click anywhere else on the card → sends immediately (mirrors
// sendStickerInstantly's instant-send, not the GIF staging pattern —
// "clicking anywhere else... can send it" reads as immediate, and a
// YouTube/yt-dlp share doesn't have the same "attach a caption" use case
// a photo does).
// Tracks the single currently-active search-result PREVIEW, distinct from
// NowPlaying's Tier-1 sessions. Nothing previously stopped Card B's
// preview from starting while Card A's was still playing/resolving — both
// could run at once, silently fighting over the single shared ytdlp
// status pill (see _ytdlpStatusOwner above) and, worse, both audibly
// playing at the same time. This closes that gap: starting any preview
// stops whichever other preview was active first.
let _activePreview = null; // { stop() } or null

function _stopActivePreview() {
    if (_activePreview) {
        try { _activePreview.stop(); } catch (_) { /* already torn down */ }
        _activePreview = null;
    }
}

function buildYtResultCard(mode, data) {
    const card = document.createElement("div");
    card.className = "yt-result-card";
    card.tabIndex = 0;
    card.setAttribute("role", "button");

    // Everything that was directly on `card` before now lives in this
    // inner row, because the ytdlp preview needs its own full-width row
    // BELOW it (see the play-button handler) — a native <audio controls>
    // element crammed into the 120px-wide video-thumbnail box was
    // unusably small. `card` itself is column-flex now; this row is the
    // horizontal thumb+info layout it always had.
    const main = document.createElement("div");
    main.className = "yt-result-card-main";

    const thumbWrap = document.createElement("div");
    thumbWrap.className = "yt-result-thumb-wrap";

    const img = document.createElement("img");
    img.src = data.thumbnail || "";
    img.loading = "lazy";
    img.alt = "";
    thumbWrap.appendChild(img);

    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.className = "yt-preview-play-btn";
    playBtn.setAttribute("aria-label", "Preview");
    playBtn.innerHTML = `<span class="icon play"></span>`;
    thumbWrap.appendChild(playBtn);

    main.appendChild(thumbWrap);

    const info = document.createElement("div");
    info.className = "yt-result-info";

    const title = document.createElement("span");
    title.className = "yt-result-title";
    title.textContent = data.title || "Untitled";
    info.appendChild(title);

    if (data.channel) {
        const channel = document.createElement("span");
        channel.className = "yt-result-channel";
        channel.textContent = data.channel;
        info.appendChild(channel);
    }

    const hint = document.createElement("span");
    hint.className = "yt-result-hint";
    const durationStr = data.duration ? formatDuration(data.duration) + " · " : "";
    hint.textContent = mode === "youtube"
        ? `${durationStr}▶ preview (video) · tap card to send`
        : `${durationStr}▶ preview (audio) · tap card to send`;
    info.appendChild(hint);

    main.appendChild(info);
    card.appendChild(main);

    // Preview: YouTube mounts a live player in place of the thumbnail
    // (same box, no layout shift). yt-dlp adds a proper full-width audio
    // row below the thumb+info row instead — its native <audio controls>
    // bar needs real horizontal room to be usable at all; the 120px
    // thumbnail box (sized for a 16:9 video frame) crushed it into a
    // sliver. Neither path sends.
    playBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (mode === "youtube") {
            // [FIX] .yt-result-thumb-wrap is a 120×68 browsing thumbnail —
            // fine for scanning results, but far too small to actually
            // watch once a live player is mounted into it. yt-card-live-
            // preview (CSS, on the card) switches just this one card to a
            // stacked, full-width 16:9 layout; every other card in the
            // list is untouched.
            _stopActivePreview();
            card.classList.add("yt-card-live-preview");
            mountYouTubePlayer(thumbWrap, data.id, {
                observeVisibility: false,
                npMeta: { title: data.title, subtitle: data.channel || "", thumbnail: data.thumbnail || null },
                onPlayerReady: (player) => {
                    _activePreview = { stop: () => player.pauseVideo() };
                },
            });
        } else if (!card.querySelector(".yt-inline-audio-preview")) {
            // A fresh preview is about to make sound — silence whatever
            // sent bubble/YouTube session is currently active first, AND
            // whatever other preview card was active.
            _stopActivePreview();
            NowPlaying.interrupt();
            card.classList.add("yt-card-live-preview");

            const audioRow = document.createElement("div");
            audioRow.className = "yt-inline-audio-preview";
            // Interacting with the scrubber/volume/play button inside
            // this row must never bubble up into card's send-on-click.
            const audio = document.createElement("audio");
            audio.src = `/api/music/stream/${encodeURIComponent(data.id)}`;
            audio.controls = true;
            audio.autoplay = true;
            audio.className = "chat-audio";
            audio.dataset.ytdlpStatusLabel = "Resolving preview…";
            bindYtdlpAudioIndicator(audio);
            audioRow.appendChild(audio);
            card.appendChild(audioRow);

            _activePreview = { stop: () => audio.pause() };
        }
    });

    // Send: any other click on the card.
    const doSend = () => {
        if (mode === "youtube") {
            sendYouTubeShareInstantly(data.id, data.title, data.channel, data.thumbnail);
        } else {
            sendYtdlpShareInstantly(data.id, data.title, data.channel, data.thumbnail);
        }
    };
    card.addEventListener("click", doSend);
    card.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); doSend(); }
    });

    return card;
}

function formatDuration(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds));
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${String(rem).padStart(2, "0")}`;
}

// ── YouTube tab — official Data API v3 for search (sanctioned, not
// yt-dlp), IFrame Player API for playback. See mountYouTubePlayer's
// comment for why "h264 144p" isn't achievable as a hard guarantee here.
// Deliberate "Load More" pagination, NOT auto-triggered infinite scroll.
// Each click is exactly one more search.list call against the ~100
// search.list calls/day quota bucket (per Google's June 2026 quota
// change — search.list no longer draws from the shared 10,000-unit pool,
// it has its own dedicated ~100-call/day allocation, shared across this
// whole API key/project, i.e. across all 3 of you). Auto-scroll would
// silently spend that budget on scroll position, not intent — a single
// idle scroll session could burn several calls nobody meant to spend,
// out of a budget that's already only ~100/day total. A button makes
// each spend visible and deliberate.
let _ytSearchQuery         = null;
let _ytSearchNextPageToken = null;

async function searchYouTube() {
    const query = $("youtube-search-input")?.value.trim();
    const list  = $("youtube-results-list");
    if (!list) return;
    _ytSearchQuery = query || null;
    _ytSearchNextPageToken = null;
    if (!query) { list.innerHTML = ""; return; }

    list.innerHTML = `<div style="color:#8b949e;font-size:12px;text-align:center;padding:30px;">Searching YouTube...</div>`;

    try {
        const url = `https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&maxResults=15&videoEmbeddable=true&q=${encodeURIComponent(query)}&key=${YOUTUBE_API_KEY}`;
        const res  = await fetch(url);
        const data = await res.json();

        list.innerHTML = "";
        if (data.error) {
            list.innerHTML = `<div style="color:#f85149;font-size:12px;text-align:center;padding:30px;">${escapeHTML(data.error.message || "YouTube API error.")}</div>`;
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            list.innerHTML = `<div style="color:#484f58;font-size:12px;text-align:center;padding:30px;">No results.</div>`;
            return;
        }
        appendYouTubeResultCards(list, items);
        _ytSearchNextPageToken = data.nextPageToken || null;
        renderYouTubeLoadMoreButton(list);
    } catch (_) {
        list.innerHTML = `<div style="color:#f85149;font-size:12px;text-align:center;padding:30px;">Search failed — network error.</div>`;
    }
}

async function loadMoreYouTubeResults() {
    const list = $("youtube-results-list");
    if (!list || !_ytSearchQuery || !_ytSearchNextPageToken) return;

    const btnWrap = list.querySelector(".yt-load-more-wrap");
    if (btnWrap) btnWrap.textContent = "Loading more...";

    try {
        const url = `https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&maxResults=15&videoEmbeddable=true&pageToken=${encodeURIComponent(_ytSearchNextPageToken)}&q=${encodeURIComponent(_ytSearchQuery)}&key=${YOUTUBE_API_KEY}`;
        const res  = await fetch(url);
        const data = await res.json();

        if (btnWrap) btnWrap.remove();
        if (data.error) {
            showToast(data.error.message || "YouTube API error.");
            return;
        }
        appendYouTubeResultCards(list, data.items || []);
        _ytSearchNextPageToken = data.nextPageToken || null;
        renderYouTubeLoadMoreButton(list);
    } catch (_) {
        if (btnWrap) btnWrap.textContent = "Load more failed — tap to retry";
    }
}

function appendYouTubeResultCards(list, items) {
    items.forEach(item => {
        const videoId = item.id?.videoId;
        if (!videoId) return;
        const card = buildYtResultCard("youtube", {
            id: videoId,
            title: item.snippet?.title,
            channel: item.snippet?.channelTitle,
            thumbnail: item.snippet?.thumbnails?.medium?.url
                || item.snippet?.thumbnails?.default?.url
                || `https://img.youtube.com/vi/${videoId}/hqdefault.jpg`,
        });
        list.appendChild(card);
    });
}

function renderYouTubeLoadMoreButton(list) {
    list.querySelector(".yt-load-more-wrap")?.remove();
    if (!_ytSearchNextPageToken) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "yt-load-more-wrap";
    btn.textContent = "Load more results";
    btn.addEventListener("click", loadMoreYouTubeResults);
    list.appendChild(btn);
}

function sendYouTubeShareInstantly(videoId, title, channel, thumbnailUrl) {
    if (!videoId) return;
    if (!socket || socket.readyState !== 1) { showToast("Not connected."); return; }

    const payload = {
        type: "youtube",
        id: genId(),
        user: username,
        videoId,
        title: title || "Untitled",
        channel: channel || "",
        thumbnailUrl: thumbnailUrl || "",
    };
    if (replyingTo) payload.replyTo = replyingTo;
    socket.send(JSON.stringify(payload));

    Unread.markOwnMessage();
    clearReply();
    closeMusicDrawer();
    $("message-input")?.focus();
}

// ── yt-dlp tab — search/stream via this project's own /api/music/* routes
// (server-side yt-dlp backend). This client only calls those routes; it
// does not implement or modify the extraction/resolution logic itself.
let _ytdlpSearchQuery    = null;
let _ytdlpSearchNextPage = null;
const YTDLP_PAGE_SIZE    = 10;

async function searchYtdlp() {
    const query = $("ytdlp-search-input")?.value.trim();
    const list  = $("ytdlp-results-list");
    if (!list) return;

    _ytdlpSearchQuery = query || null;
    _ytdlpSearchNextPage = null;

    if (!query) {
        list.innerHTML = "";
        return;
    }

    list.innerHTML = `<div style="color:#8b949e;font-size:12px;text-align:center;padding:30px;">Searching (yt-dlp)...</div>`;

    try {
        const res  = await fetch(`/api/music/search?q=${encodeURIComponent(query)}&page=1&limit=${YTDLP_PAGE_SIZE}`);
        const data = await res.json();

        list.innerHTML = "";
        if (data.error) {
            list.innerHTML = `<div style="color:#f85149;font-size:12px;text-align:center;padding:30px;">${escapeHTML(data.error)}</div>`;
            return;
        }

        const items = Array.isArray(data) ? data : (data.items || []);
        if (!items.length) {
            list.innerHTML = `<div style="color:#484f58;font-size:12px;text-align:center;padding:30px;">No results.</div>`;
            return;
        }

        items.forEach(item => {
            if (!item.id) return;
            const card = buildYtResultCard("ytdlp", {
                id: item.id,
                title: item.title,
                channel: "",
                thumbnail: item.thumbnail || `https://img.youtube.com/vi/${item.id}/hqdefault.jpg`,
                duration: item.duration,
            });
            list.appendChild(card);
        });

        _ytdlpSearchNextPage = data.hasMore ? 2 : null;
        renderYtdlpLoadMoreButton(list);
    } catch (_) {
        list.innerHTML = `<div style="color:#f85149;font-size:12px;text-align:center;padding:30px;">Search failed — network error.</div>`;
    }
}

async function loadMoreYtdlpResults() {
    const list = $("ytdlp-results-list");
    if (!list || !_ytdlpSearchQuery || !_ytdlpSearchNextPage) return;

    const btnWrap = list.querySelector(".yt-load-more-wrap");
    if (btnWrap) btnWrap.textContent = "Loading more...";

    try {
        const res  = await fetch(`/api/music/search?q=${encodeURIComponent(_ytdlpSearchQuery)}&page=${_ytdlpSearchNextPage}&limit=${YTDLP_PAGE_SIZE}`);
        const data = await res.json();

        if (btnWrap) btnWrap.remove();

        if (data.error) {
            showToast(data.error || "yt-dlp search error.");
            return;
        }

        const items = Array.isArray(data) ? data : (data.items || []);
        if (!items.length) {
            _ytdlpSearchNextPage = null;
            return;
        }

        items.forEach(item => {
            if (!item.id) return;
            const card = buildYtResultCard("ytdlp", {
                id: item.id,
                title: item.title,
                channel: "",
                thumbnail: item.thumbnail || `https://img.youtube.com/vi/${item.id}/hqdefault.jpg`,
                duration: item.duration,
            });
            list.appendChild(card);
        });

        if (!Array.isArray(data) && data.hasMore) {
            _ytdlpSearchNextPage = (data.page || _ytdlpSearchNextPage) + 1;
        } else {
            _ytdlpSearchNextPage = null;
        }

        renderYtdlpLoadMoreButton(list);
    } catch (_) {
        if (btnWrap) btnWrap.textContent = "Load more failed — tap to retry";
    }
}

function renderYtdlpLoadMoreButton(list) {
    list.querySelector(".yt-load-more-wrap")?.remove();
    if (!_ytdlpSearchNextPage) return;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "yt-load-more-wrap";
    btn.textContent = "Load more results";
    btn.addEventListener("click", loadMoreYtdlpResults);
    list.appendChild(btn);
}

function sendYtdlpShareInstantly(videoId, title, channel, thumbnailUrl) {
    if (!videoId) return;
    if (!socket || socket.readyState !== 1) { showToast("Not connected."); return; }

    const payload = {
        type: "ytdlp_audio",
        id: genId(),
        user: username,
        videoId,
        title: title || "Untitled",
        channel: channel || "",
        thumbnailUrl: thumbnailUrl || "",
    };
    if (replyingTo) payload.replyTo = replyingTo;
    socket.send(JSON.stringify(payload));

    Unread.markOwnMessage();
    clearReply();
    closeMusicDrawer();
    $("message-input")?.focus();
}

// ── UNIFIED DRAG & DROP MASKS ──────────────────────────────────────────
window.addEventListener("dragenter", e => {
    const gifPanel     = $("gif-manager-panel");
    const stickerPanel = $("sticker-manager-panel");
    const musicPanel   = $("music-manager-panel");
    if ((gifPanel && gifPanel.style.display === "flex") ||
        (stickerPanel && stickerPanel.style.display === "flex") ||
        (musicPanel && musicPanel.style.display === "flex")) return;
    e.preventDefault();
    dragCounter++;
    if (dragCounter === 1) $("drop-overlay").style.display = "flex";
});

window.addEventListener("dragover", e => e.preventDefault());

window.addEventListener("dragleave", e => {
    const gifPanel     = $("gif-manager-panel");
    const stickerPanel = $("sticker-manager-panel");
    const musicPanel   = $("music-manager-panel");
    if ((gifPanel && gifPanel.style.display === "flex") ||
        (stickerPanel && stickerPanel.style.display === "flex") ||
        (musicPanel && musicPanel.style.display === "flex")) return;
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
        dragCounter = 0;
        $("drop-overlay").style.display = "none";
    }
});

window.addEventListener("drop", e => {
    e.preventDefault();
    dragCounter = 0;
    $("drop-overlay").style.display = "none";
    if (e.target.closest("#gif-manager-panel") || e.target.closest("#sticker-manager-panel") || e.target.closest("#music-manager-panel")) return;
    if (e.dataTransfer.files.length > 0) handleFiles(Array.from(e.dataTransfer.files));
});

document.addEventListener("DOMContentLoaded", () => {
    injectQoLStyles();

    // [QoL] Viewer: scroll-to-zoom, pinch-zoom, 2-finger pan, mouse-drag pan, dblclick reset.
    const _viewerEl = $("media-viewer");
    if (_viewerEl) {
        const _applyViewerTransform = (imgEl) => {
            imgEl.style.transform =
                `translate(${_viewerTransX}px,${_viewerTransY}px) scale(${_viewerScale})`;
        };

        _viewerEl.addEventListener("wheel", e => {
            const imgEl = $("viewer-image");
            if (!imgEl || imgEl.style.display === "none") return;
            e.preventDefault();
            _viewerScale = Math.min(8, Math.max(0.5, _viewerScale * (e.deltaY > 0 ? 0.9 : 1.1)));
            _applyViewerTransform(imgEl);
        }, { passive: false });

        _viewerEl.addEventListener("dblclick", e => {
            const imgEl = $("viewer-image");
            if (e.target !== imgEl) return;
            _viewerScale = 1; _viewerTransX = 0; _viewerTransY = 0;
            imgEl.style.transition = "transform 0.2s ease";
            _applyViewerTransform(imgEl);
            setTimeout(() => { imgEl.style.transition = ""; }, 220);
        });

        // ── Touch: pinch-zoom + 2-finger pan ─────────────────────────────
        let _pinchStartDist  = 0;
        let _pinchStartScale = 1;
        let _pinchStartMid   = { x: 0, y: 0 };
        let _pinchStartTX    = 0;
        let _pinchStartTY    = 0;
        let _pinching        = false;

        _viewerEl.addEventListener("touchstart", e => {
            const imgEl = $("viewer-image");
            if (!imgEl || imgEl.style.display === "none") return;
            if (e.touches.length === 2) {
                e.preventDefault();
                const t1 = e.touches[0], t2 = e.touches[1];
                _pinchStartDist  = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
                _pinchStartScale = _viewerScale;
                _pinchStartTX    = _viewerTransX;
                _pinchStartTY    = _viewerTransY;
                _pinchStartMid   = {
                    x: (t1.clientX + t2.clientX) / 2,
                    y: (t1.clientY + t2.clientY) / 2
                };
                _pinching = true;
            }
        }, { passive: false });

        _viewerEl.addEventListener("touchmove", e => {
            const imgEl = $("viewer-image");
            if (!imgEl || imgEl.style.display === "none") return;
            if (!_pinching || e.touches.length !== 2) return;
            e.preventDefault();

            const t1   = e.touches[0], t2 = e.touches[1];
            const dist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
            const mid  = { x: (t1.clientX + t2.clientX) / 2, y: (t1.clientY + t2.clientY) / 2 };

            const newScale = Math.min(8, Math.max(0.5, _pinchStartScale * (dist / _pinchStartDist)));
            const rect = _viewerEl.getBoundingClientRect();
            const cx   = rect.left + rect.width  / 2;
            const cy   = rect.top  + rect.height / 2;

            const ipx = (_pinchStartMid.x - cx - _pinchStartTX) / _pinchStartScale;
            const ipy = (_pinchStartMid.y - cy - _pinchStartTY) / _pinchStartScale;

            _viewerScale  = newScale;
            _viewerTransX = mid.x - cx - ipx * newScale;
            _viewerTransY = mid.y - cy - ipy * newScale;

            _applyViewerTransform(imgEl);
        }, { passive: false });

        _viewerEl.addEventListener("touchend", e => {
            if (e.touches.length < 2) _pinching = false;
        });

        _viewerEl.addEventListener("dragstart", e => e.preventDefault());

        // ── Mouse drag to pan when zoomed (desktop) ────────────────────
        let _drag = null;

        _viewerEl.addEventListener("mousedown", e => {
            const imgEl = $("viewer-image");
            if (!imgEl || imgEl.style.display === "none") return;
            if (_viewerScale <= 1 || e.button !== 0) return;
            e.preventDefault();
            _drag = { sx: e.clientX, sy: e.clientY, tx: _viewerTransX, ty: _viewerTransY, moved: false };
            imgEl.style.cursor = "grabbing";
        });

        _viewerEl.addEventListener("mousemove", e => {
            if (!_drag) return;
            const imgEl = $("viewer-image");
            if (!imgEl) return;
            const dx = e.clientX - _drag.sx;
            const dy = e.clientY - _drag.sy;
            if (!_drag.moved && (Math.abs(dx) > 5 || Math.abs(dy) > 5)) _drag.moved = true;
            _viewerTransX = _drag.tx + dx;
            _viewerTransY = _drag.ty + dy;
            _applyViewerTransform(imgEl);
        });

        const _endDrag = () => {
            if (!_drag) return;
            if (_drag.moved) _viewerDragOccurred = true;
            _drag = null;
            const imgEl = $("viewer-image");
            if (imgEl) imgEl.style.cursor = "";
        };
        _viewerEl.addEventListener("mouseup",    _endDrag);
        _viewerEl.addEventListener("mouseleave", _endDrag);
    }

    // [QoL] Restore clean title + reset unread badge when user focuses the window.
    window.addEventListener("focus", () => {
        const el = $("messages");
        if (el && isNearBottom(el)) Unread.markSeenUpTo(Date.now());
        updateTabTitle();
    });

    // GIF panel drag setup
    const gifPanel  = $("gif-manager-panel");
    const gifDrop   = $("gif-dropzone");

    if (gifPanel) {
        gifPanel.addEventListener("dragenter", e => { e.stopPropagation(); e.preventDefault(); });
        gifPanel.addEventListener("dragover",  e => { e.stopPropagation(); e.preventDefault(); });
    }

    if (gifDrop) {
        gifDrop.addEventListener("dragover", e => {
            e.stopPropagation(); e.preventDefault();
            gifDrop.style.borderColor = "#58a6ff";
            gifDrop.style.background  = "#1f6feb11";
        });
        gifDrop.addEventListener("dragleave", e => {
            e.stopPropagation(); e.preventDefault();
            gifDrop.style.borderColor = "#444";
            gifDrop.style.background  = "#161b22";
        });
        gifDrop.addEventListener("drop", async e => {
            e.stopPropagation(); e.preventDefault();
            gifDrop.style.borderColor = "#444";
            gifDrop.style.background  = "#161b22";
            if (e.dataTransfer.files.length > 0) await uploadGifAssetBinary(e.dataTransfer.files[0]);
        });
    }

    // Sticker panel drag setup
    const stickerPanel     = $("sticker-manager-panel");
    const stickerDrop      = $("sticker-dropzone");
    const newStickerFolder = $("btn-new-sticker-folder");

    if (stickerPanel) {
        stickerPanel.addEventListener("dragenter", e => { e.stopPropagation(); e.preventDefault(); });
        stickerPanel.addEventListener("dragover",  e => { e.stopPropagation(); e.preventDefault(); });
    }

    if (stickerDrop) {
        stickerDrop.addEventListener("dragover", e => {
            e.stopPropagation(); e.preventDefault();
            stickerDrop.style.borderColor = "#58a6ff";
            stickerDrop.style.background  = "#1f6feb11";
        });
        stickerDrop.addEventListener("dragleave", e => {
            e.stopPropagation(); e.preventDefault();
            stickerDrop.style.borderColor = "#444";
            stickerDrop.style.background  = "#161b22";
        });
        stickerDrop.addEventListener("drop", async e => {
            e.stopPropagation(); e.preventDefault();
            stickerDrop.style.borderColor = "#444";
            stickerDrop.style.background  = "#161b22";
            for (const file of e.dataTransfer.files) {
                await uploadStickerAssetBinary(file);
            }
        });
    }

    if (newStickerFolder) {
        newStickerFolder.addEventListener("click", async () => {
            const name = prompt("New sticker folder name:");
            if (!name) return;
            const safe = name.replace(/[^a-zA-Z0-9\-_ ]/g, "").trim();
            if (!safe) return;
            try {
                await fetch("/api/stickers/create-folder", {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ name: safe })
                });
            } catch (_) {}
            currentStickerFolder = safe;
            localStorage.setItem("last_sticker_folder", safe);
            loadLocalStickers();
        });
    }

    const newGifFolder = $("btn-new-folder");
    if (newGifFolder) {
        newGifFolder.addEventListener("click", async () => {
            const name = prompt("New GIF space name:");
            if (!name) return;
            const safe = name.replace(/[^a-zA-Z0-9\-_ ]/g, "").trim();
            if (!safe) return;
            try {
                await fetch("/api/gifs/create-folder", {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ name: safe })
                });
            } catch (_) {
                showToast("Failed to create GIF folder.");
            }
            currentActiveFolder = safe;
            localStorage.setItem("last_gif_folder", safe);
            loadLocalGifs();
        });
    }

    // Music panel drag setup — mirrors GIF/sticker exactly, targeting the
    // real /api/sfx/* routes (that's what local music actually is server-
    // side; "music" is only the client-facing name for this tab).
    const musicPanel     = $("music-manager-panel");
    const musicDrop      = $("music-dropzone");
    const newMusicFolder = $("btn-new-music-folder");

    if (musicPanel) {
        musicPanel.addEventListener("dragenter", e => { e.stopPropagation(); e.preventDefault(); });
        musicPanel.addEventListener("dragover",  e => { e.stopPropagation(); e.preventDefault(); });
    }

    if (musicDrop) {
        musicDrop.addEventListener("dragover", e => {
            e.stopPropagation(); e.preventDefault();
            musicDrop.style.borderColor = "#58a6ff";
            musicDrop.style.background  = "#1f6feb11";
        });
        musicDrop.addEventListener("dragleave", e => {
            e.stopPropagation(); e.preventDefault();
            musicDrop.style.borderColor = "#444";
            musicDrop.style.background  = "#161b22";
        });
        musicDrop.addEventListener("drop", async e => {
            e.stopPropagation(); e.preventDefault();
            musicDrop.style.borderColor = "#444";
            musicDrop.style.background  = "#161b22";
            for (const file of e.dataTransfer.files) {
                await uploadMusicAssetBinary(file);
            }
        });
    }

    if (newMusicFolder) {
        newMusicFolder.addEventListener("click", async () => {
            const name = prompt("New music folder name:");
            if (!name) return;
            const safe = name.replace(/[^a-zA-Z0-9\-_ ]/g, "").trim();
            if (!safe) return;
            try {
                await fetch("/api/sfx/create-folder", {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ name: safe })
                });
            } catch (_) {
                showToast("Failed to create music folder.");
            }
            currentActiveMusicFolder = safe;
            localStorage.setItem("last_music_folder", safe);
            loadLocalMusic();
        });
    }

    const stickerFileInput = document.querySelector('[onchange*="handleManualStickerSelection"]');
    if (stickerFileInput) {
        stickerFileInput.accept   = ".png,.jpg,.jpeg,.gif,.webp,.avif,.mp4,.webm,.mov";
        stickerFileInput.multiple = true;
    }

    if (localStorage.getItem("last_gif_folder")) loadLocalGifs();

    // Toolbar tooltips
    [
        { match: "toggleGifDrawer",     tip: "GIFs (Alt+G)"        },
        { match: "toggleStickerDrawer", tip: "Stickers (Alt+S)"    },
        { match: "toggleMusicDrawer",   tip: "Music (Alt+M)"       },
        { match: "file-input",          tip: "Attach file (Alt+A)" },
    ].forEach(({ match, tip }) => {
        document.querySelectorAll(`[onclick*="${match}"]`).forEach(el => {
            if (!el.title) el.title = tip;
        });
    });

    const giphyInput = $("giphy-search-input");
    if (giphyInput) {
        giphyInput.addEventListener("keydown", e => {
            if (e.key === "Enter") { e.preventDefault(); searchGiphy(); }
        });
    }

    // Arrow-key grid navigation
    [
        { id: "gif-grid",     sel: "img[tabindex='0']",                     folderId: "folder-list-container"         },
        { id: "sticker-grid", sel: "img[tabindex='0'],video[tabindex='0']", folderId: "sticker-folder-list-container" },
        { id: "giphy-grid",   sel: "img[tabindex='0']",                     folderId: null                            },
    ].forEach(({ id, sel, folderId }) => {
        const grid = $(id);
        if (!grid) return;
        grid.addEventListener("keydown", e => {
            if (!["ArrowRight","ArrowLeft","ArrowUp","ArrowDown"].includes(e.key)) return;
            e.preventDefault();
            const items = Array.from(grid.querySelectorAll(sel));
            if (!items.length) return;
            const focused = document.activeElement;
            const idx  = items.indexOf(focused);
            if (idx === -1) { items[0]?.focus(); return; }
            const itemW = (items[0].offsetWidth || 90) + 8;
            const cols  = Math.max(1, Math.floor(grid.offsetWidth / itemW));
            if (e.key === "ArrowLeft" && folderId && idx % cols === 0) {
                const folders = Array.from(document.querySelectorAll(`#${folderId} [tabindex='0']`));
                if (folders.length) { folders[0].focus(); return; }
            }
            const delta = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols };
            const next  = Math.min(Math.max(0, idx + delta[e.key]), items.length - 1);
            items[next]?.focus();
        });
    });

    // Arrow-key folder sidebar navigation
    [
        { containerId: "folder-list-container",         gridId: "gif-grid",     gridSel: "img[tabindex='0']"                    },
        { containerId: "sticker-folder-list-container", gridId: "sticker-grid", gridSel: "img[tabindex='0'],video[tabindex='0']" },
    ].forEach(({ containerId, gridId, gridSel }) => {
        const container = $(containerId);
        if (!container) return;
        container.addEventListener("keydown", e => {
            if (e.key === "ArrowRight") {
                e.preventDefault();
                document.querySelector(`#${gridId} ${gridSel.split(",")[0]}`)?.focus();
                return;
            }
            if (!["ArrowUp","ArrowDown"].includes(e.key)) return;
            e.preventDefault();
            const items = Array.from(container.querySelectorAll("[tabindex='0']"));
            if (!items.length) return;
            const idx  = items.indexOf(document.activeElement);
            const next = Math.min(Math.max(0, idx + (e.key === "ArrowDown" ? 1 : -1)), items.length - 1);
            items[next]?.focus();
        });
    });

    // [PERF] Exclusive video playback — use a Set instead of querySelectorAll on every play.
    document.addEventListener("play", e => {
        if (e.target.tagName?.toLowerCase() === "video" && !e.target.classList.contains("chat-sticker")) {
            _playingVideos.forEach(v => { if (v !== e.target) v.pause(); });
            _playingVideos.add(e.target);
        }
    }, true);

    document.addEventListener("pause", e => {
        if (e.target.tagName?.toLowerCase() === "video") _playingVideos.delete(e.target);
    }, true);

    // Click messages area to close GIF/Sticker/Music panels
    const messagesContainer = $("messages");
    if (messagesContainer) {
        messagesContainer.addEventListener("click", () => {
            const gifPanel     = $("gif-manager-panel");
            const stickerPanel = $("sticker-manager-panel");
            const musicPanel   = $("music-manager-panel");

            if (gifPanel && gifPanel.style.display === "flex") {
                gifPanel.style.display = "none";
                $("message-input")?.focus();
            }
            if (stickerPanel && stickerPanel.style.display === "flex") {
                stickerPanel.style.display = "none";
                $("message-input")?.focus();
            }
            if (musicPanel && musicPanel.style.display === "flex") {
                closeMusicDrawer();
            }
        });
    }
});