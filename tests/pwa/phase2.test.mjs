// PWA 테스트 Phase 2(2026-09-28): 탭 워킹셋 저장/복원 · 알림 진입(#open/#monitor) 소비 · 탭 스트립 칩 클래스.
// 관제 필터 판정(rowMatches)은 monitor.test.mjs 가 openMonitor 를 실제로 열어 이미 검증한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b) => assert.equal(JSON.stringify(a), JSON.stringify(b));   // vm realm 배열은 deepEqual 불가

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const C = "cccccccc-1111-2222-3333-444444444444";
const D = "dddddddd-1111-2222-3333-444444444444";

// ---------------------------------------------------------------- 탭 워킹셋 저장/복원
test("saveTabs/restoreTabsOnce: 저장 형태, 목록에 없는 세션 버림, PTY 없으면 dead, 1회만, 깨진 저장은 무시", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m)");
  ev(ctx, `TABS.list = [{key:${JSON.stringify(A)}, s:{session_id:${JSON.stringify(A)}}, safe:true, dead:false},
                        {key:${JSON.stringify(B)}, s:{session_id:${JSON.stringify(B)}}, safe:false, dead:false}]; TABS.active = ${JSON.stringify(B)}`);
  ctx.saveTabs();
  const saved = JSON.parse(ctx.localStorage.getItem("sm_tabs"));
  assert.equal(JSON.stringify(saved), JSON.stringify({ active: B, list: [{ key: A, sid: A, safe: true }, { key: B, sid: B, safe: false }] }));
  // 부팅 상황 재현: 탭 비움, 목록엔 A(PTY 살아있음)·C(PTY 없음) 만 있고 B 는 삭제됨
  ev(ctx, "TABS.list = []; TABS.active = null; _tabsRestored = false");
  ctx.localStorage.setItem("sm_tabs", JSON.stringify({ active: B, list: [{ key: A, sid: A, safe: true }, { key: B, sid: B }, { key: C, sid: C }] }));
  ev(ctx, `SESSIONS = [{session_id:${JSON.stringify(A)}, live_terminal:true}, {session_id:${JSON.stringify(C)}, live_terminal:false}]`);
  ctx.restoreTabsOnce();
  const tabs = ev(ctx, "TABS.list");
  same(tabs.map((t) => [t.key, t.safe, t.dead]), [[A, true, false], [C, false, true]]);
  assert.equal(ev(ctx, "TABS.active"), null, "저장된 활성 탭(B)이 사라졌으면 활성 없음");
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("2개 복원"));
  assert.equal(JSON.parse(ctx.localStorage.getItem("sm_tabs")).list.length, 2, "정리된 워킹셋을 다시 저장");
  // 두 번째 호출은 무동작(목록 갱신마다 불리지만 1회 가드)
  ev(ctx, "TABS.list = []");
  ctx.restoreTabsOnce();
  assert.equal(ev(ctx, "TABS.list.length"), 0);
  // 깨진 저장값 → 예외 없이 무시
  ev(ctx, "_tabsRestored = false");
  ctx.localStorage.setItem("sm_tabs", "{not json");
  ctx.restoreTabsOnce();
  assert.equal(ev(ctx, "TABS.list.length"), 0);
});

// ---------------------------------------------------------------- 알림 진입 소비
test("#monitor=<gid> 콜드 스타트 → consumePendingMonitor 가 1회만 그룹을 연다", async () => {
  const ctx = await load({ hash: "#monitor=g1" });
  assert.equal(ev(ctx, "PENDING_OPEN_GID"), "g1");
  ev(ctx, "globalThis.__gids = []; openMonitorGroupById = async (g) => globalThis.__gids.push(g)");
  ctx.consumePendingMonitor();
  ctx.consumePendingMonitor();
  same(ev(ctx, "globalThis.__gids"), ["g1"]);
  assert.equal(ev(ctx, "PENDING_OPEN_GID"), "");
});

test("openMonitorGroupById: 저장 그룹 조회 → openMonitor(ids=관리+하위, gid 갱신 모드) / 없으면 toast / 이미 관제 중이면 무시", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);"
        + "globalThis.__opens = []; openMonitor = (ids, mgr, labels, opts) => globalThis.__opens.push({ ids, mgr, labels, opts });"
        + `fetchMonGroups = async () => [{ id: 'g1', name: '팀', manager: ${JSON.stringify(A)}, subs: [${JSON.stringify(B)}, ${JSON.stringify(A)}], labels: { ${JSON.stringify(A)}: '관리' } }]`);
  await ctx.openMonitorGroupById("g1");
  const o = ev(ctx, "globalThis.__opens")[0];
  same(o.ids, [A, B], "관리 세션이 앞, 중복 제거");
  assert.equal(o.mgr, A);
  assert.equal(JSON.stringify(o.opts), JSON.stringify({ gid: "g1", name: "팀" }));
  await ctx.openMonitorGroupById("nope");
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("찾을 수 없습니다"));
  ctx.document._register("mon-ov", stubEl("div"));                  // 이미 관제 오버레이가 떠 있음
  await ctx.openMonitorGroupById("g1");
  assert.equal(ev(ctx, "globalThis.__opens.length"), 1, "갈아엎지 않는다");
  await ctx.openMonitorGroupById("");
  assert.equal(ev(ctx, "globalThis.__opens.length"), 1);
});

