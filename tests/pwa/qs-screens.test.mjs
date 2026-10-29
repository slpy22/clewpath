// 바로가기(quick-switch) 검토 지적 — 화면을 오가는 길(T26~T32·T35·T36). docs/designs/quick-switch.md §4-1·§5-2·§6-2·§7.
// 2단계부터 터미널을 떠나면 늘 정리(연결 닫음)하므로, '떠나 있는 사이'를 목록(20초)만으로 판단하던 곳이 틀리기 쉬워졌다 —
// 방금 띄운 터미널을 꺼진 것으로 보거나(탭을 버림), 그사이 다른 기기가 가져간 것을 몰랐다(확인창 없이 가져옴).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const RENDER = 350;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const backBtn = (pd) => pd.children.find((c) => String(c.className).startsWith("backbar")).children[0];   // '‹ 목록'
const composer = (pd) => pd.children.find((c) => String(c.className) === "composer").children;           // [입력칸, 보내기]

async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
const all = (paths, want) => Object.fromEntries(paths.map((p) => [p, want]));

// 상세 칸·#app·목록 칸을 고정하고(하네스 주의 §8) 터미널·관제·재개 연결은 스텁으로 기록한다(기본 릴레이·PC 폭)
async function boot({ width = 1400, local = false, hash = "", setup = null } = {}) {
  const ctx = await load(local ? { hash, setup, location: LOCAL } : { hash, setup });
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = [];
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb, gid) => { const h = { ids, mgr, gid, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.api = async () => ({});
    fetchRecentMessages = async () => [];
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  if (local) ev(ctx, "RELAY_APP_URL = ''");
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  return { ctx, pd, app };
}

// ---------------------------------------------------------------- 방금 띄운 터미널로 돌아오기(F1)
test("T26 방금 띄운 터미널(20초 목록은 아직 꺼짐)을 떠났다가 같은 세션 줄로 돌아오면 목록을 새로 받아 판정한다 — 살아 있으면 그 탭으로, 정말 꺼졌으면 탭을 닫고 상세(새 claude 없음), 목록을 못 받으면 떠나기 직전의 관찰대로", async () => {
  const paths = [];
  for (const mode of ["릴레이", "로컬"]) for (const via of ["loadViewer", "loadDetail"]) for (const pty of ["살아 있음", "꺼짐", "목록 못 받음"]) paths.push([mode, via, pty].join("|"));
  const got = await each(paths, async (p) => {
    const [mode, via, pty] = p.split("|");
    const { ctx, pd } = await boot({ local: mode === "로컬" });
    ev(ctx, `SESSIONS.forEach((s) => { s.live_terminal = false; });   // 20초 목록은 아직 이 PTY 를 모른다
      globalThis.__lists = 0; T.list = async () => { __lists++; ${pty === "목록 못 받음" ? "throw new Error('disconnected');" : ""}
        return { sessions: SESSIONS.map((s) => ({ ...s, live_terminal: ${pty === "살아 있음"} && s.session_id === '${A}' })) }; };`);
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);   // ▶ 재개 → 터미널(새로 띄움)
    await ev(ctx, `${via}(SESSIONS[1])`); await tick();                          // 20초 안에 다른 세션 화면으로
    const menuAway = ev(ctx, "rowMenuItems(SESSIONS[0])[0][1]");                 // 떠나 있는 사이 그 줄의 ⋯ 첫 항목
    await ctx.jumpToSession(ev(ctx, "SESSIONS[0]")); await tick(RENDER);        // 목록 줄(같은 세션)로 돌아옴
    return { menuAway, termMode: pd.classList.contains("term-mode"), tabs: ev(ctx, "TABS.list.length"), lists: ev(ctx, "__lists"),
             now: ev(ctx, "PANE_NOW.kind"), newTerm: mode === "릴레이" ? ev(ctx, "__tc.length") : "-" };
  });
  const want = (mode, pty) => pty !== "꺼짐"
    ? { menuAway: "터미널 열기", termMode: true, tabs: 1, lists: 1, now: "terminal", newTerm: mode === "릴레이" ? 2 : "-" }
    : { menuAway: "터미널 열기", termMode: false, tabs: 0, lists: 1, now: "detail", newTerm: mode === "릴레이" ? 1 : "-" };
  assert.deepEqual(got, Object.fromEntries(paths.map((p) => { const [mode, , pty] = p.split("|"); return [p, want(mode, pty)]; })));
});

