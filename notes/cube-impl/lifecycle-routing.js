// =============================================================================
// Cube View — Lifecycle & Routing Integration
// =============================================================================
//
// This file contains code snippets for integrating the Cube (3D graph) view
// into index.html. Each section is labeled with WHERE it should be inserted.
//
// Dependencies (assumed to exist from core-rendering.js):
//   - initCubeGraph(data)     — creates the ForceGraph3D instance, assigns to cubeGraph
//   - updateCubeData(data)    — calls cubeGraph.graphData(...) for incremental updates
//
// =============================================================================


// =============================================================================
// SECTION 1: Nav Tab HTML
// INSERT IN: <div class="nav">, after the Stats tab (line 2694)
// =============================================================================
//
// <span class="nav-tab" data-view="cube" onclick="location.hash='#/cube'">Cube</span>
//
// Full context — the nav bar should read:
//   <span class="nav-tab" data-view="board">Board</span>
//   <span class="nav-tab" data-view="list">List</span>
//   <span class="nav-tab" data-view="activity">Activity</span>
//   <span class="nav-tab" data-view="stats">Stats</span>
//   <span class="nav-tab" data-view="cube" onclick="location.hash='#/cube'">Cube</span>
//   <div class="nav-right">


// =============================================================================
// SECTION 2: Global State
// INSERT IN: "// --- State ---" block, after line 3340 (after draggedFromStatus)
// =============================================================================

var cubeGraph = null;              // ForceGraph3D instance (set by initCubeGraph)
var cubeRenderGeneration = 0;      // Monotonic counter — incremented on every render/cleanup
var cubeCurrentRevision = null;    // Tracks /api/graph revision for cheap change detection


// =============================================================================
// SECTION 3: Cleanup on Route Change
// INSERT IN: route() function, as the FIRST lines inside the function body
//            (line 3421, before the existing `var h = ...`)
// =============================================================================
//
// When navigating away from cube to a different main view, tear down the
// 3D scene to free GPU/memory. We check `view !== "task"` because task
// detail is an overlay — the cube should stay alive underneath if the user
// clicks a task node and we route to #/task/<id>.

