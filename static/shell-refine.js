// ─────────────────────────────────────────────────────────────────────────────
//  ChatBucket — shell-refine.js   (PATCH LAYER · loads LAST, after index.js,
//  perf-overlay.js and msg-actions.js)
//
//  v3 — corrected scope. This layer only ever *changes behaviour*; it does not
//  restyle persistent chrome. Rules:
//
//  [1] PRESENCE RAIL   #presence-bar becomes one horizontally-scrollable line.
//                      No count pill, no wrapper — just the names. The header
//                      can never grow a second row again.
//  [2] ADAPTIVE DOCK   The four compose buttons are MOVED into a floating dock
//                      ONLY when #bottom-bar cannot actually fit them — measured
//                      live with a ResizeObserver, on any device, at any zoom
//                      level or window size. The moment there's room again, the
//                      originals move straight back. A wide desktop never sees
//                      the dock.
//  [3] SHELL GLUE      Presence edge-fades, dock dismissal, drawer-state tick.
//
//  Everything is additive: no index.js function is rewritten, buttons keep
//  their ids + inline onclick, and each block is independently try/caught.
// ─────────────────────────────────────────────────────────────────────────────

(function () {
"use strict";

if (window.__cbShellRefine) return;          // idempotent
window.__cbShellRefine = { version: 3 };

const $id = (id) => document.getElementById(id);
const isTouch = () => document.body.classList.contains("cb-touch");

// The four buttons we relocate when the composer overflows, in dock order.
// `key` mirrors the Alt+<k> shortcut index.js already binds globally — the
// dock only labels them, it never re-implements them.
const DOCK_ITEMS = [
    { id: "attach-btn",         label: "Attach file", key: "ALT+A" },
    { id: "sticker-toggle-btn", label: "Stickers",    key: "ALT+S" },
    { id: "gif-toggle-btn",     label: "GIFs",        key: "ALT+G" },
    { id: "music-toggle-btn",   label: "Music",       key: "ALT+M" },
];

// ═══ 1 · PRESENCE RAIL ══════════════════════════════════════════════════════

function updateRailFades() {
    const bar = $id("presence-bar");
    if (!bar) return;
    const overflowing = bar.scrollWidth - bar.clientWidth > 2;
    const atStart = bar.scrollLeft <= 1;
    const atEnd   = bar.scrollLeft >= bar.scrollWidth - bar.clientWidth - 1;
    bar.classList.toggle("has-start", overflowing && !atStart);
    bar.classList.toggle("has-end",   overflowing && !atEnd);
}

function initPresence() {
    const bar = $id("presence-bar");
    if (!bar) return;

    bar.addEventListener("scroll", updateRailFades, { passive: true });
    // Vertical wheel over a horizontal rail scrolls it sideways — a desktop
    // user with a plain mouse has no other way to reach the overflow.
    bar.addEventListener("wheel", (e) => {
        if (e.deltaY === 0 || bar.scrollWidth <= bar.clientWidth) return;
        e.preventDefault();
        bar.scrollLeft += e.deltaY;
    }, { passive: false });

    // index.js re-renders the pill list on any presence change (and
    // perf-overlay wraps updateOnlineStatus) — a MutationObserver catches
    // every path without caring which layer produced it.
    if (typeof MutationObserver === "function") {
        new MutationObserver(updateRailFades)
            .observe(bar, { childList: true, subtree: true });
    }
    if (typeof ResizeObserver === "function") {
        try { new ResizeObserver(updateRailFades).observe(bar); } catch (_) {}
    }
    window.addEventListener("resize", updateRailFades, { passive: true });
    updateRailFades();
}

// ═══ 2 · ADAPTIVE COMPOSE DOCK ══════════════════════════════════════════════

let dock = null, dockBtn = null, dockOpen = false, dockHideTimer = 0;
let collapsed = false;          // are the four buttons currently in the dock?
let homeAnchor = null;          // comment node marking where they normally live

function dockBar()   { return $id("bottom-bar"); }
function dockItems() { return DOCK_ITEMS.map(it => ({ ...it, el: $id(it.id) })).filter(it => it.el); }

// Build the trigger + dock once. The trigger is display:none until the
// overflow check decides the buttons don't fit.
function buildDock() {
    const barEl = dockBar();
    if (!barEl || dockBtn) return;
    const found = dockItems();
    if (!found.length) return;

    dockBtn = document.createElement("button");
    dockBtn.id = "cb-dock-btn";
    dockBtn.type = "button";
    dockBtn.title = "More — attach, stickers, GIFs, music";
    dockBtn.setAttribute("aria-label", "More options");
    dockBtn.setAttribute("aria-expanded", "false");
    dockBtn.setAttribute("aria-haspopup", "menu");
    dockBtn.innerHTML = `<span class="cb-plus" aria-hidden="true"></span>`;
    dockBtn.style.display = "none";
    // Lives exactly where the first button would — invisible until needed.
    found[0].el.parentNode.insertBefore(dockBtn, found[0].el);

    // Reserve the buttons' home NOW, while they are still in the bar — a
    // comment node placed right before the first one. (Doing this after the
    // row-building loop below would strand the marker inside a dock row.)
    homeAnchor = document.createComment("cb-dock-home");
    found[0].el.parentNode.insertBefore(homeAnchor, found[0].el);

    dock = document.createElement("div");
    dock.id = "cb-dock";
    dock.setAttribute("role", "menu");
    dock.setAttribute("aria-label", "Compose options");
    dock.innerHTML = `<div class="cb-dock-head">COMPOSE</div>`;
    document.body.appendChild(dock);

    found.forEach((it, i) => {
        const row = document.createElement("div");
        row.className = "cb-dock-row";
        row.setAttribute("role", "menuitem");
        row.tabIndex = 0;
        row.style.setProperty("--i", String(i));
        row.dataset.for = it.id;
        row.appendChild(it.el);                       // MOVE the real button
        const lbl = document.createElement("span");
        lbl.className = "cb-dock-lbl";
        lbl.textContent = it.label;
        row.appendChild(lbl);
        const k = document.createElement("span");
        k.className = "cb-dock-key";
        k.textContent = it.key;
        row.appendChild(k);
        dock.appendChild(row);
    });

    // The row forwards to the real button (pointer-events:none in CSS), so
    // index.js's inline onclick runs untouched.
    dock.addEventListener("click", (e) => {
        const row = e.target.closest(".cb-dock-row");
        if (!row) return;
        e.preventDefault();
        const btn = $id(row.dataset.for);
        closeDock();
        if (btn) btn.click();
    });
    dock.addEventListener("keydown", onDockKeydown);
    dock.addEventListener("pointerdown", (e) => e.stopPropagation());

    dockBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (dockOpen) closeDock(); else openDock();
    });

    // Start expanded: the originals live in the bar (the row loop above moved
    // them into the dock during construction — move them back). evaluateFit()
    // then decides whether they stay.
    found.forEach(it => homeAnchor.parentNode.insertBefore(it.el, homeAnchor));
    collapsed = false;
}

