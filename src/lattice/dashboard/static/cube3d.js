/* ============================================================================
 * cube3d.js — Lattice Cube 3D Workspace
 *
 * A 3D spatial workspace where tasks are arranged in 3D space:
 *   X = status, Y = priority, Z = recency
 * with semantic zoom (distant dots become rich cards as you fly closer).
 *
 * Dependencies (global):
 *   THREE  — Three.js from CDN
 *   d3     — d3-force-3d from CDN
 *
 * Exposes globals:
 *   renderCube3D()          — mount and render the 3D view
 *   updateCube3DData(data)  — incremental data update
 *   cleanupCube3D()         — teardown everything
 *
 * Dashboard globals used (via window._lattice):
 *   config, getLaneColor, esc, api, apiPost, currentView, showToast
 * ========================================================================= */

/* --------------------------------------------------------------------------
 * 0. Dashboard Integration — resolve shared utilities from host IIFE
 * ----------------------------------------------------------------------- */

var _L = (typeof window !== 'undefined' && window._lattice) || {};
var api = _L.api || function() { return Promise.reject(new Error('api unavailable')); };
var apiPost = _L.apiPost || function() { return Promise.reject(new Error('apiPost unavailable')); };
var esc = _L.esc || function(s) { return String(s); };
var showToast = _L.showToast || function() {};
var getLaneColor = _L.getLaneColor || function() { return '#6b7280'; };

/* config and currentView are live references — read via getter each time */
function _cube3dConfig() { return (_L.getConfig ? _L.getConfig() : null); }
function _cube3dCurrentView() { return (_L.getCurrentView ? _L.getCurrentView() : 'cube'); }

/* --------------------------------------------------------------------------
 * 1. Constants & Color Maps
 * ----------------------------------------------------------------------- */

var CUBE3D_STATUS_COLORS = {
  backlog: '#6b7280', in_planning: '#a78bfa', planned: '#60a5fa',
  in_progress: '#34d399', review: '#fbbf24', done: '#22d3ee',
  blocked: '#f87171', needs_human: '#f59e0b', cancelled: '#374151'
};

var CUBE3D_EDGE_COLORS = {
  blocks: '#ef4444', depends_on: '#f97316', subtask_of: '#3b82f6',
  related_to: '#6b7280', spawned_by: '#8b5cf6'
};

var CUBE3D_PRIORITY_Y = { critical: 120, high: 50, medium: 0, low: -60 };
var CUBE3D_PRIORITY_SCALE = { critical: 2.0, high: 1.5, medium: 1.0, low: 0.7 };

/* Spacing between status lanes on X axis */
var CUBE3D_LANE_SPACING = 80;
/* Zone box dimensions (width along X, height along Y, depth along Z) */
var CUBE3D_ZONE_WIDTH = 70;
var CUBE3D_ZONE_HEIGHT = 400;
var CUBE3D_ZONE_DEPTH = 350;

/* --------------------------------------------------------------------------
 * 2. Module State
 * ----------------------------------------------------------------------- */

var _cube3d = {
  scene: null,
  camera: null,
  webglRenderer: null,
  css3dRenderer: null,
  controls: null,
  animFrameId: null,
  generation: 0,
  simulation: null,
  instancedMesh: null,
  edgeLines: null,
  flowPoints: null,
  fogPlanes: [],
  fogLabels: [],
  nodeData: [],
  linkData: [],
  resizeHandler: null,
  currentRevision: null,
  // LOD state
  lodLevels: [],
  textSprites: [],
  css3dCards: [],
  workspacePanel: null,
  workspaceTaskId: null,
  // Search state
  searchActive: false,
  searchMatches: new Set(),
  searchLines: [],
  // Camera flight
  flyingTo: null,
  flyStartTime: 0,
  flyDuration: 600,
  flyStartPos: null,
  flyStartTarget: null,
  flyEndPos: null,
  flyEndTarget: null,
  // Internal
  _flowT: null,
  _frameCount: 0
};

/* --------------------------------------------------------------------------
 * 3. Inlined OrbitControls
 * ----------------------------------------------------------------------- */

function Cube3DOrbitControls(camera, domElement) {
  var scope = this;
  this.enabled = true;
  this.target = new THREE.Vector3();
  this.enableDamping = true;
  this.dampingFactor = 0.1;
  this.rotateSpeed = 0.8;
  this.zoomSpeed = 1.2;
  this.panSpeed = 0.8;
  this.minDistance = 10;
  this.maxDistance = 3000;

  var spherical = new THREE.Spherical();
  var sphericalDelta = new THREE.Spherical();
  var panOffset = new THREE.Vector3();
  var scale = 1;

  var STATE = { NONE: -1, ROTATE: 0, ZOOM: 1, PAN: 2 };
  var state = STATE.NONE;
  var rotateStart = new THREE.Vector2();
  var rotateEnd = new THREE.Vector2();
  var panStart = new THREE.Vector2();
  var panEnd = new THREE.Vector2();

  function getZoomScale() { return Math.pow(0.95, scope.zoomSpeed); }

  this.lockVertical = false;

  function rotateLeft(angle) { sphericalDelta.theta -= angle; }
  function rotateUp(angle) {
    if (scope.lockVertical) return;
    sphericalDelta.phi -= angle;
  }

  function panLeft(distance) {
    var v = new THREE.Vector3();
    v.setFromMatrixColumn(camera.matrix, 0);
    v.multiplyScalar(-distance);
    panOffset.add(v);
  }

  function panUp(distance) {
    var v = new THREE.Vector3();
    v.setFromMatrixColumn(camera.matrix, 1);
    v.multiplyScalar(distance);
    panOffset.add(v);
  }

  function pan(deltaX, deltaY) {
    var element = domElement;
    var offset = new THREE.Vector3();
    offset.copy(camera.position).sub(scope.target);
    var targetDistance = offset.length();
    targetDistance *= Math.tan((camera.fov / 2) * Math.PI / 180.0);
    panLeft(2 * deltaX * targetDistance / element.clientHeight * scope.panSpeed);
    panUp(2 * deltaY * targetDistance / element.clientHeight * scope.panSpeed);
  }

  this.update = function() {
    var offset = new THREE.Vector3();
    offset.copy(camera.position).sub(scope.target);
    spherical.setFromVector3(offset);
    spherical.theta += sphericalDelta.theta;
    if (!scope.lockVertical) {
      spherical.phi += sphericalDelta.phi;
    }
    spherical.phi = Math.max(0.01, Math.min(Math.PI - 0.01, spherical.phi));
    spherical.radius *= scale;
    spherical.radius = Math.max(scope.minDistance, Math.min(scope.maxDistance, spherical.radius));
    scope.target.add(panOffset);
    offset.setFromSpherical(spherical);
    camera.position.copy(scope.target).add(offset);
    camera.lookAt(scope.target);
    if (scope.enableDamping) {
      sphericalDelta.theta *= (1 - scope.dampingFactor);
      sphericalDelta.phi *= (1 - scope.dampingFactor);
    } else {
      sphericalDelta.set(0, 0, 0);
    }
    panOffset.set(0, 0, 0);
    scale = 1;
  };

  function onMouseDown(event) {
    if (!scope.enabled) return;
    event.preventDefault();
    if (event.button === 0) {
      state = STATE.ROTATE;
      rotateStart.set(event.clientX, event.clientY);
    } else if (event.button === 1 || event.button === 2) {
      state = STATE.PAN;
      panStart.set(event.clientX, event.clientY);
    }
    document.addEventListener('mousemove', onMouseMove, false);
    document.addEventListener('mouseup', onMouseUp, false);
  }

  function onMouseMove(event) {
    if (!scope.enabled) return;
    if (state === STATE.ROTATE) {
      rotateEnd.set(event.clientX, event.clientY);
      var rotateDelta = new THREE.Vector2().subVectors(rotateEnd, rotateStart);
      rotateLeft(2 * Math.PI * rotateDelta.x / domElement.clientHeight * scope.rotateSpeed);
      rotateUp(2 * Math.PI * rotateDelta.y / domElement.clientHeight * scope.rotateSpeed);
      rotateStart.copy(rotateEnd);
    } else if (state === STATE.PAN) {
      panEnd.set(event.clientX, event.clientY);
      var panDelta = new THREE.Vector2().subVectors(panEnd, panStart);
      pan(panDelta.x, panDelta.y);
      panStart.copy(panEnd);
    }
  }

  function onMouseUp() {
    state = STATE.NONE;
    document.removeEventListener('mousemove', onMouseMove, false);
    document.removeEventListener('mouseup', onMouseUp, false);
  }

  function onWheel(event) {
    if (!scope.enabled) return;
    event.preventDefault();
    if (event.deltaY < 0) {
      scale /= getZoomScale();
    } else if (event.deltaY > 0) {
      scale *= getZoomScale();
    }
  }

  function onContextMenu(event) { event.preventDefault(); }

  domElement.addEventListener('mousedown', onMouseDown, false);
  domElement.addEventListener('wheel', onWheel, { passive: false });
  domElement.addEventListener('contextmenu', onContextMenu, false);

  this.dispose = function() {
    domElement.removeEventListener('mousedown', onMouseDown, false);
    domElement.removeEventListener('wheel', onWheel, false);
    domElement.removeEventListener('contextmenu', onContextMenu, false);
    document.removeEventListener('mousemove', onMouseMove, false);
    document.removeEventListener('mouseup', onMouseUp, false);
  };

  // Initialize spherical from current camera position
  var initOffset = new THREE.Vector3();
  initOffset.copy(camera.position).sub(this.target);
  spherical.setFromVector3(initOffset);
}

