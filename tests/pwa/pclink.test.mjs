// pclink T7(2026-10-23): PWA 화면 — 설정 「🖥 PC 짝(원격 묻기)」(짝·초대·붙여넣기·해제·모두 끄기·최근 기록),
// 상세 칸 「📡 원격 묻기 받기」, pclink 승인 시트(폰에서도 2차 인증 칸·결정 즉시 실행), 📱 기기 목록의 PC 짝 배지.
// 폰(릴레이)에서 '켜는' 동작은 2차 인증(grace/otp)을 싣고 '끄는' 동작은 싣지 않는다(D11).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const NOW = Math.floor(Date.now() / 1000);
const H1 = "a1".repeat(16), H2 = "b2".repeat(16), H3 = "c3".repeat(16);
const INV = "aaaa1111bbbb2222";
const STATUS = {
  host_id: "abcdef0123456789abcdef0123456789", name: "데스크톱", paused: false, room_ready: true,
  pairs: [
    { host: H1, name: "노트북", state: "active", role: "inviter", created: NOW - 9000, last_ok: NOW - 120, peer_ver: "0.33.0", device_id: "d1" },
    { host: H2, name: "사무실", state: "cleanup", role: "invitee", created: NOW - 9000, last_ok: null, peer_ver: null, device_id: null },
    { host: H3, name: "옛PC", state: "revoked", role: "inviter", created: NOW - 9000, last_ok: NOW - 86400 * 3, peer_ver: "0.30.0", device_id: "d3" },
  ],
  invites: [{ inv: INV, state: "open", exp: NOW + 5 * 3600, created: NOW - 60 }, { inv: "9999000011112222", state: "used", exp: NOW + 100, created: NOW - 99 }],
  sessions: [{ id: "sid-1", title: "배포" }],
};
const AUDIT = { events: [
  { ts: NOW - 300, event: "paired", peer: H1, role: "inviter" },
  { ts: NOW - 60, event: "ask", peer: H1, session: "sid-1", code: "answered" },
] };
const BASE = {
  "GET /api/owner/pclink": STATUS, "GET /api/owner/pclink/audit": AUDIT,
  "GET /api/owner/2fa/status": { required: true, has_secret: true },
};
const clone = (o) => JSON.parse(JSON.stringify(o));

async function boot(local, data) {
  const ctx = await load(local ? { location: LOCAL_LOC } : {});
  ctx.__DATA = clone({ ...BASE, ...(data || {}) });
  ev(ctx, `globalThis.__calls = []; globalThis.__modals = []; globalThis.__toasts = []; globalThis.__grants = []; globalThis.__clip = null;
    toast = (m) => __toasts.push(m);
    openModal = (title, body, foot) => { __modals.push({ title, body, foot }); }; closeModal = () => {};
    navigator.clipboard = { writeText: async (t) => { globalThis.__clip = t; } };
    T = { api: async (verb, path, o) => { __calls.push([verb, path, o]); const d = globalThis.__DATA[verb + ' ' + path];
        if (d === undefined) throw new Error('404 ' + path);
        if (d && d.__err) { const e = new Error('HTTP ' + (d.__status || 400)); e.status = d.__status || 400; e.json = d.__err; throw e; }
        if (d && d.__throw) throw new Error(d.__throw);
        return JSON.parse(JSON.stringify(d)); },
      grant: async (otp) => { __grants.push(otp); const g = globalThis.__DATA['GRANT'];
        if (g && g.__throw) throw new Error(g.__throw); return JSON.parse(JSON.stringify(g || {})); } };`);
  return ctx;
}
const tick = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const lastModal = (ctx) => ev(ctx, "globalThis.__modals.at(-1)");
const calls = (ctx, verb, path) => ev(ctx, "globalThis.__calls").filter((c) => c[0] === verb && (path instanceof RegExp ? path.test(c[1]) : c[1] === path));
const toasts = (ctx) => ev(ctx, "globalThis.__toasts");
const rowWith = (body, s) => body.children.find((c) => String(c.className).startsWith("devrow") && (c.text || "").includes(s));
const btnOf = (row, label) => row.children[1].children.find((b) => b.textContent === label);
const same = (a, b) => assert.equal(JSON.stringify(a), JSON.stringify(b));
// 확인 시트의 동사 버튼 — 하네스 스텁은 정하지 않은 disabled 를 함수(참)로 돌려주므로 브라우저처럼 false 로 두고 누른다
const go = (m) => { m.foot[1].disabled = false; return m.foot[1].onclick(); };

const PAIR_AP = { id: "ap_p", kind: "pclink_pair", args: { action: "invite" }, status: "pending", created: NOW, expires: NOW + 1800, requested_by: "owner",
  summary: "PC 짝짓기 초대 만들기 — 1회용·24시간. 초대 문자열을 받은 PC 가 이 PC 의 「원격 묻기 받기」 세션에 물을 수 있게 됩니다" };
const CONTACT_AP = { id: "ap_c", kind: "pclink_contact", status: "pending", created: NOW, expires: NOW + 1800, requested_by: "pclink:" + H1,
  args: { peer: H1, peer_name: "노트북", session_id: "sid-1", title: "배포" },
  summary: "원격 묻기 허용: PC '노트북' 가 세션 [배포] 의 지금과 앞으로의 대화 전체를 30일 동안 물어볼 수 있게 합니다(답만 돌려주고 이 PC 에서는 아무것도 실행하지 않음)" };

