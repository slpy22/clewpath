// 바로가기(quick-switch) 검토 3회차 — 폰 연결 안정성(T60~). 실제 Conn·페어링·재접속 흐름을 브라우저 규칙 WebSocket 대역으로 돌린다.
// 재접속 백오프 타이머(1초 이상)는 붙잡아 두고 손으로 돌린다 — 실제 시간을 기다리지 않고 '다시 걸렸는가'를 센다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const settle = async (n = 12) => { for (let i = 0; i < n; i++) await tick(); };
const until = async (f, n = 150) => { for (let i = 0; i < n && !f(); i++) await tick(20); return f(); };
const flatEl = (n) => [n, ...(n.children || []).flatMap(flatEl)];
const plain = (v) => JSON.parse(JSON.stringify(v));   // vm 쪽 배열·객체를 이쪽 값으로(엄격 비교는 프로토타입까지 본다)

// 브라우저와 같은 규칙: CONNECTING 에서 send 는 예외, CLOSING·CLOSED 에서 send 는 버린다. close() 뒤 onclose 는 바로(deferClose:false)
function browserWs(ctx, { deferClose = false } = {}) {
  const all = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; all.push(this); }
    send(s) { if (this.readyState === 0) throw new Error("InvalidStateError"); if (this.readyState === 1) this.sent.push(JSON.parse(s)); }
    close(code) { if (this.readyState >= 2) return; this.readyState = 2; this._code = code || 1000; if (!deferClose) this.finishClose(); }
    finishClose() { if (this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose({ code: this._code || 1006 }); }
    drop(code = 1006) { if (this.readyState >= 2) return; this.readyState = 3; this.onclose && this.onclose({ code }); }   // 서버 쪽 끊김·거부
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    push(m) { this.onmessage({ data: JSON.stringify(m) }); }
  };
  return all;
}
const roomOf = (ws) => new URL(ws.url).searchParams.get("room");
function holdTimers(ctx) {
  const timers = [], realT = ctx.setTimeout, realC = ctx.clearTimeout;
  let n = 1e6;
  ctx.setTimeout = (f, ms) => { if (ms >= 1000) { const id = ++n; timers.push({ f, ms, id }); return id; } return realT(f, ms); };
  ctx.clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); else realC(id); };   // 지운 타이머는 빠진다
  return timers;
}
const reconnectTimers = (timers) => timers.filter((t) => String(t.f).includes("_retryTimer"));   // 붙잡은 것 중 재접속 타이머만
// 부트 중에 뜨는 안내도 놓치지 않게 #toast 요소에 쓰인 글을 모은다(toast 는 스크립트가 정의해 부트 전에 바꿔 끼울 수 없다)
function toastLog(sb) {
  const log = [];
  sb.document._register("toast", { classList: { add() {}, remove() {}, contains() { return false; } },
    set textContent(v) { log.push(String(v)); }, get textContent() { return log.at(-1) || ""; } });
  return log;
}
const PAIR_IDS = ["p-go", "p-err", "p-room", "p-token", "p-pcs", "p-link", "p-sit", "p-back", "app", "pair", "paneList", "paneDetail", "head"];
function regIds(sb, ids = PAIR_IDS) {
  const els = {};
  for (const id of ids) { els[id] = stubEl(id === "p-go" ? "button" : "div"); sb.document._register(id, els[id]); }
  return els;
}
// CP /client/token: cpB 는 이미 무효(401), 나머지는 JWT
const cpFetch = (sb, bad = ["cpB"]) => async (url, opts) => {
  const pid = JSON.parse(opts.body).public_id;
  (sb.__fetches ||= []).push(pid);
  if (bad.includes(pid)) return { ok: false, status: 401, json: async () => ({ error: "invalid" }) };
  return { ok: true, status: 200, json: async () => ({ token: "jwt-" + pid, expires_at: Math.floor(Date.now() / 1000) + 3600 }) };
};
const PC_A = { room: "roomA", cs: "csA", cp: "cpA", dev: "devA", name: "A" };
const pcsOf = (ctx) => JSON.parse(ctx.localStorage.getItem("sm_pcs") || "[]").map((x) => [x.room, x.state || null]);
const activeKeys = (ctx) => Object.fromEntries(["sm_room", "sm_cs", "sm_cp", "sm_devtoken"].map((k) => [k, ctx.localStorage.getItem(k)]));
// hello → auth 요청 → 답(평문)
async function helloAuth(ws, cid, reply = { ok: true, data: { id: "d", name: "n" } }) {
  ws.open(); ws.push({ type: "hello", peer_present: true, cid });
  await until(() => ws.sent.some((f) => f.method === "auth"));
  const a = ws.sent.find((f) => f.method === "auth");
  ws.push({ type: "res", id: a.id, cid, ...reply });
  await settle();
  return a;
}

// ---------------------------------------------------------------- 결함 1: 새 PC 의 무효 QR(CP 401)이 멀쩡한 지금 PC 를 '⛔ 페어링 바뀜'으로
test("T60 '＋ 새 PC 추가'로 넣은 새 PC(B) QR 이 이미 무효(CP 401)여도 지금 PC(A)는 그대로 — 표시·자격·재접속 모두 되돌리고 '← 돌아가기'면 A 로 다시 붙는다(E-D16)", async () => {
  let els;
  const ctx = await load({ setup: (sb) => {
    sb.localStorage.setItem("sm_pcs", JSON.stringify([PC_A]));
    els = regIds(sb); sb.fetch = cpFetch(sb);
  } });
  const timers = holdTimers(ctx);
  const sockets = browserWs(ctx);
  ev(ctx, "globalThis.__toasts = []; toast = (m) => __toasts.push(String(m)); globalThis.__entered = 0; enterApp = () => { __entered++; }; openModal = () => {}; loadList = async () => {};");
  // 1) PC A 에 연결(머리 줄 전환과 같은 길)
  const p = ev(ctx, "pcsActivate('roomA'); conn._stopped=false; conn.connect('roomA', '')");
  await until(() => sockets.length === 1);
  await helloAuth(sockets[0], "cA"); await p;
  ev(ctx, "LS.setItem('sm_room','roomA')");
  // 2) '＋ 새 PC 추가' — PC B 링크(그 자격은 이미 무효: CP 401 → JWT 없이 붙어 릴레이가 4401 로 끊는다)
  ev(ctx, "openAddPc()");
  els["p-link"].value = "https://relay.test/relay/app#room=roomB&cs=csB&cp=cpB&dev=devB";
  ev(ctx, "doPair()");
  await until(() => sockets.length === 2);
  const wsB = sockets[1];
  wsB.open(); wsB.drop(4401);
  await settle(); await tick(30);
  const reconnects = reconnectTimers(timers).length;
  for (const t of reconnectTimers(timers)) t.f();     // 걸린 재접속이 있으면 돌려 본다
  await settle(); await tick(30);
  const afterFail = { pcs: pcsOf(ctx), keys: activeKeys(ctx), reconnects, socketsAfterTimers: sockets.length,
                      changedToast: ev(ctx, "__toasts.some((m) => m.includes('페어링이 바뀌었'))"), err: !!els["p-err"].textContent, go: els["p-go"].textContent,
                      entered: ev(ctx, "__entered"), stopped: ev(ctx, "conn._stopped") };
  // 3) '← 돌아가기' — 원래 PC A 로 다시 붙는다(연결 없는 빈 앱에 갇히지 않게)
  ev(ctx, "leaveAddPc()");
  await until(() => sockets.length === 3);
  const wsA2 = sockets.at(-1);
  if (sockets.length === 3) await helloAuth(wsA2, "cA2");
  await settle();
  const back = { room: sockets.length === 3 ? roomOf(wsA2) : null, jwt: sockets.length === 3 ? new URL(wsA2.url).searchParams.get("jwt") : null,
                 entered: ev(ctx, "__entered"), stopped: ev(ctx, "conn._stopped"), current: ev(ctx, "conn.ws") === wsA2 };
  assert.deepEqual({ afterFail, back }, {
    afterFail: { pcs: [["roomA", null]], keys: { sm_room: "roomA", sm_cs: "csA", sm_cp: "cpA", sm_devtoken: "devA" }, reconnects: 0, socketsAfterTimers: 2,
                 changedToast: false, err: true, go: "연결", entered: 0, stopped: true },
    back: { room: "roomA", jwt: "jwt-cpA", entered: 1, stopped: false, current: true },
  });
});

