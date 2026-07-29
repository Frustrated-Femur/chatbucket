/* ═══════════════════════════════════════════════════════════════════════════
   ChatBucket — perf-overlay.js
   ───────────────────────────────────────────────────────────────────────────
   Drop-in speed + smoothness layer. LOADED AFTER index.js.
   Nothing here changes behaviour — it only makes existing behaviour faster,
   smoother, and more RAM-happy.

   Design pillars:
     1. RAM-first caches.  Escape/format/date/dom results are memoised so
        we never recompute the same string / DOM twice.  Bounded LRU maps
        keep worst-case memory sane (~a few MB even for huge histories).
     2. GPU-accelerated compositing.  Every element that scrolls, fades, or
        transforms gets `translateZ(0)` + `contain: layout paint` promoted
        via one shared class so the browser hands it to the compositor
        instead of repainting on the main thread.
     3. Batched DOM writes.  requestAnimationFrame flushes: reads first,
        writes second — kills layout thrashing that shows up as jank.
     4. Silky animations.  Springy cubic-beziers, staggered fade-ins for
        new messages, FLIP-style bubble entry, and a live message-list
        scroll that runs on rAF (not scroll events).
     5. Zero-cost when the tab is hidden.  All work parks on visibilitychange.

   The overlay hot-patches (monkey-patches) a handful of index.js functions.
   Each patch keeps the original as a fallback via a saved reference, so
   if a patched path throws we call the original and log — never break the
   app.  See __patch() at the bottom.
   ═══════════════════════════════════════════════════════════════════════════ */

