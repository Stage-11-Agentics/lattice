/* ============================================================================
 * cube-v2.js — Lattice Cube v2: DAG-Driven 2.5D Task Visualization
 *
 * Layout: X = topological depth in the DAG, Y = force-directed spread
 * Rendering: flat 2D — orthographic top-down (no tilt/perspective)
 * Color: Node color = task status (animated transitions)
 * Camera: Map-style navigation (pan/zoom, no tilt/orbit)
 *
 * Dependencies (global):
 *   THREE  — Three.js from CDN
 *   d3     — d3 from CDN (uses d3.forceSimulation 2D, not 3D)
 *
 * Exposes globals:
 *   renderCubeV2()          — mount and render the v2 view
 *   updateCubeV2Data(data)  — incremental data update
 *   cleanupCubeV2()         — teardown everything
 *
 * Dashboard globals used (via window._lattice):
 *   config, getLaneColor, esc, api, showToast, openDetailPanel
 * ========================================================================= */

/* --------------------------------------------------------------------------
 * 0. Dashboard Integration
 * ----------------------------------------------------------------------- */

var _cv2L = (typeof window !== 'undefined' && window._lattice) || {};
var _cv2Api = _cv2L.api || function() { return Promise.reject(new Error('api unavailable')); };
var _cv2Esc = _cv2L.esc || function(s) { return String(s); };
var _cv2ShowToast = _cv2L.showToast || function() {};
var _cv2GetStatusDisplayName = _cv2L.getStatusDisplayName || function(s) { return (s || '').replace(/_/g, ' '); };
function _cv2OpenDetailPanel(id) {
  var fn = (window._lattice && window._lattice.openDetailPanel) || function() {};
  fn(id);
}
function _cv2CloseDetailPanel() {
  var fn = (window._lattice && window._lattice.closeDetailPanel) || function() {};
  fn();
}

/* --------------------------------------------------------------------------
 * 1. Constants
 * ----------------------------------------------------------------------- */

/* Status palette — ordered along the lifecycle so hue tracks progress.
   No two statuses share a gray (backlog vs cancelled was the old collision),
   done reads green ("complete"), in_progress reads as active cyan, and the
   previously-missing in_validation / pr_open get their own hues instead of
   falling through to default gray. */
var CV2_STATUS_COLORS = {
  backlog: '#64748b',        // slate — captured, not started
  in_planning: '#818cf8',    // indigo
  planned: '#3b82f6',        // blue — ready to implement
  in_progress: '#22d3ee',    // bright cyan — active, pops
  review: '#fbbf24',         // amber
  in_validation: '#2dd4bf',  // teal
  pr_open: '#c084fc',        // violet — awaiting merge
  done: '#22c55e',           // green — complete
  blocked: '#ef4444',        // red
  needs_human: '#f59e0b',    // orange (also the orthogonal-flag override)
  cancelled: '#3f3f46'       // dark desaturated — dead (also rendered smaller/dimmer)
};

/* needs_human is an orthogonal flag: when set, override status color with amber. */
var CV2_NEEDS_HUMAN_COLOR = '#f59e0b';
function _cv2NodeColor(node) {
  if (node && node.needs_human) return CV2_NEEDS_HUMAN_COLOR;
  return CV2_STATUS_COLORS[node && node.status] || '#64748b';
}

/* Gold "ready now" halo (Stage 11 accent). A node earns it only when it is
   genuinely actionable: unstarted AND every blocking prerequisite is done. */
var CV2_READY_COLOR = '#f5c518';

/* Visual hierarchy: finished/dead work recedes so live + ready work pops.
   { scale, bright } multipliers applied as a baseline (selection dimming
   multiplies on top of this). Statuses not listed render at full weight. */
var CV2_DEEMPHASIS = {
  done: { scale: 0.82, bright: 0.82 },
  cancelled: { scale: 0.64, bright: 0.55 }
};

/* Readiness classification sets. */
var CV2_UNSTARTED = { backlog: 1, in_planning: 1, planned: 1 };
var CV2_COMPLETE = { done: 1, cancelled: 1 };

/* Blocking adjacency: which tasks must complete before a given task can start.
   Only depends_on and blocks carry "must-finish-first" meaning; subtask_of /
   related_to / spawned_by do not. Returns { upstream, downstream } id->ids maps
   where upstream[id] = prerequisites of id, downstream[id] = things id unblocks. */
function _cv2BuildBlockingAdjacency(nodes, links) {
  var nodeIds = new Set();
  for (var i = 0; i < nodes.length; i++) nodeIds.add(nodes[i].id);
  var upstream = {}, downstream = {};
  for (var i = 0; i < nodes.length; i++) { upstream[nodes[i].id] = []; downstream[nodes[i].id] = []; }
  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var src = typeof link.source === 'object' ? link.source.id : link.source;
    var tgt = typeof link.target === 'object' ? link.target.id : link.target;
    if (!nodeIds.has(src) || !nodeIds.has(tgt)) continue;
    if (link.type === 'depends_on') {
      // src depends on tgt → tgt is a prerequisite of src
      upstream[src].push(tgt); downstream[tgt].push(src);
    } else if (link.type === 'blocks') {
      // src blocks tgt → src is a prerequisite of tgt
      upstream[tgt].push(src); downstream[src].push(tgt);
    }
  }
  return { upstream: upstream, downstream: downstream };
}

/* The set of ids genuinely startable now: unstarted, not flagged for a human,
   and every blocking prerequisite already done/cancelled. */
function _cv2ComputeReady(nodes, blockUp) {
  var statusById = {};
  for (var i = 0; i < nodes.length; i++) statusById[nodes[i].id] = nodes[i].status;
  var ready = new Set();
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    if (!CV2_UNSTARTED[n.status]) continue;
    if (n.needs_human) continue; // already blocked on a human decision
    var ups = blockUp[n.id] || [];
    var clear = true;
    for (var j = 0; j < ups.length; j++) {
      if (!CV2_COMPLETE[statusById[ups[j]]]) { clear = false; break; }
    }
    if (clear) ready.add(n.id);
  }
  return ready;
}

/* Transitive closure from startId following an adjacency map (excludes startId). */
function _cv2ReachSet(startId, adj) {
  var seen = new Set();
  var stack = [startId];
  while (stack.length) {
    var id = stack.pop();
    var nbrs = adj[id] || [];
    for (var k = 0; k < nbrs.length; k++) {
      if (!seen.has(nbrs[k])) { seen.add(nbrs[k]); stack.push(nbrs[k]); }
    }
  }
  return seen;
}

/* True when the node SET or link SET differs — i.e. a task/relationship was
   added or removed. A pure status/metadata edit returns false, which is what
   keeps the layout from reflowing on unrelated updates. */
function _cv2TopologyChanged(oldNodes, oldLinks, newNodes, newLinks) {
  if (oldNodes.length !== newNodes.length || oldLinks.length !== newLinks.length) return true;
  var seen = {};
  for (var i = 0; i < oldNodes.length; i++) seen[oldNodes[i].id] = 1;
  for (var i = 0; i < newNodes.length; i++) { if (!seen[newNodes[i].id]) return true; }
  var lkey = function(l) {
    var s = typeof l.source === 'object' ? l.source.id : l.source;
    var t = typeof l.target === 'object' ? l.target.id : l.target;
    return s + '|' + t + '|' + l.type;
  };
  var lseen = {};
  for (var i = 0; i < oldLinks.length; i++) lseen[lkey(oldLinks[i])] = 1;
  for (var i = 0; i < newLinks.length; i++) { if (!lseen[lkey(newLinks[i])]) return true; }
  return false;
}

var CV2_EDGE_COLORS = {
  blocks: '#ef4444', depends_on: '#f97316', subtask_of: '#3b82f6',
  related_to: '#6b7280', spawned_by: '#8b5cf6'
};

var CV2_PRIORITY_SCALE = { critical: 1.8, high: 1.3, medium: 1.0, low: 0.7 };
var CV2_PRIORITY_BIAS = { critical: 80, high: 30, medium: 0, low: -40 };

var CV2_COLUMN_SPACING = 100;
var CV2_NODE_BASE_RADIUS = 10;
var CV2_TUBE_DISTANCE_THRESHOLD = 150;
var CV2_TUBE_RADIUS = 0.8;
var CV2_TUBE_SEGMENTS = 8;

/* Camera (flat 2D, orthographic top-down). viewHalfH is the half-height of the
   visible world in node-layout units; zoom scales it. No tilt — the graph reads
   as a flat map (X = DAG depth, Y = spread). */
