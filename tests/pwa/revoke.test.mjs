// 0.11.0 T11/T13/T15: 폐기 큐(self-revoke)·notice·⛔🔄 상태·해제 순서·📵⏳💤 칩.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const tick = () => new Promise((r) => setTimeout(r, 0));
const QUIET = "location.reload = () => {}; setTitle = () => {}; toast = (m) => { (globalThis.__toasts ||= []).push(m); };"
  + "cpBase = () => 'https://cp.test/cp';";

function fakeFetch(ctx, statusFor) {
  const calls = [];
  ctx.fetch = async (url, o) => {
    const st = statusFor(url, o);
    calls.push({ url, body: o && o.body ? JSON.parse(o.body) : null });
    return { status: st, ok: st === 200, json: async () => ({}) };
  };
  return calls;
}

test("revokeEnqueue: 기기 자격(cs+cp)만 큐에 넣고, 같은 cpub 은 하나로, 저장은 persistSync 까지 기다린다", async () => {
  const ctx = await load();
  ev(ctx, QUIET + "globalThis.__ps = []; window.ClewBridge = { persistSync: async (k) => { globalThis.__ps.push(k); } };");
  assert.equal(await ev(ctx, "revokeEnqueue({room:'r', cs:'', cp:''})"), false, "공유토큰(구형)은 폐기할 자격 없음");
  assert.equal(await ev(ctx, "revokeEnqueue({room:'r1', cs:'s1', cp:'p1'})"), true);
  await ev(ctx, "revokeEnqueue({room:'r1', cs:'s1b', cp:'p1'})");
  const q = ev(ctx, "revokeQLoad()");
  assert.equal(q.length, 1); assert.equal(q[0].secret, "s1b"); assert.equal(q[0].cp, "https://cp.test/cp");
  same(ev(ctx, "globalThis.__ps"), ["sm_revoke_queue", "sm_revoke_queue"]);
});

test("revokeFlush: 200·401 은 완료로 빼고, 404(구 CP)·5xx 는 남기고, 90일 지난 항목은 포기", async () => {
  const ctx = await load(); ev(ctx, QUIET);
  const calls = fakeFetch(ctx, (u, o) => ({ p200: 200, p401: 401, p404: 404 })[JSON.parse(o.body).public_id] || 500);
  const old = Date.now() - 91 * 86400000;
  ctx.localStorage.setItem("sm_revoke_queue", JSON.stringify([
    { cp: "https://cp.test/cp/", public_id: "p200", secret: "a", room: "r200", ts: Date.now() },
    { cp: "https://cp.test/cp", public_id: "p401", secret: "b", room: "r401", ts: Date.now() },
    { cp: "https://cp.test/cp", public_id: "p404", secret: "c", room: "r404", ts: Date.now() },
    { cp: "https://cp.test/cp", public_id: "p500", secret: "d", room: "r500", ts: old },
  ]));
  const r = await ev(ctx, "revokeFlush()");
  same(r.done, ["r200", "r401"]); assert.equal(r.left, 1);
  same(ev(ctx, "revokeQLoad().map(x => x.public_id)"), ["p404"]);
  assert.equal(calls[0].url, "https://cp.test/cp/client/revoke-self", "끝 슬래시 정리");
  same(calls[0].body, { public_id: "p200", secret: "a" });
  fakeFetch(ctx, () => 200); await ev(ctx, "revokeFlush()");
  assert.equal(ctx.localStorage.getItem("sm_revoke_queue"), null, "비면 키 자체를 지운다");
});