test("T26b 떠난 뒤 받은 목록이 꺼졌다고 하면(그 뒤 PTY 가 끝남) ⋯ 메뉴는 '재개'를 보인다 — 떠나기 직전의 관찰보다 새 목록을 믿는다", async () => {
  const { ctx } = await boot();
  ev(ctx, "SESSIONS.forEach((s) => { s.live_terminal = false; }); T.list = async () => ({ sessions: SESSIONS.map((s) => ({ ...s })) });");
  await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
  await ev(ctx, "loadViewer(SESSIONS[1])"); await tick(15);           // 떠난 시각과 다음 목록을 청한 시각이 같은 ms 가 되지 않게
  const before = ev(ctx, "rowMenuItems(SESSIONS[0])[0][1]");
  await ev(ctx, "loadList()"); await tick();                               // 떠난 뒤 새 목록 — 여전히 꺼짐
  assert.deepEqual({ before, after: ev(ctx, "rowMenuItems(SESSIONS[0])[0][1]") }, { before: "터미널 열기", after: "재개" });
});

// ---------------------------------------------------------------- 떠난 사이 다른 기기가 가져감(F7·F21)
test("T27 터미널을 떠난 사이 다른 기기가 그 터미널을 가져갔는데 목록이 옛값(내가 놓은 화면)이면, 다시 열 때 목록을 새로 받아 확인창을 띄운다 — 아직 내 옛 화면·아무도 없음이면 묻지 않는다", async () => {
  const paths = ["다른 기기가 가져감", "아직 내 옛 화면", "아무도 안 붙음"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot();
    let asked = 0; ctx.confirm = () => { asked++; return true; };
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    const mine = ev(ctx, "__tc[0].opts.screen");
    ev(ctx, `SESSIONS[0].screen = { attached: true, screen_id: ${JSON.stringify(mine)} }`);   // 떠나기 전에 받은 20초 목록
    await ev(ctx, "loadViewer(SESSIONS[1])");                                                 // 떠남 — 연결을 닫아 인계 통지를 받을 길이 없다
    const fresh = p === "다른 기기가 가져감" ? { attached: true, screen_id: "scr-pc-local" } : p === "아직 내 옛 화면" ? { attached: true, screen_id: mine } : { attached: false };
    ev(ctx, `globalThis.__lists = 0; T.list = async () => { __lists++; return { sessions: SESSIONS.map((s, i) => (i === 0 ? { ...s, screen: ${JSON.stringify(fresh)} } : s)) }; };`);
    ctx.jumpToSession(ev(ctx, "SESSIONS[0]")); await tick(RENDER);                           // 목록을 새로 받기 전에 그 줄을 누름
    return { asked, lists: ev(ctx, "__lists"), attached: ev(ctx, "__tc.length") };
  });
  assert.deepEqual(got, {
    "다른 기기가 가져감": { asked: 1, lists: 1, attached: 2 },
    "아직 내 옛 화면": { asked: 0, lists: 1, attached: 2 },
    "아무도 안 붙음": { asked: 0, lists: 1, attached: 2 },
  });
});

// ---------------------------------------------------------------- 폰 '‹ 목록'으로 숨긴 터미널(F14·F19)
test("T28 폰: '‹ 목록'으로 숨긴 터미널이 끊긴 채면 같은 세션 알림이 같은 탭에 다시 붙이고 보인다(죽은 화면만 보이지 않음)", async () => {
  const { ctx, pd, app } = await boot({ width: 390 });
  await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
  backBtn(pd).onclick();                                                  // 숨기기만(살아 있음)
  ev(ctx, "__tc[0].cb(null, true)");                                       // 앱을 내린 사이 릴레이가 끊김 → disconnected
  let prompts = 0; ctx.ensurePriv = async () => { prompts++; return {}; };
  await ctx.openSessionById(A); await tick(RENDER);
  assert.deepEqual({ view: app.getAttribute("data-view"), conns: ev(ctx, "__tc.length"), prompts, live: ev(ctx, "!termView.disconnected") },
                   { view: "detail", conns: 2, prompts: 1, live: true });
});