test("설정 「🖥 PC 짝」: 내 PC·끄기 스위치·짝(상태 배지 cleanup·revoked 문구)·열린 초대만·받는 세션·최근 기록 · 첫 화면 줄에 지금 상태", async () => {
  const ctx = await boot(true);
  await ev(ctx, "showPclink()"); await tick();
  const m = lastModal(ctx);
  assert.equal(m.title, "🖥 PC 짝(원격 묻기)");
  const t = m.body.text;
  assert.ok(t.includes("🖥 데스크톱") && t.includes("PC 번호 abcdef01"), "내 PC 이름·host_id 앞 8자");
  assert.ok(t.includes("▶ 원격 묻기 받는 중"));
  assert.ok(!t.includes("릴레이)이 꺼져") && !t.includes("2차 인증이 꺼져"), "room_ready·2FA 가 괜찮으면 경고 없음");
  const p1 = rowWith(m.body, "노트북"), p2 = rowWith(m.body, "사무실"), p3 = rowWith(m.body, "옛PC");
  assert.ok(p1.text.includes("🔗 짝 맺음") && p1.text.includes("마지막 연결 2분 전") && p1.text.includes("내가 초대") && p1.text.includes("v0.33.0"));
  assert.ok(p2.text.includes("상대 쪽 정리 대기") && p2.text.includes("상대 PC 가 켜지면 정리") && p2.text.includes("아직 연결 없음"));
  assert.ok(p3.text.includes("⛔ 기기 목록에서 폐기됨 — 쓸 수 없음") && p3.text.includes("해제로 정리"));
  assert.notEqual(btnOf(p3, "해제").disabled, true, "폐기·정리 대기 짝도 해제는 누를 수 있다");
  const inv = rowWith(m.body, "🎟 초대 aaaa11");
  assert.ok(inv && inv.text.includes("시간 남음"));
  same(inv.children[1].children.map((b) => b.textContent), ["보기", "취소"]);
  assert.ok(!t.includes("초대 999900"), "쓴 초대는 목록에 없다");
  assert.ok(rowWith(m.body, "배포"), "받는 세션");
  const acts = m.body.children.find((c) => c.className === "actions");
  same(acts.children.map((b) => [b.textContent, b.disabled === true]), [["＋ 초대 만들기", false], ["📥 초대 붙여넣기", false]]);
  // 최근 기록: 최신이 위, 짝 이름으로
  const log = m.body.children.at(-1);
  same(calls(ctx, "GET", "/api/owner/pclink/audit")[0][2], { query: { n: 30 } });
  assert.ok(log.children[0].textContent.includes("📨 받은 물음") && log.children[0].textContent.includes("PC '노트북'") && log.children[0].textContent.includes("answered"));
  assert.ok(log.children[1].textContent.includes("🔗 짝 맺음"));
  // 설정 첫 화면 줄: 지금 상태(정리할 것이 있으면 주황)
  ev(ctx, "openModal = (t, b) => { globalThis.__sb = b; }; pushSupported = () => false; showSettings()");
  await tick();
  const row = ev(ctx, "globalThis.__sb.children.find(n => String(n.className).startsWith('setrow') && n.text.includes('PC 짝(원격 묻기)'))");
  assert.ok(row.text.includes("짝 3개 (정리할 것 2) · 받는 세션 1개"), row.text);
  assert.ok(row.classList.contains("warn"));
});

test("room_ready false → 짝짓기 불가 안내·초대 버튼 잠금 · 2FA 비밀 없음 → 승인 불가 경고 · 모두 꺼 둠 표시 · 폰에서도 설정 줄이 보인다", async () => {
  const ctx = await boot(true, { "GET /api/owner/pclink": { ...STATUS, room_ready: false, paused: true, pairs: [], invites: [], sessions: [] },
    "GET /api/owner/2fa/status": { required: false, has_secret: false } });
  await ev(ctx, "showPclink()"); await tick();
  const m = lastModal(ctx), t = m.body.text;
  assert.ok(t.includes("원격 연결(릴레이)이 꺼져 있어 짝짓기를 할 수 없습니다"));
  assert.ok(t.includes("2차 인증이 꺼져 있어 짝짓기·첫 연락을 승인할 수 없습니다"));
  assert.ok(t.includes("⏸ 원격 묻기 모두 꺼 둠") && t.includes("아직 짝지은 PC 가 없습니다"));
  const acts = m.body.children.find((c) => c.className === "actions");
  assert.ok(acts.children.every((b) => b.disabled === true));
  // 상태를 못 읽으면 화면 안에 이유
  ctx.__DATA["GET /api/owner/pclink"] = { __err: { error: "policy_corrupt" }, __status: 503 };
  await ev(ctx, "showPclink()");
  assert.ok(lastModal(ctx).body.text.includes("PC 짝 설정 파일이 손상"));
  // 폰(릴레이) 설정 첫 화면에도 줄이 있다(로컬 전용 아님)
  const phone = await boot(false);
  ev(phone, "openModal = (t, b) => { globalThis.__sb = b; }; pushSupported = () => false; curPcLabel = () => 'PC'; showSettings()");
  assert.ok(ev(phone, "globalThis.__sb").text.includes("PC 짝(원격 묻기)"));
});

