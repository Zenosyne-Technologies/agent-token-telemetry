// Behavioural harness for dashboard.html's client code, driven by
// tests/test_dashboard_client.py under the system `node` (no npm deps).
//
// Reads one JSON job on stdin:
//   {script, bannerTree, pageIds, hostile, mode, steps, fmtValues}
// - script: the page's inline <script> body, with a hook line injected at
//   the end of its IIFE exposing renderPriceWarning/fmtUSD (the REAL
//   functions in their real closure — any helper they call, wherever it is
//   defined in the page, comes along).
// - bannerTree: the #price-warn subtree parsed from the shipped markup.
// - pageIds: every other id in the shipped markup.
// - mode "direct": call renderPriceWarning(step) for each step.
//   mode "page": serve each step as the /api/data response — the page's own
//   load() -> renderAll() runs on the initial load, then each later step is
//   loaded by clicking #reset (exactly what a user refresh does).
//   mode "fmt": return fmtUSD(v) for each of fmtValues.
// Writes {snapshots, violations, fmt} JSON on stdout.
//
// The fake DOM is deliberately HOSTILE to the defects this banner has had:
// - ANY innerHTML/outerHTML/insertAdjacentHTML write to the banner subtree
//   or to a createElement()-made node throws (the property is an accessor,
//   so a helper function or el["inner"+"HTML"] hits it just the same);
//   outerHTML/insertAdjacentHTML/document.write throw on EVERY node, since
//   the page has no legitimate use for them.
// - innerHTML writes elsewhere on the page (which the page uses, with esc())
//   are allowed but taint-checked: a raw hostile model name reaching one
//   throws.
// - removing any shipped banner node (removeChild, remove, replaceChildren,
//   replaceWith, replaceChild, or textContent= on an ancestor) throws.
// Every violation is recorded before it is thrown, because the page's load()
// swallows exceptions into its "Connection lost" modal — which the snapshot
// also reports.
"use strict";
const fs = require("fs");
const vm = require("vm");

const job = JSON.parse(fs.readFileSync(0, "utf8"));
const violations = [];
function violate(msg) { violations.push(msg); throw new Error("fake-DOM violation: " + msg); }

let seq = 0;
class ClassList {
  constructor(el) { this.el = el; }
  _get() { return (this.el.className || "").split(/\s+/).filter(Boolean); }
  _set(a) { this.el.className = a.join(" "); }
  contains(c) { return this._get().includes(c); }
  add(...cs) { const a = this._get(); cs.forEach(c => { if (!a.includes(c)) a.push(c); }); this._set(a); }
  remove(...cs) { this._set(this._get().filter(c => !cs.includes(c))); }
  toggle(c, force) { const on = force === undefined ? !this.contains(c) : force; on ? this.add(c) : this.remove(c); return on; }
}

