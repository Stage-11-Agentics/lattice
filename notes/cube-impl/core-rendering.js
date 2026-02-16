// =============================================================================
// Lattice Cube View — Core Graph Rendering
// =============================================================================
//
// Integration snippet for index.html. Uses the 2D force-graph library with
// status-constrained X-axis layout. No modules, no arrow functions, var only.
//
// CDN dependencies (add to <head>, in this order):
//   <script src="https://unpkg.com/d3-force-3d@3"></script>
//   <script src="https://unpkg.com/force-graph@1/dist/force-graph.min.js"></script>
//
// d3-force-3d is loaded separately because force-graph bundles it internally
// but does NOT expose the d3 global. We need d3.forceX, d3.forceY, and
// d3.forceManyBody constructors to configure custom forces via .d3Force().
//
// Assumes these globals exist (defined elsewhere in index.html):
//   config         — parsed /api/config response (has config.workflow.statuses)
//   getLaneColor()  — returns hex color for a status, theme-aware
//   esc()          — HTML escaping
//   api()          — async fetch wrapper returning parsed JSON data field
//   currentView    — the active view name string
//
// =============================================================================


// ---- Section 1: Module-level state ----

var cubeGraph = null;          // ForceGraph instance (or null when not mounted)
var cubeResizeHandler = null;  // stored reference for removeEventListener
var cubeData = null;           // last fetched graph data (for diffing on refresh)


// ---- Section 2: Constants ----

// Edge colors by relationship type
var CUBE_EDGE_COLORS = {
  blocks:      "#dc3545",   // red
  depends_on:  "#fd7e14",   // orange
  subtask_of:  "#0d6efd",   // blue
  related_to:  "#6c757d",   // gray
  spawned_by:  "#6f42c1",   // purple
  duplicate_of:"#adb5bd",   // muted
  supersedes:  "#adb5bd"    // muted
};
var CUBE_EDGE_COLOR_DEFAULT = "#adb5bd";

// Human-readable labels for edge types (used in legend)
var CUBE_EDGE_LABELS = {
  blocks:      "Blocks",
  depends_on:  "Depends on",
  subtask_of:  "Subtask of",
  related_to:  "Related to",
  spawned_by:  "Spawned by"
};

// Priority -> node size mapping (used by nodeVal)
var CUBE_PRIORITY_SIZE = {
  critical: 12,
  high:     8,
  medium:   5,
  low:      3
};
var CUBE_PRIORITY_SIZE_DEFAULT = 5;


// ---- Section 3: renderCube() — main entry point ----