var CV2_VIEW_DEFAULT_HALF_H = 240;
var CV2_VIEW_MIN_HALF_H = 40;
var CV2_VIEW_MAX_HALF_H = 1400;
var CV2_CAM_Z = 1000;        // camera height on +Z; ortho size is Z-independent
var CV2_PAN_SPEED = 0.8;
var CV2_ZOOM_SPEED = 0.05;

/* --------------------------------------------------------------------------
 * 2. Module State
 * ----------------------------------------------------------------------- */

var _cv2 = {
  scene: null,
  camera: null,
  renderer: null,
  animFrameId: null,
  generation: 0,
  simulation: null,
  instancedMesh: null,
  nodeData: [],
  linkData: [],
  // Edge rendering
  edgeLines: null,
  edgeTubes: [],
  flowPoints: null,
  // Camera state (orthographic 2D)
  camTarget: null,   // THREE.Vector3 — center of view on the Z=0 plane
  viewHalfH: CV2_VIEW_DEFAULT_HALF_H,  // half the visible world-height; zoom adjusts
  // Interaction state
  raycaster: null,
  mouse: null,
  hoveredNode: null,
  selectedNode: null,
  // Search
  searchActive: false,
  searchMatches: new Set(),
  // Topology
  nodeDepths: {},
  // Blocking graph (depends_on + blocks) — drives readiness + chain highlight
  blockUp: {},
  blockDown: {},
  readySet: null,
  readyHalo: null,        // InstancedMesh: gold "ready now" glow behind nodes
  // Selection chain (transitive, both directions)
  chainUp: null,          // Set of prerequisite ids of the selected node
  chainDown: null,        // Set of dependent ids of the selected node
  _depchainEl: null,      // bottom dependency-rail element
  _edgeBaseColors: null,  // Float32Array snapshot for selection edge dimming
  // Color animation
  nodeTargetColors: [],   // target RGB per node
  nodeCurrentColors: [],  // current (animating) RGB per node
  // Pan drag state
  _dragState: null,
  // Key state for WASD
  _keysDown: new Set(),
  // Handlers (for cleanup)
  _mouseDownHandler: null,
  _mouseMoveHandler: null,
  _mouseUpHandler: null,
  _wheelHandler: null,
  _keyDownHandler: null,
  _keyUpHandler: null,
  _resizeHandler: null,
  _clickHandler: null,
  _dblClickHandler: null,
  // Tooltip
  _tooltipEl: null,
  // Node labels (HTML overlay)
  _labelContainer: null,
  _labelEls: [],
  // Click dedup
  _lastClickHandledByMouseUp: false,
  // Revision
  currentRevision: null,
  // Internal
  _flowT: null,
  _frameCount: 0
};

/* --------------------------------------------------------------------------
 * 3. Topological Depth Computation
 * ----------------------------------------------------------------------- */

function _cv2ComputeDepths(nodes, links) {
  // Build adjacency: for depends_on and subtask_of, the dependent/subtask is downstream
  var nodeIds = new Set();
  for (var i = 0; i < nodes.length; i++) nodeIds.add(nodes[i].id);

  // adjacency[nodeId] = [list of downstream node ids]
  var downstream = {};
  var upstream = {};
  for (var i = 0; i < nodes.length; i++) {
    downstream[nodes[i].id] = [];
    upstream[nodes[i].id] = [];
  }

  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var src = typeof link.source === 'object' ? link.source.id : link.source;
    var tgt = typeof link.target === 'object' ? link.target.id : link.target;
    if (!nodeIds.has(src) || !nodeIds.has(tgt)) continue;

    // For depends_on: source depends on target, so target is upstream of source
    // For subtask_of: source is subtask of target, so target is upstream
    // For blocks: source blocks target, so source is upstream
    if (link.type === 'depends_on' || link.type === 'subtask_of') {
      downstream[tgt] = downstream[tgt] || [];
      downstream[tgt].push(src);
      upstream[src] = upstream[src] || [];
      upstream[src].push(tgt);
    } else if (link.type === 'blocks') {
      downstream[src] = downstream[src] || [];
      downstream[src].push(tgt);
      upstream[tgt] = upstream[tgt] || [];
      upstream[tgt].push(src);
    }
    // related_to and spawned_by don't imply flow direction — ignore for depth
  }

  // Find roots (no upstream)
  var depths = {};
  var roots = [];
  for (var i = 0; i < nodes.length; i++) {
    var id = nodes[i].id;
    if (!upstream[id] || upstream[id].length === 0) {
      roots.push(id);
    }
  }

  // BFS to compute longest path from any root (topological depth)
  // Use iterative approach to handle cycles safely
  for (var i = 0; i < nodes.length; i++) depths[nodes[i].id] = 0;

  var changed = true;
  var iterations = 0;
  var maxIterations = nodes.length + 1; // safety valve for cycles
  while (changed && iterations < maxIterations) {
    changed = false;
    iterations++;
    for (var id in downstream) {
      var children = downstream[id];
      for (var j = 0; j < children.length; j++) {
        var childDepth = depths[id] + 1;
        if (childDepth > depths[children[j]]) {
          depths[children[j]] = childDepth;
          changed = true;
        }
      }
    }
  }

  if (iterations >= maxIterations) {
    console.warn('Cube v2: cycle detected in task graph, depth computation may be approximate');
  }

  return depths;
}

/* --------------------------------------------------------------------------
 * 4. Scene Initialization
 * ----------------------------------------------------------------------- */

function _cv2InitScene() {
  var container = document.getElementById('cv2-container');
  if (!container) return;

  var w = container.clientWidth;
  var h = container.clientHeight;

  // Scene (no fog — it was a depth cue, meaningless in flat 2D)
  _cv2.scene = new THREE.Scene();
  _cv2.scene.background = new THREE.Color(0x0a0a0a);

  // Camera — orthographic, looking straight down +Z at the Z=0 plane (flat 2D)
  _cv2.camera = new THREE.OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, 0.1, 5000);
  _cv2.camTarget = new THREE.Vector3(0, 0, 0);
  _cv2.viewHalfH = CV2_VIEW_DEFAULT_HALF_H;
  _cv2UpdateCameraProjection();
  _cv2UpdateCameraPosition();

  // Lighting
  var ambient = new THREE.AmbientLight(0xffffff, 0.6);
  _cv2.scene.add(ambient);
  var directional = new THREE.DirectionalLight(0xffffff, 0.8);
  directional.position.set(-100, 200, 150);
  _cv2.scene.add(directional);

  // Renderer
  _cv2.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  _cv2.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  _cv2.renderer.setSize(w, h);
  container.appendChild(_cv2.renderer.domElement);

  // Raycaster
  _cv2.raycaster = new THREE.Raycaster();
  _cv2.raycaster.params.Points = { threshold: CV2_NODE_BASE_RADIUS * 2 };
  _cv2.mouse = new THREE.Vector2();
}

/* Recompute the orthographic frustum from viewHalfH and the container aspect.
 * Call on init, zoom, resize, and fit. */
function _cv2UpdateCameraProjection() {
  if (!_cv2.camera) return;
  var c = document.getElementById('cv2-container');
  var aspect = c && c.clientHeight ? (c.clientWidth / c.clientHeight) : 1.8;
  var halfH = _cv2.viewHalfH;
  var halfW = halfH * aspect;
  _cv2.camera.left = -halfW;
  _cv2.camera.right = halfW;
  _cv2.camera.top = halfH;
  _cv2.camera.bottom = -halfH;
  _cv2.camera.updateProjectionMatrix();
}

function _cv2UpdateCameraPosition() {
  if (!_cv2.camera || !_cv2.camTarget) return;
  // Straight-down orthographic view: camera directly over the target on +Z.
  _cv2.camera.position.set(_cv2.camTarget.x, _cv2.camTarget.y, CV2_CAM_Z);
  _cv2.camera.up.set(0, 1, 0);
  _cv2.camera.lookAt(_cv2.camTarget.x, _cv2.camTarget.y, 0);
}

/* --------------------------------------------------------------------------
 * 5. Force Simulation
 * ----------------------------------------------------------------------- */