// ── the fit check ────────────────────────────────────────────────────────────
// scrollWidth/clientWidth can't detect our failure mode: #message-input is a
// flex:1 child, so flexbox answers "too many buttons" by SHRINKING the input
// to nothing rather than overflowing. "The bar doesn't fit" therefore means
// "the input can't keep a usable width while all four buttons are present".
//
// Thresholds are computed from live geometry (gaps, padding, the buttons'
// real widths), not a device breakpoint — so zoom, window resize, rotation
// and foldables all just work. COLLAPSE and EXPAND use DIFFERENT thresholds
// on purpose (hysteresis): without it a bar whose width sits exactly on the
// boundary would flip-flop every time the input's own width changed.
function composerFits() {
    const barEl = dockBar();
    if (!barEl) return true;
    const input = $id("message-input");
    const send  = $id("send-btn");
    if (!input || !send) return true;

    // Minimum the input may keep before the composer counts as "crowded" —
    // about 8 characters, or a third of the bar, whichever is kinder.
    const minInput = Math.min(150, Math.max(88, Math.round(barEl.clientWidth * 0.32)));

    const neededNow = () => {
        // Sum the non-input children (buttons + gaps + padding), then see if
        // what's left still affords the input its minimum.
        const cs = getComputedStyle(barEl);
        const gap = parseFloat(cs.columnGap || cs.gap) || 8;
        const padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
        let btns = send.getBoundingClientRect().width;
        let count = 1;                          // send always present
        for (const it of DOCK_ITEMS) {
            const el = $id(it.id);
            if (el) { btns += el.getBoundingClientRect().width || 38; count++; }
        }
        if (!collapsed && dockBtn) {            // trigger occupies a slot too
            const trigW = dockBtn.getBoundingClientRect().width || 38;
            if (dockBtn.style.display !== "none") { btns += trigW; count++; }
        }
        return padX + btns + gap * count + minInput;
    };

    if (!collapsed) {
        return barEl.clientWidth >= neededNow();
    }
    // Currently collapsed: only expand back when there's CLEARLY room — the
    // input's post-expansion width must clear its minimum by a wide margin.
    return barEl.clientWidth >= neededNow() + 40;
}

