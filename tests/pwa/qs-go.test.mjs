// 상세 칸 화면 바로가기(quick-switch) 4단계 — 바로가기 동작(화면 없이 함수만). docs/designs/quick-switch.md §4·§5·§8 4단계.
// quickTargets() = 네 칸(🖥 터미널 · 📄 파일 · 💬 뷰어/ℹ 상세 · 🎬 팀 관제)의 있음·이름·보조 글·흐림·👀(20초 목록과 기억만, 새 연결 없이).
// quickGo(kind) = 그 화면의 마지막 상태로(돌아가기 전용 — 새 탭·새 claude 없음, 관문은 각 화면을 여는 기존 함수가 그대로).
// 5단계(머리 줄 단추·폰 제목 ▾ 시트·단축키)는 이 두 함수를 그리고 부르기만 한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const C = "cccccccc-1111-2222-3333-444444444444";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const RENDER = 350;   // 터미널 attach 의 resize 타이머(80·300ms)·renderTabBarSoon(300ms)이 지나가게
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const RK = "e:/p";    // 세션 A 의 작업 폴더(E:/p) 뿌리 키

const TEAM1 = { code: "T1", name: "팀1", kind: "team", monitor_group_id: "g1",
  members: [{ member_role: "manager", session_id: "m1", alias: "관리" }, { member_role: "worker", session_id: "w1", alias: "일꾼" }] };
const TEAM2 = { code: "T2", name: "팀2", kind: "team", monitor_group_id: "g2", members: [{ member_role: "manager", session_id: "m2", alias: "관리2" }] };
const PORTFOLIO = { code: "ASSIST", name: "비서", kind: "portfolio", monitor_group_id: "gA", members: [{ member_role: "manager", session_id: "s-a", alias: "비서" }] };

// 이 PC 의 기억(새로고침 전에 남긴 것): 터미널 탭(sm_tabs_<PC>) · 파일 탭(sm_fb_state2_<PC>) · 뷰어·관제(sm_pane_<PC>). null 이면 그 기억 없음
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

