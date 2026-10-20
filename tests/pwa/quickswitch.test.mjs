// 상세 칸 화면 바로가기(quick-switch) 0단계 안전망 — docs/designs/quick-switch.md §2-4·§8.
// 바로가기가 생기면 화면을 오가는 횟수가 늘어 기존 결함(§2-4)을 자주 밟는다. 고치기 전에 '고친 뒤의 동작'을 먼저
// 고정한다 — 그래서 1·2단계 전에는 실패하는 게 정상이다(T6=1-1, T8=1-2, T1=1-3, T10=1-4, T7=1-5, 나머지=2단계).
// 경로가 여럿인 검사는 경로마다 새로 부팅해 결과를 한 객체로 모아 비교한다 — 실패 한 번에 어긋난 경로가 다 보이게.
// T12~ 는 2단계에서 더한 검사(화면 진입 기록 PANE_NOW · 화면 기억 sm_pane_<PC> · 웹 재개 닫기 결정 7 · 늦은 응답).
// T20~ 는 3단계(PC 별 기억 — 터미널 탭·파일 기억 키, 옛 키 옮기기, 최근 본 순서, 릴레이 PC 전환 정리).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const RENDER = 350;   // 터미널 attach 의 resize 타이머(80·300ms)·renderTabBarSoon(300ms)이 지나가게
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

