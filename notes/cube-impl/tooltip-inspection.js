/**
 * Cube Graph View — Tooltip & Inspection Interaction Code
 *
 * Provides hover tooltips, click-to-select highlighting, double-click
 * navigation, link hover labels, a selection side panel, and background
 * deselection for the force-graph based Cube view.
 *
 * Dependencies (expected to exist in the dashboard's global scope):
 *   - esc(str)            — HTML-escapes a string
 *   - getLaneColor(status) — returns hex color for a task status
 *   - config              — Lattice config object
 *
 * CSS classes required (add to the dashboard stylesheet):
 *   .cube-tooltip
 *   .cube-tooltip-id
 *   .cube-tooltip-row
 *   .cube-tooltip-status-dot
 *   .cube-tooltip-hint
 *   .cube-side-panel
 *   .cube-side-panel-header
 *   .cube-side-panel-close
 *   .cube-side-panel-id
 *   .cube-side-panel-title
 *   .cube-side-panel-meta
 *   .cube-side-panel-meta-row
 *   .cube-side-panel-meta-label
 *   .cube-side-panel-open-btn
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

var highlightNodes = new Set();
var highlightLinks = new Set();
var selectedNode = null;

// Double-click detection (force-graph has no onNodeDblClick)
var lastClickTime = 0;
var lastClickNode = null;
var DBLCLICK_MS = 300;

// ---------------------------------------------------------------------------
// Relationship-type edge colors
// ---------------------------------------------------------------------------

/**
 * Returns a color for a given relationship type. Semantically grouped:
 *   - blocking/dependency edges are warm (red/orange)
 *   - structural edges (subtask, spawned) are cool (blue/teal)
 *   - informational edges (related, duplicate, supersedes) are neutral
 */
function getRelColor(type) {
  switch (type) {
    case "blocks":       return "#e53935"; // red
    case "depends_on":   return "#fb8c00"; // orange
    case "subtask_of":   return "#1e88e5"; // blue
    case "spawned_by":   return "#00897b"; // teal
    case "related_to":   return "#8e99a4"; // grey
    case "duplicate_of": return "#ab47bc"; // purple
    case "supersedes":   return "#7e57c2"; // deep purple
    default:             return "#999999";
  }
}

// ---------------------------------------------------------------------------
// 1. Rich hover tooltip (node)
// ---------------------------------------------------------------------------

/**
 * Bind to graph via: graph.nodeLabel(cubeNodeLabel);
 *
 * Returns an HTML string rendered by force-graph's built-in tooltip.
 */
function cubeNodeLabel(node) {
  var statusColor = getLaneColor(node.status);
  var displayId = node.short_id || node.id;

  var html = '<div class="cube-tooltip">';

  // Short ID header
  html += '<div class="cube-tooltip-id">' + esc(displayId) + '</div>';

  // Title
  html += '<div class="cube-tooltip-row">' + esc(node.title || "Untitled") + '</div>';

  // Status with colored dot
  if (node.status) {
    html += '<div class="cube-tooltip-row">';
    html += '<span class="cube-tooltip-status-dot" style="background:' + esc(statusColor) + '"></span> ';
    html += esc(node.status);
    html += '</div>';
  }

  // Priority
  if (node.priority) {
    html += '<div class="cube-tooltip-row">Priority: ' + esc(node.priority) + '</div>';
  }

  // Assigned to
  if (node.assigned_to) {
    html += '<div class="cube-tooltip-row">Assigned: ' + esc(node.assigned_to) + '</div>';
  }

  // Interaction hint
  html += '<div class="cube-tooltip-hint">Click to select &middot; Double-click to open</div>';

  html += '</div>';
  return html;
}

// ---------------------------------------------------------------------------
// 2. Link hover tooltip
// ---------------------------------------------------------------------------

/**
 * Bind to graph via: graph.linkLabel(cubeLinkLabel);
 *
 * Returns a plain-text label showing: SourceID  --type-->  TargetID
 */
function cubeLinkLabel(link) {
  var sourceName = (typeof link.source === "object")
    ? (link.source.short_id || link.source.id)
    : link.source;
  var targetName = (typeof link.target === "object")
    ? (link.target.short_id || link.target.id)
    : link.target;

  // Format: "LAT-12 blocks LAT-34"
  return esc(sourceName) + " " + esc(link.type) + " " + esc(targetName);
}

