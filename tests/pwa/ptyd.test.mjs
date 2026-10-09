// 터미널 관리 프로세스(PTY 브로커) 설정 화면 — 상태 문구, 업데이트/재시작은 확인창(D2: 열린 터미널 수를 먼저 알림).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

test("ptydSummary: 꺼짐·미실행·실행 중(터미널 수·업데이트 대기)", async () => {
  const ctx = await load();
  assert.ok(ev(ctx, "ptydSummary({enabled:false})").includes("꺼져 있음"));
  assert.ok(ev(ctx, "ptydSummary({enabled:true, running:false})").includes("자동으로 시작"));
  const s = ev(ctx, "ptydSummary({enabled:true, running:true, pid:42, sessions:3, update_pending:true})");
  assert.ok(s.includes("pid 42") && s.includes("터미널 3개") && s.includes("업데이트 대기"));
});

test("showPtyd: 터미널이 있으면 '종료하고 진행' 확인창 → force:true + 2FA 동봉 POST", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__api = []; globalThis.__modals = []; globalThis.__t = []; toast = (m) => globalThis.__t.push(m);"
    + "openModal = (title, body, foot) => { globalThis.__modals.push({ title, body, foot }); }; closeModal = () => {};"
    + "ensurePriv = async () => ({ grace: 'g', otp: '' });"
    + "T = { api: async (m, p, o) => { globalThis.__api.push([m, p, o]);"
    + "  return m === 'GET' ? { enabled: true, running: true, pid: 7, sessions: 2, update_pending: true } : { ok: true }; } };"
    + "confirmSheet = async (o) => { globalThis.__sheet = o; await o.run(); return true; }");
  await ev(ctx, "showPtyd()");
  const m = ev(ctx, "globalThis.__modals.at(-1)");
  assert.equal(m.foot[0].textContent, "⬆ 지금 업데이트");
  await ev(ctx, "globalThis.__modals.at(-1).foot[0].onclick()");
  const sh = ev(ctx, "globalThis.__sheet");
  assert.ok(sh.main.includes("2개가 종료") && sh.action === "종료하고 진행" && sh.danger);
  const post = ev(ctx, "globalThis.__api").find((x) => x[0] === "POST");
  assert.equal(post[1], "/api/owner/ptyd/restart");
  assert.equal(post[2].body.force, true); assert.equal(post[2].grace, "g");
  assert.ok(ev(ctx, "globalThis.__t")[0].includes("다시 시작"));
});
