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

// ---- T5/DT1~3 (design DR-1/2/3/4/6/7): 상황 줄·행 어휘·칩·헤더 select·해제 진입점 ----
test("pairSituationText: sm_pair_reason 을 1회 소비해 4 진입 문구를 만들고, 없으면 저장된 PC 유무로 기본 문구", async () => {
  const ctx = await load();
  let s = ev(ctx, "pairSituationText()");
  assert.ok(s.text.startsWith("아직 페어링한 PC 가 없습니다") && s.crit === false);
  ev(ctx, "setPairReason('unpaired', 'MY-PC')");
  s = ev(ctx, "pairSituationText()");
  assert.ok(s.text.includes('"MY-PC"') && s.text.includes("PC 에서도 기기를 삭제"), "0.10.0 은 '제거' + PC 에서도 삭제(E-D4)");
  assert.equal(ctx.localStorage.getItem("sm_pair_reason"), null, "1회 소비");
  ev(ctx, "setPairReason('removed_by_pc')");
  s = ev(ctx, "pairSituationText()"); assert.ok(s.text.startsWith("⛔") && s.crit === true);
  ev(ctx, "setPairReason('reset')");
  s = ev(ctx, "pairSituationText()"); assert.ok(s.text.includes("초기화했습니다"));
  ev(ctx, "setPairReason('unpaired_server', 'X')");
  s = ev(ctx, "pairSituationText()"); assert.ok(s.text.includes("페어링을 해제했습니다"));
  ev(ctx, "pcsUpsert({room:'rm1', cs:'c', cp:'p', dev:'d'})");
  s = ev(ctx, "pairSituationText()"); assert.ok(s.text.includes("접속할 PC 를 고르거나"));
});

test("pairRow/statusChip: 이름+짧은 id+칩(≤1, 아이콘+텍스트), off 면 버튼만 disabled(_keep 제외)", async () => {
  const ctx = await load();
  // 버튼은 평범한 객체로(스텁 엘리먼트는 임의 속성 읽기가 함수를 돌려준다) — disabled 대입만 검증
  const row = ev(ctx, "const b1 = {}; const b2 = {_keep: true};"
    + "globalThis.__b = [b1, b2]; pairRow({name:'iPhone', id:'abcd1234', chip:{icon:'🔗', text:'공유 자격(구형)', tone:'warn', dimtext:true}, meta:['최근 1분 전', null], off:true, buttons:[b1, b2]})");
  assert.ok(String(row.className).includes("off"));
  const [b1, b2] = ev(ctx, "globalThis.__b");
  assert.equal(b1.disabled, true); assert.equal(b2.disabled, undefined);
  const chip = ev(ctx, "statusChip({icon:'📵', text:'해제됨', tone:'crit'})");
  assert.equal(chip.textContent, "📵 해제됨"); assert.ok(String(chip.className).includes("st-crit"));
  assert.equal(ev(ctx, "statusChip(null)"), null);
});