test("T29 폰: '‹ 목록'으로 숨긴 터미널은 화면 복귀·릴레이 재연결 때 저절로 다시 뜨지 않는다(2차 인증도 묻지 않음) — 보고 있던 터미널은 지금처럼 다시 붙는다", async () => {
  const run = async (hide) => {
    const { ctx, pd, app } = await boot({ width: 390 });
    const timers = [], realT = ctx.setTimeout;
    ctx.setTimeout = (f, ms) => (ms >= 1000 ? (timers.push(f), 0) : realT(f, ms));   // 재연결 백오프 타이머만 붙잡는다
    ev(ctx, "toast = () => {}; conn.connect = async () => { conn.peer = true; return {}; }; T.list = async () => ({ sessions: SESSIONS.slice() });");
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    if (hide) backBtn(pd).onclick();
    let prompts = 0; ctx.ensurePriv = async () => { prompts++; return {}; };
    const out = {};
    ev(ctx, "__tc.at(-1).cb(null, true)");                                   // 앱을 내렸다 올림 — 소켓이 끊겨 eof
    ctx.reattachForegroundTab(); await tick(RENDER);                          // visibilitychange
    out.resume = { view: app.getAttribute("data-view"), conns: ev(ctx, "__tc.length"), prompts };
    ev(ctx, "__tc.at(-1).cb(null, true); conn._retry = 0; conn._retryTimer = null; conn._stopped = false; conn._scheduleReconnect(1006)");
    await timers.at(-1)(); await tick(RENDER);                                // 재연결 성공 → 목록 → 전경 탭 다시 붙이기
    out.reconnect = { view: app.getAttribute("data-view"), conns: ev(ctx, "__tc.length"), prompts };
    return out;
  };
  assert.deepEqual({ hidden: await run(true), shown: await run(false) }, {
    hidden: { resume: { view: "list", conns: 1, prompts: 0 }, reconnect: { view: "list", conns: 1, prompts: 0 } },
    shown: { resume: { view: "detail", conns: 2, prompts: 1 }, reconnect: { view: "detail", conns: 3, prompts: 2 } },
  });
});

// ---------------------------------------------------------------- 늦게 온 xterm 실패·화면 꺼짐 방지(F5·F17)
test("T30 xterm 첫 로드가 늦게 실패해도(그사이 다른 화면으로 감) 지금 칸을 지우거나 '로드 실패'를 그리지 않는다 — 떠나지 않았으면 지금처럼 알린다", async () => {
  const run = async (leave) => {
    const { ctx, pd } = await boot();
    ev(ctx, "globalThis.__xr = null; loadXterm = () => new Promise((res, rej) => { globalThis.__xr = rej; });");
    const pt = ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false);
    for (let i = 0; i < 5 && !ev(ctx, "__xr"); i++) await tick();
    if (!ev(ctx, "__xr")) throw new Error("xterm 로드까지 가지 못함");
    if (leave) await ev(ctx, "loadViewer(SESSIONS[1])");
    ev(ctx, "__xr(new Error('cdn 실패'))"); await pt; await tick();
    return { failNote: pd.children.some((c) => String(c.className) === "term-note"), kind: ev(ctx, "PANE_NOW.kind"),
             viewer: leave ? [ev(ctx, "_liveViewer !== null"), ev(ctx, "__mon.find((h) => h.mgr === '" + B + "').closed")] : "-" };
  };
  assert.deepEqual({ left: await run(true), stayed: await run(false) },
                   { left: { failNote: false, kind: "viewer", viewer: [true, 0] }, stayed: { failNote: true, kind: "terminal", viewer: "-" } });
});