test("T60b 앱 QR 스캔·링크 열기(부트 해시)로 넣은 새 PC(B) QR 이 무효(CP 401)여도 이전 PC(A)는 그대로 — B 는 목록에서 빠지고, 다시 열면 A 로 자동 접속한다", async () => {
  let els, toasts;
  const ctx = await load({ hash: "#room=roomB&cs=csB&cp=cpB&dev=devB", setup: (sb) => {
    for (const [k, v] of [["sm_room", "roomA"], ["sm_cs", "csA"], ["sm_cp", "cpA"], ["sm_devtoken", "devA"], ["sm_pcs", JSON.stringify([PC_A])]]) sb.localStorage.setItem(k, v);
    els = regIds(sb); sb.fetch = cpFetch(sb); toasts = toastLog(sb);
    sb.__all = browserWs(sb);                         // 부트가 바로 연결하므로 대역은 부트 전에 심는다
  } });
  const timers = holdTimers(ctx);
  ev(ctx, "enterApp = () => {}; openModal = () => {}; loadList = async () => {};");
  const sockets = ctx.__all;
  await until(() => sockets.length === 1);
  const wsB = sockets[0];
  assert.equal(roomOf(wsB), "roomB", "부트가 새 PC 로 연결을 시도한다");
  wsB.open(); wsB.drop(4401);
  await settle(); await tick(30);
  const afterFail = { pcs: pcsOf(ctx), keys: activeKeys(ctx), reconnects: reconnectTimers(timers).length, changedToast: toasts.some((m) => m.includes("페어링이 바뀌었")),
                      err: !!els["p-err"].textContent, go: els["p-go"].textContent };
  // 같은 저장소로 다시 연다 — 멀쩡한 A 로 자동 접속하는가
  const ls = ctx.localStorage;
  const ctx2 = await load({ setup: (sb) => {
    for (let i = 0; i < ls.length; i++) { const k = ls.key(i); sb.localStorage.setItem(k, ls.getItem(k)); }
    regIds(sb); sb.fetch = cpFetch(sb);
    const s2 = browserWs(sb); sb.__s2 = s2;
  } });
  await settle(); await tick(30);
  const reopened = { sockets: ctx2.__s2.length, room: ctx2.__s2.length ? roomOf(ctx2.__s2[0]) : null };
  assert.deepEqual({ afterFail, reopened }, {
    afterFail: { pcs: [["roomA", null]], keys: { sm_room: "roomA", sm_cs: "csA", sm_cp: "cpA", sm_devtoken: "devA" }, reconnects: 0, changedToast: false, err: true, go: "연결" },
    reopened: { sockets: 1, room: "roomA" },
  });
});

test("T60c 저장된 지금 PC 의 자격을 CP 가 거부(401)하면 그 PC 만 ⛔ 로 적고 안내하며, 4401 로 끝없이 다시 붙지 않는다 — 다른 저장된 PC 는 그대로", async () => {
  let els;
  const ctx = await load({ setup: (sb) => {
    sb.localStorage.setItem("sm_pcs", JSON.stringify([PC_A, { room: "roomC", cs: "csC", cp: "cpC", dev: "devC", name: "C" }]));
    els = regIds(sb); sb.fetch = cpFetch(sb, ["cpA"]);
  } });
  const timers = holdTimers(ctx);
  const sockets = browserWs(ctx);
  ev(ctx, "globalThis.__toasts = []; toast = (m) => __toasts.push(String(m)); enterApp = () => {}; openModal = () => {}; loadList = async () => {};");
  const p = ev(ctx, "pcsActivate('roomA'); conn._stopped=false; conn.connect('roomA', '').catch((e) => 'rejected')");
  await until(() => sockets.length === 1);
  sockets[0].open(); sockets[0].drop(4401);
  await p; await settle();
  assert.deepEqual({ pcs: pcsOf(ctx), toast: ev(ctx, "__toasts.filter((m) => m.includes('페어링이 바뀌었')).length"), reconnects: reconnectTimers(timers).length },
                   { pcs: [["roomA", "changed"], ["roomC", null]], toast: 1, reconnects: 0 });
});

// ---------------------------------------------------------------- 결함 2·7·8·10: PC(커넥터)가 내려갔다 다시 붙을 때
// 새 커넥터는 이 연결의 기기 인증을 모른다(새 프로세스·_on_connected 가 비움). 다시 인증하지 않으면 첫 요청이 auth_required 를 받아
// 멀쩡한 PC 가 '⛔ 페어링 바뀜'으로 저장되고 새로고침해도 자동 접속하지 않았다. E2EE 면 PC 가 없는 동안의 요청이 영원히 기다렸다.
const rk = (seed) => { const raw = new Uint8Array(32); for (let i = 0; i < 32; i++) raw[i] = (i * 7 + seed) & 255;
  return Buffer.from(raw).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); };
const roomConn = (ctx, room) => ev(ctx, `(async () => { const c = new Conn(); c.e2eeKey = await crypto.subtle.importKey('raw',
  b64uToU8(LS.getItem('sm_e2ee_${room}')), 'AES-GCM', false, ['encrypt','decrypt']); return c; })()`);
