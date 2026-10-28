// 바로가기 6단계 다듬기(0.32.3) — docs/designs/quick-switch.md §8 6단계 · TODOS 0.31.x 다듬기. 고치기 전에 이 파일로 실패를 먼저 확인했다(T105~).
// 머리 줄 제목 · PC 꺼짐과 '받는 중' 구분 · 목록 줄·알림 길의 '가져간 뒤 끝난 탭' · 탭 닫은 뒤 최근 본 탭 · 🖥 볼 일 점 · 파일 ⟳·이동 경로 ·
// Ctrl+Shift+B · 이름 바꾸기·삭제가 보던 화면을 지우지 않음 · 업데이트 확인의 unsupported.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { load, ev, stubEl, INDEX } from "./harness.mjs";

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
// 머리 줄·시트 맨 앞의 🏠 홈(2026-10-26)은 기억이 없는 칸이라 네 칸 시험에서는 뺀다(홈 시험은 qs-ui.test.mjs T95)
const four = (nav) => new Proxy(nav, { get: (t, k) => (k === "children" ? t.children.filter((b) => b.dataset.k !== "home") : t[k]) });
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
      if (p.endsWith('/files')) { const at = (o.query || {}).path || '';   // 탐색기 폴더 — 뿌리엔 src 폴더, 그 안엔 a.py
        return { path: at, truncated: false, entries: at ? [{ name: 'a.py', type: 'file', size: 5, mtime: 1 }] : [{ name: 'src', type: 'dir', mtime: 1 }] }; }
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
  return { ctx, pd, app, nav: four(ctx.document.getElementById("qs")), title: ctx.document.getElementById("qs-title") };
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
test("T105 PC 머리 줄 제목은 상세 칸 화면 이름을 따른다 — 목록을 새로 받아도(⟳·보기 전환·재연결) '세션'으로 덮지 않고, 상세 칸이 비면 그 칸이 🏠 홈이라 '홈'(2026-10-28). 폰은 목록 칸으로 돌아오니 지금처럼 '세션'", async () => {
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
    "빈 칸": { open: "뷰어", empty: "홈", list: "홈", view: "홈" },
    "폰": { open: "뷰어", list: "세션", view: "세션" },
  });
});