test("#open=<sid> 콜드 스타트 → consumePendingOpen 이 목록에서 접두 일치로 찾아 jumpToSession, 없으면 버린다", async () => {
  const ctx = await load({ hash: "#open=" + A.slice(0, 8) });
  assert.equal(ev(ctx, "PENDING_OPEN_SID"), A.slice(0, 8));
  ev(ctx, "globalThis.__jumps = []; jumpToSession = (s) => globalThis.__jumps.push(s.session_id)");
  ev(ctx, "SESSIONS = []");
  ctx.consumePendingOpen();                                          // 목록에 없음 → 소비하고 끝(무한 대기 없음)
  same(ev(ctx, "globalThis.__jumps"), []);
  assert.equal(ev(ctx, "PENDING_OPEN_SID"), "");
  ev(ctx, `PENDING_OPEN_SID = ${JSON.stringify(A.slice(0, 8))}; SESSIONS = [{session_id:${JSON.stringify(B)}}, {session_id:${JSON.stringify(A)}}]`);
  ctx.consumePendingOpen();
  same(ev(ctx, "globalThis.__jumps"), [A]);
});

// ---------------------------------------------------------------- 탭 스트립 칩 클래스
test("renderTabBar: 칩 클래스(sel/st/stpulse/dead/taken)·--st 색·툴팁·활동 점·'+ 탭' 버튼", async () => {
  const ctx = await load();
  ev(ctx, "CLOCK_SKEW = 0");
  const now = Date.now() / 1000;
  ev(ctx, `SESSIONS = [
    {session_id:${JSON.stringify(A)}, title:'A', runtime:{phase:'thinking', thinking_at:${now - 30}}},
    {session_id:${JSON.stringify(B)}, title:'B', runtime:{phase:'ready', ready_at:${now - 30}}},
    {session_id:${JSON.stringify(C)}, title:'C', runtime:{phase:'thinking', thinking_at:${now - 30}}},
    {session_id:${JSON.stringify(D)}, title:'D'}]`);
  ev(ctx, `TABS.list = [
    {key:${JSON.stringify(A)}, s:{session_id:${JSON.stringify(A)}, title:'old'}, dead:false, act:false},
    {key:${JSON.stringify(B)}, s:{session_id:${JSON.stringify(B)}}, dead:false, act:true, actN:2, last:{what:'도구 결과', at:new Date().toISOString()}},
    {key:${JSON.stringify(C)}, s:{session_id:${JSON.stringify(C)}}, dead:true, act:false},
    {key:${JSON.stringify(D)}, s:{session_id:${JSON.stringify(D)}}, dead:false, act:false, taken:true}]; TABS.active = ${JSON.stringify(A)}`);
  const strip = stubEl("div");
  ctx.__strip = strip;
  ev(ctx, "termView.strip = globalThis.__strip; tabBadge.sync = () => {}");
  ctx.renderTabBar();
  const kids = strip.children;
  assert.equal(kids.length, 5, "칩 4 + '+ 탭'");
  const [a, b, c, d, add] = kids;
  assert.equal(String(a.className), "lchip sel st stpulse", "전경 + 작업 중(맥동)");
  assert.equal(a.style["--st"], "#2f81f7");
  assert.ok(a.text.includes("A"), "제목은 목록의 최신 객체(old → A)");
  assert.ok(a.title.includes("상태: 작업 중") && a.title.includes(A));
  assert.equal(String(b.className), "lchip st", "완료는 고정 초록(맥동 없음)");
  assert.equal(b.style["--st"], "#3fb950");
  assert.ok(b.children.some((k) => String(k.className) === "lact"), "배경 탭 활동 점");
  assert.ok(b.title.includes("● 새 출력 2건 · 최근: 도구 결과 (방금)"));
  assert.equal(String(c.className), "lchip dead", "중지된 탭은 상태 밑줄 없음");
  assert.ok(c.title.startsWith("[중지됨 — 열면 새로 시작] "));
  assert.equal(String(d.className), "lchip taken");
  assert.ok(d.title.startsWith("[다른 기기에서 보는 중 — 누르면 가져오기] "));
  assert.equal(add.tagName, "BUTTON"); assert.equal(String(add.className), "ladd"); assert.equal(add.textContent, "+ 탭");
  assert.equal(a.children.filter((k) => String(k.className) === "lx").length, 1, "닫기 × 는 칩마다 1개");
  // 전경 탭 변경 → 재렌더에서만 활성 칩 scrollIntoView
  let scrolls = 0; strip.querySelector = () => ({ scrollIntoView() { scrolls++; } });
  ctx.renderTabBar(); assert.equal(scrolls, 0, "첫 렌더가 이미 활성 탭을 기억(strip._sel) → 같은 활성이면 스크롤 없음");
  ev(ctx, `TABS.active = ${JSON.stringify(B)}`); ctx.renderTabBar(); assert.equal(scrolls, 1);
  ctx.renderTabBar(); assert.equal(scrolls, 1);
});
