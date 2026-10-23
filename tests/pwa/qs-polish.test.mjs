// 바로가기 6단계 다듬기(0.32.3) — docs/designs/quick-switch.md §8 6단계 · TODOS 0.31.x 다듬기. 고치기 전에 이 파일로 실패를 먼저 확인했다(T105~).
// 머리 줄 제목 · PC 꺼짐과 '받는 중' 구분 · 목록 줄·알림 길의 '가져간 뒤 끝난 탭' · 탭 닫은 뒤 최근 본 탭 · 🖥 볼 일 점 · 파일 ⟳·이동 경로 ·
// Ctrl+Shift+B · 이름 바꾸기·삭제가 보던 화면을 지우지 않음 · 업데이트 확인의 unsupported.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const RENDER = 350;   // 터미널 attach 의 resize 타이머(80·300ms)·renderTabBarSoon(300ms)이 지나가게
const QS = 150;       // 바로가기 단추 다시 그리기(100ms 로 모음)가 지나가게
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const RK = "e:/p";
const team = (code, name, gid, mgr = "m1") => ({ code, name, kind: "team", monitor_group_id: gid, members: [{ member_role: "manager", session_id: mgr, alias: "관리" }] });

// 이 PC 의 기억(새로고침 전에 남긴 것) — qs-ui-review.test.mjs 와 같은 모양. null 이면 그 기억 없음
const DEF = { tabs: [A], files: ["a.py"], viewer: { sid: B, kind: "viewer", name: "B" }, monitor: { team: "T1", gid: "g1", name: "팀1" } };
function seed(sb, pc, mem) {
  const m = { ...DEF, ...mem };
  if (m.tabs) sb.localStorage.setItem("sm_tabs_" + pc, JSON.stringify({ active: m.tabs[0], list: m.tabs.map((k) => ({ key: k, sid: k })), mru: m.tabs }));
  if (m.files) sb.localStorage.setItem("sm_fb_state2_" + pc, JSON.stringify({ open: [], sort: "name",
    tabs: m.files.map((f) => ({ root: RK, path: f, pinned: true })), active: RK + "|" + m.files[0] }));
  const pane = {};
  if (m.viewer) pane.viewer = { ...m.viewer, at: Date.now() };
  if (m.monitor) pane.monitor = { ...m.monitor, at: Date.now() };
  sb.localStorage.setItem("sm_pane_" + pc, JSON.stringify(pane));
}

