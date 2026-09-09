/*
 * video-thumbs.js — real video thumbnails for ChatBucket web.
 *
 * The stock video bubble (`.video-thumbnail-container`) is text-only: a play
 * glyph + the filename, with no actual frame from the video. This module
 * upgrades each such bubble to show a real poster frame captured from the
 * video itself — matching what the Android app already does with Coil's
 * VideoFrameDecoder.
 *
 * Design:
 * - Zero changes to index.js. We observe the message list with a
 *   MutationObserver and enhance any `.video-thumbnail-container` that
 *   appears (history render, live append, older-page loads — all covered).
 * - Lazy + IntersectionObserver-gated: a thumbnail is generated only when the
 *   bubble is about to scroll into view, so a long history never spins up N
 *   off-screen <video> elements.
 * - The source <video> uses preload="metadata" and seek-to-1s, then draws a
 *   single frame to a <canvas> and tears the element down. Only one
 *   generation runs at a time (queued), so we never hold many decoders.
 * - Everything is wrapped in try/catch and feature-detected; if the browser
 *   can't decode a given codec, the bubble silently keeps its original
 *   text-only placeholder. Failures never break rendering.
 */
(function () {
  "use strict";

  // Feature / environment gate.
  if (typeof document === "undefined" || !document.createElement("canvas").getContext) return;
  if (typeof IntersectionObserver === "undefined") return;

  const SEEK_TO_S = 1.0;          // grab the frame at ~1s (matches Android)
  const MAX_DIM   = 480;          // cap the poster's longest edge
  const OBSERVE_MARGIN = "200px"; // start generating slightly before visible

  // One thumbnail at a time.
  const queue = [];
  let pumping = false;

  function enqueue(container) {
    queue.push(container);
    if (!pumping) pump();
  }

  function pump() {
    const next = queue.shift();
    if (!next) { pumping = false; return; }
    pumping = true;
    generate(next)
      .catch(() => { /* keep placeholder */ })
      .finally(() => pump());
  }

  function srcFor(container) {
    const raw = container.getAttribute("data-viewer-src") || "";
    if (!raw) return "";
    // Keep same-origin and http(s) only — never hand an arbitrary scheme to a
    // media element.
    if (/^(https?:)?\/\//i.test(raw) || raw.charAt(0) === "/") return raw;
    return "";
  }

  function generate(container) {
    return new Promise((resolve, reject) => {
      const src = srcFor(container);
      if (!src || !container.isConnected) return reject(new Error("no-src"));
      if (container.dataset.thumbDone === "1") return resolve();

      const video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.crossOrigin = "anonymous";

      let settled = false;
      const cleanup = () => {
        video.removeAttribute("src");
        try { video.load(); } catch (e) {}
        video.src = "";
      };
      const fail = () => { if (settled) return; settled = true; cleanup(); reject(new Error("thumb-failed")); };

      const timer = setTimeout(fail, 12000); // hard cap per video

      video.addEventListener("error", fail, { once: true });
      video.addEventListener("loadedmetadata", () => {
        if (settled) return;
        // Clamp the seek target inside the real duration.
        const t = isFinite(video.duration) && video.duration > 0
          ? Math.min(SEEK_TO_S, Math.max(0, video.duration - 0.1))
          : SEEK_TO_S;
        try { video.currentTime = t; } catch (e) { /* seek anyway on 'seeked' */ }
      });
      video.addEventListener("seeked", () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try {
          const vw = video.videoWidth || 0;
          const vh = video.videoHeight || 0;
          if (!vw || !vh) { cleanup(); return reject(new Error("no-frames")); }

          const scale = Math.min(1, MAX_DIM / Math.max(vw, vh));
          const cw = Math.max(1, Math.round(vw * scale));
          const ch = Math.max(1, Math.round(vh * scale));
          const canvas = document.createElement("canvas");
          canvas.width = cw;
          canvas.height = ch;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(video, 0, 0, cw, ch);
          cleanup();
          applyThumbnail(container, canvas.toDataURL("image/jpeg", 0.7));
          resolve();
        } catch (e) {
          cleanup();
          reject(e);
        }
      }, { once: true });

      // Kick off load.
      try { video.src = src; } catch (e) { clearTimeout(timer); fail(); }
    });
  }

  function applyThumbnail(container, dataUrl) {
    if (!container.isConnected) return;
    container.dataset.thumbDone = "1";
    const img = new Image();
    img.className = "video-thumb";
    img.alt = "";
    img.decoding = "async";
    img.src = dataUrl;
    // Insert as the first child so the play glyph + name overlay on top.
    container.insertBefore(img, container.firstChild);
    container.classList.add("has-thumb");
  }

  // Watch a container and generate when it nears the viewport.
  const io = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        const el = entry.target;
        io.unobserve(el);
        if (el.dataset.thumbDone !== "1") enqueue(el);
      }
    }
  }, { rootMargin: OBSERVE_MARGIN });

  function enhance(container) {
    if (!container || container.dataset.thumbWired === "1") return;
    container.dataset.thumbWired = "1";
    io.observe(container);
  }

  function scan(root) {
    const scope = root || document;
    if (scope.matches && scope.matches(".video-thumbnail-container")) enhance(scope);
    scope.querySelectorAll(".video-thumbnail-container").forEach(enhance);
  }

  // Observe the message list for new bubbles.
  function boot() {
    scan(document);
    const messages = document.getElementById("messages") || document.body;
    const mo = new MutationObserver((mutations) => {
      for (const m of mutations) {
        m.addedNodes.forEach((node) => {
          if (node.nodeType === 1) scan(node);
        });
      }
    });
    mo.observe(messages, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
