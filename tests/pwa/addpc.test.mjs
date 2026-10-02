// 2026-10-02 사장님 아이폰 실기: ① 앱의 'QR 스캔' 이 아무 반응 없음(iOS 플러그인 미등록 + 조용한 실패)
// ② 접속한 채로 새 PC 를 더할 입구가 없음. → 스캔 실패는 이유를 보이고, '＋ 새 PC 추가' 로 페어링 화면에 갔다 올 수 있게.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import { load, ev, stubEl, makeSandbox } from "./harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const BRIDGE = join(here, "..", "..", "pwa", "native-bridge.js");

function bootBridge(scanner) {
  const sb = makeSandbox();
  Object.assign(sb.location, { protocol: "capacitor:", host: "localhost", pathname: "/", href: "capacitor://localhost/", hash: "" });
  sb.location.reload = () => {};
  sb.Storage = class { setItem() {} removeItem() {} };
  const plugins = { Preferences: { set: async () => {}, remove: async () => {}, get: async () => ({ value: null }), keys: async () => ({ keys: [] }) },
                    App: { addListener() {} } };
  if (scanner) plugins.CapacitorBarcodeScanner = scanner;
  sb.Capacitor = { isNativePlatform: () => true, Plugins: plugins };
  vm.runInContext(readFileSync(BRIDGE, "utf-8"), vm.createContext(sb), { filename: "native-bridge.js" });
  return sb.ClewBridge;
}

test("bridge.scanQr: 사용자 취소는 '' , 플러그인 없음·권한 거부 등은 이유를 담아 reject(조용한 실패 금지)", async () => {
  const none = bootBridge(null);
  await assert.rejects(none.scanQr(), /scanner_unavailable/);
  const cancel = bootBridge({ scanBarcode: async () => { throw { code: "OS-PLUG-BARC-0006", message: "Scanning cancelled." }; } });
  assert.equal(await cancel.scanQr(), "");
  const denied = bootBridge({ scanBarcode: async () => { throw { code: "OS-PLUG-BARC-0007", message: "Couldn't scan because camera access wasn't provided." }; } });
  await assert.rejects(denied.scanQr(), /BARC-0007/);
  const cancel2 = bootBridge({ scanBarcode: async () => { throw "Scanning cancelled"; } });     // iOS call.reject("Scanning cancelled")
  assert.equal(await cancel2.scanQr(), "");
  const notImpl = bootBridge({ scanBarcode: async () => { throw { code: "UNIMPLEMENTED", message: "not implemented on ios" }; } });
  await assert.rejects(notImpl.scanQr(), /UNIMPLEMENTED/);
});

const bridgeStub = (scanQr) => ({
  isApp: () => true, modeOverride: () => "relay", wsBaseOverride: () => "wss://relay.test/relay/ws",
  showLocalPcOption: () => false, syncRoomKey() {}, registerPush() {}, keyboard: { setup() {} },
  scanQr, pairFromUrl: () => "ok", pairErrorText: (c) => "E:" + c });

test("appScanPair: 스캐너 실패는 원인별 안내 토스트, 취소는 조용히", async () => {
  const ctx = await load({ setup: (sb) => { sb.ClewBridge = bridgeStub(async () => ""); } });
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);");
  assert.equal(await ctx.appScanPair(), false);
  assert.equal(ev(ctx, "globalThis.__toasts.length"), 0, "취소는 조용히");
  for (const [err, want] of [["scanner_unavailable", "카메라 앱으로"], ["UNIMPLEMENTED", "최신 빌드"],
                             ["OS-PLUG-BARC-0007 Couldn't scan because camera access wasn't provided.", "카메라 권한"], ["weird", "QR 스캔 실패: weird"]]) {
    ctx.ClewBridge.scanQr = async () => { throw new Error(err); };
    assert.equal(await ctx.appScanPair(), false);
    assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes(want), err);
  }
});