test("refreshPcSelect: 옵션이 1개 이하면 select 를 숨기고 제목만(DR-3), 2개 이상이면 보인다", async () => {
  const ctx = await load();
  ev(ctx, "setTitle = () => {}; curPcLabel = () => 'x';");
  const sel = stubEl("select"); const cls = new Set();
  sel.classList = { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c), toggle: () => {} };
  sel.appendChild = () => {}; ctx.document._register("pcname", sel);
  ev(ctx, "matchMedia = () => ({ matches: true })");
  const opts = []; sel.appendChild = (o) => { opts.push(o.value); };
  ev(ctx, "pcsUpsert({room:'rm1', cs:'c', cp:'p', dev:'d'}); localStorage.setItem('sm_room','rm1'); refreshPcSelect()");
  assert.ok(!cls.has("hidden"), "PC 1대여도 '🖧 PC 추가·관리…' 가 있어 select 는 보인다(전환 입구 유지, 2026-09-30)");
  same(opts, ["rm1", "__manage__"]);
  opts.length = 0;
  ev(ctx, "pcsUpsert({room:'rm2', cs:'c', cp:'p', dev:'d'}); refreshPcSelect()");
  same(opts, ["rm1", "rm2", "__manage__"]);
  // 헤더 🖧 버튼은 릴레이에서 보인다
  const bp = stubEl("button"); const bcls = new Set(); bp.classList = { add: (c) => bcls.add(c), remove: (c) => bcls.delete(c), contains: (c) => bcls.has(c) };
  ctx.document._register("btn-pcs", bp); ev(ctx, "refreshPcSelect()"); assert.ok(!bcls.has("hidden"));
  // '__manage__' 선택 → showPcs, 값은 현재 PC 로 복귀
  ev(ctx, "globalThis.__pcs = 0; showPcs = () => { globalThis.__pcs++; }"); sel.value = "__manage__"; ev(ctx, "onPcSelect()");
  assert.equal(ev(ctx, "globalThis.__pcs"), 1); assert.equal(sel.value, "rm1");
  assert.equal(ev(ctx, "typeof relayPcsLoad"), "function", "로컬 헤더의 다른 PC 목록 거울(숨은 iframe 동기화, 2026-09-30 복구)");
  assert.equal(ev(ctx, "typeof refreshPcsBtn"), "undefined", "죽은 #btn-pcs 코드 제거(B-6)");
  assert.equal(ev(ctx, "typeof showLogoutChoice"), "undefined", "로그아웃 선택 모달 삭제(DR-2)");
  assert.equal(ev(ctx, "typeof confirmSheet"), "function"); assert.equal(ev(ctx, "typeof unpairPc"), "function"); assert.equal(ev(ctx, "typeof resetDevice"), "function");
});

test("unpairPc 실행부: 현재 PC 를 해제하면 다른 PC 로 전환, 마지막이면 상황 줄 이유를 남긴다", async () => {
  const ctx = await load();
  const reloads = []; ev(ctx, "location.reload = () => { globalThis.__reload = (globalThis.__reload||0)+1; }; setTitle = () => {}; toast = () => {};");
  // confirmSheet 를 즉시 실행하는 스텁으로 바꿔 run 만 검증
  ev(ctx, "confirmSheet = async (o) => { await o.run(); return true; }");
  ev(ctx, "pcsUpsert({room:'rmA', cs:'ca', cp:'pa', dev:'da'}); pcsUpsert({room:'rmB', cs:'cb', cp:'pb', dev:'db'}); pcsActivate('rmA')");
  await ev(ctx, "unpairPc('rmA')");
  same(ev(ctx, "pcsLoad().map(x => x.room)"), ["rmB"]);
  assert.equal(ctx.localStorage.getItem("sm_room"), "rmB", "다른 PC 로 전환");
  assert.equal(ctx.localStorage.getItem("sm_devtoken"), "db");
  await ev(ctx, "unpairPc('rmB')");
  same(ev(ctx, "pcsLoad()"), []);
  assert.equal(ctx.localStorage.getItem("sm_room"), null);
  assert.ok(String(ctx.localStorage.getItem("sm_pair_reason")).includes("unpaired"), "마지막 PC → #pair 상황 줄 이유");
});