function _cv2InitSimulation(nodes, links) {
  var depths = _cv2ComputeDepths(nodes, links);
  _cv2.nodeDepths = depths;

  // Set initial positions based on depth
  for (var i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    var depth = depths[node.id] || 0;
    node.x = depth * CV2_COLUMN_SPACING;
    node.y = (Math.random() - 0.5) * 300; // random spread
  }

  // Build simulation links — only include directional edges
  var simLinks = [];
  var nodeIdSet = new Set();
  for (var i = 0; i < nodes.length; i++) nodeIdSet.add(nodes[i].id);

  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var src = typeof link.source === 'object' ? link.source.id : link.source;
    var tgt = typeof link.target === 'object' ? link.target.id : link.target;
    if (nodeIdSet.has(src) && nodeIdSet.has(tgt)) {
      simLinks.push({ source: src, target: tgt, type: link.type });
    }
  }

  _cv2.simulation = d3.forceSimulation(nodes)
    .force('x', d3.forceX(function(d) {
      return (depths[d.id] || 0) * CV2_COLUMN_SPACING;
    }).strength(0.95))
    .force('y', d3.forceY(function(d) {
      return CV2_PRIORITY_BIAS[d.priority] || 0;
    }).strength(0.08))
    .force('charge', d3.forceManyBody().strength(-200).distanceMax(600))
    .force('link', d3.forceLink(simLinks).id(function(d) { return d.id; }).distance(80).strength(0.12))
    .force('collide', d3.forceCollide(function(d) {
      return CV2_NODE_BASE_RADIUS * (CV2_PRIORITY_SCALE[d.priority] || 1) * 2.8;
    }).strength(0.85))
    .alphaDecay(0.018)
    .velocityDecay(0.3);

  _cv2.nodeData = nodes;
  _cv2.linkData = links;

  // Blocking graph + readiness (independent of layout)
  var adj = _cv2BuildBlockingAdjacency(nodes, links);
  _cv2.blockUp = adj.upstream;
  _cv2.blockDown = adj.downstream;
  _cv2.readySet = _cv2ComputeReady(nodes, _cv2.blockUp);

  // Initialize color arrays
  _cv2.nodeTargetColors = [];
  _cv2.nodeCurrentColors = [];
  for (var i = 0; i < nodes.length; i++) {
    var c = new THREE.Color(_cv2NodeColor(nodes[i]));
    _cv2.nodeTargetColors.push({ r: c.r, g: c.g, b: c.b });
    _cv2.nodeCurrentColors.push({ r: c.r, g: c.g, b: c.b });
  }
}

/* --------------------------------------------------------------------------
 * 6. Node Rendering (InstancedMesh)
 * ----------------------------------------------------------------------- */

function _cv2CreateNodes(nodes) {
  if (_cv2.instancedMesh) {
    _cv2.scene.remove(_cv2.instancedMesh);
    _cv2.instancedMesh.geometry.dispose();
    _cv2.instancedMesh.material.dispose();
  }

  var count = nodes.length;
  if (count === 0) return;

  var geo = new THREE.SphereGeometry(CV2_NODE_BASE_RADIUS, 16, 12);
  var mat = new THREE.MeshPhongMaterial({
    color: 0xffffff,
    shininess: 60,
    specular: new THREE.Color(0x333333)
  });

  var mesh = new THREE.InstancedMesh(geo, mat, count);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

  // Instance colors
  var colors = new Float32Array(count * 3);
  for (var i = 0; i < count; i++) {
    var c = _cv2.nodeCurrentColors[i] || { r: 0.42, g: 0.45, b: 0.5 };
    colors[i * 3] = c.r;
    colors[i * 3 + 1] = c.g;
    colors[i * 3 + 2] = c.b;
  }
  mesh.instanceColor = new THREE.InstancedBufferAttribute(colors, 3);

  _cv2.instancedMesh = mesh;
  _cv2.scene.add(mesh);
}

/* Gold additive glow rendered behind "ready now" nodes. One instance per node;
 * the animation loop scales non-ready instances to zero so only actionable
 * tasks show the halo. */
function _cv2CreateReadyHalos(nodes) {
  if (_cv2.readyHalo) {
    _cv2.scene.remove(_cv2.readyHalo);
    _cv2.readyHalo.geometry.dispose();
    _cv2.readyHalo.material.dispose();
    _cv2.readyHalo = null;
  }
  var count = nodes.length;
  if (count === 0) return;

  var geo = new THREE.SphereGeometry(CV2_NODE_BASE_RADIUS, 16, 12);
  var mat = new THREE.MeshBasicMaterial({
    color: new THREE.Color(CV2_READY_COLOR),
    transparent: true,
    opacity: 0.24,
    blending: THREE.AdditiveBlending,
    depthWrite: false
  });
  var mesh = new THREE.InstancedMesh(geo, mat, count);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.renderOrder = -1; // behind the opaque node spheres

  // Start hidden (scale 0); the animation loop reveals ready nodes.
  var m = new THREE.Matrix4();
  m.makeScale(0, 0, 0);
  for (var i = 0; i < count; i++) mesh.setMatrixAt(i, m);

  _cv2.readyHalo = mesh;
  _cv2.scene.add(mesh);
}

/* --------------------------------------------------------------------------
 * 7. Edge Rendering (Lines + Tubes + Flow Particles)
 * ----------------------------------------------------------------------- */

function _cv2CreateEdges(links, nodes) {
  var nodeMap = {};
  for (var i = 0; i < nodes.length; i++) nodeMap[nodes[i].id] = nodes[i];

  // --- Line edges (always present, visible at distance) ---
  var linePositions = [];
  var lineColors = [];
  var validLinks = [];

  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var src = typeof link.source === 'object' ? link.source : nodeMap[link.source];
    var tgt = typeof link.target === 'object' ? link.target : nodeMap[link.target];
    if (!src || !tgt) continue;
    validLinks.push({ source: src, target: tgt, type: link.type });

    // Initial positions (will be updated each frame)
    linePositions.push(src.x || 0, src.y || 0, 0);
    linePositions.push(tgt.x || 0, tgt.y || 0, 0);

    var ec = new THREE.Color(CV2_EDGE_COLORS[link.type] || '#6b7280');
    lineColors.push(ec.r, ec.g, ec.b);
    lineColors.push(ec.r, ec.g, ec.b);
  }

  if (linePositions.length > 0) {
    var lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(linePositions, 3));
    lineGeo.setAttribute('color', new THREE.Float32BufferAttribute(lineColors, 3));
    var lineMat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.6 });
    _cv2.edgeLines = new THREE.LineSegments(lineGeo, lineMat);
    _cv2.scene.add(_cv2.edgeLines);
    // Snapshot base colors so selection highlighting can dim off-chain edges
    // and restore them on deselect without recomputing the palette.
    _cv2._edgeBaseColors = new Float32Array(lineColors);
  } else {
    _cv2._edgeBaseColors = null;
  }

  // --- Flow particles ---
  if (validLinks.length > 0) {
    var particlesPerEdge = 2;
    var totalParticles = validLinks.length * particlesPerEdge;
    var pPositions = new Float32Array(totalParticles * 3);
    var pColors = new Float32Array(totalParticles * 3);

    _cv2._flowT = new Float32Array(totalParticles);
    for (var i = 0; i < totalParticles; i++) {
      _cv2._flowT[i] = Math.random(); // stagger initial positions
    }

    for (var i = 0; i < validLinks.length; i++) {
      var ec = new THREE.Color(CV2_EDGE_COLORS[validLinks[i].type] || '#6b7280');
      // Brighten particle color
      ec.r = Math.min(1, ec.r * 1.5);
      ec.g = Math.min(1, ec.g * 1.5);
      ec.b = Math.min(1, ec.b * 1.5);
      for (var p = 0; p < particlesPerEdge; p++) {
        var idx = (i * particlesPerEdge + p) * 3;
        pColors[idx] = ec.r;
        pColors[idx + 1] = ec.g;
        pColors[idx + 2] = ec.b;
      }
    }

    var pGeo = new THREE.BufferGeometry();
    pGeo.setAttribute('position', new THREE.Float32BufferAttribute(pPositions, 3));
    pGeo.setAttribute('color', new THREE.Float32BufferAttribute(pColors, 3));
    var pMat = new THREE.PointsMaterial({ size: 3, vertexColors: true, sizeAttenuation: true, transparent: true, opacity: 0.85 });
    _cv2.flowPoints = new THREE.Points(pGeo, pMat);
    _cv2.scene.add(_cv2.flowPoints);
  }

  // Store validated links for animation
  _cv2._validLinks = validLinks;

  // If a selection survived a data refresh, re-apply its edge highlight.
  if (_cv2.selectedNode) _cv2ApplyEdgeHighlight();
}

