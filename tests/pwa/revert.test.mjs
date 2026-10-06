// 이어하기 되돌리기(2026-10-06): 실수로 생긴 이어받은 세션을 지우고 원래 세션으로 — 버튼 노출·확인창 내용·실행.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const PLAN = { ok: true, old: A, new: B, old_title: "논문 서비스", new_title: "논문 서비스", kind: "background",
  new_process: { kind: "background" }, stop: "백그라운드 에이전트 종료(claude stop)", new_messages: 0, new_inputs: 0,
  last_inputs: [], steps: ["사본 bbbbbbbb 의 실행 중지: 백그라운드 에이전트 종료(claude stop)", "사본 bbbbbbbb 를 휴지통으로 이동(30일 안 복구 가능)",
  "팀 명부: KP/논문 를 원래 세션으로 되돌리고 사본의 중복 기록 정리", "원래 세션 aaaaaaaa 파일에서 '이어짐' 표시 한 줄 제거(원본은 휴지통에 백업)"] };

async function boot(resp) {
  const ctx = await load();
  ctx.__R = resp;
  ev(ctx, "globalThis.__t = []; toast = (m) => globalThis.__t.push(m); globalThis.__ll = 0; loadList = async () => { globalThis.__ll++; };"
    + "globalThis.__api = []; T = { api: async (m, p, o) => { globalThis.__api.push([m, p, o]); const d = globalThis.__R[m + ' ' + p];"
    + "  if (d && d.__err) { const e = new Error('HTTP 409'); e.json = d.__err; throw e; } return d; } };"
    + "ensurePriv = async () => ({ grace: 'g', otp: '' }); globalThis.__det = null; loadDetail = (s) => { globalThis.__det = s.session_id; };"
    + "confirmSheet = async (o) => { globalThis.__sheet = o; try { await o.run(); return true; } catch (e) { globalThis.__err = e.message; return false; } }");
  return ctx;
}

test("continuationPair: 원래 쪽·이어받은 쪽 모두 짝을 찾고, 대상이 목록에 없으면 null", async () => {
  const ctx = await load();
  ev(ctx, `SESSIONS = [{session_id:'${A}', continued_in:'${B}'}, {session_id:'${B}'}, {session_id:'c', continued_in:'gone'}]`);
  assert.deepEqual({ ...ev(ctx, `continuationPair({session_id:'${A}', continued_in:'${B}'})`) }, { old: A, neu: B });
  assert.deepEqual({ ...ev(ctx, `continuationPair({session_id:'${B}'})`) }, { old: A, neu: B });
  assert.equal(ev(ctx, "continuationPair({session_id:'c', continued_in:'gone'})"), null, "지워졌거나 다른 PC 의 이어받기는 버튼 없음");
  assert.equal(ev(ctx, "continuationPair({session_id:'z'})"), null);
});

test("revertContinuation: 미리보기 → 확인창(할 일·유지/삭제 대상) → 2FA 동봉 POST → 원래 세션 상세로", async () => {
  const ctx = await boot({ [`GET /api/sessions/${B}/revert-continuation/preview`]: PLAN,
                           [`POST /api/sessions/${B}/revert-continuation`]: { ok: true, old: A, new: B } });
  ev(ctx, `SESSIONS = [{session_id:'${A}'}]`);
  await ev(ctx, `revertContinuation({session_id:'${B}'})`);
  const sheet = ev(ctx, "globalThis.__sheet");
  const txt = sheet.subNode.textContent;
  assert.ok(txt.includes("원래 세션: 논문 서비스 (aaaaaaaa) — 그대로 유지") && txt.includes("(bbbbbbbb) — 삭제"));
  assert.ok(txt.includes("← 를 눌러") && txt.includes("1. 사본 bbbbbbbb 의 실행 중지") && txt.includes("4. 원래 세션"));
  assert.ok(txt.includes("새로 진행된 대화는 없습니다"));
  const api = ev(ctx, "globalThis.__api");
  assert.equal(api[1][0], "POST"); assert.equal(api[1][2].grace, "g", "릴레이면 2FA 동봉");
  assert.ok(ev(ctx, "globalThis.__t")[0].includes("aaaaaaaa"));
  assert.equal(ev(ctx, "globalThis.__ll"), 1); assert.equal(ev(ctx, "globalThis.__det"), A, "원래 세션 상세로 이동");
});

test("revertContinuation: 사본에만 있는 대화는 경고, 미리보기 거절(원래 세션 실행 중)은 토스트만", async () => {
  const ctx = await boot({ [`GET /api/sessions/${A}/revert-continuation/preview`]:
    { ...PLAN, new_messages: 4, new_inputs: 2, last_inputs: [{ text: "사본에서 새로 물어본 것" }] } ,
    [`GET /api/sessions/x/revert-continuation/preview`]: { __err: { ok: false, error: "old_live", message: "원래 세션이 지금 실행 중입니다." } },
    [`POST /api/sessions/${A}/revert-continuation`]: { __err: { ok: false, message: "사본을 휴지통으로 옮기지 못했습니다" } } });
  await ev(ctx, `revertContinuation({session_id:'${A}'})`);
  const txt = ev(ctx, "globalThis.__sheet").subNode.textContent;
  assert.ok(txt.includes("대화 4건(입력 2건)도 함께 휴지통") && txt.includes("사본에서 새로 물어본 것"));
  assert.ok(String(ev(ctx, "globalThis.__err")).includes("휴지통으로 옮기지 못했습니다"), "실패 사유는 시트에");
  ev(ctx, "globalThis.__sheet = null; globalThis.__t = []");
  await ev(ctx, "revertContinuation({session_id:'x'})");
  assert.equal(ev(ctx, "globalThis.__sheet"), null); assert.ok(ev(ctx, "globalThis.__t")[0].includes("실행 중"));
});
