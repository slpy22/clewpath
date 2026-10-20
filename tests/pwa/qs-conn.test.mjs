// 바로가기(quick-switch) 검토 3회차 — 폰 연결 안정성(T60~). 실제 Conn·페어링·재접속 흐름을 브라우저 규칙 WebSocket 대역으로 돌린다.
// 재접속 백오프 타이머(1초 이상)는 붙잡아 두고 손으로 돌린다 — 실제 시간을 기다리지 않고 '다시 걸렸는가'를 센다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const settle = async (n = 12) => { for (let i = 0; i < n; i++) await tick(); };
const until = async (f, n = 150) => { for (let i = 0; i < n && !f(); i++) await tick(20); return f(); };
const flatEl = (n) => [n, ...(n.children || []).flatMap(flatEl)];

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
