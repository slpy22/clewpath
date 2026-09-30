// PWA 테스트 Phase 4(2026-09-28): 릴레이 RPC(Conn) 를 이벤트 구동 WebSocket 스텁으로 · 세션 상세(loadDetail)·재개 선택 모달.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const A = "aaaaaaaa-1111-2222-3333-444444444444";
const tick = () => new Promise((r) => setTimeout(r, 0));

// 이벤트 구동 WebSocket 스텁: 보낸 프레임을 기록하고, 테스트가 서버 프레임을 밀어 넣는다.
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
async function connected(ctx, sockets, { peer = true } = {}) {
  const p = ev(ctx, "conn.connect('room1', 'tok')");
  await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: peer, cid: "cid1" });
  await p; await tick();
  return ws;
}

// ---------------------------------------------------------------- Conn
test("Conn: connect(hello)→상태 콜백, req 는 id 로 응답 매칭, 소켓 없으면 즉시 disconnected", async () => {
  const ctx = await load();
  const sockets = fakeWs(ctx);
  ev(ctx, "globalThis.__st = []; conn.onstatus = (up, peer) => globalThis.__st.push([up, peer])");
  same(await ev(ctx, "conn.req('ping')"), { ok: false, error: "disconnected" }, "연결 전 요청은 영원히 기다리지 않는다");
  const ws = await connected(ctx, sockets);
  assert.ok(ws.url.startsWith("wss://test.local/relay/ws?role=client&room=room1&v=1&token=tok"));
  same(ev(ctx, "globalThis.__st"), [[true, true]]);
  assert.equal(ev(ctx, "conn.cid"), "cid1");
  const p1 = ev(ctx, "conn.req('list_sessions', {limit: 0})");
  const p2 = ev(ctx, "conn.req('get_stats', {session_id: 'x'})");
  await tick();
  same(ws.sent.map((f) => [f.type, f.id, f.method]), [["req", "c1", "list_sessions"], ["req", "c2", "get_stats"]], "연결 전 실패한 요청은 seq 를 쓰지 않는다");
  ws.push({ type: "res", id: "c2", ok: true, data: { n: 2 } });      // 순서 뒤바뀐 응답
  ws.push({ type: "res", id: "c1", ok: true, data: { sessions: [] } });
  same((await p2).data, { n: 2 }); same((await p1).data, { sessions: [] });
  ws.push({ type: "res", id: "c999", ok: true });                     // 모르는 id 는 무시
  ws.push({ type: "peer", event: "agent_offline" }); ws.push({ type: "peer", event: "agent_online" });
  same(ev(ctx, "globalThis.__st").slice(1), [[true, false], [true, true]]);
  // api(): HTTP 상태를 예외로, 정상은 json 만
  const pa = ev(ctx, "conn.api('GET', '/api/x').then(v => ['ok', v], e => ['err', e.message, e.status])");
  await tick(); ws.push({ type: "res", id: ws.sent.at(-1).id, ok: true, data: { status: 200, json: { a: 1 } } });
  same(await pa, ["ok", { a: 1 }]);
  const pb = ev(ctx, "conn.api('POST', '/api/y').then(v => ['ok', v], e => ['err', e.message, e.status])");
  await tick(); ws.push({ type: "res", id: ws.sent.at(-1).id, ok: false, data: { status: 404, json: { error: "nf" } } });
  same(await pb, ["err", "HTTP 404", 404]);
  ev(ctx, "conn._stopped = true"); ws.close();
});

