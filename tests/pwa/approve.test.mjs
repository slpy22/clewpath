// ✋ 터미널 선택(2026-11-04 사장님): 🔔·✋ 승인 창이 터미널에 떠 있는 선택지를 그대로 보여 주고 그 번호(+글자)를 보낸다.
// 옛 고정 버튼(허용·계속 허용·거부 = 1·2·Esc)은 터미널 선택지와 달라 무엇이 골라질지 몰랐다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const flat = (n) => [n, ...(n.children || []).flatMap(flat)];
const cls = (n) => String(n.className || "");
const txt = (n) => flat(n).map((x) => x.textContent || "").join("|");
const PROMPT = { question: "Do you want to create probe.txt?", footer: "Esc to cancel · Tab to amend", multi: false, context: [],
  choices: [{ n: 1, label: "Yes", more: "", selected: true },
    { n: 2, label: "Yes, and switch to accept edits for this session", more: "(shift+tab)", selected: false },
    { n: 3, label: "No", more: "", selected: false }] };

async function boot(reply) {
  const ctx = await load();
  ctx.__reply = reply;
  ev(ctx, `globalThis.__calls = []; globalThis.__m = null; globalThis.__jump = [];
    openModal = (t, b) => { globalThis.__m = b; globalThis.__t = t; }; closeModal = () => { globalThis.__closed = true; };
    toast = () => {}; jumpToSession = (s) => __jump.push(s.session_id);
    T = Object.assign({}, T, { api: async (v, p, o) => { __calls.push([v, p, o && o.body]); return __reply(v, p, o && o.body); } });
    SESSIONS = [{ session_id: 's1', title: '가', cwd: 'E:/p' }];`);
  return ctx;
}

test("선택 창: 터미널의 선택지 그대로(번호·글자·덧말) · 무엇을 하려는지 · 누르면 번호+글자를 보낸다", async () => {
  const ctx = await boot((v, p) => v === "GET" ? { terminal: true, prompt: PROMPT, tool: { name: "Write", detail: "probe.txt" } } : { ok: true });
  await ev(ctx, "showApprove(SESSIONS[0])");
  const m = ev(ctx, "__m");
  assert.equal(ev(ctx, "__t"), "✋ 터미널 선택");
  assert.ok(txt(m).includes("파일 만들기 · probe.txt"), "대화 기록의 도구 호출로 무엇을 하려는지");
  assert.ok(txt(m).includes("Do you want to create probe.txt?"));
  const btns = flat(m).filter((x) => cls(x).includes("apv-c"));
  assert.deepEqual(btns.map((b) => flat(b).find((x) => cls(x) === "apv-n").textContent), ["1", "2", "3"]);
  assert.ok(txt(btns[1]).includes("(shift+tab)"), "덧말도 보인다");
  assert.ok(cls(btns[2]).includes("no"), "No 는 빨갛게");
  await btns[1].onclick();
  assert.equal(JSON.stringify(ev(ctx, "__calls").at(-1)),
    JSON.stringify(["POST", "/api/owner/sessions/s1/respond", { action: "choose", n: 2, label: "Yes, and switch to accept edits for this session" }]));
  assert.ok(ev(ctx, "__closed"));
});

test("선택지가 바뀌었으면(409 changed) 새로 읽어 다시 보여 준다 · Esc 취소", async () => {
  let n = 0;
  const ctx = await boot((v, p, b) => {
    if (v === "GET") { n++; return { terminal: true, prompt: n === 1 ? PROMPT : { ...PROMPT, question: "새 질문" }, tool: null }; }
    if (b.action === "choose") { const e = new Error("409"); e.json = { error: "changed" }; throw e; }
    return { ok: true };
  });
  await ev(ctx, "showApprove(SESSIONS[0])");
  const m = ev(ctx, "__m");
  await flat(m).filter((x) => cls(x).includes("apv-c"))[0].onclick();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(n, 2, "다시 읽었다");
  assert.ok(txt(m).includes("새 질문") && txt(m).includes("바뀌었습니다"));
  await flat(m).find((x) => x.textContent === "Esc — 취소").onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(ev(ctx, "__calls").at(-1)[2])), { action: "cancel" });
});

test("선택 창이 없거나 · 여러 개 고르기 · ClewPath 터미널이 아니면 버튼 없이 안내(터미널 열기)", async () => {
  let r = { terminal: true, prompt: null, tool: null };
  const ctx = await boot(() => r);
  await ev(ctx, "showApprove(SESSIONS[0])");
  let m = ev(ctx, "__m");
  assert.ok(txt(m).includes("선택 창이 없습니다"));
  assert.equal(flat(m).filter((x) => cls(x).includes("apv-c")).length, 0);
  await flat(m).find((x) => x.textContent === "🖥 터미널 열기").onclick();
  assert.deepEqual([...ev(ctx, "__jump")], ["s1"]);
  r = { terminal: true, tool: null, prompt: { ...PROMPT, multi: true } };
  await ev(ctx, "showApprove(SESSIONS[0])"); m = ev(ctx, "__m");
  assert.equal(flat(m).filter((x) => cls(x).includes("apv-c")).length, 0, "여러 개 고르기는 터미널에서");
  assert.ok(txt(m).includes("1. Yes"));
  r = { terminal: false, prompt: null, tool: null };
  await ev(ctx, "showApprove(SESSIONS[0])"); m = ev(ctx, "__m");
  assert.ok(txt(m).includes("ClewPath 터미널로 실행 중이 아니라"));
});

test("🔔 입력 대기 항목: 터미널에 선택 창이 보이면(runtime.choices) 선택 창, 아니면 그 세션으로", async () => {
  const ctx = await load();
  const now = Math.floor(Date.now() / 1000);
  ev(ctx, `CLOCK_SKEW = 0; SESSIONS = [
    { session_id: 'q', title: '질문', cwd: 'E:/a', runtime: { phase: 'waiting', waiting_at: ${now}, choices: true } },
    { session_id: 'w', title: '대기', cwd: 'E:/b', runtime: { phase: 'waiting', waiting_at: ${now} } }];
    globalThis.__c = []; showApprove = (s) => __c.push(['approve', s.session_id]); jumpToSession = (s) => __c.push(['jump', s.session_id]);
    T = Object.assign({}, T, { api: async () => ({ approvals: [] }) }); openPop = (a, box) => { globalThis.__box = box; POP = { box: { classList: { add() {} }, setAttribute() {} } }; };`);
  await ev(ctx, "showTodo()");
  const items = flat(ev(ctx, "__box")).filter((x) => cls(x).includes("todo-it") && cls(x).includes("k-wait"));
  assert.equal(items.length, 2);
  assert.ok(txt(items[0]).includes("선택지가 떠 있음"));
  items.forEach((b) => b.onclick());
  assert.deepEqual(JSON.parse(JSON.stringify(ev(ctx, "__c"))), [["approve", "q"], ["jump", "w"]]);
});
