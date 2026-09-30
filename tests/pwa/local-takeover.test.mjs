// 로컬(iframe) 모드 화면 인계(v0.9.6) — terminal.html 이 서버 통지를 감지해 부모에 알리고, 앱은 탭을 '빼앗김'으로
// 그린 뒤 탭 클릭 → 확인창 → iframe 리로드로 되찾는다. 사장님 실기(2026-09-28)에서 드러난 로컬 모드 공백.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import { load, ev, stubEl, makeSandbox, extractScript } from "./harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const TERMINAL = join(here, "..", "..", "session_manager", "static", "terminal.html");
const A = "aaaaaaaa-1111-2222-3333-444444444444";
const NOTE = "다른 화면이 이 세션을 가져갔습니다";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };

// terminal.html 의 인라인 스크립트를 부모(iframe 호스트)가 있는 샌드박스에 부트한다
function bootTerminal({ id = A, fork = "" } = {}) {
  const sandbox = makeSandbox();
  Object.assign(sandbox.location, { pathname: "/terminal", search: `?id=${id}&skip=1${fork ? "&fork=" + fork : ""}`,
    origin: "http://127.0.0.1:5100", host: "127.0.0.1:5100", protocol: "http:" });
  for (const k of ["sid", "dot", "status", "termwrap", "term", "delfork", "cwd"]) sandbox.document._register(k, stubEl("div"));
  const posted = [];
  sandbox.parent = { postMessage: (m, origin) => posted.push({ m, origin }) };
  let ws;
  sandbox.WebSocket = class { constructor(url) { this.url = url; this.readyState = 1; this.sent = []; ws = this; } send(s) { this.sent.push(s); } close() {} };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(extractScript(readFileSync(TERMINAL, "utf-8")), ctx, { filename: "terminal.html" });
  return { ctx, ws, posted, status: sandbox.document.getElementById("status"), term: vm.runInContext("term", ctx) };
}

test("terminal.html: 붙으면 attached, 통지를 보면 taken 을 부모에 알리고(같은 origin) 끊길 때 노란 안내", () => {
  const { ws, posted, status, term } = bootTerminal();
  assert.ok(ws.url.endsWith(`/ws/terminal/${A}?skip=1`));
  ws.onopen();
  assert.equal(JSON.stringify(posted.at(-1)), JSON.stringify({ m: { type: "clewpath-term", event: "attached", id: A, fork: "" }, origin: "http://127.0.0.1:5100" }));
  ws.onmessage({ data: "hello\r\n" });
  assert.equal(posted.length, 1);
  ws.onmessage({ data: `\r\n\x1b[93m[ClewPath] ${NOTE} — 여기서는 더 입력할 수 없습니다.\x1b[0m\r\n` });
  assert.equal(posted.at(-1).m.event, "taken");
  assert.equal(status.textContent, "다른 화면이 보는 중");
  ws.onmessage({ data: `${NOTE} 다시` });                       // 중복 통지는 한 번만
  assert.equal(posted.filter((p) => p.m.event === "taken").length, 1);
  ws.onclose();
  const last = term.written.at(-1);
  assert.ok(last.includes("다른 기기에서 보는 중") && last.includes("\x1b[93m"), "빼앗긴 종료는 노란 안내");
  assert.ok(!last.includes("연결이 끊겼습니다"));
});

test("terminal.html: 통지 없이 끊기면 기존 회색 '재연결' 안내, 포크 모드는 fork id 를 함께 알린다", () => {
  const a = bootTerminal();
  a.ws.onclose();
  assert.ok(a.term.written.at(-1).includes("연결이 끊겼습니다") && a.term.written.at(-1).includes("\x1b[90m"));
  assert.equal(a.status.textContent, "연결 종료");
  const f = bootTerminal({ fork: "ffffffff-1111-2222-3333-444444444444" });
  f.ws.onopen();
  assert.equal(f.posted.at(-1).m.fork, "ffffffff-1111-2222-3333-444444444444");
});

function pinPaneDetail(ctx) {
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  return pd;
}

