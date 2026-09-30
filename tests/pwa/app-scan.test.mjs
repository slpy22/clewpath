// 앱 안 QR 스캔 입구(2026-09-29): 첫 실행 오버레이에만 있던 네이티브 스캔을 페어링 화면·PC 관리에서도.
// native-bridge.js 를 '앱' 으로 부트해 pairFromUrl 계약을 고정하고, index.html 은 브리지 스텁으로 입구를 검증한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import { load, ev, stubEl, makeSandbox } from "./harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const BRIDGE = join(here, "..", "..", "pwa", "native-bridge.js");
const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);

// native-bridge.js 부트. app=true 면 Capacitor 런타임(플러그인 프록시)을 흉내 낸다.
function bootBridge({ app = true, relayWs = null, scan = "https://relay.test/relay/app#room=r9&rk=k9" } = {}) {
  const sb = makeSandbox();
  Object.assign(sb.location, { protocol: "capacitor:", host: "localhost", pathname: "/", href: "capacitor://localhost/", hash: "" });
  const reloads = []; sb.location.reload = () => reloads.push(sb.location.hash);
  sb.Storage = class { setItem() {} removeItem() {} };          // 브리지가 prototype 을 감싼다
  if (relayWs) sb.localStorage.setItem("sm_relay_ws", relayWs);
  const scanned = [];
  if (app) sb.Capacitor = { isNativePlatform: () => true, Plugins: {
    Preferences: { set: async () => {}, remove: async () => {}, get: async () => ({ value: null }), keys: async () => ({ keys: [] }) },
    App: { addListener() {} },
    CapacitorBarcodeScanner: { scanBarcode: async (o) => { scanned.push(o); return { ScanResult: scan }; } } } };
  vm.runInContext(readFileSync(BRIDGE, "utf-8"), vm.createContext(sb), { filename: "native-bridge.js" });
  return { sb, B: sb.ClewBridge, reloads, scanned };
}

test("native-bridge(웹): 스캔·pairFromUrl 없음, 오류 문구는 빈 문자열", () => {
  const { B } = bootBridge({ app: false });
  assert.equal(B.isApp(), false); assert.equal(B.scanQr, null); assert.equal(B.pairFromUrl, null);
  assert.equal(B.pairErrorText("need_https"), "");
});

test("native-bridge(앱): pairFromUrl — https 만, 릴레이 ws 유도·저장, fragment 를 해시로 옮겨 리로드, 오류 코드→문구", async () => {
  const { sb, B, reloads, scanned } = bootBridge();
  assert.equal(B.isApp(), true); assert.equal(B.modeOverride(), "relay");
  assert.equal(B.pairFromUrl("garbage"), "bad_url");
  assert.equal(B.pairFromUrl("http://127.0.0.1:5100/relay/app#room=x"), "need_https", "로컬 http 링크는 거부");
  assert.equal(B.pairFromUrl("clewpath://pair#room=x"), "bad_url", "릴레이 주소를 모르면 clewpath:// 는 처리 불가");
  assert.equal(B.pairFromUrl("https://relay.test/relay/app"), "no_frag");
  assert.equal(sb.localStorage.getItem("sm_relay_ws"), "wss://relay.test/relay/ws", "wsUrl() 과 같은 유도 규칙");
  assert.equal(reloads.length, 0);
  assert.equal(B.pairFromUrl("https://relay.test/relay/app#room=r1&rk=k1&dev=d1"), "ok");
  same(reloads, ["room=r1&rk=k1&dev=d1"], "해시만 바꾸고 리로드(부트 파서가 pcsUpsert)");
  assert.equal(B.wsBaseOverride(), "wss://relay.test/relay/ws");
  assert.equal(B.pairFromUrl("clewpath://pair#room=r2"), "ok", "릴레이 주소가 있으면 딥링크도");
  for (const c of ["bad_url", "need_https", "no_frag", "unknown"]) assert.ok(B.pairErrorText(c).length > 10);
  assert.equal(await B.scanQr(), "https://relay.test/relay/app#room=r9&rk=k9");
  assert.ok(scanned[0].scanInstructions.includes("QR"));
});

test("native-bridge(앱): 첫 실행 오버레이(#cb-pair)는 0.11.0 에서 폐기 — 입구는 #pair 1벌(B-2/T15)", () => {
  const a = bootBridge();
  assert.ok(!a.sb.document.body.children.some((c) => c.id === "cb-pair"), "미페어링이어도 오버레이 없음");
  const b = bootBridge({ relayWs: "wss://relay.test/relay/ws" });
  assert.ok(!b.sb.document.body.children.some((c) => c.id === "cb-pair"));
});

