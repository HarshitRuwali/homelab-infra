/*
 * diagram-zoom.js: click a Mermaid diagram to open it full screen, where it can
 * be zoomed and panned.
 *
 * Material renders each diagram into a closed shadow root, so nothing outside
 * Material can read the SVG. This file wraps Element.prototype.attachShadow to
 * remember the root of every `.mermaid` host as Material creates it, and leaves
 * the root closed. It has to load after Material's bundle (extra_javascript
 * does) and before the diagrams render, which waits on Mermaid arriving from
 * the CDN. A diagram that rendered before this ran is simply not zoomable.
 *
 * The viewer works on a clone of the SVG, so the page is never touched.
 *
 *   click or Enter    open a diagram
 *   wheel, pinch, + - zoom (around the pointer for the wheel and pinch)
 *   drag, arrow keys  pan
 *   double-click      zoom in at the pointer
 *   0                 fit the diagram to the screen
 *   Esc, Close, or a click outside the diagram    close
 */
(function () {
  "use strict";

  const roots = new WeakMap();
  const attachShadow = Element.prototype.attachShadow;

  Element.prototype.attachShadow = function (init) {
    const root = attachShadow.call(this, init);
    if (this.classList && this.classList.contains("mermaid")) {
      roots.set(this, root);
      this.classList.add("diagram-zoomable");
      this.setAttribute("tabindex", "0");
      this.setAttribute("role", "button");
      this.setAttribute("aria-label", "Enlarge diagram");
    }
    return root;
  };

  const FIT_PADDING = 48; // px between a fitted diagram and the screen edge
  const MAX_FIT = 4; // a small diagram is enlarged to fill the screen, up to this
  const STEP = 1.25; // one press of a zoom button or key
  const PAN_STEP = 60; // px per arrow key press
  const DRAG_SLOP = 5; // px of movement that turns a click into a drag

  let viewer = null;

  function el(tag, className, attrs) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    for (const name in attrs || {}) node.setAttribute(name, attrs[name]);
    return node;
  }

  function button(label, text, onClick) {
    const b = el("button", "diagram-zoom__button", {
      type: "button",
      "aria-label": label,
      title: label,
    });
    b.textContent = text;
    b.addEventListener("click", onClick);
    return b;
  }

  // The natural size of a Mermaid SVG comes from its viewBox; the width and
  // height attributes are "100%" and a max-width, which mean nothing off-page.
  function naturalSize(svg) {
    const box = svg.viewBox && svg.viewBox.baseVal;
    if (box && box.width && box.height) return { w: box.width, h: box.height };
    const rect = svg.getBoundingClientRect();
    return { w: rect.width || 1, h: rect.height || 1 };
  }

  function openViewer(host) {
    if (viewer) return;
    const root = roots.get(host);
    const source = root && root.querySelector("svg");
    if (!source) return;

    const size = naturalSize(source);
    const svg = source.cloneNode(true);
    svg.removeAttribute("style");
    svg.setAttribute("width", size.w);
    svg.setAttribute("height", size.h);
    svg.style.cssText = "display:block;max-width:none;overflow:visible";

    const overlay = el("div", "diagram-zoom", {
      role: "dialog",
      "aria-modal": "true",
      "aria-label": "Diagram viewer",
    });
    const surface = el("div", "diagram-zoom__surface");
    const stage = el("div", "diagram-zoom__stage");
    stage.appendChild(svg);
    surface.appendChild(stage);

    const level = el("span", "diagram-zoom__level", { "aria-hidden": "true" });
    const bar = el("div", "diagram-zoom__bar");
    const closeButton = button("Close", "Close", close);
    bar.append(
      button("Zoom out", "−", () => zoomBy(1 / STEP)),
      level,
      button("Zoom in", "+", () => zoomBy(STEP)),
      button("Fit to screen", "Fit", fit),
      closeButton
    );
    const hint = el("p", "diagram-zoom__hint");
    hint.textContent = matchMedia("(pointer: coarse)").matches
      ? "Pinch to zoom, drag to pan, tap outside the diagram to close"
      : "Scroll to zoom, drag to pan, Esc to close";
    overlay.append(surface, bar, hint);

    let k = 1; // scale
    let x = 0; // translation, px
    let y = 0;
    let fitScale = 1;

    const minScale = () => Math.min(fitScale, 1) * 0.5;
    const maxScale = () => Math.max(fitScale, 1) * 8;

    function apply() {
      stage.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
      level.textContent = Math.round(k * 100) + "%";
    }

    function fit() {
      const vw = surface.clientWidth;
      const vh = surface.clientHeight;
      fitScale = Math.min(
        (vw - 2 * FIT_PADDING) / size.w,
        (vh - 2 * FIT_PADDING) / size.h,
        MAX_FIT
      );
      if (!(fitScale > 0)) fitScale = 1;
      k = fitScale;
      x = (vw - size.w * k) / 2;
      y = (vh - size.h * k) / 2;
      apply();
    }

    // Zoom by `factor`, keeping the point (cx, cy) in surface coordinates fixed.
    function zoomAt(factor, cx, cy) {
      const next = Math.min(maxScale(), Math.max(minScale(), k * factor));
      const ratio = next / k;
      x = cx - (cx - x) * ratio;
      y = cy - (cy - y) * ratio;
      k = next;
      apply();
    }

    function zoomBy(factor) {
      zoomAt(factor, surface.clientWidth / 2, surface.clientHeight / 2);
    }

    function local(event) {
      const r = surface.getBoundingClientRect();
      return { x: event.clientX - r.left, y: event.clientY - r.top };
    }

    surface.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const lines = event.deltaMode === 1 ? 16 : 1;
        const speed = event.ctrlKey ? 0.01 : 0.0015; // ctrlKey is a trackpad pinch
        const p = local(event);
        zoomAt(Math.exp(-event.deltaY * lines * speed), p.x, p.y);
      },
      { passive: false }
    );

    // Pointer events cover mouse, pen and touch: one pointer pans, two pinch.
    const pointers = new Map();
    let gesture = null; // last { cx, cy, dist } of the pointers on screen
    let moved = 0;
    let pressedOnDiagram = false;

    function snapshot() {
      const pts = Array.from(pointers.values());
      if (pts.length === 1) return { cx: pts[0].x, cy: pts[0].y, dist: 0 };
      return {
        cx: (pts[0].x + pts[1].x) / 2,
        cy: (pts[0].y + pts[1].y) / 2,
        dist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y),
      };
    }

    surface.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      surface.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1) {
        moved = 0;
        // Pointer capture makes the click's target this surface whatever is
        // underneath, so where the press started has to be recorded here.
        pressedOnDiagram = svg.contains(event.target);
      }
      if (pointers.size <= 2) gesture = snapshot();
      surface.classList.add("is-dragging");
    });

    surface.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (!gesture || pointers.size > 2) return;
      const now = snapshot();
      moved = Math.max(moved, Math.hypot(now.cx - gesture.cx, now.cy - gesture.cy));
      const r = surface.getBoundingClientRect();
      if (pointers.size === 2 && gesture.dist > 0) {
        zoomAt(now.dist / gesture.dist, now.cx - r.left, now.cy - r.top);
      }
      x += now.cx - gesture.cx;
      y += now.cy - gesture.cy;
      apply();
      gesture = now;
    });

    function release(event) {
      if (!pointers.delete(event.pointerId)) return;
      gesture = pointers.size ? snapshot() : null;
      if (!pointers.size) surface.classList.remove("is-dragging");
    }
    surface.addEventListener("pointerup", release);
    surface.addEventListener("pointercancel", release);

    // A click outside the diagram, with no drag, closes the viewer. This is a
    // click and not a pointerup so that no later click lands on the page below.
    surface.addEventListener("click", (event) => {
      if (moved < DRAG_SLOP && !pressedOnDiagram) close();
    });

    surface.addEventListener("dblclick", (event) => {
      const p = local(event);
      zoomAt(2, p.x, p.y);
    });

    function pan(dx, dy) {
      x += dx;
      y += dy;
      apply();
    }

    function trapTab(event) {
      const stops = Array.from(overlay.querySelectorAll("button"));
      const next = stops.indexOf(document.activeElement) + (event.shiftKey ? -1 : 1);
      stops[(next + stops.length) % stops.length].focus();
    }

    function onKey(event) {
      if (event.ctrlKey || event.metaKey || event.altKey) return; // browser shortcuts
      switch (event.key) {
        case "Escape": close(); break;
        case "+": case "=": zoomBy(STEP); break;
        case "-": case "_": zoomBy(1 / STEP); break;
        case "0": fit(); break;
        case "ArrowLeft": pan(PAN_STEP, 0); break;
        case "ArrowRight": pan(-PAN_STEP, 0); break;
        case "ArrowUp": pan(0, PAN_STEP); break;
        case "ArrowDown": pan(0, -PAN_STEP); break;
        case "Tab": trapTab(event); break;
        default: return; // Enter and Space stay with the focused button
      }
      event.preventDefault();
    }

    const onResize = () => fit();
    const scrollLock = document.documentElement.style.overflow;

    function close() {
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onResize);
      document.documentElement.style.overflow = scrollLock;
      overlay.remove();
      viewer = null;
      host.focus({ preventScroll: true });
    }

    document.documentElement.style.overflow = "hidden";
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);
    fit();
    closeButton.focus();
    viewer = { close };
  }

  document.addEventListener("click", (event) => {
    const host = event.target instanceof Element && event.target.closest(".diagram-zoomable");
    if (host) openViewer(host);
  });

  document.addEventListener("keydown", (event) => {
    if (viewer || event.repeat || (event.key !== "Enter" && event.key !== " ")) return;
    const host = event.target instanceof Element && event.target.closest(".diagram-zoomable");
    if (!host) return;
    event.preventDefault();
    openViewer(host);
  });
})();