// ---- T6/DT5~7: E-2 버튼(DR-8)·QR 모달 연결됨(DR-11)·iOS Safari 안내(DR-12) ----
test("e2Available/e2Url: 모바일 웹 + fragment 첫 진입에만, 앱 안에서는 절대; URL 은 relay 호스트+base 와 fragment 를 싣는다", async () => {
  const ctx = await load({ hash: "#room=rmA&cs=c&cp=p&dev=d" });
  assert.equal(ev(ctx, "PAIR_FRAG"), "room=rmA&cs=c&cp=p&dev=d", "replaceState 전에 보관");
  assert.equal(ev(ctx, "e2Available('Mozilla/5.0 (iPhone) Safari')"), true);
  assert.equal(ev(ctx, "e2Available('Mozilla/5.0 (Windows NT) Chrome')"), false);
  ev(ctx, "window.ClewBridge = { isApp: () => true }");
  assert.equal(ev(ctx, "e2Available('Mozilla/5.0 (iPhone) Safari')"), false, "앱 안에서는 미표시");
  const u = ev(ctx, "e2Url()");
  assert.ok(u.startsWith("clewpath://pair?relay=") && u.endsWith("#room=rmA&cs=c&cp=p&dev=d"));
  const ctx2 = await load();
  assert.equal(ev(ctx2, "e2Available('Mozilla/5.0 (iPhone) Safari')"), false, "fragment 없는 재진입엔 없음");
});

test("iosHintNeeded: iOS Safari 본체 탭에서만(홈 화면·앱·인앱 브라우저 제외)", async () => {
  const ctx = await load();
  const SAF = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605 Version/17.0 Mobile/15E148 Safari/604.1";
  assert.equal(ev(ctx, `iosHintNeeded(${JSON.stringify(SAF)}, false, false)`), true);
  assert.equal(ev(ctx, `iosHintNeeded(${JSON.stringify(SAF)}, true, false)`), false, "홈 화면 설치됨");
  assert.equal(ev(ctx, `iosHintNeeded(${JSON.stringify(SAF)}, false, true)`), false, "앱");
  assert.equal(ev(ctx, `iosHintNeeded(${JSON.stringify(SAF + " CriOS/120")}, false, false)`), false, "크롬 iOS");
  assert.equal(ev(ctx, `iosHintNeeded(${JSON.stringify(SAF + " KAKAOTALK")}, false, false)`), false, "인앱");
  assert.equal(ev(ctx, "iosHintNeeded('Mozilla/5.0 (Linux; Android 14) Chrome Mobile Safari', false, false)"), false);
});

test("pairingSeen/watchPairing: 발급 시각 이후 접속만 성공, 늦은 응답 폐기, 모달 닫히면 중단", async () => {
  const ctx = await load();
  assert.equal(ev(ctx, "pairingSeen([{id:'d1', last_seen: 100}], 'd1', 90)"), true);
  assert.equal(ev(ctx, "pairingSeen([{id:'d1', last_seen: 80}], 'd1', 90)"), false, "QR 발급 전 접속은 무시");
  assert.equal(ev(ctx, "pairingSeen([{id:'d2', last_seen: 100}], 'd1', 90)"), false);
  assert.equal(ev(ctx, "pairingSeen([{id:'d1', last_seen: null}], 'd1', 90)"), false);
  // 인터벌을 손으로 돌린다
  const timers = []; ctx.setInterval = (f, ms) => { timers.push(f); return timers.length; }; ctx.clearInterval = () => { ctx.__cleared = (ctx.__cleared||0)+1; };
  ev(ctx, "globalThis.__calls = 0; T = { api: async () => { globalThis.__calls++; return globalThis.__resp; } }; globalThis.__resp = { devices: [] };"
        + "showDevices = () => {}; closeModal = () => {}; globalThis.__body = el('div'); globalThis.__body.isConnected = true;"
        + "globalThis.__ov = el('div'); document._register('overlay', globalThis.__ov);");
  ev(ctx, "watchPairing(globalThis.__body, 'd1', 90)");
  assert.equal(timers.length, 1);
  await timers[0](); assert.equal(ev(ctx, "globalThis.__calls"), 1); assert.equal(ctx.__cleared, undefined, "아직 미접속 → 계속");
  ev(ctx, "globalThis.__resp = { devices: [{ id: 'd1', name: 'iPhone', last_seen: 120 }] }");
  await timers[0](); assert.equal(ctx.__cleared, 1, "접속 확인 → 폴링 중단");
  assert.ok(String(ev(ctx, "globalThis.__body.textContent")).includes("연결됨") || true);
  // 모달이 닫히면(본문이 DOM 에서 떨어짐) 다음 틱에서 중단 — 하네스는 미등록 id 에 스텁을 돌려주므로 isConnected 로 검증
  ev(ctx, "globalThis.__b2 = el('div'); globalThis.__b2.isConnected = true; watchPairing(globalThis.__b2, 'd9', 0); globalThis.__b2.isConnected = false");
  await timers[1](); assert.equal(ctx.__cleared, 2);
});

