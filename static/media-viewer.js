/* ==========================================================================
   media-viewer.js — ChatBucket media viewer, attachment preview composer,
   and the centralized Back-priority stack. Loads AFTER every other client
   layer so it can wrap openMediaViewer / closeMediaViewer / sendMessage.

   Three subsystems, one file:

     1. BackStack — a single LIFO-ish registry of "what is currently open".
        Owns BOTH the desktop Escape key AND the Android / browser Back
        button (via one sacrificial history entry + popstate). Any feature —
        built-in now or a future patch layer — registers
        { id, isOpen, close, priority } and gets correct, centralized back
        behaviour. The page never navigates away while any ChatBucket-owned
        transient UI is active; when everything is closed the history guard
        is released so Back performs its normal navigation again.

     2. MediaViewer — the sent-media takeover viewer. Replaces the stock
        fixed <img>-only overlay with a full shell inside #chat-screen:
        sender header, "n of m" counter, edge arrows, keyboard nav, touch
        swipe, a toggleable filmstrip, an in-viewer reply composer, a
        download action, and a "Go to message" action. The legacy
        openMediaViewer(src, type, opts) / openImageViewer(src) /
        closeMediaViewer() API is preserved: blob-URL (trustedLocal) calls
        are unsent media and route into the preview composer instead.

     3. AttachViewer — the unsent-attachment preview composer. Same chrome
        as MediaViewer plus a persistent per-attachment caption input.
        Sends through the real sendMessage() (wrapped below to honour
        per-file captions); closing never loses staged attachments.
        Non-previewable files get a generic card; audio gets a real player.

   Perf notes: transforms/opacity only for motion; the filmstrip rail is
   emptied while hidden (zero retained thumbnails); video/audio strip
   thumbnails reuse the posters media-smooth.js / video-thumbs.js already
   decoded; everything respects prefers-reduced-motion.
   ========================================================================== */