// 상세 칸·#app·머리 줄·목록 칸을 고정하고(하네스 주의 §8) 터미널·관제·재개 연결과 API 호출은 스텁으로 기록한다(기본 릴레이·PC 폭).
// ready: 첫 목록을 받는다(loadList — 탭 기억 복원까지). pre: 첫 목록 전에 돌릴 코드(세션 목록 손보기 등)
async function boot({ width = 1400, local = false, mem = null, ready = true, pre = "", setup = null } = {}) {
  const pc = local ? "local" : "relay";
  const ctx = await load({ ...(local ? { location: LOCAL } : {}), setup: (sb) => { if (mem) seed(sb, pc, mem); if (setup) setup(sb); } });
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = []; globalThis.__calls = []; globalThis.__lists = 0;
    globalThis.__toasts = []; toast = (m) => __toasts.push(m);
    globalThis.__teams = ${JSON.stringify([TEAM1])}; globalThis.__groups = [];
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb, gid) => { const h = { ids, mgr, gid: gid || null, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s })) }; };
    T.api = async (v, p, o) => { __calls.push(p);
      if (p === '/api/owner/teams') { const t = typeof __teams === 'function' ? await __teams() : __teams;
        if (t === 'fail') throw new Error('agent_offline'); return { teams: JSON.parse(JSON.stringify(t)) }; }
      if (p === '/api/owner/monitor/groups') { if (__groups === 'fail') throw new Error('agent_offline'); return { groups: JSON.parse(JSON.stringify(__groups)) }; }
      if (p.endsWith('/file')) return { path: o.query.path, name: o.query.path, size: 5, mtime: 1, kind: 'text', text: 'x' };
      return {}; };
    fetchRecentMessages = async () => [];
    loadMonaco = async () => ({ editor: { createModel: (t, l) => ({ t, l, dispose() {} }),
      create: (h, o) => ({ model: o.model, setModel(m) { this.model = m; }, dispose() {}, saveViewState() { return { top: 1 }; },
        restoreViewState() {}, updateOptions() {}, focus() {}, getAction() { return null; } }) } });
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p', mtime: 2 },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q', mtime: 1 }];
    ${pre}`);
  if (local) ev(ctx, "RELAY_APP_URL = ''");   // 로컬 부팅의 릴레이 PC 목록 동기화(15초 타이머)는 이 검사와 무관
  ctx.ensurePriv = async () => ({});          // 2차 인증 유예 안(코드를 묻지 않음)
  ctx.confirm = () => true;
  if (ready) { await ev(ctx, "loadList()"); await tick(); ev(ctx, "__toasts.length = 0; __calls.length = 0; __lists = 0;"); }
  return { ctx, pd, app };
}

// 지금 상세 칸에 무엇이 떠 있나 — 종류:대상(터미널 탭·파일·세션·관제 id). 칸 모드 클래스가 안 맞으면 '?'
const at = (ctx) => ev(ctx, `(() => { const k = PANE_NOW.kind, p = paneDetail().classList;
  if (k === 'terminal') return 'terminal:' + String(termView.active || '').slice(0, 1) + (p.contains('term-mode') ? '' : '?');
  if (k === 'files') return 'files:' + String(FB.active || '').split('|')[1] + (p.contains('fb-mode') ? '' : '?');
  if (k === 'viewer' || k === 'detail') return k + ':' + String(PANE_NOW.sid || '').slice(0, 1);
  if (k === 'monitor') return 'monitor:' + (MON_INLINE && MON_INLINE.gid) + (p.contains('mon-mode') ? '' : '?');
  return k; })()`);
// 누르고 그 화면이 설 때까지 — 터미널만 짓는 데(xterm 첫 로드·attach 타이머) 시간이 걸리고 나머지는 마이크로태스크로 끝난다
const press = async (ctx, k) => { await ctx.quickGo(k); await tick(k === "terminal" ? RENDER : 0); };
const counts = (ctx) => JSON.parse(ev(ctx, "JSON.stringify({ tc: __tc.length, mon: __mon.length, api: __calls.length, lists: __lists })"));
const backBtn = (pd) => pd.children.find((c) => String(c.className).startsWith("backbar")).children[0];   // '‹ 목록'·'‹ 탐색기'
async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
const all = (paths, want) => Object.fromEntries(paths.map((p) => [p, want]));
const NONE_TERM = "열어 둔 터미널이 없습니다 — 세션 줄의 🖥/▶ 로 여세요";
const NONE_FILE = "열어 둔 파일이 없습니다 — 파일을 고르세요";
const NONE_VIEW = "최근에 본 대화가 없습니다 — 세션 ⋯ → 💬 대화 보기";
const OFF = "꺼진 터미널이라 상세로 열었습니다 — ▶ 재개로 다시 띄울 수 있어요";
const CANT = "그 팀 관제를 열 수 없습니다";

// ---------------------------------------------------------------- 네 화면 사이 모든 방향
test("T70 바로가기: 네 화면(터미널·파일·뷰어·관제) 사이 모든 방향으로 마지막 상태 그대로 오간다 — 떠나는 화면은 정리하고 새 탭·새 claude 는 만들지 않는다(릴레이·로컬)", async () => {
  // 완전 방향 그래프의 오일러 회로 — 12 방향을 한 번씩(터미널 → 파일 → 터미널 → 뷰어 → … → 관제 → 터미널)
  const ROUTE = ["terminal", "files", "terminal", "viewer", "terminal", "monitor", "files", "viewer", "files", "monitor", "viewer", "monitor", "terminal"];
  const WANT = { terminal: "terminal:a", files: "files:a.py", viewer: "viewer:b", monitor: "monitor:g1" };
  const got = await each(["릴레이", "로컬"], async (mode) => {
    const { ctx } = await boot({ local: mode === "로컬", mem: {} });
    const seen = [];
    for (const k of ROUTE) { await press(ctx, k); seen.push(at(ctx)); }
    return { seen: seen.join(" "), tabs: ev(ctx, "TABS.list.map((t) => t.key.slice(0, 1)).join(',')"), resumes: ev(ctx, "__res.length"),
             live: { term: ev(ctx, "__tc.filter((h) => !h.closed).length"), viewer: ev(ctx, "__mon.filter((h) => h.mgr === h.ids && !h.closed).length"),
                     monitor: ev(ctx, "__mon.filter((h) => h.gid && !h.closed).length") },
             opened: { viewer: ev(ctx, "__mon.filter((h) => h.mgr === h.ids).length"), monitor: ev(ctx, "__mon.filter((h) => h.gid).length") } };
  });
  const want = (term) => ({ seen: ROUTE.map((k) => WANT[k]).join(" "), tabs: "a", resumes: 0,
                            live: { term, viewer: 0, monitor: 0 }, opened: { viewer: 3, monitor: 3 } });
  assert.deepEqual(got, { "릴레이": want(1), "로컬": want(0) });   // 로컬 터미널은 터미널 창(iframe)이라 스트림 없음
});

// ---------------------------------------------------------------- 이미 그 화면
test("T71 이미 그 화면이면 다시 짓지 않는다 — 세대·칸·연결 그대로, 터미널·편집기는 커서만 옮긴다(PC·폰, 폰 관제 덮개도)", async () => {
  const paths = ["PC|terminal", "PC|files", "PC|viewer", "PC|detail", "PC|monitor", "폰|terminal", "폰|files", "폰|viewer", "폰|monitor"];
  const got = await each(paths, async (p) => {
    const [dev, kind] = p.split("|");
    const { ctx, pd, app } = await boot({ width: dev === "폰" ? 390 : 1400, mem: { viewer: { sid: B, kind: kind === "detail" ? "detail" : "viewer", name: "B" } } });
    const go = kind === "detail" ? "viewer" : kind;
    await press(ctx, go);
    ev(ctx, `globalThis.__focus = 0; { const c = termView.cur(); if (c && c.term) c.term.focus = () => { __focus++; }; }
             if (FB.ed) FB.ed.focus = () => { __focus++; }; globalThis.__ov0 = MON_OV;`);
    const gen = ev(ctx, "PANE_NOW.gen"), first = pd.children[0], c0 = counts(ctx);
    await press(ctx, go);
    return { same: ev(ctx, "PANE_NOW.gen") === gen && pd.children[0] === first && ev(ctx, "MON_OV === __ov0"),
             quiet: JSON.stringify(counts(ctx)) === JSON.stringify(c0), focus: ev(ctx, "__focus"), at: at(ctx),
             ov: ev(ctx, "MON_OV && MON_OV.gid"), view: app.getAttribute("data-view") };
  });
  const ok = (at, focus, ov = null, view = "detail") => ({ same: true, quiet: true, focus, at, ov, view });
  assert.deepEqual(got, {
    "PC|terminal": ok("terminal:a", 1), "PC|files": ok("files:a.py", 1), "PC|viewer": ok("viewer:b", 0), "PC|detail": ok("detail:b", 0),
    "PC|monitor": ok("monitor:g1", 0),
    "폰|terminal": ok("terminal:a", 1), "폰|files": ok("files:a.py", 1), "폰|viewer": ok("viewer:b", 0),
    "폰|monitor": ok("empty", 0, "g1", "list"),                              // 폰 관제는 상세 칸 밖 덮개 — 그대로
  });
});

// ---------------------------------------------------------------- 폰 숨김 복귀 · 끊긴 화면
test("T72 폰: '‹ 목록'으로 숨겨 둔 화면은 다시 보여 주기만 한다(loadTerminal 0회·재접속 없음) — 끊겼으면 같은 탭에 다시 붙고, 뷰어 실시간·관제가 끊겼으면 다시 연다", async () => {
  const paths = ["폰|terminal", "폰|terminal 끊김", "폰|files", "폰|viewer", "폰|viewer 끊김", "폰|detail", "PC|monitor 끊김", "폰|monitor 끊김"];
  const CUT = { terminal: "__tc.at(-1).cb(null, true)", viewer: "__mon.filter((h) => h.mgr === h.ids).at(-1).cb(null, true)",
                monitor: "__mon.filter((h) => h.gid).at(-1).cb(null, true)" };
  const got = await each(paths, async (p) => {
    const [dev, what] = p.split("|"), [kind, cut] = what.split(" ");
    const { ctx, pd, app } = await boot({ width: dev === "폰" ? 390 : 1400, mem: { viewer: { sid: B, kind: kind === "detail" ? "detail" : "viewer", name: "B" } } });
    const go = kind === "detail" ? "viewer" : kind;
    await press(ctx, go);
    if (dev === "폰" && kind !== "monitor") { backBtn(pd).onclick(); if (app.getAttribute("data-view") !== "list") throw new Error("숨기지 못함"); }
    if (cut) ev(ctx, CUT[kind]);                                                 // 숨긴 사이 앱 전환 등으로 연결이 끊김(eof)
    ev(ctx, "globalThis.__lt = 0; globalThis.__realLT = loadTerminal; loadTerminal = (...a) => { __lt++; return __realLT(...a); }; globalThis.__ov0 = MON_OV;");
    const gen = ev(ctx, "PANE_NOW.gen"), c0 = counts(ctx);
    await press(ctx, go);
    const c1 = counts(ctx);
    return { view: app.getAttribute("data-view"), at: at(ctx), rebuilt: ev(ctx, "PANE_NOW.gen") !== gen, loadTerminal: ev(ctx, "__lt"),
             newTerm: c1.tc - c0.tc, newMon: c1.mon - c0.mon, ovNew: ev(ctx, "MON_OV !== __ov0") };
  });
  const r = (view, at, rebuilt, loadTerminal, newTerm, newMon, ovNew = false) => ({ view, at, rebuilt, loadTerminal, newTerm, newMon, ovNew });
  assert.deepEqual(got, {
    "폰|terminal": r("detail", "terminal:a", false, 0, 0, 0),
    "폰|terminal 끊김": r("detail", "terminal:a", true, 1, 1, 0),             // 같은 탭에 다시 붙기(빠른 길 — 탭 배지는 그대로)
    "폰|files": r("detail", "files:a.py", false, 0, 0, 0),
    "폰|viewer": r("detail", "viewer:b", false, 0, 0, 0),
    "폰|viewer 끊김": r("detail", "viewer:b", true, 0, 0, 1),
    "폰|detail": r("detail", "detail:b", false, 0, 0, 0),
    "PC|monitor 끊김": r("detail", "monitor:g1", true, 0, 0, 1),
    "폰|monitor 끊김": r("list", "empty", false, 0, 0, 1, true),               // 덮개를 새로(앞 덮개·연결은 닫힘)
  });
});

// ---------------------------------------------------------------- 기억 없음(결정 5)
test("T73 기억이 없으면(결정 5) 안내하고 목록 칸을 그 보기로 바꾼다(PC 는 접혀 있으면 펼침, 폰은 목록 칸으로) — 상세 칸은 그대로. 관제는 팀이 1개면 바로 연다", async () => {
  const paths = ["PC|terminal", "PC|files", "PC|viewer", "PC|monitor 팀 0", "PC|monitor 팀 1", "PC|monitor 팀 2", "폰|terminal", "폰|files", "폰|monitor 팀 2"];
  const got = await each(paths, async (p) => {
    const [dev, what] = p.split("|"), [kind, , n] = what.split(" ");
    const teams = { "0": [PORTFOLIO, { ...TEAM2, archived: true }, { ...TEAM1, members: [] }], "1": [PORTFOLIO, TEAM1], "2": [TEAM1, TEAM2] }[n || "1"];
    const start = kind === "terminal" || kind === "viewer" ? "files" : "sessions";   // 바뀌는지 보이게 다른 보기에서 시작
    const { ctx, app } = await boot({ width: dev === "폰" ? 390 : 1400, mem: { tabs: null, files: null, viewer: null, monitor: null },
      setup: (sb) => sb.localStorage.setItem("sm_list_view", start), pre: `__teams = ${JSON.stringify(teams)};` });
    if (dev === "PC") app.classList.add("side-off"); else app.setAttribute("data-view", "detail");
    await press(ctx, kind);
    return { toast: ev(ctx, "__toasts.join('|')"), view: ev(ctx, "LIST_VIEW"), sideOff: app.classList.contains("side-off"),
             dataView: app.getAttribute("data-view"), at: at(ctx) };
  });
  const r = (toast, view, sideOff, dataView, at = "empty") => ({ toast, view, sideOff, dataView, at });
  assert.deepEqual(got, {
    "PC|terminal": r(NONE_TERM, "sessions", false, "list"),
    "PC|files": r(NONE_FILE, "files", false, "list"),
    "PC|viewer": r(NONE_VIEW, "sessions", false, "list"),
    "PC|monitor 팀 0": r("관제할 팀이 없습니다", "sessions", true, "list"),              // 비서·보관·관리 없는 팀은 관제 대상이 아니다
    "PC|monitor 팀 1": r("", "sessions", true, "detail", "monitor:g1"),                  // 읽기 전용이라 바로 연다
    "PC|monitor 팀 2": r("관제할 팀을 고르세요 — 팀 ⋯ → 🎬 관제 열기", "groups", false, "list"),
    "폰|terminal": r(NONE_TERM, "sessions", false, "list"),
    "폰|files": r(NONE_FILE, "files", false, "list"),
    "폰|monitor 팀 2": r("관제할 팀을 고르세요 — 팀 ⋯ → 🎬 관제 열기", "groups", false, "list"),
  });
});

// ---------------------------------------------------------------- 꺼진 터미널(결정 4)
test("T74 꺼진 터미널(결정 4): 목록을 한 번 새로 받아 판정하고, 그래도 꺼졌으면 그 탭을 닫고 그 세션 상세로 — 새 claude 는 띄우지 않는다. 목록을 못 받으면 상세로 가되 탭은 둔다", async () => {
  const paths = ["떠났다 돌아옴", "중지됨으로 되살린 탭", "보던 터미널의 claude 가 끝남", "목록 못 받음(본 지 오래)"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ mem: {}, pre: p === "중지됨으로 되살린 탭" ? "SESSIONS[0].live_terminal = false;" : "" });
    if (p !== "중지됨으로 되살린 탭") { await ctx.quickGo("terminal"); await tick(RENDER); }
    if (p === "떠났다 돌아옴" || p.startsWith("목록 못 받음")) { await ctx.quickGo("viewer"); await tick(); }
    if (p === "보던 터미널의 claude 가 끝남") ev(ctx, "__tc.at(-1).cb('세션이 종료되었습니다'); __tc.at(-1).cb(null, true);");
    if (p.startsWith("목록 못 받음")) ev(ctx, "TABS.list[0].seenLiveAt = Date.now() - 3600 * 1000;");   // 떠난 지 한 시간(앱을 내려 둠)
    // 20초 목록: 끝난 직후엔 아직 살아 있다고 할 수 있다. 새로 받은 목록은 꺼짐
    ev(ctx, `SESSIONS[0].live_terminal = ${p === "보던 터미널의 claude 가 끝남"};
      T.list = async () => { __lists++; ${p.startsWith("목록 못 받음") ? "throw new Error('disconnected');" : ""}
        return { sessions: SESSIONS.map((s) => ({ ...s, live_terminal: false })) }; };`);
    const c0 = counts(ctx); ev(ctx, "__toasts.length = 0");
    await ctx.quickGo("terminal"); await tick(RENDER);
    return { at: at(ctx), tabs: ev(ctx, "TABS.list.length"), newTerm: counts(ctx).tc - c0.tc, lists: counts(ctx).lists - c0.lists, toast: ev(ctx, "__toasts.join('|')") };
  });
  assert.deepEqual(got, {
    "떠났다 돌아옴": { at: "detail:a", tabs: 0, newTerm: 0, lists: 1, toast: OFF },
    "중지됨으로 되살린 탭": { at: "detail:a", tabs: 0, newTerm: 0, lists: 1, toast: OFF },
    "보던 터미널의 claude 가 끝남": { at: "detail:a", tabs: 0, newTerm: 0, lists: 1, toast: OFF },
    "목록 못 받음(본 지 오래)": { at: "detail:a", tabs: 1, newTerm: 0, lists: 1, toast: "터미널 상태를 확인하지 못해 상세로 열었습니다" },
  });
});

// ---------------------------------------------------------------- 갓 띄운 터미널의 낡은 목록
test("T75 방금 띄운 터미널(20초 목록은 아직 꺼짐)로 바로가기를 누르면 목록을 새로 받아 살아 있음을 보고 그 탭으로 간다 — 탭을 버리거나 상세로 오판하지 않는다. 목록을 못 받아도 방금 본 탭이면 그 탭으로", async () => {
  const paths = ["새 목록은 살아 있음", "목록 못 받음(방금 봄)"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ mem: { tabs: null } });
    ev(ctx, "SESSIONS.forEach((s) => { s.live_terminal = false; })");            // 20초 목록은 아직 이 PTY 를 모른다
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);   // ▶ 재개 → 터미널(새로 띄움)
    await ctx.quickGo("viewer"); await tick();                                    // 20초 안에 뷰어로
    ev(ctx, `T.list = async () => { __lists++; ${p.startsWith("목록 못 받음") ? "throw new Error('disconnected');" : ""}
      return { sessions: SESSIONS.map((s) => ({ ...s, live_terminal: s.session_id === '${A}' })) }; };`);
    const c0 = counts(ctx); ev(ctx, "__toasts.length = 0");
    await ctx.quickGo("terminal"); await tick(RENDER);
    return { at: at(ctx), tabs: ev(ctx, "TABS.list.length"), newTerm: counts(ctx).tc - c0.tc, lists: counts(ctx).lists - c0.lists, toast: ev(ctx, "__toasts.join('|')") };
  });
  assert.deepEqual(got, all(paths, { at: "terminal:a", tabs: 1, newTerm: 1, lists: 1, toast: "" }));
});

// ---------------------------------------------------------------- 지워진 세션
test("T76 지워진·옮겨진 세션: 터미널 탭은 조용히 내리고 다음 최근 탭으로(없으면 '열어 둔 터미널 없음'), 뷰어는 그 기억만 지우고 알린다 — 지금 화면은 그대로", async () => {
  const paths = ["터미널: 다음 탭", "터미널: 남은 탭 없음", "뷰어"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ mem: { tabs: [A, B] } });                      // 마지막 탭 A, 최근 본 순서 A → B
    const keep = p === "뷰어" ? `s.session_id !== '${B}'` : p.endsWith("다음 탭") ? `s.session_id !== '${A}'` : "false";
    ev(ctx, `SESSIONS = SESSIONS.filter((s) => ${keep})`);                         // 다른 기기에서 지움·옮김(20초 목록에서 빠짐)
    const c0 = counts(ctx);
    await press(ctx, p === "뷰어" ? "viewer" : "terminal");
    const pane = JSON.parse(ctx.localStorage.getItem("sm_pane_relay"));
    return { at: at(ctx), tabs: ev(ctx, "TABS.list.map((t) => t.key.slice(0, 1)).join(',')"),
             saved: JSON.parse(ctx.localStorage.getItem("sm_tabs_relay")).list.length, viewerMem: (pane.viewer || {}).sid || null,
             toast: ev(ctx, "__toasts.join('|')"), lists: counts(ctx).lists - c0.lists };
  });
  assert.deepEqual(got, {
    "터미널: 다음 탭": { at: "terminal:b", tabs: "b", saved: 1, viewerMem: B, toast: "", lists: 0 },
    "터미널: 남은 탭 없음": { at: "empty", tabs: "", saved: 0, viewerMem: B, toast: NONE_TERM, lists: 0 },
    "뷰어": { at: "empty", tabs: "a,b", saved: 2, viewerMem: null, toast: "그 세션은 목록에 없습니다", lists: 0 },
  });
});

// ---------------------------------------------------------------- 이어받음
test("T77 이어받은 세션: 뷰어·상세 기억이 옛 세션(continued-in)이면 이어받은 세션으로 열고 알린다 — 기억도 새 세션으로", async () => {
  const got = await each(["viewer", "detail"], async (kind) => {
    const { ctx } = await boot({ mem: { viewer: { sid: A, kind, name: "A" } },
      pre: `SESSIONS[0].continued_in = '${C}'; SESSIONS.push({ session_id: '${C}', title: 'C', live_terminal: false, cwd: 'E:/p', mtime: 3 });` });
    await ctx.quickGo("viewer"); await tick();
    return { at: at(ctx), toast: ev(ctx, "__toasts.join('|')"), mem: JSON.parse(ctx.localStorage.getItem("sm_pane_relay")).viewer.sid };
  });
  assert.deepEqual(got, { viewer: { at: "viewer:c", toast: "이어받은 세션으로 열었습니다", mem: C },
                          detail: { at: "detail:c", toast: "이어받은 세션으로 열었습니다", mem: C } });
});

// ---------------------------------------------------------------- 팀 관제: 팀 삭제와 통신 실패 구분
test("T78 팀 관제: 팀 코드로 지금 팀을 찾아 연다 — 삭제·보관·관리 세션 없음이면 그 기억만 지우고 알리고, 통신 실패면 기억을 두고 '연결을 확인하세요'", async () => {
  const SETUP = {
    "팀 삭제": { teams: [], groups: [] },
    "보관된 팀": { teams: [{ ...TEAM1, archived: true }], groups: [] },
    "관리 세션 없음": { teams: [{ ...TEAM1, members: [{ member_role: "worker", session_id: "w1", alias: "일꾼" }] }], groups: [] },
    "팀 목록 통신 실패": { teams: "fail", groups: [] },
    "관제 목록 통신 실패": { teams: [], groups: "fail" },
    "팀이 아닌 옛 관제": { teams: [], groups: [{ id: "g1", name: "옛 관제", manager: "m1", subs: [] }] },
    "보관했다 푼 팀(관제 id 바뀜)": { teams: [{ ...TEAM1, monitor_group_id: "g9" }], groups: [] },
    "옛 sm_mon_last 만": { teams: [TEAM1, TEAM2], groups: [], old: true },
  };
  const got = await each(Object.keys(SETUP), async (p) => {
    const s = SETUP[p];
    const { ctx } = await boot({ mem: s.old ? { monitor: null } : {}, setup: (sb) => { if (s.old) sb.localStorage.setItem("sm_mon_last", "g1"); },
      pre: `__teams = ${JSON.stringify(s.teams)}; __groups = ${JSON.stringify(s.groups)};` });
    await ctx.quickGo("monitor"); await tick();
    const mem = JSON.parse(ctx.localStorage.getItem("sm_pane_relay") || "{}").monitor || null;
    return { at: at(ctx), toast: ev(ctx, "__toasts.join('|')"), mem: mem && [mem.team, mem.gid],
             groupsAsked: ev(ctx, "__calls.filter((p) => p === '/api/owner/monitor/groups').length") };
  });
  assert.deepEqual(got, {
    "팀 삭제": { at: "empty", toast: CANT, mem: null, groupsAsked: 1 },                     // 팀 목록에도 관제 목록에도 없음
    "보관된 팀": { at: "empty", toast: CANT, mem: null, groupsAsked: 0 },
    "관리 세션 없음": { at: "empty", toast: CANT, mem: null, groupsAsked: 0 },
    "팀 목록 통신 실패": { at: "empty", toast: "팀 목록을 받지 못했습니다 — 연결을 확인하세요", mem: ["T1", "g1"], groupsAsked: 0 },
    "관제 목록 통신 실패": { at: "empty", toast: "관제 목록을 받지 못했습니다 — 연결을 확인하세요", mem: ["T1", "g1"], groupsAsked: 1 },
    "팀이 아닌 옛 관제": { at: "monitor:g1", toast: "", mem: ["T1", "g1"], groupsAsked: 1 },  // 관제 그룹 목록의 id 로 한 번 더
    "보관했다 푼 팀(관제 id 바뀜)": { at: "monitor:g9", toast: "", mem: ["T1", "g9"], groupsAsked: 0 },   // 팀 코드를 먼저 쓴다
    "옛 sm_mon_last 만": { at: "monitor:g1", toast: "", mem: ["T1", "g1"], groupsAsked: 0 },   // 새 기억이 없을 때 한 번 참고
  });
});

// ---------------------------------------------------------------- 2차 인증 취소 · 인계 거절
test("T79 터미널 바로가기의 관문(2차 인증 취소·화면 인계 거절)에서 멈추면 지금 화면 그대로 — 보던 뷰어 실시간·관제 연결이 살아 있다", async () => {
  const paths = ["뷰어|2차 인증 취소", "뷰어|인계 거절", "관제|2차 인증 취소", "관제|인계 거절"];
  const got = await each(paths, async (p) => {
    const [from, gate] = p.split("|");
    const { ctx, pd } = await boot({ mem: {} });
    await ctx.quickGo(from === "뷰어" ? "viewer" : "monitor"); await tick();
    const where = at(ctx);
    let asked = 0;
    if (gate === "2차 인증 취소") ctx.ensurePriv = async () => { asked++; return null; };
    else { ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' }"); ctx.confirm = () => { asked++; return false; }; }
    await ctx.quickGo("terminal"); await tick(RENDER);
    return { same: at(ctx) === where, where, asked, viewerLive: ev(ctx, "!!_liveViewer && !_liveViewer.closed"),
             monLive: ev(ctx, "!!MON_INLINE && !MON_INLINE.dead() && !__mon.filter((h) => h.gid).some((h) => h.closed)"),
             terms: ev(ctx, "__tc.length"), termMode: pd.classList.contains("term-mode") };
  });
  const r = (where, viewerLive, monLive) => ({ same: true, where, asked: 1, viewerLive, monLive, terms: 0, termMode: false });
  assert.deepEqual(got, { "뷰어|2차 인증 취소": r("viewer:b", true, false), "뷰어|인계 거절": r("viewer:b", true, false),
                          "관제|2차 인증 취소": r("monitor:g1", false, true), "관제|인계 거절": r("monitor:g1", false, true) });
});

// ---------------------------------------------------------------- 연타
test("T80 연달아 누르면 마지막 누름만 화면을 바꾼다 — 2차 인증·팀 목록·터미널 상태 확인·대화를 기다리던 앞 누름은 늦게 끝나도 화면을 덮지 않는다", async () => {
  const PAIRS = { "터미널(2차 인증 중) → 뷰어": ["terminal", "viewer"], "관제(팀 목록 받는 중) → 파일": ["monitor", "files"],
                  "터미널(상태 확인 중) → 뷰어": ["terminal", "viewer"], "뷰어(대화 받는 중) → 터미널": ["viewer", "terminal"],
                  "터미널 두 번(2차 인증 중)": ["terminal", "terminal"] };
  const got = await each(Object.keys(PAIRS), async (p) => {
    const { ctx } = await boot({ mem: {}, pre: p.startsWith("터미널(상태") ? "SESSIONS[0].live_terminal = false;" : "" });
    const rel = [];
    if (p.includes("2차 인증 중")) ctx.ensurePriv = () => new Promise((r) => rel.push(() => r({})));
    if (p.startsWith("관제")) ev(ctx, `globalThis.__tw = []; __teams = () => new Promise((r) => __tw.push(() => r(${JSON.stringify([TEAM1])})));`);
    if (p.startsWith("터미널(상태")) ev(ctx, "globalThis.__lr = []; T.list = () => { __lists++; return new Promise((r) => __lr.push(r)); };");
    if (p.startsWith("뷰어")) ev(ctx, "globalThis.__late = []; fetchRecentMessages = () => new Promise((r) => __late.push(r));");
    const [first, second] = PAIRS[p];
    const p1 = ctx.quickGo(first); await tick(20);
    const p2 = ctx.quickGo(second); await tick(20);
    while (rel.length) rel.shift()();                                            // 앞 누름이 기다리던 것이 늦게 끝남
    if (p.startsWith("관제")) ev(ctx, "__tw.forEach((f) => f())");
    if (p.startsWith("터미널(상태")) ev(ctx, "__lr.forEach((r) => r({ sessions: SESSIONS.map((s) => ({ ...s, live_terminal: true })) }))");
    if (p.startsWith("뷰어")) ev(ctx, "__late.forEach((r) => r([]))");
    await p1; await p2; await tick(RENDER);
    return { at: at(ctx), terms: ev(ctx, "__tc.length"), monitors: ev(ctx, "__mon.filter((h) => h.gid).length"),
             viewers: ev(ctx, "__mon.filter((h) => h.mgr === h.ids && !h.closed).length"), tabs: ev(ctx, "TABS.list.length") };
  });
  assert.deepEqual(got, {
    "터미널(2차 인증 중) → 뷰어": { at: "viewer:b", terms: 0, monitors: 0, viewers: 1, tabs: 1 },
    "관제(팀 목록 받는 중) → 파일": { at: "files:a.py", terms: 0, monitors: 0, viewers: 0, tabs: 1 },
    "터미널(상태 확인 중) → 뷰어": { at: "viewer:b", terms: 0, monitors: 0, viewers: 1, tabs: 1 },   // 늦은 판정이 탭을 버리지도 않는다
    "뷰어(대화 받는 중) → 터미널": { at: "terminal:a", terms: 1, monitors: 0, viewers: 0, tabs: 1 },
    "터미널 두 번(2차 인증 중)": { at: "terminal:a", terms: 1, monitors: 0, viewers: 0, tabs: 1 },
  });
});

// ---------------------------------------------------------------- 목록 전 누름
test("T81 첫 목록 전(새로고침 직후·PC 전환 직후)에는 바로가기를 받지 않는다 — '목록을 받는 중입니다', 화면·기억·연결 그대로, 기다리던 알림 진입도 끊지 않는다", async () => {
  const { ctx } = await boot({ ready: false, mem: {} });
  const pane0 = ctx.localStorage.getItem("sm_pane_relay");
  ev(ctx, "globalThis.__lr = []; T.list = () => { __lists++; return new Promise((r) => __lr.push(r)); };");
  const pOpen = ctx.openSessionById(B); await tick();                          // 알림으로 앱을 엶 — 목록을 받는 중
  for (const k of ["terminal", "files", "viewer", "monitor"]) await ctx.quickGo(k);
  await tick(RENDER);
  const waiting = { toasts: ev(ctx, "__toasts.join('|')"), at: at(ctx), api: ev(ctx, "__calls.length"), conns: ev(ctx, "__tc.length + __mon.length"),
                    pane: ctx.localStorage.getItem("sm_pane_relay") === pane0, ready: ev(ctx, "LIST_READY") };
  ev(ctx, "__lr[0]({ sessions: SESSIONS.map((s) => ({ ...s })) })"); await pOpen; await tick(RENDER);
  const opened = at(ctx);
  ev(ctx, "__toasts.length = 0; LIST_READY = true; pcSwitchReset();");         // PC 를 바꾼 직후(새 PC 의 첫 목록 전)
  await ctx.quickGo("viewer"); await tick();
  assert.deepEqual({ waiting, opened, afterSwitch: { toasts: ev(ctx, "__toasts.join('|')"), at: at(ctx) } }, {
    waiting: { toasts: Array(4).fill("목록을 받는 중입니다").join("|"), at: "empty", api: 0, conns: 0, pane: true, ready: false },
    opened: "terminal:b",                                                         // 알림 진입은 그대로 끝까지
    afterSwitch: { toasts: "목록을 받는 중입니다", at: "empty" },
  });
});

// ---------------------------------------------------------------- quickTargets
const pick = (ctx) => JSON.parse(ev(ctx, "JSON.stringify(quickTargets().map((o) => [o.kind, o.has, o.name, o.sub, o.dim, o.taken, o.wait]))"));

test("T82 quickTargets: 네 칸(터미널·파일·뷰어·관제)의 있음·이름·보조 글·흐림·👀 — 첫 목록 전엔 모두 흐림(준비 중), 기억이 없으면 흐림, 새 연결은 열지 않는다", async () => {
  const { ctx } = await boot({ ready: false, mem: { tabs: [A, B], files: ["a.py", "b.md"] },
    pre: "SESSIONS[0].runtime = { phase: 'waiting', waiting_at: Date.now() / 1000 };" });
  const c0 = counts(ctx);
  const before = pick(ctx);
  const quietBefore = JSON.stringify(counts(ctx)) === JSON.stringify(c0);
  await ev(ctx, "loadList()"); await tick();
  const c1 = counts(ctx);
  const after = pick(ctx);
  const quietAfter = JSON.stringify(counts(ctx)) === JSON.stringify(c1);
  const none = await boot({ mem: { tabs: null, files: null, viewer: null, monitor: null } });
  assert.deepEqual({ before, after, none: pick(none.ctx), quietBefore, quietAfter }, {
    before: [["terminal", false, "", "열어 둔 터미널 없음", true, false, true], ["files", true, "a.py", "파일 2개", true, false, true],
             ["viewer", true, "B", "방금", true, false, true], ["monitor", true, "팀1", "", true, false, true]],
    after: [["terminal", true, "A", "입력 대기 · 탭 2개", false, false, false], ["files", true, "a.py", "파일 2개", false, false, false],
            ["viewer", true, "B", "방금", false, false, false], ["monitor", true, "팀1", "", false, false, false]],
    none: [["terminal", false, "", "열어 둔 터미널 없음", true, false, false], ["files", false, "", "열어 둔 파일 없음", true, false, false],
           ["viewer", false, "", "최근에 본 대화 없음", true, false, false], ["monitor", false, "", "최근에 연 관제 없음", true, false, false]],
    quietBefore: true, quietAfter: true,
  });
});

test("T83 quickTargets 상태: 👀(다른 기기가 보는 중·인계 통지)·꺼짐·목록에 없음·이어받음·ℹ 상세 이름 · 지금 화면(cur)·폰에서 숨겨 둔 화면(back)", async () => {
  const item = (ctx, kind) => JSON.parse(ev(ctx, `JSON.stringify(quickTargets().find((o) => o.kind === '${kind}'))`));
  const sub = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k]]));
  const T = ["taken", "sub", "cur"], V = ["icon", "label", "name", "sub", "dim"];
  const got = {};
  { const { ctx } = await boot({ mem: {} });                                    // 20초 목록: 다른 기기 화면이 붙어 있음(이 기기는 안 붙음)
    ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' }"); got.other = sub(item(ctx, "terminal"), T); }
  { const { ctx } = await boot({ mem: {} });                                    // 보던 터미널에 인계 통지가 옴
    await ctx.quickGo("terminal"); await tick(RENDER);
    ev(ctx, "__tc.at(-1).cb('\\r\\n' + TAKEOVER_NOTE + '\\r\\n'); __tc.at(-1).cb(null, true);"); got.notice = sub(item(ctx, "terminal"), T); }
  { const { ctx } = await boot({ mem: {} });                                    // 내가 붙어 보는 중 — 20초 목록이 옛 화면(앞 기기)을 보여도 아니다
    await ctx.quickGo("terminal"); await tick(RENDER);
    ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-old-device' }"); got.mine = sub(item(ctx, "terminal"), T); }
  { const { ctx } = await boot({ mem: {}, pre: "SESSIONS[0].live_terminal = false;" }); got.off = sub(item(ctx, "terminal"), T); }
  { const { ctx } = await boot({ mem: { viewer: { sid: C, kind: "viewer", name: "C" } } }); got.gone = sub(item(ctx, "viewer"), V); }
  { const { ctx } = await boot({ mem: { viewer: { sid: A, kind: "viewer", name: "A" } },
      pre: `SESSIONS[0].continued_in = '${C}'; SESSIONS.push({ session_id: '${C}', title: 'C', cwd: 'E:/p', mtime: 3 });` });
    got.continued = sub(item(ctx, "viewer"), V); }
  { const { ctx } = await boot({ mem: { viewer: { sid: B, kind: "detail", name: "B" } } }); got.detail = sub(item(ctx, "viewer"), V); }
  { const { ctx, pd } = await boot({ width: 390, mem: {} });                    // 폰: 터미널 → '‹ 목록' → 관제 덮개
    const cb = (o) => [o.cur, o.back];
    await ctx.quickGo("terminal"); await tick(RENDER);
    const shown = cb(item(ctx, "terminal"));
    backBtn(pd).onclick();
    const hidden = cb(item(ctx, "terminal"));
    await ctx.quickGo("monitor"); await tick();
    got.phone = { shown, hidden, overlay: { terminal: cb(item(ctx, "terminal")), monitor: cb(item(ctx, "monitor")), files: cb(item(ctx, "files")) } };
  }
  assert.deepEqual(got, {
    other: { taken: true, sub: "", cur: false },
    notice: { taken: true, sub: "", cur: true },
    mine: { taken: false, sub: "", cur: true },
    off: { taken: false, sub: "꺼짐", cur: false },
    gone: { icon: "💬", label: "뷰어", name: "C", sub: "목록에 없음 · 방금", dim: true },
    continued: { icon: "💬", label: "뷰어", name: "C", sub: "이어받음 · 방금", dim: false },
    detail: { icon: "ℹ", label: "상세", name: "B", sub: "방금", dim: false },
    phone: { shown: [true, false], hidden: [false, true], overlay: { terminal: [false, true], monitor: [true, false], files: [false, false] } },
  });
});

// ---------------------------------------------------------------- 폰 관제 덮개(§5-3)
test("T84 폰 관제 덮개에서 다른 화면을 고르면 덮개를 닫고(관제 연결도) 그 화면으로 간다 — 알림은 덮개를 닫은 뒤에, 관제를 다시 고르면 그대로(끊긴 덮개를 다시 열지 못하면 닫고 알림)", async () => {
  const GO = { "터미널": "terminal", "파일(기억 없음)": "files", "관제": "monitor", "관제(끊김·팀 목록 실패)": "monitor" };
  const got = await each(Object.keys(GO), async (p) => {
    const { ctx } = await boot({ width: 390, mem: p === "파일(기억 없음)" ? { files: null } : {} });
    await ctx.quickGo("monitor"); await tick();
    if (!ev(ctx, "MON_OV")) throw new Error("덮개가 안 열림");
    if (p.includes("끊김")) ev(ctx, "__mon.filter((x) => x.gid).at(-1).cb(null, true); __teams = 'fail';");   // 덮개 관제가 끊긴 채, PC 와도 통신이 안 됨
    ev(ctx, "globalThis.__log = []; { const h = __mon.filter((x) => x.gid).at(-1), c = h.close; h.close = () => { __log.push('close'); c(); }; } toast = (m) => __log.push('toast:' + m);");
    await press(ctx, GO[p]);
    return { closed: ev(ctx, "MON_OV === null"), log: ev(ctx, "__log.join('|')"), at: at(ctx) };
  });
  assert.deepEqual(got, {
    "터미널": { closed: true, log: "close", at: "terminal:a" },
    "파일(기억 없음)": { closed: true, log: "close|toast:" + NONE_FILE, at: "empty" },
    "관제": { closed: false, log: "", at: "empty" },
    "관제(끊김·팀 목록 실패)": { closed: true, log: "close|toast:팀 목록을 받지 못했습니다 — 연결을 확인하세요", at: "empty" },   // 덮개가 알림을 가리지 않게
  });
});

// ---------------------------------------------------------------- 파일(§4-2)
test("T85 파일 바로가기: 마지막 파일로 편집기를 연다 — 새로고침 뒤엔 마지막 파일 하나만 받고 같은 창에서 다시 오면 받지 않는다. 일시 오류(세션·폴더 못 찾음)는 지우고 다시 받고 '파일이 없습니다'는 그대로, 목록 칸 보기는 그대로", async () => {
  const { ctx } = await boot({ mem: { files: ["a.py", "b.md", "c.txt"] } });
  const fetched = () => ev(ctx, "__calls.filter((p) => p.endsWith('/file')).length");
  await press(ctx, "files");
  const first = { at: at(ctx), fetched: fetched(), tabs: ev(ctx, "FB.tabs.length"), view: ev(ctx, "LIST_VIEW") };
  await ctx.quickGo("viewer"); await tick();
  await press(ctx, "files");
  const again = { at: at(ctx), fetched: fetched() };
  ev(ctx, `FB.tabs[0].err = { error: 'no_session' }; FB.tabs[0].data = null;       // 목록 전에 열어 굳었던 것
           FB.tabs[1].err = { error: 'no_cwd' }; FB.tabs[2].err = { error: 'not_found' };`);
  await ctx.quickGo("viewer"); await tick();
  await press(ctx, "files");
  assert.deepEqual({ first, again, retry: { at: at(ctx), fetched: fetched(), errs: ev(ctx, "FB.tabs.map((t) => (t.err && t.err.error) || '-').join(',')") } }, {
    first: { at: "files:a.py", fetched: 1, tabs: 3, view: "sessions" },
    again: { at: "files:a.py", fetched: 1 },
    retry: { at: "files:a.py", fetched: 2, errs: "-,-,not_found" },
  });
});

// ---------------------------------------------------------------- 다른 기기가 가져간 터미널(§4-1)
test("T86 다른 기기가 가져간 터미널: 살아 있으면 바로가기도 인계 확인창을 거쳐서만 가져오고(자동으로 되찾지 않음), 가져간 뒤 claude 가 끝났으면 확인창 없이 탭을 닫고 상세로 — 새 claude 없음", async () => {
  const paths = ["살아 있음 → 확인 후 가져옴", "살아 있음 → 거절", "그 뒤 claude 끝남"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ mem: {} });
    await press(ctx, "terminal");
    ev(ctx, "__tc.at(-1).cb('\\r\\n' + TAKEOVER_NOTE + '\\r\\n'); __tc.at(-1).cb(null, true);");   // 다른 기기가 가져감(인계 통지 → 스트림 끝)
    let asked = 0; ctx.confirm = () => { asked++; return p !== "살아 있음 → 거절"; };
    if (p === "그 뒤 claude 끝남") ev(ctx, "SESSIONS[0].live_terminal = false; T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s, live_terminal: false })) }; };");
    else ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' }");
    const c0 = counts(ctx); ev(ctx, "__toasts.length = 0");
    const taken = ev(ctx, "quickTargets()[0].taken");
    await press(ctx, "terminal");
    return { taken, asked, at: at(ctx), newTerm: counts(ctx).tc - c0.tc, lists: counts(ctx).lists - c0.lists, tabs: ev(ctx, "TABS.list.length"), toast: ev(ctx, "__toasts.join('|')") };
  });
  assert.deepEqual(got, {
    "살아 있음 → 확인 후 가져옴": { taken: true, asked: 1, at: "terminal:a", newTerm: 1, lists: 0, tabs: 1, toast: "" },
    "살아 있음 → 거절": { taken: true, asked: 1, at: "terminal:a", newTerm: 0, lists: 0, tabs: 1, toast: "" },
    "그 뒤 claude 끝남": { taken: false, asked: 0, at: "detail:a", newTerm: 0, lists: 1, tabs: 0, toast: OFF },
  });
});
