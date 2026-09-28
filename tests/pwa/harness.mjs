// PWA 인라인 스크립트를 node:vm 에 통째로 부트하는 헤드리스 하네스 (v0.9.4 테스트 인프라).
// 제품 코드는 손대지 않는다: index.html 은 릴레이가 그대로 라이브 서빙하므로 파일을 쪼갤 수 없어,
// 대신 DOM/브라우저 API 를 Proxy 스텁으로 흉내 내 부트시키고 순수 함수·라우팅 함수를 검증한다.
// 사용: const ctx = await load(); ctx.sessionBadge(...) / ev(ctx, "TABS.list = [...]")
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
export const INDEX = join(here, "..", "..", "pwa", "index.html");

export function extractScript(html) {
  const m = html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/);
  if (!m) throw new Error("inline <script> not found");
  return m[1];
}

// ---- DOM 스텁: 어떤 프로퍼티든 읽으면 동작하고(함수는 no-op), 대입은 기억한다 ----
function makeClassList(set) {
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    toggle: (c, force) => { const on = force === undefined ? !set.has(c) : !!force; on ? set.add(c) : set.delete(c); return on; },
    contains: (c) => set.has(c),
    toString: () => [...set].join(" "),
  };
}

export function stubEl(tag = "div") {
  const classes = new Set();
  const store = {
    tagName: tag.toUpperCase(), classList: makeClassList(classes), style: new Proxy({}, { get: (t, k) => (k === "setProperty" ? (a, b) => { t[a] = b; } : k === "getPropertyValue" ? (a) => t[a] || "" : (t[k] ?? "")) }),
    dataset: {}, children: [], childNodes: [], title: "", textContent: "", innerHTML: "", value: "", id: "", hidden: false,
    offsetHeight: 0, offsetWidth: 0, scrollTop: 0, scrollHeight: 0, clientHeight: 0, scrollWidth: 0,
    appendChild(c) { store.children.push(c); return c; }, prepend(c) { store.children.unshift(c); return c; },
    insertBefore(c) { store.children.push(c); return c; }, removeChild(c) { return c; }, remove() {},
    querySelector() { return stubEl(); }, querySelectorAll() { return []; }, closest() { return null; },
    getBoundingClientRect() { return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }; },
    addEventListener() {}, removeEventListener() {}, setAttribute(k, v) { store[k] = v; }, getAttribute(k) { return store[k] ?? null; },
    removeAttribute() {}, focus() {}, blur() {}, click() {}, scrollIntoView() {}, contains() { return false; },
    get className() { return [...classes].join(" "); }, set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
    // 텍스트 노드까지 합친 표시 문자열(테스트가 칩 라벨을 찾을 때) — 브라우저 textContent 와 같은 의미
    get text() { return (store.textContent || "") + store.children.map((c) => (c.nodeType === 3 ? c.textContent : (c.text ?? ""))).join(""); },
  };
  return new Proxy(store, {
    get(t, k) { if (k in t) return t[k]; if (typeof k === "symbol") return undefined; return () => stubEl(); },
    set(t, k, v) {
      // innerHTML/textContent 대입은 브라우저처럼 자식을 비운다(재렌더 패턴 `x.innerHTML=''` 지원)
      if (k === "innerHTML" || k === "textContent") { t.children.length = 0; }
      t[k] = v; return true;
    },
  });
}

class MemStorage {
  constructor() { this.m = new Map(); }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); }
  clear() { this.m.clear(); }
  key(i) { return [...this.m.keys()][i] ?? null; }
  get length() { return this.m.size; }
}

