// 개인 비서 단계 3 화면: ✅ 승인 시트(PC=2FA 칸, 폰=탭), 🧑‍💼 헤더 버튼 3분기, 사장님 프로필, 동봉 스킬 설치 일반화.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const AP = { id: "ap_1", kind: "team_purge", args: { team: "WEB" }, summary: "팀 보존본 지우기: WEB", status: "pending",
             created: 1, expires: 2, requested_by: "s-aaaaaaaa" };

async function boot(local, data) {
  const ctx = await load(local ? { location: LOCAL_LOC } : {});
  ctx.__DATA = data || {};
  ev(ctx, "globalThis.__calls = []; globalThis.__modals = []; globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);"
    + "openModal = (title, body, foot) => { globalThis.__modals.push({ title, body, foot }); }; closeModal = () => {};"
    + "T = { api: async (verb, path, o) => { globalThis.__calls.push([verb, path, o]); const d = globalThis.__DATA[verb + ' ' + path];"
    + "  if (d && d.__throw) throw new Error(d.__throw); if (d === undefined) throw new Error('404 ' + path); return JSON.parse(JSON.stringify(d)); } };");
  return ctx;
}
const lastModal = (ctx) => ev(ctx, "globalThis.__modals.at(-1)");

test("승인 시트(PC 로컬): 2FA 코드 칸, 승인 → decide{otp} → execute → onDone, 증명 없음은 안내", async () => {
  const ctx = await boot(true, {
    "POST /api/owner/approvals/ap_1/decide": { ...AP, status: "approved" },
    "POST /api/owner/approvals/ap_1/execute": { ...AP, status: "executed", result: { purged_events: 3 } } });
  ev(ctx, "globalThis.__done = null; showApprovalSheet(" + JSON.stringify(AP) + ", {execute:true, onDone:(r)=>{ globalThis.__done = r; }})");
  const m = lastModal(ctx);
  assert.ok(m.body.text.includes("2차 인증") && m.body.text.includes('"team": "WEB"'), "정확한 인자를 보여 준다");
  assert.equal(m.foot.map((b) => b.textContent).join(","), "거절,승인");
  ev(ctx, "globalThis.__modals.at(-1).body.children.find(c => c.placeholder === '2차 인증 6자리').value = ' 123456 '");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "executed");
  const dec = ev(ctx, "globalThis.__calls.find(c => c[1].endsWith('/decide'))");
  assert.equal(JSON.stringify(dec[2].body), JSON.stringify({ approve: true, otp: "123456" }));
  assert.equal(JSON.stringify(ev(ctx, "globalThis.__done")), JSON.stringify({ purged_events: 3 }));
  ctx.__DATA["POST /api/owner/approvals/ap_1/decide"] = { __throw: "human_proof_required" };
  ev(ctx, "showApprovalSheet(" + JSON.stringify(AP) + ")");
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "error");
  assert.ok(lastModal(ctx).body.text.includes("2차 인증 코드가 필요합니다"));
});

test("승인 시트(폰): 2FA 칸 없이 탭 · 세션이 낸 요청은 그 세션이 실행, 화면에서 낸 요청은 폰에서도 바로 실행(팀 통합 U5)", async () => {
  const ctx = await boot(false, { "POST /api/owner/approvals/ap_1/decide": { ...AP, status: "approved" },
    "POST /api/owner/approvals/ap_1/execute": { ...AP, status: "executed", result: { ok: 1 } } });
  ev(ctx, "showApprovalSheet(" + JSON.stringify(AP) + ")");
  assert.ok(!lastModal(ctx).body.text.includes("2차 인증"));
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "approved");
  assert.ok(!ev(ctx, "globalThis.__calls.some(c => c[1].endsWith('/execute'))"));
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("요청한 세션이 실행"));
  ev(ctx, "showApprovalSheet(" + JSON.stringify({ ...AP, requested_by: "owner" }) + ", {execute:true})");
  assert.ok(lastModal(ctx).body.text.includes("사장님 화면"));
  assert.equal(await ev(ctx, "globalThis.__approvalDecide(true)"), "executed");
});

test("승인 대기 목록 → 항목을 누르면 승인 시트, 처리된 요청은 버튼 없이", async () => {
  const ctx = await boot(false, { "GET /api/owner/approvals": { approvals: [AP] }, "GET /api/owner/approvals/ap_9": { ...AP, id: "ap_9", status: "executed" } });
  await ev(ctx, "showApprovals()");
  assert.ok(lastModal(ctx).body.text.includes("팀 보존본 지우기: WEB"));
  await ev(ctx, "openApprovalById('ap_9')");
  assert.equal(lastModal(ctx).foot.length, 0);
});