test("T31 화면 꺼짐 방지(wakeLock) 응답이 오기 전에 터미널을 떠나면, 늦게 온 잠금을 바로 놓는다(목록·뷰어를 보는 동안 화면이 계속 켜져 있지 않게)", async () => {
  const wl = { requested: 0, released: 0, grant: null };
  const { ctx } = await boot({ width: 390, setup: (sb) => { sb.navigator.wakeLock = { request: () => { wl.requested++;
    return new Promise((r) => { wl.grant = () => r({ release() { wl.released++; return Promise.resolve(); } }); }); } }; } });
  const pt = ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false);
  for (let i = 0; i < 20 && !wl.grant; i++) await tick();
  if (!wl.grant) throw new Error("잠금 요청까지 가지 못함");
  await ev(ctx, "loadViewer(SESSIONS[1])");                                 // 응답 전에 떠남(해체 때는 아직 놓을 잠금이 없다)
  wl.grant(); await pt; await tick(RENDER);
  assert.deepEqual({ requested: wl.requested, released: wl.released, held: ev(ctx, "termView.wl !== null") }, { requested: 1, released: 1, held: false });
});

// ---------------------------------------------------------------- 알림 콜드 스타트의 탭별 권한확인(F20)
test("T32 알림으로 새로고침·앱 다시 열기를 시작해도 그 탭에 저장된 '권한확인'(safe)을 끄지 않는다 — 로컬·릴레이, 연결도 그 설정으로", async () => {
  const got = await each(["로컬", "릴레이"], async (mode) => {
    const key = mode === "로컬" ? "sm_tabs_local" : "sm_tabs_relay";
    const { ctx } = await boot({ local: mode === "로컬", hash: "#open=" + A.slice(0, 8),
      setup: (sb) => sb.localStorage.setItem(key, JSON.stringify({ active: B, list: [{ key: A, sid: A, safe: true }, { key: B, sid: B, safe: false }] })) });
    ev(ctx, "T.list = async () => ({ sessions: SESSIONS.slice() })");
    await ev(ctx, "loadList()"); await tick(RENDER);                       // 첫 목록 → 알림 진입 → 탭 기억 복원
    const saved = JSON.parse(ctx.localStorage.getItem(key));
    return { savedA: saved.list.find((t) => t.key === A).safe, memA: ev(ctx, `TABS.list.find((t) => t.key === '${A}').safe`), active: ev(ctx, "TABS.active") === A,
             conn: mode === "로컬" ? /skip=0/.test(ev(ctx, `termView.tabs['${A}'].frame.src`)) : ev(ctx, "!!__tc[0].opts.safe") };
  });
  assert.deepEqual(got, all(["로컬", "릴레이"], { savedA: true, memA: true, active: true, conn: true }));
});

// ---------------------------------------------------------------- 관제 덮개(F12)
// 부모를 기억해 remove() 가 실제로 빠지고, getElementById 가 문서 순서(상세 칸 → body)로 찾는 DOM 흉내
function trackDom(ctx, pd) {
  const wrap = (e) => {
    const add = (front) => (c) => { if (c && typeof c === "object") { if (c.__parent && c.__parent !== e) c.remove(); c.__parent = e; }
      if (front) e.children.unshift(c); else e.children.push(c); return c; };
    e.__parent = null; e._mon = undefined;   // 스텁은 없는 속성을 함수로 돌려준다 — 브라우저처럼 비어 있게
    e.appendChild = add(false); e.prepend = add(true); e.insertBefore = add(false);
    e.remove = () => { const p = e.__parent; if (p) { const i = p.children.indexOf(e); if (i >= 0) p.children.splice(i, 1); } e.__parent = null; };
    return e;
  };
  const orig = ctx.document.createElement;
  ctx.document.createElement = (t) => wrap(orig(t));
  wrap(pd); wrap(ctx.document.body);
  const dfs = (n, id) => { if (!n || typeof n !== "object") return null; if (n.id === id) return n;
    for (const c of (n.children || [])) { const r = dfs(c, id); if (r) return r; } return null; };
  const origGet = ctx.document.getElementById;
  ctx.document.getElementById = (id) => dfs(pd, id) || dfs(ctx.document.body, id) || origGet(id);
  return () => ctx.document.body.children.filter((c) => c.id === "mon-ov");
}

