// 바로가기(quick-switch) 검토 지적 — 릴레이 연결 쪽(T40~T42). docs/designs/quick-switch.md §6-2·§7(바뀌는 것 6·9·12).
// 1-2 로 관제·뷰어·탭 배지·웹 재개도 stream_close 를 보내게 됐다. 커넥터는 rid 만 보고 닫으므로(보낸 기기를 보지 않음)
// 닫기 신호는 '그 스트림을 연 소켓·키로, 아직 살아 있는 스트림에만' 가야 한다 — 다른 PC·다른 기기의 같은 번호 파이프를 끊지 않게.
// PC 를 바꿀 때는 이전 소켓을 닫고(커넥터가 client_offline 으로 그 기기 파이프를 모두 해체), 늦게 온 그 소켓의 통지는 새 연결을 건드리지 않는다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const RENDER = 350;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const settle = async () => { for (let i = 0; i < 12; i++) await tick(); };   // WebCrypto 봉인·수신 큐(_txq·_rxq)가 돌게
const flatEl = (n) => [n, ...(n.children || []).flatMap(flatEl)];
const composer = (pd) => pd.children.find((c) => String(c.className) === "composer").children;   // [입력칸, 보내기]
const closes = (frames) => frames.filter((f) => f.type === "stream_close").map((f) => f.id);
const TWO_PCS = "pcsUpsert({ room: 'roomA', name: 'A' }); pcsUpsert({ room: 'roomB', name: 'B' });";

async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
const all = (paths, want) => Object.fromEntries(paths.map((p) => [p, want]));

// 브라우저와 같은 규칙의 WebSocket 대역: CONNECTING 에서 send 는 예외, CLOSING·CLOSED 에서 send 는 버린다.
// close() 뒤 onclose 는 닫기 핸드셰이크가 끝난 뒤에 온다(finishClose) — 새 연결이 선 뒤에 오는 경우를 만든다.
function browserWs(ctx, { deferClose = true } = {}) {
  const all = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; all.push(this); }
    send(s) { if (this.readyState === 0) throw new Error("InvalidStateError"); if (this.readyState === 1) this.sent.push(JSON.parse(s)); }
    close(code) { if (this.readyState >= 2) return; this.readyState = 2; this._code = code || 1000; if (!deferClose) this.finishClose(); }
    finishClose() { if (this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose({ code: this._code || 1006 }); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    push(m) { this.onmessage({ data: JSON.stringify(m) }); }
  };
  return all;
}
async function connectWs(ctx, sockets, room) {
  const n = sockets.length;
  const p = ev(ctx, `conn.connect('${room}', 'tok')`);
  for (let i = 0; i < 50 && sockets.length === n; i++) await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c" + sockets.length });
  await p; await tick();
  return ws;
}
const rk = (seed) => { const raw = new Uint8Array(32); for (let i = 0; i < 32; i++) raw[i] = (i * 7 + seed) & 255;
  return Buffer.from(raw).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); };
// 그 방의 E2EE 키를 든 별도 Conn — 선에 실린 봉투를 열거나(이 프레임이 어느 PC 키로 봉인됐나) PC 가 보낸 것처럼 봉인한다
const roomConn = (ctx, room) => ev(ctx, `(async () => { const c = new Conn(); c.e2eeKey = await crypto.subtle.importKey('raw',
  b64uToU8(LS.getItem('sm_e2ee_${room}')), 'AES-GCM', false, ['encrypt','decrypt']); return c; })()`);
async function wire(ctx, ws, room) {
  const c = room ? await roomConn(ctx, room) : null;
  const out = [];
  for (const f of ws.sent) {
    if (f && f.e === "a1") { try { out.push(await c._openEnv(f)); } catch (e) { out.push({ undecryptable: true }); } }
    else out.push(f);
  }
  return out;
}

// 상세 칸·#app 을 고정하고(하네스 주의 §8) 터미널·관제·재개 연결은 스텁으로 기록한다(기본 릴레이·PC 폭)
async function boot({ width = 1400, setup = null } = {}) {
  const ctx = await load({ setup });
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
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  return { ctx, pd, app };
}

