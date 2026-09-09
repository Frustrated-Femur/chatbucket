/* ==========================================================================
   media-smooth.js — [UX]/[PERF] overlay. MUST load LAST (after fix-refine).

   Owns:
     1. Video thumbnails in chat bubbles — canvas-captured JPEG posters for
        same-origin /uploads/ videos, graceful fallback to the "#t=0.5"
        fragment (browser shows a real frame) for cross-origin sources.
     2. Video thumbnails in the ready-to-send attachment tray (#preview-list)
        — matched to the live File objects from #file-input via a name/size
        index, captured through object URLs (zero network cost).
     3. Lazy-loading removal — every <img>/<iframe>, existing AND future, is
        coerced to loading="eager". The property setter itself is wrapped, so
        even code that does `img.loading = "lazy"` is neutralised.
     4. Smoothness — decode-async images, fade-in on load, reserved media
        boxes (no layout shift), GPU-promoted preview tray.

   Everything is DOM-driven (MutationObserver). It patches behaviour without
   touching index.js internals; any failure degrades to stock behaviour.
   ========================================================================== */
(function () {
  "use strict";

  /* ── tunables ─────────────────────────────────────────────────────── */
  var POSTER_MAX_W   = 480;    // px, captured poster width cap
  var POSTER_QUALITY = 0.72;   // jpeg quality for captured posters
  var SEEK_T         = 0.5;    // seconds into the video to grab the frame
  var THUMB_BOX      = 96;     // px, preview-tray thumb box (matches CSS)

  /* ── helpers ──────────────────────────────────────────────────────── */
  function isVideoEl(el) {
    return el && el.tagName === "VIDEO";
  }

  function isProbablyVideoUrl(url) {
    return /\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(url || "");
  }

  /* ======================================================================
     1. LAZY-LOADING REMOVAL
     ====================================================================== */

  // Coerce the IDL property itself: anything that sets .loading = "lazy"
  // (libraries, future code, the browser default) gets "eager" instead.
  try {
    [HTMLImageElement, HTMLIFrameElement].forEach(function (Ctor) {
      var proto = Ctor.prototype;
      var desc = Object.getOwnPropertyDescriptor(proto, "loading");
      if (!desc || !desc.configurable) return;
      Object.defineProperty(proto, "loading", {
        configurable: true,
        enumerable: desc.enumerable,
        get: function () { return desc.get.call(this); },
        set: function (v) {
          desc.set.call(this, v === "lazy" ? "eager" : v);
        },
      });
    });
  } catch (e) { /* older engines: attribute sweep below still covers us */ }

  function eagerize(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var nodes = scope.querySelectorAll
      ? scope.querySelectorAll('img[loading="lazy"], iframe[loading="lazy"]')
      : [];
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].setAttribute("loading", "eager");
      nodes[i].removeAttribute("loading"); // attribute gone → default eager
    }
    // Root itself may be a lazy img inserted directly.
    if (root && root.nodeType === 1 &&
        /^(IMG|IFRAME)$/.test(root.tagName) &&
        root.getAttribute("loading") === "lazy") {
      root.removeAttribute("loading");
    }
  }

  /* ======================================================================
     2. SMOOTHNESS — async decode + fade-in
     ====================================================================== */

  function smoothifyImage(img) {
    if (img.dataset.msSmooth) return;
    img.dataset.msSmooth = "1";
    if (!img.hasAttribute("decoding")) img.setAttribute("decoding", "async");
    if (!img.complete || img.naturalWidth === 0) {
      img.classList.add("ms-fade");
      img.addEventListener("load", function () {
        img.classList.add("ms-loaded");
      }, { once: true });
    }
  }

  /* ======================================================================
     3. VIDEO POSTER CAPTURE
     ====================================================================== */

  // Capture one frame from `src` into a JPEG data URL.
  // onDone(dataUrl|null) — null means tainted/unreadable (caller falls back).
  function capturePoster(src, onDone) {
    var v = document.createElement("video");
    var settled = false;

    function finish(url) {
      if (settled) return;
      settled = true;
      v.removeAttribute("src");
      v.load();
      onDone(url);
    }

    var killer = setTimeout(function () { finish(null); }, 8000);

    v.muted = true;
    v.playsInline = true;
    v.preload = "auto";
    v.crossOrigin = "anonymous"; // same-origin /uploads/: fine

    v.addEventListener("loadeddata", function () {
      try {
        var t = Math.min(SEEK_T, (v.duration || 1) / 2);
        v.currentTime = isFinite(t) && t > 0 ? t : 0.1;
      } catch (e) { clearTimeout(killer); finish(null); }
    });

    v.addEventListener("seeked", function () {
      try {
        var w = v.videoWidth, h = v.videoHeight;
        if (!w || !h) { clearTimeout(killer); return finish(null); }
        var scale = Math.min(1, POSTER_MAX_W / w);
        var c = document.createElement("canvas");
        c.width = Math.round(w * scale);
        c.height = Math.round(h * scale);
        c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
        var url = c.toDataURL("image/jpeg", POSTER_QUALITY);
        clearTimeout(killer);
        finish(url);
      } catch (e) {
        // SecurityError → cross-origin without CORS. Caller uses #t fallback.
        clearTimeout(killer); finish(null);
      }
    });

    v.addEventListener("error", function () { clearTimeout(killer); finish(null); });
    v.src = src;
  }

  /* ======================================================================
     4. VIDEO THUMBNAILS IN CHAT
     ====================================================================== */

  function enhanceChatVideo(video) {
    if (video.dataset.msEnhanced) return;
    video.dataset.msEnhanced = "1";

    // Metadata up front: duration + intrinsic size (kills layout shift and
    // lets the browser paint a first frame even before our poster lands).
    if (!video.hasAttribute("preload")) video.setAttribute("preload", "metadata");
    video.muted = true;
    video.playsInline = true;
    video.classList.add("ms-video");

    // Wrap once in a positioned shell so CSS can draw the play glyph.
    var parent = video.parentNode;
    if (parent && !parent.classList.contains("ms-videothumb")) {
      var wrap = document.createElement("span");
      wrap.className = "ms-videothumb";
      parent.insertBefore(wrap, video);
      wrap.appendChild(video);
    }

    if (video.hasAttribute("poster")) return; // server/stock poster wins

    var src = video.currentSrc || video.src ||
              (video.querySelector("source") && video.querySelector("source").src);
    if (!src) return;

    capturePoster(src, function (poster) {
      if (!video.isConnected) return;
      if (poster) {
        video.poster = poster;
      } else if (!/#t=/.test(src) && /^https?:|^blob:|^\//.test(src)) {
        // Cross-origin w/o CORS: ask the browser itself to show a frame.
        try { video.src = src + (src.indexOf("#") === -1 ? "#t=" + SEEK_T : ""); } catch (e) {}
      }
    });
  }

  // File-card fallback: a bare download link pointing at a video gets an
  // inline thumbnail strip prepended (poster-captured, click passes through).
  function enhanceVideoFileLink(a) {
    if (a.dataset.msThumbDone) return;
    var href = a.getAttribute("href") || "";
    if (!isProbablyVideoUrl(href)) return;
    if (a.querySelector("img, video, canvas")) return; // already visual
    a.dataset.msThumbDone = "1";

    var holder = document.createElement("span");
    holder.className = "ms-videothumb ms-videothumb--card";
    var thumb = document.createElement("video");
    thumb.muted = true;
    thumb.playsInline = true;
    thumb.preload = "metadata";
    thumb.className = "ms-video ms-video--card";
    holder.appendChild(thumb);
    a.insertBefore(holder, a.firstChild);

    capturePoster(href, function (poster) {
      if (poster) {
        thumb.poster = poster;
        thumb.removeAttribute("src"); // poster alone — no extra fetch
      } else {
        thumb.src = href + "#t=" + SEEK_T;
      }
    });
  }

  function scanChat(root) {
    var scope = root || document;
    var vids = scope.querySelectorAll
      ? scope.querySelectorAll("#messages video:not([data-ms-enhanced])")
      : [];
    for (var i = 0; i < vids.length; i++) enhanceChatVideo(vids[i]);
    if (isVideoEl(root) && !root.dataset.msEnhanced &&
        root.closest && root.closest("#messages")) enhanceChatVideo(root);

    var links = scope.querySelectorAll
      ? scope.querySelectorAll("#messages a[href]:not([data-ms-thumb-done])")
      : [];
    for (var j = 0; j < links.length; j++) enhanceVideoFileLink(links[j]);
  }

  /* ======================================================================
     5. VIDEO THUMBNAILS IN THE READY-TO-SEND TRAY
     ====================================================================== */

  // Live index of the File objects the user just picked, keyed "name:size".
  // The tray renders names — we match back to the real File for object URLs.
  var pickedFiles = new Map();

  function indexPickedFiles(fileList) {
    if (!fileList) return;
    for (var i = 0; i < fileList.length; i++) {
      var f = fileList[i];
      pickedFiles.set(f.name + ":" + f.size, f);
      if (!pickedFiles.has(f.name)) pickedFiles.set(f.name, f); // loose key
    }
  }

  // Hook the input(s) at capture phase so we see the File objects no matter
  // which handler index.js bound.
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t && t.type === "file" && t.files && t.files.length) {
      indexPickedFiles(t.files);
    }
  }, true);

  function matchFileForItem(item) {
    var text = (item.textContent || "").trim();
    if (!text) return null;
    // Exact "name:size" is impossible from DOM text alone; try name keys.
    var hit = null;
    pickedFiles.forEach(function (f, key) {
      if (hit) return;
      var name = key.indexOf(":") === -1 ? key : key.slice(0, key.lastIndexOf(":"));
      if (name && text.indexOf(name) !== -1) hit = f;
    });
    return hit;
  }

  function buildTrayThumb(item, file) {
    if (!file || !/^video\//.test(file.type)) return;
    if (item.querySelector(".ms-tray-thumb")) return;

    var url = URL.createObjectURL(file);
    var holder = document.createElement("span");
    holder.className = "ms-tray-thumb";
    var img = document.createElement("img");
    img.alt = "";
    img.width = THUMB_BOX;
    img.height = THUMB_BOX;
    img.className = "ms-fade";
    holder.appendChild(img);
    item.insertBefore(holder, item.firstChild);

    capturePoster(url, function (poster) {
      if (poster) {
        img.src = poster;
        img.classList.add("ms-loaded");
        URL.revokeObjectURL(url);
      } else {
        // Should not happen for blob: URLs, but degrade to a frame video.
        var v = document.createElement("video");
        v.muted = true; v.playsInline = true; v.preload = "metadata";
        v.src = url + "#t=" + SEEK_T;
        holder.replaceChild(v, img);
      }
    });
  }

  function scanTray(root) {
    var list = document.getElementById("preview-list");
    if (!list) return;
    var items = list.children;
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.dataset.msTrayDone) continue;
      item.dataset.msTrayDone = "1";

      // Case A: index.js already rendered a <video> — just enhance it.
      var v = item.querySelector("video");
      if (v) { enhanceChatVideo(v); continue; }

      // Case B: name-only card → build a thumb from the matched File.
      var f = matchFileForItem(item);
      if (f && /^video\//.test(f.type)) buildTrayThumb(item, f);
    }
  }

  /* ======================================================================
     6. OBSERVERS — one pass for everything, batched per animation frame
     ====================================================================== */

  var scheduled = false;
  function scheduleScan() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      try {
        eagerize(document);
        scanChat(document);
        scanTray();
        var imgs = document.images;
        for (var i = 0; i < imgs.length; i++) smoothifyImage(imgs[i]);
      } catch (e) { /* degrade to stock */ }
    });
  }

  function boot() {
    scheduleScan();

    var messages = document.getElementById("messages");
    if (messages) {
      new MutationObserver(scheduleScan)
        .observe(messages, { childList: true, subtree: true });
    }

    var tray = document.getElementById("preview-list");
    if (tray) {
      new MutationObserver(scheduleScan)
        .observe(tray, { childList: true, subtree: true });
    }

    // Global lazy-attribute sweeper (catches panels: stickers/gifs/giphy).
    new MutationObserver(function (muts) {
      var needed = false;
      for (var i = 0; i < muts.length && !needed; i++) {
        var m = muts[i];
        if (m.type === "attributes") needed = true;
        else for (var j = 0; j < m.addedNodes.length; j++) {
          var n = m.addedNodes[j];
          if (n.nodeType === 1 &&
              (n.hasAttribute && n.hasAttribute("loading") ||
               n.querySelector && n.querySelector('[loading="lazy"]'))) {
            needed = true; break;
          }
        }
      }
      if (needed) scheduleScan();
    }).observe(document.body, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ["loading"],
    });

    // Re-index picks when the tray is cleared so stale entries can't leak.
    var clearBtn = document.getElementById("cancel-attach");
    if (clearBtn) {
      clearBtn.addEventListener("click", function () { pickedFiles.clear(); }, true);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