/* Modulate edge colors for the current selection: edges fully inside the
 * selected node's chain brighten, everything else dims hard. With no
 * selection, restore the snapshot base colors. */
function _cv2ApplyEdgeHighlight() {
  if (!_cv2.edgeLines || !_cv2._edgeBaseColors || !_cv2._validLinks) return;
  var attr = _cv2.edgeLines.geometry.attributes.color;
  var arr = attr.array;
  var base = _cv2._edgeBaseColors;
  var sel = _cv2.selectedNode;
  function onChain(id) {
    return id === sel
      || (_cv2.chainUp && _cv2.chainUp.has(id))
      || (_cv2.chainDown && _cv2.chainDown.has(id));
  }
  for (var i = 0; i < _cv2._validLinks.length; i++) {
    var vl = _cv2._validLinks[i];
    var m = 1.0;
    if (sel) m = (onChain(vl.source.id) && onChain(vl.target.id)) ? 1.35 : 0.10;
    var o = i * 6;
    for (var k = 0; k < 6; k++) {
      if (o + k >= base.length || o + k >= arr.length) break;
      var v = base[o + k] * m;
      arr[o + k] = v > 1 ? 1 : v;
    }
  }
  attr.needsUpdate = true;
}

/* --------------------------------------------------------------------------
 * 8. Animation Loop
 * ----------------------------------------------------------------------- */

function _cv2Animate() {
  var gen = _cv2.generation;
  _cv2.animFrameId = requestAnimationFrame(function() {
    if (_cv2.generation !== gen) return;
    _cv2Animate();
  });

  _cv2._frameCount++;
  var nodes = _cv2.nodeData;
  var count = nodes.length;

  if (count === 0 || !_cv2.instancedMesh) return;

  // --- Update node positions and colors ---
  var tmpMatrix = new THREE.Matrix4();
  var colorAttr = _cv2.instancedMesh.instanceColor;
  var haloMatrix = _cv2.readyHalo ? new THREE.Matrix4() : null;
  var sel = _cv2.selectedNode;

  for (var i = 0; i < count; i++) {
    var node = nodes[i];

    // Baseline de-emphasis: finished/dead work recedes so live work pops.
    var de = CV2_DEEMPHASIS[node.status];
    var emphScale = de ? de.scale : 1.0;
    var emphBright = de ? de.bright : 1.0;

    var scale = (CV2_PRIORITY_SCALE[node.priority] || 1.0) * emphScale;
    var x = node.x || 0;
    var y = node.y || 0;

    // Focus brightness: search match → selection chain → default.
    var bright = 1.0;
    var onChain = true; // for halo gating below
    if (_cv2.searchActive && !_cv2.searchMatches.has(node.id)) {
      bright = 0.12;
      scale *= 0.5;
      onChain = false;
    } else if (sel) {
      if (node.id === sel) {
        bright = 1.0;
        scale *= 1.25; // selected node grows
      } else if (_cv2.chainUp && _cv2.chainUp.has(node.id)) {
        bright = 1.0;   // prerequisites — strongest (what you asked about)
      } else if (_cv2.chainDown && _cv2.chainDown.has(node.id)) {
        bright = 0.82;  // dependents — present but secondary
      } else {
        bright = 0.12;  // off-chain — dim hard
        onChain = false;
      }
    }

    // Animate colors toward target
    var tc = _cv2.nodeTargetColors[i];
    var cc = _cv2.nodeCurrentColors[i];
    if (tc && cc) {
      cc.r += (tc.r - cc.r) * 0.08;
      cc.g += (tc.g - cc.g) * 0.08;
      cc.b += (tc.b - cc.b) * 0.08;
      var fb = bright * emphBright;
      colorAttr.setXYZ(i, cc.r * fb, cc.g * fb, cc.b * fb);
    }

    // Position (2.5D — nodes live on Z=0 plane)
    tmpMatrix.makeScale(scale, scale, scale);
    tmpMatrix.setPosition(x, y, 0);
    _cv2.instancedMesh.setMatrixAt(i, tmpMatrix);

    // Ready halo: visible only for genuinely-ready nodes that aren't dimmed out.
    if (haloMatrix) {
      var hs = 0;
      if (_cv2.readySet && _cv2.readySet.has(node.id) && onChain) {
        var pulse = 1.7 + 0.16 * Math.sin(_cv2._frameCount * 0.06 + i);
        hs = scale * pulse;
      }
      haloMatrix.makeScale(hs, hs, hs);
      haloMatrix.setPosition(x, y, 0);
      _cv2.readyHalo.setMatrixAt(i, haloMatrix);
    }
  }

  _cv2.instancedMesh.instanceMatrix.needsUpdate = true;
  colorAttr.needsUpdate = true;
  if (_cv2.readyHalo) _cv2.readyHalo.instanceMatrix.needsUpdate = true;

  // --- Update edge positions ---
  if (_cv2.edgeLines && _cv2._validLinks) {
    var positions = _cv2.edgeLines.geometry.attributes.position.array;
    for (var i = 0; i < _cv2._validLinks.length; i++) {
      var vl = _cv2._validLinks[i];
      var idx = i * 6;
      positions[idx] = vl.source.x || 0;
      positions[idx + 1] = vl.source.y || 0;
      positions[idx + 2] = 0;
      positions[idx + 3] = vl.target.x || 0;
      positions[idx + 4] = vl.target.y || 0;
      positions[idx + 5] = 0;
    }
    _cv2.edgeLines.geometry.attributes.position.needsUpdate = true;
  }

  // --- Update flow particles ---
  if (_cv2.flowPoints && _cv2._flowT && _cv2._validLinks) {
    var particlesPerEdge = 2;
    var pPos = _cv2.flowPoints.geometry.attributes.position.array;
    for (var i = 0; i < _cv2._validLinks.length; i++) {
      var vl = _cv2._validLinks[i];
      var sx = vl.source.x || 0, sy = vl.source.y || 0;
      var tx = vl.target.x || 0, ty = vl.target.y || 0;
      for (var p = 0; p < particlesPerEdge; p++) {
        var fi = i * particlesPerEdge + p;
        _cv2._flowT[fi] += 0.005;
        if (_cv2._flowT[fi] > 1) _cv2._flowT[fi] -= 1;
        var t = _cv2._flowT[fi];
        var idx = fi * 3;
        pPos[idx] = sx + (tx - sx) * t;
        pPos[idx + 1] = sy + (ty - sy) * t;
        pPos[idx + 2] = 0;
      }
    }
    _cv2.flowPoints.geometry.attributes.position.needsUpdate = true;
  }

  // --- WASD panning ---
  var panDelta = new THREE.Vector3();
  var panAmount = CV2_PAN_SPEED * _cv2.viewHalfH * 0.012;
  if (_cv2._keysDown.has('w') || _cv2._keysDown.has('arrowup')) panDelta.y += panAmount;
  if (_cv2._keysDown.has('s') || _cv2._keysDown.has('arrowdown')) panDelta.y -= panAmount;
  if (_cv2._keysDown.has('a') || _cv2._keysDown.has('arrowleft')) panDelta.x -= panAmount;
  if (_cv2._keysDown.has('d') || _cv2._keysDown.has('arrowright')) panDelta.x += panAmount;
  if (panDelta.length() > 0 && _cv2.camTarget) {
    _cv2.camTarget.add(panDelta);
    _cv2UpdateCameraPosition();
  }

  // --- Render ---
  if (_cv2.renderer && _cv2.scene && _cv2.camera) {
    _cv2.renderer.render(_cv2.scene, _cv2.camera);
  }

  // --- Update HTML labels (every 2nd frame for performance) ---
  if (_cv2._frameCount % 2 === 0) {
    _cv2UpdateLabels();
  }
}

/* --------------------------------------------------------------------------
 * 9. Camera Controls (Map-style)
 * ----------------------------------------------------------------------- */