test("T35 큰 폰 가로·태블릿 회전(상세 칸 관제 → 폰 폭) 뒤 관제 알림이 와도 덮개가 쌓이지 않고 앞 덮개의 관제 연결은 닫힌다 — 같은 팀이 살아 있으면 그대로", async () => {
  const { ctx, pd } = await boot();
  const overlays = trackDom(ctx, pd);
  ev(ctx, "fetchMonGroups = async () => ['g1', 'g2', 'g3'].map((id, i) => ({ id, name: '팀' + (i + 1), manager: 'm' + (i + 1), subs: [] }));");
  ev(ctx, "openMonitorGroup({ id: 'g1', name: '팀1', manager: 'm1', subs: [] })");   // 가로: 상세 칸 관제
  ev(ctx, "innerWidth = 820");                                                       // 세로로 돌림
  await ctx.openMonitorGroupById("g2");
  await ctx.openMonitorGroupById("g2");                                              // 같은 팀이 살아 있음 → 그대로
  const same = { overlays: overlays().length, mons: ev(ctx, "__mon.length") };
  await ctx.openMonitorGroupById("g3");                                              // 다른 팀 → 앞 덮개를 닫고 바꾼다
  const ov = overlays();
  ov.length && ov[0].children[0].children.find((c) => String(c.className) === "x").onclick();   // 덮개 ✕
  await ctx.openMonitorGroupById("g1");                                              // 닫은 뒤 관제 알림 — 새 덮개 하나
  assert.deepEqual({ same, replaced: ov.length, mons: ev(ctx, "JSON.stringify(__mon.map((h) => [h.gid, h.closed]))"), now: overlays().length },
                   { same: { overlays: 1, mons: 2 }, replaced: 1, mons: JSON.stringify([["g1", 1], ["g2", 1], ["g3", 1], ["g1", 0]]), now: 1 });
});

// ---------------------------------------------------------------- 폰 웹 재개를 탭 막대로 떠나기(F18)
test("T36 폰 웹 재개: 아래 탭 막대로 나가도 '‹ 목록'처럼 재개를 끝낸다 — 응답 중이면 그 응답이 끝난 뒤", async () => {
  const paths = ["응답 중 아님", "응답 중"];
  const got = await each(paths, async (p) => {
    const { ctx, pd, app } = await boot({ width: 390 });
    ev(ctx, `globalThis.__toasts = []; toast = (m) => __toasts.push(m); T.list = async () => ({ sessions: SESSIONS.slice() });
      T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };`);
    await ev(ctx, "loadList()"); await tick();
    await ev(ctx, "loadResume(SESSIONS[1], true, false)");
    if (p === "응답 중") { const [ta, send] = composer(pd); ta.value = "질문"; send.onclick(); }
    await ctx.tabbarGo("sessions"); await tick();
    const r = ev(ctx, "__res[0]"), afterLeave = r.closed;
    if (p === "응답 중") r.cb({ data: { type: "result", total_cost_usd: 0 } });   // 그 응답이 끝남
    return { view: app.getAttribute("data-view"), afterLeave, closed: r.closed, handle: ev(ctx, "_liveResume === null") };
  });
  assert.deepEqual(got, { "응답 중 아님": { view: "list", afterLeave: 1, closed: 1, handle: true },
                          "응답 중": { view: "list", afterLeave: 0, closed: 1, handle: true } });
});

// ---------------------------------------------------------------- 검토 2회차: 목록을 새로 받는 사이의 누름(§4 공통 '연달아 누르면 마지막 누름만')
const nowAt = (ctx) => ev(ctx, "PANE_NOW.kind + ':' + (PANE_NOW.sid || '').slice(0, 1)");