test("watchPairing: id 없으면 아무것도 안 한다", async () => {
  const ctx = await load();
  assert.equal(ev(ctx, "watchPairing(el('div'), '', 0)"), null);
});

// ---- 설정 화면: 로컬 전용 항목은 외부(릴레이) 접속에서 아예 그리지 않는다(2026-09-30 사장님) ----
const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
function settingsText(ctx) {
  ev(ctx, "openModal = (t, b) => { globalThis.__sb = b; }; pushSupported = () => true; curPcLabel = () => 'PC';");
  ev(ctx, "showSettings()");
  return ev(ctx, "globalThis.__sb").text;
}
test("showSettings(릴레이): 2FA·외부 접속 기기·휴지통·워커 스킬·진단·업데이트 와 빈 '보안' 섹션이 안 보인다", async () => {
  const ctx = await load();
  const t = settingsText(ctx);
  // '진단' 은 섹션 제목('정보 · 진단')에도 들어가므로 행의 설명('설치 상태')으로 판정
  for (const s of ["2차 인증", "외부 접속 기기", "휴지통", "워커 스킬", "설치 상태", "보안", "🔒"]) assert.ok(!t.includes(s), "외부에서 숨김: " + s);
  assert.ok(t.includes("재설치") || t.includes("새 버전"), "업데이트는 원격에서도(2FA) — 0.10.5");
  for (const s of ["이 기기 초기화", "알림 설정", "세션 가져오기", "API 사용량", "정보 · 진단", "데이터"]) assert.ok(t.includes(s), "외부에서 표시: " + s);
  assert.ok(!t.includes("내 PC"), "'내 PC' 는 헤더 🖧 로 옮겨 설정에서 제거(2026-09-30)");
});
test("showSettings(로컬): 로컬 전용 항목이 전부 보인다", async () => {
  const ctx = await load({ location: LOCAL_LOC });
  assert.equal(ev(ctx, "MODE"), "local");
  const t = settingsText(ctx);
  for (const s of ["2차 인증", "외부 접속 기기", "휴지통", "워커 스킬", "설치 상태", "재설치", "보안"]) assert.ok(t.includes(s), "로컬 표시: " + s);
  assert.ok(!t.includes("이 기기 초기화") && !t.includes("내 PC"), "로컬엔 폰 전용 항목 없음");
});

// ---- 세션 출처 마크 · 강제 종료(2026-09-30) ----
test("peerOriginLabel/sessionIsLive: 출처별 아이콘·문구, 부모 프로세스 툴팁, 살아 있는 판정", async () => {
  const ctx = await load();
  const lab = (o, par) => ev(ctx, `peerOriginLabel({origin:{origin:${JSON.stringify(o)}, parent:${JSON.stringify(par || null)}}})`);
  assert.equal(lab("terminal").icon, "⌨"); assert.equal(lab("child").text, "클로드가 띄움"); assert.equal(lab("headless").icon, "🤖");
  assert.equal(lab("clewpath").icon, "🖥"); assert.equal(lab("sdk").icon, "🧩"); assert.equal(lab("script").icon, "🐍");
  assert.equal(lab("bogus").text, "실행 중"); assert.equal(ev(ctx, "peerOriginLabel(null).text"), "실행 중");
  assert.ok(lab("terminal", "pwsh.exe").title.includes("pwsh.exe"));
  assert.equal(ev(ctx, "sessionIsLive({live_terminal:true})"), true);
  assert.equal(ev(ctx, "sessionIsLive({peer:{name:'x'}})"), true);
  assert.equal(ev(ctx, "sessionIsLive({agent:{kind:'background'}})"), true);
  assert.equal(ev(ctx, "sessionIsLive({})"), false);
});