test("unpairPc(pv≥2, 현재 PC): 큐 영속화 → bye_device → 로컬 제거 → 플러시, 서버 폐기되면 unpaired_server", async () => {
  const ctx = await load(); ev(ctx, QUIET);
  ev(ctx, "globalThis.__order = []; confirmSheet = async (o) => { await o.run(); return true; };"
    + "globalThis.__enq = revokeEnqueue; revokeEnqueue = async (pc) => { globalThis.__order.push('enqueue'); return globalThis.__enq(pc); };"
    + "conn.req = async (m) => { globalThis.__order.push(m + ':' + pcsLoad().length); return {}; };");
  fakeFetch(ctx, () => 200);
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa', dev:'da', pv:2}); pcsActivate('rmA')");
  await ev(ctx, "unpairPc('rmA')");
  same(ev(ctx, "globalThis.__order"), ["enqueue", "bye_device:1"], "bye 는 로컬 제거 전(PC 목록에 아직 있음)");
  same(ev(ctx, "pcsLoad()"), []);
  assert.equal(ev(ctx, "revokeQLoad().length"), 0, "폐기 완료 → 큐 비움");
  assert.ok(ctx.localStorage.getItem("sm_pair_reason").includes("unpaired_server"));
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("서버 폐기 완료"));
});

test("unpairPc(pv<2 Host): bye 없이 '이 폰에서만', 큐는 남아 다음에 재시도(CP 404)", async () => {
  const ctx = await load(); ev(ctx, QUIET);
  ev(ctx, "globalThis.__req = 0; confirmSheet = async (o) => { globalThis.__sub = o.sub; await o.run(); return true; }; conn.req = async () => { globalThis.__req++; };");
  fakeFetch(ctx, () => 404);
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa', dev:'da', pv:1}); pcsActivate('rmA')");
  await ev(ctx, "unpairPc('rmA')");
  assert.equal(ev(ctx, "globalThis.__req"), 0);
  assert.ok(ev(ctx, "globalThis.__sub").includes("이 폰에서만"), "구 Host 는 이 폰에서만 문구");
  assert.equal(ev(ctx, "revokeQLoad().length"), 1, "404 는 남긴다");
  const r = JSON.parse(ctx.localStorage.getItem("sm_pair_reason")); assert.equal(r.k, "unpaired");
});

test("resetDevice: 모든 PC 를 큐에 넣고 초기화 — 큐 키는 초기화가 지우지 않는다", async () => {
  const ctx = await load(); ev(ctx, QUIET);
  ev(ctx, "confirmSheet = async (o) => { await o.run(); return true; };");
  fakeFetch(ctx, () => 503);
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa'}); pcsUpsert({room:'rmB', cs:'cb', cp:'pb'}); pcsUpsert({room:'rmC'}); pcsActivate('rmA')");
  await ev(ctx, "resetDevice()");
  same(ev(ctx, "pcsLoad()"), []);
  same(ev(ctx, "revokeQLoad().map(x => x.room).sort()"), ["rmA", "rmB"]);
});

test("notice/onauth: device_removed→⛔removed, device_reissued→🔄reissued, auth_invalid→⛔changed(사유 단정 안 함)", async () => {
  const ctx = await load(); ev(ctx, QUIET + "globalThis.__lost = []; showAuthLost = (e) => globalThis.__lost.push(e);");
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa'}); pcsActivate('rmA')");
  ev(ctx, "conn.onnotice({type:'notice', kind:'device_reissued'})");
  assert.equal(ev(ctx, "pcsLoad()[0].state"), "reissued");
  ev(ctx, "conn.onnotice({type:'notice', kind:'device_removed'})");
  assert.equal(ev(ctx, "pcsLoad()[0].state"), "removed");
  ev(ctx, "conn.onauth(false, 'auth_invalid')");
  assert.equal(ev(ctx, "pcsLoad()[0].state"), "changed");
  same(ev(ctx, "globalThis.__lost"), ["reissued", "removed", "auth_invalid"]);
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa'})");
  assert.equal(ev(ctx, "pcsLoad()[0].state"), "changed", "같은 자격 갱신은 상태 유지");
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca2', cp:'pa2', dev:'d2'})");
  assert.equal(ev(ctx, "pcsLoad()[0].state"), undefined, "새 QR 자격이면 상태 해제");
});