// 상세 칸·#app·머리 줄·목록 칸을 고정하고, 터미널·모니터·재개 연결을 기록하는 스텁을 심는다(기본 릴레이·PC 폭).
async function boot({ width = 1400, local = false, hash = "", setup = null } = {}) {
  const ctx = await load(local ? { hash, setup, location: LOCAL } : { hash, setup });
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = [];
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb) => { const h = { ids, mgr, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.api = async () => ({});
    fetchRecentMessages = async () => [];
    globalThis.MutationObserver = class { observe() {} disconnect() {} };   // loadResume 이 쓴다(하네스에 없음)
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  // 로컬 부팅의 릴레이 PC 목록 동기화(숨은 iframe + 15초 정리 타이머)는 이 검사와 무관 — 꺼서 프로세스가 15초 더 살지 않게
  if (local) ev(ctx, "RELAY_APP_URL = ''");
  ctx.ensurePriv = async () => ({});          // 2차 인증 유예 안(코드를 묻지 않음)
  ctx.confirm = () => true;
  return { ctx, pd, app };
}

// 상세 칸으로 들어가는 길 — 목록 줄·⋯ 메뉴·팀 ⋯·탭 칩이 부르는 함수 그대로
const GO = {
  loadViewer: "loadViewer(SESSIONS[1])",
  loadDetail: "loadDetail(SESSIONS[1])",
  loadResume: "loadResume(SESSIONS[1], true, false)",
  openLedger: "openLedger({ code: 'T1', name: '팀' })",
  detailPlaceholder: "detailPlaceholder()",
  fbShowEditor: "fbShowEditor()",
  loadTerminal: "loadTerminal(SESSIONS[1], false)",
};
const go = async (ctx, via) => { await ev(ctx, GO[via]); await tick(via === "loadTerminal" ? RENDER : 0); };

// 경로마다 새로 부팅해 결과를 모은다. 하네스 예외도 결과에 남겨 비교에서 드러나게 한다.
async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
const all = (paths, want) => Object.fromEntries(paths.map((p) => [p, want]));
const backBtn = (pd) => pd.children.find((c) => String(c.className).startsWith("backbar")).children[0];   // '‹ 목록'

// 이벤트 구동 WebSocket 스텁(phase4.test.mjs 와 같은 모양): 보낸 프레임을 기록하고, 테스트가 서버 프레임을 밀어 넣는다.
function fakeWs(ctx) {
  const sockets = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    close(code) { this.readyState = 3; this.onclose && this.onclose({ code: code || 1000 }); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    push(m) { this.onmessage({ data: JSON.stringify(m) }); }
  };
  return sockets;
}

// ---------------------------------------------------------------- 1-3 폰 무반응
test("T1 폰: '‹ 목록' 으로 숨긴 터미널은 같은 탭·같은 세션 줄·같은 세션 알림으로 다시 보인다(새 연결 없음)", async () => {
  const paths = ["같은 탭 switchTab", "같은 세션 줄", "같은 세션 알림"];
  const got = await each(paths, async (p) => {
    const { ctx, pd, app } = await boot({ width: 390 });
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    backBtn(pd).onclick();                                       // 상세 칸을 숨기기만 한다(터미널은 살아 있음)
    if (app.getAttribute("data-view") !== "list") throw new Error("'‹ 목록' 뒤 목록 칸이 아님");
    if (p === "같은 탭 switchTab") ctx.switchTab(ev(ctx, "TABS.active"));
    else if (p === "같은 세션 줄") ctx.jumpToSession(ev(ctx, "SESSIONS[0]"));   // 목록 줄 → 같은 탭 switchTab
    else await ctx.openSessionById(A);
    await tick(RENDER);
    return { view: app.getAttribute("data-view"), termMode: pd.classList.contains("term-mode"),
             conns: ev(ctx, "__tc.length"), live: !ev(ctx, "__tc[0].closed") };
  });
  assert.deepEqual(got, all(paths, { view: "detail", termMode: true, conns: 1, live: true }));
});

// ---------------------------------------------------------------- 2단계 화면 진입 정리(paneEnter)
test("T2 릴레이 터미널 → 다른 화면: 터미널 화면을 정리하고(연결·탭 배지 닫음) 뒤이은 끊김에도 터미널로 끌려가지 않는다", async () => {
  const paths = ["loadViewer", "loadDetail", "loadResume", "openLedger", "detailPlaceholder"];
  const got = await each(paths, async (via) => {
    const { ctx, pd } = await boot();
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    if (!pd.classList.contains("term-mode") || ev(ctx, "__mon.length") !== 1) throw new Error("출발이 터미널 + 탭 배지 구독이 아님");
    await go(ctx, via);
    const left = { mainCleared: ev(ctx, "termView.main === null"), termClosed: ev(ctx, "__tc[0].closed"),
                   badgeClosed: ev(ctx, "__mon[0].closed > 0 && tabBadge.h === null") };
    // 릴레이 소켓이 끊겨 스트림에 eof 가 온 뒤 재연결·화면 복귀 처리(reattachForegroundTab)가 돈다
    ev(ctx, "__tc[0].cb(null, true); globalThis.__lt = []; loadTerminal = (s) => { __lt.push(s.session_id); };");
    ctx.reattachForegroundTab();
    return { ...left, pulledBack: ev(ctx, "__lt.length") > 0, termMode: pd.classList.contains("term-mode") };
  });
  assert.deepEqual(got, all(paths, { mainCleared: true, termClosed: true, badgeClosed: true, pulledBack: false, termMode: false }));
});

test("T3 터미널 → 뷰어·웹 재개·빈 안내: '지금 터미널로 보는 세션' 표시를 지워 그 세션 알림이 무시되지 않는다", async () => {
  const paths = ["loadViewer", "loadResume", "detailPlaceholder"];
  const got = await each(paths, async (via) => {
    const { ctx } = await boot();
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    await go(ctx, via);
    const cleared = ev(ctx, "CURRENT_TERM_SID") === null;
    ev(ctx, "globalThis.__sw = []; switchTab = (k) => { __sw.push(k); };");
    await ctx.openSessionById(A);                                // 그 세션(A) 알림을 누름
    return { cleared, followed: ev(ctx, "__sw.join(',')") };
  });
  assert.deepEqual(got, all(paths, { cleared: true, followed: A }));
});

test("T4 웹 재개 → 상세는 재개 연결(claude -p)을 한 번 닫고, 뷰어 → 웹 재개는 뷰어 실시간 연결을 닫는다", async () => {
  const r = await boot();                                        // 응답 중이 아닌 재개 → 바로 닫는다(결정 7)
  await ev(r.ctx, "loadResume(SESSIONS[1], true, false)");
  await ev(r.ctx, "loadDetail(SESSIONS[0])"); await tick();
  const v = await boot();
  await ev(v.ctx, "loadViewer(SESSIONS[0])");
  await ev(v.ctx, "loadResume(SESSIONS[1], true, false)"); await tick();
  assert.deepEqual({
    resumeOpened: ev(r.ctx, "__res.length"), resumeClosed: ev(r.ctx, "__res[0].closed"),
    viewerOpened: ev(v.ctx, "__mon.length"), viewerClosed: ev(v.ctx, "__mon[0].closed"), viewerGone: ev(v.ctx, "_liveViewer === null"),
  }, { resumeOpened: 1, resumeClosed: 1, viewerOpened: 1, viewerClosed: 1, viewerGone: true });
});

test("T5 뷰어 응답을 기다리는 사이 다른 화면으로 가면, 늦게 온 응답이 실시간 연결을 열지 않는다", async () => {
  const paths = ["loadDetail", "loadTerminal", "detailPlaceholder"];
  const got = await each(paths, async (via) => {
    const { ctx } = await boot();
    ev(ctx, "globalThis.__late = null; fetchRecentMessages = () => new Promise((r) => { globalThis.__late = r; });");
    const pv = ev(ctx, "loadViewer(SESSIONS[0])");
    await go(ctx, via);
    ev(ctx, "__late([])"); await pv; await tick();
    return { viewerGone: ev(ctx, "_liveViewer === null"), viewerConns: ev(ctx, `__mon.filter((h) => h.mgr === '${A}').length`) };
  });
  assert.deepEqual(got, all(paths, { viewerGone: true, viewerConns: 0 }));
});

// ---------------------------------------------------------------- 1-1 가짜 인계 확인창(2단계보다 먼저)
test("T6 터미널을 정리하고 20초 안에 돌아와도(목록의 screen = 내 옛 화면) 확인창이 안 뜨고, 남의 화면이면 뜬다", async () => {
  const paths = ["fbShowEditor", "loadViewer"];   // 지금 정리하는 길 · 2단계부터 정리하게 될 길
  const got = await each(paths, async (via) => {
    const { ctx } = await boot();
    let asked = 0; ctx.confirm = () => { asked++; return true; };
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    const mine = ev(ctx, "__tc[0].opts.screen");
    ev(ctx, `SESSIONS[0].screen = { attached: true, screen_id: ${JSON.stringify(mine)} }`);   // 20초 목록이 마지막으로 본 값
    await go(ctx, via);
    ctx.switchTab(ev(ctx, "TABS.active")); await tick(RENDER);
    const onMine = asked;
    await go(ctx, via);
    ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' }");
    ctx.switchTab(ev(ctx, "TABS.active")); await tick(RENDER);
    return { onMine, onOther: asked - onMine };
  });
  assert.deepEqual(got, all(paths, { onMine: 0, onOther: 1 }));
});

// ---------------------------------------------------------------- 1-5 관제 가드
test("T7 같은 팀 관제가 끊긴 채 떠 있으면 '관제 열기'가 다시 연결한다(살아 있으면 그대로)", async () => {
  const { ctx, pd } = await boot();
  const G = "{ id: 'g1', name: '팀1', manager: 'm1', subs: ['w1'] }";
  ev(ctx, `openMonitorGroup(${G})`);
  const opened = ev(ctx, "__mon.length");
  ev(ctx, `openMonitorGroup(${G})`);
  const aliveAgain = ev(ctx, "__mon.length");
  ev(ctx, "__mon[0].cb(null, true)");                            // 관제 스트림 끊김(eof) — 칸은 '연결 종료'로 남아 있다
  ev(ctx, `openMonitorGroup(${G})`);
  assert.deepEqual({ opened, aliveAgain, deadAgain: ev(ctx, "__mon.length"),
                     gid: ev(ctx, "MON_INLINE && MON_INLINE.gid"), monMode: pd.classList.contains("mon-mode") },
                   { opened: 1, aliveAgain: 1, deadAgain: 2, gid: "g1", monMode: true });
});

// ---------------------------------------------------------------- 1-2 릴레이 닫기 신호
test("T8 릴레이: 관제·웹 재개 연결을 닫으면 PC 에도 stream_close 를 보낸다(터미널처럼)", async () => {
  const ctx = await load();
  const sockets = fakeWs(ctx);
  const p = ev(ctx, "conn.connect('room1', 'tok')"); await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c1" }); await p; await tick();
  ev(ctx, `globalThis.__m = conn.startMonitor('a,b', 'a', () => {}, 'g1');
           globalThis.__r = conn.startResume('${A}', () => {}, true, { grace: 'g' });`);
  const [fm, fr] = ws.sent.slice(-2);
  ev(ctx, "__m.close()"); const onMon = ws.sent.at(-1);
  ev(ctx, "__r.close()"); const onRes = ws.sent.at(-1);
  const streams = ev(ctx, "conn.streams.size");
  ev(ctx, "conn._stopped = true"); ws.close();
  assert.deepEqual({ reqs: [fm.method, fr.method], onMon, onRes, streams },
                   { reqs: ["monitor", "resume"], onMon: { v: 1, type: "stream_close", id: fm.id },
                     onRes: { v: 1, type: "stream_close", id: fr.id }, streams: 0 });
});

// ---------------------------------------------------------------- 파일 편집기(2단계)
const RK = "f:\\p\\proj";
const fid = (rel) => RK + "|" + rel;
const FB_SAVED = JSON.stringify({ open: [fid("")], sort: "name",
  tabs: [{ root: RK, path: "a.py", pinned: true }, { root: RK, path: "b.md", pinned: false }], active: fid("b.md") });
const S1 = "{ session_id: 's1', title: 'A', cwd: 'F:\\\\p\\\\proj', mtime: 10 }";

// 저장된 파일 탭(a.py 고정 · b.md 마지막)과 파일 API·Monaco 스텁(스크롤 위치 = saveViewState {top:42})
async function bootFiles({ withSessions = true } = {}) {
  const b = await boot({ setup: (sb) => sb.localStorage.setItem("sm_fb_state2", FB_SAVED) });
  ev(b.ctx, `globalThis.__calls = []; globalThis.__restored = [];
    SESSIONS = ${withSessions ? `[${S1}]` : "[]"};
    T.api = async (v, p, o) => { __calls.push([v, p, (o && o.query) || null]);
      if (p.endsWith('/files')) return { root: 'F:\\\\p\\\\proj', path: '', entries: [], truncated: false };
      if (p.endsWith('/file')) return { path: o.query.path, name: o.query.path, size: 5, mtime: 1, kind: 'text', text: 'x' };
      return {}; };
    loadMonaco = async () => ({ editor: { createModel: (t, l) => ({ t, l, dispose() {} }),
      create: (h, o) => ({ model: o.model, setModel(m) { this.model = m; }, dispose() {},
        saveViewState() { return { top: 42 }; }, restoreViewState(v) { __restored.push(v); },
        updateOptions() {}, focus() {}, getAction() { return null; } }) } });`);
  return b;
}

test("T9 파일: 목록을 받기 전에 연 편집기는, 목록이 오면 다시 받아 정상으로 보인다(오류가 굳지 않음)", async () => {
  const { ctx, pd } = await bootFiles({ withSessions: false });
  ev(ctx, "fbRestoreState(); fbShowEditor()"); await tick(); await tick();
  assert.equal(ev(ctx, "FB.tabs.find((t) => t.id === FB.active).err.error"), "no_session", "출발: 목록 전이라 세션을 못 찾음");
  ev(ctx, `T.list = async () => ({ sessions: [${S1}] })`);
  await ev(ctx, "loadList()"); await tick(RENDER);              // 첫 목록이 온다
  assert.deepEqual({ err: ev(ctx, "(FB.tabs.find((t) => t.id === FB.active).err || {}).error || null"),
                     fetched: ev(ctx, "__calls.filter((c) => c[1].endsWith('/file')).map((c) => c[2].path).join(',')"),
                     fbMode: pd.classList.contains("fb-mode") },
                   { err: null, fetched: "b.md", fbMode: true });
});

test("T9 파일: 편집기에서 다른 화면에 갔다 돌아오면 스크롤 위치(viewState)가 그대로다", async () => {
  const paths = ["detailPlaceholder", "loadViewer", "loadTerminal"];
  const got = await each(paths, async (via) => {
    const { ctx } = await bootFiles();
    ev(ctx, `SESSIONS.push({ session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }); fbRestoreState(); FB.tree = null;`);
    ev(ctx, `fbOpen(${JSON.stringify(fid("a.py"))}, { pin: true })`); await tick(); await tick(); await tick();
    if (ev(ctx, "__restored.length") !== 0) throw new Error("처음 열 때 복원할 위치가 없어야 함");
    await go(ctx, via);
    ev(ctx, "fbShowEditor()"); await tick(); await tick(); await tick();   // 바로가기가 쓸 다시 들어오기
    return { restored: ev(ctx, "JSON.stringify(__restored.at(-1) ?? null)") };
  });
  assert.deepEqual(got, all(paths, { restored: JSON.stringify({ top: 42 }) }));
});

// ---------------------------------------------------------------- 1-4 탭 복원 어긋남
test("T10 콜드 스타트: 알림(#open=A)으로 시작했는데 저장된 마지막 탭이 B 여도 화면·기억(TABS.active) 모두 A", async () => {
  const paths = ["local", "relay"];   // 결함은 로컬(알림 진입이 동기로 탭을 먼저 잡음), 릴레이는 지금도 맞음 — 둘 다 지킨다
  const got = await each(paths, async (mode) => {
    const { ctx } = await boot({ local: mode === "local", hash: "#open=" + A.slice(0, 8) });
    ctx.localStorage.setItem("sm_tabs", JSON.stringify({ active: B, list: [{ key: A, sid: A }, { key: B, sid: B }] }));
    ev(ctx, "T.list = async () => ({ sessions: SESSIONS.slice() })");
    await ev(ctx, "loadList()"); await tick(RENDER);             // 첫 목록 → 알림 진입 → 탭 목록 복원(실제 순서 그대로)
    return { shown: ev(ctx, "termView.active") === A, active: ev(ctx, "TABS.active") === A };   // saveTabs 는 TABS.active 를 저장
  });
  assert.deepEqual(got, all(paths, { shown: true, active: true }));
});

// ---------------------------------------------------------------- 2단계 관문 뒤 정리
test("T11 터미널 관문(2차 인증 취소·인계 확인 거절)에서 멈추면 보던 뷰어의 실시간 연결이 그대로다", async () => {
  const paths = ["2차 인증 취소", "인계 확인 거절"];
  const got = await each(paths, async (gate) => {
    const { ctx, pd } = await boot();
    await ev(ctx, "loadViewer(SESSIONS[1])");
    if (ev(ctx, "__mon.length") !== 1) throw new Error("출발이 뷰어 실시간 연결이 아님");
    if (gate === "2차 인증 취소") ctx.ensurePriv = async () => null;
    else { ev(ctx, "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' }"); ctx.confirm = () => false; }
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick();
    return { viewerLive: ev(ctx, "_liveViewer !== null"), viewerClosed: ev(ctx, "__mon[0].closed"),
             termConns: ev(ctx, "__tc.length"), termMode: pd.classList.contains("term-mode") };
  });
  assert.deepEqual(got, all(paths, { viewerLive: true, viewerClosed: 0, termConns: 0, termMode: false }));
});

// ---------------------------------------------------------------- 2단계 보강: 화면 진입 기록·화면 기억
const paneNow = (ctx) => JSON.parse(ev(ctx, "JSON.stringify(PANE_NOW)"));

test("T12 PANE_NOW: 상세 칸 화면을 열 때마다 종류·대상·세대(gen)를 적는다 — 폰 관제 덮개는 상세 칸 밖이라 그대로", async () => {
  const { ctx } = await boot();
  const steps = [
    ["loadViewer(SESSIONS[1])", { kind: "viewer", sid: B }],
    ["loadDetail(SESSIONS[0])", { kind: "detail", sid: A }],
    ["loadResume(SESSIONS[1], true, false)", { kind: "resume", sid: B }],
    ["loadTerminal(SESSIONS[0], false)", { kind: "terminal", sid: A, key: A }],
    ["fbShowEditor()", { kind: "files" }],
    ["openLedger({ code: 'T1', name: '팀' })", { kind: "ledger", code: "T1" }],
    ["openMonitorGroup({ id: 'g1', name: '팀1', manager: 'm1', subs: [] })", { kind: "monitor", gid: "g1" }],
    ["detailPlaceholder()", { kind: "empty" }],
  ];
  const got = [], gens = [];
  for (const [code] of steps) {
    await ev(ctx, code); await tick(code.startsWith("loadTerminal") ? RENDER : 0);
    const { gen, ...rest } = paneNow(ctx); gens.push(gen);
    got.push(Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== null)));
  }
  assert.deepEqual(got, steps.map(([, want]) => want));
  assert.ok(gens.every((g, i) => i === 0 || g > gens[i - 1]), "세대는 들어올 때마다 커진다: " + gens);
  ev(ctx, "innerWidth = 390");
  const before = paneNow(ctx);
  ev(ctx, "openMonitorGroup({ id: 'g2', name: '팀2', manager: 'm2', subs: [] })");   // 폰: 화면 전체 덮개
  assert.deepEqual(paneNow(ctx), before);
});

