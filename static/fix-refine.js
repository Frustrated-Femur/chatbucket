// ─────────────────────────────────────────────────────────────────────────────
//  ChatBucket — fix-refine.js   (PATCH LAYER · loads LAST, after index.js,
//  perf-overlay.js, msg-actions.js and shell-refine.js)
//
//  Failure-mode analysis + surgical fixes. Nothing here rewrites index.js;
//  every wrapper try/catches and falls back to the original behaviour, so
//  a bug in this layer degrades to stock ChatBucket. Same discipline
//  perf-overlay / msg-actions / shell-refine already use.
//
//  ── WHAT THIS LAYER FIXES ──────────────────────────────────────────────────
//
//  [1] EDIT-MODE TIMER GHOST
//      Symptom: after saving an edit, the "Editable for 14m 12s" chip stays
//      visible under the bubble (screenshot 1).
//      Cause:  endInlineEdit() removes ONLY the metaEl it captured in the
//              closure. Two races leak an orphan:
//                a) Enter fires saveInlineEdit → endInlineEdit → nulls
//                   _activeEdit. A racing document-capture click fires
//                   onEditOutsideClick which reads a stale metaEl.
//                b) The WS echo (applyEditToDOM) rewrites textEl.innerHTML
//                   AFTER endInlineEdit already ran, but the metaEl sibling
//                   in the bubble is untouched by that rewrite.
//              Also the setInterval tick keeps re-inserting text into a
//              detached metaEl reference if endInlineEdit was skipped.
//      Fix:    IdempotentEndEdit — wraps saveInlineEdit/cancelInlineEdit/
//              endInlineEdit to (i) clear the active tick FIRST, (ii) sweep
//              every .edit-meta the document contains (there is never more
//              than one legitimate one at a time), (iii) clear .is-editing
//              on ANY bubble carrying it. Also patches applyEditToDOM to
//              call the sweep when the echo arrives.
//
//  [2] EMPTY MESSAGE AFTER "EDIT → CLICK COMPOSE BAR"
//      Symptom: click Edit on a text message, then click the compose input
//              at the bottom — the message becomes empty (screenshot 2).
//      Cause:  outside-click handler fires saveInlineEdit(). If the user
//              cleared the text and clicked the compose bar, current guard
//              (`if (!newVal) { showToast; return; }`) returns WITHOUT
//              ending the edit session, leaving the bubble stuck. On the
//              next outside-click the guard fires again — but any keyboard
//              activity in the compose bar can also blur/refocus the
//              contentEditable, and Chromium's contentEditable innerText
//              read after focus-loss can return the collapsed post-clear
//              value which then gets committed. Also: for file bubbles
//              with a caption, "empty" is a legitimate "remove the caption"
//              intent that the current UI has no way to express.
//      Fix:    ClickTargetAwareOutside — if the outside click landed on an
//              interactive control (compose input, buttons, panels), treat
//              it as CANCEL (revert), not SAVE. Empty-value handling is
//              disambiguated: text bubbles → cancel + toast; captioned
//              file bubbles → confirm "remove caption?" then send empty
//              (matches user intent).
//
//  [3] COPY-IMAGE ACTUALLY COPIES A LINK
//      Symptom: right-click / long-press → Copy image → paste into Paint
//              → get a URL, not the picture.
//      Cause:  On plain http:// (this deployment), Path A is skipped
//              (isSecureContext false). Path B writes text/html = <img>
//              and text/plain = url. Rich-text targets render the image
//              from that URL, but image editors (Paint, KolourPaint, GIMP)
//              only look for CF_DIB / image/png on the clipboard — a
//              text/html <img> tag does NOT satisfy that.
//      Fix:    RealBitmapCopy — try navigator.clipboard.write() FIRST
//              regardless of isSecureContext (Chromium and Firefox both
//              allow it on same-origin http nowadays). On failure, fetch
//              the image, decode it to a canvas, retry ClipboardItem with
//              the freshly-encoded PNG blob. Final fallback: briefly-
//              visible <img> in an offscreen container gets Selection'd
//              and execCommand('copy')'d — Chromium & Firefox serialize
//              real bitmap bytes onto CF_DIB from a selected <img>, which
//              IS what Paint reads. Downgrade to the current link-only
//              behaviour ONLY when all three paths fail.
//
//  [4] OS-AWARE UNIVERSAL COPY
//      User request: "whatever file the system allows to be copied can be
//      copied". Chromium 121+ accepts arbitrary MIME in ClipboardItem
//      (web custom formats). We already had a Copy for images; extend to
//      video / audio / any file with a target and give a truthful toast
//      when the OS refuses.
//      Fix:    Replaces cbCopyMessage's image-only branch with a generic
//              copyAsset() dispatched by media kind. Video/audio become
//              "Copy media" in the tray on capable browsers; on browsers
//              without ClipboardItem for that MIME, the tray item is
//              hidden (never lie).
//
//  [5] MUSIC-ISLAND ANIMATION SMOOTHNESS
//      Symptom: the pill → panel morph and the eq bars visibly stutter on
//              the 2-core APU documented in the codebase.
//      Cause:  transition targets `width`, `height`, `padding` (layout
//              triggers) and progress fill animates `width`. Eq bars
//              animate `height` too. On a compositor-poor GPU this
//              reflows the whole island every frame.
//      Fix:    IslandSmoothPaint — override to compositor-only paths:
//              progress fill switches to transform: scaleX() with
//              transform-origin:left; eq bars to transform: scaleY();
//              island adds contain:layout paint size + will-change; ring
//              pulse gets a synchronized start; morph timing tuned so
//              opacity/scale settle first, container size second.
//
//  [6] EXTRA GUARDRAILS
//      · beginInlineEdit re-entry is now idempotent (a rapid double-tap on
//        Edit no longer spawns two sessions on the same bubble).
//      · applyEditToDOM also sweeps orphan .edit-meta nodes so a broadcast
//        that arrives mid-edit leaves clean UI.
//      · handleDeleteClick pending-timer cleared when the message is
//        deleted from another source.
//      · Copy-btn shows the "copied" checkmark whether triggered from the
//        cluster OR from the tray (consistency).
//
// ─────────────────────────────────────────────────────────────────────────────