test("Conn: notice 프레임은 onnotice 로 전달(응답 대기와 무관)", async () => {
  const ctx = await load();
  const all = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; all.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    close(code) { this.readyState = 3; this.onclose && this.onclose({ code: code || 1000 }); }
  };
  ev(ctx, "globalThis.__n = []; conn.onauth = null; conn.devToken = ''; conn.onnotice = (m) => globalThis.__n.push(m.kind);");
  const p = ev(ctx, "conn.connect('room1', 'tok')"); await tick();
  const ws = all.at(-1); ws.readyState = 1; ws.onopen && ws.onopen();
  ws.onmessage({ data: JSON.stringify({ type: "hello", peer_present: true, cid: "c1" }) });
  await p;
  ws.onmessage({ data: JSON.stringify({ type: "notice", kind: "device_removed" }) });
  await tick(); await tick();
  same(ev(ctx, "globalThis.__n"), ["device_removed"]);
  ev(ctx, "conn._stopped = true"); ws.close();
});

test("renderPairPcs: ⛔/🔄 PC 는 연결 버튼 대신 칩+[다시 페어링][지우기], 지우기는 목록에서 제거", async () => {
  const wrap = stubEl("div");
  const ctx = await load({ setup: (sb) => sb.document._register("p-pcs", wrap) });
  ev(ctx, QUIET + "applyPairSituation = () => {}; globalThis.__rows = []; pairRow = (o) => { globalThis.__rows.push(o); return el('div'); };");
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa', name:'집 PC', state:'removed'}); pcsUpsert({room:'rmB', cs:'cb', cp:'pb', name:'회사 PC'})");
  ev(ctx, "renderPairPcs()");
  const rows = ev(ctx, "globalThis.__rows");
  assert.equal(rows.length, 1, "정상 PC 는 기존 연결 버튼(pairRow 아님)");
  assert.equal(rows[0].chip.text, "PC 에서 해제됨");
  same(rows[0].buttons.map((b) => b.textContent), ["다시 페어링", "지우기"]);
  rows[0].buttons[1].onclick();
  same(ev(ctx, "pcsLoad().map(x => x.room)"), ["rmB"]);
});

test("showDevices: 칩 우선순위 📵(폰) > ⏳ > 🔗 > 💤(90일), 진단 줄에 📵 수·CP 동기화", async () => {
  const ctx = await load(); ev(ctx, QUIET);
  const now = Math.floor(Date.now() / 1000);
  ev(ctx, "globalThis.__rows = []; pairRow = (o) => { globalThis.__rows.push(o.chip ? o.chip.icon : null); return el('div'); };"
    + "openModal = (t, body) => { globalThis.__diag = body.children.find(c => String(c.className) === 'diag').textContent; };");
  ev(ctx, `T = { api: async () => ({ active: 2, total: 5, phone_revoked: 1, cp_synced: ${now - 60}, devices: [
    { id: 'a', name: 'A', revoked: true, revoked_by: 'phone', client_scoped: true },
    { id: 'b', name: 'B', revoked: true, delete_pending: true, cp_pending: true, client_scoped: true },
    { id: 'c', name: 'C', client_scoped: false, last_seen: ${now - 100 * 86400} },
    { id: 'd', name: 'D', client_scoped: true, last_seen: ${now - 100 * 86400} },
    { id: 'e', name: 'E', client_scoped: true, last_seen: ${now} } ] }) }`);
  await ev(ctx, "showDevices()");
  same(ev(ctx, "globalThis.__rows"), ["📵", "⏳", "🔗", "💤", null]);
  const d = ev(ctx, "globalThis.__diag");
  assert.ok(d.includes("📵 1") && d.includes("CP 동기화"), d);
});

test("noteHostVer/showStaleBanner: 이 탭에서 처음 본 PC 버전과 달라지면 새로고침 배너 1개(PC 별)", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__b = 0; showStaleBanner = (v) => { globalThis.__b++; globalThis.__bv = v; };");
  assert.equal(ev(ctx, "noteHostVer('0.10.8')"), false, "처음 본 버전은 기준");
  assert.equal(ev(ctx, "noteHostVer('0.10.8')"), false);
  assert.equal(ev(ctx, "noteHostVer('0.11.0')"), true);
  assert.equal(ev(ctx, "globalThis.__bv"), "0.11.0");
  ev(ctx, "showVer('0.11.1', null)");
  assert.equal(ev(ctx, "globalThis.__b"), 2, "showVer 경유로도 감지");
});