// PC 를 바꾸는 세 길: 머리 줄 PC 선택(switchToPc) · '＋ 새 PC 추가' 화면의 저장된 PC 누름(renderPairPcs) · 새 PC 페어링(doPair)
async function switchVia(ctx, how, { e2ee = false } = {}) {
  if (how === "머리 줄") return ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })");
  if (how === "저장된 PC") {
    const host = stubEl("div"); ctx.document._register("p-pcs", host);
    ctx.renderPairPcs();
    return flatEl(host).find((n) => n.tagName === "BUTTON" && n.textContent === "🖥 B").onclick();
  }
  const li = stubEl("input"); li.value = "https://relay.test/relay/app#room=roomB&token=tok" + (e2ee ? "&rk=" + rk(2) : "");
  ctx.document._register("p-link", li);
  ev(ctx, "doPair()");
  return null;
}

// ---------------------------------------------------------------- 응답 중에 떠난 웹 재개(결정 7)와 PC 전환
test("T40 응답 중에 떠나 닫기를 미룬 웹 재개도 PC 를 바꿀 때 이전 PC 에서 바로 닫는다 — 이전 연결의 늦은 끝 알림에 '응답이 끝나…' 알림이 뜨지 않는다", async () => {
  const paths = ["머리 줄", "저장된 PC", "새 PC 페어링"];
  const got = await each(paths, async (how) => {
    const { ctx, pd } = await boot({ setup: (sb) => sb.localStorage.setItem("sm_room", "roomA") });
    for (const id of ["p-err", "p-go"]) ctx.document._register(id, stubEl(id === "p-go" ? "button" : "div"));
    ev(ctx, TWO_PCS + `globalThis.__toasts = []; toast = (m) => __toasts.push(m); maybeShowIosHint = () => {}; refreshPushBtn = () => {}; enterApp = () => {};
      conn.connect = async () => ({ auth: null }); T.list = async () => ({ sessions: [] });
      T.resume = (id, cb) => { const h = { id, cb, closed: 0, room: null, input() {}, close() { h.closed++; h.room = LS.getItem('sm_room'); } }; __res.push(h); return h; };`);
    await ev(ctx, "loadResume(SESSIONS[1], true, false)");
    const [ta, send] = composer(pd); ta.value = "질문"; send.onclick();   // 응답 중
    await ev(ctx, "loadDetail(SESSIONS[0])"); await tick();               // 떠남 — 그 응답이 끝난 뒤 닫는다(미룸)
    const deferred = ev(ctx, "__res[0].closed");
    await switchVia(ctx, how); await tick(); await tick();
    const r = ev(ctx, "__res[0]"), atSwitch = { closed: r.closed, room: r.room };
    r.cb({ eof: true }); await tick();                                     // 이전 연결이 끊기며 오는 늦은 끝 알림
    return { deferred, atSwitch, closed: r.closed, finishedToast: ev(ctx, "__toasts.some((m) => m.startsWith('응답이 끝나'))") };
  });
  assert.deepEqual(got, all(paths, { deferred: 0, atSwitch: { closed: 1, room: "roomA" }, closed: 1, finishedToast: false }));
});