/* --------------------------------------------------------------------------
 * 4. Inlined CSS3DRenderer & CSS3DObject
 * ----------------------------------------------------------------------- */

function CSS3DObject(element) {
  THREE.Object3D.call(this);
  this.element = element;
  this.element.style.position = 'absolute';
  this.element.style.pointerEvents = 'auto';
}
CSS3DObject.prototype = Object.create(THREE.Object3D.prototype);
CSS3DObject.prototype.constructor = CSS3DObject;

function CSS3DRenderer() {
  var _width, _height;
  var _widthHalf, _heightHalf;
  var domElement = document.createElement('div');
  this.domElement = domElement;
  domElement.style.overflow = 'hidden';

  var viewElement = document.createElement('div');
  viewElement.style.transformOrigin = '0 0';
  viewElement.style.pointerEvents = 'none';
  domElement.appendChild(viewElement);

  var cache = { camera: { fov: 0, style: '' }, objects: new WeakMap() };

  this.getSize = function() { return { width: _width, height: _height }; };

  this.setSize = function(width, height) {
    _width = width;
    _height = height;
    _widthHalf = _width / 2;
    _heightHalf = _height / 2;
    domElement.style.width = width + 'px';
    domElement.style.height = height + 'px';
    viewElement.style.width = width + 'px';
    viewElement.style.height = height + 'px';
  };

  function epsilon(value) { return Math.abs(value) < 1e-10 ? 0 : value; }

  function getCameraCSSMatrix(matrix) {
    var elements = matrix.elements;
    return 'matrix3d(' +
      epsilon(elements[0]) + ',' + epsilon(-elements[1]) + ',' + epsilon(elements[2]) + ',' + epsilon(elements[3]) + ',' +
      epsilon(elements[4]) + ',' + epsilon(-elements[5]) + ',' + epsilon(elements[6]) + ',' + epsilon(elements[7]) + ',' +
      epsilon(elements[8]) + ',' + epsilon(-elements[9]) + ',' + epsilon(elements[10]) + ',' + epsilon(elements[11]) + ',' +
      epsilon(elements[12]) + ',' + epsilon(-elements[13]) + ',' + epsilon(elements[14]) + ',' + epsilon(elements[15]) +
    ')';
  }

  function getObjectCSSMatrix(matrix) {
    var elements = matrix.elements;
    return 'translate(-50%,-50%) matrix3d(' +
      epsilon(elements[0]) + ',' + epsilon(elements[1]) + ',' + epsilon(elements[2]) + ',' + epsilon(elements[3]) + ',' +
      epsilon(-elements[4]) + ',' + epsilon(-elements[5]) + ',' + epsilon(-elements[6]) + ',' + epsilon(-elements[7]) + ',' +
      epsilon(elements[8]) + ',' + epsilon(elements[9]) + ',' + epsilon(elements[10]) + ',' + epsilon(elements[11]) + ',' +
      epsilon(elements[12]) + ',' + epsilon(elements[13]) + ',' + epsilon(elements[14]) + ',' + epsilon(elements[15]) +
    ')';
  }

  function renderObject(object, scene, camera) {
    if (object instanceof CSS3DObject) {
      var style = getObjectCSSMatrix(object.matrixWorld);
      var cachedObject = cache.objects.get(object);
      if (cachedObject === undefined || cachedObject !== style) {
        object.element.style.transform = style;
        cache.objects.set(object, style);
      }
      if (object.element.parentNode !== viewElement) {
        viewElement.appendChild(object.element);
      }
    }
    for (var i = 0; i < object.children.length; i++) {
      renderObject(object.children[i], scene, camera);
    }
  }

  this.render = function(scene, camera) {
    var fov = camera.projectionMatrix.elements[5] * _heightHalf;
    if (cache.camera.fov !== fov) {
      domElement.style.perspective = fov + 'px';
      cache.camera.fov = fov;
    }
    if (scene.matrixWorldAutoUpdate === true) scene.updateMatrixWorld();
    if (camera.parent === null) camera.updateMatrixWorld();

    var cameraCSSMatrix = getCameraCSSMatrix(camera.matrixWorldInverse);
    var style = cameraCSSMatrix + ' translate(' + _widthHalf + 'px,' + _heightHalf + 'px)';
    if (cache.camera.style !== style) {
      viewElement.style.transform = style;
      cache.camera.style = style;
    }
    renderObject(scene, scene, camera);
  };
}

/* --------------------------------------------------------------------------
 * 5. Helper Functions
 * ----------------------------------------------------------------------- */

function cube3dStatusColor(status) {
  return CUBE3D_STATUS_COLORS[status] || '#6b7280';
}

function cube3dHexToRgb(hex) {
  var result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  return result ? {
    r: parseInt(result[1], 16) / 255,
    g: parseInt(result[2], 16) / 255,
    b: parseInt(result[3], 16) / 255
  } : { r: 0.5, g: 0.5, b: 0.5 };
}

function cube3dEaseInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

function cube3dRecencyZ(updatedAt, now, maxAge) {
  if (!updatedAt) return -250;
  var age = now - new Date(updatedAt).getTime();
  var t = Math.min(age / maxAge, 1);
  // Range: 0 (just updated) → -300 (oldest). sqrt curve makes
  // recent items cluster near front, old items spread toward back.
  return -Math.sqrt(t) * 300;
}

/* --------------------------------------------------------------------------
 * 6. Scene Setup
 * ----------------------------------------------------------------------- */