test("T13 화면 기억(sm_pane_<PC>): 뷰어·상세·웹 재개는 뷰어 칸에, 관제는 상세 칸·폰 덮개·관제 알림 모두 팀 코드와 함께 — sm_mon_last 는 그대로", async () => {
  const { ctx } = await boot({ setup: (sb) => sb.localStorage.setItem("sm_room", "room1") });
  const raw = () => ctx.localStorage.getItem("sm_pane_room1");
  const mem = (part) => { const v = JSON.parse(raw() || "{}")[part]; return v && { ...v, at: typeof v.at }; };   // 시각은 숫자인지만
  ev(ctx, `TEAMS = [{ code: 'T1', name: '팀1', monitor_group_id: 'g1', members: [] }, { code: 'T2', name: '팀2', monitor_group_id: 'g2', members: [] }];
    fetchMonGroups = async () => [{ id: 'g2', name: '팀2', manager: 'm2', subs: [] }, { id: 'g9', name: '새 팀', manager: 'm9', subs: [] }];`);
  const got = {};
  await ev(ctx, "loadViewer(SESSIONS[1])"); got.viewer = mem("viewer");
  await ev(ctx, "loadDetail(SESSIONS[0])"); got.detail = mem("viewer");
  await ev(ctx, "loadResume(SESSIONS[1], true, false)"); got.resume = mem("viewer");   // 웹 재개는 그 세션 대화로
  const keep = raw();
  await ev(ctx, "loadTerminal(SESSIONS[0], false)"); await tick(RENDER);
  ev(ctx, "fbShowEditor(); openLedger({ code: 'T1', name: '팀1' }); detailPlaceholder()");
  got.untouched = raw() === keep;                                // 터미널·파일·장부·빈 안내는 이 기억을 바꾸지 않는다
  ev(ctx, "openMonitorGroup({ id: 'g1', name: '팀1', manager: 'm1', subs: ['w1'] })");   // PC 상세 칸
  got.inline = mem("monitor"); got.lastInline = ctx.localStorage.getItem("sm_mon_last");
  ev(ctx, "innerWidth = 390; openMonitorGroup({ id: 'g2', name: '팀2', manager: 'm2', subs: [] })");   // 폰 덮개
  got.overlay = mem("monitor"); got.lastOverlay = ctx.localStorage.getItem("sm_mon_last");
  ev(ctx, "TEAMS = null");                                       // 팀 목록을 아직 못 받음(관제 알림으로 콜드 스타트 등)
  await ctx.openMonitorGroupById("g2"); got.notifySame = mem("monitor");   // 같은 관제 → 옛 팀 코드를 이어 쓴다
  await ctx.openMonitorGroupById("g9"); got.notifyNew = mem("monitor");    // 처음 보는 관제 → 팀 코드는 모른다
  got.viewerKept = mem("viewer").sid;                            // 관제 기억이 뷰어 기억을 지우지 않는다
  assert.deepEqual(got, {
    viewer: { sid: B, kind: "viewer", name: "B", at: "number" },
    detail: { sid: A, kind: "detail", name: "A", at: "number" },
    resume: { sid: B, kind: "viewer", name: "B", at: "number" },
    untouched: true,
    inline: { team: "T1", gid: "g1", name: "팀1", at: "number" }, lastInline: "g1",
    overlay: { team: "T2", gid: "g2", name: "팀2", at: "number" }, lastOverlay: "g1",
    notifySame: { team: "T2", gid: "g2", name: "팀2", at: "number" },
    notifyNew: { team: null, gid: "g9", name: "새 팀", at: "number" },
    viewerKept: B,
  });
});