async function renderCube() {
  var app = document.getElementById("app");
  app.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

  // Cleanup any previous graph instance before building a new one
  cleanupCube();

  // ---- CDN fallback: check that libraries loaded ----
  if (typeof ForceGraph === "undefined") {
    app.innerHTML = '<div class="empty">'
      + '<p>Graph library failed to load.</p>'
      + '<p style="color:var(--text-muted);font-size:.875rem;">'
      + 'The Cube view requires an external library (force-graph). '
      + 'Check your network connection and reload.</p></div>';
    return;
  }
  if (typeof d3 === "undefined" || typeof d3.forceX !== "function") {
    app.innerHTML = '<div class="empty">'
      + '<p>Force layout library failed to load.</p>'
      + '<p style="color:var(--text-muted);font-size:.875rem;">'
      + 'The Cube view requires d3-force-3d. '
      + 'Check your network connection and reload.</p></div>';
    return;
  }

  // ---- Fetch graph data ----
  try {
    var data = await api("/api/graph");
  } catch (e) {
    app.innerHTML = '<div class="empty"><p>Failed to load graph data: ' + esc(e.message) + '</p></div>';
    return;
  }

  // Store for later diffing on refresh
  cubeData = data;

  var nodes = (data && data.nodes) || [];
  var links = (data && data.links) || [];

  // ---- Empty state: not enough tasks ----
  if (nodes.length < 3) {
    app.innerHTML = '<div class="empty cube-empty">'
      + '<p>Not enough tasks with relationships to visualize.</p>'
      + '<p style="color:var(--text-muted);font-size:.875rem;">'
      + 'The graph view becomes useful with 3+ tasks.</p>'
      + '</div>';
    return;
  }

  // ---- Build container DOM ----
  app.innerHTML = '<div id="cube-container" style="'
    + 'position:relative;width:100%;height:calc(100vh - 60px);overflow:hidden;'
    + 'background:var(--bg-base);"></div>';
  var container = document.getElementById("cube-container");

  // Append legend overlay
  container.insertAdjacentHTML("beforeend", buildCubeLegendHTML());

  // ---- Layout parameters ----
  // Adapt spacing and force strengths for small graphs so they don't
  // look sparse and lost in the viewport.
  var statuses = (config && config.workflow && config.workflow.statuses) || [];
  var isSmallGraph = nodes.length < 10;
  var statusSpacing = isSmallGraph ? 140 : 200;
  var chargeStrength = isSmallGraph ? -80 : -150;
  var xStrength = isSmallGraph ? 0.9 : 0.8;
  var yStrength = isSmallGraph ? 0.15 : 0.1;

  // Build status -> X position map.
  // Each status column gets a fixed X coordinate. Nodes are pulled toward
  // their status column by a strong forceX, creating a left-to-right
  // pipeline layout WITHOUT relying on dagMode (which uses topology, not
  // status, and produces wrong results when graph structure doesn't match
  // the workflow progression).
  var statusX = {};
  statuses.forEach(function(s, i) {
    statusX[s] = i * statusSpacing;
  });

  // ---- Compute text color once (CSS vars don't work in canvas) ----
  var textColor = getComputedStyle(document.documentElement)
    .getPropertyValue("--text-primary").trim() || "#212529";

  // ---- Instantiate ForceGraph (2D) ----
  var w = container.offsetWidth;
  var h = container.offsetHeight;

  cubeGraph = ForceGraph()(container)
    .graphData({ nodes: nodes, links: links })
    .width(w)
    .height(h)

    // Node identity
    .nodeId("id")

    // Node tooltip on hover: short_id + title + metadata
    .nodeLabel(function(node) {
      var label = "";
      if (node.short_id) label += node.short_id + ": ";
      label += node.title || node.id;
      if (node.status) label += " [" + node.status + "]";
      if (node.assigned_to) label += " \u2014 " + node.assigned_to;
      return label;
    })

    // Node color: theme-aware status color (used as fallback; custom
    // canvas rendering below overrides the default circle)
    .nodeColor(function(node) {
      return getLaneColor(node.status);
    })

    // Node size: priority-based with safe fallback
    .nodeVal(function(node) {
      if (node.priority && CUBE_PRIORITY_SIZE[node.priority]) {
        return CUBE_PRIORITY_SIZE[node.priority];
      }
      return CUBE_PRIORITY_SIZE_DEFAULT;
    })

    // Link identity fields
    .linkSource("source")
    .linkTarget("target")

    // Link color: relationship-type based
    .linkColor(function(link) {
      return CUBE_EDGE_COLORS[link.type] || CUBE_EDGE_COLOR_DEFAULT;
    })

    // Link width: slightly thicker for "blocks" to emphasize critical paths
    .linkWidth(function(link) {
      return link.type === "blocks" ? 2 : 1;
    })

    // Directional arrows on all edges
    .linkDirectionalArrowLength(6)
    .linkDirectionalArrowRelPos(1)
    .linkDirectionalArrowColor(function(link) {
      return CUBE_EDGE_COLORS[link.type] || CUBE_EDGE_COLOR_DEFAULT;
    })

    // Slight curve to distinguish overlapping/bidirectional edges
    .linkCurvature(0.15)

    // Click node -> navigate to task detail view
    .onNodeClick(function(node) {
      if (node && node.id) {
        location.hash = "#/task/" + node.id;
      }
    })

    // ---- Custom canvas rendering: circle + short_id label ----
    // Replaces the default circle renderer so we can add text labels
    // and scale them properly with zoom.
    .nodeCanvasObjectMode(function() { return "replace"; })
    .nodeCanvasObject(function(node, ctx, globalScale) {
      var size = CUBE_PRIORITY_SIZE[node.priority] || CUBE_PRIORITY_SIZE_DEFAULT;
      var radius = Math.sqrt(size) * 2.5;
      var color = getLaneColor(node.status);

      // Filled circle
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI, false);
      ctx.fillStyle = color;
      ctx.fill();

      // Subtle border for definition against backgrounds
      ctx.strokeStyle = "rgba(0,0,0,0.15)";
      ctx.lineWidth = 0.5;
      ctx.stroke();

      // Label: only render when zoomed in enough to be readable.
      // At very high zoom the font gets huge, so cap it.
      var fontSize = 10 / globalScale;
      if (fontSize < 2) return;
      if (fontSize > 10) fontSize = 10;

      var label = node.short_id || node.id.substring(0, 8);
      ctx.font = fontSize + "px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = textColor;
      ctx.fillText(label, node.x, node.y + radius + 2);
    });

  // ---- Apply status-constrained forces ----
  //
  // This is the key layout strategy. Instead of using dagMode (which
  // positions by graph topology), we use d3 forces to pin each node's
  // X coordinate to its status column. The Y axis has weak centering
  // force, letting nodes spread vertically within their column.
  //
  // d3.forceX / forceY / forceManyBody come from the d3-force-3d CDN
  // script. The .d3Force() method on the graph instance accesses the
  // internal d3 simulation to add or replace named forces.
  cubeGraph
    .d3Force("x", d3.forceX(function(d) {
      return statusX[d.status] || 0;
    }).strength(xStrength))
    .d3Force("y", d3.forceY(0).strength(yStrength))
    .d3Force("charge", d3.forceManyBody().strength(chargeStrength))
    // Remove the default center force — it fights with our explicit
    // X-positioning and pulls everything back toward (0,0).
    .d3Force("center", null);

  // ---- Window resize handler ----
  cubeResizeHandler = function() {
    if (!cubeGraph) return;
    var c = document.getElementById("cube-container");
    if (!c) return;
    cubeGraph.width(c.offsetWidth).height(c.offsetHeight);
  };
  window.addEventListener("resize", cubeResizeHandler);

  // ---- Zoom to fit after simulation stabilizes ----
  // The force simulation needs ~2 seconds to reach equilibrium with
  // custom forces. Reheat to ensure our new forces are applied, then
  // zoom the camera to fit all nodes with comfortable padding.
  cubeGraph.d3ReheatSimulation();
  setTimeout(function() {
    if (cubeGraph) {
      cubeGraph.zoomToFit(400, 40); // 400ms transition, 40px padding
    }
  }, 2000);
}