(function () {
"use strict";

if (window.__cbMediaViewer) return;          // idempotent — double-include safe
window.__cbMediaViewer = { version: 1 };

/* ── small helpers ─────────────────────────────────────────────────────── */
const $id = (id) => document.getElementById(id);
const qs  = (sel, root) => (root || document).querySelector(sel);
const qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(str) {
    // Prefer the app's own escapeHTML; fall back to a local copy so this
    // layer can never inject markup even if loaded standalone.
    if (typeof escapeHTML === "function") return escapeHTML(str);
    return String(str == null ? "" : str)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function reducedMotion() {
    return window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function isTypingTarget(t) {
    return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
}

function fmtBytes(n) {
    if (!Number.isFinite(n)) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

function fmtTimestamp(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return "";
    const today = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return "Today at " + time;
    const yest = new Date(today); yest.setDate(yest.getDate() - 1);
    if (d.toDateString() === yest.toDateString()) return "Yesterday at " + time;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + time;
}

function isVideoUrl(u) { return /\.(mp4|webm|mov|mkv|m4v)(\?|#|$)/i.test(u || ""); }

/* ════════════════════════════════════════════════════════════════════════
   1. BACK STACK — centralized Back / Escape priority
   ════════════════════════════════════════════════════════════════════════ */
const BackStack = (() => {
    const entries = new Map();   // id -> { id, isOpen, close, priority }
    let guardPushed = false;
    let suppressPop = false;     // set while WE pop the guard ourselves
    let wired = false;

    function ordered() {
        return Array.from(entries.values()).sort((a, b) => a.priority - b.priority);
    }
    function top() {
        for (const e of ordered()) {
            let open = false;
            try { open = !!e.isOpen(); } catch (_) { open = false; }
            if (open) return e;
        }
        return null;
    }

    function register(entry) {
        // entry: { id, isOpen: () => bool, close: () => void, priority?: n }
        if (!entry || !entry.id || typeof entry.isOpen !== "function" || typeof entry.close !== "function") return;
        entries.set(entry.id, {
            id: entry.id,
            isOpen: entry.isOpen,
            close: entry.close,
            priority: Number.isFinite(entry.priority) ? entry.priority : 50,
        });
        wireHistory();
    }
    function unregister(id) { entries.delete(id); }

    // Close the highest-priority open entry. Returns true if one was closed.
    function handleBack() {
        const e = top();
        if (!e) return false;
        try { e.close(); } catch (_) {}
        return true;
    }

    /* ── history guard (Android Back / browser Back button) ──────────────
       While any registered entry is open we keep exactly one sacrificial
       history entry pushed. A hardware Back pops it → popstate fires → we
       close the top entry and (if anything is still open) re-push. The
       page itself never moves. When the last entry closes — regardless of
       HOW it closed (Back, Escape, a ✕ button) — syncGuard() releases the
       guard, so the very next Back performs the browser's normal
       navigation. That is the spec's core rule: Back never leaves
       ChatBucket while ChatBucket UI is active, and behaves completely
       normally once the UI is bare. */
    function pushGuard() {
        if (guardPushed) return;
        try {
            history.pushState({ cbBackGuard: true }, "", location.href);
            guardPushed = true;
        } catch (_) { /* file:// or sandboxed contexts — Escape still works */ }
    }
    function releaseGuard() {
        if (!guardPushed) return;
        suppressPop = true;
        guardPushed = false;
        try { history.back(); } catch (_) { suppressPop = false; }
    }

    // Call after ANY open/close transition to keep the guard truthful.
    function syncGuard() {
        if (top()) pushGuard();
        else releaseGuard();
    }

    function wireHistory() {
        if (wired) return;
        wired = true;
        window.addEventListener("popstate", () => {
            if (suppressPop) { suppressPop = false; return; }  // our own release
            if (!guardPushed) return;                          // real navigation — allow
            guardPushed = false;                               // the guard was just popped
            handleBack();
            syncGuard();                                       // re-push if more remain
        });
    }

    /* ── Escape key — same priority order, capture phase ──────────────────
       Capture on document runs after msg-actions' window-level capture, so
       its tray/selection get first claim (they stopImmediatePropagation);
       when they have nothing open, we run. We stopImmediatePropagation so
       the legacy index.js Escape ladder below us can never double-fire. */
    function onKeydown(e) {
        if (e.key !== "Escape") return;
        const t = e.target;
        // Inline message edit owns its own Escape (cancel edit) — never let
        // the stack swallow it.
        if (isTypingTarget(t) && t.closest && t.closest(".message.is-editing")) return;
        if (!handleBack()) return;                  // idle — nothing to consume
        e.preventDefault();
        e.stopImmediatePropagation();
        syncGuard();
    }

    function init() {
        document.addEventListener("keydown", onKeydown, true);
    }

    return { register, unregister, handleBack, syncGuard, pushGuard, init };
})();

/* ── Register the pre-existing transient UI layers ────────────────────────
   Lower priority number = closed first. Mirrors the spec's ordering:
   in-viewer composer → viewers → tray/selection → island → panels →
   attachments → reply bar. */
function registerBuiltinLayers() {
    BackStack.register({
        id: "longpress-tray", priority: 10,
        isOpen: () => {
            const tray = $id("cb-tray");
            return !!(tray && tray.classList.contains("is-open"));
        },
        close: () => { try { window.__cbMsgActions.api.closeTray(); } catch (_) {} },
    });
    BackStack.register({
        id: "selection-mode", priority: 12,
        isOpen: () => document.body.classList.contains("cb-select"),
        close: () => { try { window.__cbMsgActions.api.exitSelect(); } catch (_) {} },
    });
    BackStack.register({
        id: "now-playing-island", priority: 20,
        isOpen: () => { try { return typeof Island !== "undefined" && Island.isExpanded(); } catch (_) { return false; } },
        close: () => { try { Island.collapse(); } catch (_) {} },
    });
    const panelEntry = (id, closer) => ({
        id: "panel:" + id, priority: 30,
        isOpen: () => { const el = $id(id); return !!el && el.style.display === "flex"; },
        close: closer,
    });
    BackStack.register(panelEntry("gif-manager-panel",   () => { try { closeGifDrawer(); } catch (_) {} }));
    BackStack.register(panelEntry("music-manager-panel", () => { try { closeMusicDrawer(); } catch (_) {} }));
    BackStack.register(panelEntry("sticker-manager-panel", () => {
        const el = $id("sticker-manager-panel");
        if (el) el.style.display = "none";
        try { $id("message-input") && $id("message-input").focus(); } catch (_) {}
    }));
    BackStack.register({
        id: "attachments", priority: 40,
        isOpen: () => typeof attachmentFiles !== "undefined" && attachmentFiles.length > 0,
        close: () => { try { clearAttachments(); } catch (_) {} },
    });
    BackStack.register({
        id: "reply-bar", priority: 45,
        isOpen: () => { const el = $id("reply-bar"); return !!el && el.style.display === "flex"; },
        close: () => { try { clearReply(); } catch (_) {} },
    });
}

/* ════════════════════════════════════════════════════════════════════════
   2. SENT-MEDIA VIEWER
   ════════════════════════════════════════════════════════════════════════ */
const MediaViewer = (() => {
    let root = null;            // #media-viewer (rebuilt as a full shell)
    let items = [];             // [{ id, src, kind, user, ts, thumb }]
    let index = -1;
    let open = false;
    let stripVisible = true;
    let stripBuiltFor = -1;     // item-set generation the rail was built for
    let replyOpen = false;
    let _generation = 0;        // bumped whenever items are recomputed

    const el = {};              // dom refs, filled by build()

    /* ── construction ─────────────────────────────────────────────────── */
    function build() {
        const old = $id("media-viewer");
        if (!old || old.dataset.cbmShell === "1") { root = old; return !!root; }

        root = document.createElement("div");
        root.id = "media-viewer";
        root.dataset.cbmShell = "1";
        root.tabIndex = -1;
        root.setAttribute("role", "dialog");
        root.setAttribute("aria-label", "Media viewer");

        root.innerHTML =
            `<div class="cbm-head">` +
                `<span class="cbm-avatar" aria-hidden="true"></span>` +
                `<div class="cbm-sender">` +
                    `<span class="cbm-sender-name"></span>` +
                    `<span class="cbm-sender-time"></span>` +
                `</div>` +
                `<div class="cbm-head-spacer"></div>` +
                `<span class="cbm-counter" aria-live="polite"></span>` +
                `<button type="button" class="cbm-btn" data-cbm="jump"  title="Go to message (J)" aria-label="Go to message"><span class="icon jump"></span></button>` +
                `<button type="button" class="cbm-btn" data-cbm="reply" title="Reply (R)" aria-label="Reply"><span class="icon reply"></span></button>` +
                `<button type="button" class="cbm-btn" data-cbm="download" title="Download (D)" aria-label="Download"><span class="icon arrow-down"></span></button>` +
                `<button type="button" class="cbm-btn" data-cbm="strip" title="Toggle filmstrip (T)" aria-label="Toggle filmstrip" aria-pressed="true"><span class="icon filmstrip"></span></button>` +
                `<button type="button" class="cbm-btn" data-cbm="close" title="Close (Esc)" aria-label="Close viewer"><span class="icon close"></span></button>` +
            `</div>` +
            `<div class="cbm-stage">` +
                `<div class="cbm-stage-media"></div>` +
                `<button type="button" class="cbm-edge cbm-edge-prev" data-cbm="prev" aria-label="Previous media"><span class="icon chev-left"></span></button>` +
                `<button type="button" class="cbm-edge cbm-edge-next" data-cbm="next" aria-label="Next media"><span class="icon chev-right"></span></button>` +
            `</div>` +
            `<div class="cbm-strip" hidden><div class="cbm-strip-rail" role="listbox" aria-label="Media in this chat"></div></div>` +
            `<div class="cbm-reply" hidden><div class="cbm-reply-inner">` +
                `<div class="cbm-reply-quote"></div>` +
                `<div class="cbm-reply-row">` +
                    `<div class="cbm-input" contenteditable="true" placeholder="Reply to this media…" aria-label="Reply text"></div>` +
                    `<button type="button" class="cbm-send" data-cbm="send" title="Send reply (Enter)" aria-label="Send reply"><span class="icon send"></span></button>` +
                `</div>` +
            `</div></div>`;

        old.replaceWith(root);

        // Relocate INTO #chat-screen, between #messages and #bottom-zone —
        // the takeover layout (flex:1 inside the chat column) depends on it.
        const chatScreen = $id("chat-screen");
        const bottomZone = $id("bottom-zone");
        if (chatScreen && bottomZone && root.parentElement !== chatScreen) {
            chatScreen.insertBefore(root, bottomZone);
        }

        el.avatar     = qs(".cbm-avatar", root);
        el.name       = qs(".cbm-sender-name", root);
        el.time       = qs(".cbm-sender-time", root);
        el.counter    = qs(".cbm-counter", root);
        el.stageBox   = qs(".cbm-stage-media", root);
        el.prev       = qs(".cbm-edge-prev", root);
        el.next       = qs(".cbm-edge-next", root);
        el.strip      = qs(".cbm-strip", root);
        el.rail       = qs(".cbm-strip-rail", root);
        el.reply      = qs(".cbm-reply", root);
        el.replyQuote = qs(".cbm-reply-quote", root);
        el.input      = qs(".cbm-input", root);
        el.stripBtn   = qs('[data-cbm="strip"]', root);

        wire();
        return true;
    }

    function wire() {
        root.addEventListener("click", onClick);
        root.addEventListener("keydown", onKeydown);

        // Swipe navigation (touch only) — vertical movement untouched.
        let swipe = null;
        root.addEventListener("pointerdown", (e) => {
            if (e.pointerType === "mouse") return;   // desktop uses arrows/keys
            if (e.target.closest(".cbm-strip, .cbm-reply, .cbm-head, .cbm-edge, video")) return;
            swipe = { x: e.clientX, y: e.clientY, id: e.pointerId };
        }, { passive: true });
        root.addEventListener("pointerup", (e) => {
            if (!swipe || e.pointerId !== swipe.id) return;
            const dx = e.clientX - swipe.x, dy = e.clientY - swipe.y;
            swipe = null;
            if (Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy) * 1.4) nav(dx < 0 ? 1 : -1);
        }, { passive: true });
        root.addEventListener("pointercancel", () => { swipe = null; }, { passive: true });

        el.rail.addEventListener("click", (e) => {
            const t = e.target.closest(".cbm-thumb");
            if (!t) return;
            const i = parseInt(t.dataset.index, 10);
            if (Number.isFinite(i)) show(i);
        });
    }

    function onClick(e) {
        const btn = e.target.closest("[data-cbm]");
        if (!btn) return;
        e.stopPropagation();
        switch (btn.dataset.cbm) {
            case "close":    close(); break;
            case "prev":     nav(-1); break;
            case "next":     nav(1);  break;
            case "strip":    toggleStrip(); break;
            case "reply":    replyOpen ? closeReply() : openReply(); break;
            case "unreply":  closeReply(); break;
            case "send":     sendReply(); break;
            case "download": downloadCurrent(); break;
            case "jump":     goToMessage(); break;
        }
    }

    function onKeydown(e) {
        if (e.key === "Enter" && e.target === el.input) {
            e.preventDefault();
            sendReply();
            return;
        }
        if (isTypingTarget(e.target)) return;      // arrows belong to the text caret
        if (e.key === "ArrowLeft")       { e.preventDefault(); nav(-1); }
        else if (e.key === "ArrowRight") { e.preventDefault(); nav(1); }
        else if (e.key === "t" || e.key === "T")   { e.preventDefault(); toggleStrip(); }
        else if (e.key === "r" || e.key === "R")   { e.preventDefault(); replyOpen ? closeReply() : openReply(); }
        else if (e.key === "j" || e.key === "J")   { e.preventDefault(); goToMessage(); }
        else if (e.key === "d" || e.key === "D")   { e.preventDefault(); downloadCurrent(); }
    }

    /* ── data: walk the live chat DOM for media-bearing messages ───────── */
    function collect() {
        const out = [];
        const host = $id("messages");
        if (!host) return out;
        qsa(".message", host).forEach((msgEl) => {
            if (msgEl.classList.contains("message--deleted")) return;
            const media = qs("[data-viewer-src]", msgEl);
            if (!media || !media.dataset.viewerSrc) return;
            const kind = media.dataset.viewerType === "video" ? "video" : "image";
            let thumb = null;
            if (kind === "image") {
                // NOTE: read the ATTRIBUTE, not img.src — the property
                // resolves src="" (lazy images) to the page URL in Chrome.
                thumb = media.getAttribute("src") || media.dataset.src || media.dataset.viewerSrc;
            } else {
                // Reuse the poster video-thumbs.js already captured — no re-decode.
                const posterImg = qs("img.video-thumb", media);
                thumb = (posterImg && posterImg.src) || null;
            }
            out.push({
                id:    msgEl.dataset.msgid || "",
                src:   media.dataset.viewerSrc,
                kind:  kind,
                user:  msgEl.dataset.user || "",
                ts:    msgEl.dataset.ts || "",
                thumb: thumb,
            });
        });
        return out;
    }

    /* ── open / close ─────────────────────────────────────────────────── */
    function openAt(startIndex) {
        if (!items.length) return;
        open = true;
        replyOpen = false;
        _generation++;

        // Take over the chat content area: messages + composer hide, the
        // viewer sits where #messages was. Original display values are
        // restored on close.
        const messages   = $id("messages");
        const bottomZone = $id("bottom-zone");
        if (messages)   { messages.dataset.cbmPrevDisplay   = messages.style.display   || ""; messages.style.display   = "none"; }
        if (bottomZone) { bottomZone.dataset.cbmPrevDisplay = bottomZone.style.display || ""; bottomZone.style.display = "none"; }

        root.classList.add("cbm-open");
        // Two rAFs: display applies first, then the class that runs the
        // transform/opacity enter transition.
        requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add("cbm-shown")));

        stripVisible = true;
        el.stripBtn.setAttribute("aria-pressed", "true");
        stripBuiltFor = -1;
        el.strip.hidden = false;
        hideReply(true);

        show(Math.max(0, Math.min(startIndex, items.length - 1)));
        BackStack.syncGuard();   // push the history guard
        try { root.focus({ preventScroll: true }); } catch (_) { root.focus(); }
    }

    function openWithSrc(src, type) {
        if (!root && !build()) { try { window.open(src, "_blank", "noopener"); } catch (_) {} return; }
        items = collect();
        let idx = items.findIndex((it) => it.src === src);
        if (idx === -1) {
            // Source no longer in the DOM (trimmed window / sticker edge
            // case): still open as a single-item viewer — never a silent no-op.
            items = [{ id: "", src, kind: type === "video" ? "video" : "image", user: "", ts: "", thumb: isVideoUrl(src) ? null : src }];
            idx = 0;
        }
        openAt(idx);
    }

    function close() {
        if (!open) return;
        open = false;
        root.classList.remove("cbm-shown");
        const done = () => {
            root.classList.remove("cbm-open");
            const messages   = $id("messages");
            const bottomZone = $id("bottom-zone");
            if (messages)   messages.style.display   = messages.dataset.cbmPrevDisplay   || "";
            if (bottomZone) bottomZone.style.display = bottomZone.dataset.cbmPrevDisplay || "";
            clearStage();
            hideReply(true);
            el.rail.innerHTML = "";
            stripBuiltFor = -1;
        };
        if (reducedMotion()) done(); else setTimeout(done, 200);
        BackStack.syncGuard();
    }

    function clearStage() {
        qsa(".cbm-media", el.stageBox).forEach((m) => {
            if (m.tagName === "VIDEO") {
                try { m.pause(); } catch (_) {}
                m.removeAttribute("src");
                try { m.load(); } catch (_) {}
            }
            m.remove();
        });
    }

    /* ── navigation ───────────────────────────────────────────────────── */
    function nav(dir) {
        const n = index + dir;
        if (n < 0 || n >= items.length) return;
        show(n);
    }

    function show(i) {
        if (i < 0 || i >= items.length) return;
        const prevIndex = index;
        index = i;
        const item = items[i];
        root.dataset.navdir = prevIndex === -1 ? "0" : (i >= prevIndex ? "1" : "-1");

        // Stage swap — pause + tear down any previous video FIRST so audio
        // never bleeds across items.
        clearStage();
        const media = document.createElement(item.kind === "video" ? "video" : "img");
        media.className = "cbm-media";
        if (item.kind === "video") {
            media.controls = true;
            media.playsInline = true;
            media.preload = "metadata";
            media.src = item.src;
            try { if (typeof NowPlaying !== "undefined") NowPlaying.interrupt(); } catch (_) {}
        } else {
            media.src = item.src;
            media.alt = "";
            media.decoding = "async";
            media.draggable = false;
        }
        el.stageBox.appendChild(media);
        requestAnimationFrame(() => requestAnimationFrame(() => media.classList.add("cbm-media-in")));

        // Header metadata.
        el.name.textContent = item.user || "Media";
        el.name.style.color = (item.user && typeof userColor === "function") ? userColor(item.user) : "";
        el.time.textContent = fmtTimestamp(item.ts);
        el.avatar.textContent = (item.user || "?").slice(0, 1);
        el.avatar.style.background = (item.user && typeof userColor === "function") ? userColor(item.user) : "";
        el.counter.textContent = items.length > 1 ? `${i + 1} of ${items.length}` : "";

        // Edge arrows hide at the ends — there is nothing in that direction.
        el.prev.hidden = i <= 0;
        el.next.hidden = i >= items.length - 1;

        rebuildStripIfNeeded();
        syncStripSelection();

        if (replyOpen) renderReplyQuote();   // reply target tracks the viewed item
    }

    /* ── filmstrip ────────────────────────────────────────────────────── */
    function makeThumb(item, i) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "cbm-thumb";
        b.dataset.index = String(i);
        b.setAttribute("role", "option");
        b.setAttribute("aria-label", `${item.kind === "video" ? "Video" : "Image"} ${i + 1} of ${items.length}`);
        if (item.thumb) {
            const img = document.createElement("img");
            img.loading = "lazy";
            img.decoding = "async";
            img.alt = "";
            img.src = item.thumb;
            b.appendChild(img);
        }
        if (item.kind === "video") {
            const badge = document.createElement("span");
            badge.className = "cbm-thumb-badge";
            badge.innerHTML = `<span class="icon play"></span>`;
            b.appendChild(badge);
        }
        return b;
    }

    function rebuildStripIfNeeded() {
        if (!stripVisible || stripBuiltFor === _generation) return;
        stripBuiltFor = _generation;
        el.rail.innerHTML = "";
        const frag = document.createDocumentFragment();
        items.forEach((item, i) => frag.appendChild(makeThumb(item, i)));
        el.rail.appendChild(frag);
    }

    function syncStripSelection() {
        if (!stripVisible) return;
        qsa(".cbm-thumb", el.rail).forEach((t) => {
            const cur = parseInt(t.dataset.index, 10) === index;
            t.classList.toggle("cbm-current", cur);
            t.setAttribute("aria-selected", cur ? "true" : "false");
            if (cur) {
                // Only scroll when the thumb is actually out of view —
                // never yank the rail on every tap.
                const tRect = t.getBoundingClientRect();
                const rRect = el.rail.getBoundingClientRect();
                if (tRect.left < rRect.left || tRect.right > rRect.right) {
                    t.scrollIntoView({ block: "nearest", inline: "center", behavior: reducedMotion() ? "auto" : "smooth" });
                }
            }
        });
    }

    function setStripVisible(visible, instant) {
        stripVisible = visible;
        el.stripBtn.setAttribute("aria-pressed", visible ? "true" : "false");
        const strip = el.strip;
        if (!visible) {
            if (strip.hidden) return;
            const finish = () => {
                strip.hidden = true;
                strip.classList.remove("cbm-strip-collapsed");
                el.rail.innerHTML = "";        // zero retained thumbs while hidden
                stripBuiltFor = -1;
            };
            if (instant || reducedMotion()) { finish(); return; }
            strip.classList.add("cbm-strip-collapsed");
            setTimeout(finish, 200);
        } else {
            if (!strip.hidden) { syncStripSelection(); return; }
            strip.hidden = false;
            if (instant || reducedMotion()) {
                rebuildStripIfNeeded();
                syncStripSelection();
                return;
            }
            strip.classList.add("cbm-strip-collapsed");
            rebuildStripIfNeeded();
            requestAnimationFrame(() => requestAnimationFrame(() => {
                strip.classList.remove("cbm-strip-collapsed");
                syncStripSelection();
            }));
        }
    }

    function toggleStrip() { setStripVisible(!stripVisible); }

    /* ── in-viewer reply ──────────────────────────────────────────────── */
    function currentMsgEl() {
        const item = items[index];
        if (!item || !item.id) return null;
        if (typeof findMsgEl === "function") {
            const node = findMsgEl(item.id);
            if (node) return node;
        }
        return qs(`[data-msgid="${CSS.escape(item.id)}"]`);
    }

    function renderReplyQuote() {
        const item = items[index];
        if (!item) return;
        let inner = "";
        // Thumbs are same-origin paths, http(s), or our own data: posters —
        // the same trust envelope buildReplyQuoteHTML already relies on.
        const safeThumb = item.thumb && /^(\/|https?:\/\/|data:image\/)/i.test(item.thumb) ? item.thumb : null;
        if (safeThumb) inner += `<img src="${esc(safeThumb)}" alt="">`;
        inner += `<span class="cbm-reply-quote-text">Replying to <b>${esc(item.user || "media")}</b></span>`;
        inner += `<button type="button" class="cbm-btn" data-cbm="unreply" title="Cancel reply" aria-label="Cancel reply"><span class="icon close sm"></span></button>`;
        el.replyQuote.innerHTML = inner;
    }

    function openReply() {
        const item = items[index];
        if (!item || !item.id) { try { showToast("Can't reply to this item."); } catch (_) {} return; }
        const msgEl = currentMsgEl();
        if (!msgEl) { try { showToast("Message isn't loaded anymore."); } catch (_) {} return; }
        // Set the REAL reply state — the standard reply-bar is staged under
        // the (hidden) composer and will be there when the viewer closes.
        try { setReply(msgEl); } catch (_) {}
        replyOpen = true;
        renderReplyQuote();
        el.reply.hidden = false;
        el.reply.classList.add("cbm-reply-collapsed");
        requestAnimationFrame(() => requestAnimationFrame(() => {
            el.reply.classList.remove("cbm-reply-collapsed");
            try { el.input.focus({ preventScroll: true }); } catch (_) { el.input.focus(); }
        }));
    }

    function hideReply(instant) {
        replyOpen = false;
        el.input.innerText = "";
        if (instant || reducedMotion()) {
            el.reply.hidden = true;
            el.reply.classList.remove("cbm-reply-collapsed");
            return;
        }
        el.reply.classList.add("cbm-reply-collapsed");
        setTimeout(() => {
            el.reply.hidden = true;
            el.reply.classList.remove("cbm-reply-collapsed");
        }, 200);
    }

    function closeReply() {
        if (!replyOpen) return;
        hideReply(false);
        try { clearReply(); } catch (_) {}
        try { root.focus({ preventScroll: true }); } catch (_) {}
    }

    function sendReply() {
        const text = el.input.innerText.trim();
        if (!text) { closeReply(); return; }
        const input = $id("message-input");
        if (!input) return;
        input.innerText = text;                                   // sendMessage() reads innerText
        input.dispatchEvent(new Event("input", { bubbles: true })); // draft bookkeeping
        try { sendMessage(); } catch (_) {}
        hideReply(false);
        try { root.focus({ preventScroll: true }); } catch (_) {}
    }

    /* ── actions ──────────────────────────────────────────────────────── */
    function downloadCurrent() {
        const item = items[index];
        if (!item) return;
        const a = document.createElement("a");
        a.href = item.src;
        a.download = (item.src.split("/").pop() || "media").split("?")[0];
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    // "Go to message": close the viewer, then land the chat on the exact
    // bubble the viewed media belongs to (existing flash-highlight scroll).
    function goToMessage() {
        const item = items[index];
        const id = item && item.id;
        close();
        if (!id) return;
        requestAnimationFrame(() => { try { scrollToMessage(id); } catch (_) {} });
    }

    return {
        openWithSrc,
        close,
        closeReply,
        goToMessage,
        isOpen:      () => open,
        isReplyOpen: () => replyOpen,
        _build: build,
    };
})();

/* ════════════════════════════════════════════════════════════════════════
   3. ATTACHMENT PREVIEW COMPOSER
   ════════════════════════════════════════════════════════════════════════ */
const AttachViewer = (() => {
    let root = null;
    let files = [];             // snapshot of attachmentFiles at open
    let index = 0;
    let open = false;
    let stripVisible = true;
    const captions = new Map(); // fileFingerprint(file) -> caption text
    const blobUrls = new Map(); // fileFingerprint(file) -> object URL (stage playback)
    const el = {};

    function fp(file) {
        return typeof fileFingerprint === "function"
            ? fileFingerprint(file)
            : `${file.name}|${file.size}|${file.lastModified || 0}|${file.type}`;
    }
    function kindOf(file) {
        if (/^image\//.test(file.type)) return "image";
        if (/^video\//.test(file.type)) return "video";
        if (/^audio\//.test(file.type)) return "audio";
        return "file";
    }
    function blobFor(file) {
        const k = fp(file);
        if (!blobUrls.has(k)) blobUrls.set(k, URL.createObjectURL(file));
        return blobUrls.get(k);
    }
    function releaseBlobs() {
        // Only OUR stage URLs — the tray chips' own blob URLs are owned (and
        // revoked) by index.js's removePreviewItem/clearAttachments.
        blobUrls.forEach((u) => { try { URL.revokeObjectURL(u); } catch (_) {} });
        blobUrls.clear();
    }

    function build() {
        root = $id("attach-viewer");
        if (root) return true;
        root = document.createElement("div");
        root.id = "attach-viewer";
        root.tabIndex = -1;
        root.setAttribute("role", "dialog");
        root.setAttribute("aria-label", "Attachment preview");

        root.innerHTML =
            `<div class="cbm-head">` +
                `<span class="cbm-avatar" aria-hidden="true">+</span>` +
                `<div class="cbm-sender">` +
                    `<span class="cbm-sender-name">Ready to send</span>` +
                    `<span class="cbm-sender-time"></span>` +
                `</div>` +
                `<div class="cbm-head-spacer"></div>` +
                `<span class="cbm-counter" aria-live="polite"></span>` +
                `<button type="button" class="cbm-btn cbm-danger" data-cba="remove" title="Remove this attachment" aria-label="Remove this attachment"><span class="icon trash"></span></button>` +
                `<button type="button" class="cbm-btn" data-cba="strip" title="Toggle filmstrip (T)" aria-label="Toggle filmstrip" aria-pressed="true"><span class="icon filmstrip"></span></button>` +
                `<button type="button" class="cbm-btn" data-cba="close" title="Back to chat (Esc) — keeps attachments" aria-label="Close preview"><span class="icon close"></span></button>` +
            `</div>` +
            `<div class="cbm-stage">` +
                `<div class="cbm-stage-media"></div>` +
                `<button type="button" class="cbm-edge cbm-edge-prev" data-cba="prev" aria-label="Previous attachment"><span class="icon chev-left"></span></button>` +
                `<button type="button" class="cbm-edge cbm-edge-next" data-cba="next" aria-label="Next attachment"><span class="icon chev-right"></span></button>` +
            `</div>` +
            `<div class="cbm-strip" hidden><div class="cbm-strip-rail" role="listbox" aria-label="Attachments"></div></div>` +
            `<div class="cbm-caption-row">` +
                `<div class="cbm-input" contenteditable="true" placeholder="Add a caption…" aria-label="Caption for this attachment"></div>` +
                `<button type="button" class="cbm-send" data-cba="send" title="Send (Enter)" aria-label="Send attachments"><span class="icon send"></span></button>` +
            `</div>`;

        document.body.appendChild(root);

        el.name     = qs(".cbm-sender-name", root);
        el.sub      = qs(".cbm-sender-time", root);
        el.counter  = qs(".cbm-counter", root);
        el.stageBox = qs(".cbm-stage-media", root);
        el.prev     = qs(".cbm-edge-prev", root);
        el.next     = qs(".cbm-edge-next", root);
        el.strip    = qs(".cbm-strip", root);
        el.rail     = qs(".cbm-strip-rail", root);
        el.input    = qs(".cbm-input", root);
        el.stripBtn = qs('[data-cba="strip"]', root);

        wire();
        return true;
    }

    function wire() {
        root.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-cba]");
            if (!btn) return;
            e.stopPropagation();
            switch (btn.dataset.cba) {
                case "close":  close(); break;
                case "prev":   nav(-1); break;
                case "next":   nav(1);  break;
                case "strip":  toggleStrip(); break;
                case "send":   send(); break;
                case "remove": removeCurrent(); break;
            }
        });
        root.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && e.target === el.input && !e.shiftKey) {
                e.preventDefault();
                send();
                return;
            }
            if (isTypingTarget(e.target)) return;
            if (e.key === "ArrowLeft")       { e.preventDefault(); nav(-1); }
            else if (e.key === "ArrowRight") { e.preventDefault(); nav(1); }
            else if (e.key === "t" || e.key === "T") { e.preventDefault(); toggleStrip(); }
        });
        // Caption belongs to the CURRENTLY VIEWED attachment — saved on
        // every keystroke, restored on every navigation.
        el.input.addEventListener("input", () => {
            const f = files[index];
            if (f) captions.set(fp(f), el.input.innerText.replace(/\n$/, ""));
        }, { passive: true });
        el.rail.addEventListener("click", (e) => {
            const t = e.target.closest(".cbm-thumb");
            if (!t) return;
            const i = parseInt(t.dataset.index, 10);
            if (Number.isFinite(i)) show(i);
        });

        let swipe = null;
        root.addEventListener("pointerdown", (e) => {
            if (e.pointerType === "mouse") return;
            if (e.target.closest(".cbm-strip, .cbm-caption-row, .cbm-head, .cbm-edge, video, audio")) return;
            swipe = { x: e.clientX, y: e.clientY, id: e.pointerId };
        }, { passive: true });
        root.addEventListener("pointerup", (e) => {
            if (!swipe || e.pointerId !== swipe.id) return;
            const dx = e.clientX - swipe.x, dy = e.clientY - swipe.y;
            swipe = null;
            if (Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy) * 1.4) nav(dx < 0 ? 1 : -1);
        }, { passive: true });
        root.addEventListener("pointercancel", () => { swipe = null; }, { passive: true });
    }

    function openWith(startFile) {
        if (typeof attachmentFiles === "undefined" || !attachmentFiles.length) return;
        if (!root && !build()) return;
        files = attachmentFiles.slice();
        index = Math.max(0, startFile ? files.indexOf(startFile) : 0);
        open = true;
        stripVisible = files.length > 1;
        el.stripBtn.setAttribute("aria-pressed", stripVisible ? "true" : "false");
        el.strip.hidden = !stripVisible;

        root.classList.add("cbm-open");
        requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add("cbm-shown")));

        if (stripVisible) buildStrip();
        show(index);
        BackStack.syncGuard();
        try { root.focus({ preventScroll: true }); } catch (_) { root.focus(); }
    }

    function close() {
        if (!open) return;
        open = false;
        // Persist captions onto the chips so nothing is lost when the
        // composer closes — the send patch reads them back from here.
        persistCaptionsToChips();
        releaseBlobs();
        root.classList.remove("cbm-shown");
        const done = () => {
            root.classList.remove("cbm-open");
            clearStage();
        };
        if (reducedMotion()) done(); else setTimeout(done, 200);
        BackStack.syncGuard();
        try { const mi = $id("message-input"); mi && mi.focus({ preventScroll: true }); } catch (_) {}
    }

    function persistCaptionsToChips() {
        const list = $id("preview-list");
        if (!list) return;
        qsa(".preview-item", list).forEach((item) => {
            if (!item._file) return;
            const c = captions.get(fp(item._file));
            if (typeof c === "string" && c) item._caption = c;
        });
    }

    function nav(dir) {
        const n = index + dir;
        if (n < 0 || n >= files.length) return;
        show(n);
    }

    function clearStage() {
        qsa(".cbm-media, .cbm-filecard, .cbm-audio", el.stageBox).forEach((m) => {
            const av = (m.tagName === "VIDEO" || m.tagName === "AUDIO") ? m : qs("video, audio", m);
            if (av) {
                try { av.pause(); } catch (_) {}
                av.removeAttribute("src");
                try { av.load(); } catch (_) {}
            }
            m.remove();
        });
    }

    function show(i) {
        if (i < 0 || i >= files.length) return;
        index = i;
        const file = files[i];
        const kind = kindOf(file);
        root.dataset.navdir = "0";

        clearStage();
        let node;
        if (kind === "image") {
            node = document.createElement("img");
            node.className = "cbm-media";
            node.alt = "";
            node.decoding = "async";
            node.draggable = false;
            // Object URL beats a full data:URL re-read for big photos.
            node.src = blobFor(file);
        } else if (kind === "video") {
            node = document.createElement("video");
            node.className = "cbm-media";
            node.controls = true;
            node.playsInline = true;
            node.preload = "metadata";
            node.src = blobFor(file);
            try { if (typeof NowPlaying !== "undefined") NowPlaying.interrupt(); } catch (_) {}
        } else if (kind === "audio") {
            // Audio gets a real player — never forced into an image frame.
            node = document.createElement("div");
            node.className = "cbm-audio";
            node.innerHTML =
                `<span class="icon music"></span>` +
                `<span class="cbm-audio-name">${esc(file.name)}</span>` +
                `<span class="cbm-filecard-meta">${esc(fmtBytes(file.size))}</span>`;
            const audio = document.createElement("audio");
            audio.controls = true;
            audio.preload = "metadata";
            audio.src = blobFor(file);
            node.appendChild(audio);
        } else {
            // Generic, non-previewable file: clean card, zero decode work.
            node = document.createElement("div");
            node.className = "cbm-filecard";
            const ext = (file.name.split(".").pop() || "").slice(0, 8);
            node.innerHTML =
                `<span class="icon file"></span>` +
                `<span class="cbm-filecard-name">${esc(file.name)}` +
                (ext ? `<span class="cbm-filecard-ext">${esc(ext)}</span>` : "") +
                `</span>` +
                `<span class="cbm-filecard-meta">${esc(fmtBytes(file.size))} · No preview available</span>`;
        }
        el.stageBox.appendChild(node);
        if (node.classList.contains("cbm-media")) {
            requestAnimationFrame(() => requestAnimationFrame(() => node.classList.add("cbm-media-in")));
        }

        el.name.textContent = file.name;
        el.sub.textContent  = `${fmtBytes(file.size)} · ${kind === "file" ? "File" : kind[0].toUpperCase() + kind.slice(1)}`;
        el.counter.textContent = files.length > 1 ? `${i + 1} of ${files.length}` : "";
        el.prev.hidden = i <= 0;
        el.next.hidden = i >= files.length - 1;

        // This attachment's OWN caption — switching never clobbers another's.
        el.input.innerText = captions.get(fp(file)) || "";

        syncStripSelection();
    }

    function stripThumbFor(file) {
        // Reuse the poster/dataURL the tray chip already shows (captured by
        // media-smooth.js / index.js) — no second decode pass.
        const list = $id("preview-list");
        if (!list) return null;
        const chip = qsa(".preview-item", list).find((it) => it._file === file);
        if (!chip) return null;
        const t = qs(".ms-tray-thumb img", chip) || qs("img.preview-img", chip);
        return (t && t.src) || null;
    }

    function buildStrip() {
        el.rail.innerHTML = "";
        const frag = document.createDocumentFragment();
        files.forEach((file, i) => {
            const kind = kindOf(file);
            const b = document.createElement("button");
            b.type = "button";
            b.className = "cbm-thumb";
            b.dataset.index = String(i);
            b.setAttribute("role", "option");
            b.setAttribute("aria-label", `${kind} ${i + 1} of ${files.length}: ${file.name}`);
            const thumbSrc = stripThumbFor(file);
            if ((kind === "image" || kind === "video") && thumbSrc) {
                const img = document.createElement("img");
                img.loading = "lazy"; img.decoding = "async"; img.alt = "";
                img.src = thumbSrc;
                b.appendChild(img);
            } else if (kind === "image") {
                const img = document.createElement("img");
                img.loading = "lazy"; img.decoding = "async"; img.alt = "";
                img.src = blobFor(file);
                b.appendChild(img);
            } else {
                const badge = document.createElement("span");
                badge.className = "cbm-thumb-badge cbm-thumb-badge--static";
                badge.innerHTML = `<span class="icon ${kind === "video" ? "play" : kind === "audio" ? "music" : "file"}"></span>`;
                b.appendChild(badge);
            }
            frag.appendChild(b);
        });
        el.rail.appendChild(frag);
    }

    function syncStripSelection() {
        if (!stripVisible) return;
        qsa(".cbm-thumb", el.rail).forEach((t) => {
            const cur = parseInt(t.dataset.index, 10) === index;
            t.classList.toggle("cbm-current", cur);
            t.setAttribute("aria-selected", cur ? "true" : "false");
            if (cur) {
                const tRect = t.getBoundingClientRect();
                const rRect = el.rail.getBoundingClientRect();
                if (tRect.left < rRect.left || tRect.right > rRect.right) {
                    t.scrollIntoView({ block: "nearest", inline: "center", behavior: reducedMotion() ? "auto" : "smooth" });
                }
            }
        });
    }

    function toggleStrip() {
        stripVisible = !stripVisible;
        el.stripBtn.setAttribute("aria-pressed", stripVisible ? "true" : "false");
        if (stripVisible) { el.strip.hidden = false; buildStrip(); syncStripSelection(); }
        else { el.strip.hidden = true; el.rail.innerHTML = ""; }
    }

    function removeCurrent() {
        const file = files[index];
        if (!file) return;
        captions.delete(fp(file));
        const list = $id("preview-list");
        if (list) {
            const chip = qsa(".preview-item", list).find((it) => it._file === file);
            if (chip) { try { removePreviewItem(chip); } catch (_) {} }
        }
        files.splice(index, 1);
        if (!files.length) { close(); return; }
        if (index >= files.length) index = files.length - 1;
        if (stripVisible) buildStrip();
        show(index);
    }

    function send() {
        persistCaptionsToChips();
        // Hand the caption map to the sendMessage() wrapper below.
        window.__cbPendingCaptions = new Map(captions);
        open = false;
        releaseBlobs();              // File objects are unaffected
        root.classList.remove("cbm-shown", "cbm-open");
        clearStage();
        captions.clear();
        BackStack.syncGuard();
        try { sendMessage(); } catch (_) {}
    }

    return { openWith, close, isOpen: () => open };
})();