function _cv2SetupControls() {
  var container = document.getElementById('cv2-container');
  if (!container) return;
  var canvas = _cv2.renderer ? _cv2.renderer.domElement : container;

  // --- Mouse drag for pan ---
  _cv2._mouseDownHandler = function(e) {
    if (e.button !== 0 && e.button !== 1) return; // left or middle
    _cv2._dragState = {
      startX: e.clientX,
      startY: e.clientY,
      startTargetX: _cv2.camTarget.x,
      startTargetY: _cv2.camTarget.y,
      shift: e.shiftKey,
      moved: false
    };
  };

  _cv2._mouseMoveHandler = function(e) {
    // Update mouse for raycasting
    var rect = canvas.getBoundingClientRect();
    _cv2.mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    _cv2.mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    if (_cv2._dragState) {
      var dx = e.clientX - _cv2._dragState.startX;
      var dy = e.clientY - _cv2._dragState.startY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) _cv2._dragState.moved = true;

      // Drag = pan. World units per pixel = (2 * viewHalfH) / canvas height,
      // so dragging tracks the cursor 1:1 regardless of zoom.
      var rectH = canvas.getBoundingClientRect().height || 1;
      var perPx = (_cv2.viewHalfH * 2) / rectH;
      _cv2.camTarget.x = _cv2._dragState.startTargetX - dx * perPx;
      _cv2.camTarget.y = _cv2._dragState.startTargetY + dy * perPx;
      _cv2UpdateCameraPosition();
    } else {
      // Hover detection (throttled)
      if (_cv2._frameCount % 3 === 0) _cv2UpdateHover(e);
    }
  };

  _cv2._mouseUpHandler = function(e) {
    if (_cv2._dragState && !_cv2._dragState.moved) {
      // This was a click, not a drag — handle selection
      _cv2._lastClickHandledByMouseUp = true;
      var hit = _cv2HitTest(e);
      if (hit !== null) {
        _cv2SelectNode(hit);
      } else {
        _cv2DeselectNode();
      }
    }
    _cv2._dragState = null;
  };

  // --- Scroll for zoom (orthographic: scale the visible world height) ---
  _cv2._wheelHandler = function(e) {
    e.preventDefault();
    var zoomFactor = 1 + (e.deltaY > 0 ? CV2_ZOOM_SPEED : -CV2_ZOOM_SPEED);
    _cv2.viewHalfH = Math.max(CV2_VIEW_MIN_HALF_H,
      Math.min(CV2_VIEW_MAX_HALF_H, _cv2.viewHalfH * zoomFactor));
    _cv2UpdateCameraProjection();
  };

  // --- WASD for pan ---
  _cv2._keyDownHandler = function(e) {
    var key = e.key.toLowerCase();

    // Search shortcut
    if (key === '/' && !_cv2.searchActive) {
      e.preventDefault();
      _cv2OpenSearch();
      return;
    }

    // Escape
    if (key === 'escape') {
      if (_cv2.searchActive) {
        _cv2CloseSearch();
      } else if (_cv2.selectedNode) {
        _cv2DeselectNode();
      }
      return;
    }

    // WASD (tracked for continuous movement in animation loop)
    if (['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].indexOf(key) >= 0) {
      // Don't capture when typing in search
      if (_cv2.searchActive) return;
      _cv2._keysDown.add(key);
    }
  };

  _cv2._keyUpHandler = function(e) {
    _cv2._keysDown.delete(e.key.toLowerCase());
  };

  // --- Click to select ---
  // Note: click fires after mouseup. mouseup handles selection for drag vs click
  // distinction. Keep click handler as fallback for accessibility / programmatic clicks.
  _cv2._clickHandler = function(e) {
    // Already handled by mouseup if _dragState was set
    if (_cv2._lastClickHandledByMouseUp) {
      _cv2._lastClickHandledByMouseUp = false;
      return;
    }
    var hit = _cv2HitTest(e);
    if (hit !== null) {
      _cv2SelectNode(hit);
    } else {
      _cv2DeselectNode();
    }
  };

  // --- Double-click to center ---
  _cv2._dblClickHandler = function(e) {
    var hit = _cv2HitTest(e);
    if (hit !== null && _cv2.nodeData[hit]) {
      var node = _cv2.nodeData[hit];
      _cv2SmoothPanTo(node.x || 0, node.y || 0);
    }
  };

  // --- Resize ---
  _cv2._resizeHandler = function() {
    var c = document.getElementById('cv2-container');
    if (!c || !_cv2.renderer || !_cv2.camera) return;
    var w = c.clientWidth, h = c.clientHeight;
    _cv2UpdateCameraProjection(); // recompute ortho frustum for the new aspect
    _cv2.renderer.setSize(w, h);
  };

  // Attach
  canvas.addEventListener('mousedown', _cv2._mouseDownHandler);
  window.addEventListener('mousemove', _cv2._mouseMoveHandler);
  window.addEventListener('mouseup', _cv2._mouseUpHandler);
  canvas.addEventListener('wheel', _cv2._wheelHandler, { passive: false });
  document.addEventListener('keydown', _cv2._keyDownHandler);
  document.addEventListener('keyup', _cv2._keyUpHandler);
  canvas.addEventListener('click', _cv2._clickHandler);
  canvas.addEventListener('dblclick', _cv2._dblClickHandler);
  canvas.addEventListener('contextmenu', function(e) { e.preventDefault(); });
  window.addEventListener('resize', _cv2._resizeHandler);
}

/* --------------------------------------------------------------------------
 * 10. Screen-Space Hit Testing & Hover
 * ----------------------------------------------------------------------- */

function _cv2HitTest(e) {
  // Project each node to screen space, find closest to mouse
  if (!_cv2.camera || !_cv2.renderer || !_cv2.nodeData.length) return null;
  var canvas = _cv2.renderer.domElement;
  var rect = canvas.getBoundingClientRect();
  var mouseX = e.clientX - rect.left;
  var mouseY = e.clientY - rect.top;
  var w = rect.width, h = rect.height;

  var bestIdx = null;
  var bestDist = Infinity;
  var hitThreshold = 20; // pixels

  var tmpVec = new THREE.Vector3();
  for (var i = 0; i < _cv2.nodeData.length; i++) {
    var node = _cv2.nodeData[i];
    tmpVec.set(node.x || 0, node.y || 0, 0);
    tmpVec.project(_cv2.camera);

    // Convert from NDC (-1..1) to screen pixels
    var sx = (tmpVec.x * 0.5 + 0.5) * w;
    var sy = (-tmpVec.y * 0.5 + 0.5) * h;

    var dx = sx - mouseX;
    var dy = sy - mouseY;
    var dist = Math.sqrt(dx * dx + dy * dy);

    // Scale threshold by node size
    var scale = CV2_PRIORITY_SCALE[node.priority] || 1.0;
    var threshold = hitThreshold * scale;

    if (dist < threshold && dist < bestDist) {
      bestDist = dist;
      bestIdx = i;
    }
  }
  return bestIdx;
}

function _cv2UpdateHover(e) {
  var hit = _cv2HitTest(e);
  var tooltip = _cv2._tooltipEl;
  if (!tooltip) return;

  if (hit !== null && _cv2.nodeData[hit]) {
    var node = _cv2.nodeData[hit];
    _cv2.hoveredNode = node.id;
    var statusName = _cv2GetStatusDisplayName(node.status || 'backlog');
    var statusColor = _cv2NodeColor(node);
    var priorityLabel = (node.priority || 'medium');
    var typeLabel = (node.type || 'task');
    var snippet = node.description_snippet || '';
    if (snippet.length > 120) snippet = snippet.substring(0, 120) + '...';

    tooltip.innerHTML = '<div class="cv2-tooltip-header">'
      + '<span class="cv2-tooltip-id">' + _cv2Esc(node.short_id || '') + '</span>'
      + '<span class="cv2-tooltip-status" style="color:' + statusColor + '">' + _cv2Esc(statusName) + '</span>'
      + '</div>'
      + '<div class="cv2-tooltip-title">' + _cv2Esc(node.title || 'Untitled') + '</div>'
      + '<div class="cv2-tooltip-meta">' + _cv2Esc(priorityLabel) + ' &middot; ' + _cv2Esc(typeLabel) + '</div>'
      + (snippet ? '<div class="cv2-tooltip-desc">' + _cv2Esc(snippet) + '</div>' : '');
    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX + 14) + 'px';
    tooltip.style.top = (e.clientY - 10) + 'px';

    // Change cursor
    if (_cv2.renderer) _cv2.renderer.domElement.style.cursor = 'pointer';
  } else {
    _cv2.hoveredNode = null;
    tooltip.style.display = 'none';
    if (_cv2.renderer) _cv2.renderer.domElement.style.cursor = 'grab';
  }
}

/* --------------------------------------------------------------------------
 * 11. Selection
 * ----------------------------------------------------------------------- */

function _cv2SelectNode(instanceId) {
  if (!_cv2.nodeData[instanceId]) return;
  var node = _cv2.nodeData[instanceId];
  _cv2.selectedNode = node.id;

  // Transitive chain through the selected node, both directions.
  _cv2.chainUp = _cv2ReachSet(node.id, _cv2.blockUp || {});
  _cv2.chainDown = _cv2ReachSet(node.id, _cv2.blockDown || {});

  _cv2ApplyEdgeHighlight();
  _cv2RenderDepChain(node);

  // Open detail panel
  _cv2OpenDetailPanel(node.id);
}