test("T48 방금 본 터미널 줄(A)이 목록을 새로 받는 사이 다른 줄(B 상세)을 누르면 마지막 누름(B)이 이긴다 — 늦게 끝난 A 가 B 를 덮지 않는다(릴레이·로컬, 목록이 늦게 옴·안 옴)", async () => {
  const paths = ["릴레이|늦게 옴", "릴레이|안 옴", "로컬|늦게 옴"];
  const got = await each(paths, async (p) => {
    const [mode, net] = p.split("|");
    const { ctx, pd } = await boot({ local: mode === "로컬" });
    ev(ctx, "SESSIONS.forEach((s) => { s.live_terminal = false; }); globalThis.__lr = []; T.list = async () => ({ sessions: SESSIONS.map((s) => ({ ...s })) });");
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);   // ▶ 재개 → 터미널 A(새로 띄움 — 20초 목록은 아직 모른다)
    await ev(ctx, "loadViewer(SESSIONS[1])"); await tick(15);                     // 다른 화면으로 떠남
    ev(ctx, "T.list = () => new Promise((r) => __lr.push(r));");               // 느린 망
    const pA = ctx.jumpToSession(ev(ctx, "SESSIONS[0]")); await tick(50);        // 목록 줄 A — 목록을 새로 받는 중
    ctx.jumpToSession(ev(ctx, "SESSIONS[1]")); await tick(50);                   // 기다리다 줄 B(탭 없음 → 상세)
    const afterB = nowAt(ctx);
    if (net === "늦게 옴") ev(ctx, `__lr[0]({ sessions: SESSIONS.map((s) => ({ ...s, live_terminal: s.session_id === '${A}' })) })`);
    await pA; await tick(RENDER);
    return { afterB, final: nowAt(ctx), termMode: pd.classList.contains("term-mode"), tabA: ev(ctx, "TABS.list.length") };
  });
  assert.deepEqual(got, all(paths, { afterB: "detail:b", final: "detail:b", termMode: false, tabA: 1 }));
});

test("T49 느린 망(목록이 3초 안에 오지 않음): 방금 본 터미널 줄을 누르면 기다리는 동안 받아들였음을 알리고, 3초 뒤 그 탭으로 간다 — 목록을 두 번 기다리지 않는다", async () => {
  const { ctx } = await boot();
  ev(ctx, "SESSIONS.forEach((s) => { s.live_terminal = false; }); T.list = async () => ({ sessions: SESSIONS.map((s) => ({ ...s })) });"
        + "globalThis.__toasts = []; toast = (m) => __toasts.push(m);");
  await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
  const mine = ev(ctx, "__tc[0].opts.screen");
  ev(ctx, `SESSIONS[0].screen = { attached: true, screen_id: ${JSON.stringify(mine)} }`);   // 떠나기 전에 받은 20초 목록(내 화면이 붙어 있음)
  await ev(ctx, "loadViewer(SESSIONS[1])"); await tick(15);
  ev(ctx, "globalThis.__lists = 0; T.list = () => { __lists++; return new Promise(() => {}); };");   // 목록 응답이 오지 않는다
  const t0 = Date.now();
  const p = ctx.jumpToSession(ev(ctx, "SESSIONS[0]"));
  await tick(700);
  const waiting = { now: ev(ctx, "PANE_NOW.kind"), told: ev(ctx, "__toasts.some((m) => m.includes('확인하는 중'))") };
  await p; await tick(RENDER);
  // 실제 시계: 한 번 기다림(3초)이면 통과, 두 번(6초)이면 실패. 전엔 3.7초로 잡아 CPU 가 바쁜 전체 시험에서 타이머가 조금만 늦어도
  // 넘었다(2026-10-29 부하 재현 8번 중 1번 — 출시가 테스트로 멈추던 간헐 실패). 두 번 기다리지 않음은 lists:1 도 함께 본다
  assert.deepEqual({ waiting, now: ev(ctx, "PANE_NOW.kind"), lists: ev(ctx, "__lists"), conns: ev(ctx, "__tc.length"), inTime: Date.now() - t0 < 5500 },
                   { waiting: { now: "viewer", told: true }, now: "terminal", lists: 1, conns: 2, inTime: true });
});