// ---------------------------------------------------------------------------
// 3 & 4. Click behavior — select/highlight + double-click navigation
// ---------------------------------------------------------------------------

/**
 * Bind to graph via: graph.onNodeClick(cubeOnNodeClick);
 *
 * Single click: select node, highlight neighbors, show side panel.
 * Rapid double-click: navigate to the task detail view.
 *
 * @param {object} node      - The clicked node object
 * @param {object} graphData - Reference to {nodes: [...], links: [...]}
 * @param {Element} cubeContainer - DOM element that wraps the graph (for
 *                                  side panel injection)
 */
function cubeOnNodeClick(node, graphData, cubeContainer) {
  if (!node) return;

  var now = Date.now();

  // --- Double-click detection ---
  if (lastClickNode === node && (now - lastClickTime) < DBLCLICK_MS) {
    // Reset double-click tracker
    lastClickTime = 0;
    lastClickNode = null;
    // Navigate to task detail
    location.hash = "#/task/" + node.id;
    return;
  }

  // Record for potential double-click
  lastClickTime = now;
  lastClickNode = node;

  // --- Single click: toggle selection ---
  if (selectedNode === node) {
    // Deselect
    selectedNode = null;
    highlightNodes.clear();
    highlightLinks.clear();
    removeSidePanel(cubeContainer);
  } else {
    // Select
    selectedNode = node;
    highlightNodes.clear();
    highlightLinks.clear();
    highlightNodes.add(node);

    // Walk links to find neighbors
    graphData.links.forEach(function(link) {
      var s = (typeof link.source === "object")
        ? link.source
        : findNodeById(graphData.nodes, link.source);
      var t = (typeof link.target === "object")
        ? link.target
        : findNodeById(graphData.nodes, link.target);

      if (s === node || t === node) {
        highlightLinks.add(link);
        if (s) highlightNodes.add(s);
        if (t) highlightNodes.add(t);
      }
    });

    showSidePanel(node, cubeContainer);
  }

  // Force a visual refresh
  updateHighlights();
}

/**
 * Helper: find a node by ID in the nodes array.
 */
function findNodeById(nodes, id) {
  for (var i = 0; i < nodes.length; i++) {
    if (nodes[i].id === id) return nodes[i];
  }
  return null;
}

// ---------------------------------------------------------------------------
// 5. Side panel on selection
// ---------------------------------------------------------------------------

/**
 * Inject or update a side panel inside the cube container showing selected
 * task details.
 *
 * @param {object}  node          - The selected node
 * @param {Element} cubeContainer - Parent DOM element for the panel
 */
function showSidePanel(node, cubeContainer) {
  // Remove existing panel first
  removeSidePanel(cubeContainer);

  var displayId = node.short_id || node.id;
  var statusColor = getLaneColor(node.status);

  var panel = document.createElement("div");
  panel.className = "cube-side-panel";
  panel.id = "cube-side-panel";

  var html = '';

  // Header with close button
  html += '<div class="cube-side-panel-header">';
  html += '<span class="cube-side-panel-id">' + esc(displayId) + '</span>';
  html += '<button class="cube-side-panel-close" title="Close">&times;</button>';
  html += '</div>';

  // Title
  html += '<div class="cube-side-panel-title">' + esc(node.title || "Untitled") + '</div>';

  // Metadata rows
  html += '<div class="cube-side-panel-meta">';

  // Status
  if (node.status) {
    html += '<div class="cube-side-panel-meta-row">';
    html += '<span class="cube-side-panel-meta-label">Status</span>';
    html += '<span>';
    html += '<span class="cube-tooltip-status-dot" style="background:' + esc(statusColor) + '"></span> ';
    html += esc(node.status);
    html += '</span>';
    html += '</div>';
  }

  // Priority
  if (node.priority) {
    html += '<div class="cube-side-panel-meta-row">';
    html += '<span class="cube-side-panel-meta-label">Priority</span>';
    html += '<span class="badge pri-' + esc(node.priority) + '">' + esc(node.priority) + '</span>';
    html += '</div>';
  }

  // Type
  if (node.type) {
    html += '<div class="cube-side-panel-meta-row">';
    html += '<span class="cube-side-panel-meta-label">Type</span>';
    html += '<span class="badge type-badge">' + esc(node.type) + '</span>';
    html += '</div>';
  }

  // Assigned to
  if (node.assigned_to) {
    html += '<div class="cube-side-panel-meta-row">';
    html += '<span class="cube-side-panel-meta-label">Assigned</span>';
    html += '<span>' + esc(node.assigned_to) + '</span>';
    html += '</div>';
  }

  html += '</div>'; // end .cube-side-panel-meta

  // Open task button
  html += '<a class="cube-side-panel-open-btn" href="#/task/' + esc(node.id) + '">';
  html += 'Open task &rarr;';
  html += '</a>';

  panel.innerHTML = html;
  cubeContainer.appendChild(panel);

  // --- Event listeners ---

  // Close button deselects
  var closeBtn = panel.querySelector(".cube-side-panel-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      selectedNode = null;
      highlightNodes.clear();
      highlightLinks.clear();
      updateHighlights();
      removeSidePanel(cubeContainer);
    });
  }
}

