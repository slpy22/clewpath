// PWA 테스트 Phase 3(2026-09-28): 목록 카드 렌더(renderRows) · 서비스워커 알림 클릭 → 페이지 메시지 계약.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const here = dirname(fileURLToPath(import.meta.url));
const SW = join(here, "..", "..", "pwa", "sw.js");

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const C = "cccccccc-1111-2222-3333-444444444444";
const D = "dddddddd-1111-2222-3333-444444444444";

const cls = (e) => String(e.className);
const chipsOf = (row) => row.children[0].children[0].children.filter((k) => cls(k).startsWith("schip")).map((k) => k.textContent);
const rows = (wrap) => wrap.children.filter((k) => cls(k) === "glist").flatMap((g) => g.children);

// ---------------------------------------------------------------- 목록 카드
test("renderRows: 폴더 그룹(최신순)·상태 점/문구·🔗 피어 칩(우리 PTY 면 없음)·🤖/⚙/#라벨/🧠 칩·🖥 vs ▶·권한 대기 ✋", async () => {
  const ctx = await load();
  ev(ctx, "CLOCK_SKEW = 0");
  const now = Date.now() / 1000;
  const iso = (secAgo) => new Date(Date.now() - secAgo * 1000).toISOString();
  ev(ctx, `SESSIONS = [
    {session_id:${JSON.stringify(A)}, title:'알파', project_folder:'F--p1', cwd:'F:/p1', ended_at:${JSON.stringify(iso(60))}, message_count:3, size_bytes:2048,
     runtime:{phase:'thinking', thinking_at:${now - 30}}, peer:{name:'w1', status:'busy'}, live_terminal:false, labels:['팀'], resume_model:'claude-opus-5', model_override:true},
    {session_id:${JSON.stringify(B)}, slug:'beta-slug', project_folder:'F--p1', cwd:'F:/p1/sub', ended_at:${JSON.stringify(iso(3600 * 5))}, message_count:0,
     runtime:{phase:'permission', permission_at:${now - 10}}, peer:{name:'w2'}, live_terminal:true},
    {session_id:${JSON.stringify(C)}, title:'감마', project_folder:'F--p2', cwd:'F:/p2', ended_at:${JSON.stringify(iso(10))}, agent:{kind:'background', name:'bg-1'}, picker_hidden:true},
    {session_id:${JSON.stringify(D)}, title:'델타', project_folder:'F--p2', cwd:'F:/p2', ended_at:${JSON.stringify(iso(20))}, picker_hidden:true, picker_expose:true}
  ]; SELECTED_ID = ${JSON.stringify(D)}; ACTIVE_LABEL = ''; COLLAPSED.clear()`);
  const wrap = stubEl("div"); ctx.__wrap = wrap;
  ev(ctx, "renderRows(globalThis.__wrap, '')");
  // 그룹: p2(최신 10초 전)가 p1(60초 전)보다 앞
  const heads = wrap.children.filter((k) => cls(k) === "ghead");
  assert.equal(heads.length, 2);
  same(heads.map((h) => h.children[1].children[0].textContent), ["📁 p2", "📁 p1"], "폴더 최신순");
  same(heads.map((h) => h.children[2].textContent), ["2", "2"], "그룹 건수");
  assert.equal(heads[1].children[1].children[1].textContent, "F:/p1", "대표 cwd = 가장 짧은 경로");
  const r = rows(wrap);
  assert.equal(r.length, 4);
  const byId = Object.fromEntries(r.map((row) => [row.dataset.sid, row]));
  // A: 작업 중(맥동 점) + 🔗 피어(우리 PTY 아님) + #라벨 + 🧠 설정됨 + ▶
  const a = byId[A], a1 = a.children[0].children[0];
  assert.equal(cls(a1.children[0]), "sdot pulse"); assert.equal(a1.children[0].style["--c"], "#2f81f7");
  assert.equal(a1.children[1].textContent, "알파");
  assert.equal(a1.children[2].textContent, "작업 중");
  same(chipsOf(a), ["🔗 w1 · 작업 중", "#팀", "🧠 Opus 5"]);
  assert.ok(a1.children.find((k) => cls(k) === "schip model set"), "설정된 모델은 set 클래스");
  assert.equal(a.children.at(-2).textContent, "▶"); assert.equal(a.children.at(-1).textContent, "✎");
  assert.equal(a.children[0].children[1].children.map((k) => k.textContent).join("|"), "3 msgs|2.0KB|1분 전");
  assert.equal(a.children[0].children.length, 2, "cwd 가 대표와 같으면 경로 줄 없음");
  // B: 권한 대기 → ✋ 버튼 + 상태 문구 클릭 가능, 우리 PTY(live_terminal) → 🔗 없음, 🖥, 이름은 slug, 경로 줄 있음
  const b = byId[B], b1 = b.children[0].children[0];
  assert.equal(b1.children[2].textContent, "권한 대기"); assert.equal(b1.children[2].style.cursor, "pointer");
  same(chipsOf(b), [], "우리 PTY 가 떠 있으면 피어 칩을 달지 않는다");
  assert.equal(b1.children[1].textContent, "beta-slug");
  same(b.children.map((k) => k.textContent), ["", "✋", "🖥", "✎"]);
  assert.equal(b.children[0].children[2].textContent, "F:/p1/sub");
  assert.ok(b.children[0].children[1].children.map((k) => k.textContent).includes("5시간 전"));
  // C: 🤖 배경 에이전트 + ⚙(피커 미표시) + dim
  const c = byId[C];
  same(chipsOf(c), ["🤖 bg-1", "⚙ 에이전트"]);
  assert.ok(c.classList.contains("dim"));
  // D: picker_expose → ⚙/dim 없음, 선택 행 sel
  const d = byId[D];
  same(chipsOf(d), []); assert.ok(!d.classList.contains("dim")); assert.ok(d.classList.contains("sel"));
  // 접기: 그룹 헤더 클릭 → 그 그룹 행 사라짐, 검색 중엔 항상 펼침
  heads[0].onclick();
  assert.equal(rows(wrap).length, 2);
  assert.equal(ev(ctx, "COLLAPSED.has('F--p2')"), true);
  ev(ctx, "renderRows(globalThis.__wrap, '감마')");
  same(rows(wrap).map((x) => x.dataset.sid), [C], "검색은 접힘을 무시하고 제목으로 거른다");
  ev(ctx, "renderRows(globalThis.__wrap, 'p1/sub')");
  same(rows(wrap).map((x) => x.dataset.sid), [B], "경로로도 검색");
  // 라벨 필터 / 빈 결과 문구
  ev(ctx, "COLLAPSED.clear(); ACTIVE_LABEL = '팀'; renderRows(globalThis.__wrap, '')");
  same(rows(wrap).map((x) => x.dataset.sid), [A]);
  ev(ctx, "renderRows(globalThis.__wrap, '없는것')");
  assert.equal(wrap.children[0].textContent, "해당 세션 없음");
  ev(ctx, "ACTIVE_LABEL = ''; SESSIONS = []; renderRows(globalThis.__wrap, '')");
  assert.equal(wrap.children[0].textContent, "세션 없음");
});

