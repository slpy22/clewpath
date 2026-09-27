// PWA 순수 함수·라우팅 계약 테스트 (node --test). pytest 의 test_pwa_js.py 가 이 파일을 감싼다.
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

let ctx;
before(async () => { ctx = await load(); });

const now = () => Date.now() / 1000;

test("부트: 스크립트가 헤드리스 스텁 위에서 예외 없이 로드된다", () => {
  assert.equal(typeof ctx.sessionBadge, "function");
  assert.equal(typeof ctx.jumpToSession, "function");
  assert.equal(ev(ctx, "typeof TABS"), "object");
});

test("sessionBadge: 훅 상태 + 신선도 게이트", () => {
  ev(ctx, "CLOCK_SKEW = 0");
  const t = now();
  // vm 컨텍스트 객체는 다른 realm 이라 deepStrictEqual 이 프로토타입에서 갈린다 → JSON 비교
  assert.equal(JSON.stringify(ctx.sessionBadge({ runtime: { phase: "thinking", thinking_at: t - 60 } })), JSON.stringify({ c: "#2f81f7", pulse: true, text: "작업 중" }));
  assert.equal(ctx.sessionBadge({ runtime: { phase: "thinking", thinking_at: t - 3 * 3600 } }), null);   // 2h 창 밖
  assert.equal(ctx.sessionBadge({ runtime: { phase: "permission", permission_at: t - 10 } }).text, "권한 대기");
  assert.equal(ctx.sessionBadge({ runtime: { phase: "waiting", waiting_at: t - 10 } }).text, "입력 대기");
  assert.equal(ctx.sessionBadge({ runtime: { phase: "ready", ready_at: t - 30 } }).pulse, false);
  assert.equal(ctx.sessionBadge({ runtime: { phase: "ready", ready_at: t - 2 * 3600 } }), null);
  assert.equal(ctx.sessionBadge({}), null);
});

test("externallyActive: 피어 레지스트리·agents·훅 순서", () => {
  ev(ctx, "CLOCK_SKEW = 0");
  assert.equal(ctx.externallyActive({ peer: { name: "w" }, live_terminal: false }), true);     // v0.9.0 1차-b
  assert.equal(ctx.externallyActive({ peer: { name: "w" }, live_terminal: true }), false);     // 우리 PTY 면 무관
  assert.equal(ctx.externallyActive({ agent: { kind: "interactive" }, live_terminal: false }), true);
  assert.equal(ctx.externallyActive({ agent: { kind: "background" }, live_terminal: false }), false);  // bg 는 별도 게이트
  assert.equal(ctx.externallyActive({ runtime: { phase: "ready", active_at: now() - 60 } }), true);    // 10분 내 활동
  assert.equal(ctx.externallyActive({ runtime: { phase: "ready", active_at: now() - 3600 } }), false);
});

test("tabEvKind / relIso: 배지 툴팁 문구", () => {
  assert.equal(ctx.tabEvKind({ role: "assistant", tools: ["Bash", "Read"] }), "도구 실행(Bash, Read)");
  assert.equal(ctx.tabEvKind({ role: "assistant", tools: ["a", "b", "c", "d"] }), "도구 실행(a, b, c 외)");
  assert.equal(ctx.tabEvKind({ role: "assistant", text: "hi" }), "응답");
  assert.equal(ctx.tabEvKind({ role: "user", tool_results: [{}] }), "도구 결과");
  assert.equal(ctx.tabEvKind({ role: "user", text: "x" }), "프롬프트 입력");
  assert.equal(ctx.relIso("garbage"), "");
  assert.equal(ctx.relIso(new Date().toISOString()), "방금");
});

test("jumpToSession: 탭 → 전환, live_terminal → 터미널, 그 외 → 상세 (알림으로 새 claude 안 띄움)", () => {
  const calls = [];
  ctx.switchTab = (k) => calls.push(["switch", k]);
  ctx.loadTerminal = (s) => calls.push(["term", s.session_id]);
  ctx.loadDetail = (s) => calls.push(["detail", s.session_id]);
  ev(ctx, "TABS.list = [{key:'k1', s:{session_id:'A'}, safe:false, dead:false, act:false}]; TABS.active = 'k1'");
  ctx.jumpToSession({ session_id: "A" });
  ctx.jumpToSession({ session_id: "B", live_terminal: true });
  ctx.jumpToSession({ session_id: "C", live_terminal: false });
  assert.deepEqual(calls, [["switch", "k1"], ["term", "B"], ["detail", "C"]]);
});

test("openSessionById: 판단 전 목록 재조회, 이미 보는 중이면 무동작, 미지 세션은 보류", async () => {
  const calls = [];
  ctx.switchTab = (k) => calls.push(["switch", k]);
  ctx.loadTerminal = (s) => calls.push(["term", s.session_id]);
  ctx.loadDetail = (s) => calls.push(["detail", s.session_id]);
  ev(ctx, "TABS.list = []; TABS.active = null; CURRENT_TERM_SID = null; SESSIONS = []");
  ev(ctx, "T.list = async () => ({ sessions: [{session_id:'X', live_terminal:true}], now: Date.now()/1000 })");
  await ctx.openSessionById("X");
  assert.deepEqual(calls, [["term", "X"]]);                    // stale 캐시가 아니라 새 목록으로 판단
  ev(ctx, "CURRENT_TERM_SID = 'X'");
  await ctx.openSessionById("X");
  assert.equal(calls.length, 1);                               // 이미 그 터미널을 보는 중
  await ctx.openSessionById("nope");
  assert.equal(ev(ctx, "PENDING_OPEN_SID"), "nope");
});

test("tabBadge._on: 배경 탭만, 구독 이후 ts 만, 건수·최근 종류 누적", async () => {
  ev(ctx, `TABS.list = [{key:'a', s:{session_id:'A'}, act:false}, {key:'b', s:{session_id:'B'}, act:false}]; TABS.active = 'a'`);
  const arm = new Date(Date.now() - 60000).toISOString();
  const tabBadge = ev(ctx, "tabBadge");                                          // const 바인딩 → 컨텍스트에서 읽는다
  tabBadge.armedAt = { A: arm, B: arm };
  const fresh = new Date().toISOString(), old = new Date(Date.now() - 120000).toISOString();
  tabBadge._on(JSON.stringify({ type: "events", events: [
    { session_id: "A", ts: fresh, role: "assistant", tools: ["Bash"] },        // 전경 → 무시
    { session_id: "B", ts: old, role: "assistant", tools: ["Bash"] },          // 구독 전 → 무시
    { session_id: "B", ts: fresh, role: "assistant", tools: ["Bash"] },
    { session_id: "B", ts: fresh, role: "user", tool_results: [{}] },
  ] }));
  tabBadge._on(JSON.stringify({ type: "heartbeat" }));                          // 무시
  const tabs = ev(ctx, "TABS.list");
  assert.equal(tabs[0].act, false);
  assert.equal(tabs[1].act, true);
  assert.equal(tabs[1].actN, 2);
  assert.equal(tabs[1].last.what, "도구 결과");
  await new Promise((r) => setTimeout(r, 350));                                  // renderTabBarSoon 코얼레싱 소진
});

test("관제 알림 라벨/기본값에 error(호출 실패) 포함", () => {
  const labels = ev(ctx, "MON_NOTIFY_LABELS"), defaults = ev(ctx, "MON_NOTIFY_DEFAULT");
  assert.ok("error" in labels && defaults.error === true);
  assert.equal(defaults.sub_start, false);
});
