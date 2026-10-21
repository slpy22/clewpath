// 바로가기 화면(quick-switch 5단계) 검토 지적 — 0.31.0 검토 1회차(T95~). docs/designs/quick-switch.md §3-5~3-7·§4·§5·§6-2.
// 두 관점(화면·기존 흐름 / 단축키·터미널·폰)에서 재현된 것만 고친다. 재현은 고치기 전에 이 파일로 옮겨 실패를 먼저 확인했다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const X = "xxxxxxxx-1111-2222-3333-444444444444";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const RENDER = 350;   // 터미널 attach 의 resize 타이머(80·300ms)·renderTabBarSoon(300ms)이 지나가게
const QS = 150;       // 바로가기 단추 다시 그리기(100ms 로 모음)가 지나가게
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const RK = "e:/p";
const TWO_PCS = "pcsUpsert({ room: 'roomA', cs: 'c', cp: 'p', dev: 'dA', name: 'A' }); pcsUpsert({ room: 'roomB', cs: 'c', cp: 'p', dev: 'dB', name: 'B' });";
const team = (code, name, gid, mgr = "m1") => ({ code, name, kind: "team", monitor_group_id: gid, members: [{ member_role: "manager", session_id: mgr, alias: "관리" }] });

// 이 PC 의 기억(새로고침 전에 남긴 것) — qs-ui.test.mjs 와 같은 모양. null 이면 그 기억 없음
const DEF = { tabs: [A], files: ["a.py"], viewer: { sid: B, kind: "viewer", name: "B" }, monitor: { team: "T1", gid: "g1", name: "팀1" } };
const NONE = { tabs: null, files: null, viewer: null, monitor: null };
function seed(sb, pc, mem) {
  const m = { ...DEF, ...mem };
  if (m.tabs) sb.localStorage.setItem("sm_tabs_" + pc, JSON.stringify({ active: m.tabs[0], list: m.tabs.map((k) => ({ key: k, sid: k })), mru: m.tabs }));
  if (m.files) sb.localStorage.setItem("sm_fb_state2_" + pc, JSON.stringify({ open: [], sort: "name",
    tabs: m.files.map((f) => ({ root: RK, path: f, pinned: true })), active: RK + "|" + m.files[0] }));
  const pane = {};
  if (m.viewer) pane.viewer = { ...m.viewer, at: Date.now() };
  if (m.monitor) pane.monitor = { ...m.monitor, at: Date.now() };
  sb.localStorage.setItem("sm_pane_" + pc, JSON.stringify(pane));
}