function _cube3dInitScene() {
  var container = document.getElementById('cube3d-container');
  if (!container) return;

  var w = container.offsetWidth;
  var h = container.offsetHeight;

  // Parse background color from CSS custom property
  var bgHex = getComputedStyle(document.documentElement)
    .getPropertyValue('--bg-base').trim() || '#0a0e17';
  var bgColor = new THREE.Color(bgHex);

  // Scene
  var scene = new THREE.Scene();
  scene.background = bgColor;
  scene.fog = new THREE.Fog(bgColor, 600, 1600);

  // Camera
  var camera = new THREE.PerspectiveCamera(60, w / h, 1, 5000);
  camera.position.set(250, 120, 400);

  // WebGL Renderer
  var webglRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  webglRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  webglRenderer.setSize(w, h);
  container.appendChild(webglRenderer.domElement);

  // CSS3D Renderer
  var css3dContainer = document.getElementById('cube3d-css3d-container');
  var css3dRenderer = new CSS3DRenderer();
  css3dRenderer.setSize(w, h);
  if (css3dContainer) {
    css3dContainer.appendChild(css3dRenderer.domElement);
  }

  // Lights
  var ambient = new THREE.AmbientLight(0xffffff, 0.6);
  scene.add(ambient);
  var directional = new THREE.DirectionalLight(0xffffff, 0.4);
  directional.position.set(100, 200, 150);
  scene.add(directional);

  // Controls (inlined OrbitControls)
  var controls = new Cube3DOrbitControls(camera, webglRenderer.domElement);
  controls.lockVertical = true;
  controls.target.set(300, 0, -100);
  controls.update();

  // Store references
  _cube3d.scene = scene;
  _cube3d.camera = camera;
  _cube3d.webglRenderer = webglRenderer;
  _cube3d.css3dRenderer = css3dRenderer;
  _cube3d.controls = controls;
}

/* --------------------------------------------------------------------------
 * 7. Force Simulation (d3-force-3d)
 * ----------------------------------------------------------------------- */

function _cube3dInitSimulation(nodes, links) {
  var _cfg = _cube3dConfig();
  var statuses = (_cfg && _cfg.workflow && _cfg.workflow.statuses) || [];
  var statusX = {};
  statuses.forEach(function(s, i) { statusX[s] = i * CUBE3D_LANE_SPACING; });

  var now = Date.now();
  var maxAge = 30 * 24 * 60 * 60 * 1000; // 30 days

  var simulation = d3.forceSimulation(nodes)
    .numDimensions(3)
    .force('x', d3.forceX(function(d) { return statusX[d.status] || 0; }).strength(0.95))
    .force('y', d3.forceY(function(d) { return CUBE3D_PRIORITY_Y[d.priority] || 0; }).strength(0.3))
    .force('z', d3.forceZ(function(d) { return cube3dRecencyZ(d.updated_at, now, maxAge); }).strength(0.35))
    .force('charge', d3.forceManyBody().strength(-60))
    .force('link', d3.forceLink(links).id(function(d) { return d.id; }).distance(50).strength(0.3))
    .alphaDecay(0.02)
    .velocityDecay(0.3);

  _cube3d.simulation = simulation;
  return simulation;
}

/* --------------------------------------------------------------------------
 * 8. InstancedMesh Nodes
 * ----------------------------------------------------------------------- */

function _cube3dCreateNodes(nodes) {
  var geometry = new THREE.SphereGeometry(1, 16, 12);
  var material = new THREE.MeshStandardMaterial({
    roughness: 0.4,
    metalness: 0.1,
    vertexColors: false
  });

  var mesh = new THREE.InstancedMesh(geometry, material, nodes.length);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

  // Per-instance color buffer
  var colors = new Float32Array(nodes.length * 3);
  var dummy = new THREE.Object3D();

  for (var i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    var scale = CUBE3D_PRIORITY_SCALE[node.priority] || 1.0;
    dummy.position.set(0, 0, 0);
    dummy.scale.set(scale * 3, scale * 3, scale * 3);
    dummy.updateMatrix();
    mesh.setMatrixAt(i, dummy.matrix);

    var rgb = cube3dHexToRgb(cube3dStatusColor(node.status));
    colors[i * 3]     = rgb.r;
    colors[i * 3 + 1] = rgb.g;
    colors[i * 3 + 2] = rgb.b;
  }

  mesh.instanceColor = new THREE.InstancedBufferAttribute(colors, 3);
  _cube3d.scene.add(mesh);
  _cube3d.instancedMesh = mesh;
  _cube3d.nodeData = nodes;
}

/* --------------------------------------------------------------------------
 * 9. Edge Lines + Flow Particles
 * ----------------------------------------------------------------------- */

function _cube3dCreateEdges(links, nodes) {
  // Build node index for reference
  var nodeIndex = {};
  nodes.forEach(function(n, i) { nodeIndex[n.id] = i; });

  var positions = new Float32Array(links.length * 6); // 2 vertices * 3 floats
  var colors = new Float32Array(links.length * 6);
  _cube3d.linkData = links;

  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var color = cube3dHexToRgb(CUBE3D_EDGE_COLORS[link.type] || '#6b7280');
    // Source vertex color
    colors[i * 6]     = color.r;
    colors[i * 6 + 1] = color.g;
    colors[i * 6 + 2] = color.b;
    // Target vertex color
    colors[i * 6 + 3] = color.r;
    colors[i * 6 + 4] = color.g;
    colors[i * 6 + 5] = color.b;
  }

  var lineGeo = new THREE.BufferGeometry();
  lineGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  lineGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  var lineMat = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.5
  });
  var lineSegments = new THREE.LineSegments(lineGeo, lineMat);
  _cube3d.scene.add(lineSegments);
  _cube3d.edgeLines = lineSegments;

  // Flow particles — one per edge
  var particlePositions = new Float32Array(links.length * 3);
  var particleGeo = new THREE.BufferGeometry();
  particleGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));
  var particleMat = new THREE.PointsMaterial({
    size: 2,
    color: 0xffffff,
    transparent: true,
    opacity: 0.7,
    sizeAttenuation: true
  });
  var particles = new THREE.Points(particleGeo, particleMat);
  _cube3d.scene.add(particles);
  _cube3d.flowPoints = particles;

  // Stagger flow t-values
  _cube3d._flowT = new Float32Array(links.length);
  for (var j = 0; j < links.length; j++) {
    _cube3d._flowT[j] = Math.random();
  }
}

/* --------------------------------------------------------------------------
 * 10. Status Zone Volumes
 * ----------------------------------------------------------------------- */