test("🧑‍💼 헤더: 비서 있으면 그 세션 터미널, 없으면 로컬은 만들기/지정, 폰은 PC 안내", async () => {
  const ctx = await boot(true, { "GET /api/owner/assistant": { assistant: { session_id: "s-as", live: true } } });
  ev(ctx, "globalThis.__term = null; loadTerminal = (s) => { globalThis.__term = s; }; SESSIONS = [{session_id:'s-as', title:'비서 세션'}];");
  await ev(ctx, "openAssistant()");
  assert.equal(ev(ctx, "globalThis.__term.title"), "비서 세션");
  ctx.__DATA["GET /api/owner/assistant"] = { assistant: null };
  await ev(ctx, "openAssistant()");
  const t = lastModal(ctx).body.text;
  assert.ok(t.includes("새 비서 만들기") && t.includes("기존 세션을 비서로"));
  ctx.__DATA["POST /api/owner/approvals"] = { ...AP, kind: "assistant_set", args: { create: true } };
  ev(ctx, "globalThis.__sheets = []; showApprovalSheet = (a, o) => globalThis.__sheets.push([a, o]);");
  await ev(ctx, "globalThis.__modals.at(-1).body.children.find(c => (c.text||'').includes('새 비서 만들기')).onclick()");
  const post = ev(ctx, "globalThis.__calls.find(c => c[1] === '/api/owner/approvals')");
  assert.equal(JSON.stringify(post[2].body), JSON.stringify({ kind: "assistant_set", args: { create: true } }));
  const phone = await boot(false, { "GET /api/owner/assistant": { assistant: null } });
  await ev(phone, "openAssistant()");
  assert.ok(lastModal(phone).body.text.includes("PC 의 ClewPath 에서"));
});

test("사장님 프로필(팀 보기의 🧑‍💼 개인비서 칸): 제안·확정 표시, 확정/거절/삭제는 승인 요청(profile_decide)으로 · 폰에서도", async () => {
  const ctx = await boot(false, { "GET /api/owner/profile": { profile: [
    { id: "pf_1", statement: "결론 먼저", status: "proposed", topic: "말투", confidence: 0.8, evidence: [1, 2], source: "assistant" },
    { id: "pf_2", statement: "테스트 먼저", status: "confirmed", confidence: 1, evidence: [], source: "owner" }] } });
  ev(ctx, "globalThis.__req = []; requestApproval = async (k, a) => { globalThis.__req.push([k, a]); }; LIST_CTX = {rerender(){}};");
  const t = "({code:'ASSIST', kind:'portfolio', members:[{alias:'비서', member_role:'manager', live:true}]})";
  ev(ctx, `assistantBody(${t})`);                           // 처음엔 불러오는 중
  await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0));
  ev(ctx, `globalThis.__b = assistantBody(${t})`);
  const txt = ev(ctx, "__b.text");
  for (const s of ["📌 테스트 먼저", "💡 결론 먼저", "제안 · 말투 · 확신 80%", "확정 · 확신 100%"]) assert.ok(txt.includes(s), s);
  assert.ok(ev(ctx, `assistantLine(${t})`).startsWith("● 실행 중"));
  const flat = "(n => { const f = x => [x, ...(x.children || []).flatMap(f)]; return f(n); })(__b)";
  ev(ctx, `${flat}.find(b => b.textContent === '확정').onclick()`);
  assert.equal(JSON.stringify(ev(ctx, "globalThis.__req[0]")), JSON.stringify(["profile_decide", { id: "pf_1", decision: "confirm" }]));
});

test("설정: 승인 요청 안내 줄 없음(🔔 할 일이 대신, 2026-10-13), '비서 스킬 설치' 는 PC 로컬만 · installBundledSkill 은 스킬별 경로", async () => {
  const text = async (local) => {
    const ctx = await boot(local, {});
    ev(ctx, "openModal = (t, b) => { globalThis.__sb = b; }; pushSupported = () => true; curPcLabel = () => 'PC';");
    ev(ctx, "showSettings()");
    return ev(ctx, "globalThis.__sb").text;
  };
  const r = await text(false), l = await text(true);
  assert.ok(!r.includes("할 일에 모입니다") && !r.includes("비서 스킬"));
  assert.ok(!l.includes("할 일에 모입니다") && l.includes("비서 스킬"));
  const ctx = await boot(true, { "GET /api/owner/skills/assistant": { installed: false, path: "p" } });
  await ev(ctx, "installBundledSkill('assistant')");
  assert.equal(lastModal(ctx).title, "📦 비서 스킬 설치");
  assert.equal(ev(ctx, "globalThis.__calls[0][1]"), "/api/owner/skills/assistant");
});