test("앱(로컬): iframe 의 taken 통지 → 탭 taken(점선) → 탭 클릭 시 확인창 → 취소면 그대로, 확인이면 iframe 리로드로 되찾음", async () => {
  const ctx = await load({ location: LOCAL });
  assert.equal(ev(ctx, "MODE"), "local");
  pinPaneDetail(ctx);
  const handlers = ctx.__winListeners.message || [];
  assert.ok(handlers.length >= 1, "로컬 부팅이 message 리스너(터미널 통지)를 등록 — 팝업 동기화 리스너는 0.10.0(E-4)에서 제거");
  const fire = (origin, data) => handlers.forEach((h) => h({ origin, data }));
  ev(ctx, "TABS.list = []; TABS.active = null");
  await ctx.loadTerminal({ session_id: A, title: "A" }, false);
  const key = ev(ctx, "TABS.active");
  const rec = () => ev(ctx, "TABS.list").find((t) => t.key === key);
  const tab = ev(ctx, `termView.tabs[${JSON.stringify(key)}]`);
  let reloads = 0; let src = tab.frame.src;
  tab.frame = { tagName: "IFRAME", get src() { return src; }, set src(v) { src = v; reloads++; } };
  let syncs = 0; ev(ctx, "tabBadge.sync = () => {}");
  const strip = ev(ctx, "termView.strip");
  // 다른 origin / 다른 종류 메시지는 무시
  fire("https://evil.example", { type: "clewpath-term", event: "taken", id: A, fork: "" });
  fire("http://127.0.0.1:5100", { type: "clewpath-term", event: "attached", id: A, fork: "" });
  fire("http://127.0.0.1:5100", { type: "clewpath_pcs", pcs: [] });
  assert.equal(!!rec().taken, false);
  // 진짜 통지
  fire("http://127.0.0.1:5100", { type: "clewpath-term", event: "taken", id: A, fork: "" });
  assert.equal(rec().taken, true);
  await new Promise((r) => setTimeout(r, 350));                       // renderTabBarSoon
  const chip = strip.children.find((c) => String(c.className).includes("lchip"));
  assert.ok(chip.classList.contains("taken"), "칩이 점선(taken)");
  assert.ok(chip.title.startsWith("[다른 기기에서 보는 중 — 누르면 가져오기] "));
  // 탭 클릭(=switchTab) → 확인창. 취소: 리로드 없음, taken 유지
  let asked = 0; ctx.confirm = () => { asked++; return false; };
  ctx.switchTab(key); await new Promise((r) => setTimeout(r, 0));
  assert.equal(asked, 1); assert.equal(reloads, 0); assert.equal(rec().taken, true);
  // 확인: iframe 리로드(재접속) + taken 해제
  ctx.confirm = () => { asked++; return true; };
  ctx.switchTab(key); await new Promise((r) => setTimeout(r, 0));
  assert.equal(asked, 2); assert.equal(reloads, 1); assert.equal(rec().taken, false);
  // 되찾은 뒤 같은 탭을 또 눌러도(이미 전경) 묻지도 리로드하지도 않는다
  ctx.switchTab(key); await new Promise((r) => setTimeout(r, 0));
  assert.equal(asked, 2); assert.equal(reloads, 1);
  // 모르는 세션 통지는 무시
  fire("http://127.0.0.1:5100", { type: "clewpath-term", event: "taken", id: "nope", fork: "" });
  assert.equal(rec().taken, false);
});

test("CSS 가드: 빼앗긴 탭은 전경(.sel)일 때도 점선·주황이 보여야 한다(실선 outline 에 가려진 사고, 0.9.7)", () => {
  const css = readFileSync(join(here, "..", "..", "pwa", "index.html"), "utf-8");
  assert.match(css, /\.term-tabs \.lchip\.taken\{[^}]*border:1px dashed var\(--warn\)/);
  assert.match(css, /\.term-tabs \.lchip\.taken\.sel\{[^}]*outline:2px dashed var\(--warn\)/, "전경 outline 도 점선");
  assert.match(css, /\.lchip\.taken \.lname::before\{content:'👀 '\}/);
});