function _cube3dCreateFogPlanes() {
  var _cfg = _cube3dConfig();
  var statuses = (_cfg && _cfg.workflow && _cfg.workflow.statuses) || [];
  statuses.forEach(function(status, i) {
    var x = i * CUBE3D_LANE_SPACING;
    var color = new THREE.Color(cube3dStatusColor(status));

    // 3D box volume — translucent zone that nodes sit inside
    var boxGeo = new THREE.BoxGeometry(CUBE3D_ZONE_WIDTH, CUBE3D_ZONE_HEIGHT, CUBE3D_ZONE_DEPTH);
    var boxMat = new THREE.MeshBasicMaterial({
      color: color,
      transparent: true,
      opacity: 0.035,
      side: THREE.BackSide,
      depthWrite: false
    });
    var box = new THREE.Mesh(boxGeo, boxMat);
    box.position.set(x, 0, -CUBE3D_ZONE_DEPTH / 2);
    _cube3d.scene.add(box);
    _cube3d.fogPlanes.push(box);

    // Thin wireframe outline for spatial definition
    var edgesGeo = new THREE.EdgesGeometry(boxGeo);
    var edgesMat = new THREE.LineBasicMaterial({
      color: color,
      transparent: true,
      opacity: 0.12
    });
    var edges = new THREE.LineSegments(edgesGeo, edgesMat);
    edges.position.copy(box.position);
    _cube3d.scene.add(edges);
    _cube3d.fogPlanes.push(edges);

    // Floor tint — subtle colored ground plane at bottom of zone
    var floorGeo = new THREE.PlaneGeometry(CUBE3D_ZONE_WIDTH, CUBE3D_ZONE_DEPTH);
    var floorMat = new THREE.MeshBasicMaterial({
      color: color,
      transparent: true,
      opacity: 0.06,
      side: THREE.DoubleSide,
      depthWrite: false
    });
    var floor = new THREE.Mesh(floorGeo, floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(x, -CUBE3D_ZONE_HEIGHT / 2, -CUBE3D_ZONE_DEPTH / 2);
    _cube3d.scene.add(floor);
    _cube3d.fogPlanes.push(floor);

    // Text sprite label — above the zone box
    var canvas = document.createElement('canvas');
    var ctx = canvas.getContext('2d');
    canvas.width = 256;
    canvas.height = 64;
    ctx.fillStyle = 'rgba(0,0,0,0)';
    ctx.fillRect(0, 0, 256, 64);
    ctx.fillStyle = '#' + color.getHexString();
    ctx.font = 'bold 24px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(status.replace(/_/g, ' '), 128, 40);
    var texture = new THREE.CanvasTexture(canvas);
    var spriteMat = new THREE.SpriteMaterial({
      map: texture,
      transparent: true,
      opacity: 0.7
    });
    var sprite = new THREE.Sprite(spriteMat);
    sprite.position.set(x, CUBE3D_ZONE_HEIGHT / 2 + 20, -CUBE3D_ZONE_DEPTH / 2);
    sprite.scale.set(80, 20, 1);
    _cube3d.scene.add(sprite);
    _cube3d.fogLabels.push(sprite);
  });
}

/* --------------------------------------------------------------------------
 * 11. HUD Overlay (Legend, Controls Hint, Search)
 * ----------------------------------------------------------------------- */

function _cube3dCreateHUD() {
  var container = document.getElementById('cube3d-container');
  if (!container) return;
  var _cfg = _cube3dConfig();
  var statuses = (_cfg && _cfg.workflow && _cfg.workflow.statuses) || [];

  // Legend (bottom-left)
  var legendHtml = '<div class="cube3d-legend">';
  legendHtml += '<div class="cube3d-legend-title">Statuses</div>';
  statuses.forEach(function(s) {
    var c = cube3dStatusColor(s);
    legendHtml += '<div class="cube3d-legend-item">'
      + '<span class="cube3d-legend-dot" style="background:' + c + '"></span>'
      + '<span>' + s.replace(/_/g, ' ') + '</span></div>';
  });
  legendHtml += '<div class="cube3d-legend-divider"></div>';
  legendHtml += '<div class="cube3d-legend-title">Edges</div>';
  var edgeTypes = {
    blocks: 'Blocks',
    depends_on: 'Depends on',
    subtask_of: 'Subtask of',
    related_to: 'Related to'
  };
  for (var et in edgeTypes) {
    if (edgeTypes.hasOwnProperty(et)) {
      legendHtml += '<div class="cube3d-legend-item">'
        + '<span class="cube3d-legend-line" style="background:' + CUBE3D_EDGE_COLORS[et] + '"></span>'
        + '<span>' + edgeTypes[et] + '</span></div>';
    }
  }
  legendHtml += '</div>';

  // Controls hint (top-right)
  var hintHtml = '<div class="cube3d-controls-hint">';
  hintHtml += 'Drag to orbit &middot; Scroll to zoom<br>';
  hintHtml += 'Right-drag to pan &middot; Click node to fly<br>';
  hintHtml += '<kbd>/</kbd> Search &middot; <kbd>Esc</kbd> Rise up';
  hintHtml += '</div>';

  // Search bar
  var searchHtml = '<div class="cube3d-search cube3d-search-hidden" id="cube3d-search">';
  searchHtml += '<input type="text" class="cube3d-search-input" id="cube3d-search-input" placeholder="Search tasks...">';
  searchHtml += '</div>';

  container.insertAdjacentHTML('beforeend', legendHtml + hintHtml + searchHtml);

  // Attach search keyboard handler
  document.addEventListener('keydown', _cube3dSearchKeyHandler);
}

/* --------------------------------------------------------------------------
 * 12. LOD System
 *
 * Per-frame LOD assignment based on camera distance:
 *   >800  : LOD 0 — sphere only
 *   300-800: LOD 1 — sphere + short_id text sprite
 *   80-300 : LOD 2 — sphere + short_id + title text sprite
 *   20-80  : LOD 3 — hide sphere, show CSS3D card
 *   <20    : LOD 4 — expanded workspace panel
 *
 * Pool limits: 50 text sprites, 8 CSS3D cards, 1 workspace panel.
 * ----------------------------------------------------------------------- */

function _cube3dUpdateLOD() {
  var camera = _cube3d.camera;
  if (!camera || !_cube3d.nodeData) return;

  var camPos = camera.position;
  var nodePos = new THREE.Vector3();

  // Pool limits
  var maxSprites = 30;
  var maxCards = 4;

  // Build distance-sorted list (closest first)
  var nodeDistances = [];
  for (var i = 0; i < _cube3d.nodeData.length; i++) {
    var node = _cube3d.nodeData[i];
    nodePos.set(node.x || 0, node.y || 0, node.z || 0);
    var dist = camPos.distanceTo(nodePos);
    nodeDistances.push({ index: i, node: node, dist: dist });
  }
  nodeDistances.sort(function(a, b) { return a.dist - b.dist; });

  // Clear previous LOD objects
  _cube3dClearLODObjects();

  // Spatial dedup: once a node claims a card/sprite slot, nearby nodes
  // (within MIN_LABEL_SEP) get demoted to sphere-only to avoid overlap.
  var MIN_LABEL_SEP = 25;
  var claimedPositions = [];
  var spriteCount = 0;
  var cardCount = 0;

  function tooCloseToExisting(nx, ny, nz) {
    for (var k = 0; k < claimedPositions.length; k++) {
      var cp = claimedPositions[k];
      var dx = nx - cp[0], dy = ny - cp[1], dz = nz - cp[2];
      if (Math.sqrt(dx * dx + dy * dy + dz * dz) < MIN_LABEL_SEP) return true;
    }
    return false;
  }

  for (var j = 0; j < nodeDistances.length; j++) {
    var nd = nodeDistances[j];
    var n = nd.node;
    var d = nd.dist;
    var idx = nd.index;
    var nx = n.x || 0, ny = n.y || 0, nz = n.z || 0;

    if (d < 20 && !_cube3d.workspaceTaskId) {
      // LOD 4 — workspace (max 1, always wins)
      _cube3dShowWorkspace(n);
      _cube3dScaleInstance(idx, 0);
      claimedPositions.push([nx, ny, nz]);
    } else if (d < 80 && cardCount < maxCards && !tooCloseToExisting(nx, ny, nz)) {
      // LOD 3 — CSS3D card (skip if another card already nearby)
      cardCount++;
      _cube3dShowCard(n);
      _cube3dScaleInstance(idx, 0);
      claimedPositions.push([nx, ny, nz]);
    } else if (d < 300 && spriteCount < maxSprites && !tooCloseToExisting(nx, ny, nz)) {
      // LOD 2 — text sprite with title
      spriteCount++;
      _cube3dShowTextSprite(n, true);
      claimedPositions.push([nx, ny, nz]);
    } else if (d < 600 && spriteCount < maxSprites && !tooCloseToExisting(nx, ny, nz)) {
      // LOD 1 — text sprite ID only
      spriteCount++;
      _cube3dShowTextSprite(n, false);
      claimedPositions.push([nx, ny, nz]);
    }
    // LOD 0 — instanced sphere only (default / too close to another label)
  }
}

function _cube3dClearLODObjects() {
  // Remove text sprites
  _cube3d.textSprites.forEach(function(s) { _cube3d.scene.remove(s); });
  _cube3d.textSprites = [];

  // Remove CSS3D cards
  _cube3d.css3dCards.forEach(function(c) { _cube3d.scene.remove(c); });
  _cube3d.css3dCards = [];

  // Restore all instance scales to their default
  if (_cube3d.instancedMesh && _cube3d.nodeData) {
    var dummy = new THREE.Object3D();
    for (var i = 0; i < _cube3d.nodeData.length; i++) {
      var node = _cube3d.nodeData[i];
      var scale = CUBE3D_PRIORITY_SCALE[node.priority] || 1.0;
      dummy.position.set(node.x || 0, node.y || 0, node.z || 0);
      dummy.scale.set(scale * 3, scale * 3, scale * 3);
      dummy.updateMatrix();
      _cube3d.instancedMesh.setMatrixAt(i, dummy.matrix);
    }
    _cube3d.instancedMesh.instanceMatrix.needsUpdate = true;
  }
}

function _cube3dScaleInstance(index, scale) {
  if (!_cube3d.instancedMesh) return;
  var dummy = new THREE.Object3D();
  var node = _cube3d.nodeData[index];
  dummy.position.set(node.x || 0, node.y || 0, node.z || 0);
  dummy.scale.set(scale, scale, scale);
  dummy.updateMatrix();
  _cube3d.instancedMesh.setMatrixAt(index, dummy.matrix);
  _cube3d.instancedMesh.instanceMatrix.needsUpdate = true;
}

function _cube3dShowTextSprite(node, showTitle) {
  var canvas = document.createElement('canvas');
  var ctx = canvas.getContext('2d');
  canvas.width = 512;
  canvas.height = showTitle ? 128 : 64;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Short ID
  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 28px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(node.short_id || node.id.substring(0, 8), 256, 32);

  // Title (LOD 2 only)
  if (showTitle && node.title) {
    ctx.font = '22px sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.8)';
    var title = node.title.length > 40
      ? node.title.substring(0, 37) + '...'
      : node.title;
    ctx.fillText(title, 256, 80);
  }

  var texture = new THREE.CanvasTexture(canvas);
  var spriteMat = new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthTest: false
  });
  var sprite = new THREE.Sprite(spriteMat);
  var scale = CUBE3D_PRIORITY_SCALE[node.priority] || 1.0;
  var yOffset = scale * 3 + 5;
  sprite.position.set(node.x || 0, (node.y || 0) + yOffset, node.z || 0);
  sprite.scale.set(showTitle ? 45 : 30, showTitle ? 12 : 7, 1);
  _cube3d.scene.add(sprite);
  _cube3d.textSprites.push(sprite);
}