/* ════════════════════════════════════════════════════════════════════════
   4. PATCHES into the existing app (wrap, never fork)
   ════════════════════════════════════════════════════════════════════════ */
function patchCore() {
    /* openMediaViewer — same signature every call site already uses.
       blob:/data: trustedLocal calls are UNSENT media → the composer.
       Everything else (sent chat bubbles) → the takeover viewer. */
    window.openMediaViewer = function (src, type, opts) {
        opts = opts || {};
        if (opts.trustedLocal && typeof attachmentFiles !== "undefined" && attachmentFiles.length) {
            // Find the File this URL belongs to so the composer opens on it.
            let startFile = null;
            const list = $id("preview-list");
            if (list) {
                const chip = qsa(".preview-item", list).find((it) => it._previewBlobUrl === src);
                if (chip) startFile = chip._file || null;
            }
            AttachViewer.openWith(startFile);
            return;
        }
        MediaViewer.openWithSrc(src, type);
    };
    window.openImageViewer = function (src) { window.openMediaViewer(src, "image"); };
    window.closeMediaViewer = function () { MediaViewer.close(); };

    /* sendMessage — honour per-attachment captions. The original upload
       loop is byte-for-byte untouched; we wrap FormData.prototype.append
       for the duration of the send so each file's FormData gains its own
       caption (and a chat-text caption can never overwrite a per-file one).
       Sequential uploads inside sendMessage mean one FormData at a time,
       so a single "did we set the caption" flag per instance is exact. */
    if (typeof window.sendMessage === "function" && !window.sendMessage.__cbCaptionPatched) {
        const origSend = window.sendMessage;
        const patched = async function () {
            const pending = window.__cbPendingCaptions;
            window.__cbPendingCaptions = null;
            const map = (pending instanceof Map && pending.size) ? pending : readChipCaptions();
            if (!map || !map.size) return origSend();

            const fpOf = (f) => typeof fileFingerprint === "function" ? fileFingerprint(f)
                : `${f.name}|${f.size}|${f.lastModified || 0}|${f.type}`;

            const origAppend = FormData.prototype.append;
            FormData.prototype.append = function (name, value) {
                if (name === "file" && value instanceof File) {
                    origAppend.call(this, name, value);
                    const cap = map.get(fpOf(value));
                    if (typeof cap === "string" && cap) {
                        origAppend.call(this, "caption", cap);
                        this.__cbCaptionSet = true;
                    }
                    return;
                }
                // A per-file caption already set wins over anything else.
                if (name === "caption" && this.__cbCaptionSet) return;
                return origAppend.call(this, name, value);
            };
            try {
                return await origSend();
            } finally {
                FormData.prototype.append = origAppend;
            }
        };
        patched.__cbCaptionPatched = true;
        window.sendMessage = patched;
    }
}