// 머리 줄 단추 칸(#qs)·제목 단추(#qs-title)는 부트 전에 고정한다(제목 누름은 부트 때 건다). 나머지는 qs-ui 와 같다(하네스 주의 §8).
// room: 릴레이 PC 방 이름(그 PC 키에 기억을 심고 PC 둘을 저장해 둔다 — switchToPc 로 바꿔 본다). __teams 는 배열·'fail'·함수(받기를 늦출 때)
async function boot({ width = 1400, local = false, room = null, mem = null, more = null, ready = true, pre = "", setup = null } = {}) {
  const pc = local ? "local" : (room || "relay");
  const ctx = await load({ ...(local ? { location: LOCAL } : {}), setup: (sb) => {
    sb.document._register("qs", sb.document.createElement("nav")); sb.document._register("qs-title", sb.document.createElement("button"));
    if (room) sb.localStorage.setItem("sm_room", room);
    if (mem) seed(sb, pc, mem);
    if (more) for (const [p, m] of Object.entries(more)) seed(sb, p, m);
    if (setup) setup(sb); } });
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = []; globalThis.__calls = []; globalThis.__lists = 0;
    globalThis.__toasts = []; toast = (m) => __toasts.push(m); maybeShowIosHint = () => {}; refreshPushBtn = () => {};
    globalThis.__teams = ${JSON.stringify([team("T1", "팀1", "g1")])};
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb, gid) => { const h = { ids, mgr, gid: gid || null, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s })) }; };
    T.api = async (v, p, o) => { __calls.push(p);
      if (p === '/api/owner/teams') { const t = typeof __teams === 'function' ? await __teams() : __teams;
        if (t === 'fail') throw new Error('agent_offline'); return { teams: JSON.parse(JSON.stringify(t)) }; }
      if (p === '/api/owner/monitor/groups') return { groups: [] };
      if (p.endsWith('/file')) return { path: o.query.path, name: o.query.path, size: 5, mtime: 1, kind: 'text', text: 'x' };
      return {}; };
    fetchRecentMessages = async () => [];
    loadMonaco = async () => ({ editor: { createModel: (t, l) => ({ t, l, dispose() {} }),
      create: (h, o) => ({ model: o.model, setModel(m) { this.model = m; }, dispose() {}, saveViewState() { return { top: 1 }; },
        restoreViewState() {}, updateOptions() {}, focus() {}, getAction() { return null; } }) } });
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    conn.connect = async () => ({});
    ${room ? TWO_PCS : ""}
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p', mtime: 2 },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q', mtime: 1 }];
    ${pre}`);
  if (local) ev(ctx, "RELAY_APP_URL = ''");
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  if (ready) { await ev(ctx, "loadList()"); await tick(QS); ev(ctx, "__toasts.length = 0; __calls.length = 0; __lists = 0;"); }   // 목록 끝의 다시 그리기까지
  return { ctx, pd, app, nav: ctx.document.getElementById("qs"), title: ctx.document.getElementById("qs-title") };
}

const at = (ctx) => ev(ctx, `(() => { const k = PANE_NOW.kind, p = paneDetail().classList;
  if (k === 'terminal') return 'terminal:' + String(termView.active || '').slice(0, 1) + (p.contains('term-mode') ? '' : '?');
  if (k === 'files') return 'files:' + String(FB.active || '').split('|')[1] + (p.contains('fb-mode') ? '' : '?');
  if (k === 'viewer' || k === 'detail') return k + ':' + String(PANE_NOW.sid || '').slice(0, 1);
  if (k === 'monitor') return 'monitor:' + (MON_INLINE && MON_INLINE.gid) + (p.contains('mon-mode') ? '' : '?');
  return k; })()`);
const press = async (ctx, k) => { await ctx.quickGo(k); await tick(k === "terminal" ? RENDER : QS); };
async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
// PC 머리 줄 단추 하나 — [클래스, 툴팁]
const btn = (nav, i) => [String(nav.children[i].className), nav.children[i].title];
// 시트 한 줄 — [칸, 이름(라벨), 보조 글, 꼬리표, 흐림]
const rows = (ctx) => ev(ctx, "POP.box.children[1].children").map((b) => [b.dataset.k, b.children[1].children[0].textContent,
  (b.children[1].children[1] || {}).textContent || "", (b.children[2] || {}).textContent || "", b.classList.contains("dim")]);
// 빈 상세 칸 '최근 화면' — [칸:이름]
const recent = (pd) => { const box = pd.children[0], r = box && box.children.find((c) => String(c.className) === "qs-recent");
  return r ? r.children[1].children.map((b) => b.dataset.k + ":" + b.children[1].children[0].textContent) : []; };

// ---------------------------------------------------------------- PC 전환 뒤 🎬(검토 r1·r1b)
test("T95 PC 전환 뒤 🎬 단추·폰 시트·빈 칸 '최근 화면'은 새 PC 기억으로 — 이전 PC 에서 받아 둔 팀 목록(팀 코드가 겹쳐도)·늦게 온 이전 PC 팀 목록을 쓰지 않는다", async () => {
  const SECRET = [team("TA", "A 쪽 비밀 팀", "gA", A)];
  const toB = `T.list = async () => { __lists++; return { sessions: [{ session_id: '${X}', title: 'X', live_terminal: true, cwd: 'F:/x', mtime: 1 }] }; };`;
  const got = await each(["PC: B 는 팀·기억 없음", "폰: B 는 팀·기억 없음", "B 에 같은 팀 코드 기억", "받는 중이던 A 팀 목록(👥 보기)", "받는 중이던 A 팀 목록(20초 갱신)"], async (p) => {
    const phone = p.startsWith("폰");
    if (p.endsWith("팀·기억 없음")) {                                // A 에서 🎬(팀 1개라 바로 열림) → B(팀 0개, 관제를 연 적 없음)
      // sm_mon_last(PC 구분 없음)는 PC 상세 칸 관제를 열 때만 적힌다 — 폰은 PC 에서 한 번 연 적이 있는 것으로
      const { ctx, pd, nav, title } = await boot({ width: phone ? 390 : 1400, room: "roomA", mem: NONE, pre: `__teams = ${JSON.stringify(SECRET)};`,
        setup: (sb) => { if (phone) sb.localStorage.setItem("sm_mon_last", "gA"); } });
      await press(ctx, "monitor");
      const onA = [phone ? (ev(ctx, "MON_OV && MON_OV.gid") ? "덮개" : "?") : at(ctx), ctx.localStorage.getItem("sm_mon_last")];
      if (phone) ev(ctx, "MON_OV.close()");
      ev(ctx, `__teams = []; ${toB}`);
      await ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })"); await tick(QS * 2);
      const teams = ev(ctx, "TEAMS && TEAMS.map((t) => t.name).join(',')");
      const shown = phone ? (title.onclick(), rows(ctx)[3]) : { btn: btn(nav, 3), recent: recent(pd) };
      if (phone) ev(ctx, "closePop()");
      ev(ctx, "__toasts.length = 0"); await press(ctx, "monitor");
      return { onA, teams, shown, press: ev(ctx, "__toasts.join('|')") };
    }
    if (p === "B 에 같은 팀 코드 기억") {                           // 팀 코드는 이름의 영문·숫자로 만든다 — 한글 이름 팀은 PC 마다 TM 으로 겹친다
      const { ctx, nav } = await boot({ room: "roomA", mem: NONE, more: { roomB: { ...NONE, monitor: { team: "TM", gid: "gB", name: "B 개발팀" } } },
        setup: (sb) => sb.localStorage.setItem("sm_list_view", "groups"), pre: `__teams = ${JSON.stringify([team("TM", "A 영업팀", "gA", A)])};` });
      const onA = ev(ctx, "TEAMS && TEAMS.map((t) => t.name).join(',')");   // A 👥 팀 보기 — 받아 둔 팀 목록 = A 의 팀
      ev(ctx, `setListView('sessions'); __teams = ${JSON.stringify([team("TM", "B 개발팀", "gB", X)])}; ${toB}`);   // 세션 보기로 바꿔 B 로 — B 팀 목록은 아직 받지 않는다
      await ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })"); await tick(QS * 2);
      const shown = btn(nav, 3)[1];
      await press(ctx, "monitor");
      return { onA, shown, opened: ev(ctx, "__mon.filter((h) => h.gid).map((h) => h.gid).join(',')"), at: at(ctx) };
    }
    // 이전 PC 의 팀 목록을 받는 사이 PC 를 바꿨다 — 늦게 온 그 목록이 새 PC 의 받아 둔 팀 목록이 되지 않는다
    const tickPath = p.endsWith("(20초 갱신)");
    const { ctx, nav } = await boot({ room: "roomA", mem: NONE, more: { roomB: { ...NONE, monitor: { team: "TM", gid: "gB", name: "B 개발팀" } } },
      setup: (sb) => sb.localStorage.setItem("sm_list_view", "groups"), ready: !tickPath ? false : true,
      pre: `__teams = ${JSON.stringify([team("TM", "A 영업팀", "gA", A)])};` });
    ev(ctx, `__teams = () => new Promise((r) => { globalThis.__lateA = () => r(${JSON.stringify([team("TM", "A 영업팀", "gA", A)])}); });`);
    if (tickPath) { ctx.listTick(); await tick(); } else { ev(ctx, "loadList()"); await tick(); }
    if (!ev(ctx, "typeof __lateA === 'function'")) throw new Error("A 팀 목록 받기가 걸리지 않음");
    ev(ctx, `__teams = ${JSON.stringify([team("TM", "B 개발팀", "gB", X)])}; ${toB}`);
    await ev(ctx, "switchToPc({ room: 'roomB', name: 'B' })"); await tick(QS * 2);
    ev(ctx, "__lateA()"); await tick(QS);
    return { teams: ev(ctx, "TEAMS && TEAMS.map((t) => t.name).join(',')"), shown: btn(nav, 3)[1] };
  });
  const NO_MON = "팀 관제 — 최근에 연 관제 없음 · Alt+4";
  assert.deepEqual(got, {
    "PC: B 는 팀·기억 없음": { onA: ["monitor:gA", "gA"], teams: null, shown: { btn: ["dim", NO_MON], recent: [] }, press: "관제할 팀이 없습니다" },
    "폰: B 는 팀·기억 없음": { onA: ["덮개", "gA"], teams: null, shown: ["monitor", "팀 관제", "최근에 연 관제 없음", "고르기…", true], press: "관제할 팀이 없습니다" },
    "B 에 같은 팀 코드 기억": { onA: "A 영업팀", shown: "팀 관제 — B 개발팀 · Alt+4", opened: "gB", at: "monitor:gB" },
    "받는 중이던 A 팀 목록(👥 보기)": { teams: "B 개발팀", shown: "팀 관제 — B 개발팀 · Alt+4" },
    "받는 중이던 A 팀 목록(20초 갱신)": { teams: "B 개발팀", shown: "팀 관제 — B 개발팀 · Alt+4" },
  });
});