test("초대 만들기(로컬): invite POST → 승인 시트(코드 칸) → 결정 즉시 실행(execute 재호출 없음) → invites GET → 문자열·복사", async () => {
  const ctx = await boot(true, {
    "POST /api/owner/pclink/invite": { approval: PAIR_AP },
    "POST /api/owner/approvals/ap_p/decide": { ...PAIR_AP, status: "executed", result: { inv: INV, exp: NOW + 86400 } },
    [`GET /api/owner/pclink/invites/${INV}`]: { inv: INV, invite: "clewpath-pair:0:SECRETXYZ" } });
  await ev(ctx, "pclinkInvite()");
  same(calls(ctx, "POST", "/api/owner/pclink/invite")[0][2], { body: {} });
  const sheet = lastModal(ctx);
  assert.equal(sheet.title, "✅ 승인 요청");
  assert.ok(sheet.body.text.includes("🖥 PC 짝짓기") && sheet.body.children.some((c) => c.className === "sheet-main" && c.textContent.includes("1회용·24시간")));
  ev(ctx, "globalThis.__modals.at(-1).body.children.find(c => c.placeholder === '2차 인증 6자리').value = '123456'");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "executed");
  same(calls(ctx, "POST", "/api/owner/approvals/ap_p/decide")[0][2].body, { approve: true, otp: "123456" });
  assert.equal(calls(ctx, "POST", /\/execute$/).length, 0, "pclink 는 결정이 곧 실행 — 다시 부르지 않는다");
  await tick(8);
  same(calls(ctx, "GET", `/api/owner/pclink/invites/${INV}`)[0][2], { query: {} });
  const m = lastModal(ctx);
  assert.equal(m.title, "🎟 초대 문자열");
  assert.ok(m.body.text.includes("1회용·24시간") && m.body.text.includes("「초대 붙여넣기」") && m.body.text.includes("비밀번호처럼") && m.body.text.includes("메신저에 남기지 말 것"));
  const ta = m.body.children[2].children[1];
  assert.equal(ta.value, "clewpath-pair:0:SECRETXYZ"); assert.equal(ta.readOnly, true);
  await m.foot[0].onclick();
  assert.equal(ev(ctx, "globalThis.__clip"), "clewpath-pair:0:SECRETXYZ");
  assert.ok(toasts(ctx).includes("초대 문자열을 복사했습니다"));
  assert.ok(calls(ctx, "GET", "/api/owner/pclink").length >= 1, "짝 화면도 새로 읽는다(열린 초대)");
  // 열린 초대 「보기」·「취소」(취소는 확인 없이 바로)
  ctx.__DATA[`POST /api/owner/pclink/invites/${INV}/cancel`] = { cancelled: true };
  await ev(ctx, "showPclink()"); await tick();
  const inv = rowWith(lastModal(ctx).body, "🎟 초대 aaaa11");
  await btnOf(inv, "보기").onclick();
  assert.equal(lastModal(ctx).title, "🎟 초대 문자열");
  await ev(ctx, `showPclink()`); await tick();
  await btnOf(rowWith(lastModal(ctx).body, "🎟 초대 aaaa11"), "취소").onclick();
  same(calls(ctx, "POST", `/api/owner/pclink/invites/${INV}/cancel`)[0][2], undefined);
  assert.ok(toasts(ctx).includes("초대를 취소했습니다"));
  // 실행 실패(릴레이 꺼짐) → 시트 안에 문구
  ctx.__DATA["POST /api/owner/approvals/ap_p/decide"] = { __err: { error: "relay_unavailable", approval: PAIR_AP }, __status: 409 };
  await ev(ctx, "pclinkInvite()");
  ev(ctx, "globalThis.__modals.at(-1).body.children.find(c => c.placeholder === '2차 인증 6자리').value = '123456'");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "error");
  assert.ok(lastModal(ctx).body.text.includes("이 PC 의 원격 연결이 꺼져 있습니다"));
});

test("초대 붙여넣기: 빈 칸은 막음 → accept POST → 승인 시트(상대 PC) → 결정하면 짝짓기 끝 → 'PC 노트북 와 짝을 맺었습니다' · 오류 문구", async () => {
  const AP = { ...PAIR_AP, id: "ap_a", args: { action: "accept", accept_id: "x1", peer_host: H1, peer_name: "노트북" },
    summary: `PC 짝 맺기: '노트북' (${H1.slice(0, 8)}) — 서로의 「원격 묻기 받기」 세션에 물을 수 있게 됩니다` };
  const ctx = await boot(true, { "POST /api/owner/pclink/accept": { approval: AP, peer_name: "노트북" },
    "POST /api/owner/approvals/ap_a/decide": { ...AP, status: "executed", result: { host: H1, name: "노트북" } } });
  const h = ev(ctx, "globalThis.__h = pclinkPaste()");
  assert.equal(lastModal(ctx).title, "📥 초대 붙여넣기");
  await h.go.onclick();
  assert.equal(h.err.textContent, "초대 문자열을 붙여 넣으세요");
  assert.equal(calls(ctx, "POST", "/api/owner/pclink/accept").length, 0);
  // 오류: 잘못된 초대 → 칸 아래 문구, 다시 누를 수 있음
  ctx.__DATA["POST /api/owner/pclink/accept"] = { __err: { error: "bad_invite" } };
  h.ta.value = "  clewpath-pair:0:BAD  ";
  await h.go.onclick();
  assert.equal(h.err.textContent, "초대가 만료됐거나 잘못됐습니다"); assert.equal(h.go.disabled, false);
  same(calls(ctx, "POST", "/api/owner/pclink/accept")[0][2], { body: { invite: "clewpath-pair:0:BAD" } });
  ctx.__DATA["POST /api/owner/pclink/accept"] = { __err: { error: "self_invite" }, __status: 409 };
  await h.go.onclick();
  assert.ok(h.err.textContent.includes("이 PC 가 만든 초대"));
  ctx.__DATA["POST /api/owner/pclink/accept"] = { approval: AP, peer_name: "노트북" };
  h.ta.value = "clewpath-pair:0:GOOD";
  await h.go.onclick();
  const sheet = lastModal(ctx);
  assert.equal(sheet.title, "✅ 승인 요청");
  assert.ok(sheet.body.text.includes("상대 PC: 노트북 (" + H1.slice(0, 8) + ")"));
  ev(ctx, "globalThis.__modals.at(-1).body.children.find(c => c.placeholder === '2차 인증 6자리').value = '111222'");
  const p = ev(ctx, "globalThis.__approvalDecide(true)");
  assert.ok(lastModal(ctx).body.text.includes("처리 중"), "짝짓기는 몇 초 — 진행 표시");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "busy", "처리 중에는 두 번 보내지 않는다");
  assert.equal(await p, "executed");
  await tick(6);
  assert.ok(toasts(ctx).includes("PC '노트북' 와 짝을 맺었습니다"));
  assert.ok(calls(ctx, "GET", "/api/owner/pclink").length >= 1, "짝 화면을 새로 읽는다");
  // 상대가 거절(초대가 이미 쓰임) → 시트 안 문구
  ctx.__DATA["POST /api/owner/approvals/ap_a/decide"] = { __err: { error: "pair_refused", approval: AP }, __status: 409 };
  await h.go.onclick();
  ev(ctx, "globalThis.__modals.at(-1).body.children.find(c => c.placeholder === '2차 인증 6자리').value = '111222'");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "error");
  assert.ok(lastModal(ctx).body.text.includes("상대가 거절했습니다 — 초대가 이미 쓰였거나 취소됐습니다"));
});