/* Captions persisted onto chips by a previous composer session. */
function readChipCaptions() {
    const map = new Map();
    const list = $id("preview-list");
    if (!list) return map;
    qsa(".preview-item", list).forEach((item) => {
        if (!item._file || typeof item._caption !== "string" || !item._caption) return;
        const key = typeof fileFingerprint === "function" ? fileFingerprint(item._file)
            : `${item._file.name}|${item._file.size}|${item._file.lastModified || 0}|${item._file.type}`;
        map.set(key, item._caption);
    });
    return map;
}

/* Clicking a preview chip opens the full composer. Image chips already
   route through openMediaViewer (patched above); this gives every other
   chip type — and the empty chip background — the same door, with a
   drag-guard so reordering a chip never pops the composer open. */
function wireTrayEntryPoints() {
    const list = $id("preview-list");
    if (!list || list.__cbComposerWired) return;
    list.__cbComposerWired = true;

    let downPos = null;
    list.addEventListener("pointerdown", (e) => { downPos = { x: e.clientX, y: e.clientY }; }, { passive: true });
    list.addEventListener("click", (e) => {
        // Reorder drags end in a click too — swallow those.
        if (downPos && Math.hypot(e.clientX - downPos.x, e.clientY - downPos.y) > 6) { downPos = null; return; }
        downPos = null;
        if (e.target.closest(".preview-remove, .preview-media-btn, img.preview-img")) return; // their own handlers
        const item = e.target.closest(".preview-item");
        if (!item || !item._file) return;
        AttachViewer.openWith(item._file);
    });

    // Header affordance: a "Preview" button next to the existing Clear all.
    const area = $id("attachment-preview");
    if (area && !$id("cb-composer-open-btn")) {
        const btn = document.createElement("button");
        btn.id = "cb-composer-open-btn";
        btn.type = "button";
        btn.textContent = "Preview";
        btn.title = "Preview & caption attachments";
        btn.addEventListener("click", () => AttachViewer.openWith(null));
        const clearBtn = $id("cancel-attach");
        if (clearBtn) area.insertBefore(btn, clearBtn);
        else area.appendChild(btn);
    }
}

/* ════════════════════════════════════════════════════════════════════════
   5. REGISTRATION + BOOT
   ════════════════════════════════════════════════════════════════════════ */
function init() {
    registerBuiltinLayers();

    BackStack.register({   // in-viewer reply composer closes before the viewer
        id: "media-viewer-reply", priority: 2,
        isOpen: () => MediaViewer.isReplyOpen(),
        close:  () => MediaViewer.closeReply(),
    });
    BackStack.register({
        id: "media-viewer", priority: 5,
        isOpen: () => MediaViewer.isOpen(),
        close:  () => MediaViewer.close(),
    });
    BackStack.register({
        id: "attach-viewer", priority: 5,
        isOpen: () => AttachViewer.isOpen(),
        close:  () => AttachViewer.close(),
    });

    MediaViewer._build();
    patchCore();
    wireTrayEntryPoints();
    BackStack.init();

    // Public handles for future layers — same contract as __cbMsgActions.api.
    window.__cbMediaViewer.api = {
        openViewer:    (src, type) => MediaViewer.openWithSrc(src, type),
        closeViewer:   () => MediaViewer.close(),
        openComposer:  () => AttachViewer.openWith(null),
        closeComposer: () => AttachViewer.close(),
        back: BackStack,
    };
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();

})();