function _cube3dShowCard(node) {
  var statusColor = cube3dStatusColor(node.status);
  var el = document.createElement('div');
  el.className = 'cube3d-card';
  el.style.borderLeftColor = statusColor;
  el.innerHTML = '<div class="cube3d-card-header">'
    + '<span class="cube3d-card-id">' + (node.short_id || node.id.substring(0, 8)) + '</span>'
    + '<span class="cube3d-card-status" style="background:' + statusColor + '">'
    + (node.status || '').replace(/_/g, ' ') + '</span>'
    + '</div>'
    + '<div class="cube3d-card-title">' + (node.title || 'Untitled') + '</div>'
    + '<div class="cube3d-card-meta">'
    + (node.priority
      ? '<span class="cube3d-card-priority" style="background:'
        + cube3dStatusColor(
            node.priority === 'critical' ? 'blocked'
            : node.priority === 'high' ? 'review'
            : 'in_progress'
          ) + '"></span>'
      : '')
    + (node.assigned_to
      ? '<span class="cube3d-card-assignee">' + node.assigned_to + '</span>'
      : '')
    + '</div>'
    + (node.description_snippet
      ? '<div class="cube3d-card-description">' + node.description_snippet + '</div>'
      : '');

  el.addEventListener('click', function(e) {
    e.stopPropagation();
    _cube3dFlyToNode(node);
  });

  var css3dObj = new CSS3DObject(el);
  css3dObj.position.set(node.x || 0, node.y || 0, node.z || 0);
  css3dObj.scale.set(0.5, 0.5, 0.5);
  _cube3d.scene.add(css3dObj);
  _cube3d.css3dCards.push(css3dObj);
}

function _cube3dShowWorkspace(node) {
  if (_cube3d.workspaceTaskId === node.id) return;
  _cube3d.workspaceTaskId = node.id;

  // Fetch full task data from API
  api('/api/tasks/' + node.id + '/full').then(function(data) {
    if (_cube3d.workspaceTaskId !== node.id) return; // stale request
    _cube3dRenderWorkspacePanel(node, data);
  }).catch(function() {
    // Fallback to basic card if full endpoint unavailable
    _cube3dShowCard(node);
  });
}

function _cube3dRenderWorkspacePanel(node, fullData) {
  // Remove existing workspace panel
  if (_cube3d.workspacePanel) {
    _cube3d.scene.remove(_cube3d.workspacePanel);
    _cube3d.workspacePanel = null;
  }

  var statusColor = cube3dStatusColor(node.status);
  var el = document.createElement('div');
  el.className = 'cube3d-workspace';
  el.style.borderLeftColor = statusColor;

  var html = '<div class="cube3d-workspace-header">'
    + '<span class="cube3d-workspace-id">'
    + (node.short_id || node.id.substring(0, 12)) + '</span>'
    + '<span class="cube3d-workspace-status" style="background:' + statusColor + '">'
    + (node.status || '').replace(/_/g, ' ') + '</span>'
    + '</div>'
    + '<div class="cube3d-workspace-title">'
    + (fullData.title || node.title || 'Untitled') + '</div>';

  // Meta fields
  html += '<div class="cube3d-workspace-meta">';
  if (fullData.priority) {
    html += '<div class="cube3d-workspace-meta-item">'
      + '<span class="cube3d-workspace-meta-label">Priority</span>'
      + fullData.priority + '</div>';
  }
  if (fullData.assigned_to) {
    html += '<div class="cube3d-workspace-meta-item">'
      + '<span class="cube3d-workspace-meta-label">Assigned</span>'
      + fullData.assigned_to + '</div>';
  }
  if (fullData.type) {
    html += '<div class="cube3d-workspace-meta-item">'
      + '<span class="cube3d-workspace-meta-label">Type</span>'
      + fullData.type + '</div>';
  }
  html += '</div>';

  // Description
  if (fullData.description) {
    html += '<div class="cube3d-workspace-description">'
      + fullData.description + '</div>';
  }

  // Recent events
  var events = fullData.recent_events || [];
  if (events.length > 0) {
    html += '<div class="cube3d-workspace-section-title">Recent Events</div>';
    html += '<ul class="cube3d-workspace-timeline">';
    events.forEach(function(ev) {
      var timeStr = ev.ts ? new Date(ev.ts).toLocaleString() : '';
      html += '<li class="cube3d-workspace-event">'
        + '<span class="cube3d-workspace-event-type">'
        + (ev.type || '').replace(/_/g, ' ') + '</span>'
        + '<span class="cube3d-workspace-event-actor">'
        + (ev.actor || '') + '</span>'
        + '<span class="cube3d-workspace-event-time">'
        + timeStr + '</span>'
        + '</li>';
    });
    html += '</ul>';
  }

  // Comments
  var comments = fullData.comments || [];
  if (comments.length > 0) {
    html += '<div class="cube3d-workspace-section-title">Comments</div>';
    html += '<ul class="cube3d-workspace-comments">';
    comments.forEach(function(c) {
      var timeStr = c.created_at ? new Date(c.created_at).toLocaleString() : '';
      html += '<li class="cube3d-workspace-comment">'
        + '<div class="cube3d-workspace-comment-header">'
        + '<span class="cube3d-workspace-comment-actor">'
        + (c.actor || '') + '</span>'
        + '<span class="cube3d-workspace-comment-time">'
        + timeStr + '</span>'
        + '</div>'
        + '<div class="cube3d-workspace-comment-body">'
        + (c.body || '') + '</div>'
        + '</li>';
    });
    html += '</ul>';
  }

  // Relationships
  var rels = fullData.relationships_out || [];
  if (rels.length > 0) {
    html += '<div class="cube3d-workspace-section-title">Relationships</div>';
    html += '<ul class="cube3d-workspace-relations">';
    rels.forEach(function(r) {
      html += '<li class="cube3d-workspace-relation">'
        + '<span class="cube3d-workspace-relation-type">'
        + (r.type || '').replace(/_/g, ' ') + '</span>'
        + '<span class="cube3d-workspace-relation-target">'
        + (r.target_task_id || '').substring(0, 12) + '</span>'
        + '</li>';
    });
    html += '</ul>';
  }

  el.innerHTML = html;

  var css3dObj = new CSS3DObject(el);
  css3dObj.position.set(node.x || 0, node.y || 0, (node.z || 0) + 10);
  css3dObj.scale.set(0.4, 0.4, 0.4);
  _cube3d.scene.add(css3dObj);
  _cube3d.workspacePanel = css3dObj;
}

