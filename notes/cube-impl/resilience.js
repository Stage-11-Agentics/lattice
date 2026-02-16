/**
 * Cube View — Resilience & Compatibility Layer
 * ==============================================
 *
 * This file contains all defensive code for the Cube graph view. The Cube
 * view is the first dashboard feature to depend on an external CDN library
 * (force-graph). Every function here handles a failure mode gracefully so
 * the dashboard degrades without breaking.
 *
 * Integration: These functions are called from renderCube() in index.html.
 * Copy them into the <script> block alongside the other render functions.
 *
 * CSS classes required (add to the <style> block):
 *
 *   .cube-fallback          — centered container for error/fallback states
 *   .cube-fallback-icon     — large warning icon (emoji)
 *   .cube-fallback-title    — bold heading for the fallback message
 *   .cube-fallback-msg      — explanatory text
 *   .cube-fallback-retry    — retry button (styled like .btn)
 *   .cube-mobile-banner     — dismissible info banner for small screens
 *   .cube-mobile-dismiss    — close button inside the banner
 *   .sr-only                — visually hidden, accessible to screen readers
 *
 * Script tag to add to <head>:
 *
 *   <script src="https://unpkg.com/force-graph" defer></script>
 *
 *   Use `defer` — not `async` — so it executes after the HTML is parsed
 *   but does not block parsing. This preserves the dashboard's fast load.
 */


// ===========================================================================
// 1. CDN Failure Detection
// ===========================================================================
//
// The force-graph library is loaded via <script defer>. If the CDN is
// unreachable (offline, firewall, DNS failure, corporate proxy block),
// the global `ForceGraph` constructor will be undefined.
//
// Call this at the TOP of renderCube(), after clearing the container.
// If it returns false, stop — the fallback UI is already shown.
// ---------------------------------------------------------------------------

function checkCubeAvailable() {
  if (typeof ForceGraph === 'undefined') {
    var container = document.getElementById('cube-container');
    if (container) {
      container.innerHTML =
        '<div class="cube-fallback">' +
          '<div class="cube-fallback-icon">\u26a0</div>' +
          '<div class="cube-fallback-title">Graph library unavailable</div>' +
          '<div class="cube-fallback-msg">' +
            'The visualization library failed to load. ' +
            'This may be due to a network issue or firewall restriction.' +
          '</div>' +
          '<button class="cube-fallback-retry btn" onclick="location.reload()">Retry</button>' +
        '</div>';
    }
    return false;
  }
  return true;
}


// ===========================================================================
// 2. Canvas / 2D Context Capability Detection
// ===========================================================================
//
// force-graph renders to an HTML5 Canvas element. Virtually all modern
// browsers support this, but headless environments, very old browsers,
// or restrictive enterprise configs might not.
//
// Call this AFTER checkCubeAvailable() returns true, still before init.
// If it returns false, show the fallback and stop.
// ---------------------------------------------------------------------------

function checkCanvasSupport() {
  var canvas = document.createElement('canvas');
  return !!(canvas.getContext && canvas.getContext('2d'));
}

function showCanvasFallback() {
  var container = document.getElementById('cube-container');
  if (container) {
    container.innerHTML =
      '<div class="cube-fallback">' +
        '<div class="cube-fallback-icon">\u26a0</div>' +
        '<div class="cube-fallback-title">Canvas not supported</div>' +
        '<div class="cube-fallback-msg">' +
          'Your browser does not support the Canvas element required ' +
          'for graph visualization. Please use a modern browser ' +
          'or switch to Board or List view.' +
        '</div>' +
      '</div>';
  }
}


// ===========================================================================
// 3. Mobile Viewport Detection
// ===========================================================================
//
// On screens narrower than 768px the graph is usable but awkward. Rather
// than disabling it, we show a dismissible info banner with interaction
// hints. The banner appears once per renderCube() call; dismissing it
// removes it until the next render.
//
// Call this inside renderCube() after the graph is initialized, or before
// — it only adds a banner to the container, it does not block rendering.
// ---------------------------------------------------------------------------

