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
// 기억 키는 PC 별(quick-switch 3단계·결정 9): 로컬 화면 = sm_tabs_local, 릴레이 = sm_tabs_<그 PC 방 이름>. 최근 본 순서(mru)도 함께.
test("saveTabs/restoreTabsOnce: 저장 형태(PC 별 키·최근 본 순서), 목록에 없는 세션 버림, PTY 없으면 dead, 1회만, 깨진 저장은 무시", async () => {
  const ctx = await load({ setup: (sb) => sb.localStorage.setItem("sm_room", "rm1") });
  const K = "sm_tabs_rm1";
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m)");
  ev(ctx, `TABS.list = [{key:${JSON.stringify(A)}, s:{session_id:${JSON.stringify(A)}}, safe:true, dead:false},
                        {key:${JSON.stringify(B)}, s:{session_id:${JSON.stringify(B)}}, safe:false, dead:false}]; TABS.active = ${JSON.stringify(B)};
           TABS.mru = [${JSON.stringify(B)}, 'closed-tab', ${JSON.stringify(A)}]`);
  ctx.saveTabs();
  const saved = JSON.parse(ctx.localStorage.getItem(K));
  assert.equal(JSON.stringify(saved), JSON.stringify({ active: B, list: [{ key: A, sid: A, safe: true }, { key: B, sid: B, safe: false }], mru: [B, A] }),
    "최근 본 순서는 열린 탭만");
  assert.equal(ctx.localStorage.getItem("sm_tabs"), null, "옛 키(PC 구분 없음)에는 쓰지 않는다");
  // 부팅 상황 재현: 탭 비움, 목록엔 A(PTY 살아있음)·C(PTY 없음) 만 있고 B 는 삭제됨
  ev(ctx, "TABS.list = []; TABS.active = null; TABS.mru = []; _tabsRestored = false");
  ctx.localStorage.setItem(K, JSON.stringify({ active: B, list: [{ key: A, sid: A, safe: true }, { key: B, sid: B }, { key: C, sid: C }], mru: [B, C, A] }));
  ev(ctx, `SESSIONS = [{session_id:${JSON.stringify(A)}, live_terminal:true}, {session_id:${JSON.stringify(C)}, live_terminal:false}]`);
  ctx.restoreTabsOnce();
  const tabs = ev(ctx, "TABS.list");
  same(tabs.map((t) => [t.key, t.safe, t.dead]), [[A, true, false], [C, false, true]]);
  assert.equal(ev(ctx, "TABS.active"), null, "저장된 활성 탭(B)이 사라졌으면 활성 없음");
  same(ev(ctx, "TABS.mru"), [C, A], "최근 본 순서도 되살린다(사라진 B 는 뺀다)");
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("2개 복원"));
  assert.equal(JSON.parse(ctx.localStorage.getItem(K)).list.length, 2, "정리된 워킹셋을 다시 저장");
  // 두 번째 호출은 무동작(목록 갱신마다 불리지만 1회 가드)
  ev(ctx, "TABS.list = []");
  ctx.restoreTabsOnce();
  assert.equal(ev(ctx, "TABS.list.length"), 0);
  // 깨진 저장값 → 예외 없이 무시. 새 키가 있으면(깨졌어도) 옛 키로 돌아가지 않는다
  ev(ctx, "_tabsRestored = false");
  ctx.localStorage.setItem(K, "{not json");
  ctx.localStorage.setItem("sm_tabs", JSON.stringify({ active: A, list: [{ key: A, sid: A }] }));
  ctx.restoreTabsOnce();
  assert.equal(ev(ctx, "TABS.list.length"), 0);
});

