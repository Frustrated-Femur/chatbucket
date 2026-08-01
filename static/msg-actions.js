// ─────────────────────────────────────────────────────────────────────────────
//  ChatBucket — msg-actions.js   (PATCH LAYER · loaded AFTER index.js and
//  perf-overlay.js, exactly like perf-overlay does: nothing in index.js is
//  edited, hot functions are wrapped and the originals are always kept.)
//
//  WHAT IT DOES
//  ────────────
//  [1] TOUCH DE-CROWDING   Phones keep ONLY the copy button in the bubble;
//                          edit/delete/reply/select move into a long-press
//                          tray. Desktop hover cluster is untouched (the PC
//                          layout was explicitly fine as-is).
//  [2] PEEK                On touch, .msg-actions is invisible until that one
//                          message is tapped (it used to sit at opacity .55
//                          on every single bubble, permanently).
//  [3] TRAY                Right-click (desktop) / long-press (touch) opens a
//                          fixed-position tray: Reply · Copy · Save · Edit ·
//                          Select · Delete. position:fixed + no backdrop + no
//                          scroll lock ⇒ ZERO layout shift, page never moves.
//  [4] COPY, FIXED         Copy used to read msgEl.dataset.previewText, which
//                          for a file bubble is the FILENAME — so "copy" on an
//                          image copied "IMG_2231.jpg". Now copy resolves real
//                          text only (message text, or a file's caption), a
//                          media share resolves its LINK, and when there is no
//                          text at all the button is removed instead of lying.
//                          Files get a "Save" action in the tray instead.
//  [5] MULTI-SELECT        Long-press → Select (or Ctrl/⌘-click, Shift-click
//                          for a range) → bulk delete in one confirm.
//  [6] MOBILE ENTER        On touch, Enter inserts a newline; sending is the
//                          send button only (Ctrl/⌘+Enter still sends for
//                          anyone on a phone with a hardware keyboard).
//
//  FAILURE POLICY: every wrapper try/catches and falls back to the original
//  behaviour, so a bug in this layer degrades to "stock ChatBucket".
// ─────────────────────────────────────────────────────────────────────────────