class Node {
  // kind: "banner" (shipped #price-warn subtree node — structural),
  //       "created" (document.createElement), "page" (any other page node),
  //       "text".
  constructor(tag, kind) {
    this.tagName = (tag || "").toUpperCase();
    this.kind = kind;
    this.structural = kind === "banner";
    this.children = [];
    this.parentNode = null;
    this.id = "";
    this.className = "";
    this.hidden = false;
    this.attrs = {};
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this._text = "";
    this._html = "";
    this.uid = ++seq;
    this.classList = new ClassList(this);
  }
  get strict() { return this.kind === "banner" || this.kind === "created" || this.inBanner(); }
  inBanner() { for (let n = this; n; n = n.parentNode) if (n.kind === "banner") return true; return false; }
  get firstChild() { return this.children[0] || null; }
  get childElementCount() { return this.children.filter(c => c.kind !== "text").length; }
  _structuralBelow() { return this.children.some(c => c.structural || c._structuralBelow()); }
  _detach(child, how) {
    if (child.structural || child._structuralBelow()) violate(`${how} removes shipped banner node ${describe(child)}`);
    const i = this.children.indexOf(child);
    if (i >= 0) this.children.splice(i, 1);
    child.parentNode = null;
  }
  _clear(how) { this.children.slice().forEach(c => this._detach(c, how)); }
  get textContent() { return this.kind === "text" ? this._text : this.children.map(c => c.textContent).join(""); }
  set textContent(v) {
    if (this.kind === "text") { this._text = String(v); return; }
    this._clear(`textContent= on ${describe(this)}`);
    const s = v == null ? "" : String(v);
    if (s !== "") { const t = new Node("#text", "text"); t._text = s; t.parentNode = this; this.children.push(t); }
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    if (this.strict) violate(`innerHTML write on ${describe(this)}`);
    const s = String(v);
    for (const h of job.hostile || []) if (s.includes(h)) violate(`raw hostile name reached innerHTML of ${describe(this)}: ${JSON.stringify(h)}`);
    this._clear(`innerHTML= on ${describe(this)}`);
    this._html = s;
  }
  get outerHTML() { return ""; }
  set outerHTML(v) { violate(`outerHTML write on ${describe(this)}`); }
  insertAdjacentHTML() { violate(`insertAdjacentHTML on ${describe(this)}`); }
  setHTMLUnsafe() { violate(`setHTMLUnsafe on ${describe(this)}`); }
  appendChild(c) {
    if (typeof c === "string") { const t = new Node("#text", "text"); t._text = c; c = t; }
    if (c.parentNode) c.parentNode._detach(c, "re-parent");
    c.parentNode = this; this.children.push(c); return c;
  }
  append(...cs) { cs.forEach(c => this.appendChild(c)); }
  removeChild(c) { this._detach(c, "removeChild"); return c; }
  replaceChild(n, o) { this._detach(o, "replaceChild"); this.appendChild(n); return o; }
  replaceChildren(...cs) { this._clear(`replaceChildren on ${describe(this)}`); cs.forEach(c => this.appendChild(c)); }
  remove() { if (this.parentNode) this.parentNode._detach(this, "remove()"); else if (this.structural) violate(`remove() on ${describe(this)}`); }
  replaceWith() { if (this.structural || this._structuralBelow()) violate(`replaceWith on ${describe(this)}`); if (this.parentNode) this.parentNode._detach(this, "replaceWith"); }
  setAttribute(k, v) { if (k === "hidden") this.hidden = true; else if (k === "class") this.className = String(v); else if (k === "id") this.id = String(v); else this.attrs[k] = String(v); }
  getAttribute(k) { return k === "hidden" ? (this.hidden ? "" : null) : k === "class" ? this.className : (k in this.attrs ? this.attrs[k] : null); }
  removeAttribute(k) { if (k === "hidden") this.hidden = false; else delete this.attrs[k]; }
  hasAttribute(k) { return this.getAttribute(k) !== null; }
  toggleAttribute(k, force) { const on = force === undefined ? !this.hasAttribute(k) : force; on ? this.setAttribute(k, "") : this.removeAttribute(k); return on; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 }; }
  _all() { return this.children.flatMap(c => c.kind === "text" ? [] : [c, ...c._all()]); }
  _match(sel) {
    sel = sel.trim();
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    if (sel.startsWith(".")) return this.classList.contains(sel.slice(1));
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) {
    if (this.kind === "page") return [];
    if (/[\s>\[:,]/.test(sel.trim())) throw new Error("fake DOM supports only #id/.class/tag selectors in the banner: " + sel);
    return this._all().filter(n => n._match(sel));
  }
  querySelector(sel) {
    // Off-banner page nodes are inert stand-ins (the page builds them from
    // innerHTML strings the harness does not parse), so a lookup inside one
    // yields another inert stand-in rather than null.
    if (this.kind === "page") return new Node("div", "page");
    return this.querySelectorAll(sel)[0] || null;
  }
}
function describe(n) { return n.id ? `#${n.id}` : `<${n.tagName.toLowerCase()}${n.className ? "." + n.className : ""}>`; }