/**
 * Remove the side panel if it exists.
 *
 * @param {Element} cubeContainer - Parent DOM element
 */
function removeSidePanel(cubeContainer) {
  var existing = document.getElementById("cube-side-panel");
  if (existing && existing.parentNode) {
    existing.parentNode.removeChild(existing);
  }
}

// ---------------------------------------------------------------------------
// 6. Deselect on background click
// ---------------------------------------------------------------------------

/**
 * Bind to graph via: graph.onBackgroundClick(cubeOnBackgroundClick);
 *
 * Clears selection and removes side panel.
 *
 * @param {Element} cubeContainer - Parent DOM element for the panel
 */
function cubeOnBackgroundClick(cubeContainer) {
  selectedNode = null;
  highlightNodes.clear();
  highlightLinks.clear();
  updateHighlights();
  removeSidePanel(cubeContainer);
}

// ---------------------------------------------------------------------------
// 7. updateHighlights — visual refresh based on selection state
// ---------------------------------------------------------------------------

/**
 * Call this after any change to highlightNodes / highlightLinks / selectedNode.
 * It re-binds the color and width accessors on the graph so force-graph
 * re-renders with the correct highlight/dim state.
 *
 * Expects `cubeGraph` to be the ForceGraph instance in the outer scope.
 *
 * @param {object} [graphInstance] - Optional override; if omitted, uses
 *                                   the global `cubeGraph` variable.
 */
function updateHighlights(graphInstance) {
  var graph = graphInstance || cubeGraph;
  if (!graph) return;

  graph
    .nodeColor(function(node) {
      // When nothing is highlighted, use normal status color
      if (highlightNodes.size === 0) return getLaneColor(node.status);
      // When there is a selection, highlight members, dim the rest
      return highlightNodes.has(node) ? getLaneColor(node.status) : "#e0e0e0";
    })
    .linkColor(function(link) {
      if (highlightLinks.size === 0) return getRelColor(link.type);
      return highlightLinks.has(link) ? getRelColor(link.type) : "#e0e0e0";
    })
    .linkWidth(function(link) {
      return highlightLinks.has(link) ? 3 : 1;
    })
    .nodeRelSize(function(node) {
      // Make the selected node slightly larger
      if (selectedNode === node) return 8;
      if (highlightNodes.has(node) && highlightNodes.size > 0) return 6;
      return 4;
    });
}

// ---------------------------------------------------------------------------
// Hover glow effect (optional enhancement)
// ---------------------------------------------------------------------------

var hoverNode = null;

/**
 * Bind to graph via: graph.onNodeHover(cubeOnNodeHover);
 *
 * Applies a subtle cursor change on hover and tracks the hovered node.
 *
 * @param {object}  node         - The node being hovered (null on leave)
 * @param {Element} cubeContainer - DOM element wrapping the graph canvas
 */
function cubeOnNodeHover(node, cubeContainer) {
  hoverNode = node || null;

  // Change cursor to pointer when over a node
  if (cubeContainer) {
    cubeContainer.style.cursor = node ? "pointer" : "default";
  }
}

