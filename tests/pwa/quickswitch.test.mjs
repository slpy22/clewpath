// 상세 칸 화면 바로가기(quick-switch) 0단계 안전망 — docs/designs/quick-switch.md §2-4·§8.
// 바로가기가 생기면 화면을 오가는 횟수가 늘어 기존 결함(§2-4)을 자주 밟는다. 고치기 전에 '고친 뒤의 동작'을 먼저
// 고정한다 — 그래서 1·2단계 전에는 실패하는 게 정상이다(T6=1-1, T8=1-2, T1=1-3, T10=1-4, T7=1-5, 나머지=2단계).
// 경로가 여럿인 검사는 경로마다 새로 부팅해 결과를 한 객체로 모아 비교한다 — 실패 한 번에 어긋난 경로가 다 보이게.
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