test("Conn: 터미널/모니터 스트림 라우팅, close 는 stream_close, 소켓 끊기면 waiter 거부 + 모든 스트림 eof + 재연결 예약", async () => {
  const ctx = await load();
  const sockets = fakeWs(ctx);
  const ws = await connected(ctx, sockets);
  ev(ctx, "globalThis.__term = []; globalThis.__mon = [];"
        + "globalThis.__h = conn.startTerminal('" + A + "', (d, eof, err) => globalThis.__term.push([d, eof, err]), {grace: 'g', screen: 'S1', safe: true});"
        + "globalThis.__m = conn.startMonitor('a,b', 'a', (d, eof, err) => globalThis.__mon.push([d, eof, err]))");
  const [ft, fm] = ws.sent.slice(-2);
  assert.equal(ft.method, "terminal"); same(ft.params, { session_id: A, skip: false, grace: "g", otp: "", screen: "S1" }, "safe → skip:false, screen 커서 동봉");
  assert.equal(fm.method, "monitor"); same(fm.params, { ids: "a,b", manager: "a" });
  ws.push({ type: "stream", id: ft.id, data: "hello" });
  ws.push({ type: "stream", id: fm.id, data: "{\"type\":\"heartbeat\"}" });
  ws.push({ type: "stream", id: "t404", data: "x" });                 // 모르는 스트림 무시
  await tick();                                                        // 수신은 _rxq 직렬화(비동기)
  same(ev(ctx, "globalThis.__term"), [["hello", false, undefined]]);
  same(ev(ctx, "globalThis.__mon"), [["{\"type\":\"heartbeat\"}", false, undefined]]);
  ev(ctx, "globalThis.__h.send({type: 'input', data: 'ls'}); globalThis.__m.add('c', 'sub'); globalThis.__m.remove('b')");
  same(ws.sent.slice(-3).map((f) => [f.type, f.id, f.data]), [
    ["stream_in", ft.id, { type: "input", data: "ls" }], ["stream_in", fm.id, { type: "add", session_id: "c", role: "sub" }],
    ["stream_in", fm.id, { type: "remove", session_id: "b" }]]);
  ev(ctx, "globalThis.__m.close()");                                  // 모니터 close 는 핸들러만
  ws.push({ type: "stream", id: fm.id, data: "late" }); await tick();
  assert.equal(ev(ctx, "globalThis.__mon.length"), 1);
  // 서버가 2fa 로 스트림을 끝냄 → eof + error 전달
  ws.push({ type: "stream", id: ft.id, eof: true, error: "2fa_invalid" }); await tick();
  same(ev(ctx, "globalThis.__term").at(-1), [null, true, "2fa_invalid"]);
  // 터미널 close → 서버에 stream_close(배경 탭 detach 계약)
  ev(ctx, "globalThis.__h.close()");
  same(ws.sent.at(-1), { v: 1, type: "stream_close", id: ft.id });
  // 새 스트림 + 미해결 요청을 둔 채 소켓이 끊기면: waiter 는 disconnected, 스트림은 eof, 재연결 예약
  ev(ctx, "globalThis.__term2 = []; conn.startTerminal('" + A + "', (d, eof) => globalThis.__term2.push([d, eof]), {})");
  const pend = ev(ctx, "conn.req('ping')");
  ev(ctx, "globalThis.__sched = []; conn._scheduleReconnect = (code) => globalThis.__sched.push(code)");
  ws.close(4402);
  same(await pend, { ok: false, error: "disconnected" });
  same(ev(ctx, "globalThis.__term2"), [[null, true]], "죽은 스트림을 살아있다고 착각하지 않게 eof");
  assert.equal(ev(ctx, "conn.streams.size"), 0); assert.equal(ev(ctx, "conn.waiters.size"), 0);
  same(ev(ctx, "globalThis.__sched"), [4402]);
});

test("Conn: 기기 인증 — devToken 있으면 hello 직후 auth, 성공 시 ver 표시·pv 경고 1회, auth_required 는 onauth(false)", async () => {
  const ctx = await load();
  ctx.localStorage.setItem("sm_devtoken", "dev-1");
  ev(ctx, "conn.devToken = 'dev-1'; globalThis.__auth = []; conn.onauth = (ok, err) => globalThis.__auth.push([ok, err]);"
        + "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m); showVer = (c) => { globalThis.__ver = c; }; refreshVer = async () => {}");
  const sockets = fakeWs(ctx);
  const p = ev(ctx, "conn.connect('room1', 'tok')");
  await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true });
  await tick();
  const af = ws.sent.at(-1);
  assert.equal(af.method, "auth"); assert.equal(af.params.token, "dev-1"); assert.equal(af.params.pv, 2);
  assert.equal(typeof af.params.name, "string", "0.10.0: 폰이 자기 이름을 보고한다(B-1)");
  let settled = false; p.then(() => { settled = true; });
  await tick(); assert.equal(settled, false, "auth 가 끝나야 connect 가 풀린다");
  ws.push({ type: "res", id: af.id, ok: true, data: { id: "d1", name: "폰", ver: "0.9.5", pv: 3 } });
  await p; await tick();
  assert.equal(ev(ctx, "conn.authed"), true); assert.equal(ev(ctx, "globalThis.__ver"), "0.9.5");
  same(ev(ctx, "globalThis.__auth"), [[true, undefined]]);
  assert.ok(ev(ctx, "globalThis.__toasts")[0].includes("더 새 통신 규약"), "Host pv 가 높으면 업데이트 안내");
  // 이후 아무 응답이 auth_required 면(폐기 기기) onauth(false)
  const q = ev(ctx, "conn.req('list_sessions')"); await tick();
  ws.push({ type: "res", id: ws.sent.at(-1).id, ok: false, error: "auth_required" });
  await q;
  same(ev(ctx, "globalThis.__auth").at(-1), [false, "auth_required"]);
  ev(ctx, "conn._stopped = true"); ws.close();
});