var _cubeMobileBannerDismissed = false;

function isMobileViewport() {
  return window.innerWidth < 768;
}

function showMobileBanner() {
  // Don't re-show if user already dismissed this session
  if (_cubeMobileBannerDismissed) return;

  var container = document.getElementById('cube-container');
  if (!container) return;

  var banner = document.createElement('div');
  banner.className = 'cube-mobile-banner';
  banner.setAttribute('role', 'status');
  banner.innerHTML =
    '<span>Graph visualization works best on larger screens. ' +
    'Pinch to zoom, drag to pan.</span>' +
    '<button class="cube-mobile-dismiss" ' +
      'aria-label="Dismiss mobile notice" ' +
      'onclick="this.parentNode.remove(); _cubeMobileBannerDismissed = true;"' +
    '>\u2715</button>';

  // Insert at top of container, before the canvas
  container.insertBefore(banner, container.firstChild);
}


// ===========================================================================
// 4. Script Tag (for <head>)
// ===========================================================================
//
// Add this to the <head> of index.html, AFTER the existing <style> block:
//
//   <script src="https://unpkg.com/force-graph" defer></script>
//
// Why `defer` and not `async`:
//   - `defer` guarantees execution order and waits for DOM parsing
//   - `async` could execute before our own <script>, causing race conditions
//   - `defer` still doesn't block the parser — page paints immediately
//
// The dashboard currently loads in under 50ms with zero CDN deps. Adding
// this defer'd script keeps initial paint fast; the graph library loads
// in the background and is ready by the time the user clicks "Cube".
//
// If the script fails to load (network error), ForceGraph will be
// undefined and checkCubeAvailable() handles it gracefully.
// ---------------------------------------------------------------------------


// ===========================================================================
// 5. Accessibility: Screen Reader Summary
// ===========================================================================
//
// The force-graph canvas is opaque to screen readers — it's just pixels.
// We provide two accessibility affordances:
//
//   a) role="img" and aria-label on the canvas container so assistive
//      technology announces it as an image with a description.
//
//   b) A visually-hidden text summary inside the container with the
//      current graph metrics and a pointer to accessible alternatives.
//
// Call buildA11ySummary() with the graph data, then inject the result
// as a .sr-only span inside the cube container.
// ---------------------------------------------------------------------------

function buildA11ySummary(data) {
  var nodeCount = data.nodes ? data.nodes.length : 0;
  var linkCount = data.links ? data.links.length : 0;
  return nodeCount + ' tasks with ' + linkCount + ' relationships ' +
    'displayed in graph view. ' +
    'Use the Board or List view for full accessibility.';
}

function applyCubeAccessibility(container, data) {
  // Mark the container as an image for assistive technology
  container.setAttribute('role', 'img');
  container.setAttribute('aria-label', 'Task relationship graph');

  // Add a visually-hidden text summary
  var summary = document.createElement('span');
  summary.className = 'sr-only';
  summary.textContent = buildA11ySummary(data);
  container.appendChild(summary);
}


// ===========================================================================
// 6. Error Boundary for Graph Initialization
// ===========================================================================
//
// Wraps the ForceGraph constructor call in a try-catch. If the library
// throws during initialization (bad data, internal bug, DOM issue), we
// catch it and show a user-friendly error instead of a blank screen.
//
// Returns the graph instance on success, or null on failure.
//
// Usage in renderCube():
//   var graph = safeInitGraph(container, graphData, initFn);
//   if (!graph) return;  // fallback already shown
//
// The `initFn` parameter is a function(container, data) that creates
// and configures the ForceGraph instance — this keeps the error boundary
// generic and the graph configuration separate.
// ---------------------------------------------------------------------------

