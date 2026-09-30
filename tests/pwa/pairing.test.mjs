// 페어링 UX 0.10.0 (eng E-D16, B-1): connect() 가 auth 결과를 싣고, 페어링 경로는 auth.ok 일 때만 저장·진입한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const tick = () => new Promise((r) => setTimeout(r, 0));

function fakeWs(ctx) {
  const all = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; all.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    close(code) { this.readyState = 3; this.onclose && this.onclose({ code: code || 1000 }); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    push(m) { this.onmessage({ data: JSON.stringify(m) }); }
  };
  return all;
}
const STUBS = "globalThis.__entered = 0; enterApp = () => { globalThis.__entered++; }; refreshPcsBtn = () => {};"
  + "clientJwt = async () => null; toast = () => {}; showVer = () => {}; refreshVer = async () => {}; refreshPcSelect = () => {}; setTitle = () => {};";

function pairInputs(ctx, link) {
  const li = stubEl("input"); li.value = link; ctx.document._register("p-link", li);
  const err = stubEl("div"); err.textContent = ""; ctx.document._register("p-err", err);
  const go = stubEl("button"); go.textContent = "연결"; ctx.document._register("p-go", go);
  return { err, go };
}
const LINK = "https://relay.test/relay/app#room=rmA&cs=cli_1&cp=cpub_1&dev=dev-A&rk=AAAA";

test("Conn.connect: devToken 없으면 auth:null, 있으면 auth:{ok,error,data} 를 싣고 name 을 보고한다", async () => {
  const ctx = await load();
  ev(ctx, STUBS + "conn.onauth = null;");   // 전역 onauth(자격 상실 화면)는 여기서 검증 대상이 아니다
  const sockets = fakeWs(ctx);
  let p = ev(ctx, "conn.connect('room1', 'tok')"); await tick();
  let ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c1" });
  let r = await p; assert.equal(r.auth, null); assert.equal(r.type, "hello");
  ev(ctx, "conn._stopped = true"); ws.close();

  ev(ctx, "conn.devToken = 'dev-1'; conn._stopped = false");
  p = ev(ctx, "conn.connect('room1', 'tok')"); await tick();
  ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c2" }); await tick();
  const af = ws.sent.at(-1);
  assert.equal(af.method, "auth"); assert.equal(af.params.token, "dev-1");
  assert.equal(typeof af.params.name, "string"); assert.ok(af.params.name.length > 0, "폰이 자기 이름을 보고한다");
  ws.push({ type: "res", id: af.id, ok: false, error: "auth_invalid" });
  r = await p;
  same(r.auth, { ok: false, error: "auth_invalid", data: null }, "인증 실패해도 reject 가 아니라 auth 필드로");
  ev(ctx, "conn._stopped = true"); ws.close();
});