test("Conn: 재연결 백오프(1s→2s→…→30s, 6회 상한)·타이머 중복 없음·치명 코드는 중단·성공 시 목록+전경 탭 복원", async () => {
  const ctx = await load();
  const timers = [];
  const realTimeout = ctx.setTimeout;
  ctx.setTimeout = (f, ms) => { timers.push({ f, ms }); return timers.length; };
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);"
        + "globalThis.__ll = 0; loadList = async () => { globalThis.__ll++; }; globalThis.__rt = 0; reattachForegroundTab = () => { globalThis.__rt++; };"
        + "globalThis.__jwt = 0; clientJwt = async (force) => { if (force) globalThis.__jwt++; return null; }");
  ev(ctx, "conn._retry = 0; conn._retryTimer = null; conn._stopped = false");
  ev(ctx, "conn._scheduleReconnect(4403)");
  assert.equal(timers.length, 0, "재페어링이 필요한 코드는 재시도하지 않는다");
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("재페어링"));
  ev(ctx, "conn._scheduleReconnect(4426)"); assert.equal(timers.length, 0);
  // 백오프 열: 실패를 거듭할수록 1000·2000·4000·8000·16000·30000(캡)·30000(회수 상한 6)
  const waits = [];
  for (let i = 0; i < 7; i++) {
    ev(ctx, "conn._scheduleReconnect(1006)");
    waits.push(timers.at(-1).ms);
    ev(ctx, "conn._scheduleReconnect(1006)");                          // 같은 사이클 재호출은 무시
    assert.equal(timers.length, i + 1, "타이머 중복 없음");
    ev(ctx, "conn._retryTimer = null");                                // 타이머가 돌아 스스로 지운 상태로
  }
  same(waits, [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  // 타이머 콜백: 4402(자격 만료 추정) 는 JWT 강제 갱신 → connect 성공 → 카운터 리셋·목록·전경 탭
  ev(ctx, "conn._retry = 3; conn.connect = async () => {}");
  ev(ctx, "conn._scheduleReconnect(4402)");
  await timers.at(-1).f(); await tick();
  assert.equal(ev(ctx, "globalThis.__jwt"), 1); assert.equal(ev(ctx, "conn._retry"), 0);
  assert.equal(ev(ctx, "globalThis.__ll"), 1); assert.equal(ev(ctx, "globalThis.__rt"), 1);
  assert.equal(ev(ctx, "globalThis.__toasts").at(-1), "다시 연결됨");
  // connect 실패 → 다시 예약(다음 백오프)
  ev(ctx, "conn.connect = async () => { throw new Error('x'); }");
  const n = timers.length;
  ev(ctx, "conn._scheduleReconnect(1006)"); await timers.at(-1).f(); await tick();
  assert.equal(timers.length, n + 2, "실패하면 다음 타이머를 건다");
  // 로그아웃 뒤에는 아무것도 예약하지 않는다
  ev(ctx, "conn._retryTimer = null; conn.logout()");
  const n2 = timers.length; ev(ctx, "conn._scheduleReconnect(1006)"); assert.equal(timers.length, n2);
  ctx.setTimeout = realTimeout;
});

test("Conn E2EE: 룸 키가 있으면 _tx 는 봉투(e:'a1')만 보내고, 봉투 수신은 복호 뒤 디스패치, 키 없이 온 봉투는 무시", async () => {
  const ctx = await load();
  const raw = new Uint8Array(32); for (let i = 0; i < 32; i++) raw[i] = i * 7;
  const b64u = Buffer.from(raw).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  ctx.localStorage.setItem("sm_e2ee_room1", b64u);
  const sockets = fakeWs(ctx);
  const ws = await connected(ctx, sockets);
  assert.ok(ev(ctx, "!!conn.e2eeKey"), "페어링 때 저장한 키를 로드");
  const p = ev(ctx, "conn.req('list_sessions', {limit: 0})");
  await tick(); await tick();
  const env = ws.sent.at(-1);
  assert.equal(env.e, "a1"); assert.ok(env.p && !("method" in env), "평문 메서드가 밖으로 나가지 않는다");
  const inner = await ev(ctx, "conn._openEnv(" + JSON.stringify(env) + ")");
  assert.equal(inner.method, "list_sessions"); assert.equal(inner.id, "c1");
  // 서버 응답도 봉투로: 봉인 → push → 복호 → waiter
  const sealed = await ev(ctx, "conn._seal({type: 'res', id: 'c1', ok: true, data: {sessions: [1]}})");
  ws.push(sealed);
  same((await p).data, { sessions: [1] });
  // 키 없이 온 봉투는 버린다(대기 중 요청은 그대로)
  ev(ctx, "conn.e2eeKey = null");
  const p2 = ev(ctx, "conn.req('ping')"); await tick();
  const id2 = ws.sent.at(-1).id;
  ws.push({ v: 1, e: "a1", p: "AAAA" }); await tick(); await tick();
  assert.equal(ev(ctx, "conn.waiters.size"), 1);
  ws.push({ type: "pong", id: id2 }); same(await p2, { type: "pong", id: id2 });
  ev(ctx, "conn._stopped = true"); ws.close();
});