// PC 가 없는 방의 릴레이처럼 답한다(relay.py): 폰이 보낸 프레임마다 agent_offline — 봉투(E2EE)는 본문을 못 읽어 id 가 null
function relayNoAgent(ws) {
  ws._done ||= 0;
  for (const f of ws.sent.slice(ws._done)) ws.push({ v: 1, type: "res", id: f.e === "a1" ? null : (f.id ?? null), ok: false, error: "agent_offline" });
  ws._done = ws.sent.length;
}
async function bootPc({ e2ee = false } = {}) {
  const ctx = await load({ setup: (sb) => {
    sb.localStorage.setItem("sm_room", "room1"); sb.localStorage.setItem("sm_devtoken", "dev-1");
    sb.localStorage.setItem("sm_pcs", JSON.stringify([{ room: "room1", dev: "dev-1", name: "PC1" }]));
    if (e2ee) sb.localStorage.setItem("sm_e2ee_room1", rk(3));
    regIds(sb);
  } });
  const timers = holdTimers(ctx);
  const sockets = browserWs(ctx);
  ev(ctx, `conn.devToken = 'dev-1'; innerWidth = 1400;
    globalThis.__toasts = []; toast = (m) => __toasts.push(String(m));
    globalThis.__lost = []; showAuthLost = (e) => { __lost.push(String(e)); };
    globalThis.__ll = 0; loadList = async () => { __ll++; }; globalThis.__lt = 0; listTick = async () => { __lt++; };
    globalThis.__rt = []; reattachForegroundTab = (h) => { __rt.push(h === undefined ? 'measure' : h); };
    refreshVer = async () => {}; showVer = () => {}; enterApp = () => {};`);
  const pc = e2ee ? await roomConn(ctx, "room1") : null;
  // 선에 실린 프레임(E2EE 면 열어서) · PC 가 보낸 것처럼 답하기(E2EE 면 봉인 — 겉에는 릴레이가 붙인 cid)
  const frames = async (ws) => { const out = []; for (const f of ws.sent) out.push(f.e === "a1" ? await pc._openEnv(f) : f); return out; };
  const reply = async (ws, m) => ws.push(e2ee ? { ...(await pc._seal(m)), cid: m.cid } : m);
  return { ctx, timers, sockets, frames, reply };
}
async function connectAuthed(env, cid = "c1") {
  const { ctx, sockets, frames, reply } = env;
  const n = sockets.length;
  const p = ev(ctx, "conn.connect('room1', 'tok')");
  await until(() => sockets.length > n);
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid });
  await until(() => ws.sent.length >= 1);
  await settle();
  const a = (await frames(ws)).find((f) => f.method === "auth");
  await reply(ws, { type: "res", id: a.id, cid, ok: true, data: { id: "d", name: "n", ver: "0.29.0", pv: 2 } });
  await p; await settle();
  return ws;
}

test("T61 PC 가 재기동·업데이트되면(agent_offline → agent_online) 다시 인증한다 — 대기 요청·스트림은 끝나고, PC 가 없는 동안엔 보내지 않으며, 돌아오면 목록·보던 터미널을 이어 주고 '⛔ 페어링 바뀜'이 되지 않는다(평문·E2EE)", async () => {
  for (const e2ee of [false, true]) {
    const env = await bootPc({ e2ee });
    const { ctx, timers, frames, reply } = env;
    const ws = await connectAuthed(env);
    // PC 가 내려가기 직전: 응답을 기다리는 요청 하나 + 관제 스트림 하나
    ev(ctx, "globalThis.__pend = 'pending'; conn.req('list_sessions', {}).then((r) => { __pend = r; });"
          + "globalThis.__mon = []; conn.startMonitor('x', 'x', (d, eof) => __mon.push(eof ? 'eof' : d));");
    await settle();
    ws.push({ type: "peer", event: "agent_offline" }); await settle();
    const nSent = ws.sent.length;
    ev(ctx, "globalThis.__off = 'pending'; conn.req('ping', {}).then((r) => { __off = r; }); globalThis.__t = [];"
          + "conn.startTerminal('s1', (d, eof) => __t.push(eof ? 'eof' : d), {});");
    await settle();
    const whileOff = { pend: ev(ctx, "__pend.error || __pend"), mon: ev(ctx, "__mon.slice()"), off: ev(ctx, "__off.error || __off"),
                       term: ev(ctx, "__t.slice()"), sentWhileOff: ws.sent.length - nSent, authed: ev(ctx, "conn.authed"), peer: ev(ctx, "conn.peer") };
    // PC 가 돌아옴 → 곧바로 다시 인증
    ws.push({ type: "peer", event: "agent_online" }); await settle();
    const re = (await frames(ws)).slice(nSent).filter((f) => f.method === "auth");
    if (re.length) await reply(ws, { type: "res", id: re[0].id, cid: "c1", ok: true, data: { id: "d", name: "n" } });
    await settle();
    for (const t of timers.filter((t) => t.ms <= 1500)) t.f();             // PC 가 준비될 틈(1.5초) 뒤 목록·터미널
    await settle();
    // 다시 인증보다 먼저 나간 요청이 auth_required 를 받으면 — 다시 인증하고 한 번 더 보낸다(조용히 성공)
    ev(ctx, "globalThis.__late = 'pending'; conn.req('list_sessions', { q: 'x' }).then((r) => { __late = r; });"); await settle();
    const before = ws.sent.length;
    const lr = (await frames(ws)).filter((f) => f.method === "list_sessions").at(-1);
    await reply(ws, { type: "res", id: lr.id, cid: "c1", ok: false, error: "auth_required" }); await settle();
    const ra = (await frames(ws)).slice(before).find((f) => f.method === "auth");
    if (ra) await reply(ws, { type: "res", id: ra.id, cid: "c1", ok: true, data: { id: "d", name: "n" } });
    await settle();
    const retry = (await frames(ws)).slice(before).find((f) => f.method === "list_sessions");
    if (retry) await reply(ws, { type: "res", id: retry.id, cid: "c1", ok: true, data: { sessions: ["ok"] } });
    await settle();
    const got = { whileOff, reauth: re.length, reauthToken: re[0] && re[0].params.token, authedAfter: ev(ctx, "conn.authed"),
                  back: { toast: ev(ctx, "__toasts.includes('PC 다시 연결됨')"), lists: ev(ctx, "__ll + __lt"), reattach: ev(ctx, "__rt.length") },
                  late: { reauth: !!ra, retried: !!retry, ok: ev(ctx, "__late.ok"), data: ev(ctx, "JSON.stringify(__late.data)") },
                  lost: ev(ctx, "__lost.slice()"), state: ev(ctx, "(pcsLoad()[0] || {}).state || null") };
    assert.deepEqual(plain(got), {
      whileOff: { pend: "agent_offline", mon: ["eof"], off: "agent_offline", term: ["eof"], sentWhileOff: 0, authed: false, peer: false },
      reauth: 1, reauthToken: "dev-1", authedAfter: true,
      back: { toast: true, lists: 1, reattach: 1 },
      late: { reauth: true, retried: true, ok: true, data: JSON.stringify({ sessions: ["ok"] }) },
      lost: [], state: null,
    }, e2ee ? "E2EE" : "평문");
  }
});

test("T61b PC 가 돌아와 다시 인증했는데 자격이 정말 무효(PC 에서 기기 삭제·재발급)면 지금처럼 '⛔ 페어링 바뀜'과 자격 상실 안내", async () => {
  const env = await bootPc();
  const { ctx, timers } = env;
  const ws = await connectAuthed(env);
  ws.push({ type: "peer", event: "agent_offline" }); ws.push({ type: "peer", event: "agent_online" }); await settle();
  const a = ws.sent.filter((f) => f.method === "auth").at(-1);
  ws.push({ type: "res", id: a.id, cid: "c1", ok: false, error: "auth_invalid" }); await settle();
  for (const t of timers.filter((t) => t.ms <= 1500)) t.f();
  await settle();
  assert.deepEqual(plain({ reauth: ws.sent.filter((f) => f.method === "auth").length, lost: ev(ctx, "__lost.slice()"), state: ev(ctx, "pcsLoad()[0].state || null"),
                     back: ev(ctx, "__toasts.includes('PC 다시 연결됨')"), reattach: ev(ctx, "__rt.length") }),
                   { reauth: 2, lost: ["auth_invalid"], state: "changed", back: false, reattach: 0 });
});