function _cv2DeselectNode() {
  _cv2.selectedNode = null;
  _cv2.chainUp = null;
  _cv2.chainDown = null;
  _cv2ApplyEdgeHighlight();
  _cv2HideDepChain();
  _cv2CloseDetailPanel();
}

/* --------------------------------------------------------------------------
 * 11b. Dependency Rail (bottom)
 * ----------------------------------------------------------------------- */

function _cv2HideDepChain() {
  if (_cv2._depchainEl) _cv2._depchainEl.classList.add('cv2-depchain-hidden');
}

/* Render the selected node's chain along the bottom:
 *   Depends on  [prereqs, root→near] →  [SELECTED]  → Unlocks  [dependents] */
function _cv2RenderDepChain(node) {
  var el = _cv2._depchainEl;
  if (!el) return;

  var byId = {}, idxById = {};
  for (var i = 0; i < _cv2.nodeData.length; i++) {
    byId[_cv2.nodeData[i].id] = _cv2.nodeData[i];
    idxById[_cv2.nodeData[i].id] = i;
  }
  var depthOf = function(id) { return _cv2.nodeDepths[id] || 0; };
  var byDepth = function(a, b) { return depthOf(a) - depthOf(b); };

  var ups = Array.from(_cv2.chainUp || []).filter(function(id) { return byId[id]; }).sort(byDepth);
  var downs = Array.from(_cv2.chainDown || []).filter(function(id) { return byId[id]; }).sort(byDepth);

  function chip(id, cls) {
    var n = byId[id];
    if (!n) return '';
    return '<button class="cv2-dep-chip ' + (cls || '') + '" data-idx="' + idxById[id] + '" title="' + _cv2Esc(n.title || '') + '">'
      + '<span class="cv2-dep-dot" style="background:' + _cv2NodeColor(n) + '"></span>'
      + _cv2Esc(n.short_id || '') + '</button>';
  }

  var html = '';
  if (ups.length) {
    html += '<span class="cv2-dep-label">Depends on</span>';
    for (var i = 0; i < ups.length; i++) html += chip(ups[i], 'cv2-dep-up');
    html += '<span class="cv2-dep-arrow">&rarr;</span>';
  }
  html += chip(node.id, 'cv2-dep-self');
  if (downs.length) {
    html += '<span class="cv2-dep-arrow">&rarr;</span><span class="cv2-dep-label">Unlocks</span>';
    for (var j = 0; j < downs.length; j++) html += chip(downs[j], 'cv2-dep-down');
  }
  if (!ups.length && !downs.length) {
    html += '<span class="cv2-dep-empty">no blocking dependencies</span>';
  }

  el.innerHTML = html;
  el.classList.remove('cv2-depchain-hidden');

  var chips = el.querySelectorAll('.cv2-dep-chip');
  for (var c = 0; c < chips.length; c++) {
    chips[c].addEventListener('click', function(ev) {
      ev.stopPropagation();
      var idx = parseInt(this.getAttribute('data-idx'), 10);
      if (isNaN(idx)) return;
      _cv2SelectNode(idx);
      var nn = _cv2.nodeData[idx];
      if (nn) _cv2SmoothPanTo(nn.x || 0, nn.y || 0);
    });
  }
}

/* --------------------------------------------------------------------------
 * 12. Smooth Pan
 * ----------------------------------------------------------------------- */

function _cv2SmoothPanTo(x, y) {
  var startX = _cv2.camTarget.x;
  var startY = _cv2.camTarget.y;
  var duration = 500;
  var startTime = performance.now();

  function step(now) {
    var t = Math.min(1, (now - startTime) / duration);
    // Ease in-out cubic
    t = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    _cv2.camTarget.x = startX + (x - startX) * t;
    _cv2.camTarget.y = startY + (y - startY) * t;
    _cv2UpdateCameraPosition();
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* --------------------------------------------------------------------------
 * 13. Search
 * ----------------------------------------------------------------------- */

function _cv2OpenSearch() {
  var el = document.getElementById('cv2-search');
  if (!el) return;
  el.classList.remove('cv2-search-hidden');
  var input = el.querySelector('.cv2-search-input');
  if (input) {
    input.value = '';
    input.focus();
  }
  _cv2.searchActive = true;
  _cv2.searchMatches = new Set();
}

function _cv2CloseSearch() {
  var el = document.getElementById('cv2-search');
  if (el) el.classList.add('cv2-search-hidden');
  _cv2.searchActive = false;
  _cv2.searchMatches = new Set();
}

function _cv2OnSearchInput(query) {
  if (!query || query.length === 0) {
    _cv2.searchMatches = new Set();
    // When search is open but query empty, show all
    for (var i = 0; i < _cv2.nodeData.length; i++) {
      _cv2.searchMatches.add(_cv2.nodeData[i].id);
    }
    return;
  }
  var q = query.toLowerCase();
  _cv2.searchMatches = new Set();
  for (var i = 0; i < _cv2.nodeData.length; i++) {
    var node = _cv2.nodeData[i];
    var text = ((node.short_id || '') + ' ' + (node.title || '') + ' ' + (node.status || '')).toLowerCase();
    if (text.indexOf(q) >= 0) {
      _cv2.searchMatches.add(node.id);
    }
  }
}

/* --------------------------------------------------------------------------
 * 14. HUD (Legend, Controls Hint, Search, Tooltip)
 * ----------------------------------------------------------------------- */

function _cv2CreateHUD() {
  var container = document.getElementById('cv2-container');
  if (!container) return;

  // --- Status color legend ---
  var legend = document.createElement('div');
  legend.className = 'cv2-legend';
  var legendHTML = '<div class="cv2-legend-title">Status</div>';
  var statuses = Object.keys(CV2_STATUS_COLORS);
  for (var i = 0; i < statuses.length; i++) {
    var s = statuses[i];
    legendHTML += '<div class="cv2-legend-item">'
      + '<span class="cv2-legend-dot" style="background:' + CV2_STATUS_COLORS[s] + '"></span>'
      + '<span>' + _cv2GetStatusDisplayName(s) + '</span></div>';
  }
  legendHTML += '<div class="cv2-legend-item cv2-legend-ready">'
    + '<span class="cv2-legend-ring"></span>'
    + '<span>ready now</span></div>';
  legendHTML += '<div class="cv2-legend-divider"></div>';
  legendHTML += '<div class="cv2-legend-title">Edges</div>';
  var edgeTypes = Object.keys(CV2_EDGE_COLORS);
  for (var i = 0; i < edgeTypes.length; i++) {
    var et = edgeTypes[i];
    legendHTML += '<div class="cv2-legend-item">'
      + '<span class="cv2-legend-line" style="background:' + CV2_EDGE_COLORS[et] + '"></span>'
      + '<span>' + et.replace(/_/g, ' ') + '</span></div>';
  }
  legend.innerHTML = legendHTML;
  container.appendChild(legend);

  // --- Controls hint ---
  var hint = document.createElement('div');
  hint.className = 'cv2-controls-hint';
  hint.id = 'cv2-controls-hint';
  hint.innerHTML = '<kbd>Drag</kbd> Pan &nbsp; <kbd>Scroll</kbd> Zoom<br>'
    + '<kbd>WASD</kbd> Navigate &nbsp; <kbd>Click</kbd> Trace chain<br>'
    + '<kbd>/</kbd> Search &nbsp; <kbd>Esc</kbd> Clear';
  container.appendChild(hint);

  // Fade hint after 5 seconds
  setTimeout(function() {
    var h = document.getElementById('cv2-controls-hint');
    if (h) h.classList.add('cv2-hint-faded');
  }, 5000);

  // --- Search bar ---
  var searchDiv = document.createElement('div');
  searchDiv.className = 'cv2-search cv2-search-hidden';
  searchDiv.id = 'cv2-search';
  searchDiv.innerHTML = '<input type="text" class="cv2-search-input" placeholder="Search tasks...">';
  container.appendChild(searchDiv);

  var searchInput = searchDiv.querySelector('.cv2-search-input');
  searchInput.addEventListener('input', function() { _cv2OnSearchInput(this.value); });
  searchInput.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      _cv2CloseSearch();
    }
  });

  // --- Tooltip ---
  var tooltip = document.createElement('div');
  tooltip.className = 'cv2-tooltip';
  tooltip.style.display = 'none';
  document.body.appendChild(tooltip);
  _cv2._tooltipEl = tooltip;

  // --- Node labels (HTML overlay showing short_id on each bubble) ---
  var labelContainer = document.createElement('div');
  labelContainer.className = 'cv2-label-container';
  container.appendChild(labelContainer);
  _cv2._labelContainer = labelContainer;
  _cv2._labelEls = [];

  // --- Dependency rail (bottom; populated on selection) ---
  var depchain = document.createElement('div');
  depchain.className = 'cv2-depchain cv2-depchain-hidden';
  depchain.id = 'cv2-depchain';
  container.appendChild(depchain);
  _cv2._depchainEl = depchain;
}