/* --------------------------------------------------------------------------
 * 13. Click-to-fly Navigation
 * ----------------------------------------------------------------------- */

function _cube3dSetupRaycaster() {
  var raycaster = new THREE.Raycaster();
  var mouse = new THREE.Vector2();

  var container = document.getElementById('cube3d-container');
  if (!container) return;

  container.addEventListener('click', function(event) {
    if (!_cube3d.instancedMesh || !_cube3d.controls || !_cube3d.controls.enabled) return;

    var rect = container.getBoundingClientRect();
    mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(mouse, _cube3d.camera);
    var intersects = raycaster.intersectObject(_cube3d.instancedMesh);
    if (intersects.length > 0) {
      var instanceId = intersects[0].instanceId;
      if (instanceId !== undefined && _cube3d.nodeData[instanceId]) {
        _cube3dFlyToNode(_cube3d.nodeData[instanceId]);
      }
    }
  });
}

function _cube3dFlyToNode(node) {
  if (_cube3d.flyingTo) return; // already in flight

  var target = new THREE.Vector3(node.x || 0, node.y || 0, node.z || 0);
  var offset = new THREE.Vector3(0, 20, 60);
  var endPos = target.clone().add(offset);

  _cube3d.flyingTo = node;
  _cube3d.flyStartTime = performance.now();
  _cube3d.flyStartPos = _cube3d.camera.position.clone();
  _cube3d.flyStartTarget = _cube3d.controls.target.clone();
  _cube3d.flyEndPos = endPos;
  _cube3d.flyEndTarget = target;
  _cube3d.controls.enabled = false;
}

function _cube3dUpdateFlight() {
  if (!_cube3d.flyingTo) return;

  var elapsed = performance.now() - _cube3d.flyStartTime;
  var t = Math.min(elapsed / _cube3d.flyDuration, 1);
  var eased = cube3dEaseInOutCubic(t);

  _cube3d.camera.position.lerpVectors(_cube3d.flyStartPos, _cube3d.flyEndPos, eased);
  _cube3d.controls.target.lerpVectors(_cube3d.flyStartTarget, _cube3d.flyEndTarget, eased);
  _cube3d.controls.update();

  if (t >= 1) {
    _cube3d.flyingTo = null;
    _cube3d.controls.enabled = true;
  }
}

/* --------------------------------------------------------------------------
 * 14. Search
 * ----------------------------------------------------------------------- */

var _cube3dSearchKeyHandler = function(e) {
  if (e.key === '/' && _cube3dCurrentView() === 'cube') {
    e.preventDefault();
    var searchEl = document.getElementById('cube3d-search');
    var inputEl = document.getElementById('cube3d-search-input');
    if (searchEl && inputEl) {
      searchEl.classList.remove('cube3d-search-hidden');
      inputEl.focus();
      inputEl.value = '';
      _cube3d.searchActive = true;
    }
  }
  if (e.key === 'Escape' && _cube3d.searchActive) {
    _cube3dClearSearch();
  }
  if (e.key === 'Escape' && !_cube3d.searchActive && _cube3dCurrentView() === 'cube') {
    // Rise: pull camera backward
    _cube3dRise();
  }
};

function _cube3dSetupSearch() {
  var input = document.getElementById('cube3d-search-input');
  if (!input) return;
  input.addEventListener('input', function() {
    var query = input.value.toLowerCase().trim();
    if (!query) {
      _cube3dClearSearchHighlight();
      return;
    }
    _cube3dApplySearch(query);
  });
}

function _cube3dApplySearch(query) {
  _cube3d.searchMatches.clear();
  if (!_cube3d.nodeData || !_cube3d.instancedMesh) return;

  for (var i = 0; i < _cube3d.nodeData.length; i++) {
    var node = _cube3d.nodeData[i];
    var match = (node.title && node.title.toLowerCase().indexOf(query) >= 0)
      || (node.short_id && node.short_id.toLowerCase().indexOf(query) >= 0);
    if (match) _cube3d.searchMatches.add(i);
  }

  // Dim non-matching, highlight matching
  var colors = _cube3d.instancedMesh.instanceColor;
  if (!colors) return;
  var arr = colors.array;
  for (var j = 0; j < _cube3d.nodeData.length; j++) {
    var n = _cube3d.nodeData[j];
    if (_cube3d.searchMatches.has(j)) {
      var rgb = cube3dHexToRgb(cube3dStatusColor(n.status));
      arr[j * 3]     = rgb.r;
      arr[j * 3 + 1] = rgb.g;
      arr[j * 3 + 2] = rgb.b;
    } else {
      arr[j * 3]     = 0.15;
      arr[j * 3 + 1] = 0.15;
      arr[j * 3 + 2] = 0.15;
    }
  }
  colors.needsUpdate = true;

  // Scale matching nodes larger, non-matching smaller
  var dummy = new THREE.Object3D();
  for (var k = 0; k < _cube3d.nodeData.length; k++) {
    var nd = _cube3d.nodeData[k];
    var s = CUBE3D_PRIORITY_SCALE[nd.priority] || 1.0;
    if (_cube3d.searchMatches.has(k)) {
      s *= 1.5;
    } else {
      s *= 0.5;
    }
    dummy.position.set(nd.x || 0, nd.y || 0, nd.z || 0);
    dummy.scale.set(s * 3, s * 3, s * 3);
    dummy.updateMatrix();
    _cube3d.instancedMesh.setMatrixAt(k, dummy.matrix);
  }
  _cube3d.instancedMesh.instanceMatrix.needsUpdate = true;
}