// ---------------------------------------------------------------------------
// Integration — wire everything up on a ForceGraph instance
// ---------------------------------------------------------------------------

/**
 * Call this once after creating the ForceGraph instance to bind all
 * tooltip and interaction callbacks.
 *
 * Usage:
 *   var graphData = { nodes: [...], links: [...] };
 *   var cubeGraph = ForceGraph()(cubeContainer)
 *     .graphData(graphData)
 *     .nodeId("id")
 *     ...;
 *
 *   initCubeInteractions(cubeGraph, graphData, cubeContainer);
 *
 * @param {object}  graph          - The ForceGraph instance
 * @param {object}  graphData      - { nodes: [], links: [] }
 * @param {Element} cubeContainer  - DOM element wrapping the graph
 */
function initCubeInteractions(graph, graphData, cubeContainer) {
  // Store reference for updateHighlights
  if (typeof cubeGraph === "undefined") {
    // Declare in the outer scope so updateHighlights can find it
    window.cubeGraph = graph;
  } else {
    cubeGraph = graph;
  }

  // Tooltips
  graph.nodeLabel(cubeNodeLabel);
  graph.linkLabel(cubeLinkLabel);

  // Hover
  graph.onNodeHover(function(node) {
    cubeOnNodeHover(node, cubeContainer);
  });

  // Click (select + double-click to navigate)
  graph.onNodeClick(function(node) {
    cubeOnNodeClick(node, graphData, cubeContainer);
  });

  // Background click (deselect)
  graph.onBackgroundClick(function() {
    cubeOnBackgroundClick(cubeContainer);
  });

  // Initial highlight state (no selection)
  updateHighlights(graph);
}

// ---------------------------------------------------------------------------
// CSS (reference — add to the <style> block in index.html)
// ---------------------------------------------------------------------------

/*
 * Below is the CSS to accompany this JS. Paste into the dashboard's
 * <style> block or a separate stylesheet.
 *
 * The tooltip classes (.cube-tooltip*) are rendered inside force-graph's
 * built-in tooltip container, which is a <div> appended to <body> with
 * class "graph-tooltip". force-graph sets pointer-events:none and
 * position:absolute on it. We style the inner content only.
 *
 * The side panel (.cube-side-panel*) is appended inside the cube
 * container div, positioned absolutely to the right.
 *

.cube-tooltip {
  font-family: var(--font-body);
  font-size: 13px;
  line-height: 1.4;
  max-width: 280px;
  padding: 8px 10px;
  color: var(--text-primary);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card-hover);
}
.cube-tooltip-id {
  font-weight: 700;
  font-size: 12px;
  color: var(--accent-primary);
  margin-bottom: 4px;
  font-family: var(--font-mono);
}
.cube-tooltip-row {
  margin-bottom: 2px;
}
.cube-tooltip-status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  vertical-align: middle;
  margin-right: 4px;
}
.cube-tooltip-hint {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-muted);
  border-top: 1px solid var(--border-subtle);
  padding-top: 4px;
}

/* --- Side panel (appended inside cube container) --- */

.cube-side-panel {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 260px;
  max-height: calc(100% - 24px);
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card-hover);
  padding: 14px 16px;
  z-index: 20;
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--text-primary);
}
.cube-side-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.cube-side-panel-id {
  font-weight: 700;
  font-size: 12px;
  color: var(--accent-primary);
  font-family: var(--font-mono);
}
.cube-side-panel-close {
  background: none;
  border: none;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  color: var(--text-muted);
  padding: 0 2px;
}
.cube-side-panel-close:hover {
  color: var(--text-primary);
}
.cube-side-panel-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  line-height: 1.3;
}
.cube-side-panel-meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 14px;
}
.cube-side-panel-meta-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.cube-side-panel-meta-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .03em;
  color: var(--text-muted);
}
.cube-side-panel-open-btn {
  display: block;
  text-align: center;
  padding: 8px 0;
  background: var(--accent-primary);
  color: var(--text-on-accent);
  border-radius: var(--radius-sm);
  text-decoration: none;
  font-size: 13px;
  font-weight: 600;
  transition: background .15s;
}
.cube-side-panel-open-btn:hover {
  background: var(--accent-hover);
}

 *
 * End of CSS reference.
 */