export function makeSandbox() {
  const byId = new Map();
  const document = stubEl("document");
  // <head> 에 붙는 <script>/<link> 는 다음 틱에 onload 를 쏜다 — loadXterm() 같은 동적 로더가 영원히
  // 기다리지 않게(실제 파일은 안 읽는다; Terminal/FitAddon 은 샌드박스 스텁이 대신한다).
  const head = stubEl("head");
  const headAppend = head.appendChild;
  head.appendChild = (c) => { headAppend(c); setTimeout(() => { if (typeof c.onload === "function") c.onload(); }, 0); return c; };
  Object.assign(document, {
    hidden: false, body: stubEl("body"), documentElement: stubEl("html"), head,
    getElementById: (id) => byId.get(id) ?? null,
    createElement: (tag) => stubEl(tag), createTextNode: (t) => ({ textContent: t, nodeType: 3 }),
    addEventListener() {}, removeEventListener() {}, cookie: "",
    _register: (id, el) => byId.set(id, el),
  });
  // setInterval 은 돌리지 않고 기록만 — 테스트가 주기 콜백(목록 20초 갱신 등)을 골라 직접 호출한다
  const intervals = [];
  const sandbox = {
    document, console, setTimeout, clearTimeout, clearInterval() {},
    setInterval: (f, ms) => { intervals.push({ f, ms }); return intervals.length; }, __intervals: intervals,
    queueMicrotask, requestAnimationFrame: (f) => setTimeout(f, 0), cancelAnimationFrame() {},
    navigator: { userAgent: "node-test", language: "ko", onLine: true, platform: "Win32" },   // serviceWorker 없음
    location: { hash: "", pathname: "/relay/app", search: "", origin: "https://test.local", href: "https://test.local/relay/app", protocol: "https:", host: "test.local", reload() {} },
    history: { replaceState() {}, pushState() {} },
    localStorage: new MemStorage(), sessionStorage: new MemStorage(),
    fetch: () => new Promise(() => {}), WebSocket: class { constructor() { this.readyState = 3; } send() {} close() {} },
    Notification: { permission: "default", requestPermission: async () => "default" },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    screen: { width: 1920, height: 1080 }, innerWidth: 1280, innerHeight: 800, devicePixelRatio: 1,
    confirm: () => true, alert() {}, prompt: () => null, open: () => null, scrollTo() {},
    URL, URLSearchParams, TextEncoder, TextDecoder, crypto: globalThis.crypto, btoa, atob, structuredClone, performance,
    Intl, AbortController, Blob, FormData: class {}, Event: class { constructor(t) { this.type = t; } }, CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o?.detail; } },
    HTMLElement: class {}, Element: class {}, Node: class {}, Image: class {}, Audio: class { play() { return Promise.resolve(); } },
    // xterm 스텁: 어떤 메서드든 no-op(이벤트 등록은 disposable 반환), 쓴 내용은 written 에 쌓는다
    Terminal: class { constructor() {
      const t = { rows: 24, cols: 80, options: {}, element: stubEl(), written: [], buffer: { active: { length: 0, cursorY: 0, baseY: 0 } },
                  write(s) { t.written.push(String(s)); }, reset() { t.written.push("\u0000RESET"); } };
      return new Proxy(t, { get: (o, k) => (k in o ? o[k] : () => ({ dispose() {} })) });
    } },
    FitAddon: { FitAddon: class { fit() {} } },
  };
  // window 이벤트는 기록만(테스트가 `__winListeners.message` 등을 직접 호출) — termView.mount 가 등록/해제한다
  const winListeners = {};
  sandbox.__winListeners = winListeners;
  sandbox.addEventListener = (t, f) => { (winListeners[t] ||= []).push(f); };
  sandbox.removeEventListener = (t, f) => { if (winListeners[t]) winListeners[t] = winListeners[t].filter((x) => x !== f); };
  sandbox.dispatchEvent = () => true;
  sandbox.ResizeObserver = class { observe() {} disconnect() {} unobserve() {} };
  sandbox.window = sandbox; sandbox.self = sandbox; sandbox.globalThis = sandbox;
  return sandbox;
}

// location 을 덮어쓰면 MODE 추론이 바뀐다(기본 /relay/app = relay, `{pathname:'/app'}` = local)
export async function load({ hash = "", location = null, navigator = null } = {}) {
  const html = readFileSync(INDEX, "utf-8");
  const js = extractScript(html);
  const sandbox = makeSandbox();
  if (location) Object.assign(sandbox.location, location);
  if (navigator) Object.assign(sandbox.navigator, navigator);   // 예: serviceWorker 스텁(알림 클릭 message 계약)
  sandbox.location.hash = hash;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(js, ctx, { filename: "index.html" });
  await new Promise((r) => setTimeout(r, 0));           // 부트 중 microtask 소진
  return ctx;
}

// 스크립트 최상위 let/const 는 전역 객체 프로퍼티가 아니라 컨텍스트의 선언 레코드에 산다 →
// 같은 컨텍스트에서 코드를 평가해 읽고 쓴다.
export function ev(ctx, code) {
  return vm.runInContext(code, ctx);
}