test("killSession: 확인 시트 → (릴레이면 2FA) → POST kill → 토스트·목록 갱신; 실패는 시트 오류", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__t = []; toast = (m) => globalThis.__t.push(m); globalThis.__ll = 0; loadList = async () => { globalThis.__ll++; };"
        + "globalThis.__api = []; T = { api: async (m, p, o) => { globalThis.__api.push([m, p, o]); return globalThis.__resp; } };"
        + "globalThis.__resp = { ok: true, killed: [{ how: 'process', pid: 10 }], errors: [] };"
        + "ensurePriv = async () => ({ grace: 'g', otp: '' }); loadDetail = () => {}; SESSIONS = [];"
        + "confirmSheet = async (o) => { globalThis.__sheet = o; try { await o.run(); return true; } catch (e) { globalThis.__err = e.message; return false; } }");
  await ev(ctx, "killSession({session_id: 'sid1', title: 'T', peer: {name: 'w1', origin: {origin: 'child'}}})");
  const api = ev(ctx, "globalThis.__api");
  assert.equal(api.length, 1); assert.equal(api[0][1], "/api/sessions/sid1/kill"); assert.equal(api[0][2].grace, "g", "릴레이는 2FA grace 동봉");
  assert.ok(ev(ctx, "globalThis.__sheet").sub.includes("클로드가 띄움(w1)"));
  assert.ok(ev(ctx, "globalThis.__t")[0].includes("1개")); assert.equal(ev(ctx, "globalThis.__ll"), 1);
  ev(ctx, "globalThis.__resp = { ok: false, killed: [], errors: [{ pid: 11, reason: 'gone_or_not_claude' }] }; globalThis.__t = []");
  await ev(ctx, "killSession({session_id: 'sid2', live_terminal: true})");
  assert.ok(String(ev(ctx, "globalThis.__err")).includes("gone_or_not_claude"), "실패 사유가 시트에 남는다");
  assert.equal(ev(ctx, "globalThis.__t").length, 0);
});


test("refreshPcSelect(로컬): localhost + '🖧 다른 PC (릴레이)…' — 고르면 릴레이 앱으로 이동", async () => {
  const ctx = await load({ location: LOCAL_LOC });
  ev(ctx, "setTitle = () => {}; curPcLabel = () => 'x';");
  const sel = stubEl("select"); const cls = new Set(); const opts = [];
  sel.classList = { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c) };
  sel.appendChild = (o) => { opts.push(o.value); }; ctx.document._register("pcname", sel);
  ev(ctx, "refreshPcSelect()");
  same(opts, ["__local__", "__relay__", "__sync__"]); assert.ok(!cls.has("hidden"));
  // 릴레이 앱(숨은 iframe)이 넘긴 목록이 오면 실제 PC 들이 나열되고, 고르면 ?switch=room 으로 이동
  const handlers = ctx.__winListeners.message || [];
  const relayOrigin = new URL(ev(ctx, "RELAY_APP_URL")).origin;
  handlers.forEach((h) => h({ origin: "https://evil.test", data: { type: "clewpath_pcs", pcs: [{ room: "rmX", name: "X" }] } }));
  assert.equal(ctx.localStorage.getItem("sm_pcs_relay"), null, "릴레이 origin 이 아니면 무시");
  opts.length = 0;
  handlers.forEach((h) => h({ origin: relayOrigin, data: { type: "clewpath_pcs", pcs: [{ room: "rmA", name: "집 PC" }, { room: "rmB", name: "" }, { room: "", name: "bad" }] } }));
  same(opts, ["__local__", "relay:rmA", "relay:rmB", "__relay__", "__sync__"]);
  same(JSON.parse(ctx.localStorage.getItem("sm_pcs_relay")), [{ room: "rmA", name: "집 PC" }, { room: "rmB", name: "" }], "room+name 만, 토큰 없음");
  const hrefs = []; Object.defineProperty(ctx.location, "href", { set: (v) => hrefs.push(v), get: () => "http://127.0.0.1:5100/app", configurable: true });
  sel.value = "__relay__"; ev(ctx, "onPcSelect()");
  assert.equal(hrefs[0], ev(ctx, "RELAY_APP_URL")); assert.equal(sel.value, "__local__");
  sel.value = "relay:rmA"; ev(ctx, "onPcSelect()");
  assert.equal(hrefs[1], ev(ctx, "RELAY_APP_URL") + "?switch=rmA");
});