function _cv2CreateLabels(nodes) {
  if (!_cv2._labelContainer) return;
  _cv2._labelContainer.innerHTML = '';
  _cv2._labelEls = [];
  for (var i = 0; i < nodes.length; i++) {
    var el = document.createElement('div');
    el.className = 'cv2-node-label';
    el.textContent = nodes[i].short_id || '';
    _cv2._labelContainer.appendChild(el);
    _cv2._labelEls.push(el);
  }
}

function _cv2UpdateLabels() {
  if (!_cv2._labelEls.length || !_cv2.camera || !_cv2.renderer) return;
  var canvas = _cv2.renderer.domElement;
  var w = canvas.clientWidth, h = canvas.clientHeight;
  var tmpVec = new THREE.Vector3();
  var nodes = _cv2.nodeData;

  for (var i = 0; i < _cv2._labelEls.length; i++) {
    var el = _cv2._labelEls[i];
    if (i >= nodes.length) { el.style.display = 'none'; continue; }
    var node = nodes[i];
    tmpVec.set(node.x || 0, node.y || 0, 0);
    tmpVec.project(_cv2.camera);

    // Check if behind camera
    if (tmpVec.z > 1) { el.style.display = 'none'; continue; }

    var sx = (tmpVec.x * 0.5 + 0.5) * w;
    var sy = (-tmpVec.y * 0.5 + 0.5) * h;

    // Position label below the node
    var scale = CV2_PRIORITY_SCALE[node.priority] || 1.0;
    var offsetY = CV2_NODE_BASE_RADIUS * scale * 0.8 + 4;

    el.style.display = '';
    el.style.left = sx + 'px';
    el.style.top = (sy + offsetY) + 'px';

    // Dim if search/selection active and not on the chain
    if (_cv2.searchActive && !_cv2.searchMatches.has(node.id)) {
      el.style.opacity = '0.08';
    } else if (_cv2.selectedNode) {
      if (node.id === _cv2.selectedNode
          || (_cv2.chainUp && _cv2.chainUp.has(node.id))
          || (_cv2.chainDown && _cv2.chainDown.has(node.id))) {
        el.style.opacity = '1';
      } else {
        el.style.opacity = '0.12';
      }
    } else {
      el.style.opacity = '0.9';
    }
  }
}

/* --------------------------------------------------------------------------
 * 15. Camera Fit
 * ----------------------------------------------------------------------- */

function _cv2FitCameraToGraph() {
  var nodes = _cv2.nodeData;
  if (!nodes || nodes.length === 0 || !_cv2.camera) return;

  // Compute bounding box
  var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (var i = 0; i < nodes.length; i++) {
    var x = nodes[i].x || 0, y = nodes[i].y || 0;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }

  var cx = (minX + maxX) / 2;
  var cy = (minY + maxY) / 2;
  var extentX = (maxX - minX) / 2 + 40; // padding
  var extentY = (maxY - minY) / 2 + 40;

  var container = document.getElementById('cv2-container');
  var aspect = container && container.clientHeight ? (container.clientWidth / container.clientHeight) : 1.8;

  // Half-height that fits both extents. The frustum width = viewHalfH * aspect,
  // so the X extent must be divided by aspect before comparing.
  var needHalfH = Math.max(extentY, extentX / aspect) * 1.1;
  needHalfH = Math.max(CV2_VIEW_MIN_HALF_H, Math.min(CV2_VIEW_MAX_HALF_H, needHalfH));

  _cv2.camTarget.set(cx, cy, 0);
  _cv2.viewHalfH = needHalfH;
  _cv2UpdateCameraProjection();
  _cv2UpdateCameraPosition();
}

/* --------------------------------------------------------------------------
 * 16. Main Entry Points
 * ----------------------------------------------------------------------- */

async function renderCubeV2() {
  cleanupCubeV2();
  var myGen = ++_cv2.generation;

  var app = document.getElementById('app');
  app.innerHTML = '<div id="cv2-container"><div class="cv2-empty">'
    + '<div class="spinner"></div><p>Loading Cube v2...</p></div></div>';

  // Check for Three.js
  if (typeof THREE === 'undefined') {
    app.innerHTML = '<div id="cv2-container"><div class="cv2-empty">'
      + '<div class="cv2-empty-title">3D library unavailable</div>'
      + '<div class="cv2-empty-msg">Cube v2 requires Three.js. Check your network connection.</div>'
      + '<button class="cv2-empty-retry" onclick="location.reload()">Retry</button>'
      + '</div></div>';
    return;
  }

  // Fetch graph data
  try {
    var data = await _cv2Api('/api/graph');
  } catch (e) {
    if (myGen !== _cv2.generation) return;
    app.innerHTML = '<div id="cv2-container"><div class="cv2-empty">'
      + '<p>Failed to load graph data: ' + _cv2Esc(e.message) + '</p>'
      + '</div></div>';
    return;
  }
  if (myGen !== _cv2.generation) return;

  _cv2.currentRevision = (data && data.revision) || null;
  var nodes = (data && data.nodes) || [];
  var links = (data && data.links) || [];

  // Empty state
  if (nodes.length < 1) {
    app.innerHTML = '<div id="cv2-container"><div class="cv2-empty">'
      + '<div class="cv2-empty-title">No tasks to visualize</div>'
      + '<div class="cv2-empty-msg">Create some tasks and they\'ll appear here as a DAG.</div>'
      + '</div></div>';
    return;
  }

  // Clear loading
  var container = document.getElementById('cv2-container');
  if (container) container.innerHTML = '';

  // Build
  _cv2InitScene();
  _cv2InitSimulation(nodes, links);

  // Pre-settle the simulation synchronously, then FREEZE it. The layout is a
  // deterministic function of topology; leaving the sim running caused constant
  // micro-drift. After this the render loop just reads frozen node.x/y, and only
  // an add/remove (handled in updateCubeV2Data) ever re-settles.
  if (_cv2.simulation) {
    _cv2.simulation.alpha(1);
    for (var tick = 0; tick < 200; tick++) {
      _cv2.simulation.tick();
    }
    _cv2.simulation.stop();
  }

  _cv2CreateNodes(nodes);
  _cv2CreateReadyHalos(nodes);
  _cv2CreateEdges(links, nodes);
  _cv2CreateHUD();
  _cv2CreateLabels(nodes);
  _cv2SetupControls();

  // Fit camera to the pre-settled layout — no delayed refits needed
  _cv2FitCameraToGraph();

  // Start animation
  _cv2Animate();
}

