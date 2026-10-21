// 바로가기 화면(quick-switch 5단계) 검토 지적 — 0.31.0 검토 1회차(T95~). docs/designs/quick-switch.md §3-5~3-7·§4·§5·§6-2.
// 두 관점(화면·기존 흐름 / 단축키·터미널·폰)에서 재현된 것만 고친다. 재현은 고치기 전에 이 파일로 옮겨 실패를 먼저 확인했다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { load, ev, stubEl, INDEX } from "./harness.mjs";

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
async function boot({ width = 1400, local = false, room = null, mem = null, more = null, ready = true, pre = "", setup = null, navigator = null } = {}) {
  const pc = local ? "local" : (room || "relay");
  const ctx = await load({ ...(local ? { location: LOCAL } : {}), ...(navigator ? { navigator } : {}), setup: (sb) => {
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
      if (p === '/api/owner/assistant') return { assistant: globalThis.__assistant || null };
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

// ---------------------------------------------------------------- 목록에 아직 없는 터미널(검토 r2)
// 🧑‍💼 비서는 claude -p 로 만든 세션이라 목록(20초)이 살아 있을 때만 싣는다 — 처음 연 비서는 직전 목록에 없고, openAssistant 는 목록 밖
// 세션 객체로 터미널을 연다. 그 터미널을 보는 중 🖥(Alt+1)를 누르면 '이미 그 화면'인데도 목록에 없다고 탭을 내리고 터미널을 허물었다.
test("T96 목록에 아직 없는 터미널(방금 연 🧑‍💼 비서): 보는 중 🖥 는 그대로(커서만), 단추는 그 터미널 이름 — 떠났다 돌아오면 목록을 한 번 새로 받아 판정한다(있으면 그 탭, 새 목록에도 없으면 조용히 내림)", async () => {
  const D = "d5d5d5d5-1111-2222-3333-444444444444";
  const listed = `{ session_id: '${D}', title: '🧑‍💼 비서', live_terminal: true, cwd: 'E:/assistant', mtime: 3 }`;
  const got = await each(["릴레이|보는 중", "로컬|보는 중", "릴레이|떠났다 돌아옴", "로컬|떠났다 돌아옴", "릴레이|떠났다 돌아옴(새 목록에도 없음)"], async (p) => {
    const [mode, what] = p.split("|");
    const { ctx, pd, nav } = await boot({ local: mode === "로컬", mem: { tabs: null }, pre: `globalThis.__assistant = { session_id: '${D}' };` });
    await ev(ctx, "openAssistant()"); await tick(RENDER);
    if (ev(ctx, "termView.active") !== D) throw new Error("비서 터미널이 뜨지 않음");
    const r = {};
    if (what === "보는 중") {
      r.btn = btn(nav, 0);
      const gen = ev(ctx, "PANE_NOW.gen"), first = pd.children[0];
      await press(ctx, "terminal");
      r.same = ev(ctx, "PANE_NOW.gen") === gen && pd.children[0] === first;
    } else {
      await press(ctx, "files");                                        // 떠남 — 터미널은 정리(연결 닫음)
      r.btn = btn(nav, 0);
      ev(ctx, `T.list = async () => { __lists++; return { sessions: [...SESSIONS.map((s) => ({ ...s })), ${p.endsWith("(새 목록에도 없음)") ? "" : listed}] }; };`);
      ev(ctx, "__toasts.length = 0; __lists = 0;");
      await press(ctx, "terminal");
      r.lists = ev(ctx, "__lists");
    }
    Object.assign(r, { at: at(ctx), tabs: ev(ctx, "TABS.list.map((t) => sessLabel(t.s)).join(',')"), toast: ev(ctx, "__toasts.join('|')"),
                       closedLive: ev(ctx, "__tc.filter((h) => !h.closed).length") });
    return r;
  });
  const TIP = "터미널 — 🧑‍💼 비서 · Alt+1";
  assert.deepEqual(got, {
    "릴레이|보는 중": { btn: ["on", TIP], same: true, at: "terminal:d", tabs: "🧑‍💼 비서", toast: "", closedLive: 1 },
    "로컬|보는 중": { btn: ["on", TIP], same: true, at: "terminal:d", tabs: "🧑‍💼 비서", toast: "", closedLive: 0 },   // 로컬은 터미널 창(iframe)
    "릴레이|떠났다 돌아옴": { btn: ["", TIP], lists: 1, at: "terminal:d", tabs: "🧑‍💼 비서", toast: "", closedLive: 1 },
    "로컬|떠났다 돌아옴": { btn: ["", TIP], lists: 1, at: "terminal:d", tabs: "🧑‍💼 비서", toast: "", closedLive: 0 },
    "릴레이|떠났다 돌아옴(새 목록에도 없음)": { btn: ["", TIP], lists: 1, at: "files:a.py", tabs: "",
      toast: "열어 둔 터미널이 없습니다 — 세션 줄의 🖥/▶ 로 여세요", closedLive: 0 },
  });
});

// ---------------------------------------------------------------- 로컬 터미널 창 커서(검토 s3b)
// 크롬은 iframe 요소에 focus 를 주면 창 문서의 커서를 body 로 옮긴다 — 창 안 xterm 이 커서를 잃어 다음 타자가 claude 로 가지 않았다
// (헤드리스 크롬 실측: Alt+1·파란 🖥 단추·검색칸에서 🖥 세 경우 모두). 창 안 xterm 입력칸에 바로 커서를 둔다.
test("T97 로컬 터미널 창(iframe)이 이미 떠 있을 때 🖥·Alt+1 은 창 안 xterm 입력칸에 커서를 둔다 — 창(iframe 요소)에 focus 를 주지 않는다. 창 문서를 못 읽으면(아직 안 뜸) 지금처럼 창에", async () => {
  const got = await each(["🖥 단추", "Alt+1(창 안에서)", "창 문서 없음"], async (p) => {
    const { ctx } = await boot({ local: true, mem: {} });
    await press(ctx, "terminal");
    const fr = ev(ctx, "termView.cur().frame"), log = [];
    const ta = Object.assign(stubEl("textarea"), { focus() { log.push("xterm"); }, closest: (s) => (s === ".xterm" ? stubEl("div") : null) });
    fr.contentDocument = p === "창 문서 없음" ? null : { querySelector: (s) => (s === ".xterm-helper-textarea" ? ta : null) };
    fr.focus = () => log.push("창 요소");
    fr.contentWindow = { focus() { log.push("창"); }, addEventListener() {} };
    const gen = ev(ctx, "PANE_NOW.gen");
    if (p.startsWith("Alt+1")) {
      const e = { type: "keydown", altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, isComposing: false, repeat: false, code: "Digit1", key: "1",
                  target: ta, preventDefault() {}, stopPropagation() {} };
      ctx.onQuickKey(e); await tick(RENDER);                          // 창에 붙인 감시(quickKeyWatch)가 부르는 것과 같은 함수
    } else await press(ctx, "terminal");
    return { focus: log.join(","), same: ev(ctx, "PANE_NOW.gen") === gen, at: at(ctx) };
  });
  assert.deepEqual(got, {
    "🖥 단추": { focus: "xterm", same: true, at: "terminal:a" },
    "Alt+1(창 안에서)": { focus: "xterm", same: true, at: "terminal:a" },
    "창 문서 없음": { focus: "창 요소,창", same: true, at: "terminal:a" },
  });
});

// ---------------------------------------------------------------- 기억을 지운 뒤 다시 그리기(검토 r5)
test("T98 🎬·💬 를 눌러 그 기억을 지우면(팀 삭제·세션이 목록에 없음) 단추·빈 칸 '최근 화면'도 곧 다시 그린다 — 지운 이름이 남아 같은 단추가 다른 화면을 열지 않는다", async () => {
  const got = await each(["🎬 팀 삭제", "💬 목록에 없는 세션"], async (p) => {
    const mon = p.startsWith("🎬");
    const { ctx, pd, nav } = await boot({ mem: mon ? { ...NONE, monitor: { team: "OLD", gid: "gOld", name: "지운 팀" } }
                                                   : { ...NONE, viewer: { sid: X, kind: "viewer", name: "지운 세션" } },
      pre: `__teams = ${JSON.stringify([team("NEW", "남은 팀", "gNew")])};` });
    const i = mon ? 3 : 2;
    const before = { btn: btn(nav, i), recent: recent(pd) };
    await press(ctx, mon ? "monitor" : "viewer");
    return { before, toast: ev(ctx, "__toasts.join('|')"), after: { btn: btn(nav, i), recent: recent(pd) } };
  });
  assert.deepEqual(got, {
    "🎬 팀 삭제": { before: { btn: ["", "팀 관제 — 지운 팀 · Alt+4"], recent: ["monitor:지운 팀"] }, toast: "그 팀 관제를 열 수 없습니다",
                   after: { btn: ["dim", "팀 관제 — 최근에 연 관제 없음 · Alt+4"], recent: [] } },
    "💬 목록에 없는 세션": { before: { btn: ["dim", "뷰어 — 지운 세션 · 목록에 없음 · 방금 · Alt+3"], recent: [] }, toast: "그 세션은 목록에 없습니다",
                   after: { btn: ["dim", "뷰어 — 최근에 본 대화 없음 · Alt+3"], recent: [] } },
  });
});

// ---------------------------------------------------------------- 첫 목록 전 폰 관제 덮개(검토 r4)
// 덮개(z 60)가 알림(z 20)을 가린다. 관제 알림이 연결 직후·PC 전환 직후 첫 목록보다 먼저 덮개를 열면(드묾) 그 ▾ 에서 다른 화면을 골라도
// '목록을 받는 중입니다'가 덮개 밑에 떠 무반응처럼 보였다(§5-3 '토스트는 덮개를 닫은 뒤').
test("T99 첫 목록 전 폰 관제 덮개 ▾ 에서 다른 화면을 고르면 덮개(와 관제 연결)를 닫고 '목록을 받는 중입니다'를 알린다 — 살아 있는 덮개에서 🎬 는 그대로(알림 없음), 끊긴 덮개면 닫고 알린다", async () => {
  const ROW = { "🖥": 0, "📄": 1, "💬": 2, "🎬": 3 };
  const got = await each(["🖥", "📄", "💬", "🎬", "🎬 끊긴 덮개"], async (p) => {
    const { ctx } = await boot({ width: 390, ready: false, pre: "T.list = () => new Promise(() => {});" });
    ev(ctx, "globalThis.__log = []; toast = (m) => __log.push(m + ' / 덮개=' + (MON_OV ? '열림' : '닫힘'));");
    ev(ctx, "loadList()");                                              // 첫 목록을 받는 중
    ev(ctx, `openMonitorGroup({ id: 'g1', name: '팀1', manager: '${A}', subs: [], labels: {} })`);   // 관제 알림이 먼저 연 덮개
    const mon = ev(ctx, "__mon.filter((h) => h.gid).at(-1)");
    if (p.includes("끊긴")) mon.cb(null, true);
    const ov = ctx.document.body.children.filter((c) => String(c.className) === "mon-ov").at(-1);
    ov.children[0].children.find((c) => String(c.className).includes("qs-ov")).onclick();   // 덮개 머리 ▾ → 시트
    ev(ctx, `POP.box.children[1].children[${ROW[p.split(" ")[0]]}].onclick()`); await tick(QS);
    return { log: ev(ctx, "__log.join('|')"), overlay: ev(ctx, "!!MON_OV"), monClosed: mon.closed };
  });
  const SAID = "목록을 받는 중입니다 / 덮개=닫힘";
  assert.deepEqual(got, {
    "🖥": { log: SAID, overlay: false, monClosed: 1 }, "📄": { log: SAID, overlay: false, monClosed: 1 }, "💬": { log: SAID, overlay: false, monClosed: 1 },
    "🎬": { log: "", overlay: true, monClosed: 0 },                     // 이미 그 화면
    "🎬 끊긴 덮개": { log: SAID, overlay: false, monClosed: 1 },
  });
});

// ---------------------------------------------------------------- 빈 칸 '최근 화면' 키보드 커서(검토 s7)
// 💬 칸 보조 글의 '본 시각'(방금 → 1분 전 → …)이 매분 바뀌어 20초 갱신이 단추를 통째로 새로 만들었다 — Tab 으로 둔 커서가 body 로 빠지고
// Enter 가 먹지 않았다(헤드리스 크롬 실측). '모습이 같으면 다시 그리지 않는다(포커스 유지)'의 뜻대로 글자만 고친다.
test("T100 빈 상세 칸 '최근 화면': 글자만 바뀌면(방금 → 1분 전) 단추를 새로 만들지 않고 글자만 고친다(키보드 커서 유지) — 차례가 바뀌어 새로 그릴 때는 커서가 있던 칸의 새 단추로 커서를 옮긴다", async () => {
  const pane = (m) => JSON.stringify(m);
  const { ctx, pd } = await boot({ mem: NONE,
    setup: (sb) => sb.localStorage.setItem("sm_pane_relay", pane({ viewer: { sid: B, kind: "viewer", name: "B", at: Date.now() - 50000 } })) });
  const buttons = () => pd.children[0].children[1].children[1].children;
  const label = (b) => b.dataset.k + ":" + b.children[1].children[0].textContent + " · " + b.children[1].children[1].textContent;
  const before = buttons().map(label), b0 = buttons()[0];
  ctx.document.activeElement = b0;                                      // Tab 으로 💬 단추에 커서
  ctx.localStorage.setItem("sm_pane_relay", pane({ viewer: { sid: B, kind: "viewer", name: "B", at: Date.now() - 61000 } }));   // 1분이 지났다
  await ctx.__intervals.find((x) => x.ms === 20000).f(); await tick(QS);   // 20초 갱신 한 번
  const minute = { same: buttons()[0] === b0, texts: buttons().map(label), tip: buttons()[0].title };
  // 관제 기억이 새로 생기고 그것이 마지막 화면(맨 앞) — 차례가 바뀌어 새로 그린다. 커서는 💬 칸의 새 단추로
  const mk = ctx.document.createElement;
  ctx.document.createElement = (t) => { const e = mk(t); if (t === "button") e.focus = () => { ctx.__focused = e; }; return e; };
  ctx.localStorage.setItem("sm_qs_last_relay", "monitor");
  ev(ctx, "paneMemSet('monitor', { team: 'T1', gid: 'g1', name: '팀1', at: Date.now() })"); await tick(QS);
  const rebuilt = { texts: buttons().map((b) => b.dataset.k), focused: ctx.__focused ? ctx.__focused.dataset.k : null,
                    fresh: ctx.__focused === buttons()[1] && buttons()[1] !== b0 };
  assert.deepEqual({ before, minute, rebuilt }, {
    before: ["viewer:B · 뷰어 · 방금"],
    minute: { same: true, texts: ["viewer:B · 뷰어 · 1분 전"], tip: "뷰어 — B · 1분 전 · Alt+3" },
    rebuilt: { texts: ["monitor", "viewer"], focused: "viewer", fresh: true },
  });
});

// ---------------------------------------------------------------- 맥 Option+숫자(검토 s8)
// 맥(아이패드 포함)의 Option 은 글자를 만드는 수식키다 — Option+1~4 = US ¡™£¢, UK 배열은 Option+3 이 '#'(그 배열에서 # 를 치는 유일한 길).
// 바로가기 판정은 물리 키(code)만 봐서 터미널·편집기 안에서 그 글자를 삼켰다(릴레이 xterm 도 claude 로 보내지 않음). 윈도우의 Alt+숫자는
// 글자를 만들지 않는다(AltGr 은 Ctrl 이 섞여 이미 뺀다).
test("T101 맥 Option+숫자가 글자를 만들면(¡™£¢·UK '#'·죽은 키) 글을 받는 곳(터미널·편집기 찾기 칸)에서는 입력으로 둔다(막지 않고 릴레이 xterm 도 claude 로 보낸다) — 읽기 전용 편집기 본문·본문에서는 바로가기, 윈도우는 지금처럼 터미널 안에서도 바로가기", async () => {
  const xt = () => Object.assign(stubEl("textarea"), { readOnly: false, closest: (s) => (s === ".xterm" ? stubEl("div") : null) });
  // 파일 편집기(Monaco)는 읽기 전용(domReadOnly — 숨은 입력칸이 readonly). 찾기 칸(Ctrl+F)은 글을 받는다
  const mo = (readOnly) => Object.assign(stubEl("textarea"), { readOnly, closest: (s) => (s === ".monaco-editor" ? stubEl("div") : null) });
  const key = (code, key, target) => ({ type: "keydown", altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, isComposing: false, repeat: false,
    code, key, target, prevented: 0, preventDefault() { this.prevented++; }, stopPropagation() {} });
  const run = async (platform, cases) => {
    const { ctx } = await boot({ mem: {}, navigator: { platform } });
    ev(ctx, "globalThis.__ckh = []; { const X = Terminal; Terminal = class { constructor(o) { const t = new X(o); t.attachCustomKeyEventHandler = (f) => __ckh.push(f); return t; } }; }");
    await press(ctx, "terminal");                                       // 릴레이 xterm — 이 조합을 claude 로 보낼지 정하는 처리기
    const h = ev(ctx, "__ckh[0]");
    ev(ctx, "globalThis.__qg = []; quickGo = (k) => { __qg.push(k); }");
    const out = {};
    for (const [name, [code, k, target]] of Object.entries(cases)) {
      const e = key(code, k, target);
      ev(ctx, "__qg.length = 0"); ctx.onQuickKey(e);
      out[name] = { go: ev(ctx, "__qg.join(',')"), prevented: e.prevented, xtermSends: target.closest(".xterm") ? h(e) : null };
    }
    return out;
  };
  const got = {
    mac: await run("MacIntel", {
      "터미널 Option+3 = '#'(UK)": ["Digit3", "#", xt()], "터미널 Option+1 = '¡'(US)": ["Digit1", "¡", xt()],
      "터미널 Option+죽은 키": ["Digit4", "Dead", xt()], "편집기 찾기 칸 Option+3 = '#'": ["Digit3", "#", mo(false)],
      "편집기 본문(읽기 전용) Option+2 = '™'": ["Digit2", "™", mo(true)],
      "본문 Option+3 = '£'": ["Digit3", "£", stubEl("body")], "터미널 Option+3 = '3'(숫자 그대로)": ["Digit3", "3", xt()],
    }),
    win: await run("Win32", { "터미널 Alt+3(AZERTY '\"')": ["Digit3", "\"", xt()], "터미널 Alt+1": ["Digit1", "1", xt()] }),
  };
  const typed = { go: "", prevented: 0, xtermSends: true };
  assert.deepEqual(got, {
    mac: { "터미널 Option+3 = '#'(UK)": typed, "터미널 Option+1 = '¡'(US)": typed, "터미널 Option+죽은 키": typed,
           "편집기 찾기 칸 Option+3 = '#'": { go: "", prevented: 0, xtermSends: null },
           "편집기 본문(읽기 전용) Option+2 = '™'": { go: "files", prevented: 1, xtermSends: null },   // 글자가 갈 데가 없다
           "본문 Option+3 = '£'": { go: "viewer", prevented: 1, xtermSends: null },   // 입력칸이 아닌 곳 — 글자는 갈 데가 없다
           "터미널 Option+3 = '3'(숫자 그대로)": { go: "viewer", prevented: 1, xtermSends: false } },
    win: { "터미널 Alt+3(AZERTY '\"')": { go: "viewer", prevented: 1, xtermSends: false }, "터미널 Alt+1": { go: "terminal", prevented: 1, xtermSends: false } },
  });
});

// ---------------------------------------------------------------- 접근성: PC 제목·aria-expanded(검토 s6)
// PC 에서 제목(#qs-title)은 CSS(pointer-events)와 탭 키만 막았을 뿐 <button aria-haspopup="menu"> 그대로라 화면 읽기가 '세션, 메뉴 단추'로
// 읽었는데 눌러도(보조기기 누름 = click) 아무 일이 없었다. 폰에서는 시트를 열고 닫아도 aria-expanded 가 한 번도 바뀌지 않았다.
test("T102 접근성: 제목(#qs-title)은 PC 에선 글자 — 막힌 단추(커서 없음)·role=none·메뉴 표시 없음, 폰에선 '화면 바로가기' 메뉴 단추. 창 폭이 바뀌면 따라 바뀌고, 시트를 열고 닫으면 aria-expanded 를 맞춘다(관제 덮개 ▾ 도)", async () => {
  // 하네스 스텁의 removeAttribute 는 아무것도 안 한다 — 제목 단추만 실제처럼 지우게
  const fix = (b) => { b.removeAttribute = (k) => { b.setAttribute(k, null); }; return b; };
  const resize = (ctx, w) => { ev(ctx, `innerWidth = ${w}`); for (const f of ctx.__winListeners.resize || []) f(); };
  const { ctx, title } = await boot({ width: 1400, mem: {}, setup: (sb) => sb.document._register("qs-title", fix(sb.document.createElement("button"))) });
  const attrs = () => ({ disabled: title.disabled, tab: title.tabIndex, role: title.getAttribute("role"), popup: title.getAttribute("aria-haspopup"),
                         tip: title.getAttribute("title") || "", expanded: title.getAttribute("aria-expanded") });
  const got = { pc: attrs() };
  resize(ctx, 390); got.phone = attrs();
  title.onclick(); got.open = title.getAttribute("aria-expanded");
  ev(ctx, "closePop()"); got.closed = title.getAttribute("aria-expanded");
  resize(ctx, 1400); got.pcAgain = attrs();
  { const { ctx: c2 } = await boot({ width: 390, mem: {} });               // 폰 관제 덮개 머리 ▾
    await press(c2, "monitor");
    const qb = c2.document.body.children.filter((c) => String(c.className) === "mon-ov").at(-1).children[0].children.find((c) => String(c.className).includes("qs-ov"));
    const before = qb.getAttribute("aria-expanded"); qb.onclick(); const open = qb.getAttribute("aria-expanded");
    ev(c2, "closePop()"); got.ov = [before, open, qb.getAttribute("aria-expanded")]; }
  const PC = { disabled: true, tab: -1, role: "none", popup: null, tip: "", expanded: null };
  assert.deepEqual(got, {
    pc: PC,
    phone: { disabled: false, tab: 0, role: null, popup: "menu", tip: "화면 바로가기", expanded: "false" },
    open: "true", closed: "false",
    pcAgain: PC,
    ov: [null, "true", "false"],
  });
});

// ---------------------------------------------------------------- 폰 머리 줄 폭(검토 hdr-cmp)
// 제목 ▾(약 11px)와 사이 4px 만큼 제목·PC 고르기 칸이 함께 줄어 0.30.0 에서 다 보이던 짧은 제목이 '세…'로 잘렸다(360·390·412px, 릴레이 🖧 +
// PC 고르기). ▾ 를 작게(.7em, 사이 2px) 하고 폰 머리 줄 사이 간격을 10→8px 로 줄여 그 폭을 돌려준다. 배치는 헤드리스 크롬으로 0.30.0 과 견줘
// 쟀다(320~430px × 릴레이·로컬 × 제목 5 × PC 이름 2 = 120가지: 0.30.0 에서 다 보이던 제목이 잘리는 경우 7 → 0, 넘침 0, ▾ 늘 보임 —
// qs31-fix/hdr-measure.cjs). 여기서는 그 규칙이 그대로 있는지만 지킨다(T87 처럼 CSS 문자열).
test("T103 폰 머리 줄: 제목 ▾ 가 짧은 제목을 자르지 않게 ▾ 는 작게(.7em·사이 2px), 폰 머리 줄 사이 간격은 8px — PC 머리 줄(10px)·사고 구역 규칙은 그대로", () => {
  const html = readFileSync(INDEX, "utf8").replace(/\r/g, "");
  const sec = html.slice(html.indexOf("/* ---- 화면 바로가기(quick-switch"), html.indexOf("/* ---- 화면 바로가기 끝 ---- */"));
  const flat = html.replace(/\s+/g, " ");
  assert.deepEqual({
    caret: sec.includes("#qs-title .qs-caret{flex:none;font-size:.7em;"),
    titleGap: sec.includes("#qs-title{gap:2px;"),
    phoneGap: sec.includes("@media (max-width:879px){ header{gap:8px} }"),
    pcHeader: flat.includes("header{display:flex;align-items:center;gap:10px;"),
    newRulesOnly: !/#main|#paneDetail|!important|side-off|term-mode/.test(sec),
  }, { caret: true, titleGap: true, phoneGap: true, pcHeader: true, newRulesOnly: true });
});