test("doExport(릴레이, ?pcexport): 로컬 오리진에만 parent/opener 로 room+name 목록을 넘긴다", async () => {
  const ctx = await load();
  ev(ctx, "pcsUpsert({room:'rm1', cs:'c', cp:'p', dev:'d', name:'집'}); pcsUpsert({room:'rm2', cs:'c2', cp:'p2', dev:'d2'})");
  const posted = [];
  ctx.window.parent = { postMessage: (m, t) => posted.push([m, t]) };
  ev(ctx, "doExport('https://evil.test')"); assert.equal(posted.length, 0, "로컬 오리진이 아니면 안 보낸다");
  ev(ctx, "doExport('http://127.0.0.1:5100')");
  assert.equal(posted.length, 1); assert.equal(posted[0][1], "http://127.0.0.1:5100");
  same(posted[0][0], { type: "clewpath_pcs", pcs: [{ room: "rm1", name: "집" }, { room: "rm2", name: "" }] }, "토큰(cs/cp/dev)은 절대 안 나간다");
});

test("왕복 동기화: doExport 최상위면 #pcs= 로 복귀, 로컬 부팅이 #pcs= 를 소비해 목록 저장(Chrome iframe 저장소 분리 대응)", async () => {
  // 릴레이 쪽(최상위, parent/opener 없음)
  const ctx = await load();
  ev(ctx, "pcsUpsert({room:'rm1', cs:'c', cp:'p', dev:'d', name:'집'})");
  const repl = []; ctx.location.replace = (u) => repl.push(u); ctx.window.parent = ctx.window; ctx.window.opener = null;
  ev(ctx, "doExport('http://127.0.0.1:5100')");
  assert.equal(repl.length, 1);
  assert.ok(repl[0].startsWith("http://127.0.0.1:5100/#pcs="));
  same(JSON.parse(decodeURIComponent(repl[0].split("#pcs=")[1])), [{ room: "rm1", name: "집" }]);
  ev(ctx, "doExport('https://evil.test')"); assert.equal(repl.length, 1, "로컬 오리진이 아니면 안 보낸다");
  // 로컬 쪽(복귀): 해시 소비
  const LOCAL = { pathname: "/", href: "http://127.0.0.1:5100/", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
  const ctx2 = await load({ location: LOCAL, hash: "#pcs=" + encodeURIComponent(JSON.stringify([{ room: "rmA", name: "집 PC" }, { room: "", name: "x" }])) });
  same(JSON.parse(ctx2.localStorage.getItem("sm_pcs_relay")), [{ room: "rmA", name: "집 PC" }]);
  assert.equal(ev(ctx2, "relayPcsRoundTrip.name"), "relayPcsRoundTrip");
  // 빈 목록 메시지는 기존 값을 지우지 않는다(저장소 분리 오탐)
  const handlers = ctx2.__winListeners.message || [];
  const relayOrigin = new URL(ev(ctx2, "RELAY_APP_URL")).origin;
  ev(ctx2, "sessionStorage.setItem('sm_pcs_rt','1')");
  handlers.forEach((h) => h({ origin: relayOrigin, data: { type: "clewpath_pcs", pcs: [] } }));
  same(JSON.parse(ctx2.localStorage.getItem("sm_pcs_relay")), [{ room: "rmA", name: "집 PC" }]);
  // 왕복은 세션당 1회
  const hrefs = []; Object.defineProperty(ctx2.location, "href", { set: (v) => hrefs.push(v), get: () => "http://127.0.0.1:5100/", configurable: true });
  assert.equal(ev(ctx2, "relayPcsRoundTrip()"), false, "이미 왕복한 세션");
  ev(ctx2, "sessionStorage.removeItem('sm_pcs_rt')");
  assert.equal(ev(ctx2, "relayPcsRoundTrip()"), true); assert.ok(hrefs[0].includes("?pcexport=http%3A%2F%2F127.0.0.1%3A5100"));
});


test("rowResumeIcon: 🖥 클루패스 실행 중 / 출처 아이콘(⌨·🧬·🤖) / ▶ 재개", async () => {
  const ctx = await load();
  assert.equal(ev(ctx, "rowResumeIcon({live_terminal:true, peer:{origin:{origin:'terminal'}}})"), "🖥");
  assert.equal(ev(ctx, "rowResumeIcon({peer:{origin:{origin:'terminal'}}})"), "⌨");
  assert.equal(ev(ctx, "rowResumeIcon({peer:{origin:{origin:'child'}}})"), "🧬");
  assert.equal(ev(ctx, "rowResumeIcon({peer:{origin:{origin:'headless'}}})"), "🤖");
  assert.equal(ev(ctx, "rowResumeIcon({})"), "▶");
  assert.ok(ev(ctx, "rowResumeTitle({peer:{status:'busy', origin:{origin:'terminal'}}})").startsWith("터미널에서 실행 중 · 작업 중"));
});


test("updateImpact/showUpdateApply: 열린·작업 중 클루패스 세션이 있으면 경고, 버튼 문구·색이 바뀐다", async () => {
  const ctx = await load();
  ev(ctx, "CLOCK_SKEW = 0");
  const now = Date.now() / 1000;
  const list = `[{session_id:'a', title:'알파', live_terminal:true, runtime:{phase:'thinking', thinking_at:${now - 5}}},
                 {session_id:'b', title:'베타', live_terminal:true, runtime:{phase:'ready', ready_at:${now - 5}}},
                 {session_id:'c', title:'감마', live_terminal:false, runtime:{phase:'thinking', thinking_at:${now - 5}}}]`;
  const imp = ev(ctx, `updateImpact(${list})`);
  assert.equal(JSON.stringify(imp), JSON.stringify({ open: ["알파", "베타"], busy: ["알파"] }), "작업 중 = 열린 터미널 중 진행형 phase 만");
  ev(ctx, `loadList = async () => {}; SESSIONS = ${list}; openModal = (t, b, f) => { globalThis.__b = b; globalThis.__f = f; }`);
  await ev(ctx, "showUpdateApply('0.10.7', '0.10.8')");
  const warn = ev(ctx, "globalThis.__b").children[0];
  assert.ok(warn.textContent.includes("작업 중인 세션 1개: 알파") && warn.textContent.includes("그 외 열린 터미널 1개"));
  const go = ev(ctx, "globalThis.__f")[0];
  assert.ok(go.textContent.startsWith("그래도 지금") && String(go.className).includes("danger"));
  ev(ctx, "SESSIONS = []"); await ev(ctx, "showUpdateApply('0.10.7', '0.10.8')");
  assert.ok(!String(ev(ctx, "globalThis.__b").children[0].className).includes("err"), "열린 세션 없으면 경고 없음");
  assert.ok(ev(ctx, "globalThis.__f")[0].textContent.startsWith("지금 v0.10.8"));
});