test("deviceSelfName: 브리지 deviceName 우선, 없으면 UA 요약", async () => {
  const ctx = await load({ navigator: { userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1" } });
  assert.equal(ev(ctx, "deviceSelfName()"), "iPhone Safari");
  ev(ctx, "window.ClewBridge = { deviceName: () => 'iPhone 15 Pro' }");
  assert.equal(ev(ctx, "deviceSelfName()"), "iPhone 15 Pro");
});

test("doPair: 새 자격의 auth 가 실패하면 아무것도 저장하지 않고 이전 PC 자격을 되살린다(E-D16)", async () => {
  const ctx = await load();
  ev(ctx, STUBS);
  // 이전에 쓰던 PC(rmPrev) 가 있다
  ev(ctx, "pcsUpsert({room:'rmPrev', cs:'cli_p', cp:'cpub_p', dev:'dev-P'}); pcsActivate('rmPrev')");
  const sockets = fakeWs(ctx);
  const { err, go } = pairInputs(ctx, LINK);
  ev(ctx, "doPair()"); await tick();
  assert.equal(ctx.localStorage.getItem("sm_devtoken"), "dev-A", "connect 전에 새 자격을 임시로 쓴다");
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c1" }); await tick();
  const af = ws.sent.at(-1); assert.equal(af.method, "auth"); assert.equal(af.params.token, "dev-A");
  ws.push({ type: "res", id: af.id, ok: false, error: "auth_invalid" }); await tick(); await tick();
  assert.ok(err.textContent.includes("더 이상 유효하지"), err.textContent);
  assert.equal(go.textContent, "연결");
  assert.equal(ev(ctx, "globalThis.__entered"), 0, "진입하지 않는다");
  same(ev(ctx, "pcsLoad().map(x => x.room)"), ["rmPrev"], "새 PC 는 목록에 저장되지 않는다");
  assert.equal(ctx.localStorage.getItem("sm_room"), "rmPrev", "이전 PC 로 되돌아간다");
  assert.equal(ctx.localStorage.getItem("sm_devtoken"), "dev-P");
  assert.equal(ctx.localStorage.getItem("sm_cs"), "cli_p");
  assert.equal(ctx.localStorage.getItem("sm_e2ee_rmA"), null, "방금 쓴 룸 키도 지운다");
  assert.equal(ev(ctx, "conn.devToken"), "dev-P");
});

test("doPair: auth 성공이면 저장·진입하고 PC 이름은 hostname, pv 를 기억한다(B-1, E-D17 준비)", async () => {
  const ctx = await load();
  ev(ctx, STUBS);
  const sockets = fakeWs(ctx);
  const { err } = pairInputs(ctx, LINK);
  ev(ctx, "doPair()"); await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c1" }); await tick();
  const af = ws.sent.at(-1);
  ws.push({ type: "res", id: af.id, ok: true, data: { id: "d1", name: "iPhone", ver: "0.10.0", pv: 3, hostname: "MY-PC" } });
  await tick(); await tick();
  assert.equal(err.textContent, "");
  assert.equal(ev(ctx, "globalThis.__entered"), 1);
  const pc = ev(ctx, "pcsLoad()[0]");
  assert.equal(pc.room, "rmA"); assert.equal(pc.name, "MY-PC"); assert.equal(pc.pv, 3);
  assert.equal(pc.dev, "dev-A"); assert.equal(pc.cs, "cli_1");
  assert.equal(ctx.localStorage.getItem("sm_room"), "rmA");
  // 사용자가 붙인 이름(pcsSetName)은 다음 auth 의 hostname 으로 안 덮이고, 비우면 다시 hostname 이 채운다
  ev(ctx, "pcsSetName('rmA', ' 내 데스크탑 '); pcsUpsert({room:'rmA', cs:'cli_1', cp:'cpub_1', dev:'dev-A', name:'OTHER', pv:4})");
  const pc2 = ev(ctx, "pcsLoad()[0]"); assert.equal(pc2.name, "내 데스크탑"); assert.equal(pc2.pv, 4);
  ev(ctx, "pcsSetName('rmA', ''); pcsUpsert({room:'rmA', cs:'cli_1', cp:'cpub_1', dev:'dev-A', name:'HOST2'})");
  assert.equal(ev(ctx, "pcsLoad()[0].name"), "HOST2");
  assert.equal(ev(ctx, "pcsSetName('nope', 'x')"), false);
  ev(ctx, "conn._stopped = true"); ws.close();
});

test("doPair: 공유토큰(dev 없음) 경로는 auth 없이 그대로 진입한다", async () => {
  const ctx = await load();
  ev(ctx, STUBS);
  const sockets = fakeWs(ctx);
  pairInputs(ctx, "https://relay.test/relay/app#room=rmS&token=rly-cli-1");
  ev(ctx, "doPair()"); await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c1" }); await tick(); await tick();
  assert.equal(ws.sent.some((f) => f.method === "auth"), false);
  assert.equal(ev(ctx, "globalThis.__entered"), 1);
  assert.equal(ev(ctx, "pcsLoad()[0].room"), "rmS");
  ev(ctx, "conn._stopped = true"); ws.close();
});