// ---- build the document ----
const byId = new Map();
function build(t) {
  const n = new Node(t.tag, "banner");
  if (t.attrs.id) { n.id = t.attrs.id; byId.set(n.id, n); }
  if (t.attrs.class) n.className = t.attrs.class;
  n.hidden = "hidden" in t.attrs;
  for (const [k, v] of Object.entries(t.attrs)) if (!["id", "class", "hidden"].includes(k)) n.attrs[k] = v;
  for (const c of t.children) {
    if (typeof c === "string") { if (c.trim()) n.appendChild(c); }
    else n.appendChild(build(c));
  }
  return n;
}
const bannerRoot = build(job.bannerTree);
const shipped = [bannerRoot, ...bannerRoot._all()].map(n => ({ n, parent: n.parentNode }));
for (const id of job.pageIds) if (!byId.has(id)) { const n = new Node("div", "page"); n.id = id; byId.set(id, n); }

const document = {
  getElementById: id => byId.get(id) || null,
  createElement: tag => new Node(tag, "created"),
  createTextNode: s => { const t = new Node("#text", "text"); t._text = String(s); return t; },
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {},
  visibilityState: "visible",
  documentElement: new Node("html", "page"),
  body: new Node("body", "page"),
  write() { violate("document.write"); },
  writeln() { violate("document.writeln"); },
};

// ---- network + timers ----
const queue = (job.mode === "page" ? job.steps : []).slice();
function fakeFetch() {
  if (job.mode !== "page") return new Promise(() => {});   // never resolves: no page render
  const payload = queue.shift();
  if (payload === undefined) return Promise.reject(new Error("no more payloads"));
  const body = JSON.parse(JSON.stringify(payload));
  return Promise.resolve({ ok: true, status: 200, json: async () => body });
}
const noop = () => 0;

const sandbox = {
  document, fetch: fakeFetch, URLSearchParams, console,
  setTimeout: noop, clearTimeout: noop, setInterval: noop, clearInterval: noop,
};
sandbox.window = sandbox;
sandbox.window.open = noop; sandbox.window.close = noop;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(job.script, sandbox, { filename: "dashboard.html<script>" });
const hooks = sandbox.__dashboardTestHooks;

// ---- observation ----
function snapGroup(key) {
  const g = byId.get(`price-warn-${key}`), head = byId.get(`price-warn-${key}-head`), list = byId.get(`price-warn-${key}-list`);
  return {
    hidden: g ? g.hidden : null,
    heading: head ? head.textContent : null,
    rows: list ? list.children.map(li => ({
      tag: li.tagName.toLowerCase(),
      cells: li.children.map(c => ({
        tag: c.tagName.toLowerCase(), cls: c.className, text: c.textContent,
        elementChildren: c.childElementCount,
      })),
    })) : null,
  };
}
function snapshot(label, error) {
  const msg = byId.get("price-warn-msg");
  const modal = byId.get("conn-modal");
  return {
    label, error: error || null,
    boxHidden: bannerRoot.hidden,
    msg: msg ? msg.textContent : null,
    estimated: snapGroup("estimated"),
    unpriced: snapGroup("unpriced"),
    // every shipped banner node still attached to its shipped parent
    structureIntact: shipped.every(({ n, parent }) => n.parentNode === parent),
    modalShown: modal ? modal.classList.contains("show") : null,
    violations: violations.slice(),
  };
}
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };

(async () => {
  const out = { snapshots: [], fmt: null };
  if (job.mode === "fmt") {
    out.fmt = job.fmtValues.map(v => hooks.fmtUSD(v));
  } else if (job.mode === "direct") {
    job.steps.forEach((pw, i) => {
      let err = null;
      try { hooks.renderPriceWarning(pw); } catch (e) { err = String(e && e.message || e); }
      out.snapshots.push(snapshot(`step ${i}`, err));
    });
  } else if (job.mode === "page") {
    await settle();                                   // initial load() -> renderAll()
    out.snapshots.push(snapshot("step 0"));
    const reset = byId.get("reset");
    for (let i = 1; i < job.steps.length; i++) {
      (reset.listeners.click || []).forEach(f => f());
      await settle();
      out.snapshots.push(snapshot(`step ${i}`));
    }
  }
  out.violations = violations;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({ fatal: String(e && e.stack || e), violations })); });