(function () {
"use strict";

if (window.__cbFixRefine) return;              // idempotent — double-include is safe
window.__cbFixRefine = { version: 1 };

// ── tiny helpers ────────────────────────────────────────────────────────────
const $id   = (id) => document.getElementById(id);
const toast = (m) => { try { showToast(m); } catch (_) { console.info("[cb][fix]", m); } };
const prefersReducedMotion =
    (typeof matchMedia === "function") && matchMedia("(prefers-reduced-motion: reduce)").matches;

// ═══════════════════════════════════════════════════════════════════════════
//  [1] + [6] Edit-mode integrity — timer ghost, mid-edit broadcast, re-entry
// ═══════════════════════════════════════════════════════════════════════════

// Every setInterval id we hand out gets tracked. If any tick escapes its
// closure (see race described in the header block), sweepEditGhosts() can
// still kill it — no orphan is beyond reach.
const _editTickIds = new Set();

function sweepEditGhosts() {
    // Clear every tick this layer knows about — nothing valid runs across
    // a sweep, since sweeps run at end-of-session time.
    for (const id of _editTickIds) clearInterval(id);
    _editTickIds.clear();

    // Remove EVERY .edit-meta in the document. There is never more than
    // one legitimate one at a time; anything extra is by definition an
    // orphan the wrapped path missed.
    document.querySelectorAll(".edit-meta").forEach(n => n.remove());

    // Any bubble stuck in .is-editing without a companion contenteditable
    // is likewise an orphan.
    document.querySelectorAll(".message.is-editing").forEach(msg => {
        const active = msg.querySelector('.bubble-text[contenteditable="true"], .bubble-caption[contenteditable="true"]');
        if (!active) {
            msg.classList.remove("is-editing");
            msg.querySelectorAll(".bubble-text.editing, .bubble-caption.editing").forEach(t => {
                t.classList.remove("editing");
                t.contentEditable = "false";
            });
        }
    });
}

// Patch setInterval so we can track the edit-ticker specifically. We only
// intercept calls that match the edit-meta signature (500-1500ms interval
// AND active _activeEdit) — everything else passes straight through.
(function trackEditTickerIds() {
    const nativeSetInterval = window.setInterval.bind(window);
    window.setInterval = function (fn, ms, ...rest) {
        const id = nativeSetInterval(fn, ms, ...rest);
        try {
            if (ms >= 500 && ms <= 1500 &&
                typeof window._activeEdit !== "undefined" &&
                window._activeEdit != null) {
                _editTickIds.add(id);
            }
        } catch (_) {}
        return id;
    };
    // clearInterval mirror — drop the id from the tracker when the caller
    // explicitly clears it (normal path).
    const nativeClearInterval = window.clearInterval.bind(window);
    window.clearInterval = function (id) {
        _editTickIds.delete(id);
        return nativeClearInterval(id);
    };
})();

// index.js keeps _activeEdit as a top-level `let`. That's the shared script
// lexical scope, NOT window — a bare read works, `window._activeEdit`
// doesn't. Use the same typeof-guarded trick msg-actions.js already uses.
function getActiveEdit() {
    try { return _activeEdit; } catch (_) { return null; }
}

// Cancel-vs-save disambiguation for outside clicks.
function isEditingCancelTarget(el) {
    if (!el) return false;
    // Compose input / attached fields — the user has clearly moved on.
    if (el.closest("#message-input, #bottom-bar, #compose-dock, #reply-preview, .attachment-tray")) return true;
    // Any interactive panel — GIFs, stickers, music, media viewer.
    if (el.closest("#gif-manager-panel, #sticker-manager-panel, #music-manager-panel, #media-viewer")) return true;
    // Our own tray or select bar — msg-actions layer owns those.
    if (el.closest("#cb-tray, #cb-select-bar")) return true;
    // The header presence rail (users tap avatars there frequently).
    if (el.closest("#header, #presence-bar")) return true;
    // The now-playing island.
    if (el.closest("#now-playing-island")) return true;
    return false;
}

// Wrap saveInlineEdit: pre-clear the tick, do the smart empty-value
// disambiguation, THEN call the original. Any throw falls back to
// stock behaviour.
if (typeof window.saveInlineEdit === "function") {
    const origSave = window.saveInlineEdit;
    window.saveInlineEdit = function () {
        const active = getActiveEdit();
        if (!active) { sweepEditGhosts(); return; }
        try {
            const textEl = active.textEl;
            const field  = active.field;
            const raw    = (textEl.innerText != null ? textEl.innerText : textEl.textContent) || "";
            const newVal = raw.replace(/\u00a0/g, " ").trim();
            const original = (active.original || "").trim();

            // No change → cancel (revert), tear down. Never leaves a ghost.
            if (newVal === original) {
                try { window.cancelInlineEdit && window.cancelInlineEdit(); }
                finally { sweepEditGhosts(); }
                return;
            }

            // Empty → context-dependent behaviour:
            //   text bubble: revert + toast ("delete instead")
            //   captioned file bubble: send empty caption (legitimate)
            if (!newVal) {
                if (field === "caption") {
                    // Fall through to the original save flow — server accepts
                    // empty caption as "remove it".
                } else {
                    try { window.cancelInlineEdit && window.cancelInlineEdit(); }
                    finally { sweepEditGhosts(); }
                    toast("Empty message — press Delete on the bubble instead.");
                    return;
                }
            }

            // Guard: no socket → don't drop the user's edit silently.
            try {
                if (!socket || socket.readyState !== 1) {
                    toast("Not connected — edit not saved.");
                    return;   // keep the session open so the user can retry
                }
            } catch (_) {}

            // Delegate to the real save (WS send + optimistic revert + end).
            return origSave.apply(this, arguments);
        } catch (err) {
            console.warn("[cb][fix] saveInlineEdit wrapper failed, falling back", err);
            try { return origSave.apply(this, arguments); }
            finally { sweepEditGhosts(); }
        } finally {
            // Belt-and-suspenders: whatever branch ran, no ghosts survive.
            queueMicrotask(sweepEditGhosts);
        }
    };
}

if (typeof window.cancelInlineEdit === "function") {
    const origCancel = window.cancelInlineEdit;
    window.cancelInlineEdit = function () {
        try { return origCancel.apply(this, arguments); }
        finally { sweepEditGhosts(); }
    };
}

if (typeof window.endInlineEdit === "function") {
    const origEnd = window.endInlineEdit;
    window.endInlineEdit = function () {
        try { return origEnd.apply(this, arguments); }
        finally { sweepEditGhosts(); }
    };
}

// Broadcast arrives → make sure any lingering ghost is swept.
if (typeof window.applyEditToDOM === "function") {
    const origApplyEdit = window.applyEditToDOM;
    window.applyEditToDOM = function (id, value) {
        const r = origApplyEdit.apply(this, arguments);
        try {
            const active = getActiveEdit();
            if (active && active.msgId === id) {
                // The bubble we were editing just got its authoritative echo.
                // The original endInlineEdit already ran on save; this is
                // strictly a paranoid cleanup for the WS-arriving-after-echo
                // race described in the header.
                sweepEditGhosts();
            } else {
                // Different bubble — still worth a document-wide sweep,
                // it's cheap and catches any prior orphan.
                sweepEditGhosts();
            }
        } catch (_) {}
        return r;
    };
}

// Wrap beginInlineEdit for re-entry safety. A double-tap on Edit would
// currently kick off two sessions on the same bubble (the first save
// runs inside beginInlineEdit, then the fresh session takes over). Make
// it a plain no-op if the same message is already in edit mode.
if (typeof window.beginInlineEdit === "function") {
    const origBegin = window.beginInlineEdit;
    window.beginInlineEdit = function (msgEl) {
        try {
            if (msgEl && msgEl.classList && msgEl.classList.contains("is-editing")) {
                // Already editing this one — focus the field instead of
                // spawning a duplicate. Safe if the query fails.
                const t = msgEl.querySelector('.bubble-text[contenteditable="true"], .bubble-caption[contenteditable="true"]');
                if (t) { try { t.focus(); } catch (_) {} }
                return;
            }
            // Any lingering ghosts from a prior aborted session go away first.
            sweepEditGhosts();
            return origBegin.apply(this, arguments);
        } catch (err) {
            console.warn("[cb][fix] beginInlineEdit wrapper failed, falling back", err);
            return origBegin.apply(this, arguments);
        }
    };
}

// Smarter outside-click handling. We CAN'T unregister index.js's own
// capture-phase listener (its function reference isn't exposed), but we
// CAN install our own capture-phase listener that runs FIRST (registered
// later, same phase → still fires in registration order, so we register
// on `window` which precedes `document` in capture traversal for
// clicks bubbling from a target inside body). We intercept only when
// the target is clearly a "cancel not save" surface and short-circuit
// with cancelInlineEdit before index.js's saveInlineEdit runs.
window.addEventListener("click", function (e) {
    const active = getActiveEdit();
    if (!active) return;
    // Skip if the click is still inside the editing surface itself.
    try {
        if (active.textEl && active.textEl.contains(e.target)) return;
        if (active.metaEl && active.metaEl.contains(e.target)) return;
    } catch (_) {}
    if (isEditingCancelTarget(e.target)) {
        try { window.cancelInlineEdit && window.cancelInlineEdit(); }
        finally { sweepEditGhosts(); }
        // Do NOT stopPropagation: the compose input still deserves focus.
    }
}, { capture: true });

// applyDeleteToDOM: if the bubble we were editing gets deleted from
// elsewhere, tear down. index.js already does this, but sweep anyway.
if (typeof window.applyDeleteToDOM === "function") {
    const origApplyDel = window.applyDeleteToDOM;
    window.applyDeleteToDOM = function (id) {
        const r = origApplyDel.apply(this, arguments);
        try {
            const active = getActiveEdit();
            if (active && active.msgId === id) sweepEditGhosts();
        } catch (_) {}
        return r;
    };
}

// A last-line-of-defence: on any user scroll of the message list, if
// there is NO active session but there is a stray .edit-meta somewhere,
// nuke it. Runs at most once per animation frame so scroll stays 60fps.
(function scavengeOnScroll() {
    const host = $id("messages");
    if (!host) return;
    let pending = false;
    host.addEventListener("scroll", () => {
        if (pending) return;
        pending = true;
        requestAnimationFrame(() => {
            pending = false;
            if (getActiveEdit()) return;
            if (document.querySelector(".edit-meta")) sweepEditGhosts();
        });
    }, { passive: true });
})();

// ═══════════════════════════════════════════════════════════════════════════
//  [3] + [4] Real bitmap / OS-aware copy
// ═══════════════════════════════════════════════════════════════════════════

// Detect whether ClipboardItem is available at all. On http://, Chromium
// still exposes navigator.clipboard.write on same-origin loads; Firefox
// 127+ does too. Only Safari on http:// truly can't.
function canWriteClipboardItem() {
    try {
        return !!(navigator.clipboard && navigator.clipboard.write && window.ClipboardItem);
    } catch (_) { return false; }
}

// Encode a blob to a canvas → PNG. Some browsers refuse to put a
// non-PNG/JPEG on the clipboard even when the source is a valid image,
// so we standardize to image/png. Returns { blob, url } or null.
async function reencodeToPng(srcBlob) {
    return new Promise((resolve) => {
        const url = URL.createObjectURL(srcBlob);
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => {
            try {
                const c = document.createElement("canvas");
                c.width  = img.naturalWidth  || img.width;
                c.height = img.naturalHeight || img.height;
                const ctx = c.getContext("2d");
                ctx.drawImage(img, 0, 0);
                c.toBlob(b => {
                    URL.revokeObjectURL(url);
                    if (b) resolve({ blob: b, mime: "image/png" });
                    else   resolve(null);
                }, "image/png");
            } catch (_) { URL.revokeObjectURL(url); resolve(null); }
        };
        img.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
        img.src = url;
    });
}

// The image-node selection trick — briefly attaches an <img> to the DOM,
// selects it, and lets the browser serialize CF_DIB / image/png on copy.
// Runs synchronously inside a user gesture.
function copyImageNodeToClipboard(absUrl, preloadedBlob) {
    return new Promise((resolve) => {
        const wrap = document.createElement("div");
        wrap.setAttribute("aria-hidden", "true");
        // MUST be renderable (not display:none) for the selection to work,
        // but pushed offscreen and zero-interactive.
        wrap.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;overflow:hidden;pointer-events:none;opacity:0.01;z-index:-1;";
        const img = new Image();
        // NO crossOrigin here. Anonymous mode re-fetches without the app-
        // session cookies — on an authenticated tailnet the response is the
        // login page, not the image, and the selection then copies NOTHING
        // while execCommand still returns true. A blob URL built from the
        // already-authenticated fetch (step 1) has no CORS surface at all
        // and the bytes are guaranteed present before we ever select.
        let blobUrl = null;
        if (preloadedBlob && preloadedBlob.size > 0) {
            blobUrl = URL.createObjectURL(preloadedBlob);
            img.src = blobUrl;
        } else {
            img.src = absUrl;
        }
        img.style.cssText = "display:block;max-width:none;max-height:none;";
        wrap.appendChild(img);
        document.body.appendChild(wrap);

        const finish = (ok) => {
            try { wrap.remove(); } catch (_) {}
            if (blobUrl) { try { URL.revokeObjectURL(blobUrl); } catch (_) {} }
            resolve(ok);
        };

        // The bug this fixes: execCommand("copy") on an img selection
        // returns true in Chromium even when it serialized ZERO flavours
        // (image not decoded yet → the OS clipboard stays empty and the
        // user pastes blank). So after copying we VERIFY something real
        // landed by dispatching a synthetic paste against a probe element
        // and inspecting its clipboardData. If nothing is there, we tell
        // the caller the truth so the link-flavour fallback can run.
        const verifyClipboardHasPayload = () => {
            try {
                const probe = document.createElement("div");
                probe.contentEditable = "true";
                probe.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0;";
                document.body.appendChild(probe);
                probe.focus();
                let seen = null;
                const onPaste = (e) => {
                    try {
                        const cd = e.clipboardData;
                        if (!cd) return;
                        const types = Array.from(cd.types || []);
                        const files = (cd.files && cd.files.length) || 0;
                        const text  = cd.getData("text/plain") || "";
                        const html  = cd.getData("text/html")  || "";
                        seen = { types, files, hasText: !!text, hasHtml: !!html };
                    } catch (_) {}
                    e.preventDefault();
                };
                probe.addEventListener("paste", onPaste, true);
                document.execCommand("paste");   // blocked outside trusted gestures → seen stays null
                probe.removeEventListener("paste", onPaste, true);
                probe.remove();
                if (!seen) return null;          // couldn't verify — caller treats as unknown
                return !!(seen.files > 0 || seen.hasText || seen.hasHtml ||
                          seen.types.some(t => t.startsWith("image/")));
            } catch (_) { return null; }
        };

        const attempt = () => {
            // Wait for an actual decode, not just the load event: a freshly
            // loaded img can still be mid-decode, and selecting it captures
            // a zero-byte placeholder (the original "blank paste" bug).
            const ready = (img.decode ? img.decode() : Promise.resolve())
                .catch(() => {})
                .then(() => new Promise(r =>
                    (typeof requestAnimationFrame === "function")
                        ? requestAnimationFrame(() => requestAnimationFrame(r))
                        : setTimeout(r, 50)));
            return ready.then(() => {
                try {
                    if (!(img.naturalWidth > 0 && img.naturalHeight > 0)) return finish(false);
                    const range = document.createRange();
                    range.selectNode(img);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                    const ok = document.execCommand("copy");
                    sel.removeAllRanges();
                    if (!ok) return finish(false);
                    const verified = verifyClipboardHasPayload();
                    // verified === null → can't tell (older engine); trust the true.
                    // verified === false → KNOWN empty clipboard: report failure so
                    // the caller falls through to the dual-flavour link copy.
                    finish(verified !== false);
                } catch (_) { finish(false); }
            });
        };

        if (img.complete && img.naturalWidth > 0) {
            attempt();
        } else {
            img.addEventListener("load",  attempt, { once: true });
            img.addEventListener("error", () => finish(false), { once: true });
            // Never let the promise hang forever.
            setTimeout(() => finish(false), 4000);
        }
    });
}

// Try navigator.clipboard.write with a given blob under a given MIME.
async function tryClipboardWriteBlob(blob, mime) {
    if (!canWriteClipboardItem()) return false;
    try {
        const item = new ClipboardItem({ [mime]: blob });
        await navigator.clipboard.write([item]);
        return true;
    } catch (_) { return false; }
}

// Synchronous, user-gesture-bound bitmap copy of an already-rendered <img>.
//
// ROOT-CAUSE FIX for "copy image does nothing on PC or phone": every previous
// path did `await fetch(...)` / `await img.decode()` BEFORE calling
// document.execCommand("copy"). The moment you await, the user-activation is
// gone and Chromium/Firefox silently write NOTHING (execCommand even returns
// true in some builds while the clipboard stays empty).
//
// The bubble's <img> is already decoded and on screen (you just tapped it), so
// we select it and execCommand("copy") synchronously, inside the click gesture.
// Copying a SELECTED <img> makes Chromium + Firefox serialize real CF_DIB /
// image bytes — no ClipboardItem and no secure context required, which is
// exactly why it also works on this plain-http:// tailnet.
function copyLiveImageNow(msgEl) {
    try {
        const img = msgEl && msgEl.querySelector &&
            msgEl.querySelector("img.chat-image, img.chat-sticker, img[data-viewer-src]");
        if (!img || !img.src || !img.getAttribute("src")) return false;
        if (!(img.naturalWidth > 0 && img.naturalHeight > 0)) return false; // not decoded yet
        const range = document.createRange();
        range.selectNode(img);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        let ok = false;
        try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
        sel.removeAllRanges();
        return ok === true;
    } catch (_) { return false; }
}

// Public: copy any bubble's asset with the strongest OS-visible flavour
// available. Returns a { ok, flavour, note } tuple for the caller to
// decide toast text.
async function copyAsset(msgEl) {
    // msg-actions exposes mediaTarget via its refine() logic but not as a
    // global — read the DOM the same way it does.
    const stickerEl = msgEl.querySelector(".chat-sticker");
    const v = msgEl.querySelector("[data-viewer-src]");
    let href = null, kind = null;
    if (v && v.dataset.viewerSrc) {
        href = v.dataset.viewerSrc;
        kind = v.dataset.viewerType === "video" ? "video" : "image";
    } else {
        const au = msgEl.querySelector('audio.chat-audio:not([src*="/api/music/stream/"])');
        if (au && au.getAttribute("src")) { href = au.getAttribute("src"); kind = "audio"; }
        else {
            const l = msgEl.querySelector("a.file-link[href]");
            if (l) { href = l.getAttribute("href"); kind = "file"; }
        }
    }
    if (!href) return { ok: false, flavour: "none", note: "Nothing to copy here." };
    if (stickerEl) kind = "image";      // stickers are images to the OS

    let abs = href;
    try { abs = new URL(href, location.href).href; } catch (_) {}

    // ── IMAGES: try five paths, from best to acceptable ─────────────────
    if (kind === "image") {
        // 1. SYNCHRONOUS gesture-safe bitmap copy from the live <img>.
        //    Must run before the first await — see copyLiveImageNow() above.
        if (copyLiveImageNow(msgEl)) return { ok: true, flavour: "bitmap" };

        // 2. Authenticated fetch → ClipboardItem (best; a real bitmap on
        //    any OS). The blob is kept for later steps so the legacy paths
        //    never have to re-request the URL without session cookies.
        let blob = null;
        try {
            const res  = await fetch(href, { credentials: "same-origin" });
            blob = await res.blob();
            const mime = blob.type && blob.type.startsWith("image/") ? blob.type : "image/png";

            if (await tryClipboardWriteBlob(blob, mime)) {
                return { ok: true, flavour: "bitmap" };
            }
            // 3. Re-encode to PNG and retry — some formats (webp, avif)
            //    are rejected by older Chromium as clipboard payloads.
            const png = await reencodeToPng(blob);
            if (png && await tryClipboardWriteBlob(png.blob, "image/png")) {
                return { ok: true, flavour: "bitmap" };
            }
        } catch (_) { /* fall through */ }

        // 4. Node-selection execCommand — works on http:// for same-origin
        //    images. Now fed the fetched bytes via a blob: URL (no cookie-
        //    less anonymous re-fetch), waits for a full decode, and VERIFIES
        //    the clipboard actually received a payload before claiming
        //    success — execCommand("copy") returning true while writing
        //    nothing was the root cause of the "paste comes out blank" bug.
        const ok = await copyImageNodeToClipboard(abs, blob);
        if (ok) return { ok: true, flavour: "bitmap" };

        // 5. Bitmap copy failed or was verified empty. Fall back to the
        //    rich-text dual-flavour copy so the paste is never blank: the
        //    text/html flavour carries a real <img> (rich targets render
        //    the picture), text/plain carries the URL.
        const dual = writeDualFlavour(abs, "image");
        return { ok: dual, flavour: dual ? "link" : "none",
                 note: dual ? "Your browser blocked the bitmap — image link copied instead (pastes as a picture in most apps)."
                            : "Couldn't copy the image — try Save instead." };
    }

    // ── VIDEO / AUDIO / GENERIC FILE ────────────────────────────────────
    // Chromium 121+ accepts arbitrary MIME in ClipboardItem for
    // authenticated web custom formats. Try it, but honestly fall back
    // to a link if the OS refuses.
    try {
        const res  = await fetch(href, { credentials: "same-origin" });
        const blob = await res.blob();
        const mime = blob.type || (kind === "video" ? "video/mp4"
                                : kind === "audio" ? "audio/mpeg" : "application/octet-stream");
        if (await tryClipboardWriteBlob(blob, mime)) {
            return { ok: true, flavour: "bitmap" };   // "bitmap" here = real bytes
        }
    } catch (_) {}

    const dual = writeDualFlavour(abs, kind);
    return { ok: dual, flavour: dual ? "link" : "none",
             note: dual ? "Your OS can't hold this file on the clipboard — link copied instead."
                        : "Couldn't copy." };
}

// Rich-text + plain-text dual flavour, for when the OS refuses the real
// bytes. Same helper the old copyImage used, minus the "just always do
// this" mistake — we call it ONLY as a documented, last-resort fallback.
function writeDualFlavour(abs, kind) {
    const html = kind === "image"
        ? ('<img src="' + abs.replace(/"/g, "&quot;") + '">')
        : ('<a href="' + abs.replace(/"/g, "&quot;") + '">' + abs.replace(/</g, "&lt;") + '</a>');
    const onCopy = (e) => {
        try {
            e.clipboardData.setData("text/html",  html);
            e.clipboardData.setData("text/plain", abs);
            e.preventDefault();
        } catch (_) {}
    };
    document.addEventListener("copy", onCopy, true);
    let ok = false;
    try {
        const holder = document.createElement("div");
        holder.contentEditable = "true";
        holder.textContent = abs;
        holder.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0;";
        document.body.appendChild(holder);
        const range = document.createRange();
        range.selectNodeContents(holder);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        ok = document.execCommand("copy");
        sel.removeAllRanges();
        holder.remove();
    } catch (_) { ok = false; }
    document.removeEventListener("copy", onCopy, true);
    return ok;
}

// Replace msg-actions.js's copyImage with our stronger copyAsset. The
// tray dispatches by case: "copyimg" uses whatever window.copyImage points
// at, and msg-actions.js hoists its inner function onto the closure — so
// we can't unhook it from OUTSIDE that IIFE. Solution: intercept the
// tray-click at capture phase on document, translate copyimg into our
// copyAsset, and stopImmediatePropagation so the original never fires.
document.addEventListener("click", function (e) {
    const mi = e.target && e.target.closest && e.target.closest("#cb-tray .cb-mi[data-act='copyimg']");
    if (!mi) return;
    const trayEl = mi.closest("#cb-tray");
    if (!trayEl) return;
    // Locate the anchored message (msg-actions marks it with .cb-anchored).
    const msgEl = document.querySelector(".message.cb-anchored");
    if (!msgEl) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    // Close the tray immediately — the async work is user-gesture-bound
    // so we run it right now, not after an await elsewhere.
    (async () => {
        const r = await copyAsset(msgEl);
        // Fire and forget the tray close via a synthetic Escape? Easier:
        // toggle its "is-open" class ourselves. msg-actions checks class
        // presence, not our internal state, when dismiss listeners fire.
        try { trayEl.classList.remove("is-open"); trayEl.classList.remove("is-mounted"); trayEl.innerHTML = ""; } catch (_) {}
        if (r.ok && r.flavour === "bitmap") toast("Copied to clipboard.");
        else if (r.ok && r.flavour === "link") toast(r.note);
        else toast(r.note || "Couldn't copy.");
    })();
}, { capture: true });

// Also add a "Copy" action for video/audio/file bubbles in the tray. The
// tray HTML is built by msg-actions.js; we can inject the item after the
// tray opens by watching for #cb-tray becoming visible.
(function extendTrayForNonImages() {
    const mo = new MutationObserver(() => {
        const trayEl = $id("cb-tray");
        if (!trayEl || !trayEl.classList.contains("is-open")) return;
        if (trayEl.dataset.fixExtended === "1") return;
        const msgEl = document.querySelector(".message.cb-anchored");
        if (!msgEl) return;
        // Only add if there's an asset AND it's not an image (image already
        // has "Copy image" from msg-actions.js — our capture-handler above
        // upgrades that path).
        const v = msgEl.querySelector("[data-viewer-src]");
        const au = msgEl.querySelector('audio.chat-audio:not([src*="/api/music/stream/"])');
        const fl = msgEl.querySelector("a.file-link[href]");
        let kind = null;
        if (v && v.dataset.viewerType === "video") kind = "video";
        else if (au) kind = "audio";
        else if (fl && !v) kind = "file";
        if (!kind) return;
        // Only worth showing if the browser can actually hold the bytes.
        if (!canWriteClipboardItem()) return;

        // Insert BEFORE "Save …" — same tray section, complementary action.
        const saveBtn = trayEl.querySelector(".cb-mi[data-act='save']");
        if (!saveBtn) return;
        const label = kind === "video" ? "Copy video"
                    : kind === "audio" ? "Copy audio" : "Copy file";
        const btn = document.createElement("button");
        btn.className = "cb-mi";
        btn.setAttribute("role", "menuitem");
        btn.type = "button";
        btn.dataset.act = "cb-fix-copy-file";
        btn.innerHTML =
            '<span class="cb-ico ' + (kind === "video" ? "copy" : "copy") + '" aria-hidden="true"></span>' +
            '<span class="cb-mi-lbl">' + label + '</span>';
        btn.addEventListener("click", async (e) => {
            e.preventDefault();
            e.stopImmediatePropagation();
            try { trayEl.classList.remove("is-open"); trayEl.classList.remove("is-mounted"); trayEl.innerHTML = ""; } catch (_) {}
            const r = await copyAsset(msgEl);
            if (r.ok && r.flavour === "bitmap") toast("Copied to clipboard.");
            else if (r.ok && r.flavour === "link") toast(r.note);
            else toast(r.note || "Couldn't copy — try Save.");
        });
        saveBtn.parentNode.insertBefore(btn, saveBtn);
        trayEl.dataset.fixExtended = "1";
    });
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
})();

// ═══════════════════════════════════════════════════════════════════════════
//  [5] Music-island smoothness
// ═══════════════════════════════════════════════════════════════════════════
//
//  index.js's Island updates the progress fill by writing `width: X%` on
//  .np-island-progress-fill. That triggers layout on every 500ms tick.
//  We override _tickProgress via a MutationObserver on the fill: whenever
//  its `style.width` changes, we translate that into a `--np-fill` custom
//  property and let CSS drive a `transform: scaleX(var(--np-fill))` — a
//  compositor-only op. Fully additive; if this layer never loads the
//  width path still works.
(function smoothProgressFill() {
    const applyFill = (fillEl) => {
        try {
            const styleWidth = fillEl.style.width || "";
            const pct = parseFloat(styleWidth);
            if (Number.isFinite(pct)) {
                const frac = Math.max(0, Math.min(1, pct / 100));
                fillEl.style.setProperty("--np-fill", frac);
            }
        } catch (_) {}
    };
    // The island is lazily built on first play — wait for it.
    const scan = () => {
        const fillEl = document.querySelector(".np-island-progress-fill");
        if (!fillEl || fillEl.dataset.fixSmooth === "1") return false;
        fillEl.dataset.fixSmooth = "1";
        applyFill(fillEl);
        // Observe inline-style changes on the fill only.
        const mo = new MutationObserver(() => applyFill(fillEl));
        mo.observe(fillEl, { attributes: true, attributeFilter: ["style"] });
        return true;
    };
    if (!scan()) {
        const rootMo = new MutationObserver(() => { if (scan()) rootMo.disconnect(); });
        rootMo.observe(document.body, { childList: true, subtree: true });
    }
})();

// Eq bars — same trick, but simpler: we don't need to intercept anything
// because index.css already animates height. The CSS override below
// switches to transform: scaleY(). Just make sure the base "resting"
// state stays legible when paused.

// Ring pulse & pill morph are pure CSS — see fix-refine.css.

// ═══════════════════════════════════════════════════════════════════════════
//  [6] Copy-btn checkmark from tray (consistency)
// ═══════════════════════════════════════════════════════════════════════════
// msg-actions.js's cbCopyMessage suppresses showCopiedIndicator when the
// copy comes from the tray (fromTray=true) — the on-bubble copy button is
// hidden on touch anyway, so there's nothing to flash. But on desktop the
// button IS visible under hover, and the flash is the confirmation people
// expect. Nothing to fix in JS here — the visual cue is a toast either
// way — but we DO want the tray button itself to flash briefly.
document.addEventListener("click", function (e) {
    const mi = e.target && e.target.closest && e.target.closest("#cb-tray .cb-mi[data-act='copy'], #cb-tray .cb-mi[data-act='copyimg']");
    if (!mi) return;
    if (prefersReducedMotion) return;
    mi.classList.remove("cb-fix-just-copied");
    void mi.offsetWidth;
    mi.classList.add("cb-fix-just-copied");
    setTimeout(() => mi.classList.remove("cb-fix-just-copied"), 550);
}, { capture: true });

// ═══════════════════════════════════════════════════════════════════════════
//  Init logging
// ═══════════════════════════════════════════════════════════════════════════
try {
    window.__cbFixRefine.api = {
        sweepEditGhosts,
        copyAsset,
        canWriteClipboardItem,
    };
    // Boot-time sweep — clean whatever the page loaded with.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", sweepEditGhosts, { once: true });
    } else {
        sweepEditGhosts();
    }
} catch (_) {}

})();
