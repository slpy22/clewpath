// 바로가기(quick-switch) 검토 지적 — PC 전환 틈과 PC 별 기억(T33~T39). docs/designs/quick-switch.md §6-1·§6-2·§9.
// 릴레이로 PC 를 바꾸면 연결을 기다리는 사이에도 이전 PC 의 줄·탐색기가 남아 눌렸고, 그 동작이 새 PC 기억 키에 이전 PC 세션·파일을
// 적었다. 첫 목록 전의 저장도 아직 읽지 않은 그 PC 기억을 덮어썼다. 페어링 해제·기기 초기화는 PC 별 기억을 남겼다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const RENDER = 350;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const TWO_PCS = "pcsUpsert({ room: 'roomA', cs: 'c', cp: 'p', dev: 'dA', name: 'A' }); pcsUpsert({ room: 'roomB', cs: 'c', cp: 'p', dev: 'dB', name: 'B' });";
const QUIET = "toast = () => {}; maybeShowIosHint = () => {}; refreshPushBtn = () => {};";

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
  const ctx = await load(local ? { hash, setup, location: { pathname: "/app" } } : { hash, setup });
  const pd = stubEl("div"), app = stubEl("div"), pl = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", pl);
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = [];
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb) => { const h = { ids, mgr, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.api = async () => ({});
    fetchRecentMessages = async () => [];
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  if (local) ev(ctx, "RELAY_APP_URL = ''");
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  return { ctx, pd, app, pl };
}

// ---------------------------------------------------------------- 첫 목록 전 저장(F15)
test("T33 첫 목록 전에 터미널을 하나 열어도(비서·알림 등) 그 PC 의 저장된 탭 기억을 덮어쓰지 않는다 — 새로고침 직후·PC 전환 직후 모두 목록 뒤 합쳐진다", async () => {
  const SAVED = JSON.stringify({ active: B, list: [{ key: A, sid: A }, { key: B, sid: B }], mru: [B, A] });
  const sA = `{ session_id: '${A}', title: 'A', live_terminal: true }`, sB = `{ session_id: '${B}', title: 'B', live_terminal: true }`;
  const got = await each(["새로고침", "PC 전환"], async (p) => {
    const room = p === "새로고침" ? "room1" : "roomB";
    const { ctx } = await boot({ width: 390, setup: (sb) => { sb.localStorage.setItem("sm_room", p === "새로고침" ? "room1" : "roomA");
      sb.localStorage.setItem("sm_tabs_" + room, SAVED); } });
    ev(ctx, TWO_PCS + QUIET + "conn.connect = async () => ({}); globalThis.__lr = [];");
    if (p === "PC 전환") {
      ev(ctx, "T.list = async () => ({ sessions: SESSIONS.slice() })");
      await ev(ctx, "loadList()"); await tick();                      // PC A 첫 목록
      ev(ctx, "T.list = () => new Promise((r) => __lr.push(r))");
      await ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })"); await tick();   // PC B 연결 — 첫 목록을 기다리는 중
    } else {
      ev(ctx, "T.list = () => new Promise((r) => __lr.push(r))");
      ev(ctx, "loadList()"); await tick();                             // 첫 목록을 기다리는 중
    }
    await ctx.loadTerminal(ev(ctx, `(${sA})`), false); await tick(RENDER);   // 그사이 터미널 하나(A)
    const mid = JSON.parse(ctx.localStorage.getItem("sm_tabs_" + room)).list.length;
    ev(ctx, `__lr.at(-1)({ sessions: [${sA}, ${sB}] })`); await tick(RENDER);   // 첫 목록 → 탭 기억 복원
    return { mid, tabs: ev(ctx, "TABS.list.map((t) => t.s.title).join(',')"),
             saved: JSON.parse(ctx.localStorage.getItem("sm_tabs_" + room)).list.map((t) => t.sid.slice(0, 1)).join(",") };
  });
  assert.deepEqual(got, all(["새로고침", "PC 전환"], { mid: 2, tabs: "A,B", saved: "a,b" }));
});