// qs-ui-review.test.mjs 의 boot 와 같다 — 머리 줄 단추 칸(#qs)·제목 단추(#qs-title)는 부트 전에 고정(제목 누름은 부트 때 건다).
// list: 첫 목록(T.list)을 바꿀 때 — 'offline' 이면 PC 꺼짐(agent_offline)으로 실패, 'fail' 이면 다른 연결 오류
async function boot({ width = 1400, local = false, mem = null, ready = true, pre = "", setup = null } = {}) {
  const pc = local ? "local" : "relay";
  const ctx = await load({ ...(local ? { location: LOCAL } : {}), setup: (sb) => {
    sb.document._register("qs", sb.document.createElement("nav")); sb.document._register("qs-title", sb.document.createElement("button"));
    if (mem) seed(sb, pc, mem);
    if (setup) setup(sb); } });
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = []; globalThis.__calls = []; globalThis.__lists = 0;
    globalThis.__toasts = []; toast = (m) => __toasts.push(m); maybeShowIosHint = () => {}; refreshPushBtn = () => {};
    globalThis.__teams = ${JSON.stringify([team("T1", "팀1", "g1")])};
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb, gid) => { const h = { ids, mgr, gid: gid || null, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s })) }; };
    T.api = async (v, p, o) => { __calls.push(p);
      if (p === '/api/owner/teams') { const t = typeof __teams === 'function' ? await __teams() : __teams;
        if (t === 'fail') throw new Error('agent_offline'); return { teams: JSON.parse(JSON.stringify(t)) }; }
      if (p === '/api/owner/monitor/groups') return { groups: [] };
      if (p.endsWith('/file')) { globalThis.__fileN = (globalThis.__fileN || 0) + 1; return { path: o.query.path, name: o.query.path, size: 5, mtime: 1, kind: 'text', text: 'x' + __fileN }; }
      return {}; };
    fetchRecentMessages = async () => [];
    loadMonaco = async () => ({ editor: { createModel: (t, l) => ({ t, l, disposed: false, dispose() { this.disposed = true; } }),
      create: (h, o) => ({ model: o.model, setModel(m) { this.model = m; }, dispose() {}, saveViewState() { return { top: 1 }; },
        restoreViewState(v) { this.restored = v; }, updateOptions() {}, focus() {}, getAction() { return null; } }) } });
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    conn.connect = async () => ({});
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p', mtime: 2 },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q', mtime: 1 }];
    ${pre}`);
  if (local) ev(ctx, "RELAY_APP_URL = ''");
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  if (ready) { await ev(ctx, "loadList()"); await tick(QS); ev(ctx, "__toasts.length = 0; __calls.length = 0; __lists = 0;"); }
  return { ctx, pd, app, nav: ctx.document.getElementById("qs"), title: ctx.document.getElementById("qs-title") };
}

const at = (ctx) => ev(ctx, `(() => { const k = PANE_NOW.kind, p = paneDetail().classList;
  if (k === 'terminal') return 'terminal:' + String(termView.active || '').slice(0, 1) + (p.contains('term-mode') ? '' : '?');
  if (k === 'files') return 'files:' + String(FB.active || '').split('|')[1] + (p.contains('fb-mode') ? '' : '?');
  if (k === 'viewer' || k === 'detail') return k + ':' + String(PANE_NOW.sid || '').slice(0, 1);
  if (k === 'monitor') return 'monitor:' + (MON_INLINE && MON_INLINE.gid) + (p.contains('mon-mode') ? '' : '?');
  return k; })()`);
const press = async (ctx, k) => { await ctx.quickGo(k); await tick(k === "terminal" ? RENDER : QS); };
async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
const head = (ctx) => ctx.document.getElementById("head").textContent;
// PC 머리 줄 단추 하나 — [클래스, 툴팁]
const btn = (nav, i) => [String(nav.children[i].className), nav.children[i].title];

// ---------------------------------------------------------------- 1. PC 머리 줄 제목
test("T105 PC 머리 줄 제목은 상세 칸 화면 이름을 따른다 — 목록을 새로 받아도(⟳·보기 전환·재연결) '세션'으로 덮지 않고, 상세 칸이 비면 '세션'. 폰은 목록 칸으로 돌아오니 지금처럼 '세션'", async () => {
  const got = await each(["터미널", "파일", "뷰어", "상세", "관제", "빈 칸", "폰"], async (p) => {
    const phone = p === "폰";
    const { ctx } = await boot({ width: phone ? 390 : 1400, mem: p === "상세" ? { viewer: { sid: B, kind: "detail", name: "B" } } : {} });
    await press(ctx, { "터미널": "terminal", "파일": "files", "관제": "monitor" }[p] || "viewer");
    const r = { open: head(ctx) };
    if (p === "빈 칸") { ev(ctx, "detailPlaceholder()"); r.empty = head(ctx); }
    await ev(ctx, "loadList()"); await tick(QS);                        // ⟳ · 재연결
    r.list = head(ctx);
    ev(ctx, "setListView('files')"); await ev(ctx, "loadList()"); await tick(QS);   // 목록 칸 보기 전환
    r.view = head(ctx);
    return r;
  });
  assert.deepEqual(got, {
    "터미널": { open: "터미널 재개", list: "터미널 재개", view: "터미널 재개" },
    "파일": { open: "파일", list: "파일", view: "파일" },
    "뷰어": { open: "뷰어", list: "뷰어", view: "뷰어" },
    "상세": { open: "세션 상세", list: "세션 상세", view: "세션 상세" },
    "관제": { open: "관제", list: "관제", view: "관제" },
    "빈 칸": { open: "뷰어", empty: "세션", list: "세션", view: "세션" },
    "폰": { open: "뷰어", list: "세션", view: "세션" },
  });
});

// ---------------------------------------------------------------- 2. 첫 목록 실패(PC 꺼짐)와 '받는 중' 구분
// 폰 시트 한 줄 — [칸, 보조 글, 꼬리표, 흐림]
const rows = (ctx) => ev(ctx, "POP.box.children[1].children").map((b) => [b.dataset.k,
  (b.children[1].children[1] || {}).textContent || "", (b.children[2] || {}).textContent || "", b.classList.contains("dim")]);
test("T106 첫 목록을 받지 못했으면(PC 꺼짐·연결 실패) 바로가기 단추·시트·누름이 '받는 중'이 아니라 그 까닭을 알린다 — 다시 받는 동안은 '받는 중', PC 가 돌아와 목록을 받으면 기억대로", async () => {
  const OFF = "T.list = async () => { __lists++; throw new Error('agent_offline'); };";
  const FAIL = "T.list = async () => { __lists++; throw new Error('disconnected'); };";
  const WAIT = "T.list = () => { __lists++; return new Promise(() => {}); };";
  const OK = "T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s })) }; };";
  const got = await each(["받는 중", "PC 꺼짐", "연결 실패", "PC 꺼짐 → 다시 받는 중(⟳)", "PC 꺼짐 → PC 돌아옴", "폰: PC 꺼짐"], async (p) => {
    const phone = p.startsWith("폰");
    const { ctx, nav, title } = await boot({ width: phone ? 390 : 1400, ready: false, mem: {}, pre: p === "받는 중" ? WAIT : p === "연결 실패" ? FAIL : OFF });
    const pl = ev(ctx, "loadList()"); if (p !== "받는 중") await pl; await tick(QS);
    if (p.endsWith("다시 받는 중(⟳)")) { ev(ctx, WAIT); ev(ctx, "loadList()"); await tick(QS); }
    if (p.endsWith("PC 돌아옴")) { ev(ctx, OK); ev(ctx, "pcBack()"); await tick(QS); }
    const shown = phone ? (title.onclick(), rows(ctx)[0]) : btn(nav, 0);
    if (phone) ev(ctx, "closePop()");
    ev(ctx, "__toasts.length = 0");
    if (p.endsWith("PC 돌아옴")) return { shown };
    await ctx.quickGo("files"); await tick();
    return { shown, press: ev(ctx, "__toasts.join('|')"), at: at(ctx) };
  });
  assert.deepEqual(got, {
    "받는 중": { shown: ["dim wait", "터미널 — 준비 중(목록을 받는 중) · Alt+1"], press: "목록을 받는 중입니다", at: "empty" },
    "PC 꺼짐": { shown: ["dim", "터미널 — PC 가 꺼져 있습니다 · Alt+1"], press: "PC 가 꺼져 있습니다 — 켜지면 자동으로 다시 불러옵니다", at: "empty" },
    "연결 실패": { shown: ["dim", "터미널 — 목록을 받지 못했습니다 · Alt+1"], press: "목록을 받지 못했습니다 — 연결을 확인하세요", at: "empty" },
    "PC 꺼짐 → 다시 받는 중(⟳)": { shown: ["dim wait", "터미널 — 준비 중(목록을 받는 중) · Alt+1"], press: "목록을 받는 중입니다", at: "empty" },
    "PC 꺼짐 → PC 돌아옴": { shown: ["", "터미널 — A · Alt+1"] },
    "폰: PC 꺼짐": { shown: ["terminal", "PC 가 꺼져 있습니다", "PC 꺼짐", true], press: "PC 가 꺼져 있습니다 — 켜지면 자동으로 다시 불러옵니다", at: "empty" },
  });
});

// ---------------------------------------------------------------- 3. 목록 줄·알림 길의 '다른 기기가 가져간 뒤 claude 가 끝난 탭'
// 바로가기 길(T86)과 같은 방어 — 가져간 뒤 끝났으면 인계 확인창 없이 탭을 닫고 상세로(새 claude 없음), 살아 있으면 확인창을 거쳐서만 가져온다
test("T107 목록 줄·알림으로 '다른 기기가 가져간 터미널'을 열 때 — 가져간 뒤 claude 가 끝났으면 확인창 없이 그 탭을 닫고 상세로(새 claude 없음), 살아 있으면 인계 확인 뒤 가져온다. 알림으로 연 꺼진 탭(중지됨)도 새로 띄우지 않는다", async () => {
  const DEAD = "SESSIONS[0].live_terminal = false; T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s, live_terminal: s.session_id === '" + A + "' ? false : s.live_terminal })) }; };";
  const got = await each(["목록 줄|끝남", "목록 줄|살아 있음", "알림|끝남", "알림|살아 있음", "알림|중지된 탭"], async (p) => {
    const [via, what] = p.split("|");
    const { ctx } = await boot({ mem: {}, pre: what === "중지된 탭" ? "SESSIONS[0].live_terminal = false;" : "" });
    if (what !== "중지된 탭") {
      await press(ctx, "terminal");
      ev(ctx, "__tc.at(-1).cb('\\r\\n' + TAKEOVER_NOTE + '\\r\\n'); __tc.at(-1).cb(null, true);");   // 다른 기기가 가져감(인계 통지 → 스트림 끝)
      if (what === "끝남") ev(ctx, DEAD);
      else ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' }");
    }
    let asked = 0; ctx.confirm = () => { asked++; return true; };
    const tc0 = ev(ctx, "__tc.length"), l0 = ev(ctx, "__lists");
    if (via === "목록 줄") await ctx.jumpToSession(ev(ctx, "SESSIONS[0]"));
    else await ctx.openSessionById(A);
    await tick(RENDER);
    return { asked, at: at(ctx), newTerm: ev(ctx, "__tc.length") - tc0, lists: ev(ctx, "__lists") - l0, tabs: ev(ctx, "TABS.list.length") };
  });
  assert.deepEqual(got, {
    "목록 줄|끝남": { asked: 0, at: "detail:a", newTerm: 0, lists: 1, tabs: 0 },      // 판정 전에 목록을 한 번 새로 받는다(20초 목록이 옛값일 수 있다)
    "목록 줄|살아 있음": { asked: 1, at: "terminal:a", newTerm: 1, lists: 0, tabs: 1 },
    "알림|끝남": { asked: 0, at: "detail:a", newTerm: 0, lists: 1, tabs: 0 },
    "알림|살아 있음": { asked: 1, at: "terminal:a", newTerm: 1, lists: 0, tabs: 1 },
    "알림|중지된 탭": { asked: 0, at: "detail:a", newTerm: 0, lists: 1, tabs: 0 },
  });
});