test("＋ 새 PC 추가: 🖧 메뉴·헤더 선택 → 페어링 화면(앱이면 📷), ← 돌아가기로 원래 화면, 연결되면 버튼 정리", async () => {
  const els = {};
  const ctx = await load({ setup: (sb) => {
    sb.ClewBridge = bridgeStub(async () => "");
    for (const id of ["pair", "app", "p-back", "p-sit", "p-link", "p-err", "p-scan"]) { els[id] = stubEl(id === "p-link" ? "input" : "div"); sb.document._register(id, els[id]); }
  } });
  ev(ctx, "closeModal = () => {}; globalThis.__m = null; openModal = (t, b) => { globalThis.__m = b; }; renderPairPcs = () => {};");
  els.pair.classList.add("hidden"); els.app.classList.remove("hidden"); els["p-back"].hidden = true;
  ctx.showPcs();
  const add = ev(ctx, "globalThis.__m").children.find((k) => (k.textContent || "") === "＋ 새 PC 추가");
  assert.ok(add, "🖧 메뉴 맨 위에 ＋ 새 PC 추가");
  add.onclick();
  assert.ok(!els.pair.classList.contains("hidden") && els.app.classList.contains("hidden"), "페어링 화면으로");
  assert.equal(els["p-back"].hidden, false);
  assert.ok(els["p-sit"].textContent.startsWith("새 PC 추가"));
  assert.equal(els["p-scan"].hidden, false, "앱이면 📷 스캔 버튼");
  els["p-back"].onclick();
  assert.ok(els.pair.classList.contains("hidden") && !els.app.classList.contains("hidden") && els["p-back"].hidden, "돌아가기");
  ev(ctx, "openAddPc()");
  ev(ctx, "loadList = () => {}; maybeShowIosHint = () => {}; refreshPcSelect = () => {}; refreshPushBtn = () => {}; enterApp()");
  assert.equal(els["p-back"].hidden, true, "새 PC 로 연결되면(enterApp) 돌아가기 버튼 정리");
  ev(ctx, "globalThis.__add = 0; openAddPc = () => { globalThis.__add++; }; onPcSelect.call(null, {target:{value:'__add__'}})");
});

test("헤더 PC 선택(릴레이)에 ＋ 새 PC 추가… 항목", async () => {
  const sel = stubEl("select");
  const ctx = await load({ setup: (sb) => { sb.document._register("pcname", sel); } });
  ev(ctx, "pcsUpsert({room:'rmA', cs:'c', cp:'p', name:'집'}); localStorage.setItem('sm_room','rmA')");
  ev(ctx, "refreshPcSelect()");
  const vals = (sel.children || []).map((o) => o.value);
  assert.ok(vals.includes("__add__") && vals.includes("__manage__"), JSON.stringify(vals));
});

test("iOS 플러그인 등록 보정 스크립트: 바코드 스캐너를 packageClassList 에 더하고 두 번째는 그대로", async () => {
  const mod = await import(pathToFileURL(join(here, "..", "..", "app", "scripts", "patch-ios-plugins.mjs")).href);
  const r1 = mod.patch(JSON.stringify({ appId: "x", packageClassList: ["AppPlugin", "PreferencesPlugin"] }));
  assert.equal(r1.added, 1);
  assert.deepEqual(JSON.parse(r1.text).packageClassList, ["AppPlugin", "PreferencesPlugin", "CapacitorBarcodeScannerPlugin"]);
  assert.equal(mod.patch(r1.text).added, 0);
  assert.equal(mod.patch(JSON.stringify({ appId: "x" })).added, 1, "목록이 없어도");
  const pkg = JSON.parse(readFileSync(join(here, "..", "..", "app", "package.json"), "utf-8"));
  assert.ok(pkg.scripts["capacitor:sync:after"].includes("patch-ios-plugins") && pkg.scripts["capacitor:copy:after"].includes("patch-ios-plugins"),
    "cap sync/copy 뒤 자동 실행 훅");
});