// ---------------------------------------------------------------- 파일 옛 키 옮기기(F16)
test("T34 파일: 첫 목록 전에 옛 키(PC 구분 없음)를 읽어도 거르지 않은 채 이 PC 키에 적지 않고, 목록이 오면 이 PC 폴더만 남겨 한 번 적는다", async () => {
  const OLD = JSON.stringify({ open: ["g:/x|"], sort: "name",
    tabs: [{ root: "g:/x", path: "other-pc.md", pinned: true }, { root: "e:/p", path: "mine.py", pinned: true }], active: "g:/x|other-pc.md" });
  const { ctx } = await boot({ width: 390, setup: (sb) => { sb.localStorage.setItem("sm_room", "room2"); sb.localStorage.setItem("sm_fb_state2", OLD); } });
  ev(ctx, "SESSIONS = [{ session_id: 's1', title: 'P', cwd: 'E:/p', mtime: 1 }]");   // 알림이 먼저 받은 목록(첫 목록 전 — LIST_READY 그대로)
  ev(ctx, "fbUseSession(SESSIONS[0])");                                 // 그 세션 상세의 📂
  const early = ctx.localStorage.getItem("sm_fb_state2_room2");
  ev(ctx, "T.list = async () => ({ sessions: SESSIONS.slice() })");
  await ev(ctx, "loadList()"); await tick(RENDER);                      // 이 PC 첫 목록
  const wrote = JSON.parse(ctx.localStorage.getItem("sm_fb_state2_room2") || "null");
  assert.deepEqual({ early, tabs: wrote && wrote.tabs.map((t) => t.root + "|" + t.path), open: wrote && wrote.open,
                     mem: ev(ctx, "FB.tabs.map((t) => t.id).join(',')"), active: ev(ctx, "FB.active"), oldKept: ctx.localStorage.getItem("sm_fb_state2") === OLD },
                   { early: null, tabs: ["e:/p|mine.py"], open: ["e:/p|"], mem: "e:/p|mine.py", active: "e:/p|mine.py", oldKept: true });
});

// ---------------------------------------------------------------- PC 전환 틈(F4·F10·F13)
test("T37 PC 를 바꾸면 곧바로 목록 칸을 보이고 이전 PC 의 줄·탐색기를 지운다 — 연결을 기다리는 사이 누를 옛 줄이 없고, 폰은 연결이 실패해도 빈 화면에 갇히지 않는다", async () => {
  const paths = ["폰|연결 대기", "폰|연결 실패", "PC|연결 대기"];
  const got = await each(paths, async (p) => {
    const [dev, how] = p.split("|");
    const { ctx, app, pl } = await boot({ width: dev === "폰" ? 390 : 1400, setup: (sb) => sb.localStorage.setItem("sm_room", "roomA") });
    ev(ctx, TWO_PCS + QUIET + `T.list = async () => ({ sessions: SESSIONS.slice() });
      conn.connect = ${how === "연결 실패" ? "async () => { throw new Error('연결이 끊겼습니다'); }" : "() => new Promise(() => {})"};`);
    await ev(ctx, "loadList()"); await tick();                          // 이전 PC 목록
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);   // 폰: 터미널 전체 화면(상세 칸)
    const before = pl.children.length;
    ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })"); await tick(); await tick();
    return { before: before > 1, view: dev === "폰" ? app.getAttribute("data-view") : "-", list: pl.children.map((c) => String(c.className)).join(","),
             sessions: ev(ctx, "SESSIONS.length"), listCtx: ev(ctx, "LIST_CTX === null") };
  });
  assert.deepEqual(got, {
    "폰|연결 대기": { before: true, view: "list", list: "empty", sessions: 0, listCtx: true },
    "폰|연결 실패": { before: true, view: "list", list: "empty", sessions: 0, listCtx: true },
    "PC|연결 대기": { before: true, view: "-", list: "empty", sessions: 0, listCtx: true },
  });
});