test("T14 기억 키는 PC 별(로컬 화면 = local, 릴레이 = 그 PC 방 이름) — 깨진 값·저장 막힘에도 화면은 그대로 뜬다", async () => {
  const L = await boot({ local: true });
  await ev(L.ctx, "loadViewer(SESSIONS[0])");
  const R = await boot({ setup: (sb) => { sb.localStorage.setItem("sm_room", "roomA"); sb.localStorage.setItem("sm_pane_roomA", "{깨진 값"); } });
  await ev(R.ctx, "loadViewer(SESSIONS[0])");                    // 깨진 값은 버리고 새로 적는다
  R.ctx.localStorage.setItem("sm_room", "roomB");                // 릴레이로 다른 PC 를 쓰는 중
  R.ctx.localStorage.setItem("sm_pane_roomB", "[1,2]");          // 객체가 아닌 값
  await ev(R.ctx, "loadDetail(SESSIONS[1])"); await tick();
  const a = JSON.parse(R.ctx.localStorage.getItem("sm_pane_roomA")), b = JSON.parse(R.ctx.localStorage.getItem("sm_pane_roomB"));
  R.ctx.localStorage.setItem = () => { throw new Error("QuotaExceededError"); };   // 저장이 막힘(용량 초과·사생활 보호 창 등)
  let threw = null, viewerLive = null;
  try { await ev(R.ctx, "loadViewer(SESSIONS[1])"); viewerLive = ev(R.ctx, "_liveViewer !== null");
        ev(R.ctx, "openMonitorGroup({ id: 'g1', name: '팀1', manager: 'm1', subs: [] })"); }
  catch (e) { threw = String((e && e.message) || e); }
  assert.deepEqual({
    local: (JSON.parse(L.ctx.localStorage.getItem("sm_pane_local") || "{}").viewer || {}).sid || null,
    localNoRelayKey: L.ctx.localStorage.getItem("sm_pane_relay"),
    roomA: [a.viewer.sid, a.viewer.kind], roomB: [b.viewer.sid, b.viewer.kind],
    threw, viewerLive, monMode: R.pd.classList.contains("mon-mode"),
  }, { local: A, localNoRelayKey: null, roomA: [A, "viewer"], roomB: [B, "detail"], threw: null, viewerLive: true, monMode: true });
});

// ---------------------------------------------------------------- 2단계 보강: 웹 재개를 떠나면 끝낸다(결정 7)
const composer = (pd) => pd.children.find((c) => String(c.className) === "composer").children;   // [입력칸, 보내기]