test("해제는 확인창(상대 꺼짐 안내) 뒤에만 POST · 취소하면 아무것도 안 함 · cleanup 이면 「상대 쪽 정리 대기」 안내", async () => {
  const ctx = await boot(true, { [`POST /api/owner/pclink/peers/${H1}/unpair`]: { peer: H1, removed: true, cp_revoked: true, cleanup: true } });
  await ev(ctx, "showPclink()"); await tick();
  const p1 = rowWith(lastModal(ctx).body, "노트북");
  let pr = btnOf(p1, "해제").onclick();
  let sheet = lastModal(ctx);
  assert.equal(sheet.title, "PC 짝 해제");
  assert.ok(sheet.body.text.includes("상대 PC 와의 짝을 해제합니다. 상대가 꺼져 있으면 상대 쪽 정리는 나중에 됩니다"));
  assert.equal(calls(ctx, "POST", /unpair$/).length, 0, "확인 전에는 보내지 않는다");
  sheet.foot[0].onclick();                                   // 취소
  assert.equal(await pr, false);
  assert.equal(calls(ctx, "POST", /unpair$/).length, 0);
  pr = ev(ctx, `pclinkUnpair(${JSON.stringify(STATUS.pairs[0])})`);
  sheet = lastModal(ctx);
  await go(sheet);                             // 해제
  assert.equal(await pr, true);
  const c = calls(ctx, "POST", `/api/owner/pclink/peers/${H1}/unpair`);
  assert.equal(c.length, 1); assert.equal(c[0][2], undefined, "끄는 동작 — 2차 인증 없이");
  assert.ok(toasts(ctx).at(-1).includes("상대 쪽 정리 대기"));
  // 알렸으면(cleanup false) 그냥 해제
  ctx.__DATA[`POST /api/owner/pclink/peers/${H1}/unpair`] = { peer: H1, removed: true, cp_revoked: true, cleanup: false };
  pr = ev(ctx, `pclinkUnpair(${JSON.stringify(STATUS.pairs[0])})`);
  await go(lastModal(ctx)); await pr;
  assert.equal(toasts(ctx).at(-1), "해제했습니다");
});

test("상세 칸 「📡 원격 묻기 받기」: 켜기는 확인 시트(30일·대화 전체·도구 없는 복제본) 뒤, 끄기는 바로(승인 폐기 토스트) · 옛 Host 면 줄 없음", async () => {
  const S = "11111111-2222-3333-4444-555555555555";
  const ctx = await boot(true, { [`GET /api/owner/pclink/sessions/${S}`]: { session_id: S, allow: false },
    [`POST /api/owner/pclink/sessions/${S}/allow`]: { session_id: S, allow: true } });
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  await ctx.loadDetail({ session_id: S, title: "배포", message_count: 1, size_bytes: 1, cwd: "F:/p" });
  await tick();
  const detail = pd.children[1];
  const row = detail.children.find((c) => String(c.className).includes("pcl-allow"));
  assert.ok(row, "상태를 받으면 줄이 붙는다");
  const [txt, btn] = row.children[0].children;
  assert.equal(txt.textContent, "📡 원격 묻기 받기: 끔"); assert.equal(btn.textContent, "켜기");
  const p = btn.onclick(); await tick();
  const sheet = lastModal(ctx);
  assert.equal(sheet.title, "📡 원격 묻기 받기");
  for (const s of ["처음 묻는 PC 마다 사장님 승인", "지금과 앞으로의 대화 전체", "30일", "도구 없는 복제본", "아무것도 실행하지 않습니다"]) assert.ok(sheet.body.text.includes(s), s);
  assert.equal(calls(ctx, "POST", /\/allow$/).length, 0, "확인 전에는 보내지 않는다");
  await go(sheet); await p;
  same(calls(ctx, "POST", `/api/owner/pclink/sessions/${S}/allow`)[0][2], { body: { on: true } });
  assert.ok(txt.textContent.startsWith("📡 원격 묻기 받기: 켬")); assert.equal(btn.textContent, "끄기");
  assert.ok(row.classList.contains("on"));
  // 끄기: 확인 없이 바로
  ctx.__DATA[`POST /api/owner/pclink/sessions/${S}/allow`] = { session_id: S, allow: false };
  const n = ev(ctx, "globalThis.__modals.length");
  await btn.onclick();
  assert.equal(ev(ctx, "globalThis.__modals.length"), n, "끌 때는 확인창 없음");
  same(calls(ctx, "POST", `/api/owner/pclink/sessions/${S}/allow`)[1][2], { body: { on: false } });
  assert.equal(txt.textContent, "📡 원격 묻기 받기: 끔");
  assert.ok(toasts(ctx).at(-1).includes("승인도 함께 폐기"));
  // 켜기 확인 시트에서 취소 → 그대로 끔
  const p2 = btn.onclick(); await tick();
  lastModal(ctx).foot[0].onclick(); await p2;
  assert.equal(calls(ctx, "POST", /\/allow$/).length, 2); assert.equal(btn.textContent, "켜기");
  // 옛 Host(pclink 없음) → 줄 자체가 없다
  delete ctx.__DATA[`GET /api/owner/pclink/sessions/${S}`];
  await ctx.loadDetail({ session_id: S, title: "배포", message_count: 1, size_bytes: 1 });
  await tick();
  assert.ok(!pd.children[1].children.some((c) => String(c.className).includes("pcl-allow")));
  // 설정 화면의 받는 세션 「끄기」도 확인 없이 바로, 끝나면 화면을 새로 읽는다
  ctx.__DATA["POST /api/owner/pclink/sessions/sid-1/allow"] = { session_id: "sid-1", allow: false };
  await ev(ctx, "showPclink()"); await tick();
  const n2 = ev(ctx, "globalThis.__modals.length"), g0 = calls(ctx, "GET", "/api/owner/pclink").length;
  await btnOf(rowWith(lastModal(ctx).body, "배포"), "끄기").onclick();
  same(calls(ctx, "POST", "/api/owner/pclink/sessions/sid-1/allow")[0][2], { body: { on: false } });
  assert.equal(ev(ctx, "globalThis.__modals.length"), n2 + 1, "확인창 없이 — 늘어난 한 겹은 새로 읽은 짝 화면");
  assert.equal(lastModal(ctx).title, "🖥 PC 짝(원격 묻기)");
  assert.equal(calls(ctx, "GET", "/api/owner/pclink").length, g0 + 1);
});