// ---------------------------------------------------------------- 세션 상세 + 재개 선택
function pinPaneDetail(ctx) {
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  return pd;
}
const byClass = (el, c) => el.children.filter((k) => String(k.className) === c);
const kvOf = (kv) => { const o = {}; for (let i = 0; i < kv.children.length; i += 2) o[kv.children[i].textContent] = kv.children[i + 1].textContent; return o; };

test("loadDetail: 정보 카드 kv·⚙ 안내 카드 토글(picker-expose API)·통계 카드(성공/실패)·버튼 문구(실행 중이면 🖥)", async () => {
  const ctx = await load();
  const pd = pinPaneDetail(ctx);
  ev(ctx, "globalThis.__api = []; T.api = async (v, p, o) => { globalThis.__api.push([v, p, o && o.body]);"
        + "  if (p.endsWith('/stats')) return { model: 'claude-opus-5', tokens: { input_tokens: 12345, output_tokens: 6, cache_read_input_tokens: 7 }, est_cost_usd: 0.12 }; return {}; };"
        + "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m); CURRENT_TERM_SID = 'zzz'");
  const s = { session_id: A, title: "알파", project_folder: "F--p1", message_count: 4, size_bytes: 3 * 1024 * 1024, cwd: "F:/p1",
              started_at: "2026-09-01T00:00:00Z", ended_at: new Date(Date.now() - 120000).toISOString(), picker_hidden: true, picker_expose: false, live_terminal: false };
  await ctx.loadDetail(s);
  assert.equal(ev(ctx, "SELECTED_ID"), A); assert.equal(ev(ctx, "CURRENT_TERM_SID"), null, "상세로 나오면 터미널 화면 표시 해제");
  assert.ok(!pd.classList.contains("term-mode"));
  const [bar, detail] = pd.children;
  assert.equal(String(bar.className), "backbar tolist"); assert.equal(bar.children[0].textContent, "‹ 목록");
  const cards = byClass(detail, "card");
  assert.equal(cards.length, 3, "정보·⚙ 안내·통계");
  assert.equal(cards[0].children[0].textContent, "알파");
  const kv = kvOf(cards[0].children[1]);
  assert.equal(kv["ID"], A.slice(0, 8)); assert.equal(kv["폴더"], "F--p1"); assert.equal(kv["메시지"], "4"); assert.equal(kv["크기"], "3.0MB");
  assert.ok(kv["생성"] && kv["마지막"].endsWith("(2분 전)"));
  assert.equal(cards[0].children[2].textContent, "F:/p1");
  // ⚙ 카드: 토글 → API POST {expose:true} → 문구 반전 → 다시 → false
  const tog = cards[1].children.find((k) => String(k.className) === "btn ghost");
  assert.ok(tog.textContent.startsWith("👌"));
  await tog.onclick();
  same(ev(ctx, "globalThis.__api").find((c) => c[1].includes("picker-expose")), ["POST", `/api/sessions/${A}/picker-expose`, { expose: true }]);
  assert.equal(s.picker_expose, true); assert.ok(tog.textContent.startsWith("🔔"));
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("경고 없이"));
  await tog.onclick(); assert.equal(s.picker_expose, false);
  // 통계 카드
  const st = kvOf(cards[2].children[1].children[0]);
  assert.equal(st["모델"], "claude-opus-5"); assert.equal(st["입력토큰"], (12345).toLocaleString()); assert.equal(st["환산비용"], "$0.12");
  // 버튼
  const act = byClass(detail, "actions")[0];
  same(act.children.map((b) => b.textContent), ["▶ 재개", "🔱 포크로 재개", "✎ 이름", "💬 뷰어", "🛡 API 정책", "📁 폴더 변경", "📤 내보내기", "🗑 삭제"]);
  assert.equal(String(act.children.at(-1).className), "btn danger full");
  // 실행 중 세션 + 통계 실패 + picker_hidden 아님
  ev(ctx, "T.api = async () => { throw new Error('boom'); }");
  await ctx.loadDetail({ session_id: A, slug: "s", live_terminal: true, size_bytes: 0 });
  const detail2 = pd.children[1];
  assert.equal(byClass(detail2, "card").length, 2);
  assert.equal(byClass(detail2, "actions")[0].children[0].textContent, "🖥 터미널로 돌아가기 (실행 중)");
  assert.equal(byClass(detail2, "card")[1].children[1].children[0].textContent, "통계 실패: boom");
});