test("T62 E2EE: PC 가 없을 때 릴레이가 돌려주는 agent_offline(봉투라 id 가 없다)이 대기 요청을 끝낸다 · PC 가 꺼진 채 연결하면 인증을 보내지 않고 바로 끝난다", async () => {
  const env = await bootPc({ e2ee: true });
  const { ctx, sockets } = env;
  const ws = await connectAuthed(env);
  ev(ctx, "globalThis.__pend = 'pending'; conn.req('list_sessions', {}).then((r) => { __pend = r; });"); await settle();
  ws.push({ v: 1, type: "res", id: null, ok: false, error: "agent_offline" });   // 릴레이: 그사이 PC 가 떠나 이 봉투를 보낼 곳이 없다
  await settle();
  const pend = ev(ctx, "__pend.error || __pend"), peer = ev(ctx, "conn.peer");
  // PC 가 꺼진 채(peer_present:false) 새로 연결 — 인증을 보내지 않고 connect 가 바로 끝난다(전엔 '연결 중…'에 멈췄다)
  ev(ctx, "globalThis.__c = 'pending'; conn.connect('room1', 'tok').then((r) => { __c = r.auth; });");
  await until(() => sockets.length === 2);
  const ws2 = sockets[1]; ws2.open(); ws2.push({ type: "hello", peer_present: false, cid: "c2" });
  await settle();
  assert.deepEqual(plain({ pend, peer, connect: ev(ctx, "JSON.stringify(__c)"), sent: ws2.sent.length, authed: ev(ctx, "conn.authed") }),
                   { pend: "agent_offline", peer: false, connect: JSON.stringify({ ok: false, error: "agent_offline", data: null }), sent: 0, authed: false });
});

test("T62b 다시 연결됐는데 PC 가 꺼져 있으면 '다시 연결됨'이라 하지 않는다 — PC 가 꺼져 있다고 알리고 터미널은 PC 가 돌아올 때 붙인다(평문·E2EE)", async () => {
  for (const e2ee of [false, true]) {
    const env = await bootPc({ e2ee });
    const { ctx, timers, sockets } = env;
    await connectAuthed(env);
    sockets[0].drop(1006); await settle();                                 // 릴레이 재기동 — 폰이 먼저 다시 붙는다
    const rt = reconnectTimers(timers); if (rt.length !== 1) throw new Error("재접속 예약 없음");
    timers.splice(timers.indexOf(rt[0]), 1); rt[0].f();
    await until(() => sockets.length === 2);
    const ws2 = sockets[1]; ws2.open(); ws2.push({ type: "hello", peer_present: false, cid: "c2" });
    await settle(); relayNoAgent(ws2); await settle();                     // 보낸 것이 있으면 릴레이처럼 agent_offline
    assert.deepEqual(plain({ toasts: ev(ctx, "__toasts.slice()"), lists: ev(ctx, "__ll"), reattach: ev(ctx, "__rt.length"), sent: ws2.sent.length, lost: ev(ctx, "__lost.slice()") }),
                     { toasts: ["연결은 됐지만 PC 가 꺼져 있습니다 — 켜지면 자동으로 이어집니다"], lists: 1, reattach: 0, sent: 0, lost: [] }, e2ee ? "E2EE" : "평문");
  }
});

test("T62c PC 가 꺼진 채 앱을 열면 앱에 들어가 'PC 가 꺼져 있습니다'를 보이고, PC 가 켜지면 인증해 목록을 받는다 — '⛔ 페어링 바뀜'이 되지 않는다", async () => {
  let els;
  const ctx = await load({ setup: (sb) => {
    for (const [k, v] of [["sm_room", "room1"], ["sm_token", "tok"], ["sm_devtoken", "dev-1"], ["sm_pcs", JSON.stringify([{ room: "room1", dev: "dev-1", name: "PC1" }])]]) sb.localStorage.setItem(k, v);
    els = regIds(sb);
    sb.__all = browserWs(sb);
  } });
  const timers = holdTimers(ctx);
  ev(ctx, "globalThis.__lost = []; showAuthLost = (e) => { __lost.push(String(e)); }; refreshVer = async () => {}; showVer = () => {}; maybeShowIosHint = () => {}; refreshPushBtn = () => {};");
  const sockets = ctx.__all;
  await until(() => sockets.length === 1);
  const ws = sockets[0]; ws.open(); ws.push({ type: "hello", peer_present: false, cid: "c1" });
  await settle(); relayNoAgent(ws); await settle(); await tick(30);         // 보낸 것이 있으면 릴레이처럼 agent_offline
  const LF = "typeof LIST_FAIL === 'undefined' ? 'none' : LIST_FAIL";
  const offline = { entered: !els.app.classList.contains("hidden") && els.pair.classList.contains("hidden"),
                    listFail: ev(ctx, LF), sent: ws.sent.length };
  ws.push({ type: "peer", event: "agent_online" }); await settle();
  const a = ws.sent.slice(ws._done).find((f) => f.method === "auth");
  if (a) ws.push({ type: "res", id: a.id, cid: "c1", ok: true, data: { id: "d", name: "n" } });
  await settle();
  for (const t of timers.filter((t) => t.ms <= 1500)) t.f();
  await settle();
  const lr = ws.sent.slice(ws._done).find((f) => f.method === "list_sessions");
  if (lr) ws.push({ type: "res", id: lr.id, cid: "c1", ok: true, data: { sessions: [{ session_id: "s1", title: "S1", cwd: "E:/p" }] } });
  await settle();
  assert.deepEqual(plain({ offline, auth: !!a, listed: !!lr, sessions: ev(ctx, "SESSIONS.length"), listFail: ev(ctx, LF), lost: ev(ctx, "__lost.slice()"),
                     state: ev(ctx, "pcsLoad()[0].state || null") }),
                   { offline: { entered: true, listFail: true, sent: 0 }, auth: true, listed: true, sessions: 1, listFail: false, lost: [], state: null });
});

test("T62d 예전 결함이 남긴 ⛔(멀쩡한 PC 를 '페어링 바뀜'으로 적음)는 그 PC 에 인증이 되면 지운다", async () => {
  const env = await bootPc();
  const { ctx } = env;
  ev(ctx, "pcMarkState('room1', 'changed')");
  await connectAuthed(env);
  assert.equal(ev(ctx, "pcsLoad()[0].state || null"), null);
});