test("T50 폰: 새로고침 뒤 '중지됨'으로 되살린 탭의 줄을 누르면 목록을 기다리지 않고 바로 상세로 간다(이 페이지가 살아 있음을 본 적 없는 탭은 목록대로) — 이어 누른 줄이 이긴다", async () => {
  const SAVED = JSON.stringify({ active: A, list: [{ key: A, sid: A, safe: false }], mru: [A] });
  const { ctx } = await boot({ width: 390, setup: (sb) => { sb.localStorage.setItem("sm_room", "room1"); sb.localStorage.setItem("sm_tabs_room1", SAVED); } });
  ev(ctx, "SESSIONS.forEach((s) => { s.live_terminal = false; }); T.list = async () => ({ sessions: SESSIONS.map((s) => ({ ...s })) });");
  await ev(ctx, "loadList()"); await tick(RENDER);                       // 첫 목록 → 탭 기억 복원(A: 중지됨 — 쉬는 세션 4시간 정리 등)
  const restored = ev(ctx, "TABS.list.map((t) => t.s.title + (t.dead ? '(중지됨)' : '')).join(',')");
  ev(ctx, "globalThis.__lists = 0; T.list = () => { __lists++; return new Promise(() => {}); };");   // 느린 망
  ctx.jumpToSession(ev(ctx, "SESSIONS[0]")); await tick(100);            // 목록 줄 A
  const atA = nowAt(ctx);
  ctx.jumpToSession(ev(ctx, "SESSIONS[1]")); await tick(100);            // 이어서 줄 B
  await tick(3100);                                                        // (고치기 전에는 3초 뒤 A 의 늦은 판정이 B 를 덮었다)
  assert.deepEqual({ restored, atA, final: nowAt(ctx), lists: ev(ctx, "__lists"), tabs: ev(ctx, "TABS.list.length"), conns: ev(ctx, "__tc.length") },
                   { restored: "A(중지됨)", atA: "detail:a", final: "detail:b", lists: 0, tabs: 0, conns: 0 });
});

test("T48b 터미널·웹 재개가 2차 인증 확인을, 알림 진입이 목록을 기다리는 사이 다른 줄(B 상세)을 누르면 그 줄이 이긴다 — 늦게 끝난 앞 누름은 화면을 바꾸지 않는다", async () => {
  const paths = ["터미널", "웹 재개", "알림"];
  const got = await each(paths, async (p) => {
    const { ctx, pd } = await boot();
    const releases = [];
    ctx.ensurePriv = () => new Promise((r) => releases.push(r));
    ev(ctx, "SESSIONS[1].live_terminal = false; globalThis.__lr = []; T.list = () => new Promise((r) => __lr.push(r));");
    const pA = p === "터미널" ? ctx.jumpToSession(ev(ctx, "SESSIONS[0]"))            // 목록상 살아 있음 → 터미널(2차 인증 확인 중)
             : p === "웹 재개" ? ev(ctx, "loadResume(SESSIONS[0], true, false)")
             : ctx.openSessionById(A);                                                // 알림 → 목록을 새로 받는 중
    await tick(20);
    ctx.jumpToSession(ev(ctx, "SESSIONS[1]")); await tick(20);                        // 기다리다 줄 B(탭 없음·꺼짐 → 상세)
    const afterB = nowAt(ctx);
    for (const r of ev(ctx, "__lr.slice()")) r({ sessions: ev(ctx, "SESSIONS").map((x) => ({ ...x })) });
    await tick(20);
    while (releases.length) releases.shift()({});                                      // 앞 누름의 2차 인증 확인이 늦게 끝남
    await pA; await tick(RENDER);
    return { afterB, final: nowAt(ctx), termMode: pd.classList.contains("term-mode"), terms: ev(ctx, "__tc.length"), resumes: ev(ctx, "__res.length") };
  });
  assert.deepEqual(got, all(paths, { afterB: "detail:b", final: "detail:b", termMode: false, terms: 0, resumes: 0 }));
});