// ---------------------------------------------------------------- 서비스워커 알림 계약
function bootSw(pathname) {
  const listeners = {};
  const calls = { shown: [], focused: [], posted: [], opened: [] };
  const self = {
    addEventListener: (t, f) => { listeners[t] = f; },
    skipWaiting() {}, location: { pathname, origin: "https://test.local" },
    registration: { showNotification: async (title, o) => calls.shown.push({ title, ...o }) },
    clients: { claim: async () => {}, openWindow: async (u) => calls.opened.push(u), matchAll: async () => self.__wins },
    __wins: [],
  };
  vm.runInContext(readFileSync(SW, "utf-8"), vm.createContext({ self, console }), { filename: "sw.js" });
  const fire = async (type, ev) => { let p; listeners[type]({ ...ev, waitUntil: (x) => { p = x; } }); await p; };
  return { self, calls, fire };
}
const win = (url, calls, { failFocus = false } = {}) => ({ url, focus: async () => { if (failFocus) throw new Error("gone"); calls.focused.push(url); }, postMessage: (m) => calls.posted.push(m) });

test("sw push: 페이로드 → 알림(title 기본값·tag·data{sid,kind,gid}), 깨진 JSON 도 알림은 뜬다", async () => {
  const { calls, fire } = bootSw("/relay/sw.js");
  await fire("push", { data: { json: () => ({ title: "T", body: "b", tag: "x:ready", sid: A, kind: "ready" }) } });
  const n = calls.shown[0];
  assert.equal(n.title, "T"); assert.equal(n.tag, "x:ready"); same(n.data, { sid: A, kind: "ready", gid: "" });
  await fire("push", { data: { json: () => { throw new Error("bad"); } } });
  assert.equal(calls.shown[1].title, "ClewPath");
  await fire("push", { data: null });
  assert.equal(calls.shown[2].title, "ClewPath"); assert.equal(calls.shown[2].tag, undefined);
});