test("T38 2차 인증 확인을 기다리는 사이 PC 를 바꾸면 그 터미널·웹 재개는 열지 않는다 — 새 PC 로 요청하지 않고 새 PC 기억에도 적지 않는다", async () => {
  const paths = ["터미널", "웹 재개"];
  const got = await each(paths, async (p) => {
    const { ctx } = await boot({ setup: (sb) => sb.localStorage.setItem("sm_room", "roomA") });
    ev(ctx, TWO_PCS + QUIET + "conn.connect = async () => ({}); T.list = () => new Promise(() => {});");
    let release = null; ctx.ensurePriv = () => new Promise((r) => { release = r; });
    const pt = p === "터미널" ? ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false) : ev(ctx, "loadResume(SESSIONS[0], true, false)");
    for (let i = 0; i < 5 && !release; i++) await tick();
    if (!release) throw new Error("2차 인증 확인까지 가지 못함");
    await ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })"); await tick();
    release({}); await pt; await tick(RENDER);                           // 이전 PC 의 확인이 늦게 끝남
    return { terms: ev(ctx, "__tc.length"), resumes: ev(ctx, "__res.length"), tabs: ev(ctx, "TABS.list.length"), now: ev(ctx, "PANE_NOW.kind"),
             tabsB: ctx.localStorage.getItem("sm_tabs_roomB"), paneB: ctx.localStorage.getItem("sm_pane_roomB") };
  });
  assert.deepEqual(got, all(paths, { terms: 0, resumes: 0, tabs: 0, now: "empty", tabsB: null, paneB: null }));
});

// ---------------------------------------------------------------- 해제·초기화(F11)
test("T39 '이 기기 초기화'·PC 페어링 해제(⛔ PC 의 '지우기' 포함)는 PC 별 화면 기억(터미널 탭·파일·화면 기억)도 지운다 — 해제는 그 PC 것만, 화면 설정은 그대로", async () => {
  const MEM = { sm_tabs_rmA: "{}", sm_fb_state2_rmA: JSON.stringify({ tabs: [{ root: "e:/clients/acme", path: "contract.md" }] }),
                sm_pane_rmA: JSON.stringify({ monitor: { name: "비밀 팀" } }), sm_tabs_rmB: "{}", sm_fb_state2_rmB: "{}", sm_pane_rmB: "{}",
                sm_tabs: "{}", sm_fb_state2: "{}", sm_list_view: "files" };
  const run = async (how) => {
    const ctx = await load({ setup: (sb) => { for (const [k, v] of Object.entries(MEM)) sb.localStorage.setItem(k, v); } });
    ev(ctx, "location.reload = () => {}; setTitle = () => {}; toast = () => {}; confirmSheet = async (o) => { await o.run(); return true; };"
          + "revokeEnqueue = async () => {}; revokeFlush = async () => ({ done: [] }); conn.req = async () => ({});"
          + "pcsUpsert({ room: 'rmA', cs: 'ca', cp: 'pa', dev: 'da' }); pcsUpsert({ room: 'rmB', cs: 'cb', cp: 'pb', dev: 'db' }); pcsActivate('rmA');");
    if (how === "지우기") {                                             // 페어링 화면: ⛔ PC 에서 해제된 PC 의 '지우기'
      ev(ctx, "pcMarkState('rmA', 'removed')");
      const host = stubEl("div"); ctx.document._register("p-pcs", host);
      ctx.renderPairPcs();
      const flat = (n) => [n, ...(n.children || []).flatMap(flat)];
      flat(host).find((n) => n.tagName === "BUTTON" && n.textContent === "지우기").onclick();
    } else await ev(ctx, how);
    return Object.keys(MEM).filter((k) => ctx.localStorage.getItem(k) !== null).join(",");
  };
  const KEEP_B = "sm_tabs_rmB,sm_fb_state2_rmB,sm_pane_rmB,sm_tabs,sm_fb_state2,sm_list_view";
  assert.deepEqual({ unpair: await run("unpairPc('rmA')"), remove: await run("지우기"), reset: await run("resetDevice()") },
                   { unpair: KEEP_B, remove: KEEP_B, reset: "sm_list_view" });
});