test("T15 결정 7: 응답 중에 웹 재개를 떠나면 끊지 않고 그 응답이 끝난 뒤 닫고 알린다(대기 메시지는 보내지 않음) — 폰 '‹ 목록'도 같고, 응답 중이 아니면 바로 닫는다", async () => {
  const paths = ["다른 화면(상세)", "폰 '‹ 목록'", "응답 중 아님"];
  const got = await each(paths, async (p) => {
    const { ctx, pd } = await boot({ width: p === "다른 화면(상세)" ? 1400 : 390 });
    ev(ctx, `globalThis.__toasts = []; toast = (m) => __toasts.push(m);
      T.resume = (id, cb) => { const h = { id, cb, closed: 0, sent: [], input(t) { h.sent.push(t); }, close() { h.closed++; } }; __res.push(h); return h; };`);
    await ev(ctx, "loadResume(SESSIONS[1], true, false)");
    const [ta, send] = composer(pd);
    if (p !== "응답 중 아님") {
      ta.value = "첫 질문"; send.onclick();                        // 응답 중(busy)
      ta.value = "다음 질문"; send.onclick();                      // 응답 중이라 대기 칩으로
    }
    if (p === "다른 화면(상세)") { await ev(ctx, "loadDetail(SESSIONS[0])"); await tick(); }
    else backBtn(pd).onclick();
    const r = ev(ctx, "__res[0]"), afterLeave = r.closed;
    pd.scrollTop = 7;                                            // 지금 칸(이제 다른 화면이거나 숨은 칸)의 스크롤
    if (p !== "응답 중 아님") {
      r.cb({ data: { type: "assistant", message: { content: [{ type: "text", text: "마무리 중" }] } } });
      r.cb({ data: { type: "result", total_cost_usd: 0.01 } });  // 그 응답이 끝남
    } else r.cb({ eof: true });                                   // 닫은 소켓의 끝 알림(로컬 WebSocket 은 닫으면 onclose → eof) — 알림·보내기 되살림 없음
    await ev(ctx, "detailPlaceholder()");                        // 또 떠나도 두 번 닫지 않는다
    return { afterLeave, closed: r.closed, sent: r.sent.join("|"), toasts: ev(ctx, "__toasts.join('|')"),
             scroll: pd.scrollTop, sendOff: send.disabled === true };
  });
  const done = "응답이 끝나 떠난 웹 재개를 닫았습니다 — 보내지 않은 메시지 1개";
  assert.deepEqual(got, {
    "다른 화면(상세)": { afterLeave: 0, closed: 1, sent: "첫 질문", toasts: done, scroll: 7, sendOff: true },
    "폰 '‹ 목록'": { afterLeave: 0, closed: 1, sent: "첫 질문", toasts: done, scroll: 7, sendOff: true },
    "응답 중 아님": { afterLeave: 1, closed: 1, sent: "", toasts: "", scroll: 7, sendOff: true },
  });
});

test("T16 결정 7: 응답 중에 떠난 재개가 끊겨도(eof) 정리·알림, 이력을 받는 사이 떠나면 claude -p 를 띄우지 않고, 2차 인증을 취소하면 보던 뷰어 그대로", async () => {
  const e = await boot({ width: 390 });                          // 폰: 응답 중 '‹ 목록' 뒤 릴레이가 끊김
  ev(e.ctx, "globalThis.__toasts = []; toast = (m) => __toasts.push(m);");
  await ev(e.ctx, "loadResume(SESSIONS[1], true, false)");
  const [ta, send] = composer(e.pd);
  ta.value = "질문"; send.onclick(); backBtn(e.pd).onclick();
  const er = ev(e.ctx, "__res[0]"), eofBefore = er.closed;
  er.cb({ eof: true });
  const a = await boot();
  ev(a.ctx, "globalThis.__hist = null; fetchRecentMessages = () => new Promise((r) => { globalThis.__hist = r; });");
  const pr = ev(a.ctx, "loadResume(SESSIONS[1], true, false)");
  for (let i = 0; i < 5 && !ev(a.ctx, "__hist"); i++) await tick();
  if (!ev(a.ctx, "__hist")) throw new Error("이력 요청까지 가지 못함");
  ev(a.ctx, "fetchRecentMessages = async () => []");
  await ev(a.ctx, "loadDetail(SESSIONS[0])");                    // 이력을 기다리는 사이 다른 화면으로
  ev(a.ctx, "__hist([])"); await pr; await tick();
  const b = await boot();
  await ev(b.ctx, "loadViewer(SESSIONS[0])");
  b.ctx.ensurePriv = async () => null;                           // 2차 인증 창에서 취소
  await ev(b.ctx, "loadResume(SESSIONS[1], true, false)");
  assert.deepEqual({
    eof: [eofBefore, er.closed, ev(e.ctx, "__toasts.join('|')")],
    spawned: ev(a.ctx, "__res.length"), shown: ev(a.ctx, "PANE_NOW.kind"),
    viewerLive: ev(b.ctx, "_liveViewer !== null"), viewerClosed: ev(b.ctx, "__mon[0].closed"), resumes: ev(b.ctx, "__res.length"), stay: ev(b.ctx, "PANE_NOW.kind"),
  }, { eof: [0, 1, "응답이 끝나 떠난 웹 재개를 닫았습니다"], spawned: 0, shown: "detail",
       viewerLive: true, viewerClosed: 0, resumes: 0, stay: "viewer" });
});

// ---------------------------------------------------------------- 2단계 보강: 늦은 응답·늦은 구축
test("T17 뷰어를 연달아 누르면 마지막 누름만 그린다 — 먼저 누른 뷰어의 늦은 응답은 실시간 연결을 열지 않는다", async () => {
  const { ctx } = await boot();
  ev(ctx, "globalThis.__late = {}; fetchRecentMessages = (sid) => new Promise((r) => { __late[sid] = r; });");
  const p1 = ev(ctx, "loadViewer(SESSIONS[0])"), p2 = ev(ctx, "loadViewer(SESSIONS[1])");
  ev(ctx, `__late['${B}']([])`); await p2;                         // 나중에 누른 B 가 먼저 도착
  ev(ctx, `__late['${A}']([])`); await p1; await tick();         // 먼저 누른 A 는 늦게 도착
  assert.deepEqual({ a: ev(ctx, `__mon.filter((h) => h.mgr === '${A}').length`), b: ev(ctx, `__mon.filter((h) => h.mgr === '${B}').length`),
                     live: ev(ctx, `_liveViewer === __mon.find((h) => h.mgr === '${B}')`), sid: ev(ctx, "PANE_NOW.sid") },
                   { a: 0, b: 1, live: true, sid: B });
});