test("상세 칸 「📡 원격 묻기 받기」 상태는 그 자리(더 보기 바로 위)가 화면에 보일 때만 받는다 — 상세 칸을 열기만 해서는 GET 이 나가지 않고, 보이면 한 번, 보기 전에 다른 화면으로 가면 받지 않는다(0.32.3)", async () => {
  const S = "11111111-2222-3333-4444-555555555555";
  const ctx = await boot(true, { [`GET /api/owner/pclink/sessions/${S}`]: { session_id: S, allow: true } });
  ev(ctx, "globalThis.__io = []; IntersectionObserver = class { constructor(cb) { this.cb = cb; this.on = true; __io.push(this); } observe(t) { this.t = t; } disconnect() { this.on = false; } unobserve() {} };");
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  const n = () => calls(ctx, "GET", `/api/owner/pclink/sessions/${S}`).length;
  const has = () => pd.children[1].children.some((c) => String(c.className).includes("pcl-allow"));
  const got = {};
  await ctx.loadDetail({ session_id: S, title: "배포", message_count: 1, size_bytes: 1, cwd: "F:/p" }); await tick();
  const io = ev(ctx, "__io.at(-1)");
  got.opened = { gets: n(), row: has(), watches: String(io.t.className) };
  io.cb([{ isIntersecting: false, target: io.t }]); await tick();
  got.hidden = n();
  io.cb([{ isIntersecting: true, target: io.t }]); await tick();
  got.shown = { gets: n(), row: has(), stopped: !io.on };
  await ctx.loadDetail({ session_id: S, title: "배포", message_count: 1, size_bytes: 1, cwd: "F:/p" }); await tick();
  const io2 = ev(ctx, "__io.at(-1)");
  ev(ctx, "detailPlaceholder()");                                       // 보기 전에 다른 화면으로
  io2.cb([{ isIntersecting: true, target: io2.t }]); await tick();
  got.left = { gets: n(), stopped: !io2.on };
  assert.deepEqual(got, { opened: { gets: 0, row: false, watches: "dmorebox" }, hidden: 0, shown: { gets: 1, row: true, stopped: true }, left: { gets: 1, stopped: true } });
});