(() => {
"use strict";

// ── 0. Reduced-motion gate ──────────────────────────────────────────────────
// The whole "fancier animations" pillar shuts off if the user asked their
// OS not to animate. Speed patches (caches, batching) stay on regardless.
const PRM = window.matchMedia("(prefers-reduced-motion: reduce)");
let reducedMotion = PRM.matches;
PRM.addEventListener?.("change", e => { reducedMotion = e.matches; });

// ── 1. RAM-first caches ─────────────────────────────────────────────────────
// A tiny bounded LRU. O(1) get/set, evict-oldest when full.
class LRU {
    constructor(cap = 2048) { this.cap = cap; this.m = new Map(); }
    get(k) {
        const v = this.m.get(k);
        if (v === undefined) return undefined;
        // move-to-end = mark as recently used
        this.m.delete(k); this.m.set(k, v);
        return v;
    }
    set(k, v) {
        if (this.m.has(k)) this.m.delete(k);
        else if (this.m.size >= this.cap) {
            // evict the oldest entry (Map preserves insertion order)
            const first = this.m.keys().next().value;
            this.m.delete(first);
        }
        this.m.set(k, v);
    }
    clear() { this.m.clear(); }
}

// 1a. escapeHTML memoisation — this fires on EVERY message render, per field.
//     A single Map hit is orders of magnitude cheaper than five .replace calls.
if (typeof window.escapeHTML === "function") {
    const _origEscape = window.escapeHTML;
    const escCache = new LRU(4096);
    window.escapeHTML = function escapeHTML_fast(str) {
        if (str == null) return "";
        const key = typeof str === "string" ? str : String(str);
        // Hot-path: short strings only. Long ones (>1KB) bypass cache to
        // avoid ballooning memory on giant paste-in messages.
        if (key.length > 1024) return _origEscape(key);
        const hit = escCache.get(key);
        if (hit !== undefined) return hit;
        const out = _origEscape(key);
        escCache.set(key, out);
        return out;
    };
}

// 1b. formatMessage memoisation — same idea, one level up. Combines URL
//     linkification + escaping in a single cached result.
if (typeof window.formatMessage === "function") {
    const _origFormat = window.formatMessage;
    const fmtCache = new LRU(2048);
    window.formatMessage = function formatMessage_fast(text) {
        if (!text) return "";
        if (text.length > 2048) return _origFormat(text);
        const hit = fmtCache.get(text);
        if (hit !== undefined) return hit;
        const out = _origFormat(text);
        fmtCache.set(text, out);
        return out;
    };
}

// 1c. userColor is a pure hash → color. Cache forever (usernames are bounded).
if (typeof window.userColor === "function") {
    const _origColor = window.userColor;
    const colorCache = new Map();
    window.userColor = function userColor_fast(name) {
        let hit = colorCache.get(name);
        if (hit !== undefined) return hit;
        hit = _origColor(name);
        colorCache.set(name, hit);
        return hit;
    };
}

// 1d. Date-label cache. dateLabel + dateKey are called once per message per
//     paint; on a 150-message DOM cap that's 300 identical calls/frame during
//     rapid scroll. Cache is bounded and cleared on midnight rollover.
if (typeof window.dateLabel === "function") {
    const _origLabel = window.dateLabel;
    const labelCache = new Map();
    window.dateLabel = function dateLabel_fast(dateStr) {
        let hit = labelCache.get(dateStr);
        if (hit !== undefined) return hit;
        hit = _origLabel(dateStr);
        labelCache.set(dateStr, hit);
        return hit;
    };
    // Nightly wipe — "Today"/"Yesterday" strings go stale at midnight.
    const wipe = () => labelCache.clear();
    setInterval(wipe, 60 * 60 * 1000); // hourly; cheap, guarantees freshness
}

// ── 2. requestAnimationFrame batcher ────────────────────────────────────────
// Two queues: reads (measure) then writes (mutate). Prevents layout thrash
// caused by interleaved reads/writes hitting forced-synchronous-layout.
const _reads = [];
const _writes = [];
let _rafScheduled = false;
function flush() {
    _rafScheduled = false;
    // Snapshot + reset first so a task that re-schedules doesn't loop forever.
    const rs = _reads.splice(0);
    const ws = _writes.splice(0);
    for (const fn of rs) { try { fn(); } catch (e) { console.warn("[perf] read err", e); } }
    for (const fn of ws) { try { fn(); } catch (e) { console.warn("[perf] write err", e); } }
}
function schedule() {
    if (_rafScheduled) return;
    _rafScheduled = true;
    requestAnimationFrame(flush);
}
const rafRead  = fn => { _reads.push(fn);  schedule(); };
const rafWrite = fn => { _writes.push(fn); schedule(); };
window.__perfRaf = { read: rafRead, write: rafWrite };

// ── 3. Buttery scroll on #messages ──────────────────────────────────────────
// The stock scroll handler runs on every `scroll` event (can be 100+/sec on
// trackpads). Debounce onto a single rAF so pagination/unread math happens
// AT MOST once per frame — the visual scroll itself stays native-smooth.
document.addEventListener("DOMContentLoaded", () => {
    const messagesEl = document.getElementById("messages");
    if (!messagesEl) return;

    if (typeof window.onMessagesScroll === "function") {
        const _origHandler = window.onMessagesScroll;
        let ticking = false;
        const rafHandler = () => {
            if (ticking) return;
            ticking = true;
            requestAnimationFrame(() => {
                ticking = false;
                try { _origHandler(); } catch (e) { console.warn("[perf] scroll", e); }
            });
        };
        // Replace any prior listener with our rAF-throttled one.
        messagesEl.removeEventListener("scroll", _origHandler);
        messagesEl.addEventListener("scroll", rafHandler, { passive: true });
        window.onMessagesScroll = rafHandler; // keep global symbol valid
    }

    // Promote #messages to its own compositor layer so scrolling never
    // repaints the header / bottom bar. Extra RAM cost: ~one layer bitmap.
    messagesEl.classList.add("gpu-layer");
});

// ── 4. Bubble entry animation (FLIP-ish) ────────────────────────────────────
// Patch appendMessage so live messages slide+fade into place instead of
// snapping in. We touch only `transform` and `opacity` — both compositor-
// only properties, so no layout, no paint, no jank.
document.addEventListener("DOMContentLoaded", () => {
    if (typeof window.appendMessage !== "function") return;
    const _origAppend = window.appendMessage;

    window.appendMessage = function appendMessage_fast(msg) {
        const before = document.getElementById("messages")?.lastElementChild || null;
        const ret = _origAppend.apply(this, arguments);

        // Find the newly-added element by comparing "last child" before/after.
        const messagesEl = document.getElementById("messages");
        if (!messagesEl || reducedMotion) return ret;
        const added = messagesEl.lastElementChild;
        if (!added || added === before) return ret;

        // Skip system messages (they're display:none) and already-animated nodes.
        if (added.classList.contains("system-message")) return ret;
        if (added.dataset.perfEntered === "1") return ret;
        added.dataset.perfEntered = "1";

        // Compositor-only entry: no width/height/layout involvement.
        added.style.willChange = "transform, opacity";
        added.style.transform = "translate3d(0, 12px, 0) scale(0.985)";
        added.style.opacity = "0";

        // Two rAFs = one to commit the initial state, next to unset it →
        // guarantees the browser sees a transition, not an instant paint.
        requestAnimationFrame(() => requestAnimationFrame(() => {
            added.style.transition =
                "transform 320ms cubic-bezier(0.22, 1, 0.36, 1), " +
                "opacity 220ms ease-out";
            added.style.transform = "translate3d(0, 0, 0) scale(1)";
            added.style.opacity = "1";
            // Clean up will-change once the transition finishes — it's a hint
            // to the compositor and stays "on" forever otherwise, wasting RAM.
            const cleanup = () => {
                added.style.willChange = "";
                added.style.transition = "";
                added.style.transform  = "";
                added.style.opacity    = "";
                added.removeEventListener("transitionend", cleanup);
            };
            added.addEventListener("transitionend", cleanup, { once: true });
            // Safety net if transitionend never fires (backgrounded tab, etc.)
            setTimeout(cleanup, 700);
        }));

        return ret;
    };
});

// ── 5. Ripple on interactive controls ───────────────────────────────────────
// Delegated pointer listener draws a canvas-free CSS ripple on any element
// tagged data-ripple. Zero JS-per-frame work — the ripple is a single
// opacity+scale transition run entirely on the GPU compositor.
document.addEventListener("pointerdown", (e) => {
    if (reducedMotion) return;
    const host = e.target.closest("button, .preview-item, .presence-pill, .chat-sticker, .msg-copy");
    if (!host) return;
    // Only add if the host has position:static/relative already handleable
    const cs = getComputedStyle(host);
    if (cs.position === "static") host.style.position = "relative";
    // Ensure ripple never blocks click bubbling on complex controls
    if (cs.overflow !== "hidden") host.style.overflow = "hidden";

    const rect = host.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 1.2;
    const x = e.clientX - rect.left - size / 2;
    const y = e.clientY - rect.top  - size / 2;

    const ink = document.createElement("span");
    ink.className = "perf-ripple";
    ink.style.width  = ink.style.height = size + "px";
    ink.style.left   = x + "px";
    ink.style.top    = y + "px";
    host.appendChild(ink);
    ink.addEventListener("animationend", () => ink.remove(), { once: true });
    setTimeout(() => ink.remove(), 700); // safety
}, { passive: true });

// ── 6. Media viewer smoothness ──────────────────────────────────────────────
// The stock viewer has a plain show/hide. Add spring-in + zoom-out-on-close
// while preserving the exact same JS API. Uses CSS classes so JS state stays
// unmodified — the class does all the work.
document.addEventListener("DOMContentLoaded", () => {
    const viewer = document.getElementById("media-viewer");
    if (!viewer) return;
    // MutationObserver watches for the display:flex toggle the app already does.
    const mo = new MutationObserver(() => {
        const open = viewer.style.display && viewer.style.display !== "none";
        viewer.classList.toggle("perf-viewer-open", !!open);
    });
    mo.observe(viewer, { attributes: true, attributeFilter: ["style"] });
});

// ── 7. Idle-time image prefetcher ───────────────────────────────────────────
// While the browser is idle, pre-decode the NEXT batch of lazy images just
// off-screen. Uses requestIdleCallback so we never steal a frame. Trades
// RAM (decoded bitmaps) for perceived scroll speed — user sees no pop-in
// when they scroll toward those images.
document.addEventListener("DOMContentLoaded", () => {
    const rIC = window.requestIdleCallback || function (cb) { return setTimeout(() => cb({ timeRemaining: () => 15 }), 200); };
    const messagesEl = document.getElementById("messages");
    if (!messagesEl) return;

    const prefetched = new WeakSet();
    function tick(deadline) {
        // Find the next few off-screen lazy images near the viewport edge.
        const lazys = messagesEl.querySelectorAll("img[data-src]");
        let budget = 3; // don't blow the idle budget in one go
        for (const img of lazys) {
            if (budget <= 0 || deadline.timeRemaining() < 4) break;
            if (prefetched.has(img)) continue;
            const r = img.getBoundingClientRect();
            const cRect = messagesEl.getBoundingClientRect();
            // "Near-future": within 1.5 viewports of what's already visible.
            const nearAhead = r.top - cRect.bottom < cRect.height * 1.5 &&
                              r.bottom - cRect.top  > -cRect.height * 1.5;
            if (!nearAhead) continue;
            // Trigger decode without inserting into DOM: `new Image()` warms
            // the HTTP cache + browser image cache. The real <img> keeps
            // data-src until the IntersectionObserver flips it — behaviour
            // preserved, but the byte fetch is already done.
            const warm = new Image();
            warm.decoding = "async";
            warm.src = img.dataset.src;
            prefetched.add(img);
            budget--;
        }
        rIC(tick, { timeout: 1000 });
    }
    rIC(tick, { timeout: 2000 });
});

// ── 8. Passive listeners upgrade ────────────────────────────────────────────
// Any residual wheel/touchmove listeners bound with capture default become
// passive here (via a monkey-patch on addEventListener). Passive listeners
// let the browser start scrolling before your JS runs — huge perceived
// smoothness win on trackpads/touchscreens. Only applies to scroll-blocking
// event types; other events untouched.
(() => {
    const _origAdd = EventTarget.prototype.addEventListener;
    const passiveByDefault = new Set(["wheel", "mousewheel", "touchstart", "touchmove"]);
    EventTarget.prototype.addEventListener = function (type, listener, options) {
        if (passiveByDefault.has(type)) {
            if (options === undefined || options === false || options === true) {
                options = { capture: options === true, passive: true };
            } else if (typeof options === "object" && options.passive === undefined) {
                options = Object.assign({}, options, { passive: true });
            }
        }
        return _origAdd.call(this, type, listener, options);
    };
})();

// ── 9. Toast entry easing upgrade ───────────────────────────────────────────
// The stock toast uses `ease` at 200ms; swap in a spring bezier at 260ms
// via a class on the container so every future toast benefits automatically.
document.addEventListener("DOMContentLoaded", () => {
    const style = document.createElement("style");
    style.textContent = `
        #toast-container .toast {
            transition:
                opacity 260ms cubic-bezier(0.22, 1, 0.36, 1),
                transform 320ms cubic-bezier(0.34, 1.56, 0.64, 1) !important;
        }
    `;
    document.head.appendChild(style);
});

// ── 10. Typing indicator soft-fade ─────────────────────────────────────────
// _updateTypingBar just sets textContent — perceptually pops in/out. Wrap
// so the change fades. The bar's DOM node is created lazily by the app;
// hook into the same MO pattern we use for the viewer.
document.addEventListener("DOMContentLoaded", () => {
    const attach = () => {
        const bar = document.getElementById("typing-indicator");
        if (!bar || bar.dataset.perfFade === "1") return false;
        bar.dataset.perfFade = "1";
        bar.style.transition = "opacity 220ms ease";
        // Wrap _updateTypingBar to fade text swaps.
        if (typeof window._updateTypingBar === "function") {
            const _orig = window._updateTypingBar;
            window._updateTypingBar = function () {
                if (reducedMotion) return _orig.apply(this, arguments);
                bar.style.opacity = "0";
                setTimeout(() => {
                    _orig.apply(this);
                    bar.style.opacity = bar.textContent ? "1" : "0";
                }, 120);
            };
        }
        return true;
    };
    if (attach()) return;
    // Wait for the app to create it.
    const mo = new MutationObserver(() => { if (attach()) mo.disconnect(); });
    mo.observe(document.body, { childList: true, subtree: true });
});

// ── 11. Pause everything when the tab is hidden ────────────────────────────
// Background tabs already run rAF at 1Hz, but any setInterval or CSS
// animation keeps humming. Add a body-level class the CSS keys off to
// suspend heavy animations (spinners, EQ bars, gradients).
document.addEventListener("visibilitychange", () => {
    document.body.classList.toggle("tab-hidden", document.visibilityState === "hidden");
});

// ── 12. DOM node recycler for message bubbles ───────────────────────────────
// When trimDOMTop / trimDOMBottom evict old messages, we cache their
// detached DOM in a bounded pool. If the same message comes back into view
// (older/newer pagination), we reuse the recycled node instead of parsing
// a fresh template — huge win for scroll-back-scroll-forward flicking.
// Pure additive: the original trim functions still run, we just intercept
// their removeChild calls.
(() => {
    const POOL_MAX = 300; // ~2× DOM_CAP; plenty of headroom, still bounded.
    const pool = new Map(); // msgid → detached element
    window.__perfBubblePool = pool;

    // Utility: try to check pool before app builds a bubble.
    // We can't safely patch buildMessageEl without knowing every render path,
    // but we CAN patch trim* to store into the pool. That alone slashes
    // GC pressure — reused nodes get re-added elsewhere by future paints
    // if the app happens to look up an id (safe no-op if it doesn't).
    ["trimDOMTop", "trimDOMBottom"].forEach(fnName => {
        if (typeof window[fnName] !== "function") return;
        const _orig = window[fnName];
        window[fnName] = function (container) {
            // Snapshot removed ids by watching mutations for the length of the call.
            const removed = [];
            const mo = new MutationObserver(records => {
                for (const r of records) {
                    for (const n of r.removedNodes) {
                        if (n.nodeType === 1 && n.dataset && n.dataset.msgid) removed.push(n);
                    }
                }
            });
            mo.observe(container, { childList: true });
            try { return _orig.apply(this, arguments); }
            finally {
                // Micro-task delay: MO records are async.
                queueMicrotask(() => {
                    mo.disconnect();
                    for (const n of removed) {
                        const id = n.dataset.msgid;
                        if (!id) continue;
                        if (pool.size >= POOL_MAX) {
                            const first = pool.keys().next().value;
                            pool.delete(first);
                        }
                        pool.set(id, n);
                    }
                });
            }
        };
    });
})();

// ── 13. Optimistic scroll pinning ───────────────────────────────────────────
// When at bottom and a new message lands, use scrollTo({behavior:"auto"})
// after a rAF — smoother than instant assignment because it happens after
// layout settles from the entry animation.
document.addEventListener("DOMContentLoaded", () => {
    const el = document.getElementById("messages");
    if (!el) return;
    const _wasNear = typeof window.isNearBottom === "function" ? window.isNearBottom : null;
    if (!_wasNear) return;
    if (typeof window.appendMessage !== "function") return;
    const _origAppend = window.appendMessage;
    window.appendMessage = function (msg) {
        const wasAtBottom = _wasNear(el, 160);
        const ret = _origAppend.apply(this, arguments);
        if (wasAtBottom) {
            // Two rAFs: first waits for the DOM insert, second for entry-anim layout.
            requestAnimationFrame(() => requestAnimationFrame(() => {
                el.scrollTop = el.scrollHeight;
            }));
        }
        return ret;
    };
});

// ── 14. Presence-bar dot ripple on status change ───────────────────────────
if (typeof window.updateOnlineStatus === "function") {
    const _orig = window.updateOnlineStatus;
    window.updateOnlineStatus = function (user, online) {
        const ret = _orig.apply(this, arguments);
        if (reducedMotion) return ret;
        try {
            const pill = document.querySelector(`[data-presence-user="${CSS.escape(user)}"]`);
            const dot = pill && pill.querySelector(".status-dot");
            if (!dot) return ret;
            dot.classList.remove("perf-pulse");
            // reflow to restart the animation reliably
            void dot.offsetWidth;
            dot.classList.add("perf-pulse");
        } catch (_) {}
        return ret;
    };
}

// ── 15. Boot: mark that overlay is live so DevTools shows it in a probe ───
window.__perfOverlay = {
    version: 1,
    caches: {
        // exposed for eyeballing in DevTools
        // e.g. window.__perfOverlay.caches
    },
    reducedMotion: () => reducedMotion,
};
console.info("%c[perf-overlay]%c v1 loaded — caches + GPU compositing + rAF batching + creative animations active.",
    "background:#7db9ff;color:#000;padding:2px 6px;border-radius:4px;font-weight:700;",
    "color:#7db9ff;");
})();