test("T18 터미널을 짓는 사이(xterm 첫 로드) 다른 화면으로 가면 늦게 붙지 않는다 — 터미널 연결·크기 감시가 남지 않는다", async () => {
  const { ctx, pd } = await boot();
  ev(ctx, "globalThis.__xr = null; loadXterm = () => new Promise((r) => { globalThis.__xr = r; });");
  const pt = ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false);
  for (let i = 0; i < 5 && !ev(ctx, "__xr"); i++) await tick();
  if (!ev(ctx, "__xr")) throw new Error("xterm 로드까지 가지 못함");
  await ev(ctx, "loadViewer(SESSIONS[1])");                       // 그사이 대화 보기로
  ev(ctx, "__xr()"); await pt; await tick(RENDER);
  assert.deepEqual({ termConns: ev(ctx, "__tc.length"), main: ev(ctx, "termView.main"), onWin: ev(ctx, "termView.onWin"), ro: ev(ctx, "termView.ro"),
                     termMode: pd.classList.contains("term-mode"), viewerLive: ev(ctx, "_liveViewer !== null"), kind: ev(ctx, "PANE_NOW.kind") },
                   { termConns: 0, main: null, onWin: null, ro: null, termMode: false, viewerLive: true, kind: "viewer" });
});

test("T19 파일: 목록이 와도 그 폴더를 쓰는 세션이 없으면 오류 그대로(다시 받지 않음), 생기면 한 번만 다시 받는다", async () => {
  const { ctx } = await bootFiles({ withSessions: false });
  ev(ctx, "fbRestoreState(); fbShowEditor()"); await tick(); await tick();
  const now = () => ({ err: ev(ctx, "(FB.tabs.find((t) => t.id === FB.active).err || {}).error || null"),
                       fetched: ev(ctx, "__calls.filter((c) => c[1].endsWith('/file')).length") });
  ev(ctx, "T.list = async () => ({ sessions: [{ session_id: 's9', title: 'Z', cwd: 'F:\\\\other', mtime: 5 }] })");
  await ev(ctx, "loadList()"); await tick(RENDER);               // 다른 폴더 세션만 있는 목록
  const still = now();
  ev(ctx, `T.list = async () => ({ sessions: [${S1}] })`);
  await ev(ctx, "loadList()"); await tick(RENDER);
  await ev(ctx, "loadList()"); await tick(RENDER);               // 이미 받은 탭은 다시 받지 않는다
  assert.deepEqual({ still, after: now() }, { still: { err: "no_session", fetched: 0 }, after: { err: null, fetched: 1 } });
});

// ---------------------------------------------------------------- 3단계: PC 별 기억(결정 9)
// 기억 키 = <키>_<PC>(로컬 화면 local · 릴레이 그 PC 방 이름). 옛 키(PC 구분 없음)는 이 PC 목록으로 걸러 1회 옮긴다.

test("T20 최근 본 순서(mru): 터미널을 볼 때마다 맨 앞, 탭을 닫으면 빠지고 PC 키에 저장 — 새로고침 뒤 알림으로 먼저 본 탭이 저장 순서보다 앞", async () => {
  const { ctx } = await boot({ setup: (sb) => sb.localStorage.setItem("sm_room", "room1") });
  const mru = (c) => ev(c, "TABS.mru.join(',')");
  await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
  await ctx.loadTerminal(ev(ctx, "SESSIONS[1]"), false); await tick(RENDER);   // 탭 전환(빠른 길)
  const seen = [mru(ctx)];
  ctx.switchTab(A); await tick(RENDER); seen.push(mru(ctx));
  const saved = JSON.parse(ctx.localStorage.getItem("sm_tabs_room1")).mru.join(",");
  ev(ctx, `removeTab('${A}'); saveTabs()`); seen.push(mru(ctx));
  // 새로고침: 저장된 마지막 탭은 B(최근 순 B, A)인데 알림(#open=A)으로 시작 — 로컬(알림이 먼저 탭을 잡음)·릴레이 둘 다
  const cold = await each(["local", "relay"], async (mode) => {
    const key = mode === "local" ? "sm_tabs_local" : "sm_tabs_relay";
    const b = await boot({ local: mode === "local", hash: "#open=" + A.slice(0, 8),
      setup: (sb) => sb.localStorage.setItem(key, JSON.stringify({ active: B, list: [{ key: A, sid: A }, { key: B, sid: B }], mru: [B, A] })) });
    ev(b.ctx, "T.list = async () => ({ sessions: SESSIONS.slice() })");
    await ev(b.ctx, "loadList()"); await tick(RENDER);
    return { mru: mru(b.ctx), saved: JSON.parse(b.ctx.localStorage.getItem(key)).mru.join(",") };
  });
  assert.deepEqual({ seen, saved, cold }, { seen: [`${B},${A}`, `${A},${B}`, B], saved: `${A},${B}`,
                                           cold: all(["local", "relay"], { mru: `${A},${B}`, saved: `${A},${B}` }) });
});

test("T21 파일 기억은 PC 별(sm_fb_state2_<PC>): 마지막 파일이 21번째 이후여도 다시 열면 그 파일 — 옛 키는 이 PC 목록에 있는 폴더만 새 키로 1회 옮김(목록 전엔 옮기지 않음)", async () => {
  const { ctx } = await boot({ setup: (sb) => sb.localStorage.setItem("sm_room", "room1") });
  ev(ctx, `FB.restored = true; FB.open = new Set(); FB.tabs = [];
    for (let i = 0; i < 25; i++) FB.tabs.push(fbNewTab('e:/p', 'f' + i + '.txt', true));
    FB.active = FB.tabs[23].id; fbSaveState();`);
  const saved = JSON.parse(ctx.localStorage.getItem("sm_fb_state2_room1"));
  ev(ctx, "FB.restored = false; fbRestoreState()");              // 새로고침 뒤(복원 상한 20개)
  const reopened = { n: ev(ctx, "FB.tabs.length"), active: ev(ctx, "FB.active") };
  // 옛 키(PC 구분 없음): 이 PC 폴더(e:/p)와 다른 PC 폴더(g:/x)가 섞여 있다. 이 PC(room2)의 새 키는 아직 없다
  const OLD = JSON.stringify({ open: ["e:/p|", "e:/p|src", "g:/x|"], sort: "mtime",
    tabs: [{ root: "g:/x", path: "other.md", pinned: true }, { root: "e:/p", path: "mine.py", pinned: true }], active: "g:/x|other.md" });
  ctx.localStorage.setItem("sm_fb_state2", OLD); ctx.localStorage.setItem("sm_room", "room2");
  ev(ctx, "LIST_READY = false; FB.restored = false; fbRestoreState()");   // 이 PC 목록 전 — 거르지도 적지도 않는다
  const early = { tabs: ev(ctx, "FB.tabs.map((t) => t.id).join(',')"), wrote: ctx.localStorage.getItem("sm_fb_state2_room2") };
  ev(ctx, "SESSIONS = [{ session_id: 's1', title: 'P', cwd: 'E:/p', mtime: 1 }]; LIST_READY = true; FB.restored = false; fbRestoreState()");
  const moved = { tabs: ev(ctx, "FB.tabs.map((t) => t.id).join(',')"), active: ev(ctx, "FB.active"), open: ev(ctx, "[...FB.open].join(',')"),
                  wrote: JSON.parse(ctx.localStorage.getItem("sm_fb_state2_room2") || "null") };
  assert.deepEqual({ savedN: saved.tabs.length, savedActiveAt: saved.tabs.findIndex((t) => "e:/p|" + t.path === saved.active), reopened, early, moved,
                     oldKept: ctx.localStorage.getItem("sm_fb_state2") === OLD }, {
    savedN: 25, savedActiveAt: 19, reopened: { n: 20, active: "e:/p|f23.txt" },
    early: { tabs: "g:/x|other.md,e:/p|mine.py", wrote: null },
    moved: { tabs: "e:/p|mine.py", active: "e:/p|mine.py", open: "e:/p|,e:/p|src",
             wrote: { open: ["e:/p|", "e:/p|src"], sort: "mtime", tabs: [{ root: "e:/p", path: "mine.py", pinned: true }], active: "e:/p|mine.py" } },
    oldKept: true });
});