test("폰(릴레이): 켜는 동작은 2차 인증(grace 또는 otp)을 싣고, 끄는 동작은 싣지 않는다 · 남은 grace 는 다시 묻지 않음", async () => {
  const S = "sid-9";
  const ctx = await boot(false, {
    "GRANT": { grace: "g-1", exp: NOW + 3600, issued: NOW },
    "POST /api/owner/pclink/pause": { paused: false },
    "POST /api/owner/pclink/invite": { approval: PAIR_AP },
    [`GET /api/owner/pclink/invites/${INV}`]: { inv: INV, invite: "clewpath-pair:0:S" },
    "POST /api/owner/pclink/accept": { approval: { ...PAIR_AP, id: "ap_a" }, peer_name: "노트북" },
    [`POST /api/owner/pclink/sessions/${S}/allow`]: { session_id: S, allow: true },
    [`POST /api/owner/pclink/invites/${INV}/cancel`]: { cancelled: true },
    [`POST /api/owner/pclink/peers/${H1}/unpair`]: { peer: H1, removed: true, cleanup: false } });
  assert.equal(ev(ctx, "MODE"), "relay");
  ev(ctx, "globalThis.__asks = 0; askOtp = () => { globalThis.__asks++; return '123456'; }; showApprovalSheet = () => {};");
  // 모두 끄기 풀기(다시 받기) = 켜는 동작 → 코드 1회 → grant → grace
  await ev(ctx, "pclinkPause(false)");
  same(ev(ctx, "globalThis.__grants"), ["123456"]);
  same(calls(ctx, "POST", "/api/owner/pclink/pause")[0][2], { body: { on: false, grace: "g-1" } });
  // 그 뒤 켜는 동작은 남은 grace 로(다시 묻지 않음)
  await ev(ctx, "pclinkInvite()");
  same(calls(ctx, "POST", "/api/owner/pclink/invite")[0][2], { body: { grace: "g-1" } });
  await ev(ctx, `pclinkShowInvite('${INV}')`);
  same(calls(ctx, "GET", `/api/owner/pclink/invites/${INV}`)[0][2], { query: { grace: "g-1" } }, "GET 은 query 로");
  const h = ev(ctx, "pclinkPaste()"); h.ta.value = "clewpath-pair:0:X"; await h.go.onclick();
  same(calls(ctx, "POST", "/api/owner/pclink/accept")[0][2], { body: { invite: "clewpath-pair:0:X", grace: "g-1" } });
  const pa = ev(ctx, `pclinkSetAllow('${S}', true)`); await tick();
  await go(lastModal(ctx)); assert.equal(await pa, true);
  same(calls(ctx, "POST", `/api/owner/pclink/sessions/${S}/allow`)[0][2], { body: { on: true, grace: "g-1" } });
  assert.equal(ev(ctx, "globalThis.__asks"), 1, "코드는 처음 한 번만");
  // 끄는 동작: 2차 인증 없이
  ctx.__DATA["POST /api/owner/pclink/pause"] = { paused: true };
  await ev(ctx, "pclinkPause(true)");
  same(calls(ctx, "POST", "/api/owner/pclink/pause")[1][2], { body: { on: true } });
  ctx.__DATA[`POST /api/owner/pclink/sessions/${S}/allow`] = { session_id: S, allow: false };
  await ev(ctx, `pclinkSetAllow('${S}', false)`);
  same(calls(ctx, "POST", `/api/owner/pclink/sessions/${S}/allow`)[1][2], { body: { on: false } });
  await ev(ctx, `pclinkCancelInvite({inv:'${INV}'})`);
  assert.equal(calls(ctx, "POST", `/api/owner/pclink/invites/${INV}/cancel`)[0][2], undefined);
  const pu = ev(ctx, `pclinkUnpair({host:'${H1}', name:'노트북'})`); await go(lastModal(ctx)); await pu;
  assert.equal(calls(ctx, "POST", `/api/owner/pclink/peers/${H1}/unpair`)[0][2], undefined);
  for (const c of ev(ctx, "globalThis.__calls").filter((c) => /cancel$|unpair$/.test(c[1]) || (c[2] && c[2].body && c[2].body.on === true && /pause$/.test(c[1])))) {
    assert.ok(!JSON.stringify(c[2] || {}).match(/grace|otp/), "끄는 동작에 2차 인증 없음: " + c[1]);
  }
  assert.equal(ev(ctx, "globalThis.__asks"), 1);
  // grace 를 못 받으면 코드 그대로 otp 로
  ev(ctx, "LS.removeItem('sm_grace')"); ctx.__DATA["GRANT"] = {};
  ctx.__DATA["POST /api/owner/pclink/pause"] = { paused: false };
  await ev(ctx, "pclinkPause(false)");
  same(calls(ctx, "POST", "/api/owner/pclink/pause").at(-1)[2], { body: { on: false, otp: "123456" } });
  // 코드 입력 취소·2FA 미설정 → 보내지 않음
  const before = calls(ctx, "POST", "/api/owner/pclink/pause").length;
  ev(ctx, "askOtp = () => null");
  await ev(ctx, "pclinkPause(false)");
  ctx.__DATA["GET /api/owner/2fa/status"] = { required: false, has_secret: false };
  ev(ctx, "askOtp = () => { globalThis.__asks++; return '123456'; }");
  const asks = ev(ctx, "globalThis.__asks");
  await ev(ctx, "pclinkInvite()");
  assert.equal(calls(ctx, "POST", "/api/owner/pclink/pause").length, before);
  assert.equal(calls(ctx, "POST", "/api/owner/pclink/invite").length, 1, "2FA 가 없으면 초대 요청도 안 보냄");
  assert.equal(ev(ctx, "globalThis.__asks"), asks, "2FA 가 없으면 코드를 묻지도 않는다");
  assert.ok(toasts(ctx).at(-1).includes("2차 인증을 켜야 폰에서 할 수 있습니다"));
  // 틀린 코드 → 문구, grace 지움
  ctx.__DATA["GET /api/owner/2fa/status"] = { required: true, has_secret: true };
  ctx.__DATA["GRANT"] = { __throw: "2fa_invalid" };
  await ev(ctx, "pclinkPause(false)");
  assert.equal(toasts(ctx).at(-1), "2차 인증 코드가 틀렸습니다");
  // 로컬(PC 화면)은 코드 없이
  const pc = await boot(true, { "POST /api/owner/pclink/pause": { paused: false } });
  ev(pc, "askOtp = () => { throw new Error('묻지 말 것'); }");
  await ev(pc, "pclinkPause(false)");
  same(calls(pc, "POST", "/api/owner/pclink/pause")[0][2], { body: { on: false } });
});