function _cube3dClearSearch() {
  var searchEl = document.getElementById('cube3d-search');
  if (searchEl) searchEl.classList.add('cube3d-search-hidden');
  _cube3d.searchActive = false;
  _cube3dClearSearchHighlight();
}

function _cube3dClearSearchHighlight() {
  _cube3d.searchMatches.clear();
  if (!_cube3d.instancedMesh || !_cube3d.nodeData) return;
  var colors = _cube3d.instancedMesh.instanceColor;
  if (!colors) return;
  var arr = colors.array;
  for (var i = 0; i < _cube3d.nodeData.length; i++) {
    var n = _cube3d.nodeData[i];
    var rgb = cube3dHexToRgb(cube3dStatusColor(n.status));
    arr[i * 3]     = rgb.r;
    arr[i * 3 + 1] = rgb.g;
    arr[i * 3 + 2] = rgb.b;
  }
  colors.needsUpdate = true;
}

function _cube3dRise() {
  if (!_cube3d.camera || !_cube3d.controls) return;

  // Move camera backward (increase distance from target)
  var dir = new THREE.Vector3();
  dir.copy(_cube3d.camera.position).sub(_cube3d.controls.target).normalize();
  var endPos = _cube3d.camera.position.clone().add(dir.multiplyScalar(200));

  _cube3d.flyingTo = {}; // placeholder so flight logic runs
  _cube3d.flyStartTime = performance.now();
  _cube3d.flyStartPos = _cube3d.camera.position.clone();
  _cube3d.flyStartTarget = _cube3d.controls.target.clone();
  _cube3d.flyEndPos = endPos;
  _cube3d.flyEndTarget = _cube3d.controls.target.clone();
  _cube3d.controls.enabled = false;

  // Clear workspace panel
  _cube3d.workspaceTaskId = null;
  if (_cube3d.workspacePanel) {
    _cube3d.scene.remove(_cube3d.workspacePanel);
    _cube3d.workspacePanel = null;
  }
}

/* --------------------------------------------------------------------------
 * 15. Animation Loop
 * ----------------------------------------------------------------------- */

function _cube3dAnimate() {
  var gen = _cube3d.generation;
  _cube3d.animFrameId = requestAnimationFrame(function() {
    if (_cube3d.generation !== gen) return;
    _cube3dAnimate();
  });

  // --- Update simulation positions onto instanced mesh ---
  if (_cube3d.simulation && _cube3d.instancedMesh && _cube3d.nodeData) {
    var dummy = new THREE.Object3D();
    var searching = _cube3d.searchMatches.size > 0;

    for (var i = 0; i < _cube3d.nodeData.length; i++) {
      var node = _cube3d.nodeData[i];
      var scale = CUBE3D_PRIORITY_SCALE[node.priority] || 1.0;
      if (searching) {
        scale = _cube3d.searchMatches.has(i) ? scale * 1.5 : scale * 0.5;
      }
      dummy.position.set(node.x || 0, node.y || 0, node.z || 0);
      dummy.scale.set(scale * 3, scale * 3, scale * 3);
      dummy.updateMatrix();
      _cube3d.instancedMesh.setMatrixAt(i, dummy.matrix);
    }
    _cube3d.instancedMesh.instanceMatrix.needsUpdate = true;

    // --- Update edge positions ---
    if (_cube3d.edgeLines) {
      var edgePos = _cube3d.edgeLines.geometry.attributes.position.array;
      for (var j = 0; j < _cube3d.linkData.length; j++) {
        var link = _cube3d.linkData[j];
        var src = (typeof link.source === 'object') ? link.source : null;
        var tgt = (typeof link.target === 'object') ? link.target : null;
        if (src && tgt) {
          edgePos[j * 6]     = src.x || 0;
          edgePos[j * 6 + 1] = src.y || 0;
          edgePos[j * 6 + 2] = src.z || 0;
          edgePos[j * 6 + 3] = tgt.x || 0;
          edgePos[j * 6 + 4] = tgt.y || 0;
          edgePos[j * 6 + 5] = tgt.z || 0;
        }
      }
      _cube3d.edgeLines.geometry.attributes.position.needsUpdate = true;
    }

    // --- Update flow particles ---
    if (_cube3d.flowPoints && _cube3d._flowT) {
      var flowPos = _cube3d.flowPoints.geometry.attributes.position.array;
      for (var k = 0; k < _cube3d.linkData.length; k++) {
        _cube3d._flowT[k] = (_cube3d._flowT[k] + 0.005) % 1;
        var fl = _cube3d.linkData[k];
        var fs = (typeof fl.source === 'object') ? fl.source : null;
        var ft = (typeof fl.target === 'object') ? fl.target : null;
        if (fs && ft) {
          var t = _cube3d._flowT[k];
          flowPos[k * 3]     = (fs.x || 0) * (1 - t) + (ft.x || 0) * t;
          flowPos[k * 3 + 1] = (fs.y || 0) * (1 - t) + (ft.y || 0) * t;
          flowPos[k * 3 + 2] = (fs.z || 0) * (1 - t) + (ft.z || 0) * t;
        }
      }
      _cube3d.flowPoints.geometry.attributes.position.needsUpdate = true;
    }
  }

  // --- Camera flight animation ---
  _cube3dUpdateFlight();

  // --- LOD update (every 5 frames to save CPU) ---
  if (!_cube3d._frameCount) _cube3d._frameCount = 0;
  _cube3d._frameCount++;
  if (_cube3d._frameCount % 5 === 0) {
    _cube3dUpdateLOD();
  }

  // --- Billboard CSS3D cards toward camera ---
  _cube3d.css3dCards.forEach(function(card) {
    if (_cube3d.camera) card.quaternion.copy(_cube3d.camera.quaternion);
  });
  if (_cube3d.workspacePanel && _cube3d.camera) {
    _cube3d.workspacePanel.quaternion.copy(_cube3d.camera.quaternion);
  }

  // --- Update orbit controls ---
  if (_cube3d.controls && _cube3d.controls.enabled) {
    _cube3d.controls.update();
  }

  // --- Render ---
  if (_cube3d.webglRenderer && _cube3d.scene && _cube3d.camera) {
    _cube3d.webglRenderer.render(_cube3d.scene, _cube3d.camera);
  }
  if (_cube3d.css3dRenderer && _cube3d.scene && _cube3d.camera) {
    _cube3d.css3dRenderer.render(_cube3d.scene, _cube3d.camera);
  }
}

/* --------------------------------------------------------------------------
 * 16. Resize Handler
 * ----------------------------------------------------------------------- */

function _cube3dOnResize() {
  var container = document.getElementById('cube3d-container');
  if (!container) return;
  var w = container.offsetWidth;
  var h = container.offsetHeight;

  if (_cube3d.camera) {
    _cube3d.camera.aspect = w / h;
    _cube3d.camera.updateProjectionMatrix();
  }
  if (_cube3d.webglRenderer) {
    _cube3d.webglRenderer.setSize(w, h);
  }
  if (_cube3d.css3dRenderer) {
    _cube3d.css3dRenderer.setSize(w, h);
  }
}

/* ==========================================================================
 * 17. Public API — the 3 globals
 * ======================================================================= */

/**
 * renderCube3D() — Mount and render the 3D workspace.
 * Fetches graph data from /api/graph, builds scene, starts animation.
 */