function updateCubeV2Data(data) {
  if (!_cv2.scene || !data) return;
  if (_cv2.currentRevision && data.revision === _cv2.currentRevision) return;
  _cv2.currentRevision = data.revision || null;

  var newNodes = data.nodes || [];
  var newLinks = data.links || [];

  // Snapshot previous settled positions and animating colors, keyed by id so
  // they survive even if node ordering shifts (an add/remove reindexes).
  var prevPos = {}, prevColor = {};
  for (var i = 0; i < _cv2.nodeData.length; i++) {
    var on = _cv2.nodeData[i];
    prevPos[on.id] = { x: on.x, y: on.y };
    if (_cv2.nodeCurrentColors[i]) prevColor[on.id] = _cv2.nodeCurrentColors[i];
  }

  // The pivotal check: did a task or relationship get added/removed? A pure
  // status/metadata edit (which still bumps the revision) returns false, and
  // below that means the layout is left completely untouched.
  var topo = _cv2TopologyChanged(_cv2.nodeData, _cv2.linkData, newNodes, newLinks);

  // Depths are a deterministic function of topology; recompute so any genuinely
  // new node can be seeded in its column.
  var depths = _cv2ComputeDepths(newNodes, newLinks);
  _cv2.nodeDepths = depths;

  // Carry settled positions onto the new node objects; seed only brand-new ones.
  for (var i = 0; i < newNodes.length; i++) {
    var p = prevPos[newNodes[i].id];
    if (p) {
      newNodes[i].x = p.x; newNodes[i].y = p.y;
      newNodes[i].vx = 0; newNodes[i].vy = 0;
    } else {
      newNodes[i].x = (depths[newNodes[i].id] || 0) * CV2_COLUMN_SPACING;
      newNodes[i].y = (Math.random() - 0.5) * 300;
    }
  }

  // Blocking graph + readiness for the refreshed data.
  var adj = _cv2BuildBlockingAdjacency(newNodes, newLinks);
  _cv2.blockUp = adj.upstream;
  _cv2.blockDown = adj.downstream;
  _cv2.readySet = _cv2ComputeReady(newNodes, _cv2.blockUp);

  _cv2.nodeData = newNodes;
  _cv2.linkData = newLinks;

  // Color targets, keyed by id so the cross-fade survives reordering.
  _cv2.nodeTargetColors = [];
  _cv2.nodeCurrentColors = [];
  for (var i = 0; i < newNodes.length; i++) {
    var c = new THREE.Color(_cv2NodeColor(newNodes[i]));
    _cv2.nodeTargetColors.push({ r: c.r, g: c.g, b: c.b });
    var pc = prevColor[newNodes[i].id];
    _cv2.nodeCurrentColors.push(pc ? { r: pc.r, g: pc.g, b: pc.b } : { r: c.r, g: c.g, b: c.b });
  }

  if (_cv2.simulation) {
    if (topo) {
      // Added/removed task or edge: re-settle ONCE, synchronously, starting from
      // the carried positions, so only the changed region relaxes. Then re-freeze.
      var idset = new Set();
      for (var i = 0; i < newNodes.length; i++) idset.add(newNodes[i].id);
      var simLinks = [];
      for (var i = 0; i < newLinks.length; i++) {
        var l = newLinks[i];
        var s = typeof l.source === 'object' ? l.source.id : l.source;
        var t = typeof l.target === 'object' ? l.target.id : l.target;
        if (idset.has(s) && idset.has(t)) simLinks.push({ source: s, target: t, type: l.type });
      }
      // Pin every node that already existed to its settled spot; only brand-new
      // nodes stay free to find a slot. This keeps an add/remove from reshuffling
      // the whole board — existing tasks hold position, the new one slots in.
      for (var i = 0; i < newNodes.length; i++) {
        if (prevPos[newNodes[i].id]) {
          newNodes[i].fx = newNodes[i].x;
          newNodes[i].fy = newNodes[i].y;
        } else {
          newNodes[i].fx = null;
          newNodes[i].fy = null;
        }
      }
      _cv2.simulation.nodes(newNodes);
      _cv2.simulation.force('x', d3.forceX(function(d) {
        return (depths[d.id] || 0) * CV2_COLUMN_SPACING;
      }).strength(0.95));
      _cv2.simulation.force('link', d3.forceLink(simLinks).id(function(d) { return d.id; }).distance(80).strength(0.12));
      _cv2.simulation.alpha(0.3);
      for (var k = 0; k < 120; k++) _cv2.simulation.tick();
      _cv2.simulation.stop();
      // Release pins so a later re-settle (or manual interaction) isn't locked.
      for (var i = 0; i < newNodes.length; i++) {
        newNodes[i].fx = null;
        newNodes[i].fy = null;
      }
    } else {
      // Pure status/metadata edit: positions are unchanged, so do NOT reheat.
      // Point the sim at the new objects and leave it frozen — nothing moves.
      _cv2.simulation.nodes(newNodes);
      _cv2.simulation.stop();
    }
  }

  // Rebuild render objects (positions already final).
  _cv2CreateNodes(newNodes);
  _cv2CreateReadyHalos(newNodes);
  _cv2CreateLabels(newNodes);

  if (_cv2.edgeLines) {
    _cv2.scene.remove(_cv2.edgeLines);
    _cv2.edgeLines.geometry.dispose();
    _cv2.edgeLines.material.dispose();
    _cv2.edgeLines = null;
  }
  if (_cv2.flowPoints) {
    _cv2.scene.remove(_cv2.flowPoints);
    _cv2.flowPoints.geometry.dispose();
    _cv2.flowPoints.material.dispose();
    _cv2.flowPoints = null;
  }
  _cv2CreateEdges(newLinks, newNodes);

  // Keep a live selection coherent across the refresh.
  if (_cv2.selectedNode) {
    var selIdx = -1;
    for (var i = 0; i < newNodes.length; i++) {
      if (newNodes[i].id === _cv2.selectedNode) { selIdx = i; break; }
    }
    if (selIdx >= 0) {
      _cv2.chainUp = _cv2ReachSet(_cv2.selectedNode, _cv2.blockUp);
      _cv2.chainDown = _cv2ReachSet(_cv2.selectedNode, _cv2.blockDown);
      _cv2ApplyEdgeHighlight();
      _cv2RenderDepChain(newNodes[selIdx]);
    } else {
      _cv2DeselectNode();
    }
  }
}

function cleanupCubeV2() {
  _cv2.generation++;

  if (_cv2.animFrameId) {
    cancelAnimationFrame(_cv2.animFrameId);
    _cv2.animFrameId = null;
  }

  // Remove event listeners
  var canvas = _cv2.renderer ? _cv2.renderer.domElement : null;
  if (canvas) {
    if (_cv2._mouseDownHandler) canvas.removeEventListener('mousedown', _cv2._mouseDownHandler);
    if (_cv2._wheelHandler) canvas.removeEventListener('wheel', _cv2._wheelHandler);
    if (_cv2._clickHandler) canvas.removeEventListener('click', _cv2._clickHandler);
    if (_cv2._dblClickHandler) canvas.removeEventListener('dblclick', _cv2._dblClickHandler);
  }
  if (_cv2._mouseMoveHandler) window.removeEventListener('mousemove', _cv2._mouseMoveHandler);
  if (_cv2._mouseUpHandler) window.removeEventListener('mouseup', _cv2._mouseUpHandler);
  if (_cv2._keyDownHandler) document.removeEventListener('keydown', _cv2._keyDownHandler);
  if (_cv2._keyUpHandler) document.removeEventListener('keyup', _cv2._keyUpHandler);
  if (_cv2._resizeHandler) window.removeEventListener('resize', _cv2._resizeHandler);

  // Stop simulation
  if (_cv2.simulation) {
    _cv2.simulation.stop();
    _cv2.simulation = null;
  }

  // Dispose Three.js
  if (_cv2.scene) {
    _cv2.scene.traverse(function(obj) {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        if (obj.material.map) obj.material.map.dispose();
        obj.material.dispose();
      }
    });
  }

  if (_cv2.renderer) {
    _cv2.renderer.dispose();
    _cv2.renderer.forceContextLoss();
    var c = _cv2.renderer.domElement;
    if (c && c.parentNode) c.parentNode.removeChild(c);
    _cv2.renderer = null;
  }

  // Remove tooltip from body
  if (_cv2._tooltipEl && _cv2._tooltipEl.parentNode) {
    _cv2._tooltipEl.parentNode.removeChild(_cv2._tooltipEl);
    _cv2._tooltipEl = null;
  }

  // Null state
  _cv2.scene = null;
  _cv2.camera = null;
  _cv2.instancedMesh = null;
  _cv2.readyHalo = null;
  _cv2.readySet = null;
  _cv2.blockUp = {};
  _cv2.blockDown = {};
  _cv2.chainUp = null;
  _cv2.chainDown = null;
  _cv2._depchainEl = null;
  _cv2._edgeBaseColors = null;
  _cv2.edgeLines = null;
  _cv2.edgeTubes = [];
  _cv2.flowPoints = null;
  _cv2.nodeData = [];
  _cv2.linkData = [];
  _cv2.nodeTargetColors = [];
  _cv2.nodeCurrentColors = [];
  _cv2.selectedNode = null;
  _cv2.hoveredNode = null;
  _cv2.searchActive = false;
  _cv2.searchMatches = new Set();
  _cv2._keysDown = new Set();
  _cv2._dragState = null;
  _cv2._validLinks = null;
  _cv2._flowT = null;
  _cv2.currentRevision = null;
  _cv2.camTarget = null;
  _cv2._labelContainer = null;
  _cv2._labelEls = [];
  _cv2._lastClickHandledByMouseUp = false;
}