// ---- Section 4: updateCubeData() — refresh data without full reinit ----
//
// Called by the auto-refresh tick handler when currentView === "cube".
// Fetches fresh data from /api/graph and updates the graph in-place if
// anything changed. Avoids tearing down and rebuilding the ForceGraph
// instance, which would reset zoom/pan state and re-run the simulation.

async function updateCubeData() {
  if (!cubeGraph) return;

  try {
    var data = await api("/api/graph");
  } catch (e) {
    // Silently skip — graph keeps showing last-known data.
    // Transient network errors shouldn't blank the view.
    return;
  }

  var nodes = (data && data.nodes) || [];
  var links = (data && data.links) || [];

  // Quick diff: the /api/graph endpoint returns a revision string
  // (format: "count:timestamp"). If revision matches, nothing changed.
  if (cubeData && data.revision && cubeData.revision === data.revision) {
    return;
  }

  // If node count dropped below threshold, switch to empty state
  if (nodes.length < 3) {
    cubeData = data;
    renderCube();
    return;
  }

  // Update graph data in place — force-graph handles the diff/merge
  // internally, preserving existing node positions and simulation state.
  cubeData = data;
  cubeGraph.graphData({ nodes: nodes, links: links });
}


// ---- Section 5: cleanupCube() — tear down graph instance ----
//
// Must be called when navigating away from the Cube view. Stops the
// animation loop, removes the resize listener, and clears the container.
// Without this, the canvas keeps rendering in the background and the
// resize handler fires on a detached container.

function cleanupCube() {
  if (cubeResizeHandler) {
    window.removeEventListener("resize", cubeResizeHandler);
    cubeResizeHandler = null;
  }
  if (cubeGraph) {
    // Pause the animation/rendering loop
    cubeGraph.pauseAnimation();
    // Clear the container so force-graph's canvas is removed from DOM
    var container = document.getElementById("cube-container");
    if (container) {
      container.innerHTML = "";
    }
    cubeGraph = null;
  }
  cubeData = null;
}