function collapseBar() {
    if (collapsed || !dock) return;
    const found = dockItems();
    found.forEach(it => {
        const row = dock.querySelector(`.cb-dock-row[data-for="${it.id}"]`);
        // insertBefore(firstChild), not appendChild — the row already holds its
        // label + keycap, and the button must stay FIRST in the flex row.
        if (row && it.el.parentNode !== row) row.insertBefore(it.el, row.firstChild);
    });
    collapsed = true;
    dockBtn.style.display = "";
}

function expandBar() {
    if (!collapsed || !dock) return;
    const barEl = dockBar();
    if (!barEl || !homeAnchor || !homeAnchor.parentNode) return;
    // Restore in original order, immediately before the marker.
    DOCK_ITEMS.forEach(it => {
        const el = $id(it.id);
        if (el) homeAnchor.parentNode.insertBefore(el, homeAnchor);
    });
    collapsed = false;
    dockBtn.style.display = "none";
    closeDock();
}

let _fitRaf = 0;
function evaluateFit() {
    // Debounce through rAF so a drag-resize fires this once per frame, max.
    cancelAnimationFrame(_fitRaf);
    _fitRaf = requestAnimationFrame(() => {
        if (!dockBtn) return;
        if (collapsed) {
            if (composerFits()) expandBar();
        } else {
            if (!composerFits()) collapseBar();
        }
    });
}

function placeDock() {
    if (!dock || !dockBtn) return;
    const pad = 8;
    const r = dockBtn.getBoundingClientRect();
    const w = dock.offsetWidth, h = dock.offsetHeight;
    let left = r.left;
    let top = r.top - h - 10;                 // default: above the compose bar
    let originY = "bottom";

    if (top < pad) { top = r.bottom + 10; originY = "top"; }   // flip if no room
    if (left + w + pad > window.innerWidth) left = window.innerWidth - w - pad;
    if (left < pad) left = pad;

    dock.style.left = Math.round(left) + "px";
    dock.style.top = Math.round(top) + "px";
    dock.style.transformOrigin = originY + " left";
}

function openDock() {
    if (!dock || !collapsed) return;
    clearTimeout(dockHideTimer);
    dockOpen = true;
    dockBtn.setAttribute("aria-expanded", "true");
    dock.classList.add("is-mounted");
    dock.classList.remove("is-open");
    // Restart the staggered row entrance on every open.
    dock.querySelectorAll(".cb-dock-row").forEach(r => {
        r.style.animation = "none";
        void r.offsetWidth;
        r.style.animation = "";
    });
    placeDock();                              // measured while mounted at opacity 0
    requestAnimationFrame(() => dock.classList.add("is-open"));

    // Keyboard users land on the first row so arrow keys walk the menu.
    const first = dock.querySelector(".cb-dock-row");
    if (first && !isTouch()) {
        try { first.focus({ preventScroll: true }); } catch (_) { first.focus(); }
    }

    document.addEventListener("pointerdown", onDocDown, true);
    window.addEventListener("keydown", onDockEscape, true);
    // The visual viewport shifts when a phone keyboard opens; keep the dock
    // pinned to its trigger instead of floating over the keyboard.
    if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", onDockReflow);
        window.visualViewport.addEventListener("scroll", onDockReflow);
    }
}