(function () {
"use strict";

if (window.__cbMsgActions) return;      // idempotent — double-include is safe
window.__cbMsgActions = { version: 1 };

// ── tunables ────────────────────────────────────────────────────────────────
const LONGPRESS_MS     = 420;   // hold before the tray opens
const PRESS_HINT_MS    = 165;   // hold before the squeeze feedback appears
const MOVE_TOLERANCE   = 10;    // px of finger travel that cancels a hold
const PEEK_MS          = 2600;  // how long a tapped bubble shows its copy btn
const CONFIRM_MS       = 3500;  // armed-delete window (matches index.js)
const TRAY_GRACE_MS    = 320;   // ignore scroll-close right after opening
const DEL_BATCH        = 8;     // bulk delete: frames per burst
const DEL_BATCH_GAP    = 45;    // ms between bursts (keeps the WS loop breathing)
const DEL_TIMEOUT_MS   = 8000;  // give up waiting for tombstone broadcasts

// ── tiny helpers ────────────────────────────────────────────────────────────
const $id   = (id) => document.getElementById(id);
const msgsEl = () => $id("messages");

// index.js declares `username` / `socket` with top-level `let`, which lives in
// the shared global lexical scope (NOT on window) — a bare reference from this
// classic script resolves to it, so read them through typeof-guarded getters.
const me    = () => { try { return username; } catch (_) { return null; } };
const sock  = () => { try { return socket;   } catch (_) { return null; } };
const toast = (m) => { try { showToast(m); } catch (_) { console.info("[cb]", m); } };
const isTouch = () => document.body.classList.contains("cb-touch");
const buzz  = (ms) => { try { navigator.vibrate && navigator.vibrate(ms); } catch (_) {} };

function wsSend(obj) {
    const s = sock();
    if (!s || s.readyState !== 1) return false;
    try { s.send(JSON.stringify(obj)); return true; } catch (_) { return false; }
}
function byId(msgId) {
    try { return document.querySelector(`[data-msgid="${CSS.escape(msgId)}"]`); }
    catch (_) { return null; }
}
const isOwn     = (el) => !!el && el.dataset.user === me();
const isDeleted = (el) => !!el && el.classList.contains("message--deleted");

// ── what does "copy" actually mean for this bubble? ──────────────────────────
// Resolved from the LIVE DOM, never from dataset.previewText — previewText is
// a notification/reply-quote label (filename for files, video title for YT),
// which is precisely why the old copy path pasted filenames.
//   · text bubble          → its text
//   · file/media + caption → the caption (requirement: text still copies)
//   · YouTube / yt-dlp     → the youtu.be link (a share has no text, but it
//                            does have a canonical address worth pasting)
//   · bare file / sticker  → null  ⇒ no copy button at all
function copyPayload(msgEl) {
    if (!msgEl || isDeleted(msgEl)) return null;

    const textEl = msgEl.querySelector(".bubble-caption, .bubble-text:not(.deleted-text)");
    if (textEl) {
        // innerText where available (it honours <br> and hidden nodes), but never
        // *depend* on it: .bubble-text/.bubble-caption hold nothing but the
        // message body — the timestamp, (edited) badge and action cluster are
        // siblings — so textContent is a faithful and much more portable read.
        const raw = textEl.innerText != null ? textEl.innerText : textEl.textContent;
        const s = String(raw || "").replace(/\u00a0/g, " ").trim();
        if (s) return { text: s, kind: "text" };
    }
    const yt = msgEl.querySelector("[data-yt-video-id]");
    if (yt && yt.dataset.ytVideoId) {
        return { text: "https://youtu.be/" + yt.dataset.ytVideoId, kind: "link" };
    }
    const au = msgEl.querySelector('audio[src*="/api/music/stream/"]');
    if (au) {
        const id = decodeURIComponent((au.getAttribute("src") || "").split("/").pop() || "");
        if (id) return { text: "https://youtu.be/" + id, kind: "link" };
    }
    return null;
}

// The downloadable/openable asset behind a bubble, if any.
function mediaTarget(msgEl) {
    if (!msgEl || isDeleted(msgEl)) return null;
    const v = msgEl.querySelector("[data-viewer-src]");
    if (v && v.dataset.viewerSrc) {
        return {
            href: v.dataset.viewerSrc,
            kind: v.dataset.viewerType === "video" ? "video" : "image",
            sticker: !!msgEl.querySelector(".chat-sticker"),
        };
    }
    const a = msgEl.querySelector('audio.chat-audio:not([src*="/api/music/stream/"])');
    if (a && a.getAttribute("src")) return { href: a.getAttribute("src"), kind: "audio" };
    const l = msgEl.querySelector("a.file-link[href]");
    if (l) return { href: l.getAttribute("href"), kind: "file" };
    return null;
}

function canEditMsg(msgEl) {
    if (!isOwn(msgEl) || isDeleted(msgEl)) return false;
    if (!msgEl.querySelector(".bubble-caption, .bubble-text:not(.deleted-text)")) return false;
    if (msgEl.querySelector(".chat-sticker")) return false;                       // stickers: no text field
    if (msgEl.querySelector("[data-yt-video-id]")) return false;                  // youtube share
    if (msgEl.querySelector('audio[src*="/api/music/stream/"]')) return false;    // yt-dlp share
    try { if (isEditWindowExpired(msgEl.dataset.ts)) return false; } catch (_) {}
    return true;
}

// ── clipboard ───────────────────────────────────────────────────────────────
// Reuse index.js's copyTextToClipboard (it already handles this deployment's
// http:// / no-secure-context reality via execCommand); fall back if absent.
function writeText(text) {
    try {
        if (typeof copyTextToClipboard === "function") return copyTextToClipboard(text);
    } catch (_) {}
    return Promise.resolve(false);
}

// Public, replaces index.js's copyMessageText — the delegated .copy-btn click
// handler in showChat() calls it by global name, so this override is enough.
function cbCopyMessage(msgEl, opts) {
    const fromTray = !!(opts && opts.fromTray);
    const pay = copyPayload(msgEl);
    if (!pay) { toast("Nothing to copy here — use Save to keep the file."); return; }
    const btn = msgEl.querySelector(".copy-btn");
    Promise.resolve(writeText(pay.text)).then(ok => {
        if (!ok) { toast("Couldn't copy to clipboard."); return; }
        if (btn && !fromTray && typeof showCopiedIndicator === "function") showCopiedIndicator(btn);
        else toast(pay.kind === "link" ? "Link copied." : "Copied.");
    });
}
window.copyMessageText = function (msgEl) { cbCopyMessage(msgEl); };

// ── per-message refinement ──────────────────────────────────────────────────
// Runs once per rendered .message (via the buildMessageEl wrapper, with a
// MutationObserver as a safety net for any path that bypasses it).
function refine(msgEl) {
    try {
        if (!msgEl || msgEl.nodeType !== 1 || !msgEl.classList.contains("message")) return msgEl;

        const pay     = copyPayload(msgEl);
        const cluster = msgEl.querySelector(".msg-actions");
        msgEl.dataset.cbCopy = pay ? pay.kind : "none";

        if (cluster) {
            const copyBtn = cluster.querySelector(".copy-btn");
            if (copyBtn) {
                if (!pay) {
                    // CSS hides it; also make it unreachable by tab/AT.
                    copyBtn.setAttribute("aria-hidden", "true");
                    copyBtn.tabIndex = -1;
                } else if (pay.kind === "link") {
                    copyBtn.title = "Copy link";
                    copyBtn.setAttribute("aria-label", "Copy link");
                }
            }
            // A cluster with nothing in it is chrome for nothing (e.g. someone
            // else's bare image: no copy, no edit, no delete) — drop the node.
            const useful = Array.from(cluster.children).some(b =>
                !(b.classList.contains("copy-btn") && !pay));
            if (!useful) cluster.remove();
        }
        if (Sel.on) ensureCheck(msgEl);
    } catch (err) { console.warn("[cb] refine failed", err); }
    return msgEl;
}

// Wrap the builder (same monkey-patch strategy perf-overlay.js already uses;
// top-level function declarations are writable window properties, and
// index.js's internal calls resolve through the global object).
if (typeof window.buildMessageEl === "function") {
    const origBuild = window.buildMessageEl;
    window.buildMessageEl = function () {
        let el;
        try { el = origBuild.apply(this, arguments); } catch (err) { throw err; }
        return refine(el);
    };
}

// Safety net + select-mode chip injection for messages added by any other path.
function observeMessages() {
    const host = msgsEl();
    if (!host) return;
    const mo = new MutationObserver(muts => {
        for (const m of muts) {
            for (const n of m.addedNodes) {
                if (n.nodeType !== 1) continue;
                if (n.classList && n.classList.contains("message")) refine(n);
                else if (n.querySelectorAll) n.querySelectorAll(".message").forEach(refine);
            }
        }
    });
    mo.observe(host, { childList: true, subtree: true });
    host.querySelectorAll(".message").forEach(refine);
}

// ═══ POINTER MODE ═══════════════════════════════════════════════════════════
// Last-input-wins instead of a one-shot media query, so a 2-in-1 / tablet with
// a keyboard+mouse gets desktop affordances the moment a mouse is used, and
// touch affordances the moment a finger is used — no reload, no wrong guess.
// Defensive: a throw at this top level would take the ENTIRE patch layer down
// (and with it copy/edit/delete), so never assume matchMedia exists.
const coarse = (function () {
    try {
        if (typeof window.matchMedia === "function") return window.matchMedia("(hover: none), (pointer: coarse)");
    } catch (_) {}
    return { matches: (navigator.maxTouchPoints || 0) > 0, addEventListener() {}, addListener() {} };
})();

function setTouchMode(on) {
    const b = document.body;
    if (!b) return;
    if (b.classList.contains("cb-touch") === !!on) return;
    b.classList.toggle("cb-touch", !!on);
    const input = $id("message-input");
    if (input) {
        // Ask the soft keyboard for a newline glyph rather than a "send"/"go"
        // key — the visual half of the Enter change below.
        if (on) input.setAttribute("enterkeyhint", "enter");
        else    input.removeAttribute("enterkeyhint");
    }
    if (!on) clearPeek();
}
try { coarse.addEventListener("change", e => setTouchMode(e.matches)); }
catch (_) { try { coarse.addListener(e => setTouchMode(e.matches)); } catch (__) {} }

window.addEventListener("pointerdown", e => {
    if (e.pointerType === "touch" || e.pointerType === "pen") setTouchMode(true);
    else if (e.pointerType === "mouse") setTouchMode(false);
}, { capture: true, passive: true });

// ═══ PEEK (touch: reveal one bubble's copy button) ═══════════════════════════
let _peekEl = null, _peekTimer = 0;
function clearPeek() {
    clearTimeout(_peekTimer);
    if (_peekEl) _peekEl.classList.remove("cb-peek");
    _peekEl = null;
}
function peek(msgEl) {
    if (!msgEl) return;
    if (_peekEl === msgEl) { clearPeek(); return; }   // second tap = hide again
    clearPeek();
    _peekEl = msgEl;
    msgEl.classList.add("cb-peek");
    _peekTimer = setTimeout(clearPeek, PEEK_MS);
}
// Anything that already does something on tap must not also trigger a peek.
const INTERACTIVE = "a[href], button, audio, video, input, .msg-actions, .reply-quote," +
                    " [data-viewer-src], .yt-bubble-thumb-wrap, .file-link";

// ═══ THE TRAY ═══════════════════════════════════════════════════════════════
let tray = null;             // the single reused DOM node
let trayMsgId = null;        // message it's acting on
let trayOpen = false;        // authoritative state — NOT derived from classes,
                             // which linger through the 180ms fade-out and made
                             // Escape close an already-closing tray instead of
                             // falling through to exit selection mode.
let trayOpenedAt = 0;
let trayHideTimer = 0;
let trayArmTimer = 0;

function buildTray() {
    if (tray) return tray;
    tray = document.createElement("div");
    tray.id = "cb-tray";
    tray.setAttribute("role", "menu");
    tray.setAttribute("aria-label", "Message actions");
    document.body.appendChild(tray);
    tray.addEventListener("click", onTrayClick);
    tray.addEventListener("keydown", onTrayKeydown);
    // Never let a stray press inside the tray reach the dismiss listener.
    tray.addEventListener("pointerdown", e => e.stopPropagation());
    return tray;
}

function mi(act, label, iconHTML, extra) {
    const o = extra || {};
    return `<button class="cb-mi${o.danger ? " is-danger" : ""}" role="menuitem" type="button"` +
           ` data-act="${act}" style="--i:${o.i || 0}">` +
           iconHTML +
           `<span class="cb-mi-lbl">${label}</span>` +
           (o.key ? `<span class="cb-mi-key">${o.key}</span>` : "") +
           `</button>`;
}
const ico     = (n) => `<span class="cb-ico ${n}" aria-hidden="true"></span>`;
const baseIco = (n) => `<span class="icon ${n}" aria-hidden="true"></span>`;

// Build the item list for one message. Everything is conditional — the tray
// only ever shows actions that will actually work on THIS bubble.
function trayHTML(msgEl) {
    const pay     = copyPayload(msgEl);
    const media   = mediaTarget(msgEl);
    const own     = isOwn(msgEl);
    const dead    = isDeleted(msgEl);
    const author  = (msgEl.dataset.user || "").slice(0, 18);
    const secure  = !!(window.isSecureContext && window.ClipboardItem && navigator.clipboard &&
                       navigator.clipboard.write);

    let i = 0, out = "";
    out += `<div class="cb-tray-head"><span>MESSAGE</span><b>${escapeSafe(author)}</b></div>`;

    if (!dead) out += mi("reply", "Reply", ico("reply"), { i: i++, key: isTouch() ? "" : "2×CLICK" });

    if (pay) {
        out += mi("copy", pay.kind === "link" ? "Copy link" : "Copy text",
                  pay.kind === "link" ? ico("link") : baseIco("copy"), { i: i++ });
    }
    if (media && secure && media.kind === "image") {
        out += mi("copyimg", "Copy image", baseIco("copy"), { i: i++ });
    }
    if (media) {
        const lbl = media.sticker ? "Save sticker"
                  : media.kind === "image" ? "Save image"
                  : media.kind === "video" ? "Save video"
                  : media.kind === "audio" ? "Save audio" : "Save file";
        out += mi("save", lbl, ico("save"), { i: i++ });
    }
    if (canEditMsg(msgEl)) out += mi("edit", "Edit", baseIco("edit"), { i: i++ });

    out += mi("select", "Select", ico("select"), { i: i++, key: isTouch() ? "" : "CTRL+CLICK" });

    if (own && !dead) {
        out += `<div class="cb-tray-sep" role="separator"></div>`;
        out += mi("delete", "Delete", baseIco("trash"), { i: i++, danger: true });
    }
    return out;
}

// Minimal escape — index.js's escapeHTML may have been wrapped by perf-overlay,
// so don't depend on its identity; this is the only spot we interpolate.
function escapeSafe(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Position: pure position:fixed maths with viewport flipping. No anchor
// element, no layout participation — the document cannot shift because of it.
function placeTray(x, y) {
    const pad = 8;
    const w = tray.offsetWidth, h = tray.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight;
    let left = x, top = y, ox = "left", oy = "top";

    if (left + w + pad > vw) { left = x - w; ox = "right"; }
    if (left < pad) { left = pad; }
    if (top + h + pad > vh) { top = y - h; oy = "bottom"; }
    if (top < pad) { top = pad; }

    tray.style.left = Math.round(left) + "px";
    tray.style.top  = Math.round(top) + "px";
    tray.style.transformOrigin = oy + " " + ox;
}

function openTray(msgEl, x, y) {
    if (!msgEl) return;
    if (Sel.on) return;                       // selection mode owns taps
    closeTray(true);
    buildTray();

    tray.innerHTML = trayHTML(msgEl);
    trayMsgId = msgEl.dataset.msgid || null;
    trayOpenedAt = Date.now();
    trayOpen = true;

    document.querySelectorAll(".message.cb-anchored").forEach(el => el.classList.remove("cb-anchored"));
    msgEl.classList.add("cb-anchored");

    clearTimeout(trayHideTimer);
    tray.classList.add("is-mounted");
    tray.classList.remove("is-open");
    placeTray(x, y);                          // measured while mounted, still opacity:0
    requestAnimationFrame(() => tray.classList.add("is-open"));

    // Keyboard users land on the first item; preventScroll so a fixed element
    // focus can never nudge the message list.
    if (!isTouch()) {
        const first = tray.querySelector(".cb-mi");
        if (first) { try { first.focus({ preventScroll: true }); } catch (_) { first.focus(); } }
    }
    addDismissListeners();
}

function closeTray(immediate) {
    trayOpen = false;
    disarmTray();
    removeDismissListeners();
    document.querySelectorAll(".message.cb-anchored").forEach(el => el.classList.remove("cb-anchored"));
    trayMsgId = null;
    if (!tray) return;
    tray.classList.remove("is-open");
    clearTimeout(trayHideTimer);
    if (immediate) { tray.classList.remove("is-mounted"); tray.innerHTML = ""; return; }
    trayHideTimer = setTimeout(() => {
        if (!tray.classList.contains("is-open")) { tray.classList.remove("is-mounted"); tray.innerHTML = ""; }
    }, 180);
}
const trayIsOpen = () => trayOpen;

function disarmTray() {
    clearTimeout(trayArmTimer);
    if (!tray) return;
    const armed = tray.querySelector(".cb-mi.is-armed");
    if (armed) {
        armed.classList.remove("is-armed");
        const lbl = armed.querySelector(".cb-mi-lbl");
        if (lbl) lbl.textContent = "Delete";
    }
}

// ── dismissal: outside press, scroll, resize, tab-away, Escape ──────────────
function onDocPointerDown(e) {
    if (tray && tray.contains(e.target)) return;
    closeTray();
}
function onAnyScroll() {
    if (Date.now() - trayOpenedAt < TRAY_GRACE_MS) return;   // ignore the jitter
    closeTray();                                              // of the opening gesture
}
function addDismissListeners() {
    document.addEventListener("pointerdown", onDocPointerDown, true);
    window.addEventListener("resize", closeTrayNow, { passive: true });
    window.addEventListener("blur", closeTrayNow);
    document.addEventListener("visibilitychange", closeTrayNow);
    const host = msgsEl();
    if (host) host.addEventListener("scroll", onAnyScroll, { passive: true });
    window.addEventListener("wheel", onAnyScroll, { passive: true });
}
function removeDismissListeners() {
    document.removeEventListener("pointerdown", onDocPointerDown, true);
    window.removeEventListener("resize", closeTrayNow);
    window.removeEventListener("blur", closeTrayNow);
    document.removeEventListener("visibilitychange", closeTrayNow);
    const host = msgsEl();
    if (host) host.removeEventListener("scroll", onAnyScroll);
    window.removeEventListener("wheel", onAnyScroll);
}
function closeTrayNow() { closeTray(true); }

// ── tray actions ────────────────────────────────────────────────────────────
function onTrayClick(e) {
    const item = e.target.closest(".cb-mi");
    if (!item) return;
    e.preventDefault();
    e.stopPropagation();
    const act = item.dataset.act;
    const msgEl = trayMsgId ? byId(trayMsgId) : null;
    if (!msgEl) { closeTray(); return; }

    switch (act) {
        case "reply":
            try { setReply(msgEl); } catch (_) {}
            closeTray();
            break;

        case "copy":
            // Must run inside this very click: the execCommand fallback that
            // this http:// deployment actually uses needs a live user gesture.
            cbCopyMessage(msgEl, { fromTray: true });
            closeTray();
            break;

        case "copyimg":
            copyImage(msgEl);
            closeTray();
            break;

        case "save":
            saveMedia(msgEl);
            closeTray();
            break;

        case "edit":
            closeTray(true);
            try { beginInlineEdit(msgEl); } catch (_) { toast("Couldn't start editing."); }
            break;

        case "select":
            closeTray(true);
            enterSelect(msgEl);
            break;

        case "delete": {
            // Two-step, in place: the item becomes its own confirmation. No
            // dialog, no overlay, nothing outside the tray changes.
            if (item.classList.contains("is-armed")) {
                disarmTray();
                if (!wsSend({ type: "delete", id: msgEl.dataset.msgid })) toast("Not connected.");
                closeTray();
                break;
            }
            disarmTray();
            item.classList.add("is-armed");
            const lbl = item.querySelector(".cb-mi-lbl");
            if (lbl) lbl.textContent = "Tap again to delete";
            buzz(8);
            trayArmTimer = setTimeout(disarmTray, CONFIRM_MS);
            break;
        }
    }
}

// Roving-focus keyboard nav so the tray is usable without a pointer.
function onTrayKeydown(e) {
    const items = Array.from(tray.querySelectorAll(".cb-mi"));
    if (!items.length) return;
    const cur = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = e.key === "ArrowDown"
            ? items[(cur + 1 + items.length) % items.length]
            : items[(cur - 1 + items.length) % items.length];
        try { next.focus({ preventScroll: true }); } catch (_) { next.focus(); }
    } else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
    else if (e.key === "End")    { e.preventDefault(); items[items.length - 1].focus(); }
}

// ── save / copy-image ───────────────────────────────────────────────────────
function saveMedia(msgEl) {
    const t = mediaTarget(msgEl);
    if (!t) { toast("Nothing to save here."); return; }
    let sameOrigin = true, name = "";
    try {
        const u = new URL(t.href, location.href);
        sameOrigin = u.origin === location.origin;
        name = decodeURIComponent((u.pathname.split("/").pop() || "")).replace(/^\d{8}_\d{6}_/, "");
    } catch (_) {}
    const a = document.createElement("a");
    a.href = t.href;
    a.rel = "noopener noreferrer";
    // `download` is ignored cross-origin (e.g. a giphy.com GIF) — in that case
    // open it in a tab so the user can still save it by hand, instead of
    // silently doing nothing.
    if (sameOrigin) a.download = name || "chatbucket-file";
    else a.target = "_blank";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(sameOrigin ? "Saving " + (name || "file") + "…" : "Opened in a new tab — long-press to save.");
}

async function copyImage(msgEl) {
    const t = mediaTarget(msgEl);
    if (!t || t.kind !== "image") return;
    try {
        const res  = await fetch(t.href, { credentials: "same-origin" });
        const blob = await res.blob();
        const type = blob.type && blob.type.startsWith("image/") ? blob.type : "image/png";
        await navigator.clipboard.write([new ClipboardItem({ [type]: blob })]);
        toast("Image copied.");
    } catch (_) {
        toast("Couldn't copy the image — use Save instead.");
    }
}

// ═══ GESTURES: right-click (desktop) · long-press (touch) ════════════════════
let press = null;   // { msgEl, x, y, hintTimer, fireTimer, fired, moved }

function cancelPress(keepFired) {
    if (!press) return;
    clearTimeout(press.hintTimer);
    clearTimeout(press.fireTimer);
    press.msgEl.classList.remove("cb-pressing");
    if (!keepFired) press = null;
}

// Swallow the click that a completed long-press leaves behind, so it can't
// also open the media viewer / mount a YT player underneath the tray.
function swallowNextClick() {
    const kill = (e) => {
        if (e.target.closest && e.target.closest("#cb-tray, #cb-select-bar")) return;
        e.stopPropagation();
        e.preventDefault();
        window.removeEventListener("click", kill, true);
    };
    window.addEventListener("click", kill, true);
    setTimeout(() => window.removeEventListener("click", kill, true), 700);
}

function onMsgPointerDown(e) {
    if (e.pointerType === "mouse") return;             // mouse uses contextmenu
    if (Sel.on) return;                                // selection owns taps
    const msgEl = e.target.closest(".message");
    if (!msgEl || isDeleted(msgEl)) return;

    cancelPress();
    press = {
        msgEl, x: e.clientX, y: e.clientY,
        fired: false, moved: false, hintTimer: 0, fireTimer: 0,
    };
    press.hintTimer = setTimeout(() => {
        if (press && !press.moved) press.msgEl.classList.add("cb-pressing");
    }, PRESS_HINT_MS);
    press.fireTimer = setTimeout(() => {
        if (!press || press.moved) return;
        press.fired = true;
        press.msgEl.classList.remove("cb-pressing");
        buzz(12);
        swallowNextClick();
        // Open a touch above the fingertip so the thumb isn't covering item #1.
        openTray(press.msgEl, press.x, Math.max(press.y - 12, 8));
    }, LONGPRESS_MS);
}

function onMsgPointerMove(e) {
    if (!press || press.fired) return;
    if (Math.abs(e.clientX - press.x) > MOVE_TOLERANCE ||
        Math.abs(e.clientY - press.y) > MOVE_TOLERANCE) {
        press.moved = true;
        cancelPress();
    }
}

function onMsgPointerUp(e) {
    if (!press) return;
    const { msgEl, fired, moved } = press;
    cancelPress();
    if (fired || moved) return;
    if (e.pointerType === "mouse") return;
    // A plain tap on a non-interactive part of the bubble reveals that one
    // message's copy button (requirement 4) — and nothing else.
    if (e.target.closest(INTERACTIVE)) return;
    peek(msgEl);
}

function onContextMenu(e) {
    const msgEl = e.target.closest(".message");
    if (!msgEl) return;
    // Android fires a synthetic contextmenu right after our own long-press
    // timer already opened the tray — suppress the native menu, ignore the rest.
    if (Date.now() - trayOpenedAt < 900) { e.preventDefault(); return; }
    if (Sel.on) { e.preventDefault(); return; }
    // Desktop: leave real links alone so "open in new tab" / "copy link
    // address" keep working. Everything else is ours.
    if (!isTouch() && e.target.closest("a[href]")) return;
    if (isDeleted(msgEl)) { e.preventDefault(); return; }
    e.preventDefault();
    openTray(msgEl, e.clientX, e.clientY);
}

// ═══ SELECTION MODE ═════════════════════════════════════════════════════════
// Entered by: long-press → Select (touch) · Ctrl/⌘-click or right-click →
// Select (desktop). Shift-click extends a range. Exit: Cancel, Escape, or
// deselecting the last message.
const Sel = { on: false, ids: new Set(), anchor: null };
let bar = null, barArmTimer = 0, delWatchdog = 0;

function ensureCheck(msgEl) {
    if (!msgEl || msgEl.querySelector(":scope > .cb-check")) return;
    const c = document.createElement("span");
    c.className = "cb-check";
    c.setAttribute("aria-hidden", "true");
    msgEl.appendChild(c);        // absolutely positioned ⇒ no reflow, no shift
}

function buildBar() {
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = "cb-select-bar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Selection actions");
    bar.innerHTML =
        `<span class="cb-count">0</span>` +
        `<span class="cb-count-lbl"><span>SELECTED</span><span class="cb-count-note"></span></span>` +
        `<button id="cb-sb-all" type="button" title="Select all of your messages">` +
            `<span class="cb-ico select"></span><span class="cb-lbl">All</span></button>` +
        `<button id="cb-sb-copy" type="button" title="Copy selected text">` +
            `<span class="icon copy"></span><span class="cb-lbl">Copy</span></button>` +
        `<button id="cb-sb-del" type="button" title="Delete selected">` +
            `<span class="icon trash"></span><span class="cb-lbl">Delete</span></button>` +
        `<button id="cb-sb-close" type="button" title="Cancel selection" aria-label="Cancel selection">` +
            `<span class="icon close sm"></span></button>`;
    document.body.appendChild(bar);

    bar.querySelector("#cb-sb-all").addEventListener("click", selectAllOwn);
    bar.querySelector("#cb-sb-copy").addEventListener("click", copySelection);
    bar.querySelector("#cb-sb-del").addEventListener("click", onBarDelete);
    bar.querySelector("#cb-sb-close").addEventListener("click", () => exitSelect());
    return bar;
}

// Mirror #header's exact height so the bar overlays the header and nothing
// else. Measured (not hard-coded) because the header's height depends on the
// logo, font metrics and the notch. Re-measured on resize/rotate.
function sizeBarToHeader() {
    const hdr = $id("header");
    if (!hdr || !bar) return;
    const h = Math.round(hdr.getBoundingClientRect().height);
    if (h <= 24) return;
    document.documentElement.style.setProperty("--cb-bar-h", h + "px");
    // min-height can't shrink content: if the bar's own controls are taller
    // than the header (short header / large text settings), tighten density
    // instead of overhanging into the message list.
    bar.classList.remove("is-compact");
    if (Math.round(bar.getBoundingClientRect().height) > h) bar.classList.add("is-compact");
}
window.addEventListener("resize", () => { if (Sel.on) sizeBarToHeader(); }, { passive: true });

const selectable = (el) => !!el && !isDeleted(el);
const deletableIds = () =>
    Array.from(Sel.ids).filter(id => { const el = byId(id); return el && isOwn(el) && !isDeleted(el); });

function updateBar() {
    if (!bar) return;
    const n    = Sel.ids.size;
    const dels = deletableIds().length;
    const foreign = n - dels;

    bar.querySelector(".cb-count").textContent = String(n);
    bar.querySelector(".cb-count-note").textContent =
        foreign > 0 ? foreign + " NOT YOURS" : "";

    const delBtn = bar.querySelector("#cb-sb-del");
    const lbl = delBtn.querySelector(".cb-lbl");
    if (!delBtn.classList.contains("is-armed")) lbl.textContent = dels ? "Delete " + dels : "Delete";
    delBtn.disabled = dels === 0;

    const copyable = Array.from(Sel.ids).some(id => copyPayload(byId(id)));
    bar.querySelector("#cb-sb-copy").disabled = !copyable;
}

function disarmBar() {
    clearTimeout(barArmTimer);
    if (!bar) return;
    const b = bar.querySelector("#cb-sb-del");
    b.classList.remove("is-armed");
    updateBar();
}

function enterSelect(seed) {
    if (!Sel.on) {
        Sel.on = true;
        document.body.classList.add("cb-select");
        buildBar();
        sizeBarToHeader();
        clearPeek();
        const host = msgsEl();
        if (host) host.querySelectorAll(".message").forEach(ensureCheck);
    }
    if (seed) toggleSelect(seed, { anchor: true });
    updateBar();
}

function exitSelect() {
    if (!Sel.on) return;
    Sel.on = false;
    Sel.anchor = null;
    disarmBar();
    document.body.classList.remove("cb-select");
    Sel.ids.forEach(id => { const el = byId(id); if (el) el.classList.remove("cb-sel"); });
    Sel.ids.clear();
}

function toggleSelect(msgEl, opts) {
    const o = opts || {};
    if (!msgEl) return;
    if (!selectable(msgEl)) {
        msgEl.classList.remove("cb-nope");
        void msgEl.offsetWidth;
        msgEl.classList.add("cb-nope");
        setTimeout(() => msgEl.classList.remove("cb-nope"), 320);
        return;
    }
    const id = msgEl.dataset.msgid;
    if (!id) return;
    ensureCheck(msgEl);

    if (Sel.ids.has(id) && !o.forceOn) {
        Sel.ids.delete(id);
        msgEl.classList.remove("cb-sel");
    } else {
        Sel.ids.add(id);
        msgEl.classList.add("cb-sel");
        buzz(6);
    }
    if (o.anchor !== false) Sel.anchor = id;
    disarmBar();
    if (Sel.ids.size === 0) { exitSelect(); return; }
    updateBar();
}

// Shift-click: everything between the anchor and here, in DOM order.
function selectRangeTo(msgEl) {
    const host = msgsEl();
    if (!host || !Sel.anchor) { toggleSelect(msgEl); return; }
    const rows = Array.from(host.querySelectorAll(".message"));
    const a = rows.findIndex(r => r.dataset.msgid === Sel.anchor);
    const b = rows.indexOf(msgEl);
    if (a < 0 || b < 0) { toggleSelect(msgEl); return; }
    const [lo, hi] = a <= b ? [a, b] : [b, a];
    for (let i = lo; i <= hi; i++) toggleSelect(rows[i], { forceOn: true, anchor: false });
    Sel.anchor = msgEl.dataset.msgid;
    updateBar();
}

function selectAllOwn() {
    const host = msgsEl();
    if (!host) return;
    const own = Array.from(host.querySelectorAll(".message.own-message")).filter(selectable);
    if (!own.length) { toast("Nothing of yours on screen to select."); return; }
    const allAlready = own.every(el => Sel.ids.has(el.dataset.msgid));
    if (allAlready) { own.forEach(el => toggleSelect(el, { anchor: false })); return; }  // toggle off
    own.forEach(el => toggleSelect(el, { forceOn: true, anchor: false }));
    updateBar();
}

function copySelection() {
    const host = msgsEl();
    if (!host) return;
    const rows = Array.from(host.querySelectorAll(".message")).filter(r => Sel.ids.has(r.dataset.msgid));
    const parts = [];
    for (const r of rows) {
        const p = copyPayload(r);
        if (!p) continue;
        parts.push(rows.length > 1 ? (r.dataset.user || "?") + ": " + p.text : p.text);
    }
    if (!parts.length) { toast("None of the selected messages have text."); return; }
    Promise.resolve(writeText(parts.join("\n"))).then(ok => {
        toast(ok ? "Copied " + parts.length + (parts.length > 1 ? " messages." : " message.")
                 : "Couldn't copy to clipboard.");
        if (ok) exitSelect();
    });
}

function onBarDelete() {
    const ids = deletableIds();
    if (!ids.length) return;
    const btn = bar.querySelector("#cb-sb-del");
    const lbl = btn.querySelector(".cb-lbl");

    if (!btn.classList.contains("is-armed")) {
        btn.classList.add("is-armed");
        lbl.textContent = "Confirm " + ids.length;
        buzz(8);
        barArmTimer = setTimeout(disarmBar, CONFIRM_MS);
        return;
    }
    clearTimeout(barArmTimer);
    btn.classList.remove("is-armed");
    bulkDelete(ids);
}

// Bulk delete = N authoritative `delete` frames (the protocol has no batch
// op, and inventing one would mean touching server.py). Sent in small bursts
// so a 40-message purge doesn't monopolise the WS loop, which does a file
// read + tombstone write + broadcast per id. The DOM is NOT touched
// optimistically: every client — including this one — updates from the
// server's own broadcast, so a rejected id simply never disappears.
function bulkDelete(ids) {
    const total = ids.length;
    ids.forEach(id => { const el = byId(id); if (el) el.classList.add("cb-deleting"); });
    exitSelect();
    toast("Deleting " + total + (total > 1 ? " messages…" : " message…"));

    let i = 0, failed = false;
    (function pump() {
        if (i >= ids.length) return;
        const slice = ids.slice(i, i + DEL_BATCH);
        i += DEL_BATCH;
        for (const id of slice) if (!wsSend({ type: "delete", id })) { failed = true; break; }
        if (failed) {
            toast("Connection dropped — some messages weren't deleted.");
            ids.forEach(id => { const el = byId(id); if (el) el.classList.remove("cb-deleting"); });
            return;
        }
        if (i < ids.length) setTimeout(pump, DEL_BATCH_GAP);
    })();

    clearTimeout(delWatchdog);
    delWatchdog = setTimeout(() => {
        const stuck = ids.filter(id => { const el = byId(id); return el && el.classList.contains("cb-deleting"); });
        stuck.forEach(id => { const el = byId(id); if (el) el.classList.remove("cb-deleting"); });
        if (stuck.length) toast(stuck.length + " message" + (stuck.length > 1 ? "s" : "") + " couldn't be deleted.");
    }, DEL_TIMEOUT_MS);
}

// The server's tombstone broadcast is the single source of truth — hook it so
// selection state and the pending pulse always match reality.
if (typeof window.applyDeleteToDOM === "function") {
    const origApplyDelete = window.applyDeleteToDOM;
    window.applyDeleteToDOM = function (id) {
        const r = origApplyDelete.apply(this, arguments);
        try {
            const el = byId(id);
            if (el) el.classList.remove("cb-deleting", "cb-sel", "cb-peek", "cb-anchored");
            if (Sel.ids.delete(id)) { if (Sel.ids.size === 0) exitSelect(); else updateBar(); }
            if (trayMsgId === id) closeTray(true);
        } catch (_) {}
        return r;
    };
}

// Swipe-to-reply must not fire while selecting (a tap-drag on a bubble would
// otherwise stage a reply behind the selection bar).
if (typeof window.setReply === "function") {
    const origSetReply = window.setReply;
    window.setReply = function () {
        if (Sel.on) return;
        return origSetReply.apply(this, arguments);
    };
}

// ── select-mode click routing (capture, so it beats index.js's delegated
//    handler on the same container: media viewer, YT mount, copy, etc.) ──────
function onMessagesClickCapture(e) {
    const msgEl = e.target.closest(".message");

    // Ctrl/⌘-click enters selection straight from normal mode (desktop).
    if (!Sel.on && msgEl && (e.ctrlKey || e.metaKey) && !isTouch()) {
        e.stopPropagation(); e.preventDefault();
        enterSelect(msgEl);
        return;
    }
    if (!Sel.on) return;

    e.stopPropagation();
    e.preventDefault();
    if (!msgEl) return;
    if (e.shiftKey) selectRangeTo(msgEl);
    else toggleSelect(msgEl);
}

// Same reasoning for the touch gestures index.js binds on #messages.
function blockWhileSelecting(e) {
    if (!Sel.on) return;
    e.stopPropagation();
}

// ═══ ENTER = NEWLINE ON TOUCH ════════════════════════════════════════════════
// index.js binds its own keydown on #message-input (Enter → sendMessage). A
// capture listener on `window` runs BEFORE any listener on the target element,
// so stopImmediatePropagation() here is what actually prevents the send —
// registering another listener on the input itself would run second (listeners
// on the event target fire in registration order regardless of phase).
function insertLineBreak(el) {
    let ok = false;
    try { ok = document.execCommand("insertLineBreak"); } catch (_) {}
    if (!ok) { try { ok = document.execCommand("insertHTML", false, "<br>"); } catch (_) {} }
    if (!ok) {
        // Last-resort manual insert; execCommand fires `input` itself, this path
        // must dispatch it so the draft-autosave listener still runs.
        const sel = window.getSelection();
        if (sel && sel.rangeCount) {
            const r = sel.getRangeAt(0);
            r.deleteContents();
            const br = document.createElement("br");
            r.insertNode(br);
            r.setStartAfter(br);
            r.collapse(true);
            sel.removeAllRanges();
            sel.addRange(r);
        } else {
            el.appendChild(document.createElement("br"));
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
}

function onGlobalKeydownCapture(e) {
    // Escape priority: tray → selection → (fall through to index.js, which
    // clears reply/attachments). Without stopImmediatePropagation, one Escape
    // would close the tray AND wipe a staged reply.
    if (e.key === "Escape") {
        if (trayIsOpen()) { e.preventDefault(); e.stopImmediatePropagation(); closeTray(); return; }
        if (Sel.on)       { e.preventDefault(); e.stopImmediatePropagation(); exitSelect(); return; }
        return;
    }
    if (Sel.on && (e.key === "Delete" || e.key === "Backspace")) {
        const t = e.target;
        const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
        if (!typing) { e.preventDefault(); e.stopImmediatePropagation(); onBarDelete(); return; }
    }
    if (e.key !== "Enter") return;

    const input = $id("message-input");
    if (!input || e.target !== input) return;      // inline message edit is untouched
    if (!isTouch()) return;                        // desktop: Enter still sends
    if (e.isComposing || e.keyCode === 229) return; // IME candidate commit

    if (e.ctrlKey || e.metaKey) {                  // phone + hardware keyboard escape hatch
        e.preventDefault(); e.stopImmediatePropagation();
        try { sendMessage(); } catch (_) {}
        return;
    }
    if (e.shiftKey) return;                        // already a newline, let it be

    e.preventDefault();
    e.stopImmediatePropagation();                  // ← index.js never sees this Enter
    insertLineBreak(input);
}

// ═══ WIRING ═════════════════════════════════════════════════════════════════
function init() {
    const host = msgsEl();
    if (!host) return;

    setTouchMode(coarse.matches);
    observeMessages();

    host.addEventListener("pointerdown", onMsgPointerDown, { passive: true });
    host.addEventListener("pointermove", onMsgPointerMove, { passive: true });
    host.addEventListener("pointerup", onMsgPointerUp, { passive: true });
    host.addEventListener("pointercancel", () => cancelPress(), { passive: true });
    host.addEventListener("contextmenu", onContextMenu);
    host.addEventListener("click", onMessagesClickCapture, true);
    host.addEventListener("touchend", blockWhileSelecting, true);
    host.addEventListener("dblclick", blockWhileSelecting, true);
    host.addEventListener("scroll", () => { cancelPress(); clearPeek(); }, { passive: true });

    window.addEventListener("keydown", onGlobalKeydownCapture, true);

    // Trimmed-away messages (DOM_CAP) must not leave phantom selections.
    setInterval(() => {
        if (!Sel.on) return;
        let dirty = false;
        Sel.ids.forEach(id => { if (!byId(id)) { Sel.ids.delete(id); dirty = true; } });
        if (dirty) { if (Sel.ids.size === 0) exitSelect(); else updateBar(); }
    }, 4000);

    window.__cbMsgActions.api = {
        openTray, closeTray, enterSelect, exitSelect, copyPayload, refine,
        get selection() { return Array.from(Sel.ids); },
    };
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();

})();
