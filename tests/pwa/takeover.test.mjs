// 화면 소유권 인계(v0.9.4) — 통지 감지로 taken, 자동 재접속 제외, 가져올 때 확인창.
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

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

// quick-switch 1-1: 이 페이지가 스스로 놓은 화면 id(RELEASED_SCREENS). 20초 목록이 아직 그 화면을 '붙어 있음'으로 보여도
// 내 옛 화면이므로 묻지 않는다 — 정리한 터미널로 금방 돌아올 때의 가짜 인계 확인창 방지(흐름 검사는 quickswitch.test T6).
test("shouldConfirmTakeover: 이 페이지가 놓은 화면은 20초 목록에 남아 있어도 묻지 않는다 — 밀려난 탭(taken)·남의 화면은 묻는다", async () => {
  const c = await load();
  const f = c.shouldConfirmTakeover;
  const old = { screen: { attached: true, screen_id: "scr-old" } };
  assert.equal(f(old, { key: "k" }), true, "모르는 화면은 묻는다");
  ev(c, "releaseScreen('scr-old')");
  assert.equal(f(old, { key: "k" }), false, "내가 놓은 옛 화면 → 그냥 다시 붙는다");
  assert.equal(f(old, null), false, "탭 기록이 없어도 같다");
  assert.equal(f(old, { key: "k", taken: true }), true, "밀려난 탭은 그래도 묻는다");
  assert.equal(f({ screen: { attached: true, screen_id: "scr-other" } }, { key: "k" }), true, "남의 화면은 묻는다");
});

test("탭 인스턴스 해체·새로 띄우기가 놓은 화면 id 를 기억한다(최대 50, 오래된 것부터 버림)", async () => {
  const c = await load();
  c.document._register("paneDetail", stubEl("div"));      // 같은 칸이라야 두 번째 열기가 fast-path(attach)를 탄다
  ev(c, "globalThis.__tcb = null; globalThis.__scr = []; T.terminal = (sid, cb, opts) => { globalThis.__tcb = cb; __scr.push(opts.screen); return { close() {}, send() {} }; }");
  c.ensurePriv = async () => ({});
  const s = { session_id: A, title: "A" };
  await c.loadTerminal(s, false);
  c.__tcb("\r\n── 세션이 종료되었습니다(claude 종료). 연결하면 새로 시작합니다 ──\r\n", false);   // claude 가 끝남
  c.__tcb(null, true);
  await c.loadTerminal(s, false);                           // 다시 열면 새로 띄움 → 새 화면 id
  const [first, second] = ev(c, "__scr");
  assert.ok(first && second && first !== second, "새로 띄우면 새 화면 id 로 받는다");
  assert.equal(ev(c, "JSON.stringify(RELEASED_SCREENS)"), JSON.stringify([first]), "새로 띄우며 놓은 옛 화면");
  ev(c, "leaveTerminalView()");                             // 뷰를 떠남 → 탭 인스턴스 해체
  assert.equal(ev(c, "JSON.stringify(RELEASED_SCREENS)"), JSON.stringify([first, second]), "해체하며 놓은 지금 화면");
  for (let i = 0; i < 49; i++) ev(c, `releaseScreen('scr-n${i}')`);
  assert.equal(ev(c, "RELEASED_SCREENS.length"), 50);
  assert.equal(ev(c, "RELEASED_SCREENS[0]"), second, "51번째가 들어오면 가장 오래된 것부터 버린다");
  assert.equal(c.shouldConfirmTakeover({ screen: { attached: true, screen_id: first } }, null), true, "버려진 옛 화면은 다시 묻는다");
  assert.equal(c.shouldConfirmTakeover({ screen: { attached: true, screen_id: second } }, null), false);
  await new Promise((r) => setTimeout(r, 350));             // resize·탭 막대 타이머 소진
});