// ---------------------------------------------------------------- 3단계: 릴레이 PC 전환 정리(T22~)
// PC 를 바꾸면 이전 PC 의 상세 칸·터미널 탭·파일 상태·지금 화면을 비우고 새 PC 기억을 쓴다(명세 §6-2).
const X = "xxxxxxxx-1111-2222-3333-444444444444";
// 터미널 연결을 닫은 순간의 방(sm_room)까지 적는 스텁 — 닫기 신호가 이전 PC 로 갔는지(방을 바꾸기 전에 닫았는지) 본다
const TC_ROOM = "T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, room: null, close() { h.closed = true; h.room = LS.getItem('sm_room'); }, send() {} }; __tc.push(h); return h; };";
const TWO_PCS = "pcsUpsert({ room: 'roomA', cs: 'c', cp: 'p', dev: 'dA', name: 'A' }); pcsUpsert({ room: 'roomB', cs: 'c', cp: 'p', dev: 'dB', name: 'B' });";
const flatEl = (n) => [n, ...(n.children || []).flatMap(flatEl)];

test("T22 PC 전환(switchToPc): 방을 바꾸기 전에 이전 PC 화면을 정리하고(닫기 신호는 이전 PC 로) 터미널 탭·파일·지금 화면을 비운다 — 새 PC 첫 목록 뒤 그 PC 기억으로, 이전 PC 기억은 그대로", async () => {
  const B_TABS = JSON.stringify({ active: X, list: [{ key: X, sid: X }], mru: [X] });
  // 파일 기억은 옛 키(PC 구분 없음)에만 — A 폴더(e:/p)와 B 폴더(h:/b) 탭이 섞여 있다. 두 PC 모두 첫 목록 뒤 제 폴더만 옮겨 온다
  const OLD_FB = JSON.stringify({ open: [], sort: "name", tabs: [{ root: "e:/p", path: "old-a.txt", pinned: true }, { root: "h:/b", path: "b.md", pinned: true }], active: "h:/b|b.md" });
  const { ctx, pd } = await boot({ setup: (sb) => { sb.localStorage.setItem("sm_room", "roomA"); sb.localStorage.setItem("sm_list_view", "files");
    sb.localStorage.setItem("sm_tabs_roomB", B_TABS); sb.localStorage.setItem("sm_fb_state2", OLD_FB); } });
  ev(ctx, TWO_PCS + TC_ROOM + `globalThis.__toasts = []; toast = (m) => __toasts.push(m); maybeShowIosHint = () => {}; refreshPushBtn = () => {};
    conn.connect = async (room) => { globalThis.__connRoom = room; return {}; };
    T.list = async () => ({ sessions: SESSIONS.slice() });`);
  await ev(ctx, "loadList()"); await tick(RENDER);              // PC A 첫 목록(탭 기억 복원 1회 끝, 파일 탐색기가 옛 키에서 A 폴더만 옮김)
  const fbA = JSON.parse(ctx.localStorage.getItem("sm_fb_state2_roomA") || "null");
  await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
  ev(ctx, `FB.tabs = [fbNewTab('e:/p', 'a.py', true)]; globalThis.__model = FB.tabs[0].model = { disposed: false, dispose() { this.disposed = true; } };
    FB.active = FB.tabs[0].id; FB.open = new Set(['e:/p|']); FB.kids = { 'e:/p|': { entries: [] } }; fbSaveState();`);
  const memA = { tabs: ctx.localStorage.getItem("sm_tabs_roomA"), fb: ctx.localStorage.getItem("sm_fb_state2_roomA") };
  if (!memA.tabs || !memA.fb || ev(ctx, "TABS.list.length") !== 1) throw new Error("출발이 PC A 터미널 탭 1개 + 파일 기억이 아님");
  ev(ctx, `T.list = async () => ({ sessions: [{ session_id: '${X}', title: 'X', live_terminal: true, cwd: 'H:/b', mtime: 1 }] })`);
  await ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })");
  await tick(RENDER);                                              // 새 PC 첫 목록 → 탭 기억·파일 탐색기 복원
  // 이전 PC 연결이 끊기며 온 끝 알림 뒤 재연결 처리 — 이전 PC 의 터미널로 끌려가지 않는다
  ev(ctx, "__tc[0].cb(null, true); globalThis.__lt = []; loadTerminal = (s) => { __lt.push(s.session_id); };"); ctx.reattachForegroundTab();
  assert.deepEqual({
    closedOn: ev(ctx, "__tc[0].room"), connected: ev(ctx, "__connRoom"), termGone: ev(ctx, "termView.main === null"),
    pane: { termMode: pd.classList.contains("term-mode"), kids: pd.children.map((c) => String(c.className)).join(","), now: ev(ctx, "PANE_NOW.kind") },
    modelDisposed: ev(ctx, "__model.disposed"),
    tabsB: { list: ev(ctx, "TABS.list.map((t) => t.key).join(',')"), active: ev(ctx, "TABS.active"), mru: ev(ctx, "TABS.mru.join(',')") },
    fbA: fbA && fbA.tabs.map((t) => t.root + "|" + t.path).join(","),
    fbB: { tabs: ev(ctx, "FB.tabs.map((t) => t.id).join(',')"), kids: ev(ctx, "Object.keys(FB.kids).length"),
           saved: (JSON.parse(ctx.localStorage.getItem("sm_fb_state2_roomB") || "null") || { tabs: [] }).tabs.map((t) => t.root + "|" + t.path).join(",") },
    memAKept: ctx.localStorage.getItem("sm_tabs_roomA") === memA.tabs && ctx.localStorage.getItem("sm_fb_state2_roomA") === memA.fb,
    oldKept: ctx.localStorage.getItem("sm_fb_state2") === OLD_FB,
    pulledBack: ev(ctx, "__lt.length"),
  }, {
    closedOn: "roomA", connected: "roomB", termGone: true,
    pane: { termMode: false, kids: "detail-empty", now: "empty" },
    modelDisposed: true,
    tabsB: { list: X, active: X, mru: X },
    fbA: "e:/p|old-a.txt",
    fbB: { tabs: "h:/b|b.md", kids: 0, saved: "h:/b|b.md" },
    memAKept: true, oldKept: true, pulledBack: 0,
  });
});