async function renderCube3D() {
  var app = document.getElementById('app');
  cleanupCube3D();
  _cube3d.generation++;
  var myGen = _cube3d.generation;

  // Guard: Three.js must be loaded
  if (typeof THREE === 'undefined') {
    app.innerHTML = '<div id="cube3d-container"><div class="cube3d-empty">'
      + '<div class="cube3d-empty-icon">\u26a0</div>'
      + '<div class="cube3d-empty-title">Three.js library unavailable</div>'
      + '<div class="cube3d-empty-msg">The 3D view requires Three.js. Check your network connection.</div>'
      + '<button class="cube3d-empty-retry" onclick="location.reload()">Retry</button>'
      + '</div></div>';
    return;
  }

  // Build DOM skeleton
  app.innerHTML = '<div id="cube3d-container">'
    + '<div class="cube3d-loading"><div class="spinner"></div></div>'
    + '</div>'
    + '<div id="cube3d-css3d-container"></div>'
    + '<div class="cube3d-mobile-only">'
    + '<div class="cube3d-mobile-only-icon">\uD83D\uDDA5\uFE0F</div>'
    + '<div class="cube3d-mobile-only-title">Desktop Only</div>'
    + '<div class="cube3d-mobile-only-msg">The 3D workspace requires a desktop browser with mouse controls.</div>'
    + '</div>';

  // Fetch graph data
  try {
    var data = await api('/api/graph');
  } catch (e) {
    if (myGen !== _cube3d.generation) return;
    app.innerHTML = '<div id="cube3d-container"><div class="cube3d-empty">'
      + '<p>Failed to load graph data: '
      + (typeof esc === 'function' ? esc(e.message) : e.message) + '</p>'
      + '</div></div>';
    return;
  }
  if (myGen !== _cube3d.generation) return;

  _cube3d.currentRevision = (data && data.revision) || null;
  var nodes = (data && data.nodes) || [];
  var links = (data && data.links) || [];

  // Empty state
  if (nodes.length < 1) {
    app.innerHTML = '<div id="cube3d-container"><div class="cube3d-empty">'
      + '<div class="cube3d-empty-title">No tasks to visualize</div>'
      + '<div class="cube3d-empty-msg">Create some tasks and they\'ll appear here in 3D space.</div>'
      + '</div></div>';
    return;
  }

  // Clear loading spinner
  var container = document.getElementById('cube3d-container');
  if (container) container.innerHTML = '';

  // Build the scene
  _cube3dInitScene();
  _cube3dInitSimulation(nodes, links);
  _cube3dCreateNodes(nodes);
  _cube3dCreateEdges(links, nodes);
  _cube3dCreateFogPlanes();
  _cube3dCreateHUD();
  _cube3dSetupRaycaster();
  _cube3dSetupSearch();

  // Resize handler
  _cube3d.resizeHandler = _cube3dOnResize;
  window.addEventListener('resize', _cube3d.resizeHandler);

  // Start animation loop
  _cube3dAnimate();

  // After simulation settles, re-center camera on center of mass
  setTimeout(function() {
    if (_cube3d.generation !== myGen) return;
    if (_cube3d.controls && nodes.length > 0) {
      var cx = 0, cy = 0, cz = 0;
      for (var i = 0; i < nodes.length; i++) {
        cx += (nodes[i].x || 0);
        cy += (nodes[i].y || 0);
        cz += (nodes[i].z || 0);
      }
      cx /= nodes.length;
      cy /= nodes.length;
      cz /= nodes.length;
      _cube3d.controls.target.set(cx, cy, cz);
      _cube3d.camera.position.set(cx + 250, cy + 120, cz + 400);
      _cube3d.controls.update();
    }
  }, 2000);
}

/**
 * updateCube3DData(data) — Incremental data update.
 * Skips if revision matches current. Otherwise rebuilds nodes/edges.
 */
function updateCube3DData(data) {
  if (!_cube3d.scene) return;
  if (!data) return;
  if (_cube3d.currentRevision && data.revision === _cube3d.currentRevision) return;
  _cube3d.currentRevision = (data.revision) || null;

  var nodes = (data.nodes) || [];
  var links = (data.links) || [];

  // Update force simulation
  if (_cube3d.simulation) {
    _cube3d.simulation.nodes(nodes);
    _cube3d.simulation.force('link').links(links);
    _cube3d.simulation.alpha(0.3).restart();
  }

  // Recreate instanced mesh
  if (_cube3d.instancedMesh) {
    _cube3d.scene.remove(_cube3d.instancedMesh);
    _cube3d.instancedMesh.geometry.dispose();
    _cube3d.instancedMesh.material.dispose();
  }
  _cube3dCreateNodes(nodes);

  // Recreate edges and flow particles
  if (_cube3d.edgeLines) {
    _cube3d.scene.remove(_cube3d.edgeLines);
    _cube3d.edgeLines.geometry.dispose();
    _cube3d.edgeLines.material.dispose();
  }
  if (_cube3d.flowPoints) {
    _cube3d.scene.remove(_cube3d.flowPoints);
    _cube3d.flowPoints.geometry.dispose();
    _cube3d.flowPoints.material.dispose();
  }
  _cube3dCreateEdges(links, nodes);
}

/**
 * cleanupCube3D() — Full teardown of the 3D view.
 * Disposes all Three.js objects, stops simulation, removes handlers.
 */
function cleanupCube3D() {
  _cube3d.generation++;

  // Cancel animation frame
  if (_cube3d.animFrameId) {
    cancelAnimationFrame(_cube3d.animFrameId);
    _cube3d.animFrameId = null;
  }

  // Remove resize handler
  if (_cube3d.resizeHandler) {
    window.removeEventListener('resize', _cube3d.resizeHandler);
    _cube3d.resizeHandler = null;
  }

  // Remove search keyboard handler
  document.removeEventListener('keydown', _cube3dSearchKeyHandler);

  // Dispose orbit controls
  if (_cube3d.controls) {
    _cube3d.controls.dispose();
    _cube3d.controls = null;
  }

  // Stop force simulation
  if (_cube3d.simulation) {
    _cube3d.simulation.stop();
    _cube3d.simulation = null;
  }

  // Traverse and dispose all Three.js geometries/materials/textures
  if (_cube3d.scene) {
    _cube3d.scene.traverse(function(obj) {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        if (obj.material.map) obj.material.map.dispose();
        obj.material.dispose();
      }
    });
  }

  // Dispose WebGL renderer
  if (_cube3d.webglRenderer) {
    _cube3d.webglRenderer.dispose();
    _cube3d.webglRenderer.forceContextLoss();
    var canvas = _cube3d.webglRenderer.domElement;
    if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
    _cube3d.webglRenderer = null;
  }

  // Remove CSS3D renderer DOM
  if (_cube3d.css3dRenderer) {
    if (_cube3d.css3dRenderer.domElement && _cube3d.css3dRenderer.domElement.parentNode) {
      _cube3d.css3dRenderer.domElement.parentNode.removeChild(_cube3d.css3dRenderer.domElement);
    }
    _cube3d.css3dRenderer = null;
  }

  // Null all references
  _cube3d.scene = null;
  _cube3d.camera = null;
  _cube3d.instancedMesh = null;
  _cube3d.edgeLines = null;
  _cube3d.flowPoints = null;
  _cube3d.fogPlanes = [];
  _cube3d.fogLabels = [];
  _cube3d.nodeData = [];
  _cube3d.linkData = [];
  _cube3d.textSprites = [];
  _cube3d.css3dCards = [];
  _cube3d.workspacePanel = null;
  _cube3d.workspaceTaskId = null;
  _cube3d.searchActive = false;
  _cube3d.searchMatches = new Set();
  _cube3d.flyingTo = null;
  _cube3d.currentRevision = null;
  _cube3d._flowT = null;
  _cube3d._frameCount = 0;
}