// ---- Section 6: Legend overlay HTML builder ----
//
// Generates a positioned overlay in the bottom-left of the cube container
// showing status colors (circles matching node colors) and relationship
// type colors (lines matching edge colors). Uses inline styles to avoid
// needing separate CSS rules.

function buildCubeLegendHTML() {
  var statuses = (config && config.workflow && config.workflow.statuses) || [];

  var html = '<div class="cube-legend" style="'
    + "position:absolute;bottom:16px;left:16px;z-index:10;"
    + "background:var(--surface);border:1px solid var(--border);"
    + "border-radius:var(--radius-md);padding:10px 14px;"
    + "font-size:.75rem;color:var(--text-secondary);"
    + "max-width:220px;pointer-events:auto;"
    + "box-shadow:var(--shadow-card-hover);"
    + "opacity:0.92;"
    + '">';

  // ---- Status colors ----
  html += '<div style="font-weight:600;margin-bottom:6px;color:var(--text-primary);">'
    + 'Statuses</div>';
  statuses.forEach(function(s) {
    var color = getLaneColor(s);
    html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;">'
      + '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
      + 'background:' + esc(color) + ';flex-shrink:0;"></span>'
      + '<span>' + esc(s.replace(/_/g, " ")) + '</span>'
      + '</div>';
  });

  // ---- Divider ----
  html += '<div style="border-top:1px solid var(--border-subtle);margin:8px 0;"></div>';

  // ---- Relationship type colors ----
  html += '<div style="font-weight:600;margin-bottom:6px;color:var(--text-primary);">'
    + 'Relationships</div>';
  var edgeType;
  for (edgeType in CUBE_EDGE_LABELS) {
    if (CUBE_EDGE_LABELS.hasOwnProperty(edgeType)) {
      var edgeColor = CUBE_EDGE_COLORS[edgeType] || CUBE_EDGE_COLOR_DEFAULT;
      // Show a short colored line (mimicking a graph edge) next to the label
      html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;">'
        + '<span style="display:inline-block;width:14px;height:2px;'
        + 'background:' + esc(edgeColor) + ';flex-shrink:0;"></span>'
        + '<span>' + esc(CUBE_EDGE_LABELS[edgeType]) + '</span>'
        + '</div>';
    }
  }

  html += '</div>';
  return html;
}


// =============================================================================
// Section 7: Integration Checklist
// =============================================================================
//
// To wire this into index.html, make these changes:
//
// 1. CDN SCRIPTS — add to <head>, BEFORE the inline <script> block:
//
//    <script src="https://unpkg.com/d3-force-3d@3"></script>
//    <script src="https://unpkg.com/force-graph@1/dist/force-graph.min.js"></script>
//
//    d3-force-3d exposes `window.d3` with forceX/forceY/forceManyBody
//    constructors. force-graph exposes `window.ForceGraph`. Both are
//    checked at the top of renderCube() with graceful fallback messages.
//
// 2. NAV TAB — add after the Stats tab in the .nav-tabs section:
//
//    <span class="nav-tab" data-view="cube">Cube</span>
//
// 3. ROUTE — in the route() function, add before the final else:
//
//    else if (view === "cube") await renderCube();
//
// 4. CLEANUP ON NAVIGATE — at the TOP of route(), before any render:
//
//    cleanupCube();  // no-ops if cube isn't active (checks cubeGraph)
//
//    This ensures the canvas, animation loop, and resize handler are
//    torn down whenever the user navigates to any other view. Safe to
//    call unconditionally — it early-returns if cubeGraph is null.
//
// 5. AUTO-REFRESH — in the refresh tick handler, add:
//
//    if (currentView === "cube") { await updateCubeData(); return; }
//
//    This avoids re-fetching /api/tasks when we only need /api/graph.
//
// 6. NO EXTRA CSS NEEDED — the .cube-empty class inherits .empty styles.
//    Legend and container use inline styles. The force-graph library
//    creates and manages its own <canvas> element.
//
// =============================================================================