function safeInitGraph(container, data, initFn) {
  try {
    var graph = initFn(container, data);
    return graph;
  } catch (err) {
    var message = (err && err.message) ? err.message : 'Unknown error';
    container.innerHTML =
      '<div class="cube-fallback">' +
        '<div class="cube-fallback-icon">\u26a0</div>' +
        '<div class="cube-fallback-title">Graph rendering failed</div>' +
        '<div class="cube-fallback-msg">' + esc(message) + '</div>' +
        '<button class="cube-fallback-retry btn" onclick="location.hash=\'#/cube\'">Retry</button>' +
      '</div>';
    // Log the full error for debugging (visible in browser console)
    if (typeof console !== 'undefined' && console.error) {
      console.error('[Cube] Graph initialization failed:', err);
    }
    return null;
  }
}


// ===========================================================================
// 7. Orchestration — Full Check Sequence
// ===========================================================================
//
// Convenience function that runs all pre-flight checks in order. Call
// this at the top of renderCube() and bail if it returns false.
//
// Usage:
//   function renderCube() {
//     var container = document.getElementById('cube-container');
//     container.innerHTML = '';
//     if (!cubePreflightChecks()) return;
//     // ... proceed with graph setup ...
//   }
// ---------------------------------------------------------------------------

function cubePreflightChecks() {
  // 1. Is the force-graph library loaded?
  if (!checkCubeAvailable()) return false;

  // 2. Does the browser support Canvas 2D?
  if (!checkCanvasSupport()) {
    showCanvasFallback();
    return false;
  }

  return true;
}


// ===========================================================================
// CSS — Required Styles
// ===========================================================================
//
// Add these to the <style> block in index.html. They use the existing
// CSS custom properties so they adapt to all dashboard themes.
//
// .cube-fallback {
//   display: flex;
//   flex-direction: column;
//   align-items: center;
//   justify-content: center;
//   padding: 48px 24px;
//   text-align: center;
//   color: var(--text-secondary);
//   min-height: 300px;
// }
// .cube-fallback-icon {
//   font-size: 2.5rem;
//   margin-bottom: 12px;
//   opacity: 0.6;
// }
// .cube-fallback-title {
//   font-size: 1.1rem;
//   font-weight: 600;
//   color: var(--text-primary);
//   margin-bottom: 8px;
// }
// .cube-fallback-msg {
//   font-size: 0.875rem;
//   color: var(--text-muted);
//   max-width: 400px;
//   line-height: 1.5;
//   margin-bottom: 16px;
// }
// .cube-fallback-retry {
//   padding: 6px 16px;
//   border: 1px solid var(--border);
//   border-radius: var(--radius-sm);
//   background: var(--surface);
//   cursor: pointer;
//   font-size: 0.875rem;
//   color: var(--text-primary);
// }
// .cube-fallback-retry:hover {
//   background: var(--surface-hover);
// }
// .cube-mobile-banner {
//   display: flex;
//   align-items: center;
//   justify-content: space-between;
//   gap: 12px;
//   padding: 8px 12px;
//   background: var(--color-warning-bg);
//   border: 1px solid var(--color-warning);
//   border-radius: var(--radius-sm);
//   font-size: 0.8rem;
//   color: var(--text-on-warning);
//   margin-bottom: 8px;
// }
// .cube-mobile-dismiss {
//   background: none;
//   border: none;
//   cursor: pointer;
//   font-size: 1rem;
//   color: var(--text-muted);
//   padding: 0 4px;
//   line-height: 1;
// }
// .cube-mobile-dismiss:hover {
//   color: var(--text-primary);
// }
// .sr-only {
//   position: absolute;
//   width: 1px;
//   height: 1px;
//   padding: 0;
//   margin: -1px;
//   overflow: hidden;
//   clip: rect(0, 0, 0, 0);
//   white-space: nowrap;
//   border: 0;
// }
//
// ===========================================================================