test("탭 기억 옛 키(sm_tabs) 옮기기: 이 PC 새 키가 없을 때만 읽고 목록으로 걸러 새 키에 1회 — 옛 키는 그대로, 다른 PC 는 제 목록으로, 로컬은 sm_tabs_local", async () => {
  // 0.28 까지는 PC 구분 없이 sm_tabs 하나 — 릴레이로 두 PC 를 오가면 A·C 처럼 다른 PC 탭이 섞여 있을 수 있다
  const OLD = JSON.stringify({ active: C, list: [{ key: A, sid: A, safe: true }, { key: C, sid: C }] });
  const ctx = await load({ setup: (sb) => { sb.localStorage.setItem("sm_room", "rmA"); sb.localStorage.setItem("sm_tabs", OLD); } });
  ev(ctx, "toast = () => {}");
  const restore = (sessions) => { ev(ctx, `TABS.list = []; TABS.active = null; TABS.mru = []; _tabsRestored = false; SESSIONS = ${JSON.stringify(sessions)}`); ctx.restoreTabsOnce(); };
  const got = {};
  restore([{ session_id: A, live_terminal: true }]);              // PC A 의 첫 복원(이 PC 목록엔 A 만)
  got.a = { tabs: ev(ctx, "TABS.list.map((t) => t.key).join(',')"), saved: JSON.parse(ctx.localStorage.getItem("sm_tabs_rmA")).list.map((t) => t.key).join(',') };
  ctx.localStorage.setItem("sm_tabs", JSON.stringify({ active: D, list: [{ key: D, sid: D }] }));   // 옛 버전 창이 옛 키를 또 고쳐도
  restore([{ session_id: A, live_terminal: true }, { session_id: D, live_terminal: true }]);
  got.again = ev(ctx, "TABS.list.map((t) => t.key).join(',')");  // 이제 새 키만 본다(1회)
  ctx.localStorage.setItem("sm_tabs", OLD);
  ctx.localStorage.setItem("sm_room", "rmB");                     // 릴레이로 PC B 를 쓰는 중 — B 의 새 키는 아직 없다
  restore([{ session_id: C, live_terminal: false }]);
  got.b = { tabs: ev(ctx, "TABS.list.map((t) => t.key).join(',')"), active: ev(ctx, "TABS.active"), saved: JSON.parse(ctx.localStorage.getItem("sm_tabs_rmB")).list.length };
  got.oldKept = ctx.localStorage.getItem("sm_tabs") === OLD;
  const L = await load({ location: { pathname: "/app" }, setup: (sb) => sb.localStorage.setItem("sm_tabs", OLD) });
  ev(L, "RELAY_APP_URL = ''; toast = () => {}");               // 로컬 부팅의 릴레이 PC 목록 동기화(15초 타이머) 끔
  ev(L, `SESSIONS = [{ session_id: ${JSON.stringify(A)}, live_terminal: true }]`); L.restoreTabsOnce();
  got.local = { tabs: ev(L, "TABS.list.map((t) => t.key).join(',')"), key: !!L.localStorage.getItem("sm_tabs_local"), relayKey: L.localStorage.getItem("sm_tabs_relay") };
  assert.deepEqual(got, {
    a: { tabs: A, saved: A }, again: A,
    b: { tabs: C, active: C, saved: 1 }, oldKept: true,
    local: { tabs: A, key: true, relayKey: null },
  });
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

test("openMonitorGroupById: 저장 그룹 조회 → openMonitor(ids=관리+하위, gid 갱신 모드) / 없으면 toast / 같은 팀이 살아 있으면 그대로, 끊겼거나 다른 팀이면 교체", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);"
        + "globalThis.__opens = []; openMonitor = (ids, mgr, labels, opts) => globalThis.__opens.push({ ids, mgr, labels, opts });"
        + `fetchMonGroups = async () => [{ id: 'g1', name: '팀', manager: ${JSON.stringify(A)}, subs: [${JSON.stringify(B)}, ${JSON.stringify(A)}], labels: { ${JSON.stringify(A)}: '관리' } }]`);
  await ctx.openMonitorGroupById("g1");
  const o = ev(ctx, "globalThis.__opens")[0];
  same(o.ids, [A, B], "관리 세션이 앞, 중복 제거");
  assert.equal(o.mgr, A);
  assert.equal(JSON.stringify(o.opts), JSON.stringify({ gid: "g1", name: "팀", inline: true }), "데스크톱 폭이면 오른쪽 칸에");
  await ctx.openMonitorGroupById("nope");
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("찾을 수 없습니다"));
  // 이미 관제 덮개(폰)가 떠 있음: 같은 팀이 살아 있으면 갈아엎지 않고, 끊겼거나 다른 팀이면 닫고 알림의 팀으로 바꾼다(quick-switch 1-5)
  const ov = stubEl("div"); let dead = false, closed = 0;
  ov._mon = { gid: "g1", dead: () => dead, close: () => { closed++; } };
  // 떠 있는 폰 덮개는 MON_OV 로 든다 — 같은 id 의 상세 칸 관제(창을 좁히기 전에 연 것)와 헷갈리지 않게(quick-switch 검토)
  ctx.__fakeMon = ov._mon; ev(ctx, "MON_OV = globalThis.__fakeMon");
  await ctx.openMonitorGroupById("g1");
  assert.equal(ev(ctx, "globalThis.__opens.length"), 1, "같은 팀이 살아 있으면 갈아엎지 않는다");
  dead = true;                                                       // 관제 스트림이 끊김(연결 종료)
  await ctx.openMonitorGroupById("g1");
  assert.deepEqual([ev(ctx, "globalThis.__opens.length"), closed], [2, 1], "끊긴 덮개는 닫고 다시 연다");
  dead = false; ov._mon.gid = "g0";                                  // 다른 팀 관제가 떠 있음
  await ctx.openMonitorGroupById("g1");
  assert.deepEqual([ev(ctx, "globalThis.__opens.length"), closed], [3, 2], "다른 팀이면 알림의 팀으로 바꾼다");
  await ctx.openMonitorGroupById("");
  assert.equal(ev(ctx, "globalThis.__opens.length"), 3);
});

test("폰 관제 덮개 + 관제 알림: 진짜 덮개가 _mon(팀·끊김·닫기)을 달고, 끊기면(eof) 알림이 덮개를 닫고 새로 연다(quick-switch 1-5)", async () => {
  const ctx = await load();
  ev(ctx, `innerWidth = 390; globalThis.__mon = [];
    T.monitor = (ids, mgr, cb, gid) => { const h = { gid, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    fetchMonGroups = async () => [{ id: 'g1', name: '팀', manager: 'm1', subs: ['w1'] }];`);
  await ctx.openMonitorGroupById("g1");
  const ov = ctx.document.body.children.at(-1);
  assert.equal(String(ov.className), "mon-ov", "폰은 전체 화면 덮개");
  assert.equal(ov._mon.gid, "g1");
  ctx.document._register("mon-ov", ov);                              // 하네스 getElementById 는 등록한 것만 찾는다
  await ctx.openMonitorGroupById("g1");
  assert.equal(ev(ctx, "__mon.length"), 1, "살아 있는 같은 팀은 그대로");
  ev(ctx, "__mon[0].cb(null, true)");                                // 관제 스트림 끊김(연결 종료)
  assert.equal(ov._mon.dead(), true);
  await ctx.openMonitorGroupById("g1");
  assert.deepEqual([ev(ctx, "__mon.length"), ev(ctx, "__mon[0].closed")], [2, 1], "끊긴 덮개는 닫고 새로 연다");
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