// --- Inside route(hash), at the very top: ---

  // Clean up cube when navigating away to a different main view
  var h = (hash || "/board").replace(/^#?\/?/, "/");
  var parts = h.split("/").filter(Boolean);
  var view = parts[0] || "board";

  if (currentView === "cube" && view !== "cube" && view !== "task") {
    cleanupCube();
  }

  currentView = view === "task" ? currentView : view;
  updateTabs();

  if (view === "board") renderBoard();
  else if (view === "list") renderList();
  else if (view === "activity") await renderActivity();
  else if (view === "stats") await renderStats();
  else if (view === "cube") await renderCube();
  else if (view === "task" && parts[1]) await renderDetail(parts[1]);
  else renderBoard();

// NOTE: This REPLACES the existing route() function body (lines 3421-3432).
// The only additions are:
//   1. The cleanup guard at the top (if currentView === "cube" ...)
//   2. The `else if (view === "cube")` dispatch line
// Everything else is unchanged.


// =============================================================================
// SECTION 4: renderCube()
// INSERT IN: Before "// --- Routing ---" (around line 3419), or after the
//            route() function. Placing it near the other render functions is
//            fine — just keep it in the same script scope.
// =============================================================================

async function renderCube() {
  // Increment generation to invalidate any in-flight renders from previous calls.
  // If the user rapidly toggles views, only the most recent render wins.
  cubeRenderGeneration++;
  var myGeneration = cubeRenderGeneration;

  var app = document.getElementById("app");
  app.innerHTML = '<div class="cube-container" id="cube-mount">'
    + '<div class="loading"><div class="spinner"></div></div>'
    + '</div>';

  try {
    var data = await api("/api/graph");

    // Stale check: if user navigated away during the fetch, bail out.
    // This prevents a delayed response from clobbering a different view.
    if (myGeneration !== cubeRenderGeneration) return;

    if (!data || !data.nodes || data.nodes.length === 0) {
      app.innerHTML = '<div class="cube-container">'
        + '<div class="empty"><p>No tasks to visualize.</p></div>'
        + '</div>';
      return;
    }

    // Track the current revision for auto-refresh comparison
    cubeCurrentRevision = data.revision || null;

    // Hand off to the core rendering module which creates the ForceGraph3D
    // instance and assigns it to the global `cubeGraph`.
    initCubeGraph(data);

  } catch (e) {
    if (myGeneration !== cubeRenderGeneration) return;
    app.innerHTML = '<div class="cube-container">'
      + '<div class="error-box"><p>Failed to load graph data.</p>'
      + '<button class="btn" onclick="location.hash=\'#/cube\'">Retry</button>'
      + '</div></div>';
  }
}


// =============================================================================
// SECTION 5: cleanupCube()
// INSERT IN: Immediately after renderCube() (same location)
// =============================================================================

function cleanupCube() {
  // Tear down the 3D scene to release WebGL context, GPU memory, and
  // animation frames. ForceGraph3D exposes _destructor() for this purpose.
  if (cubeGraph) {
    try {
      cubeGraph._destructor();
    } catch (e) {
      // Swallow — graph may already be partially torn down
    }
    cubeGraph = null;
  }

  // Bump generation to cancel any in-flight renderCube() or auto-refresh
  // fetch that hasn't resolved yet.
  cubeRenderGeneration++;

  // Clear revision so the next render starts fresh
  cubeCurrentRevision = null;
}


// =============================================================================
// SECTION 6: Auto-Refresh Integration
// INSERT IN: Inside the setInterval callback in startAutoRefresh(),
//            AFTER the existing view whitelist check (line 4839).
//
// The existing check is:
//   if (currentView !== "board" && currentView !== "list" && currentView !== "activity" && currentView !== "stats") return;
//
// REPLACE that line with:
//   if (currentView !== "board" && currentView !== "list" && currentView !== "activity" && currentView !== "stats" && currentView !== "cube") return;
//
// Then, BEFORE the existing `var newTasks = await api("/api/tasks");` line,
// add the cube-specific refresh branch that returns early (cube handles its
// own data independently from the tasks/config refresh cycle).
// =============================================================================

// --- Inside the setInterval callback, after the whitelist check: ---

    // Cube view: revision-based refresh (independent from tasks/config cycle)
    if (currentView === "cube") {
      try {
        var cubeGen = cubeRenderGeneration;
        var graphData = await api("/api/graph");

        // Stale check: user may have navigated away during fetch
        if (cubeGen !== cubeRenderGeneration) return;
        if (currentView !== "cube") return;

        // Only update if the revision has actually changed
        if (graphData.revision && graphData.revision !== cubeCurrentRevision) {
          cubeCurrentRevision = graphData.revision;
          updateCubeData(graphData);
        }
      } catch (e) {
        // Silently ignore — server may be restarting
      }
      return; // Don't fall through to the tasks/config refresh below
    }

// --- Then the existing tasks/config refresh code continues unchanged: ---
//     var newTasks = await api("/api/tasks");
//     var newConfig = await api("/api/config");
//     ...


// =============================================================================
// SECTION 7: Complete route() function (for easy copy-paste)
// This is the FULL replacement for the route() function (lines 3420-3433).
// =============================================================================

async function route(hash) {
  var h = (hash || "/board").replace(/^#?\/?/, "/");
  var parts = h.split("/").filter(Boolean);
  var view = parts[0] || "board";

  // Clean up cube's 3D scene when navigating to a different main view.
  // Task detail (#/task/<id>) is treated as an overlay — don't destroy the
  // cube if the user clicked a node to inspect a task.
  if (currentView === "cube" && view !== "cube" && view !== "task") {
    cleanupCube();
  }

  currentView = view === "task" ? currentView : view;
  updateTabs();

  if (view === "board") renderBoard();
  else if (view === "list") renderList();
  else if (view === "activity") await renderActivity();
  else if (view === "stats") await renderStats();
  else if (view === "cube") await renderCube();
  else if (view === "task" && parts[1]) await renderDetail(parts[1]);
  else renderBoard();
}


// =============================================================================
// SECTION 8: Complete startAutoRefresh() (for easy copy-paste)
// This is the FULL replacement for startAutoRefresh() (lines 4835-4866).
// =============================================================================

function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshInterval = setInterval(async function() {
    // Only auto-refresh on views that support it
    if (currentView !== "board" && currentView !== "list" && currentView !== "activity" && currentView !== "stats" && currentView !== "cube") return;

    // Cube view: revision-based refresh — independent data path
    if (currentView === "cube") {
      try {
        var cubeGen = cubeRenderGeneration;
        var graphData = await api("/api/graph");

        // Stale: user navigated away during fetch
        if (cubeGen !== cubeRenderGeneration) return;
        if (currentView !== "cube") return;

        // Only push new data if revision changed
        if (graphData.revision && graphData.revision !== cubeCurrentRevision) {
          cubeCurrentRevision = graphData.revision;
          updateCubeData(graphData);
        }
      } catch (e) {
        // Silently ignore — server may be restarting
      }
      return;
    }

    // Board/List/Activity/Stats: JSON-stringify comparison on tasks + config
    try {
      var newTasks = await api("/api/tasks");
      var newConfig = await api("/api/config");
      // Only re-render if data actually changed
      var tasksChanged = JSON.stringify(newTasks) !== JSON.stringify(tasks);
      var configChanged = JSON.stringify(newConfig) !== JSON.stringify(config);
      if (tasksChanged || configChanged) {
        tasks = newTasks;
        config = newConfig;
        updateStatsBadges();
        if (configChanged) {
          var dca = (config && config.dashboard) || {};
          if (dca.voice && VOICES[dca.voice]) currentVoice = dca.voice;
          applyTheme(getTheme());
          populateThemeSelector();
          populateVoiceSelector();
          updateStaticVoiceStrings();
          populateLaneColorSettings();
          loadBackgroundSetting();
        }
        await route(location.hash.slice(1));
      }
    } catch(e) {
      // Silently ignore refresh errors — server may be restarting
    }
  }, AUTO_REFRESH_MS);
}