// ---------------------------------------------------------------- 2. 첫 목록 실패(PC 꺼짐)와 '받는 중' 구분
// 폰 시트 한 줄 — [칸, 보조 글, 꼬리표, 흐림]
const rows = (ctx) => ev(ctx, "POP.box.children[1].children").filter((b) => b.dataset.k !== "home").map((b) => [b.dataset.k,
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

// ---------------------------------------------------------------- 4. 탭 닫기·⏹ 종료 뒤 최근 본 탭
test("T108 보던 탭을 닫거나(× → 화면만 닫기·⏹ 세션 종료) 끝내면 첫 탭이 아니라 '최근에 본 탭'으로 간다 — 종료는 × 한 곳(막대 ⏹ 종료 없음, 2026-10-28)", async () => {
  const ids = ["a", "b", "c", "d", "e"].map((c) => c.repeat(8) + "-1111-2222-3333-444444444444");
  const { ctx, pd } = await boot({ mem: { tabs: ids },
    pre: `SESSIONS = ${JSON.stringify(ids.map((id, i) => ({ session_id: id, title: id[0].toUpperCase(), live_terminal: true, cwd: "E:/p" + i, mtime: 9 - i })))};
      globalThis.__modal = null; openModal = (t, b, btns) => { __modal = { t, btns }; }; closeModal = () => {};` });
  for (const k of ["a", "e", "d", "c", "b"]) { ev(ctx, `switchTab('${ids["abcde".indexOf(k)]}')`); await tick(RENDER); }   // 본 차례 a → e → d → c → b(지금)
  const got = { start: at(ctx) };
  ev(ctx, `closeTab('${ids[1]}')`); __modalBtn(ctx, "화면만 닫기").onclick(); await tick(RENDER);
  got.closeView = at(ctx);
  got.barNoStop = !pd.children[0].children.some((b) => String(b.textContent).includes("종료"));
  ev(ctx, `closeTab('${ids[2]}')`); await __modalBtn(ctx, "⏹ 세션 종료").onclick(); await tick(RENDER);
  got.barStop = at(ctx);
  ev(ctx, `closeTab('${ids[3]}')`); await __modalBtn(ctx, "⏹ 세션 종료").onclick(); await tick(RENDER);
  got.closeStop = at(ctx);
  got.tabs = ev(ctx, "TABS.list.map((t) => sessLabel(t.s)).join(',')");
  assert.deepEqual(got, { start: "terminal:b", closeView: "terminal:c", barNoStop: true, barStop: "terminal:d", closeStop: "terminal:e", tabs: "A,E" });
});
function __modalBtn(ctx, label) { return ev(ctx, "__modal.btns").find((b) => b.textContent === label); }

// ---------------------------------------------------------------- 5. 🖥 단추의 '볼 일 점'
test("T109 🖥 단추의 볼 일 점 — 열린 탭 중 권한·입력 대기 세션이 있으면 주황 점(툴팁에 그 탭), 지금 보고 있는 터미널 탭은 빼고, 낡은 신호(30분 넘음)는 빼며, 20초 목록만 쓴다(새 연결·요청 없음). 폰 시트 줄도 같은 점", async () => {
  const { ctx, nav } = await boot({ mem: { tabs: [A, B] } });
  const now = "Date.now() / 1000";
  const calls = () => ev(ctx, "[__calls.length, __lists, __tc.length, __mon.length].join(',')");
  const draw = () => { ev(ctx, "renderQuick()"); return btn(nav, 0); };
  const got = {};
  const c0 = calls();
  got.none = draw();
  ev(ctx, `SESSIONS[0].runtime = { phase: 'waiting', waiting_at: ${now} }`);
  got.waitA = draw();
  got.calls = calls() === c0;
  await press(ctx, "terminal");                                       // 그 탭(A)을 보는 중 — 이미 보고 있으니 점 없음
  got.onA = draw();
  ev(ctx, `SESSIONS[1].runtime = { phase: 'permission', permission_at: ${now} }`);
  got.permB = draw();
  ev(ctx, `SESSIONS[1].runtime = { phase: 'permission', permission_at: ${now} - 31 * 60 }`);
  got.stale = draw();
  const W = "터미널 — A · 입력 대기 · 탭 2개 · Alt+1";
  assert.deepEqual(got, {
    none: ["", "터미널 — A · 탭 2개 · Alt+1"],
    waitA: ["attn", W + "\n● 볼 일: A(입력 대기)"],
    calls: true,
    onA: ["on", W],
    permB: ["on attn", W + "\n● 볼 일: B(권한 대기)"],
    stale: ["on", W],
  });
  // 폰: 제목 ▾ 시트의 🖥 줄
  const ph = await boot({ width: 390, mem: { tabs: [A, B] }, pre: `SESSIONS[1].runtime = { phase: 'waiting', waiting_at: ${now} };` });
  ph.title.onclick();
  const row = ev(ph.ctx, "POP.box.children[1].children").find((b) => b.dataset.k === "terminal");   // [0] = 🏠 홈
  assert.deepEqual([row.dataset.k, row.classList.contains("attn")], ["terminal", true]);
  // 점은 CSS 로 — PC 단추(머리 줄 880px 이상)·폰 시트 줄 아이콘, 🔔 배지와 같은 주황
  const css = readFileSync(INDEX, "utf-8");
  assert.match(css, /#qs button\.attn::after\{[^}]*background:#e8a03c/);
  assert.match(css, /\.pop \.popi\.qs-it\.attn \.pi::after\{[^}]*background:#e8a03c/);
});

// ---------------------------------------------------------------- 6. 파일 ⟳ 다시 읽기 · 이동 경로 → 📁 탐색기
const crumbTool = (ctx, label) => { const c = ev(ctx, "FB.crumbEl").children; return c[c.length - 1].children.find((b) => b.textContent === label); };
test("T110 파일 막대의 ⟳ 다시 읽기는 열어 둔 파일을 PC 에서 새로 받는다(스크롤 위치는 그대로) — 받지 못했던 파일(오류)도 다시 받는다", async () => {
  const { ctx } = await boot({ mem: { files: ["a.py"] } });
  await press(ctx, "files");
  const got = { first: [ev(ctx, "__fileN"), ev(ctx, "FB.ed.model.t")] };
  const old = ev(ctx, "FB.ed.model");
  const r = crumbTool(ctx, "⟳");
  got.tip = r && r.title;
  await r.onclick(); await tick(QS);
  got.again = [ev(ctx, "__fileN"), ev(ctx, "FB.ed.model.t"), old.disposed, JSON.stringify(ev(ctx, "FB.ed.restored"))];
  ev(ctx, "FB.tabs[0].err = { error: 'no_session' }; FB.tabs[0].data = null; fbRenderActive();"); await tick(QS);   // 목록 전에 열어 굳은 오류
  got.errTool = !!crumbTool(ctx, "⟳");
  await crumbTool(ctx, "⟳").onclick(); await tick(QS);
  got.afterErr = [ev(ctx, "__fileN"), ev(ctx, "FB.tabs[0].err"), ev(ctx, "FB.ed.model.t")];
  got.at = at(ctx);
  assert.deepEqual(got, {
    first: [1, "x1"], tip: "다시 읽기 — PC 에서 이 파일을 새로 받습니다",
    again: [2, "x2", true, '{"top":1}'],
    errTool: true, afterErr: [3, null, "x3"], at: "files:a.py",
  });
});
test("T111 편집기 위 이동 경로(경로 조각)를 누르면 왼쪽 목록 칸을 📁 탐색기로 바꿔(접혀 있으면 펼쳐) 그 폴더를 고른다 — 상세 칸 편집기는 그대로, 폰은 탐색기 화면으로", async () => {
  const got = await each(["PC: 세션 보기", "PC: 접힌 목록 칸", "PC: 이미 탐색기", "폰"], async (p) => {
    const phone = p === "폰";
    const { ctx, app } = await boot({ width: phone ? 390 : 1400, mem: { files: ["src/a.py"] },
      setup: (sb) => { if (p === "PC: 이미 탐색기") sb.localStorage.setItem("sm_list_view", "files"); } });
    await press(ctx, "files");
    if (p === "PC: 접힌 목록 칸") ev(ctx, "setSideOff(true)");
    const l0 = ev(ctx, "__lists");
    const seg = ev(ctx, "FB.crumbEl").children.find((b) => b.textContent === "src");
    await seg.onclick(); await tick(QS);
    return { view: ev(ctx, "LIST_VIEW"), lists: ev(ctx, "__lists") - l0, side: app.classList.contains("side-off"), sel: ev(ctx, "FB.sel"),
             open: ev(ctx, "FB.open.has('e:/p|src')"), at: at(ctx), ...(phone ? { pane: app.getAttribute("data-view") } : {}) };   // PC 는 두 칸이 함께 보인다
  });
  const PC = { view: "files", lists: 1, side: false, sel: "e:/p|src", open: true, at: "files:src/a.py" };
  assert.deepEqual(got, {
    "PC: 세션 보기": PC, "PC: 접힌 목록 칸": PC,
    "PC: 이미 탐색기": { ...PC, lists: 0 },                               // 떠 있는 탐색기를 그 자리에서 다시 그린다
    "폰": { ...PC, pane: "list" },
  });
});

// ---------------------------------------------------------------- 7. Ctrl+Shift+B 가 터미널 안에서도
test("T112 Ctrl+Shift+B(목록 칸 접기·펼치기)는 릴레이 터미널(xterm 숨은 입력칸)·편집기 본문에 커서가 있어도 먹는다 — 진짜 입력칸(검색·이름·글쓰기 칸)에서는 그대로 무시, 릴레이 xterm 은 이 조합을 claude 로 보내지 않고, 로컬 터미널 창(iframe) 안에도 같은 감시를 붙인다", async () => {
  const { ctx, app } = await boot({ mem: {} });
  ev(ctx, "globalThis.__ckh = []; { const X = Terminal; Terminal = class { constructor(o) { const t = new X(o); t.attachCustomKeyEventHandler = (f) => __ckh.push(f); return t; } }; }");
  await press(ctx, "terminal");
  const h = ev(ctx, "__ckh[0]");
  const xt = () => Object.assign(stubEl("textarea"), { className: "xterm-helper-textarea", closest: (s) => (s === ".xterm" ? stubEl("div") : null) });
  const mo = () => Object.assign(stubEl("textarea"), { closest: (s) => (s === ".monaco-editor" ? stubEl("div") : null) });
  const key = (target, o = {}) => ({ type: "keydown", ctrlKey: true, shiftKey: true, altKey: false, metaKey: false, key: "B", code: "KeyB", target,
    prevented: 0, preventDefault() { this.prevented++; }, stopPropagation() {}, ...o });
  const cases = { "터미널": [xt()], "편집기 본문": [mo()], "본문": [stubEl("body")], "검색칸": [stubEl("input")], "글쓰기 칸": [stubEl("textarea")],
    "고르기": [stubEl("select")], "Ctrl+B(Shift 없음)": [xt(), { shiftKey: false, key: "b" }], "맥 ⌘+Shift+B": [xt(), { ctrlKey: false, metaKey: true }] };
  const got = {};
  for (const [name, [t, o]] of Object.entries(cases)) {
    const before = app.classList.contains("side-off"), e = key(t, o);
    ctx.onSideKey(e);
    got[name] = { toggled: app.classList.contains("side-off") !== before, prevented: e.prevented };
  }
  got.xtermSends = [h(key(xt())), h(key(xt(), { shiftKey: false, key: "b" }))];   // Ctrl+Shift+B 는 claude 로 안 보냄, Ctrl+B 는 그대로(백그라운드로 보내기)
  const w = { ls: [], addEventListener(t, f, c) { this.ls.push([t, f, c]); } };
  ctx.quickKeyWatch(w);
  got.frame = w.ls.map(([t, f]) => t + ":" + (f === ev(ctx, "onSideKey") ? "side" : f === ev(ctx, "onQuickKey") ? "quick" : "?")).join(",");
  const on = { toggled: true, prevented: 1 }, off = { toggled: false, prevented: 0 };
  assert.deepEqual(got, {
    "터미널": on, "편집기 본문": on, "본문": on, "검색칸": off, "글쓰기 칸": off, "고르기": off, "Ctrl+B(Shift 없음)": off, "맥 ⌘+Shift+B": on,
    xtermSends: [false, true], frame: "keydown:quick,keydown:side",
  });
});

// ---------------------------------------------------------------- 8. 이름 바꾸기·삭제는 그 세션을 보고 있을 때만 상세 칸을 바꾼다
test("T113 이름 바꾸기 성공·삭제가 보던 화면을 바꾸지 않는다 — 지금 그 세션을 상세 칸에서 보고 있을 때만: 이름은 그 화면에서 새로(상세는 다시 그림·뷰어는 막대 이름), 삭제는 칸을 비운다", async () => {
  const got = await each(["이름|다른 세션(터미널 보는 중)", "이름|그 세션 터미널", "이름|그 세션 상세", "이름|그 세션 뷰어",
                          "삭제|다른 세션(전에 그 상세를 봄, 지금 터미널)", "삭제|그 세션 상세", "삭제|그 세션 뷰어"], async (p) => {
    const [what, where] = p.split("|");
    const { ctx } = await boot({ mem: { viewer: { sid: B, kind: where.includes("뷰어") ? "viewer" : "detail", name: "B" } } });
    ev(ctx, "prompt = () => '새 이름'; T.api = ((f) => async (v, path, o) => path.endsWith('/delete') ? { recoverable: true } : f(v, path, o))(T.api);");
    if (where.startsWith("다른 세션(전에")) await press(ctx, "viewer");                   // B 상세를 봤다(SELECTED_ID = B)
    if (where.includes("터미널")) await press(ctx, "terminal"); else if (where.startsWith("그 세션")) await press(ctx, "viewer");
    const target = where === "그 세션 터미널" ? 0 : 1, gen = ev(ctx, "PANE_NOW.gen");
    if (what === "이름") await ctx.renameNative(ev(ctx, `SESSIONS[${target}]`));
    else await ctx.doDelete(ev(ctx, `SESSIONS[${target}]`));
    await tick(QS);
    const r = { at: at(ctx), redrawn: ev(ctx, "PANE_NOW.gen") !== gen };
    if (what === "이름") r.name = ev(ctx, `SESSIONS[${target}].title`);
    if (where === "그 세션 뷰어" && what === "이름") r.bar = ev(ctx, "VIEWER_TITLE && VIEWER_TITLE.gen === PANE_NOW.gen && VIEWER_TITLE.el.textContent");
    return r;
  });
  assert.deepEqual(got, {
    "이름|다른 세션(터미널 보는 중)": { at: "terminal:a", redrawn: false, name: "새 이름" },
    "이름|그 세션 터미널": { at: "terminal:a", redrawn: false, name: "새 이름" },
    "이름|그 세션 상세": { at: "detail:b", redrawn: true, name: "새 이름" },
    "이름|그 세션 뷰어": { at: "viewer:b", redrawn: false, name: "새 이름", bar: "새 이름" },
    "삭제|다른 세션(전에 그 상세를 봄, 지금 터미널)": { at: "terminal:a", redrawn: false },
    "삭제|그 세션 상세": { at: "empty", redrawn: true },
    "삭제|그 세션 뷰어": { at: "empty", redrawn: true },
  });
});

// ---------------------------------------------------------------- 9. 업데이트 확인의 unsupported·확인 실패
// update/status 응답(session_manager/server.py owner_update_status = updater.read_state + current·dev): 최신 릴리스가 이 OS 용이 아니면
// available=false·latest·unsupported(사유), 업데이트 서버에 못 닿으면 available=false·error. 둘 다 '최신 버전입니다'가 아니다
test("T114 업데이트 확인: 최신 릴리스가 이 OS 용이 아니면(unsupported) 그 사유를, 업데이트 서버 확인에 실패했으면(error) 그 사실을 보여 준다 — '최신 버전입니다'로 보이지 않게. 진짜 최신·새 버전·개발 소스는 그대로", async () => {
  const UNSUP = "이 릴리스(0.33.0)는 mac 를 지원하지 않습니다";
  const cases = {
    "이 OS 용 아님": { current: "0.32.2", latest: "0.33.0", available: false, error: null, unsupported: UNSUP, dev: false },
    "확인 실패": { current: "0.32.2", latest: "0.32.2", available: false, error: "확인 실패: URLError: timed out", dev: false },
    "최신": { current: "0.32.2", latest: "0.32.2", available: false, error: null, unsupported: null, dev: false },
    "새 버전": { current: "0.32.2", latest: "0.33.0", available: true, error: null, unsupported: null, dev: false },
    "개발 소스": { current: "0.32.2", available: false, dev: true },
  };
  const got = await each(Object.keys(cases), async (p) => {
    const ctx = await load({ location: LOCAL });
    ev(ctx, `globalThis.__m = []; openModal = (t, b) => { __m.push(b); }; closeModal = () => {}; globalThis.__apply = null;
      showUpdateApply = (c, l) => { __apply = c + '→' + l; };
      T.api = async (v, path) => { if (path === '/api/owner/update/status') return ${JSON.stringify(cases[p])}; return {}; };`);
    await ev(ctx, "checkUpdateNow('0.32.2')");
    return ev(ctx, "__apply") || ev(ctx, "__m.at(-1).children[0].textContent");
  });
  assert.deepEqual(got, {
    "이 OS 용 아님": "현재 v0.32.2 — 새 버전 v0.33.0 은 아직 이 PC 에서 받을 수 없습니다(" + UNSUP + "). 이 운영체제용 판이 나오면 여기서 업데이트할 수 있습니다.",
    "확인 실패": "현재 v0.32.2 — 업데이트 서버에서 새 버전을 확인하지 못했습니다(확인 실패: URLError: timed out). 잠시 뒤 다시 확인하세요.",
    "최신": "현재 v0.32.2 — 최신 버전입니다.",
    "새 버전": "0.32.2→0.33.0",
    "개발 소스": "개발 소스에서 실행 중입니다 (v0.32.2). 이 PC 는 git 으로 갱신하세요 — 자동 업데이트는 소스 트리 보호를 위해 막혀 있습니다.",
  });
});

// ---------------------------------------------------------------- 터미널 막대 정리(2026-10-28 사장님)
test("T115 터미널 막대에는 ‹ 목록·📖 이력만(⏹ 종료 없음) — 막대는 termbar 표시가 붙고, PC 폭(≥880px) 규칙에서만 막대째 숨기며 행을 2개로 줄인다. 폰 규칙에는 숨김이 없다", async () => {
  const { ctx, pd } = await boot({ mem: { tabs: [A, B] } });
  ev(ctx, `switchTab('${A}')`); await tick(RENDER);
  const bar = pd.children[0];
  const got = { cls: String(bar.className), btns: bar.children.map((b) => String(b.textContent)), hist: String(bar.children[1].className) };
  const css = readFileSync(INDEX, "utf-8").replace(/\r/g, "");
  const pcAt = css.indexOf("/* ---- 데스크톱(≥880px)");
  const rule = "#paneDetail.term-mode>.backbar.termbar{display:none}";
  got.pcHide = css.indexOf(rule) > pcAt && pcAt > 0 && css.indexOf(rule) === css.lastIndexOf(rule);
  got.pcRows = css.slice(pcAt).includes("#paneDetail.term-mode{grid-template-rows:auto minmax(0,1fr)}");
  assert.deepEqual(got, { cls: "backbar toonly termbar", btns: ["‹ 목록", "📖 이력"], hist: "term-hist", pcHide: true, pcRows: true });
});

// ---------------------------------------------------------------- 🏠 홈 개편(2026-10-28, docs/designs/home-dashboard.md)
// 홈 한 줄 — [칸, 이름, 보조 글, 시간]
const homeRows = (pd) => { const box = pd.children[0], d = box.children.find((c) => String(c.className) === "home-dash");
  if (!d) return null;
  return d.children.map((sec) => String(sec.className) === "hd-idle" ? sec.textContent
    : [String(sec.className).replace("hd-sec k-", ""), sec.children[0].textContent + " " + sec.children[0].children[0].textContent,
       ...sec.children.slice(1).map((b) => [b.children[1].children[0].textContent, b.children[1].children[1].textContent, b.children[2].textContent])]); };
test("T116 🏠 홈: 🔔 지금 볼 일(권한·입력 대기, 오래 기다린 것 위) · ⚙ 작업 중(작업·백그라운드) · ✅ 방금 끝남(6시간 안, 이 기기에서 그 뒤 안 본 것) — claude -p 기록·6시간 넘은 완료는 빼고, 빈 칸은 숨기며 셋 다 비면 한 줄. 끝남을 누르면 대화 보기, 보고 오면 빠진다. 20초 갱신은 단추를 새로 만들지 않고 글자만. 폰 🏠 은 상세 쪽 홈(제목 '홈', '왼쪽에서' 안내 없음)", async () => {
  const id = (c) => c.repeat(8) + "-1111-2222-3333-444444444444";
  const rt = (o) => JSON.stringify(o).replace(/"NOW-(\d+)"/g, "now-$1");
  const S = [
    ["a", { phase: "permission", permission_at: "NOW-240", note: "Bash 실행 권한이 필요합니다" }],
    ["b", { phase: "waiting", waiting_at: "NOW-600" }],
    ["c", { phase: "thinking", thinking_at: "NOW-720" }],
    ["d", { phase: "background", ready_at: "NOW-60" }, { shells: 2 }],
    ["e", { phase: "ready", ready_at: "NOW-1800" }],
    ["f", { phase: "ready", ready_at: "NOW-25200" }],                 // 7시간 전 — 빼기
    ["h", { phase: "permission", permission_at: "NOW-30" }, null, true],   // claude -p 기록 — 빼기
  ];
  const pre = `const now = Date.now() / 1000; SESSIONS = [${S.map(([c, r, bg, hl]) =>
    `{ session_id: '${id(c)}', title: '${c.toUpperCase()}', cwd: 'E:/w/${c}', mtime: 1, runtime: ${rt(r)}, bg: ${JSON.stringify(bg || null)}, headless: ${!!hl} }`).join(",")}];`;
  const got = {};
  { const { ctx, pd } = await boot({ mem: {}, pre });
    ev(ctx, "quickGoHome()"); await tick(QS);
    got.head = head(ctx);
    got.rows = homeRows(pd);
    const before = pd.children[0].children[0];
    await ev(ctx, "loadList()"); await tick(QS);                         // 20초 갱신과 같은 길
    got.same = pd.children[0].children[0] === before;
    const done = homeRows(pd).find((x) => x[0] === "done");
    got.doneBefore = done.length - 2;
    before.children.find((x) => String(x.className) === "hd-sec k-done").children[1].onclick(); await tick(QS);
    got.click = at(ctx);
    ev(ctx, "quickGoHome()"); await tick(QS);
    got.afterSeen = homeRows(pd).map((x) => Array.isArray(x) ? x[0] : x);
  }
  { const { ctx, pd } = await boot({ mem: {} }); ev(ctx, "quickGoHome()"); await tick(QS); got.idle = homeRows(pd); }
  { const { ctx, pd } = await boot({ ready: false, mem: {} }); ev(ctx, "detailPlaceholder()"); got.beforeList = homeRows(pd); }
  { const { ctx, pd, app } = await boot({ width: 390, mem: {}, pre });
    ev(ctx, "quickGoHome()"); await tick(QS);
    got.phone = [app.getAttribute("data-view"), head(ctx), pd.children[0].children.some((c) => String(c.className) === "de-msg"), homeRows(pd).length];
    ev(ctx, "showListPane()"); got.phoneList = head(ctx); }
  assert.deepEqual(got, {
    head: "홈",
    rows: [
      ["att", "🔔 지금 볼 일 2", ["B", "b · 답을 기다림", "10분째"], ["A", "a · Bash 실행 권한이 필요합니다", "4분째"]],
      ["work", "⚙ 작업 중 2", ["C", "c · 작업 중", "12분째"], ["D", "d · 🐚 쉘 2", ""]],
      ["done", "✅ 방금 끝남 1", ["E", "e · 완료", "30분 전"]],
    ],
    same: true, doneBefore: 1, click: "viewer:e",
    afterSeen: ["att", "work"],
    idle: ["기다리는 일 없음 · 돌고 있는 세션 없음"],
    beforeList: null,
    phone: ["detail", "홈", false, 3], phoneList: "세션",
  });
});

test("T117 🏠 홈 2차: ⏳ 곧 지워질 대화(claude 가 3일 안에 지울 것, claude -p 기록 빼고, 3건 + '외 N건' → 🔔 할 일) — 누르면 상세(재개·내보내기 안내). 🖥 이 PC: 연결·버전(새 버전이면 단추)·열린 터미널·실행 중 세션, 로컬 화면에서만 외부 서비스 오늘 사용 — 홈에 들어올 때 한 번만 받고 20초 갱신 때는 다시 받지 않는다", async () => {
  const id = (c) => c.repeat(8) + "-1111-2222-3333-444444444444";
  const G = [["p", 1.5], ["q", 0.5], ["r", 2.5], ["s", 2.9], ["t", 6], ["u", 1, true]];
  const pre = `const now = Date.now() / 1000; SESSIONS = [${G.map(([c, d, hl]) =>
    `{ session_id: '${id(c)}', title: '${c.toUpperCase()}', cwd: 'E:/w/${c}', mtime: 1, cleanup_at: now + ${d} * 86400, headless: ${!!hl} }`).join(",")},
      { session_id: '${id("v")}', title: 'V', cwd: 'E:/w/v', mtime: 1, live_terminal: true }];
    { const a = T.api; T.api = async (v, p, o) => { if (p === '/api/owner/services') { __calls.push(p);
      return { services: [{ id: 'mt', name: 'mt-translator', status: 'active', today: { calls: 12, usd: 0.021 }, limits: { daily_calls: 500, daily_usd: 5 } },
                          { id: 'old', name: '옛 서비스', status: 'revoked' }] }; } return a(v, p, o); }; }`;
  const kids = (pd, cls) => pd.children[0].children.find((c) => String(c.className) === cls);
  const svcCalls = (ctx) => ev(ctx, "__calls.filter((p) => p === '/api/owner/services').length");
  const got = {};
  { const { ctx, pd } = await boot({ mem: {}, pre });
    ev(ctx, "VERSTATE = { cur: '0.33.5', latest: '0.33.6' }; quickGoHome()"); await tick(QS);
    const g = kids(pd, "hd-sec k-gone");
    got.gone = [g.children[0].textContent + " " + g.children[0].children[0].textContent,
      ...g.children.slice(1).map((b) => String(b.className) === "hd-more" ? b.textContent
        : [b.children[1].children[0].textContent, b.children[1].children[1].textContent, b.children[2].textContent])];
    const pc = kids(pd, "hd-pc");
    got.pc = pc.children.slice(1).map((x) => [String(x.className), x.textContent]);
    got.relaySvc = svcCalls(ctx);
    g.children[1].onclick(); await tick(QS); got.click = at(ctx);
  }
  { const { ctx, pd } = await boot({ local: true, mem: {}, pre });
    ev(ctx, "quickGoHome()"); await tick(QS);
    got.localPc = kids(pd, "hd-pc").children.slice(1).map((x) => x.textContent);
    const c0 = svcCalls(ctx);
    await ev(ctx, "loadList()"); await tick(QS); await ev(ctx, "loadList()"); await tick(QS);   // 20초 갱신 두 번 — 다시 받지 않는다
    got.afterRefresh = svcCalls(ctx) - c0;
    ev(ctx, "loadDetail(SESSIONS[0])"); await tick(QS); ev(ctx, "quickGoHome()"); await tick(QS);   // 다른 화면 갔다가 다시 홈 — 한 번 더
    got.reenter = svcCalls(ctx) - c0;
  }
  const pcGot = got.pc; delete got.pc;
  assert.deepEqual(got, {
    gone: ["⏳ 곧 지워질 대화 4",
      ["Q", "q · claude 보관 기간 끝 — 재개하면 늘어남", "오늘 지워짐"],
      ["P", "p · claude 보관 기간 끝 — 재개하면 늘어남", "1일 뒤 지워짐"],
      ["R", "r · claude 보관 기간 끝 — 재개하면 늘어남", "2일 뒤 지워짐"],
      "외 1건 — 🔔 할 일에서 모두 보기"],
    relaySvc: 0, click: "detail:q",
    localPc: ["연결: 로컬 화면(이 PC 에서 직접)", "열린 터미널 1 · 실행 중인 세션 1", "🔌 mt-translator — 오늘 12/500회 · $0.02/5"],
    afterRefresh: 0, reenter: 1,
  });
  // 릴레이 쪽 이 PC 줄: 연결(PC 이름은 기억에 따라 다름)·새 버전 단추·실행 수
  assert.equal(pcGot.length, 3);
  assert.ok(pcGot[0][0] === "hd-pl k-conn" && pcGot[0][1].startsWith("연결: 릴레이 · "));
  assert.deepEqual(pcGot.slice(1), [["hd-pl upd", "ClewPath v0.33.5 · 새 버전 v0.33.6 있음"], ["hd-pl k-run", "열린 터미널 1 · 실행 중인 세션 1"]]);
});
