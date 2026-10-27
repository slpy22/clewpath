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
  const txt = sheet.subNode.text;
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
  const txt = ev(ctx, "globalThis.__sheet").subNode.text;
  assert.ok(txt.includes("지우는 쪽에만 있는 대화 4건") && txt.includes("입력 2번") && txt.includes("사본에서 새로 물어본 것"));
  assert.ok(String(ev(ctx, "globalThis.__err")).includes("휴지통으로 옮기지 못했습니다"), "실패 사유는 시트에");
  ev(ctx, "globalThis.__sheet = null; globalThis.__t = []");
  await ev(ctx, "revertContinuation({session_id:'x'})");
  assert.equal(ev(ctx, "globalThis.__sheet"), null); assert.ok(ev(ctx, "globalThis.__t")[0].includes("실행 중"));
});

// 맥 6회차(2026-10-26): 사흘 치 작업이 담긴 사본을 회색 경고 줄을 못 보고 지웠다 — 빨간 상자·시각·확인 체크·사본 열기.
test("revertContinuation: 잃는 대화가 있으면 맨 위 경고(더 최근·기간)·양쪽 시각·같은 이름 표시·확인 체크·사본 열기", async () => {
  const RISK = { ...PLAN, new_messages: 242, new_inputs: 2, last_inputs: [{ text: "그래서 다 된거야" }],
    newer: "new", new_since: "2026-10-23T14:49:18Z", new_until: "2026-10-26T02:47:41Z",
    old_last: "2026-10-23T14:47:04Z", new_last: "2026-10-26T02:47:41Z" };
  const ctx = await boot({ [`GET /api/sessions/${A}/revert-continuation/preview`]: RISK });
  ev(ctx, `SESSIONS = [{session_id:'${A}'}, {session_id:'${B}'}]; confirmSheet = async (o) => { globalThis.__sheet = o; return false; }`);
  await ev(ctx, `revertContinuation({session_id:'${A}'})`);
  const sheet = ev(ctx, "globalThis.__sheet");
  const top = sheet.subNode.children[0];
  assert.equal(top.className, "sheet-warn", "경고는 맨 위 상자");
  assert.ok(top.text.includes("지우는 쪽에만 있는 대화 242건") && top.text.includes("더 최근에 쓴 세션"));
  assert.ok(top.text.includes("그 대화 기간:") && top.text.includes("입력 2번") && top.text.includes("그래서 다 된거야"));
  const txt = sheet.subNode.text;
  assert.ok(/\(aaaaaaaa\) · 마지막 대화 [0-9-]+ \d\d:\d\d — 그대로 유지/.test(txt), txt);
  assert.ok(/\(bbbbbbbb\) · 마지막 대화 [0-9-]+ \d\d:\d\d — 삭제/.test(txt));
  assert.ok(txt.includes("두 세션 이름이 같습니다"));
  assert.ok(sheet.ack.includes("242건"), "확인 체크를 받아야 버튼이 눌림");
  assert.equal(sheet.alt.label, "취소하고 사본 열기");
  sheet.alt.run();
  assert.equal(ev(ctx, "globalThis.__det"), B, "사본 상세로");
});

test("revertContinuation: 새 대화 없는 복사본이면 경고 상자·확인 체크·사본 열기 없음", async () => {
  const ctx = await boot({ [`GET /api/sessions/${A}/revert-continuation/preview`]: { ...PLAN, newer: "old", old_title: "가", new_title: "나" } });
  ev(ctx, "confirmSheet = async (o) => { globalThis.__sheet = o; return false; }");
  await ev(ctx, `revertContinuation({session_id:'${A}'})`);
  const sheet = ev(ctx, "globalThis.__sheet");
  assert.notEqual(sheet.subNode.children[0].className, "sheet-warn");
  assert.ok(!sheet.subNode.text.includes("이름이 같습니다"));
  assert.equal(sheet.ack, null); assert.equal(sheet.alt, null);
});

test("confirmSheet: ack 는 체크 전 동사 버튼을 막고, alt 는 시트를 닫고 다른 길로", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__closed = 0; closeModal = () => { globalThis.__closed++; }; openModal = (t, b, f) => { globalThis.__b = b; globalThis.__f = f; }");
  ev(ctx, "globalThis.__ran = 0; globalThis.__p = confirmSheet({ title: 't', main: 'm', action: '되돌리기', ack: '확인했습니다',"
    + " run: async () => { globalThis.__ran++; } })");
  const [cancel, go] = ev(ctx, "globalThis.__f");
  assert.equal(cancel.textContent, "취소");
  assert.equal(go.disabled, true, "체크 전에는 눌리지 않음");
  await go.onclick();
  assert.equal(ev(ctx, "globalThis.__ran"), 0);
  const lb = ev(ctx, "globalThis.__b").children.find((c) => c.className === "sheet-ack");
  const box = lb.children[0]; box.checked = true; box.onchange();
  assert.equal(go.disabled, false);
  await go.onclick();
  assert.equal(ev(ctx, "globalThis.__ran"), 1);
  assert.equal(await ev(ctx, "globalThis.__p"), true);

  ev(ctx, "globalThis.__alt = 0; globalThis.__p = confirmSheet({ title: 't', main: 'm', alt: { label: '다른 길', run: () => { globalThis.__alt++; } } })");
  const f = ev(ctx, "globalThis.__f");
  assert.equal(f.map((b) => b.textContent).join("|"), "취소|다른 길|확인");
  f[1].onclick();
  assert.equal(ev(ctx, "globalThis.__alt"), 1);
  assert.equal(await ev(ctx, "globalThis.__p"), false, "다른 길은 실행 아님");
});
