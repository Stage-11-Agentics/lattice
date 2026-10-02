"use strict";

// A small DOM for driving dashboard view code under node:test, zero deps.
// It parses the markup the views build (tags, quoted attributes, entities, void
// and self-closing elements), matches simple CSS selectors (tag, #id, .class,
// [attr], [attr=value], :not(), descendant and child combinators, lists),
// dispatches events through capture and bubble phases, and models the parts of
// focus, scrolling and <video> playback the issue view reads. Layout is not
// modelled: clientWidth and offsetHeight are fixed numbers.

const VOID = new Set(["area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function decode(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (all, name) => {
    if (name[0] === "#") {
      const code = name[1] === "x" || name[1] === "X" ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
      return String.fromCodePoint(code);
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, name) ? ENTITIES[name] : all;
  });
}

function escapeText(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

class Event {
  constructor(type, init) {
    init = init || {};
    this.type = type;
    this.bubbles = !!init.bubbles;
    this.cancelable = !!init.cancelable;
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
    this._stop = false;
    Object.keys(init).forEach((key) => {
      if (!(key in this)) this[key] = init[key];
    });
  }
  preventDefault() { if (this.cancelable) this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; }
}

class EventTarget {
  constructor() { this._listeners = []; }
  addEventListener(type, fn, options) {
    const capture = options === true || !!(options && options.capture);
    const once = !!(options && options.once);
    if (this._listeners.some((l) => l.type === type && l.fn === fn && l.capture === capture)) return;
    this._listeners.push({ type, fn, capture, once });
  }
  removeEventListener(type, fn, options) {
    const capture = options === true || !!(options && options.capture);
    this._listeners = this._listeners.filter((l) => !(l.type === type && l.fn === fn && l.capture === capture));
  }
  _fire(event, phase) {
    const listeners = this._listeners.filter((l) => l.type === event.type &&
      (phase === "target" || (phase === "capture") === l.capture));
    for (const listener of listeners) {
      if (listener.once) this.removeEventListener(listener.type, listener.fn, { capture: listener.capture });
      event.currentTarget = this;
      listener.fn.call(this, event);
    }
    const handler = this["on" + event.type];
    if (phase !== "capture" && typeof handler === "function" && (phase === "target" || event.bubbles)) handler.call(this, event);
  }
  _path() { return [this]; }
  dispatchEvent(event) {
    event.target = this;
    const path = this._path();
    for (let i = path.length - 1; i > 0 && !event._stop; i--) path[i]._fire(event, "capture");
    if (!event._stop) this._fire(event, "target");
    if (event.bubbles) for (let i = 1; i < path.length && !event._stop; i++) path[i]._fire(event, "bubble");
    return !event.defaultPrevented;
  }
}

class Node extends EventTarget {
  constructor(document) {
    super();
    this.ownerDocument = document;
    this.parentNode = null;
    this.childNodes = [];
  }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get children() { return this.childNodes.filter((node) => node instanceof Element); }
  get firstElementChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument;
  }
  _path() {
    const path = [];
    let node = this;
    while (node) { path.push(node); node = node.parentNode; }
    if (path[path.length - 1] === this.ownerDocument && this.ownerDocument.defaultView) path.push(this.ownerDocument.defaultView);
    return path;
  }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, ref) {
    if (child instanceof DocumentFragment) {
      child.childNodes.slice().forEach((node) => this.insertBefore(node, ref));
      return child;
    }
    if (child.parentNode) child.parentNode.removeChild(child);
    const index = ref ? this.childNodes.indexOf(ref) : -1;
    if (index < 0) this.childNodes.push(child);
    else this.childNodes.splice(index, 0, child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index >= 0) this.childNodes.splice(index, 1);
    child.parentNode = null;
    if (this.ownerDocument && child.contains(this.ownerDocument._active)) this.ownerDocument._active = null;
    return child;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(node) {
    if (!this.parentNode) return;
    const parent = this.parentNode;
    parent.insertBefore(node, this);
    parent.removeChild(this);
  }
  contains(node) {
    while (node) { if (node === this) return true; node = node.parentNode; }
    return false;
  }
  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }
  set textContent(value) {
    this.childNodes.slice().forEach((node) => this.removeChild(node));
    if (value !== "" && value != null) this.appendChild(new Text(this.ownerDocument, String(value)));
  }
  querySelectorAll(selector) {
    const groups = parseSelectorList(selector);
    const out = [];
    const walk = (node) => {
      node.children.forEach((child) => {
        if (groups.some((group) => matchComplex(child, group, this))) out.push(child);
        walk(child);
      });
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class Text extends Node {
  constructor(document, data) { super(document); this.data = data; this.nodeType = 3; }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
  get children() { return []; }
  _html() { return escapeText(this.data); }
}

class ClassList {
  constructor(element) { this.element = element; }
  _list() { return (this.element.getAttribute("class") || "").split(/\s+/).filter(Boolean); }
  _set(list) { this.element.setAttribute("class", list.join(" ")); }
  contains(name) { return this._list().includes(name); }
  add(...names) { const list = this._list(); names.forEach((n) => { if (!list.includes(n)) list.push(n); }); this._set(list); }
  remove(...names) { this._set(this._list().filter((n) => !names.includes(n))); }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : !!force;
    if (on) this.add(name); else this.remove(name);
    return on;
  }
}

class Style {
  constructor() { this._props = {}; }
  setProperty(name, value) { this._props[name] = String(value); }
  getPropertyValue(name) { return this._props[name] || ""; }
  removeProperty(name) { delete this._props[name]; }
}

const REFLECTED = ["id", "title", "type", "href", "src", "alt", "placeholder", "name", "rel", "accept"];

class Element extends Node {
  constructor(document, tag) {
    super(document);
    this.nodeType = 1;
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.style = new Style();
    this.classList = new ClassList(this);
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.clientWidth = 900;
    this.clientHeight = 600;
    this.offsetHeight = 48;
    this.disabled = false;
    this.isContentEditable = false;
    this.dataset = new Proxy({}, {
      get: (_t, key) => this.getAttribute("data-" + String(key).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())) ?? undefined,
      set: (_t, key, value) => { this.setAttribute("data-" + String(key).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()), value); return true; },
    });
    if (this.localName === "template") this.content = new DocumentFragment(document);
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) { this.attributes.set(name.toLowerCase(), String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get innerHTML() { return this.childNodes.map((node) => node._html()).join(""); }
  set innerHTML(html) {
    const target = this.localName === "template" ? this.content : this;
    target.childNodes.slice().forEach((node) => target.removeChild(node));
    parseInto(target, String(html), this.ownerDocument);
  }
  get outerHTML() { return this._html(); }
  _html() {
    const attrs = Array.from(this.attributes).map(([k, v]) => " " + k + '="' + v.replace(/&/g, "&amp;").replace(/"/g, "&quot;") + '"').join("");
    if (VOID.has(this.localName)) return "<" + this.localName + attrs + ">";
    return "<" + this.localName + attrs + ">" + this.innerHTML + "</" + this.localName + ">";
  }
  matches(selector) { return parseSelectorList(selector).some((group) => matchComplex(this, group, null)); }
  closest(selector) {
    let node = this;
    while (node instanceof Element) { if (node.matches(selector)) return node; node = node.parentNode; }
    return null;
  }
  focus() {
    const doc = this.ownerDocument;
    if (doc._active === this) return;
    const previous = doc._active;
    doc._active = this;
    if (previous) previous.dispatchEvent(new Event("blur"));
    this.dispatchEvent(new Event("focus"));
  }
  blur() {
    const doc = this.ownerDocument;
    if (doc._active !== this) return;
    doc._active = null;
    this.dispatchEvent(new Event("blur"));
  }
  click() { this.dispatchEvent(new Event("click", { bubbles: true, cancelable: true })); }
  select() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, width: this.clientWidth, height: this.offsetHeight, right: this.clientWidth, bottom: this.offsetHeight }; }
  getContext() { return { drawImage() {} }; }
}
REFLECTED.forEach((name) => {
  Object.defineProperty(Element.prototype, name, {
    get() { return this.getAttribute(name) || ""; },
    set(value) { this.setAttribute(name, value); },
  });
});

class VideoElement extends Element {
  constructor(document, tag) {
    super(document, tag);
    this.paused = true;
    this.ended = false;
    this.currentTime = 0;
    this.duration = NaN;
    this.controls = false;
    this.muted = false;
    this.plays = 0;
  }
  play() { this.paused = false; this.plays += 1; return Promise.resolve(); }
  pause() { this.paused = true; }
  get poster() { return this.getAttribute("poster") || ""; }
  set poster(value) { this.setAttribute("poster", value); }
}

class FieldElement extends Element {
  constructor(document, tag) { super(document, tag); this._value = null; }
  get value() {
    if (this._value !== null) return this._value;
    return this.localName === "textarea" ? decode(super.textContent) : (this.getAttribute("value") || "");
  }
  set value(value) { this._value = String(value); }
  get files() { return this._files || []; }
  set files(list) { this._files = list; }
}

class DocumentFragment extends Node {
  constructor(document) { super(document); }
  _html() { return this.childNodes.map((node) => node._html()).join(""); }
}

class Document extends Node {
  constructor() {
    super(null);
    this.ownerDocument = this;
    this._active = null;
    this.documentElement = this.createElement("html");
    this.head = this.createElement("head");
    this.body = this.createElement("body");
    this.appendChild(this.documentElement);
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this.visibilityState = "visible";
  }
  get activeElement() { return this._active || this.body; }
  createElement(tag) {
    const name = tag.toLowerCase();
    if (name === "video" || name === "audio") return new VideoElement(this, name);
    if (name === "input" || name === "textarea" || name === "select") return new FieldElement(this, name);
    return new Element(this, name);
  }
  createTextNode(text) { return new Text(this, text); }
  createDocumentFragment() { return new DocumentFragment(this); }
  getElementById(id) {
    const find = (node) => {
      for (const child of node.children) {
        if (child.getAttribute("id") === id) return child;
        const hit = find(child);
        if (hit) return hit;
      }
      return null;
    };
    return find(this);
  }
  execCommand() { return true; }
}

// ---- HTML parsing ----
function parseInto(parent, html, document) {
  const stack = [parent];
  const top = () => stack[stack.length - 1];
  let i = 0;
  while (i < html.length) {
    if (html.startsWith("<!--", i)) {
      const end = html.indexOf("-->", i + 4);
      i = end < 0 ? html.length : end + 3;
    } else if (html.startsWith("</", i)) {
      const end = html.indexOf(">", i);
      const name = html.slice(i + 2, end).trim().toLowerCase();
      for (let s = stack.length - 1; s > 0; s--) {
        if (stack[s].localName === name) { stack.length = s; break; }
      }
      i = end + 1;
    } else if (html[i] === "<" && /[a-zA-Z]/.test(html[i + 1] || "")) {
      const match = /^<([a-zA-Z][\w-]*)/.exec(html.slice(i));
      const name = match[1].toLowerCase();
      const element = document.createElement(name);
      let j = i + match[0].length;
      let selfClosing = false;
      for (;;) {
        while (/\s/.test(html[j])) j++;
        if (html[j] === ">") { j++; break; }
        if (html.startsWith("/>", j)) { selfClosing = true; j += 2; break; }
        const attr = /^([^\s=>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/.exec(html.slice(j));
        if (!attr) { j++; continue; }
        const value = attr[2] ?? attr[3] ?? attr[4] ?? "";
        element.setAttribute(attr[1], decode(value));
        j += attr[0].length;
      }
      top().appendChild(element);
      if (!VOID.has(name) && !selfClosing) {
        const target = name === "template" ? element.content : element;
        if (name === "template") {
          const close = html.indexOf("</template>", j);
          parseInto(target, html.slice(j, close), document);
          j = close + "</template>".length;
        } else stack.push(element);
      }
      i = j;
    } else {
      const next = html.indexOf("<", i + 1);
      const end = next < 0 ? html.length : next;
      top().appendChild(new Text(document, decode(html.slice(i, end))));
      i = end;
    }
  }
}

// ---- selectors ----
function splitTop(text, separator) {
  const parts = [];
  let depth = 0, quote = null, start = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote) { if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") quote = c;
    else if (c === "(" || c === "[") depth++;
    else if (c === ")" || c === "]") depth--;
    else if (depth === 0 && separator(c)) { parts.push(text.slice(start, i)); start = i + 1; }
  }
  parts.push(text.slice(start));
  return parts;
}

const selectorCache = new Map();
function parseSelectorList(selector) {
  if (selectorCache.has(selector)) return selectorCache.get(selector);
  const groups = splitTop(selector, (c) => c === ",").map((part) => {
    const spaced = part.replace(/\s*>\s*/g, " > ").trim();
    const tokens = splitTop(spaced, (c) => c === " ").filter(Boolean);
    const steps = [];
    let combinator = " ";
    tokens.forEach((token) => {
      if (token === ">") { combinator = ">"; return; }
      steps.push({ combinator, compound: parseCompound(token) });
      combinator = " ";
    });
    return steps;
  });
  selectorCache.set(selector, groups);
  return groups;
}

function parseCompound(token) {
  const parts = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  let i = 0;
  const tag = /^[a-zA-Z*][\w-]*/.exec(token);
  if (tag) { parts.tag = tag[0] === "*" ? null : tag[0].toLowerCase(); i = tag[0].length; }
  while (i < token.length) {
    const rest = token.slice(i);
    let m;
    if ((m = /^#([\w-]+)/.exec(rest))) parts.id = m[1];
    else if ((m = /^\.([\w-]+)/.exec(rest))) parts.classes.push(m[1]);
    else if ((m = /^\[([\w-]+)(?:([~^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/.exec(rest))) {
      parts.attrs.push({ name: m[1], op: m[2] || null, value: m[3] ?? m[4] ?? (m[5] !== undefined ? m[5].trim() : null) });
    } else if ((m = /^:not\(/.exec(rest))) {
      let depth = 1, j = m[0].length;
      while (j < rest.length && depth) { if (rest[j] === "(") depth++; else if (rest[j] === ")") depth--; j++; }
      parts.nots.push(parseCompound(rest.slice(m[0].length, j - 1)));
      m = [rest.slice(0, j)];
    } else throw new Error("mini-dom: unsupported selector " + token);
    i += m[0].length;
  }
  return parts;
}

function matchCompound(element, parts) {
  if (!(element instanceof Element)) return false;
  if (parts.tag && element.localName !== parts.tag) return false;
  if (parts.id && element.getAttribute("id") !== parts.id) return false;
  for (const name of parts.classes) if (!element.classList.contains(name)) return false;
  for (const attr of parts.attrs) {
    const value = element.getAttribute(attr.name);
    if (value === null) return false;
    if (attr.op === "=" && value !== attr.value) return false;
    if (attr.op === "^=" && !value.startsWith(attr.value)) return false;
    if (attr.op === "$=" && !value.endsWith(attr.value)) return false;
    if (attr.op === "*=" && !value.includes(attr.value)) return false;
    if (attr.op === "~=" && !value.split(/\s+/).includes(attr.value)) return false;
  }
  for (const not of parts.nots) if (matchCompound(element, not)) return false;
  return true;
}

function matchComplex(element, steps, scope) {
  const match = (node, index) => {
    if (!matchCompound(node, steps[index].compound)) return false;
    if (index === 0) return true;
    const combinator = steps[index].combinator;
    let parent = node.parentNode;
    if (combinator === ">") return parent instanceof Element && parent !== scope && match(parent, index - 1);
    while (parent instanceof Element) {
      if (match(parent, index - 1)) return true;
      parent = parent.parentNode;
    }
    return false;
  };
  return match(element, steps.length - 1);
}

// A window-like global for one test: document, a few browser APIs, and the
// globals the views read. install() points globalThis at it; restore() undoes.
function createWindow() {
  const document = new Document();
  const window = new EventTarget();
  document.defaultView = window;
  Object.assign(window, {
    document,
    innerWidth: 1500,
    innerHeight: 900,
    Event,
    KeyboardEvent: Event,
    ClipboardEvent: Event,
    DragEvent: Event,
  });
  return window;
}

const INSTALLED = ["window", "document", "Event", "KeyboardEvent"];
function install(window) {
  const saved = {};
  INSTALLED.forEach((name) => { saved[name] = Object.getOwnPropertyDescriptor(globalThis, name); });
  globalThis.window = globalThis;
  globalThis.document = window.document;
  globalThis.Event = Event;
  globalThis.KeyboardEvent = Event;
  globalThis.innerHeight = window.innerHeight;
  window.document.defaultView = globalThis;
  const listeners = new EventTarget();
  globalThis.addEventListener = listeners.addEventListener.bind(listeners);
  globalThis.removeEventListener = listeners.removeEventListener.bind(listeners);
  globalThis._fire = listeners._fire.bind(listeners);
  return function restore() {
    INSTALLED.forEach((name) => {
      if (saved[name]) Object.defineProperty(globalThis, name, saved[name]);
      else delete globalThis[name];
    });
    delete globalThis.addEventListener;
    delete globalThis.removeEventListener;
    delete globalThis._fire;
  };
}

module.exports = { Document, Element, Event, createWindow, install };