test("승인 시트(pclink_*): 폰에서도 2차 인증 칸 · 결정 body 에 otp(또는 남은 grace) · 코드 없으면 안 보냄 · 거절은 코드 없이 · 결정 즉시 실행", async () => {
  const ctx = await boot(false, {
    "POST /api/owner/approvals/ap_c/decide": { ...CONTACT_AP, status: "executed", result: { peer: H1, session_id: "sid-1", expires_at: NOW + 30 * 86400 } } });
  ev(ctx, "showApprovalSheet(" + JSON.stringify(CONTACT_AP) + ")");
  const m = lastModal(ctx);
  assert.ok(m.body.text.includes("📡 원격 묻기 허용(첫 연락)"));
  const main = m.body.children.find((c) => c.className === "sheet-main");
  assert.ok(main && main.textContent.includes("지금과 앞으로의 대화 전체를 30일 동안"), "요약은 잘리지 않게 큰 글씨");
  assert.ok(m.body.text.includes("묻는 PC: 노트북 (" + H1.slice(0, 8) + ") · 세션: 배포"));
  assert.ok(!m.body.text.includes('"peer"'), "JSON 덤프 대신 읽기 좋게");
  const otp = m.body.children.find((c) => c.placeholder === "2차 인증 6자리");
  assert.ok(otp, "폰에서도 코드 칸");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "error");
  assert.ok(m.body.text.includes("2차 인증 코드(6자리)를 넣으세요"));
  assert.equal(calls(ctx, "POST", /decide$/).length, 0, "코드도 grace 도 없으면 보내지 않는다");
  otp.value = "654321";
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "executed");
  same(calls(ctx, "POST", "/api/owner/approvals/ap_c/decide")[0][2].body, { approve: true, otp: "654321" });
  assert.equal(calls(ctx, "POST", /execute$/).length, 0);
  assert.equal(toasts(ctx).at(-1), "승인·실행했습니다");
  // 남은 grace 가 있으면 코드 없이 grace 로
  ev(ctx, `LS.setItem('sm_grace', JSON.stringify({grace:'g-7', exp:${NOW + 3600}, issued:${NOW}}))`);
  ev(ctx, "showApprovalSheet(" + JSON.stringify(CONTACT_AP) + ")");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "executed");
  same(calls(ctx, "POST", "/api/owner/approvals/ap_c/decide")[1][2].body, { approve: true, grace: "g-7" });
  // 거절은 코드 없이
  ev(ctx, "LS.removeItem('sm_grace')");
  ctx.__DATA["POST /api/owner/approvals/ap_c/decide"] = { ...CONTACT_AP, status: "rejected" };
  ev(ctx, "showApprovalSheet(" + JSON.stringify(CONTACT_AP) + ")");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(false)"), "rejected");
  same(calls(ctx, "POST", "/api/owner/approvals/ap_c/decide")[2][2].body, { approve: false });
  // 서버 관문 거절 문구 + 틀린 코드면 grace 지움
  const cases = [["e2ee_required", "이 폰 연결이 암호화돼 있지 않습니다 — PC 화면에서 하거나 폰을 다시 페어링하세요"],
    ["2fa_required", "2차 인증을 켜야 폰에서 할 수 있습니다 — PC 화면에서 하세요"], ["2fa_invalid", "2차 인증 코드가 틀렸습니다"],
    ["human_proof_required", "이 기기로는 승인할 수 없습니다(페어링을 확인하세요)."], ["not_pending", "이미 처리된 요청입니다."]];
  for (const [code, msg] of cases) {
    ev(ctx, `LS.setItem('sm_grace', JSON.stringify({grace:'g-8', exp:${NOW + 3600}, issued:${NOW}}))`);
    ctx.__DATA["POST /api/owner/approvals/ap_c/decide"] = { __err: { error: code }, __status: 403 };
    ev(ctx, "showApprovalSheet(" + JSON.stringify(CONTACT_AP) + ")");
    assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "error");
    assert.ok(lastModal(ctx).body.text.includes(msg), code + " → " + lastModal(ctx).body.text);
    assert.equal(ev(ctx, "LS.getItem('sm_grace')") === null, code.startsWith("2fa_"), "2fa 거절이면 grace 를 지운다: " + code);
  }
  // 다른 종류는 폰에서 예전처럼 코드 칸 없음
  ev(ctx, "showApprovalSheet(" + JSON.stringify({ ...CONTACT_AP, id: "ap_t", kind: "team_purge", args: { team: "W" } }) + ")");
  assert.ok(!lastModal(ctx).body.children.some((c) => c.placeholder === "2차 인증 6자리"));
});

test("오류 코드 → 한국어 문구(본문 error·code·message 어디로 와도) · 모르는 코드는 그대로", async () => {
  const ctx = await boot(true);
  const txt = (o) => ev(ctx, `pclinkErrText(${o})`);
  const exp = {
    human_proof_required: "이 기기가 인증되지 않았습니다", e2ee_required: "이 폰 연결이 암호화돼 있지 않습니다 — PC 화면에서 하거나 폰을 다시 페어링하세요",
    "2fa_required": "2차 인증을 켜야 폰에서 할 수 있습니다 — PC 화면에서 하세요", "2fa_invalid": "코드가 틀렸습니다",
    cp_unavailable: "서버(CP)", relay_unavailable: "이 PC 의 원격 연결이 꺼져 있습니다", already_paired: "이미 짝지은 PC",
    self_invite: "이 PC 가 만든 초대", invite_expired: "초대가 만료됐거나 잘못됐습니다", bad_invite: "초대가 만료됐거나 잘못됐습니다",
    pair_refused: "상대가 거절했습니다 — 초대가 이미 쓰였거나 취소됐습니다" };
  for (const [code, want] of Object.entries(exp)) {
    assert.ok(txt(`Object.assign(new Error('HTTP 403'), {json:{error:'${code}'}})`).includes(want), code);
  }
  assert.ok(txt("Object.assign(new Error('HTTP 503'), {json:{status:'error', code:'pair_refused'}})").includes("상대가 거절"), "PclinkFail 모양({code})");
  assert.ok(txt("new Error('2fa_invalid')").includes("코드가 틀렸습니다"), "커넥터 거절은 message 로");
  assert.equal(txt("new Error('weird_code')"), "실패: weird_code");
  // pclinkFail: 토스트 + 2fa 거절이면 grace 지움
  ev(ctx, "LS.setItem('sm_grace', '{\"grace\":\"g\",\"exp\":9999999999}'); pclinkFail(new Error('2fa_required'))");
  assert.equal(ev(ctx, "LS.getItem('sm_grace')"), null);
  assert.ok(toasts(ctx).at(-1).includes("2차 인증을 켜야"));
});