// ---------------------------------------------------------------- 결함 5: 저장된 자격이 PC 에서 무효(auth_invalid)일 때 '연결 중…'에 멈춤
// 자격 상실 안내(onauth → 로그아웃)가 connect 보다 먼저 돌아 기다리던 connect 가 '밀려남'으로 거부됐고, 부른 쪽은 조용히 그만둬
// 페어링 화면 버튼이 '연결 중…' 그대로·오류 문구 없음·저장된 PC 목록도 ⛔ 전 상태였다.
const flatButtons = (host) => flatEl(host).filter((n) => n.tagName === "BUTTON").map((n) => n.textContent);
test("T63 저장된 자격으로 부팅했는데 PC 에서 이 기기를 해제·재발급했으면(auth_invalid) 안내를 띄우고 페어링 화면을 맞춘다 — '연결 중…'에 멈추지 않고 그 PC 는 ⛔ 줄", async () => {
  let els;
  const ctx = await load({ setup: (sb) => {
    for (const [k, v] of [["sm_room", "roomA"], ["sm_cs", "csA"], ["sm_cp", "cpA"], ["sm_devtoken", "devA"], ["sm_pcs", JSON.stringify([PC_A])]]) sb.localStorage.setItem(k, v);
    els = regIds(sb); sb.fetch = cpFetch(sb);
    sb.__all = browserWs(sb);
  } });
  ev(ctx, "globalThis.__entered = 0; enterApp = () => { __entered++; }; globalThis.__modals = []; openModal = (t) => { __modals.push(String(t)); };");
  const sockets = ctx.__all;
  await until(() => sockets.length === 1);
  await helloAuth(sockets[0], "c1", { ok: false, error: "auth_invalid" });
  await settle(); await tick(30);
  assert.deepEqual(plain({ go: els["p-go"].textContent, err: els["p-err"].textContent.includes("페어링이 바뀌었"), entered: ev(ctx, "__entered"),
                           modals: ev(ctx, "__modals.slice()"), state: ev(ctx, "pcsLoad()[0].state || null"), rows: flatButtons(els["p-pcs"]), stopped: ev(ctx, "!!conn._stopped") }),
                   { go: "연결", err: true, entered: 0, modals: ["🔒 접속 자격 해제됨"], state: "changed", rows: ["다시 페어링", "지우기"], stopped: true });
});

test("T63b 페어링 화면의 저장된 PC·머리 줄 PC 고르기도 auth_invalid 면 '연결 중…'·'연결됨'으로 끝나지 않는다 — 앱에 들어가지 않고 ⛔ 로 다시 그린다", async () => {
  const run = async (how) => {
    let els;
    const ctx = await load({ setup: (sb) => {
      sb.localStorage.setItem("sm_pcs", JSON.stringify([PC_A, { room: "roomB", cs: "csB", cp: "cpB2", dev: "devB", name: "B" }]));
      sb.localStorage.setItem("sm_room", "roomB");
      els = regIds(sb); sb.fetch = cpFetch(sb, []);
    } });
    const sockets = browserWs(ctx);
    ev(ctx, "globalThis.__entered = 0; enterApp = () => { __entered++; }; openModal = () => {}; globalThis.__toasts = []; toast = (m) => __toasts.push(String(m));");
    if (how === "저장된 PC") {
      ctx.renderPairPcs();
      const btn = flatEl(els["p-pcs"]).find((n) => n.tagName === "BUTTON" && n.textContent === "🖥 A");
      btn.onclick();
    } else ev(ctx, "switchToPc(pcsLoad().find((p) => p.room === 'roomA'))");
    await until(() => sockets.length === 1);
    await helloAuth(sockets[0], "c1", { ok: false, error: "auth_invalid" });
    await settle(); await tick(30);
    return { entered: ev(ctx, "__entered"), stateA: ev(ctx, "pcsLoad().find((p) => p.room === 'roomA').state || null"),
             connectedToast: ev(ctx, "__toasts.some((m) => m.endsWith('연결됨'))"),
             ...(how === "저장된 PC" ? { err: els["p-err"].textContent.includes("페어링이 바뀌었"), stuck: flatButtons(els["p-pcs"]).some((t) => t.startsWith("연결 중")),
                                        rowsA: flatButtons(els["p-pcs"]).filter((t) => t === "다시 페어링").length } : {}) };
  };
  assert.deepEqual(plain({ saved: await run("저장된 PC"), header: await run("머리 줄") }), {
    saved: { entered: 0, stateA: "changed", connectedToast: false, err: true, stuck: false, rowsA: 1 },
    header: { entered: 0, stateA: "changed", connectedToast: false },
  });
});

// ---------------------------------------------------------------- 결함 4: 재접속이 4403·4426 으로 거부돼도 끝없이 다시 붙음
test("T64 끊긴 뒤 재접속이 4426(앱/커넥터 버전 낮음)·4403(room 무효)으로 거부되면 안내 한 번으로 멈춘다 — 보통 끊김(1006)은 지금처럼 다음 백오프 하나만", async () => {
  const run = async (code) => {
    const ctx = await load({ setup: (sb) => { regIds(sb); } });
    const timers = holdTimers(ctx);
    const sockets = browserWs(ctx);
    ev(ctx, "globalThis.__toasts = []; toast = (m) => __toasts.push(String(m)); loadList = async () => {}; reattachForegroundTab = () => {}; refreshVer = async () => {};");
    const p = ev(ctx, "conn._stopped = false; conn.connect('roomA', 'tok')");
    await until(() => sockets.length === 1);
    sockets[0].open(); sockets[0].push({ type: "hello", peer_present: true, cid: "c1" }); await p; await settle();
    sockets[0].drop(1006); await settle();                              // 망이 잠깐 끊김 → 재접속 예약
    const rounds = [];
    for (let i = 0; i < 3; i++) {
      const rt = reconnectTimers(timers); if (!rt.length) break;
      timers.splice(timers.indexOf(rt[0]), 1); rt[0].f();
      await until(() => sockets.length === 2 + i);
      const ws = sockets.at(-1); ws.open(); ws.drop(code);              // 재기동한 릴레이가 이 판을 거부
      await settle();
      rounds.push(reconnectTimers(timers).length);
    }
    return { rounds, sockets: sockets.length, notices: ev(ctx, "__toasts.filter((m) => !/다시 연결됨/.test(m)).length") };
  };
  assert.deepEqual(plain({ v4426: await run(4426), v4403: await run(4403), v1006: await run(1006) }), {
    v4426: { rounds: [0], sockets: 2, notices: 1 },
    v4403: { rounds: [0], sockets: 2, notices: 1 },
    v1006: { rounds: [1, 1, 1], sockets: 4, notices: 0 },
  });
});

