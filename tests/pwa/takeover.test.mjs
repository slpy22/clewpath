// 화면 소유권 인계(v0.9.4) — 통지 감지로 taken, 자동 재접속 제외, 가져올 때 확인창.
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

let ctx;
const A = "aaaaaaaa-1111-2222-3333-444444444444";

before(async () => { ctx = await load(); });

test("shouldConfirmTakeover: taken 탭·남의 화면이면 묻고, 내 화면·빈 화면이면 안 묻는다", () => {
  const f = ctx.shouldConfirmTakeover;
  assert.equal(f({ screen: null }, null), false);
  assert.equal(f({ screen: { attached: false } }, null), false);
  assert.equal(f({ screen: { attached: true, screen_id: "x" } }, null), true);          // 남의 화면, 내 탭 없음
  assert.equal(f({}, { key: "k", taken: true }), true);                                 // 우리 화면이 밀려남
  ev(ctx, "termView.tabs = { k: { screenId: 'mine' } }");
  assert.equal(f({ screen: { attached: true, screen_id: "mine" } }, { key: "k" }), false);   // 내 화면 → 재접속
  assert.equal(f({ screen: { attached: true, screen_id: "other" } }, { key: "k" }), true);
  ev(ctx, "termView.tabs = {}");
});

test("스트림 통지 → taken, eof 는 disconnected 로 안 가고 자동 재접속도 건너뛴다", async () => {
  // 하네스의 location 은 /relay/app 이라 MODE 는 이미 'relay'(const). T.terminal 을 가로채 콜백을 잡는다
  assert.equal(ev(ctx, "MODE"), "relay");
  ev(ctx, "globalThis.__tcb = null; T.terminal = (sid, cb, opts) => { globalThis.__tcb = cb; return { close() {} }; }");
  ctx.ensurePriv = async () => ({});
  ev(ctx, "TABS.list = []; TABS.active = null");
  const s = { session_id: A, title: "A" };
  await ctx.loadTerminal(s, false);
  assert.ok(ctx.__tcb, "T.terminal 이 불리지 않음");
  const key = ev(ctx, "TABS.active");
  const rec = () => ev(ctx, "TABS.list").find((t) => t.key === key);
  assert.equal(!!rec().taken, false);
  ctx.__tcb(`\r\n[ClewPath] ${ev(ctx, "TAKEOVER_NOTE")} — 여기서는 더 입력할 수 없습니다.\r\n`, false);
  assert.equal(rec().taken, true);
  ctx.__tcb(null, true);                                       // 서버가 소켓을 닫음
  assert.equal(ev(ctx, "termView.disconnected"), false, "빼앗김은 장애가 아니다");
  const calls = [];
  ctx.switchTab = (k) => calls.push(k);
  ev(ctx, "termView.disconnected = true");                     // 설령 disconnected 였어도
  ctx.reattachForegroundTab();
  assert.deepEqual(calls, [], "taken 탭은 자동으로 되찾지 않는다(핑퐁 차단)");
  ev(ctx, "termView.disconnected = false");
  await new Promise((r) => setTimeout(r, 350));
});

test("탭을 눌러 가져오기: 확인창 → attach 다시 → taken 해제 / 취소하면 그대로", async () => {
  const key = ev(ctx, "TABS.active");
  const rec = () => ev(ctx, "TABS.list").find((t) => t.key === key);
  const s = rec().s;
  let asked = 0; ctx.confirm = () => { asked++; return false; };
  ev(ctx, "globalThis.__tcb = null");
  await ctx.loadTerminal(s, false);
  assert.equal(asked, 1); assert.equal(rec().taken, true); assert.equal(ctx.__tcb, null, "취소면 붙지 않는다");
  ctx.confirm = () => { asked++; return true; };
  await ctx.loadTerminal(s, false);
  assert.equal(asked, 2); assert.equal(rec().taken, false); assert.ok(ctx.__tcb, "확인하면 다시 붙는다");
});