test("showResumeChooser: 실행 중이면 바로 터미널 / ⚙ 사전 확인 / bg 잠금 / 동시 재개 경고 / 선택 저장 후 web·term 분기", async () => {
  const ctx = await load();
  ev(ctx, "CLOCK_SKEW = 0; globalThis.__calls = [];"
        + "loadTerminal = (s, safe) => globalThis.__calls.push(['term', s.session_id, !!safe]);"
        + "loadResume = (s, inplace, safe) => globalThis.__calls.push(['web', s.session_id, inplace, !!safe]);"
        + "showBgHold = (s) => globalThis.__calls.push(['bghold', s.session_id]);"
        + "openModal = (t, b, f) => { globalThis.__modal = { t, b, f }; }; closeModal = () => {}");
  const calls = () => ev(ctx, "globalThis.__calls");
  ctx.showResumeChooser({ session_id: A, live_terminal: true });
  same(calls(), [["term", A, false]]);
  // ⚙ 세션 사전 확인: 취소하면 모달 없음
  let asked = []; ctx.confirm = (m) => { asked.push(m.slice(0, 2)); return false; };
  ev(ctx, "globalThis.__modal = null");
  ctx.showResumeChooser({ session_id: A, picker_hidden: true });
  same(asked, ["⚙ "]); assert.equal(ev(ctx, "globalThis.__modal"), null);
  ctx.confirm = () => true;
  ctx.showResumeChooser({ session_id: A, picker_hidden: true, picker_expose: true });  // 알고 있음 → 안 묻는다
  assert.equal(asked.length, 1); assert.ok(ev(ctx, "globalThis.__modal"));
  // 기본 선택(web + YOLO) → ▶ 재개 → loadResume(s, true, safe=false), 선택은 LS 에 저장
  ev(ctx, "globalThis.__modal.f[0].onclick()");
  same(calls().at(-1), ["web", A, true, false]);
  same(JSON.parse(ctx.localStorage.getItem("sm_resume_pref")), { how: "web", yolo: true });
  // bg 잠금 → 재개 대신 안내
  ctx.showResumeChooser({ session_id: A, agent: { kind: "background", name: "bg" } });
  ev(ctx, "globalThis.__modal.f[0].onclick()");
  same(calls().at(-1), ["bghold", A]);
  // 밖에서 사용 중(피어 있음) → 경고, 취소하면 재개 없음
  ctx.confirm = (m) => { asked.push(m.slice(0, 2)); return false; };
  ctx.showResumeChooser({ session_id: A, peer: { name: "w" }, live_terminal: false });
  const n = calls().length; ev(ctx, "globalThis.__modal.f[0].onclick()");
  assert.equal(calls().length, n); assert.equal(asked.at(-1), "⚠ ");
  // 터미널 + 일반(권한 확인) 선택 → loadTerminal(s, safe=true)
  ctx.localStorage.setItem("sm_resume_pref", JSON.stringify({ how: "term", yolo: false }));
  ctx.showResumeChooser({ session_id: A });
  ev(ctx, "globalThis.__modal.f[0].onclick()");
  same(calls().at(-1), ["term", A, true]);
  // 모달 안 세그먼트 버튼: 🖥 터미널 / ⚡ YOLO 를 눌러 바꾸면 그 조합으로 재개
  ctx.showResumeChooser({ session_id: A });
  const grid = ev(ctx, "globalThis.__modal.b").children[0];
  const btn = (label) => grid.children.flatMap((r) => r.children || []).find((b) => b.textContent === label);
  btn("⚡ YOLO (자동 허용)").onclick();
  ev(ctx, "globalThis.__modal.f[0].onclick()");
  same(calls().at(-1), ["term", A, false]);
  same(JSON.parse(ctx.localStorage.getItem("sm_resume_pref")), { how: "term", yolo: true });
});
