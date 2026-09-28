// 관제 오버레이 — 타임라인 필터가 실시간 증분 행에도 적용되는지(미검증 항목 소화, 2026-09-28).
// openMonitor 를 스텁 DOM 위에서 실제로 열고, T.monitor 를 가로채 스냅샷/증분 메시지를 밀어 넣는다.
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

let ctx, sent, cb, handle;
const M = "aaaaaaaa-1111-2222-3333-444444444444", S = "bbbbbbbb-1111-2222-3333-444444444444";

function evt(sid, role, extra = {}, seq) {
  return { session_id: sid, session_label: sid.slice(0, 8), color: "#123", session_role: sid === M ? "manager" : "sub",
           seq, ts: new Date().toISOString(), role, text: "", tools: [], tool_results: [], calls_out: [], ...extra };
}
const kids = (el) => el.children;
const chips = (fbar) => kids(fbar).filter((c) => String(c.className).includes("fchip"));
const rows = (body) => kids(body).filter((c) => String(c.className).includes("mrow"));
const label = (c) => c.text;   // textContent + 텍스트 노드 자식(세션 칩은 createTextNode 로 라벨을 붙인다)

before(async () => {
  ctx = await load();
  sent = [];
  handle = { add() {}, remove() {}, close() {} };
  ev(ctx, "T.monitor = (ids, mgr, fn) => { globalThis.__monCb = fn; return globalThis.__monHandle; }");
  ctx.__monHandle = handle;
  ctx.openMonitor([M, S], M, { [M]: "관리", [S]: "하위" });
  cb = ctx.__monCb;
});

test("오버레이가 스텁 DOM 에 조립되고 필터 바에 종류 칩 4 + 세션 칩 2", () => {
  const ov = kids(ctx.document.body).find((c) => String(c.className) === "mon-ov");
  assert.ok(ov, "mon-ov 없음");
  const [, legend, fbar, split] = kids(ov);
  assert.equal(String(fbar.className), "mon-filter");
  const labels = chips(fbar).map(label);
  assert.deepEqual(labels.slice(0, 4), ["전체", "→ 호출선", "💬 대화", "⚠ 오류"]);
  assert.equal(chips(fbar).length, 6);
  assert.ok(labels[4].includes("관리") && labels[5].includes("하위"));
  ctx.__ov = ov; ctx.__fbar = fbar; ctx.__body = kids(split)[0];
});

test("스냅샷 3행 → '오류' 필터 → 이후 증분 행도 렌더 순간 필터 적용", async () => {
  cb(JSON.stringify({ type: "snapshot", events: [
    evt(M, "assistant", { text: "계획" }, 1),
    evt(S, "assistant", { tools: ["Bash"] }, 2),
    evt(S, "user", { tool_results: [{ is_error: true, text: "Exit code 1" }] }, 3),
  ] }));
  assert.equal(rows(ctx.__body).length, 3);
  const err = chips(ctx.__fbar).find((c) => c.textContent.includes("오류"));
  err.onclick();
  const vis = () => rows(ctx.__body).filter((r) => !r.classList.contains("fhid")).length;
  assert.equal(vis(), 1);
  const count = () => kids(ctx.__fbar).find((c) => String(c.className) === "fcount").textContent;
  assert.equal(count(), "표시 1 / 전체 3");
  // 증분: 텍스트 행(숨겨져야) + 오류 행(보여야)
  cb(JSON.stringify({ type: "events", events: [
    evt(M, "assistant", { text: "다음 지시" }, 4),
    evt(S, "user", { tool_results: [{ is_error: true, text: "boom" }] }, 5),
  ] }));
  const all = rows(ctx.__body);
  assert.equal(all.length, 5);
  assert.equal(all[3].classList.contains("fhid"), true, "증분 텍스트 행이 숨겨지지 않음");
  assert.equal(all[4].classList.contains("fhid"), false, "증분 오류 행이 숨겨짐");
  assert.equal(count(), "표시 2 / 전체 5");
  // 전체로 복귀 → 모두 표시
  chips(ctx.__fbar).find((c) => c.textContent === "전체").onclick();
  assert.equal(vis(), 5);
  assert.equal(count(), "전체 5");
});

test("세션 숨김 토글도 증분 행에 적용되고, 복원 시 카운터가 돌아온다", () => {
  // 이벤트가 들어오면 서버가 준 session_label(여기선 sid 앞 8자)로 라벨이 갱신된다
  const hide = chips(ctx.__fbar).find((c) => label(c).includes("하위") || label(c).includes(S.slice(0, 8)));
  assert.ok(hide, "하위 세션 칩을 못 찾음: " + chips(ctx.__fbar).map(label).join(" | "));
  hide.onclick();
  assert.equal(chips(ctx.__fbar).length, 6, "재렌더 뒤에도 칩은 6개(innerHTML='' 가 자식을 비움)");
  cb(JSON.stringify({ type: "events", events: [evt(S, "assistant", { text: "x" }, 6), evt(M, "assistant", { text: "y" }, 7)] }));
  const all = rows(ctx.__body);
  assert.equal(all[5].classList.contains("fhid"), true);
  assert.equal(all[6].classList.contains("fhid"), false);
  const off = chips(ctx.__fbar).find((c) => String(c.className).includes(" off"));
  off.onclick();
  assert.equal(rows(ctx.__body).filter((r) => r.classList.contains("fhid")).length, 0);
});