// ---------------------------------------------------------------- 결함 3·6: 자동 '보던 터미널 다시 붙이기'가 사용자의 누름을 밀어냄
// 자동 다시 붙이기(재접속 성공·화면 복귀·PC 재기동 뒤)도 loadTerminal 의 누름 순번(navTicket)을 올려, 끊긴 사이·재연결 순간에 누른 다른
// 세션이 2차 인증·목록을 기다린 뒤 조용히 버려졌다. 2차 인증 유예가 끝났으면 예상 못 한 두 번째 코드 창까지 떴다.
const SA = "aaaaaaaa-1111-2222-3333-444444444444";
const SB = "bbbbbbbb-1111-2222-3333-444444444444";
async function bootTerm() {
  const pd = stubEl("div"), app = stubEl("div");
  const ctx = await load({ setup: (sb) => {
    sb.localStorage.setItem("sm_devtoken", "devX"); sb.localStorage.setItem("sm_room", "room1");
    for (const [id, e] of [["paneDetail", pd], ["app", app], ["head", stubEl("span")], ["paneList", stubEl("div")], ["p-err", stubEl("div")], ["p-go", stubEl("button")]]) sb.document._register(id, e);
  } });
  app.setAttribute("data-view", "list");
  const timers = holdTimers(ctx);
  const sockets = browserWs(ctx);
  ev(ctx, `innerWidth = 1400; conn.devToken = 'devX'; globalThis.__toasts = []; toast = (m) => __toasts.push(String(m));
    fetchRecentMessages = async () => []; globalThis.MutationObserver = class { observe() {} disconnect() {} };
    loadList = async () => {}; refreshVer = async () => {}; showVer = () => {};
    globalThis.__prompts = 0; globalThis.__codes = []; prompt = () => { __prompts++; return __codes.shift() ?? null; };
    SESSIONS = [{ session_id: '${SA}', title: 'A', live_terminal: true, cwd: 'E:/p' }, { session_id: '${SB}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  ctx.confirm = () => true;
  const pending = (ws, pred) => ws.sent.filter((f) => f.type === "req" && !(ws._ans ||= new Set()).has(f.id) && pred(f));
  const answer = async (ws, pred, data, ok = true) => { for (const f of pending(ws, pred)) { ws._ans.add(f.id); ws.push({ type: "res", id: f.id, cid: ws._cid, ok, data }); await settle(); return f; } return null; };
  const isStatus = (f) => f.method === "api" && f.params.path === "/api/owner/2fa/status";
  return { ctx, timers, sockets, pending, answer, isStatus };
}
// 연결 → 터미널 A(2차 인증 유효) → 망 끊김(1006) → 재접속 소켓의 hello 까지(기기 인증 응답 전 — 상태 점은 이미 켜졌다)
async function dropAndHello(env, { required }) {
  const { ctx, timers, sockets, answer, isStatus } = env;
  const p = ev(ctx, "conn.connect('room1', 'tok')");
  await until(() => sockets.length === 1);
  const ws1 = sockets[0]; ws1._cid = "c1"; ws1.open(); ws1.push({ type: "hello", peer_present: true, cid: "c1" });
  await until(() => ws1.sent.some((f) => f.method === "auth")); await answer(ws1, (f) => f.method === "auth", { id: "d", name: "n" }); await p;
  if (required) ev(ctx, "LS.setItem('sm_grace', JSON.stringify({ grace: 'g0', exp: Math.floor(Date.now()/1000) + 3600 }))");
  const pt = ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false);
  await until(() => ws1.sent.some(isStatus)); await answer(ws1, isStatus, { status: 200, json: { required } }); await pt; await tick(350);
  const firstTerm = ws1.sent.filter((f) => f.method === "terminal").map((f) => f.params.session_id.slice(0, 1));
  if (required) ev(ctx, "LS.removeItem('sm_grace')");                     // 몇 시간 뒤 — 유예가 끝났다
  ws1.drop(1006); await settle();
  const disc = ev(ctx, "termView.disconnected");
  const rt = reconnectTimers(timers); timers.splice(timers.indexOf(rt[0]), 1); rt[0].f();
  await until(() => sockets.length === 2);
  const ws2 = sockets[1]; ws2._cid = "c2"; ws2.open(); ws2.push({ type: "hello", peer_present: true, cid: "c2" });
  await until(() => ws2.sent.some((f) => f.method === "auth"));
  return { ws2, firstTerm, disc };
}
test("T65 재연결되는 순간(상태 점은 켜졌고 기기 인증 응답 전) 다른 세션 줄을 누르면 그 누름이 화면을 정한다 — 재접속 흐름의 자동 다시 붙이기가 그 누름을 버리지 않는다", async () => {
  const env = await bootTerm();
  const { ctx, answer, isStatus } = env;
  const { ws2, firstTerm, disc } = await dropAndHello(env, { required: false });
  const pj = ctx.jumpToSession(ev(ctx, "SESSIONS[1]"));                     // 그 순간 세션 줄 B
  await settle();
  await answer(ws2, (f) => f.method === "auth", { id: "d", name: "n" });   // 재접속 완료 → 목록·자동 다시 붙이기
  await settle(); await tick(20);
  for (let i = 0; i < 3; i++) await answer(ws2, isStatus, { status: 200, json: { required: false } });   // 2차 인증 상태(먼저 보낸 B 의 것부터)
  await pj; await tick(400);
  assert.deepEqual(plain({ firstTerm, disc, terminalsOnWs2: ws2.sent.filter((f) => f.method === "terminal").map((f) => f.params.session_id.slice(0, 1)),
                           active: (ev(ctx, "(findTab(TABS.active)||{s:{}}).s.session_id") || "").slice(0, 1), now: ev(ctx, "PANE_NOW.kind + ':' + (PANE_NOW.sid||'').slice(0,1)") }),
                   { firstTerm: ["a"], disc: true, terminalsOnWs2: ["b"], active: "b", now: "terminal:b" });
});

test("T65b 2차 인증 유예가 끝난 채 재연결되는 순간 다른 터미널(B)을 눌러 코드를 넣으면 B 가 열린다 — 자동 다시 붙이기가 두 번째 코드 창을 띄우거나 B 를 밀어내지 않는다", async () => {
  const env = await bootTerm();
  const { ctx, answer, isStatus } = env;
  const { ws2 } = await dropAndHello(env, { required: true });
  ev(ctx, "__codes = ['123456', null]");                                   // 사용자는 B 를 위해 코드를 넣는다(두 번째 창이 뜨면 취소)
  const pj = ctx.jumpToSession(ev(ctx, "SESSIONS[1]"));
  await settle();
  await answer(ws2, (f) => f.method === "auth", { id: "d", name: "n" });
  await settle(); await tick(20);
  for (let i = 0; i < 3; i++) await answer(ws2, isStatus, { status: 200, json: { required: true } });
  await answer(ws2, (f) => f.method === "grant", { grace: "g1", exp: Math.floor(Date.now() / 1000) + 3600 });
  await answer(ws2, (f) => f.method === "grant", { grace: "g2", exp: Math.floor(Date.now() / 1000) + 3600 });
  await pj; await tick(400);
  assert.deepEqual(plain({ prompts: ev(ctx, "__prompts"), terminalsOnWs2: ws2.sent.filter((f) => f.method === "terminal").map((f) => f.params.session_id.slice(0, 1)),
                           now: ev(ctx, "PANE_NOW.kind + ':' + (PANE_NOW.sid||'').slice(0,1)"), disconnected: ev(ctx, "termView.disconnected") }),
                   { prompts: 1, terminalsOnWs2: ["b"], now: "terminal:b", disconnected: false });
});

test("T65c 아무것도 누르지 않았으면 재연결 뒤 보던 터미널을 지금처럼 다시 붙이고, 다시 붙이는 사이(2차 인증 확인 중) 누른 다른 세션이 이긴다", async () => {
  const run = async (press) => {
    const env = await bootTerm();
    const { ctx, answer, isStatus } = env;
    const { ws2 } = await dropAndHello(env, { required: false });
    await answer(ws2, (f) => f.method === "auth", { id: "d", name: "n" });   // 재접속 완료 → 자동 다시 붙이기(A)가 2차 인증 상태를 묻는다
    await settle(); await tick(20);
    let pj = null;
    if (press) { pj = ctx.jumpToSession(ev(ctx, "SESSIONS[1]")); await settle(); }   // 그 사이 세션 줄 B
    for (let i = 0; i < 3; i++) await answer(ws2, isStatus, { status: 200, json: { required: false } });
    if (pj) await pj; await tick(400);
    return { terminalsOnWs2: ws2.sent.filter((f) => f.method === "terminal").map((f) => f.params.session_id.slice(0, 1)),
             now: ev(ctx, "PANE_NOW.kind + ':' + (PANE_NOW.sid||'').slice(0,1)") };
  };
  assert.deepEqual(plain({ idle: await run(false), pressed: await run(true) }),
                   { idle: { terminalsOnWs2: ["a"], now: "terminal:a" }, pressed: { terminalsOnWs2: ["b"], now: "terminal:b" } });
});

// ---------------------------------------------------------------- 검토 4회차
// 결함 D4: E2EE 에서 PC 가 응답을 보낸 직후 릴레이에서 떠나면(응답 봉투 → agent_offline 순서로 도착) — 봉투는 _rxq 에서 비동기로 복호되는데
// peer 통지는 onmessage 에서 바로 처리되어 _agentGone 이 먼저 돌았다. 이미 도착한 성공 응답이 agent_offline 실패로 바뀌고(PC 는 처리했는데
// 화면은 실패), 부트 해시 페어링(앱 QR 스캔)은 멀쩡한 QR 을 되돌렸다(pcs 비움·자격 삭제). main 은 agent_offline 에 대기를 끝내지 않아 그대로 전달됐다.
test("T66 E2EE: 응답 봉투 바로 뒤에 온 agent_offline 이 그 응답을 실패로 바꾸지 않는다 — 받은 순서대로(요청·부트 해시 페어링)", async () => {
  // ① 일반 요청('⏹ 세션 종료' 같은 POST) — PC 는 처리하고 답한 뒤 바로 떠났다
  const env = await bootPc({ e2ee: true });
  const { ctx, reply } = env;
  const ws = await connectAuthed(env);
  ev(ctx, "globalThis.__r = 'pending'; conn.req('api', { verb: 'POST', path: '/api/sessions/s1/terminal/stop' }).then((r) => { __r = r; });");
  await settle();
  const q = (await env.frames(ws)).at(-1);
  await reply(ws, { type: "res", id: q.id, cid: "c1", ok: true, data: { status: 200, json: { stopped: true } } });   // 봉인 → 복호는 비동기
  ws.push({ type: "peer", event: "agent_offline" });
  await settle(); await tick(30);
  const req = plain(ev(ctx, "__r"));
  // ② 부트 해시 페어링 — PC 가 새 QR 의 기기 인증을 받아들인 직후 커넥터가 한 번 끊겼다 다시 붙는다
  let els;
  const ctx2 = await load({ hash: "#room=roomB&token=tokB&dev=devB&rk=" + rk(5), setup: (sb) => { els = regIds(sb); sb.__all = browserWs(sb); } });
  holdTimers(ctx2);
  ev(ctx2, "globalThis.__entered = 0; enterApp = () => { __entered++; }; openModal = () => {}; loadList = async () => {}; refreshVer = async () => {}; showVer = () => {};");
  const pcB = await roomConn(ctx2, "roomB");
  const sockets = ctx2.__all;
  await until(() => sockets.length === 1);
  const wsB = sockets[0]; wsB.open(); wsB.push({ type: "hello", peer_present: true, cid: "c1" });
  await until(() => wsB.sent.length >= 1); await settle();
  const a = await pcB._openEnv(wsB.sent[0]);
  wsB.push({ ...(await pcB._seal({ type: "res", id: a.id, cid: "c1", ok: true, data: { id: "dB", name: "B", hostname: "PC-B", pv: 2 } })), cid: "c1" });
  wsB.push({ type: "peer", event: "agent_offline" });
  await settle(); await tick(30);
  wsB.push({ type: "peer", event: "agent_online" });
  await settle(); await tick(30);
  const pair = { entered: ev(ctx2, "__entered"), pcs: JSON.parse(ctx2.localStorage.getItem("sm_pcs") || "[]").map((x) => x.room),
                 dev: ctx2.localStorage.getItem("sm_devtoken"), err: els["p-err"].textContent, current: ev(ctx2, "conn.ws") === wsB };
  assert.deepEqual({ req: { ok: req.ok, json: req.data && req.data.json }, pair },
                   { req: { ok: true, json: { stopped: true } }, pair: { entered: 1, pcs: ["roomB"], dev: "devB", err: "", current: true } });
});

// 결함 D2: 폰(좁은 화면)에서 터미널을 보던 중 폰 연결이 끊기고(릴레이 재기동·망 끊김) 다시 붙었는데 PC(커넥터)가 아직 없으면
// (peer_present:false — 릴레이 재기동 때 폰이 먼저 붙는 흔한 경우), 재접속 흐름이 목록 칸을 그리며(loadList → showListPane) 미리 잰 '보던 터미널이
// 보였나'를 버렸다. PC 가 붙으면 pcBack 이 그때 다시 재는데 이미 목록 칸이라 '숨겨 둔 터미널'로 보고 다시 붙이지 않았다 —
// '켜지면 자동으로 이어집니다' 안내와 달리 폰만 목록에 남았다(PC 폭은 두 칸이 함께 보여 괜찮았다).
async function bootPhoneTerm(width) {
  const pd = stubEl("div"), app = stubEl("div");
  const ctx = await load({ setup: (sb) => {
    sb.localStorage.setItem("sm_devtoken", "devX"); sb.localStorage.setItem("sm_room", "room1");
    for (const [id, e] of [["paneDetail", pd], ["app", app], ["head", stubEl("span")], ["paneList", stubEl("div")], ["p-err", stubEl("div")], ["p-go", stubEl("button")]]) sb.document._register(id, e);
  } });
  app.setAttribute("data-view", "list");
  const timers = holdTimers(ctx);
  const sockets = browserWs(ctx);
  ev(ctx, `innerWidth = ${width}; conn.devToken = 'devX'; globalThis.__toasts = []; toast = (m) => __toasts.push(String(m));
    fetchRecentMessages = async () => []; globalThis.MutationObserver = class { observe() {} disconnect() {} };
    refreshVer = async () => {}; showVer = () => {};
    T.list = async () => { if (!conn.peer) throw new Error('agent_offline'); return { sessions: SESSIONS.slice() }; };
    SESSIONS = [{ session_id: '${SA}', title: 'A', live_terminal: true, cwd: 'E:/p' }, { session_id: '${SB}', title: 'B', live_terminal: true, cwd: 'E:/q' }];`);
  ctx.confirm = () => true;
  const pending = (ws, pred) => ws.sent.filter((f) => f.type === "req" && !(ws._ans ||= new Set()).has(f.id) && pred(f));
  const answer = async (ws, pred, data, ok = true) => { let n = 0; for (const f of pending(ws, pred)) { ws._ans.add(f.id); ws.push({ type: "res", id: f.id, cid: ws._cid, ok, data }); n++; await settle(); } return n; };
  const isStatus = (f) => f.method === "api" && f.params.path === "/api/owner/2fa/status";
  return { ctx, timers, sockets, answer, isStatus, app };
}
async function phonePcLate(width, { hideFirst = false, pcPresent = false } = {}) {
  const { ctx, timers, sockets, answer, isStatus, app } = await bootPhoneTerm(width);
  const p = ev(ctx, "conn.connect('room1', 'tok')");
  await until(() => sockets.length === 1);
  const ws1 = sockets[0]; ws1._cid = "c1"; ws1.open(); ws1.push({ type: "hello", peer_present: true, cid: "c1" });
  await until(() => ws1.sent.some((f) => f.method === "auth")); await answer(ws1, (f) => f.method === "auth", { id: "d", name: "n" }); await p;
  ev(ctx, "T = relayT; enterApp();"); await settle();
  const pt = ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false);
  await until(() => ws1.sent.some(isStatus)); await answer(ws1, isStatus, { status: 200, json: { required: false } }); await pt; await tick(350);
  if (hideFirst) ev(ctx, "showListPane()");                                // 폰 '‹ 목록'으로 터미널을 숨겨 두었다
  const before = app.getAttribute("data-view");
  ws1.drop(1006); await settle();                                          // 릴레이 재기동 — 폰이 먼저 다시 붙는다
  const rt = reconnectTimers(timers); timers.splice(timers.indexOf(rt[0]), 1); rt[0].f();
  await until(() => sockets.length === 2);
  const ws2 = sockets[1]; ws2._cid = "c2"; ws2.open();
  if (pcPresent) {
    ws2.push({ type: "hello", peer_present: true, cid: "c2" });
    await until(() => ws2.sent.some((f) => f.method === "auth")); await answer(ws2, (f) => f.method === "auth", { id: "d", name: "n" });
  } else {
    ws2.push({ type: "hello", peer_present: false, cid: "c2" });          // PC(커넥터)는 아직 다시 붙지 않았다
    await settle(); await tick(30);
    ws2.push({ type: "peer", event: "agent_online" }); await settle();     // PC 가 붙는다 → 다시 인증 → 1.5초 뒤 pcBack·다시 붙이기
    await until(() => ws2.sent.some((f) => f.method === "auth")); await answer(ws2, (f) => f.method === "auth", { id: "d", name: "n" });
    for (const t of timers.filter((t) => t.ms <= 1500 && !String(t.f).includes("_retryTimer"))) { timers.splice(timers.indexOf(t), 1); t.f(); }
  }
  await settle(); await tick(30);
  for (let i = 0; i < 3; i++) await answer(ws2, isStatus, { status: 200, json: { required: false } });
  await tick(400);
  return { before, view: app.getAttribute("data-view"), terminalReqs: ws2.sent.filter((f) => f.method === "terminal").map((f) => f.params.session_id.slice(0, 1)),
           disconnected: ev(ctx, "termView.disconnected") };
}
test("T67 폰에서 터미널을 보던 중 다시 붙었는데 PC 가 아직 없으면, PC 가 돌아올 때 보던 터미널을 다시 붙인다 — 숨겨 둔 터미널은 그대로 숨김", async () => {
  assert.deepEqual(plain({ phone: await phonePcLate(390), wide: await phonePcLate(1400), hidden: await phonePcLate(390, { hideFirst: true }),
                           pcPresent: await phonePcLate(390, { pcPresent: true }) }), {
    phone: { before: "detail", view: "detail", terminalReqs: ["a"], disconnected: false },
    wide: { before: "detail", view: "detail", terminalReqs: ["a"], disconnected: false },
    hidden: { before: "list", view: "list", terminalReqs: [], disconnected: true },
    pcPresent: { before: "detail", view: "detail", terminalReqs: ["a"], disconnected: false },
  });
});

// 결함 D1(기존, e1981d3 이 연결 실패로 넓힘): PC A 를 쓰는 중 저장돼 있지만 지금 쓰지 않는 PC B 의 새 QR 을 앱 QR 스캔·링크 열기(부트 해시)로
// 넣었는데 실패하면 hashPairRollback 이 B(옛 자격)를 활성으로 만들었다 — 다음 실행에 B 로 자동 접속하고, B 가 ⛔ 였으면 자동 접속이 아예 없었다.
// E-D16 '잘못된·만료된 QR 은 아무것도 바꾸지 않는다': B 의 옛 기록은 되살리되 활성 PC 는 이 해시 전에 쓰던 A 그대로.
async function hashRescan(how, { bState = null, boot = "roomA", hashRoom = "roomB" } = {}) {
  const PC_B = { room: "roomB", cs: "csB1", cp: "cpB1", dev: "devB1", name: "B", ...(bState ? { state: bState } : {}) };
  let els;
  const ctx = await load({ hash: `#room=${hashRoom}&cs=csN&cp=cpN&dev=devN`, setup: (sb) => {
    const act = boot === "roomA" ? PC_A : boot === "roomB" ? PC_B : null;
    if (act) for (const [k, v] of [["sm_room", act.room], ["sm_cs", act.cs], ["sm_cp", act.cp], ["sm_devtoken", act.dev]]) sb.localStorage.setItem(k, v);
    sb.localStorage.setItem("sm_pcs", JSON.stringify([PC_A, PC_B]));
    els = regIds(sb); sb.fetch = cpFetch(sb, how === "4401" ? ["cpN"] : []);
    sb.__all = browserWs(sb);
  } });
  const timers = holdTimers(ctx);
  ev(ctx, "enterApp = () => {}; openModal = () => {}; loadList = async () => {};");
  const sockets = ctx.__all;
  await until(() => sockets.length === 1);
  const ws = sockets[0];
  if (how === "4401") { ws.open(); ws.drop(4401); }
  else if (how === "1006") ws.drop(1006);                                 // 망이 잠깐 끊겨 연결 자체가 실패
  else await helloAuth(ws, "c1", { ok: false, error: "auth_invalid" });
  await settle(); await tick(30);
  const after = { tried: roomOf(ws), keys: activeKeys(ctx), pcs: JSON.parse(ctx.localStorage.getItem("sm_pcs")).map((x) => [x.room, x.cp, x.state || null]),
                  reconnects: reconnectTimers(timers).length, err: !!els["p-err"].textContent };
  const ls = ctx.localStorage;
  const ctx2 = await load({ setup: (sb) => {
    for (let i = 0; i < ls.length; i++) { const k = ls.key(i); sb.localStorage.setItem(k, ls.getItem(k)); }
    regIds(sb); sb.fetch = cpFetch(sb, []); sb.__s2 = browserWs(sb);
  } });
  await settle(); await tick(30);
  const s2 = ctx2.__s2;
  return { after, reopened: s2.length ? [roomOf(s2[0]), new URL(s2[0].url).searchParams.get("jwt")] : null };
}
test("T68 저장된 다른 PC(B)의 새 QR 이 실패하면(auth_invalid·CP 401·일시 단절) B 의 옛 기록만 되살리고 지금 쓰던 PC(A)가 활성으로 남는다 — 다시 열면 A 로 자동 접속", async () => {
  const A_KEYS = { sm_room: "roomA", sm_cs: "csA", sm_cp: "cpA", sm_devtoken: "devA" };
  const want = (bState = null) => ({ after: { tried: "roomB", keys: A_KEYS, pcs: [["roomA", "cpA", null], ["roomB", "cpB1", bState]], reconnects: 0, err: true },
                                      reopened: ["roomA", "jwt-cpA"] });
  assert.deepEqual(plain({ authInvalid: await hashRescan("auth_invalid"), cp401: await hashRescan("4401"), blip: await hashRescan("1006"),
                           blipBWasChanged: await hashRescan("1006", { bState: "changed" }) }),
                   { authInvalid: want(), cp401: want(), blip: want(), blipBWasChanged: want("changed") });
  // 회귀 가드: 지금 쓰는 PC(B) 자신의 새 QR 이 실패하면 지금처럼 B 의 옛 자격으로 남는다
  assert.deepEqual(plain(await hashRescan("auth_invalid", { boot: "roomB" })), {
    after: { tried: "roomB", keys: { sm_room: "roomB", sm_cs: "csB1", sm_cp: "cpB1", sm_devtoken: "devB1" },
             pcs: [["roomA", "cpA", null], ["roomB", "cpB1", null]], reconnects: 0, err: true },
    reopened: ["roomB", "jwt-cpB1"] });
});