test("sw notificationclick: 열린 창이 있으면 focus + postMessage(open-session/open-monitor), 없거나 focus 실패면 openWindow 딥링크", async () => {
  const { self, calls, fire } = bootSw("/relay/sw.js");
  const close = () => {};
  // 창 있음(같은 origin) → 세션 알림은 open-session, 관제 알림은 open-monitor. 다른 origin 창은 건너뜀
  self.__wins = [win("https://other.example/relay/app", calls), win("https://test.local/relay/app", calls)];
  await fire("notificationclick", { notification: { close, data: { sid: A } } });
  await fire("notificationclick", { notification: { close, data: { sid: A, gid: "g1" } } });
  same(calls.posted, [{ type: "open-session", sid: A }, { type: "open-monitor", gid: "g1" }]);
  same(calls.focused, ["https://test.local/relay/app", "https://test.local/relay/app"]);
  assert.equal(calls.opened.length, 0);
  // focus 실패(죽은 창) → 다음 창, 전부 실패 → openWindow
  self.__wins = [win("https://test.local/relay/app", calls, { failFocus: true })];
  await fire("notificationclick", { notification: { close, data: { sid: A } } });
  same(calls.opened, ["/relay/app#open=" + A]);
  // 창 없음: 관제 → #monitor=, data 없음 → 프래그먼트 없음
  self.__wins = [];
  await fire("notificationclick", { notification: { close, data: { gid: "g 1" } } });
  await fire("notificationclick", { notification: { close } });
  same(calls.opened.slice(1), ["/relay/app#monitor=g%201", "/relay/app"]);
  // 로컬 배치(/sw.js) → 루트
  const loc = bootSw("/sw.js");
  await loc.fire("notificationclick", { notification: { close, data: { sid: B } } });
  same(loc.calls.opened, ["/#open=" + B]);
});

test("페이지 쪽 계약: serviceWorker message open-session/open-monitor → openSessionById/openMonitorGroupById, 그 외 무시", async () => {
  const handlers = {};
  const ctx = await load({ navigator: { serviceWorker: { addEventListener: (t, f) => { handlers[t] = f; }, getRegistration: async () => null } } });
  assert.equal(typeof handlers.message, "function", "부팅 시 message 리스너 등록");
  ev(ctx, "globalThis.__calls = []; openSessionById = async (sid) => globalThis.__calls.push(['s', sid]); openMonitorGroupById = async (gid) => globalThis.__calls.push(['m', gid])");
  handlers.message({ data: { type: "open-session", sid: A } });
  handlers.message({ data: { type: "open-monitor", gid: "g1" } });
  handlers.message({ data: { type: "other" } });
  handlers.message({ data: null });
  same(ev(ctx, "globalThis.__calls"), [["s", A], ["m", "g1"]]);
});