function closeDock() {
    if (!dock || !dockOpen) return;
    dockOpen = false;
    dockBtn.setAttribute("aria-expanded", "false");
    dock.classList.remove("is-open");
    clearTimeout(dockHideTimer);
    dockHideTimer = setTimeout(() => {
        if (!dockOpen) dock.classList.remove("is-mounted");
    }, 200);

    document.removeEventListener("pointerdown", onDocDown, true);
    window.removeEventListener("keydown", onDockEscape, true);
    if (window.visualViewport) {
        window.visualViewport.removeEventListener("resize", onDockReflow);
        window.visualViewport.removeEventListener("scroll", onDockReflow);
    }
}

function onDocDown(e) {
    if (dock && dock.contains(e.target)) return;
    if (dockBtn && dockBtn.contains(e.target)) return;
    closeDock();
}
function onDockReflow() { if (dockOpen) placeDock(); }
function onDockEscape(e) {
    if (e.key !== "Escape" || !dockOpen) return;
    // Beat index.js's global Escape (which clears reply + attachments) — the
    // user meant "close this dock", nothing else.
    e.preventDefault();
    e.stopImmediatePropagation();
    closeDock();
    try { dockBtn.focus({ preventScroll: true }); } catch (_) { dockBtn.focus(); }
}
function onDockKeydown(e) {
    const rows = Array.from(dock.querySelectorAll(".cb-dock-row"));
    if (!rows.length) return;
    const cur = rows.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = e.key === "ArrowDown"
            ? rows[(cur + 1 + rows.length) % rows.length]
            : rows[(cur - 1 + rows.length) % rows.length];
        try { next.focus({ preventScroll: true }); } catch (_) { next.focus(); }
    } else if (e.key === "Enter" || e.key === " ") {
        if (cur < 0) return;
        e.preventDefault();
        rows[cur].click();
    }
}

// Mirror "a drawer is open" onto the trigger, so collapsing the four buttons
// doesn't hide whether GIFs / stickers / music are currently showing.
function watchDrawers() {
    const panels = ["gif-manager-panel", "sticker-manager-panel", "music-manager-panel"]
        .map($id).filter(Boolean);
    if (!panels.length || !dockBtn) return;
    const sync = () => {
        const anyOpen = panels.some(p => p.style.display && p.style.display !== "none");
        dockBtn.classList.toggle("has-active", anyOpen);
    };
    if (typeof MutationObserver === "function") {
        const mo = new MutationObserver(sync);
        panels.forEach(p => mo.observe(p, { attributes: true, attributeFilter: ["style"] }));
    }
    sync();
}

// ═══ 3 · INIT ═══════════════════════════════════════════════════════════════
function init() {
    try { initPresence(); }
    catch (err) { console.warn("[cb] presence rail failed", err); }

    try {
        buildDock();
        watchDrawers();
        const barEl = dockBar();
        if (barEl && typeof ResizeObserver === "function") {
            // The bar's own width is the source of truth for the fit check.
            // (Deliberately NOT observing #message-input: its width changes as
            // a RESULT of collapse/expand, which would feed the check with its
            // own output and oscillate forever.)
            new ResizeObserver(evaluateFit).observe(barEl);
        }
        window.addEventListener("resize", evaluateFit, { passive: true });
        // Fonts load async; re-check once everything has settled.
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(evaluateFit).catch(() => {});
        }
        evaluateFit();
    }
    catch (err) { console.warn("[cb] compose dock failed", err); }

    window.__cbShellRefine.api = {
        openDock, closeDock, updateRailFades, evaluateFit,
        get collapsed() { return collapsed; },
        get dockOpen()    { return dockOpen; },
    };
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();

})();