test("T23 PC 를 바꾸는 사이 늦게 온 이전 PC 목록은 버린다 — 새 PC 의 탭 기억을 빈 목록으로 덮어쓰지 않는다", async () => {
  const B_TABS = JSON.stringify({ active: X, list: [{ key: X, sid: X }], mru: [X] });
  const { ctx } = await boot({ setup: (sb) => { sb.localStorage.setItem("sm_room", "roomA"); sb.localStorage.setItem("sm_tabs_roomB", B_TABS); } });
  ev(ctx, TWO_PCS + "toast = () => {}; T.list = async () => ({ sessions: SESSIONS.slice() });");
  await ev(ctx, "loadList()"); await tick();                     // PC A 첫 목록
  ev(ctx, "globalThis.__lr = []; T.list = () => new Promise((r) => __lr.push(r));");
  const pOld = ev(ctx, "loadList()"); await tick();             // PC A 목록을 다시 청함(⟳ 등) — 응답 대기
  ev(ctx, "pcSwitchReset(); pcsActivate('roomB')");              // 그사이 PC B 로(switchToPc 의 앞부분)
  const ready = ev(ctx, "LIST_READY");
  ev(ctx, "__lr[0]({ sessions: SESSIONS.slice() })"); await pOld; await tick();   // A 의 목록이 늦게 도착
  const stale = { kept: ctx.localStorage.getItem("sm_tabs_roomB") === B_TABS, restored: ev(ctx, "_tabsRestored"), ready: ev(ctx, "LIST_READY") };
  const pNew = ev(ctx, "loadList()"); await tick();             // 새 PC 의 첫 목록
  ev(ctx, `__lr[1]({ sessions: [{ session_id: '${X}', title: 'X', live_terminal: true }] })`); await pNew; await tick();
  assert.deepEqual({ ready, stale, tabs: ev(ctx, "TABS.list.map((t) => t.key).join(',')"), active: ev(ctx, "TABS.active") },
                   { ready: false, stale: { kept: true, restored: false, ready: false }, tabs: X, active: X });
});

test("T24 페어링 화면에서 다른 PC 로 연결해도(저장된 PC 누름 · '＋ 새 PC 추가' 페어링) 연결을 바꾸기 전에 같은 정리 — 같은 PC 면 그대로", async () => {
  const paths = ["저장된 다른 PC", "저장된 같은 PC", "새 PC 페어링", "같은 PC 새 QR"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ setup: (sb) => sb.localStorage.setItem("sm_room", "roomA") });
    ev(ctx, TWO_PCS + TC_ROOM + `toast = () => {}; enterApp = () => {}; globalThis.__at = null;
      conn.connect = async (room) => { __at = { room, sm: LS.getItem('sm_room'), tabs: TABS.list.length, term: !!termView.main }; return { auth: null }; };`);
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    if (p.startsWith("저장된")) {
      const host = stubEl("div"); ctx.document._register("p-pcs", host);
      ctx.renderPairPcs();
      const name = p === "저장된 다른 PC" ? "🖥 B" : "🖥 A";
      const btn = flatEl(host).find((n) => n.tagName === "BUTTON" && n.textContent === name);
      await btn.onclick();
    } else {
      const li = stubEl("input"); li.value = "https://relay.test/relay/app#room=" + (p === "새 PC 페어링" ? "roomN" : "roomA") + "&token=tok";
      ctx.document._register("p-link", li); ctx.document._register("p-err", stubEl("div")); ctx.document._register("p-go", stubEl("button"));
      ev(ctx, "doPair()"); await tick(); await tick();
    }
    return { ...ev(ctx, "__at"), closedOn: ev(ctx, "__tc[0].room") };
  });
  assert.deepEqual(JSON.parse(JSON.stringify(got)), {
    "저장된 다른 PC": { room: "roomB", sm: "roomB", tabs: 0, term: false, closedOn: "roomA" },
    "저장된 같은 PC": { room: "roomA", sm: "roomA", tabs: 1, term: true, closedOn: null },
    "새 PC 페어링": { room: "roomN", sm: "roomA", tabs: 0, term: false, closedOn: "roomA" },
    "같은 PC 새 QR": { room: "roomA", sm: "roomA", tabs: 1, term: true, closedOn: null },
  });
});

test("T25 PC 전환 때 응답 중인 웹 재개는 기다리지 않고 바로 닫고(알림 없음), 받는 중이던 이전 PC 폴더의 실패는 새 트리의 오류로 남지 않는다", async () => {
  const r = await boot({ width: 390 });
  ev(r.ctx, `globalThis.__toasts = []; toast = (m) => __toasts.push(m);
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, sent: [], input(t) { h.sent.push(t); }, close() { h.closed++; } }; __res.push(h); return h; };`);
  await ev(r.ctx, "loadResume(SESSIONS[1], true, false)");
  const [ta, send] = composer(r.pd);
  ta.value = "질문"; send.onclick();                             // 응답 중
  ev(r.ctx, "pcSwitchReset()");
  const closedAt = ev(r.ctx, "__res[0].closed");
  ev(r.ctx, "__res[0].cb({ eof: true })");                        // 이전 PC 연결이 끊기며 오는 끝 알림
  const f = await boot();
  ev(f.ctx, `globalThis.__rej = null; SESSIONS = [{ session_id: 's1', title: 'P', cwd: 'E:/p', mtime: 1 }];
    T.api = () => new Promise((res, rej) => { __rej = rej; });
    FB.restored = true; FB.open = new Set(['e:/p|']); globalThis.__w = el('div');`);
  const pt = ev(f.ctx, "renderFbExplorer(__w)"); await tick();   // 이전 PC 폴더를 받는 중
  if (!ev(f.ctx, "__rej")) throw new Error("폴더 요청까지 가지 못함");
  ev(f.ctx, "pcSwitchReset()");
  ev(f.ctx, "__rej(new Error('disconnected'))"); await pt; await tick();   // 이전 연결이 닫히며 그 요청이 실패
  assert.deepEqual({ closedAt, closed: ev(r.ctx, "__res[0].closed"), toasts: ev(r.ctx, "__toasts.join('|')"), errs: ev(f.ctx, "JSON.stringify(FB.errs)") },
                   { closedAt: 1, closed: 1, toasts: "", errs: "{}" });
});