test("T52 폰: 터미널을 보다 떠나 앱을 몇 시간 내려 둔 뒤(20초 목록이 돌지 않음) 그 줄을 누르면 — 목록이 늦어도 터미널(꺼졌으면 새 claude)을 열지 않고 상세로, 탭은 버리지 않는다. 목록이 오면 그대로 판정", async () => {
  const paths = ["목록 늦음", "목록 옴(꺼짐)"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ width: 390 });
    ev(ctx, "T.list = async () => ({ sessions: SESSIONS.map((s) => ({ ...s })) });");
    await ev(ctx, "loadList()"); await tick();                                   // 목록(A 살아 있음)
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);  // 터미널 A 를 보다가
    await ev(ctx, "loadDetail(SESSIONS[1])"); await tick(15);                     // 다른 세션 상세로 떠남(터미널 정리)
    ctx.document.hidden = true;                                                   // 앱을 내림 — 20초 갱신은 숨은 동안 건너뛴다
    const poll = ev(ctx, "__intervals.find((i) => i.ms === 20000).f");
    ev(ctx, "globalThis.__lists = 0; T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s, live_terminal: false })) }; };");
    for (let i = 0; i < 3; i++) await poll();
    const polledWhileHidden = ev(ctx, "__lists");
    ev(ctx, "const __realNow = Date.now; Date.now = () => __realNow() + 4 * 3600 * 1000;");   // 4시간 뒤(그사이 쉬는 세션 정리로 꺼짐)
    ctx.document.hidden = false;
    ev(ctx, "SESSIONS[0].live_terminal = false");                                // 화면의 줄은 꺼짐으로 보인다고 해도 같은 결과가 되게
    const menu = ev(ctx, "rowMenuItems(SESSIONS[0])[0][1]");                     // 떠난 뒤 목록을 아직 못 받았다 — 누르면 목록을 새로 받아 판정
    if (p === "목록 늦음") ev(ctx, "T.list = () => { __lists++; return new Promise(() => {}); };");
    const before = ev(ctx, "__tc.length");
    await ctx.jumpToSession(ev(ctx, "SESSIONS[0]")); await tick(RENDER);
    return { polledWhileHidden, menu, now: nowAt(ctx), terminalRequests: ev(ctx, "__tc.length") - before, tabs: ev(ctx, "TABS.list.length") };
  });
  assert.deepEqual(got, {
    "목록 늦음": { polledWhileHidden: 0, menu: "터미널 열기", now: "detail:a", terminalRequests: 0, tabs: 1 },
    "목록 옴(꺼짐)": { polledWhileHidden: 0, menu: "터미널 열기", now: "detail:a", terminalRequests: 0, tabs: 0 },
  });
});

test("T48c 바로 끝나는 누름도 마지막 누름이다 — 떠났던 터미널 줄(A)이 목록을 받는 사이 지금 보는 터미널(B)의 탭 칩·알림을 누르면 늦게 끝난 A 가 B 를 덮지 않는다", async () => {
  const paths = ["탭 칩", "알림"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot();
    ev(ctx, "SESSIONS.forEach((s) => { s.live_terminal = false; }); globalThis.__lr = []; T.list = async () => ({ sessions: SESSIONS.map((s) => ({ ...s })) });");
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);   // 터미널 A(방금 띄움)
    await ev(ctx, "loadViewer(SESSIONS[1])"); await tick(15);                     // 떠남(A 를 살아 있게 봄)
    await ctx.loadTerminal(ev(ctx, "SESSIONS[1]"), false); await tick(RENDER);   // 터미널 B 를 보는 중(A 는 탭 줄에)
    ev(ctx, "T.list = () => new Promise((r) => __lr.push(r));");
    const pA = ctx.jumpToSession(ev(ctx, "SESSIONS[0]")); await tick(50);        // 목록 줄 A — 목록을 새로 받는 중
    if (p === "탭 칩") ev(ctx, `switchTab('${B}')`); else await ctx.openSessionById(B);   // 보던 B 를 그대로 — 바로 끝남
    await tick(50);
    ev(ctx, `__lr[0]({ sessions: SESSIONS.map((s) => ({ ...s, live_terminal: true })) })`);
    await pA; await tick(RENDER);
    return { now: nowAt(ctx), active: ev(ctx, "termView.active") === B };
  });
  assert.deepEqual(got, all(paths, { now: "terminal:b", active: true }));
});