test("📱 기기 목록: kind pc 기기는 🖥 배지 'PC 짝 — pclink 만' · QR 다시 만들기 없음 · 구형 자격 표시 없음 · 폰 기기는 그대로", async () => {
  const ctx = await boot(true, { "GET /api/owner/devices": { active: 2, total: 3, devices: [
    { id: "dphone01", name: "아이폰", kind: "phone", client_scoped: true, revoked: false, last_seen: NOW - 30 },
    { id: "dpc00001", name: "노트북", kind: "pc", client_scoped: false, revoked: false, last_seen: null },
    { id: "dpc00002", name: "옛PC", kind: "pc", client_scoped: true, revoked: true, last_seen: NOW - 999 } ] } });
  await ev(ctx, "showDevices()");
  const m = lastModal(ctx);
  const list = m.body.children[2];
  const [phone, pc, pcOff] = list.children;
  assert.ok(!phone.text.includes("🖥") && !phone.text.includes("PC 짝"));
  same(phone.children[1].children.map((b) => b.textContent), ["QR 다시 만들기", "페어링 해제"]);
  assert.ok(pc.text.includes("🖥 노트북") && pc.text.includes("🖥 PC 짝 — pclink 만"));
  assert.ok(!pc.text.includes("공유 자격") && !pc.text.includes("QR 다시 만들기 권장"));
  same(pc.children[1].children.map((b) => b.textContent), ["페어링 해제"], "폰 전용 동작(QR 재발급)은 숨김");
  assert.ok(pcOff.text.includes("해제됨") && !pcOff.text.includes("pclink 만"));
  same(pcOff.children[1].children.map((b) => b.textContent), ["삭제"]);
});

test("승인 시트(pclink_*): 승인됐지만 실행이 실패해 '승인됨' 으로 남은 요청은 「다시 실행」(같은 인자로 execute)", async () => {
  const ctx = await boot(false, {
    "POST /api/owner/approvals/ap_c/execute": { ...CONTACT_AP, status: "executed", result: { peer: H1, session_id: "sid-1" } } });
  ev(ctx, "showApprovalSheet(" + JSON.stringify({ ...CONTACT_AP, status: "approved" }) + ")");
  const m = lastModal(ctx);
  assert.ok(m.body.text.includes("다시 실행할 수 있습니다"));
  assert.equal(m.foot.length, 1);
  assert.equal(m.foot[0].textContent, "다시 실행");
  assert.equal(m.body.children.find((c) => c.placeholder === "2차 인증 6자리"), undefined, "이미 승인됨 — 코드 칸 없음");
  assert.equal(await ev(ctx, "globalThis.__approvalRetry()"), "executed");
  assert.equal(calls(ctx, "POST", "/api/owner/approvals/ap_c/execute").length, 1);
  assert.equal(toasts(ctx).at(-1), "실행했습니다");
  // 다른 종류(팀 등)의 '승인됨' 은 지금처럼 버튼 없이
  const other = await boot(false, {});
  ev(other, "showApprovalSheet(" + JSON.stringify({ ...CONTACT_AP, kind: "team_create", args: { name: "x" }, status: "approved" }) + ")");
  assert.equal(lastModal(other).foot.length, 0);
});

test("폰 pclink 켜는 동작: 10분 넘은 grace(또는 발급 시각 없는 옛 grace)는 쓰지 않고 코드를 다시 묻는다 · 서버 2fa_stale 이면 그 grace 를 다시 쓰지 않음(지우지는 않음)", async () => {
  const ctx = await boot(false, {
    "GRANT": { grace: "g-new", exp: NOW + 3600, issued: NOW },
    "POST /api/owner/pclink/pause": { paused: false } });
  ev(ctx, "globalThis.__asks = 0; askOtp = () => { globalThis.__asks++; return '123456'; };");
  ev(ctx, `LS.setItem('sm_grace', JSON.stringify({grace:'g-old', exp:${NOW + 3600}, issued:${NOW - 700}}))`);
  await ev(ctx, "pclinkPause(false)");
  assert.equal(ev(ctx, "globalThis.__asks"), 1, "10분 넘은 grace 는 안 씀");
  same(calls(ctx, "POST", "/api/owner/pclink/pause").at(-1)[2], { body: { on: false, grace: "g-new" } });
  ev(ctx, `LS.setItem('sm_grace', JSON.stringify({grace:'g-legacy', exp:${NOW + 3600}}))`);
  await ev(ctx, "pclinkPause(false)");
  assert.equal(ev(ctx, "globalThis.__asks"), 2, "발급 시각 없는 옛 grace 도 안 씀");
  // 서버가 2fa_stale(시계 차 등) → 그 grace 는 다시 쓰지 않되 지우지 않는다(터미널 특권은 그대로)
  ev(ctx, `LS.setItem('sm_grace', JSON.stringify({grace:'g-s', exp:${NOW + 3600}, issued:${NOW}}))`);
  ctx.__DATA["POST /api/owner/pclink/pause"] = { __err: { error: "2fa_stale" }, __status: 403 };
  await ev(ctx, "pclinkPause(false)");
  assert.ok(toasts(ctx).at(-1).includes("10분 안에 넣은 2차 인증 코드"));
  assert.ok(ev(ctx, "LS.getItem('sm_grace')").includes("g-s"), "grace 는 지우지 않는다");
  assert.equal(ev(ctx, "pclinkGrace()"), "", "같은 grace 는 다시 쓰지 않는다");
});