// ---------------------------------------------------------------- 낡은 손잡이의 stream_close
test("T41 릴레이: 서버가 끝낸 스트림이나 끊기기 전 연결의 스트림 손잡이를 닫아도 stream_close 를 보내지 않는다 — 살아 있는 스트림은 지금처럼 보낸다", async () => {
  const ctx = await load();
  const sockets = browserWs(ctx, { deferClose: false });
  ev(ctx, "toast = () => {}; globalThis.__sched = []; conn._scheduleReconnect = (c) => __sched.push(c);");
  const ws1 = await connectWs(ctx, sockets, "room1");
  ev(ctx, `globalThis.__v = conn.startMonitor('${A}', '${A}', () => {});      // 뷰어 실시간
           globalThis.__m = conn.startMonitor('a,b', 'a', () => {}, 'g1');   // 팀 관제
           globalThis.__t = conn.startTerminal('${A}', () => {}, {});
           globalThis.__r = conn.startResume('${B}', () => {}, true, {});`);
  const [iv, im, it, ir] = ws1.sent.filter((f) => f.type === "req").map((f) => f.id);
  ws1.push({ type: "stream", id: im, eof: true, error: "Host 재기동" }); await tick();   // 서버(커넥터)가 관제를 끝내고 rid 를 비움
  ev(ctx, "__m.close()");                                                // '연결 종료'로 남은 관제 칸을 나중에 닫음
  ev(ctx, "__t.close()");                                                // 살아 있는 터미널 화면 닫기 — 지금처럼 보낸다
  ws1.close(1006);                                                       // 릴레이가 끊김 — 남은 스트림은 eof
  const ws2 = await connectWs(ctx, sockets, "room1");                    // 같은 PC 에 다시 연결
  ev(ctx, "__v.close(); __r.close()");                                   // 다른 화면으로 가며 끊기기 전 연결의 손잡이를 닫음
  assert.deepEqual({ ws1: closes(ws1.sent), ws2: closes(ws2.sent), streams: ev(ctx, "conn.streams.size"), ids: [iv, ir].every(Boolean) },
                   { ws1: [it], ws2: [], streams: 0, ids: true });
});