// ---- index.html 쪽 입구 ----
const modalBody = (ctx) => {
  const ov = [...ctx.document.body.children].reverse().find((c) => c.id === "overlay");
  return ov.children[0].children.find((k) => String(k.className) === "modal-body").children[0];
};
const bridgeStub = (calls, { scan = "https://relay.test/relay/app#room=r1", result = "ok" } = {}) => ({
  isApp: () => true, modeOverride: () => "relay", wsBaseOverride: () => "wss://relay.test/relay/ws",
  showLocalPcOption: () => false, syncRoomKey() {}, registerPush() {}, keyboard: { setup() {} },
  scanQr: async () => { calls.push(["scan"]); return scan; },
  pairFromUrl: (u) => { calls.push(["pair", u]); return result; },
  pairErrorText: (c) => "E:" + c,
});

test("웹(브리지 없음): PC 관리에 스캔 버튼 없음, #p-scan 은 숨김 유지", async () => {
  const scanBtn = stubEl("button"); scanBtn.hidden = true;
  const ctx = await load({ setup: (sb) => sb.document._register("p-scan", scanBtn) });
  assert.equal(scanBtn.hidden, true);
  assert.equal(ctx.appScanAvailable(), false);
  ctx.showPcs();
  const kids = modalBody(ctx).children;
  assert.ok(!kids.some((k) => (k.textContent || "").includes("📷")));
  assert.ok(kids.at(-1).textContent.includes("QR 을 만들어 스캔하세요"));
  assert.equal(await ctx.appScanPair(), false);
});

test("앱(브리지 있음): 페어링 화면 #p-scan 노출 + PC 관리의 📷 버튼 → 스캔 → pairFromUrl, 취소는 조용히, 오류는 toast", async () => {
  const calls = [];
  const scanBtn = stubEl("button"); scanBtn.hidden = true;
  const ctx = await load({ setup: (sb) => { sb.ClewBridge = bridgeStub(calls); sb.document._register("p-scan", scanBtn); } });
  assert.equal(scanBtn.hidden, false, "부트 때 앱이면 스캔 버튼 노출");
  assert.equal(typeof scanBtn.onclick, "function");
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m); globalThis.__closed = 0; closeModal = () => { globalThis.__closed++; }");
  ctx.showPcs();
  const kids = modalBody(ctx).children;
  const btn = kids.find((k) => (k.textContent || "").includes("📷 QR 스캔으로 PC 추가"));
  assert.ok(btn, "PC 관리에 스캔 버튼");
  assert.ok(kids.at(-1).textContent.includes("QR 을 비추세요"));
  const closedBefore = ev(ctx, "globalThis.__closed");           // openModal 자체가 closeModal 을 한 번 부른다
  await btn.onclick();
  await new Promise((r) => setTimeout(r, 0));
  same(calls, [["scan"], ["pair", "https://relay.test/relay/app#room=r1"]]);
  assert.equal(ev(ctx, "globalThis.__closed"), closedBefore + 1, "모달을 닫고 스캐너로");
  // 취소(빈 문자열) → pairFromUrl 호출 없음, toast 없음
  ctx.ClewBridge.scanQr = async () => "";
  assert.equal(await ctx.appScanPair(), false);
  assert.equal(calls.length, 2); assert.equal(ev(ctx, "globalThis.__toasts.length"), 0);
  // 오류 코드 → 브리지 문구로 toast
  ctx.ClewBridge.scanQr = async () => "http://127.0.0.1:5100/relay/app#room=x";
  ctx.ClewBridge.pairFromUrl = () => "need_https";
  assert.equal(await ctx.appScanPair(), false);
  assert.equal(ev(ctx, "globalThis.__toasts").at(-1), "E:need_https");
  // #p-scan 버튼도 같은 함수
  ctx.ClewBridge.scanQr = async () => "https://relay.test/relay/app#room=r2";
  ctx.ClewBridge.pairFromUrl = (u) => { calls.push(["pair2", u]); return "ok"; };
  await scanBtn.onclick();
  same(calls.at(-1), ["pair2", "https://relay.test/relay/app#room=r2"]);
});

test("native-bridge(앱): clewpath://pair?relay= 가 실려 오면 릴레이 주소를 저장하고 진행(E-2, eng E-D2); persistSync·deviceName 계약", async () => {
  const { sb, B, reloads } = bootBridge();
  assert.equal(B.pairFromUrl("clewpath://pair?relay=clewpath.test%2Frelay#room=r1&rk=k1"), "ok");
  assert.equal(sb.localStorage.getItem("sm_relay_ws"), "wss://clewpath.test/relay/ws", "relay 파라미터 → wss://<host/base>/ws");
  assert.equal(reloads.length, 1); assert.equal(sb.location.hash, "room=r1&rk=k1");   // 샌드박스 location 은 # 를 안 붙인다
  assert.equal(B.pairFromUrl("clewpath://pair?relay=x.test#"), "no_frag");
  const p = B.persistSync("sm_room"); assert.ok(p && typeof p.then === "function"); await p;
  assert.equal(typeof B.deviceName(), "string");
  const web = bootBridge({ app: false });
  await web.B.persistSync("sm_room"); assert.equal(web.B.deviceName(), "");
});