// ---------------------------------------------------------------- PC 전환: 닫기 신호·이전 소켓·늦은 통지
test("T42 릴레이 PC 전환(머리 줄·저장된 PC·새 PC 페어링 × 평문·E2EE): 이전 PC 화면의 닫기 신호는 모두 이전 PC 키로 이전 PC 에만 가고, 이전 소켓은 닫히며, 늦게 온 그 소켓의 끝 알림·닫힘 통지는 새 연결을 건드리지 않는다", async () => {
  const paths = [];
  for (const how of ["머리 줄", "저장된 PC", "새 PC 페어링"]) for (const enc of ["평문", "E2EE"]) paths.push(how + "|" + enc);
  const got = await each(paths, async (p) => {
    const [how, enc] = p.split("|"), e2ee = enc === "E2EE";
    const ctx = await load({ setup: (sb) => {
      sb.localStorage.setItem("sm_room", "roomA");
      if (e2ee) { sb.localStorage.setItem("sm_e2ee_roomA", rk(1)); sb.localStorage.setItem("sm_e2ee_roomB", rk(2)); }
    } });
    const pd = stubEl("div"), app = stubEl("div");
    ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
    ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
    ctx.document._register("p-err", stubEl("div")); ctx.document._register("p-go", stubEl("button"));
    ev(ctx, `innerWidth = 1400; globalThis.__toasts = []; toast = (m) => __toasts.push(m); enterApp = () => {};
      maybeShowIosHint = () => {}; refreshPushBtn = () => {}; fetchRecentMessages = async () => [];
      globalThis.MutationObserver = class { observe() {} disconnect() {} };
      globalThis.__sched = []; conn._scheduleReconnect = (c) => __sched.push(c);
      ${TWO_PCS}
      SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' }, { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
    ctx.ensurePriv = async () => ({}); ctx.confirm = () => true;
    const sockets = browserWs(ctx);
    const wsA = await connectWs(ctx, sockets, "roomA");
    // 이전 PC 화면: 웹 재개 → 응답 중에 터미널로 떠남(재개 닫기는 응답이 끝난 뒤로 미룸) → 터미널 + 탭 배지 관제
    await ev(ctx, "loadResume(SESSIONS[1], true, false)"); await settle();
    const [ta, send] = composer(pd); ta.value = "질문"; send.onclick(); await settle();
    await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(RENDER);
    const opened = (await wire(ctx, wsA, e2ee && "roomA")).filter((f) => f.type === "req" && ["resume", "terminal", "monitor"].includes(f.method)).map((f) => f.id);
    if (opened.length !== 3) throw new Error("출발이 재개·터미널·탭 배지 3개가 아님: " + opened);
    const n = sockets.length;
    const pSwitch = switchVia(ctx, how, { e2ee });
    for (let i = 0; i < 50 && sockets.length === n; i++) await tick();
    const wsB = sockets.at(-1); wsB.open(); wsB.push({ type: "hello", peer_present: true, cid: "cB" });
    await pSwitch; await settle();
    // 새 PC 에서 기다리는 요청·스트림을 둔 채, 이전 PC 의 늦은 재개 결과와 이전 소켓의 닫힘 통지가 온다
    ev(ctx, "globalThis.__req = 'pending'; conn.req('list_sessions', {}).then((r) => { __req = r; }); globalThis.__eof = 0;"
          + "conn.startMonitor('x', 'x', (d, eof) => { if (eof) __eof++; });");
    const late = { v: 1, type: "stream", id: opened[0], data: { type: "result", total_cost_usd: 0 } };
    wsA.push(e2ee ? await (await roomConn(ctx, "roomA"))._seal(late) : late);   // PC A 가 봉인해 보낸 것처럼
    await settle();
    const oldSocket = wsA.readyState;                                     // 2 = 화면이 닫기를 청했다(닫기 핸드셰이크 중)
    wsA.finishClose(); await settle();                                    // 그 소켓의 닫힘 통지가 새 연결이 선 뒤에 온다
    const toA = closes(await wire(ctx, wsA, e2ee && "roomA")), toB = closes(await wire(ctx, wsB, e2ee && "roomB"));
    return { closedOnA: opened.filter((id) => toA.includes(id)).length, closedOnB: toB, oldSocket,
             newReq: ev(ctx, "__req"), newEof: ev(ctx, "__eof"), resched: ev(ctx, "__sched.length"), current: ev(ctx, "conn.ws") === wsB,
             finishedToast: ev(ctx, "__toasts.some((m) => m.startsWith('응답이 끝나'))") };
  });
  assert.deepEqual(got, all(paths, { closedOnA: 3, closedOnB: [], oldSocket: 2, newReq: "pending", newEof: 0, resched: 0, current: true, finishedToast: false }));
});

// ---------------------------------------------------------------- 검토 2회차: 진행 중이던 연결 시도와 PC 전환
// 재접속 백오프 타이머(1초 이상)는 붙잡아 손으로 돌린다 — 실제 시간을 기다리지 않고, 재접속이 하나 더 걸리는지 센다
function holdTimers(ctx) {
  const timers = [], realT = ctx.setTimeout;
  ctx.setTimeout = (f, ms) => (ms >= 1000 ? (timers.push(f), 0) : realT(f, ms));
  return timers;
}
async function bootConn({ setup = null } = {}) {
  const ctx = await load({ setup: (sb) => { sb.localStorage.setItem("sm_room", "roomA"); if (setup) setup(sb); } });
  for (const id of ["paneDetail", "app", "head", "paneList", "p-err", "p-go"]) ctx.document._register(id, stubEl(id === "p-go" ? "button" : "div"));
  ctx.document.getElementById("app").setAttribute("data-view", "list");
  const timers = holdTimers(ctx);
  ev(ctx, `innerWidth = 1400; globalThis.__toasts = []; toast = (m) => __toasts.push(m); enterApp = () => {};
    globalThis.__loads = 0; loadList = async () => { __loads++; }; maybeShowIosHint = () => {}; refreshPushBtn = () => {}; refreshVer = async () => {};`);
  return { ctx, timers };
}
const until = async (f, n = 150) => { for (let i = 0; i < n && !f(); i++) await tick(20); return f(); };

test("T43 릴레이: 재접속 connect() 가 진행 중일 때 PC 를 바꿔도 그 시도의 실패가 재접속을 하나 더 걸지 않는다 — 전환 소켓이 지금 연결로 남는다", async () => {
  const run = async (inFlight) => {
    const { ctx, timers } = await bootConn();
    ev(ctx, TWO_PCS);
    const sockets = browserWs(ctx);
    const wsA = await connectWs(ctx, sockets, "roomA");
    let pending = null;
    if (inFlight) {
      wsA.finishClose(); await tick();                                   // 망이 잠깐 끊김(1006) → 재접속 예약
      if (timers.length !== 1) throw new Error("재접속 예약이 없음");
      timers.shift()();                                                   // 1초 뒤 재접속 — 새 소켓이 hello 를 기다린다(진행 중)
      await until(() => sockets.length === 2);
      pending = sockets.at(-1);
    }
    const n = sockets.length;
    const pSwitch = ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })");   // 그 순간 머리 줄에서 PC B 로
    await until(() => sockets.length > n);
    const wsB = sockets.at(-1); wsB.open(); wsB.push({ type: "hello", peer_present: true, cid: "cB" });
    await pSwitch; await settle();
    (pending || wsA).finishClose(); await settle();                       // 내려놓은 소켓의 닫기 핸드셰이크가 끝남
    const queued = timers.length;
    while (timers.length) timers.shift()();                              // 걸린 재접속이 있으면 돌린다
    await settle();
    return { reconnectQueued: queued, sockets: sockets.length, current: ev(ctx, "conn.ws") === wsB, switchOpen: wsB.readyState,
             reconnectedToast: ev(ctx, "__toasts.filter((m) => m === '다시 연결됨').length"), loads: ev(ctx, "__loads") };
  };
  assert.deepEqual({ control: await run(false), inFlight: await run(true) }, {
    control: { reconnectQueued: 0, sockets: 2, current: true, switchOpen: 1, reconnectedToast: 0, loads: 0 },
    inFlight: { reconnectQueued: 0, sockets: 3, current: true, switchOpen: 1, reconnectedToast: 0, loads: 0 },
  });
});

test("T44 릴레이: CP 조회를 기다리던 재접속(이전 PC)이 PC 전환 뒤에 끝나도 지금 연결(새 PC)을 덮지 않는다 — 소켓을 만들지 않고, 그 JWT 를 새 PC 자격으로 적지 않으며, 새 PC 가 '페어링 바뀜'이 되지 않는다(끊김 1006·자격 만료 4402)", async () => {
  const run = async (code) => {
    const { ctx, timers } = await bootConn({ setup: (sb) => {
      sb.localStorage.setItem("sm_cs", "csA"); sb.localStorage.setItem("sm_cp", "cpA"); sb.localStorage.setItem("sm_devtoken", "devA"); } });
    ev(ctx, "conn.devToken = 'devA'; pcsUpsert({ room: 'roomA', name: 'A', cs: 'csA', cp: 'cpA', dev: 'devA' });"
          + "pcsUpsert({ room: 'roomB', name: 'B', cs: 'csB', cp: 'cpB', dev: 'devB' });");
    // CP /client/token 조회는 손으로 끝낸다(느린 망)
    const fetches = [];
    ctx.fetch = (url, opts) => new Promise((res) => { fetches.push({ pid: JSON.parse(opts.body).public_id, res }); });
    const answer = (i, token, ttl) => fetches[i].res({ ok: true, status: 200, json: async () => ({ token, expires_at: Math.floor(Date.now() / 1000) + ttl }) });
    const sockets = browserWs(ctx);
    const authed = [];
    const authOk = async (ws, cid) => {
      ws.open(); ws.push({ type: "hello", peer_present: true, cid });
      await until(() => ws.sent.some((f) => f.method === "auth"));
      const a = ws.sent.find((f) => f.method === "auth"); authed.push([new URL(ws.url).searchParams.get("room"), a.params.token]);
      ws.push({ type: "res", id: a.id, ok: true, data: { id: "d", name: "n" } }); await settle();
    };
    // 1) PC A — JWT 는 곧 만료(200초 → 5분 사전 갱신 구간이라 재접속 때 다시 조회한다)
    const p = ev(ctx, "conn.connect('roomA', 'tok')");
    await until(() => fetches.length === 1); answer(0, "jwtA", 200);
    await until(() => sockets.length === 1); await authOk(sockets[0], "cA"); await p;
    // 2) 끊김 → 재접속이 CP 조회를 기다린다(1006 은 connect 안에서, 4402 는 재접속 타이머의 강제 갱신)
    sockets[0]._code = code; sockets[0].finishClose(); await tick();
    timers.shift()();
    await until(() => fetches.length === 2);
    // 3) 그사이 머리 줄에서 PC B 로 — B 조회가 먼저 끝나 B 가 연결된다
    const pSwitch = ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })");
    await until(() => fetches.length === 3); answer(2, "jwtB", 3600);
    await until(() => sockets.length === 2); const wsB = sockets[1];
    await authOk(wsB, "cB"); await pSwitch; await settle();
    // 4) 늦게 A 조회가 끝난다
    answer(1, "jwtA2", 3600); await settle(); await tick(50);
    // (고치기 전에는 여기서 A 소켓이 생겼다 — 그 hello 가 지금 기기 토큰으로 A 에 인증하고 A 는 모르는 토큰이라 auth_invalid)
    if (sockets.length > 2) {
      const wsA2 = sockets[2]; wsA2.open(); wsA2.push({ type: "hello", peer_present: true, cid: "cA2" });
      await until(() => wsA2.sent.some((f) => f.method === "auth"), 20);
      const a = wsA2.sent.find((f) => f.method === "auth");
      if (a) { authed.push(["roomA", a.params.token]); wsA2.push({ type: "res", id: a.id, ok: false, error: "auth_invalid" }); await settle(); }
    }
    return { fetched: fetches.map((f) => f.pid), sockets: sockets.length, current: ev(ctx, "conn.ws") === wsB, wsB: wsB.readyState,
             cjwt: JSON.parse(ctx.localStorage.getItem("sm_cjwt")).token, authed, stateB: ev(ctx, "pcsLoad().find((x) => x.room === 'roomB').state || null"),
             stopped: ev(ctx, "conn._stopped"), reconnects: timers.length };
  };
  const got = await each(["1006", "4402"], (c) => run(Number(c)));
  assert.deepEqual(got, all(["1006", "4402"], { fetched: ["cpA", "cpA", "cpB"], sockets: 2, current: true, wsB: 1, cjwt: "jwtB",
    authed: [["roomA", "devA"], ["roomB", "devB"]], stateB: null, stopped: false, reconnects: 0 }));
});

test("T45 릴레이: 다른 기기(cid) 앞으로 온 프레임은 같은 번호의 내 스트림·대기 요청·통지로 받지 않는다 — 내 cid·cid 없는 프레임은 그대로(평문·E2EE)", async () => {
  const run = async (enc) => {
    const ctx = await load({ setup: (sb) => { if (enc) sb.localStorage.setItem("sm_e2ee_roomA", rk(1)); } });
    const sockets = browserWs(ctx, { deferClose: false });
    ev(ctx, "toast = () => {}; globalThis.__notice = []; conn.onnotice = (m) => __notice.push(m.kind);");
    const ws = await connectWs(ctx, sockets, "roomA");
    const me = ev(ctx, "conn.cid");
    ev(ctx, "globalThis.__got = []; conn.startMonitor('x', 'x', (d, eof) => __got.push(eof ? 'eof' : d));"
          + "globalThis.__res = 'pending'; conn.req('list_sessions', {}).then((r) => { __res = r.data; });");
    await settle();
    const [mid, rid] = (await wire(ctx, ws, enc && "roomA")).map((f) => f.id);
    const pc = enc ? await roomConn(ctx, "roomA") : null;
    const push = async (m) => ws.push(enc ? { ...(await pc._seal(m)), ...(m.cid ? { cid: m.cid } : {}) } : m);   // E2EE 는 겉에도 cid(릴레이 라우팅)
    // 릴레이가 방 전체에 보낸 다른 기기(떠난 cid) 앞 프레임 — 기기마다 번호를 1 부터 세서 내 것과 같은 번호다
    await push({ v: 1, type: "stream", id: mid, cid: "cA_old", data: "남의 관제" });
    await push({ v: 1, type: "res", id: rid, cid: "cA_old", ok: true, data: { sessions: ["남의 목록"] } });
    await push({ v: 1, type: "stream", id: mid, cid: "cA_old", eof: true });
    await push({ v: 1, type: "notice", cid: "cA_old", kind: "device_removed", device: "dA" });
    await settle();
    const foreign = { got: ev(ctx, "__got.slice()"), res: ev(ctx, "__res"), notice: ev(ctx, "__notice.slice()") };
    await push({ v: 1, type: "stream", id: mid, cid: me, data: "내 관제" });
    await push({ v: 1, type: "stream", id: mid, data: "cid 없음" });           // 옛 커넥터(cid 를 싣지 않음)
    await push({ v: 1, type: "res", id: rid, cid: me, ok: true, data: { sessions: [] } });
    await settle();
    return JSON.parse(JSON.stringify({ foreign, mine: { got: ev(ctx, "__got.slice()"), res: ev(ctx, "__res") } }));   // vm 쪽 객체를 이쪽 값으로
  };
  const got = await each(["평문", "E2EE"], (p) => run(p === "E2EE"));
  assert.deepEqual(got, all(["평문", "E2EE"], { foreign: { got: [], res: "pending", notice: [] },
                                               mine: { got: ["내 관제", "cid 없음"], res: { sessions: [] } } }));
});

test("T47 릴레이(실제 연결): 새 PC 페어링이 이전 PC 의 2차 인증 상태 요청(응답 없음)을 끊어도 이전 PC 세션의 터미널을 열지 않는다 — 새 PC 가 연결된 뒤 화면·탭 기억에 남지 않는다", async () => {
  const X = "xxxxxxxx-1111-2222-3333-444444444444";
  const ctx = await load({ setup: (sb) => sb.localStorage.setItem("sm_room", "roomA") });
  const pd = stubEl("div"), app = stubEl("div");
  for (const [id, e] of [["paneDetail", pd], ["app", app], ["head", stubEl("span")], ["paneList", stubEl("div")], ["p-err", stubEl("div")], ["p-go", stubEl("button")]]) ctx.document._register(id, e);
  app.setAttribute("data-view", "list");
  const sockets = browserWs(ctx);
  ev(ctx, `innerWidth = 1400; toast = () => {}; maybeShowIosHint = () => {}; refreshPushBtn = () => {}; fetchRecentMessages = async () => [];
      globalThis.MutationObserver = class { observe() {} disconnect() {} };
      ${TWO_PCS}
      SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' }, { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  const wsA = await connectWs(ctx, sockets, "roomA");
  const pt = ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false);           // 목록 줄 A → 2차 인증 상태를 PC A 에 묻는다 — 답이 없다
  await until(() => wsA.sent.some((f) => f.method === "api"));
  const asked = wsA.sent.filter((f) => f.method === "api").map((f) => f.params.path);
  const li = stubEl("input"); li.value = "https://relay.test/relay/app#room=roomB&token=tok"; ctx.document._register("p-link", li);
  ev(ctx, "doPair()");                                                    // 기다리다 '＋ 새 PC 추가'에서 PC B 링크로
  await pt; await tick(RENDER);
  const mid = { room: ctx.localStorage.getItem("sm_room"), tabs: ev(ctx, "TABS.list.length"), now: ev(ctx, "PANE_NOW.kind"), termMode: pd.classList.contains("term-mode"),
                streams: ev(ctx, "conn.streams.size") };
  await until(() => sockets.length === 2);
  const wsB = sockets.at(-1); wsB.open(); wsB.push({ type: "hello", peer_present: true, cid: "cB" });
  await until(() => wsB.sent.some((f) => f.method === "list_sessions"));
  const lr = wsB.sent.find((f) => f.method === "list_sessions");
  wsB.push({ type: "res", id: lr.id, ok: true, data: { sessions: [{ session_id: X, title: "X", live_terminal: true, cwd: "F:/x" }] } });
  await tick(RENDER);
  const after = { room: ctx.localStorage.getItem("sm_room"), tabsB: ctx.localStorage.getItem("sm_tabs_roomB"), now: ev(ctx, "PANE_NOW.kind"),
                  termMode: pd.classList.contains("term-mode"), terminalOnB: wsB.sent.filter((f) => f.method === "terminal").length };
  assert.deepEqual({ asked, mid, after }, {
    asked: ["/api/owner/2fa/status"],
    mid: { room: "roomA", tabs: 0, now: "empty", termMode: false, streams: 0 },
    after: { room: "roomB", tabsB: null, now: "empty", termMode: false, terminalOnB: 0 },
  });
});
