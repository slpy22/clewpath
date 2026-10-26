// 상세 칸 화면 바로가기(quick-switch) 5단계 — 화면. docs/designs/quick-switch.md §3-4~3-7·§5·§8 5단계.
// PC 머리 줄 단추 4개(#qs) · 폰 제목 ▾ 시트(#qs-title) · 폰 관제 덮개 머리 ▾ · 단축키 Alt+1~4 · 빈 상세 칸 '최근 화면' · 뷰어 막대 세션 이름.
// 모습은 quickTargets(), 누르면 quickGo(kind)(4단계) — 여기서는 그리기·입구·강조 갱신 시점만 본다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { load, ev, stubEl, INDEX } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const C = "cccccccc-1111-2222-3333-444444444444";
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const RENDER = 350;   // 터미널 attach 의 resize 타이머(80·300ms)·renderTabBarSoon(300ms)이 지나가게
const QS = 150;       // 바로가기 단추 다시 그리기(100ms 로 모음)가 지나가게
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const RK = "e:/p";

const TEAM1 = { code: "T1", name: "팀1", kind: "team", monitor_group_id: "g1",
  members: [{ member_role: "manager", session_id: "m1", alias: "관리" }, { member_role: "worker", session_id: "w1", alias: "일꾼" }] };

// 이 PC 의 기억(새로고침 전에 남긴 것) — qs-go.test.mjs 와 같은 모양. null 이면 그 기억 없음
const DEF = { tabs: [A], files: ["a.py"], viewer: { sid: B, kind: "viewer", name: "B" }, monitor: { team: "T1", gid: "g1", name: "팀1" } };
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

// 머리 줄 단추 칸(#qs)·제목 단추(#qs-title)는 부트 전에 고정한다(제목 누름은 부트 때 건다). 나머지는 qs-go 와 같다(하네스 주의 §8)
// 머리 줄·시트 맨 앞의 🏠 홈(2026-10-26)은 기억이 없는 칸이라 네 칸 시험에서는 뺀다(홈 시험은 qs-ui.test.mjs T95)
const four = (nav) => new Proxy(nav, { get: (t, k) => (k === "children" ? t.children.filter((b) => b.dataset.k !== "home") : t[k]) });
async function boot({ width = 1400, local = false, mem = null, ready = true, pre = "", setup = null } = {}) {
  const pc = local ? "local" : "relay";
  const ctx = await load({ ...(local ? { location: LOCAL } : {}), setup: (sb) => {
    sb.document._register("qs", sb.document.createElement("nav")); sb.document._register("qs-title", sb.document.createElement("button"));
    if (mem) seed(sb, pc, mem); if (setup) setup(sb); } });
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = ${width}; globalThis.__tc = []; globalThis.__mon = []; globalThis.__res = []; globalThis.__calls = []; globalThis.__lists = 0;
    globalThis.__toasts = []; toast = (m) => __toasts.push(m);
    globalThis.__teams = ${JSON.stringify([TEAM1])};
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, closed: false, close() { h.closed = true; }, send() {} }; __tc.push(h); return h; };
    T.monitor = (ids, mgr, cb, gid) => { const h = { ids, mgr, gid: gid || null, cb, closed: 0, close() { h.closed++; }, add() {}, remove() {} }; __mon.push(h); return h; };
    T.resume = (id, cb) => { const h = { id, cb, closed: 0, input() {}, close() { h.closed++; } }; __res.push(h); return h; };
    T.list = async () => { __lists++; return { sessions: SESSIONS.map((s) => ({ ...s })) }; };
    T.api = async (v, p, o) => { __calls.push(p);
      if (p === '/api/owner/teams') return { teams: JSON.parse(JSON.stringify(__teams)) };
      if (p === '/api/owner/monitor/groups') return { groups: [] };
      if (p.endsWith('/file')) return { path: o.query.path, name: o.query.path, size: 5, mtime: 1, kind: 'text', text: 'x' };
      return {}; };
    fetchRecentMessages = async () => [];
    loadMonaco = async () => ({ editor: { createModel: (t, l) => ({ t, l, dispose() {} }),
      create: (h, o) => ({ model: o.model, setModel(m) { this.model = m; }, dispose() {}, saveViewState() { return { top: 1 }; },
        restoreViewState() {}, updateOptions() {}, focus() {}, getAction() { return null; } }) } });
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p', mtime: 2 },
                { session_id: '${B}', title: 'B', live_terminal: true, cwd: 'E:/q', mtime: 1 }];
    ${pre}`);
  if (local) ev(ctx, "RELAY_APP_URL = ''");
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  if (ready) { await ev(ctx, "loadList()"); await tick(QS); ev(ctx, "__toasts.length = 0; __calls.length = 0; __lists = 0;"); }   // 목록 끝의 다시 그리기까지
  return { ctx, pd, app, nav: four(ctx.document.getElementById("qs")), title: ctx.document.getElementById("qs-title") };
}

const at = (ctx) => ev(ctx, `(() => { const k = PANE_NOW.kind, p = paneDetail().classList;
  if (k === 'terminal') return 'terminal:' + String(termView.active || '').slice(0, 1) + (p.contains('term-mode') ? '' : '?');
  if (k === 'files') return 'files:' + String(FB.active || '').split('|')[1] + (p.contains('fb-mode') ? '' : '?');
  if (k === 'viewer' || k === 'detail') return k + ':' + String(PANE_NOW.sid || '').slice(0, 1);
  if (k === 'monitor') return 'monitor:' + (MON_INLINE && MON_INLINE.gid) + (p.contains('mon-mode') ? '' : '?');
  return k; })()`);
const press = async (ctx, k) => { await ctx.quickGo(k); await tick(k === "terminal" ? RENDER : QS); };
const backBtn = (pd) => pd.children.find((c) => String(c.className).startsWith("backbar")).children[0];   // '‹ 목록'
async function each(paths, fn) {
  const got = {};
  for (const p of paths) {
    try { got[p] = await fn(p); } catch (e) { got[p] = { error: String((e && e.message) || e) }; }
  }
  return got;
}
// PC 머리 줄 단추 4개의 모습 — [칸, 클래스, 그림, 글자]
const btns = (nav) => nav.children.map((b) => [b.dataset.k, String(b.className), b.children[0].textContent, b.children[1].textContent]);
const tips = (nav) => nav.children.map((b) => b.title);
// 시트 한 줄 — [칸, 이름(라벨), 보조 글, 꼬리표, 흐림]
const rows = (ctx) => ev(ctx, "POP.box.children[1].children").filter((b) => b.dataset.k !== "home").map((b) => [b.dataset.k, b.children[1].children[0].textContent,
  (b.children[1].children[1] || {}).textContent || "", (b.children[2] || {}).textContent || "", b.classList.contains("dim")]);

// ---------------------------------------------------------------- CSS·HTML 가드
test("T87 CSS·HTML 가드: 폰은 #qs 를 숨기고 PC(880px~)만 보이며 880~1099px 는 글자를 뺀다 · 자리는 .grow 뒤·🔔 앞 · 시트는 관제 덮개 위 층 · 칸 배치·상세 칸 모드·터미널 격자·◧ 규칙은 글자 하나 다르지 않다", () => {
  const html = readFileSync(INDEX, "utf8").replace(/\r/g, "");
  const flat = html.replace(/\s+/g, " ");
  const sec = html.slice(html.indexOf("/* ---- 화면 바로가기(quick-switch"), html.indexOf("/* ---- 화면 바로가기 끝 ---- */"));
  assert.ok(sec.length > 200, "바로가기 CSS 절을 찾지 못함");
  const base = sec.slice(0, sec.indexOf("@media"));
  const desk = sec.slice(sec.indexOf("@media (min-width:880px){"), sec.indexOf("@media (min-width:880px) and (max-width:1099px)"));
  const narrow = sec.slice(sec.indexOf("@media (min-width:880px) and (max-width:1099px)"));
  const z = (sel) => Number((html.match(new RegExp(sel.replace(/[.*]/g, "\\$&") + "\\{[^}]*?z-index:(\\d+)")) || [])[1]);
  const pos = (s) => html.indexOf(s);
  assert.deepEqual({
    phoneHidden: base.includes("#qs{display:none}"),
    deskShown: /#qs\{display:flex/.test(desk), titleInertOnPc: desk.includes("#qs-title{pointer-events:none"), caretHiddenOnPc: desk.includes("#qs-title .qs-caret{display:none}"),
    iconOnly: narrow.includes("#qs .ql{display:none}"),
    caretKept: base.includes("#qs-title .qs-caret{flex:none"),             // 제목이 길어도 ▾ 는 잘리지 않게
    newRulesOnly: !/#main|#paneDetail|!important|side-off|term-mode/.test(sec),
    sheetOverOverlay: z(".pop.qs-sheet") > z(".mon-ov") && z(".popbg.qs-sheet") > z(".mon-ov"),
    place: pos('<span class="grow"></span>') < pos('<nav id="qs" aria-label="화면 바로가기">') && pos('<nav id="qs"') < pos('id="btn-pcs"') && pos('id="btn-pcs"') < pos('id="btn-todo"'),
    title: html.includes('<button class="title" id="qs-title" type="button" aria-haspopup="menu" title="화면 바로가기"><span id="head">세션</span><span class="qs-caret" aria-hidden="true">▾</span></button>'),
  }, { phoneHidden: true, deskShown: true, titleInertOnPc: true, caretHiddenOnPc: true, iconOnly: true, caretKept: true, newRulesOnly: true,
       sheetOverOverlay: true, place: true, title: true });
  // 0.22.13·0.27.2 '칸 사라짐' 사고 구역 — 그대로
  for (const rule of [
    "#main{display:grid;grid-template-columns:var(--list-w,350px) 7px 1fr;grid-template-rows:minmax(0,1fr)}",
    "#app[data-view] #paneList, #app[data-view] #paneDetail:not(.term-mode):not(.fb-mode):not(.mon-mode){display:block!important}",
    "#app[data-view] #paneDetail.mon-mode{display:flex!important}",
    "#app[data-view] #paneDetail.fb-mode{display:flex!important}",
    "#app[data-view] #paneDetail.term-mode{display:grid!important}",
    "#paneDetail.term-mode{padding:0;overflow:hidden;display:grid;grid-template-rows:auto auto minmax(0,1fr);min-height:0}",
    "#app.side-off #main{grid-template-columns:minmax(0,1fr)}",
    "#app.side-off[data-view] #paneList,#app.side-off #resizer{display:none!important}",
    "#app[data-view=list] #paneDetail{display:none}",
    ".detail-empty{display:none}",
  ]) assert.ok(flat.includes(rule), "바뀜: " + rule);
});

// ---------------------------------------------------------------- PC 단추 상태
test("T88 PC 머리 줄 단추: 첫 목록 전엔 모두 흐림·준비 중, 목록 뒤엔 기억대로(보통·흐림) — 지금 상세 칸 화면은 파랑, 세션 상세면 💬 칸이 'ℹ 상세'. 웹 재개·장부·빈 안내엔 파랑 없음. 툴팁 = 무엇이 열릴지 · 단축키(👀 면 가져오기 확인)", async () => {
  const got = {};
  { const { ctx, nav, title } = await boot({ ready: false, mem: { viewer: { sid: B, kind: "detail", name: "B" } } });
    ev(ctx, "renderQuick()");
    got.before = btns(nav); got.beforeTip = tips(nav)[0];
    got.titleTab = title.tabIndex;                                               // PC 제목은 글자 그대로 — 탭 키도 건너뛴다
    await ev(ctx, "loadList()"); await tick(QS);
    got.after = btns(nav); got.afterTips = tips(nav);
    const first = nav.children[0];
    await press(ctx, "viewer"); got.detail = btns(nav).map((b) => b[1]);
    await press(ctx, "terminal"); got.terminal = btns(nav).map((b) => b[1]);
    got.sameButtons = nav.children[0] === first;                                // 단추는 한 번만 만든다(포커스 유지)
    await press(ctx, "files"); got.files = btns(nav).map((b) => b[1]);
    await press(ctx, "monitor"); got.monitor = btns(nav).map((b) => b[1]);
    await ev(ctx, `loadResume(SESSIONS[1], true, false)`); await tick(QS); got.resume = btns(nav).map((b) => b[1]);
    ev(ctx, "openLedger({ code: 'T1', name: '팀1' })"); await tick(QS); got.ledger = btns(nav).map((b) => b[1]);
    ev(ctx, "detailPlaceholder()"); await tick(QS); got.empty = btns(nav).map((b) => b[1]);
    nav.children[1].onclick(); await tick(QS); got.click = at(ctx);              // 단추 = quickGo(그 칸)
  }
  { const { ctx, nav } = await boot({ mem: { tabs: null, files: null, viewer: null, monitor: null } });
    ev(ctx, "renderQuick()"); got.none = btns(nav).map((b) => b[1]); got.noneTips = tips(nav); }
  { const { ctx, nav } = await boot({ mem: {}, pre: "SESSIONS[0].screen = { attached: true, screen_id: 'scr-other-device' };" });
    ev(ctx, "renderQuick()"); got.taken = [btns(nav)[0][1], tips(nav)[0]]; }
  assert.deepEqual(got, {
    before: [["terminal", "dim wait", "🖥", "터미널"], ["files", "dim wait", "📄", "파일"], ["viewer", "dim wait", "ℹ", "상세"], ["monitor", "dim wait", "🎬", "관제"]],
    beforeTip: "터미널 — 준비 중(목록을 받는 중) · Alt+1", titleTab: -1,
    after: [["terminal", "", "🖥", "터미널"], ["files", "", "📄", "파일"], ["viewer", "", "ℹ", "상세"], ["monitor", "", "🎬", "관제"]],
    afterTips: ["터미널 — A · Alt+1", "파일 — a.py · Alt+2", "상세 — B · 방금 · Alt+3", "팀 관제 — 팀1 · Alt+4"],
    detail: ["", "", "on", ""], terminal: ["on", "", "", ""], sameButtons: true, files: ["", "on", "", ""], monitor: ["", "", "", "on"],
    resume: ["", "", "", ""], ledger: ["", "", "", ""], empty: ["", "", "", ""],
    click: "files:a.py",
    none: ["dim", "dim", "dim", "dim"],
    noneTips: ["터미널 — 열어 둔 터미널 없음 · Alt+1", "파일 — 열어 둔 파일 없음 · Alt+2", "뷰어 — 최근에 본 대화 없음 · Alt+3", "팀 관제 — 최근에 연 관제 없음 · Alt+4"],
    taken: ["taken", "터미널 — A · Alt+1\n👀 다른 기기에서 보는 중 — 누르면 가져오기 확인"],
  });
});

// ---------------------------------------------------------------- 폰 제목 ▾ 시트
test("T89 폰 제목 ▾ → 아래 시트 4줄(터미널·파일·뷰어·팀 관제 고정 순서) — 지금 화면 '지금', '‹ 목록'으로 숨긴 화면 '바로 돌아가기'(다시 보여 주기만), 기억 없음은 흐리게 '고르기…', 첫 목록 전 '준비 중'. PC 에선 제목을 눌러도 시트가 없다", async () => {
  const got = {};
  { const { ctx, pd, app, title } = await boot({ width: 390, mem: {} });
    title.onclick();
    got.sheet = { open: ev(ctx, "!!POP"), up: ev(ctx, "POP.box.classList.contains('qs-sheet') && POP.bg.classList.contains('qs-sheet')"), head: ev(ctx, "POP.box.children[0].textContent"),
                  tab: title.tabIndex };                                       // 폰에서는 제목이 단추 — 탭 키로도 닿는다
    got.list = rows(ctx);
    ev(ctx, "POP.box.children[1].children[1].onclick()"); await tick(RENDER);   // 터미널 줄을 누름 → 시트 닫고 그 화면
    got.picked = { closed: ev(ctx, "POP === null"), at: at(ctx), view: app.getAttribute("data-view") };
    title.onclick(); got.now = rows(ctx)[0]; ev(ctx, "closePop()");
    backBtn(pd).onclick();                                                       // '‹ 목록' — 상세 칸을 숨기기만
    title.onclick(); got.back = rows(ctx)[0];
    const terms = ev(ctx, "__tc.length");
    ev(ctx, "POP.box.children[1].children[1].onclick()"); await tick(RENDER);
    got.returned = { view: app.getAttribute("data-view"), at: at(ctx), newTerm: ev(ctx, "__tc.length") - terms };
  }
  { const { ctx, title } = await boot({ width: 390, mem: { files: null, viewer: null } });
    title.onclick(); got.none = rows(ctx); }
  { const { ctx, title } = await boot({ width: 390, ready: false, mem: {} });
    title.onclick(); got.wait = rows(ctx).map((r) => [r[0], r[3], r[4]]); }
  { const { ctx, title } = await boot({ width: 1400, mem: {} });
    title.onclick(); got.pc = ev(ctx, "POP"); }
  assert.deepEqual(got, {
    sheet: { open: true, up: true, head: "화면 바로가기", tab: 0 },
    list: [["terminal", "터미널", "A", "", false], ["files", "파일", "a.py", "", false], ["viewer", "뷰어", "B · 방금", "", false], ["monitor", "팀 관제", "팀1", "", false]],
    picked: { closed: true, at: "terminal:a", view: "detail" },
    now: ["terminal", "터미널", "A", "지금", false],
    back: ["terminal", "터미널", "A", "바로 돌아가기", false],
    returned: { view: "detail", at: "terminal:a", newTerm: 0 },                  // 재접속 없이 다시 보여 주기만
    none: [["terminal", "터미널", "A", "", false], ["files", "파일", "열어 둔 파일 없음", "고르기…", true],
           ["viewer", "뷰어", "최근에 본 대화 없음", "고르기…", true], ["monitor", "팀 관제", "팀1", "", false]],
    wait: [["terminal", "준비 중", true], ["files", "준비 중", true], ["viewer", "준비 중", true], ["monitor", "준비 중", true]],
    pc: null,
  });
});

// ---------------------------------------------------------------- 폰 관제 덮개 머리 ▾
test("T90 폰 관제 덮개 머리에도 ▾ — 같은 시트가 덮개(z 60)보다 위 층에 뜨고(관제 줄 '지금'), 다른 화면을 고르면 덮개(와 관제 연결)를 닫고 간다. PC 상세 칸 관제 머리엔 ▾ 가 없다", async () => {
  const ovHead = (ctx) => ctx.document.body.children.filter((c) => String(c.className) === "mon-ov").at(-1).children[0];
  const got = {};
  { const { ctx } = await boot({ width: 390, mem: {} });
    await press(ctx, "monitor");
    const qb = ovHead(ctx).children.find((c) => String(c.className).includes("qs-ov"));
    got.button = qb && [qb.textContent, qb.title, ovHead(ctx).children.map((c) => c.textContent).slice(-3).join(" ")];
    qb.onclick();
    got.sheet = { up: ev(ctx, "POP.box.classList.contains('qs-sheet') && POP.bg.classList.contains('qs-sheet')"), list: rows(ctx).map((r) => [r[0], r[3]]) };
    const mon = ev(ctx, "__mon.filter((h) => h.gid).at(-1)");
    ev(ctx, "POP.box.children[1].children[1].onclick()"); await tick(RENDER);
    got.after = { overlay: ev(ctx, "MON_OV"), monClosed: mon.closed, at: at(ctx) };
  }
  { const { ctx, pd } = await boot({ width: 1400, mem: {} });
    await press(ctx, "monitor");
    got.pcInline = pd.children[0].children[0].children.some((c) => String(c.className).includes("qs-ov")); }
  assert.deepEqual(got, {
    button: ["▾", "화면 바로가기", "◨ ▾ ✕"],                                 // ◨ 와 ✕ 사이(§5-3 그림)
    sheet: { up: true, list: [["terminal", ""], ["files", ""], ["viewer", ""], ["monitor", "지금"]] },
    after: { overlay: null, monClosed: 1, at: "terminal:a" },
    pcInline: false,
  });
});

// ---------------------------------------------------------------- 단축키
test("T91 단축키 Alt+1~4: 창 전체 감시(capture)로 받는다 — 진짜 입력칸(검색·이름·한글 입력창·contenteditable)·모달·팝·페어링 화면·Ctrl/Shift/⌘ 섞임·숫자 패드(Alt 코드)·조합 중은 무시, xterm 숨은 입력과 Monaco 안에서는 받는다. 누르고 있으면 되풀이하지 않는다", async () => {
  const { ctx, app } = await boot({ mem: {} });
  ev(ctx, "globalThis.__qg = []; quickGo = (k) => { __qg.push(k); }");
  const el = (tag, o = {}) => Object.assign(stubEl(tag), o);
  const inside = (sel) => el("textarea", { closest: (s) => (s === sel ? stubEl("div") : null) });
  const T = { body: el("body"), input: el("input"), select: el("select"), textarea: el("textarea"), editable: el("div", { isContentEditable: true }),
              xterm: inside(".xterm"), monaco: inside(".monaco-editor"), button: el("button") };
  const press = (o) => {
    const e = { altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, isComposing: false, repeat: false, code: "Digit1", key: "1", target: T.body,
                prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; }, ...o };
    ev(ctx, "__qg.length = 0"); ctx.onQuickKey(e);
    return [ev(ctx, "__qg.join(',')"), e.prevented + e.stopped];
  };
  const CASES = {
    "Alt+1": {}, "Alt+2": { code: "Digit2", key: "2" }, "Alt+3": { code: "Digit3", key: "3" }, "Alt+4": { code: "Digit4", key: "4" },
    "단추에 커서": { target: T.button },
    "검색칸": { target: T.input }, "SELECT": { target: T.select }, "일반 TEXTAREA": { target: T.textarea }, "contenteditable": { target: T.editable },
    "xterm 숨은 입력": { target: T.xterm, code: "Digit2", key: "2" }, "Monaco": { target: T.monaco, code: "Digit3", key: "3" },
    "Ctrl+Alt+1": { ctrlKey: true }, "Shift+Alt+1": { shiftKey: true }, "⌘+Alt+1": { metaKey: true },
    "Alt+숫자 패드 1": { code: "Numpad1" }, "Alt+5": { code: "Digit5", key: "5" }, "Alt 없이 1": { altKey: false }, "한글 조합 중": { isComposing: true },
    "누르고 있음": { repeat: true }, "code 없는 옛 브라우저": { code: "", key: "3" },
  };
  const got = Object.fromEntries(Object.entries(CASES).map(([k, o]) => [k, press(o)]));
  ctx.document._register("overlay", stubEl("div")); got["모달 열림"] = press({}); ctx.document._register("overlay", null);
  ev(ctx, "openPop(null, el('div'), '⋯')"); got["팝 열림"] = press({}); ev(ctx, "closePop()");
  app.classList.add("hidden"); got["페어링 화면"] = press({}); app.classList.remove("hidden");
  got.registered = (ctx.__winListeners.keydown || []).includes(ctx.onQuickKey);
  const go = (k) => [k, 2], no = ["", 0];
  assert.deepEqual(got, {
    "Alt+1": go("terminal"), "Alt+2": go("files"), "Alt+3": go("viewer"), "Alt+4": go("monitor"), "단추에 커서": go("terminal"),
    "검색칸": no, "SELECT": no, "일반 TEXTAREA": no, "contenteditable": no,
    "xterm 숨은 입력": go("files"), "Monaco": go("viewer"),
    "Ctrl+Alt+1": no, "Shift+Alt+1": no, "⌘+Alt+1": no, "Alt+숫자 패드 1": no, "Alt+5": no, "Alt 없이 1": no, "한글 조합 중": no,
    "누르고 있음": ["", 2],                                                       // 막기만 — 터미널로 새지 않게
    "code 없는 옛 브라우저": go("viewer"),
    "모달 열림": no, "팝 열림": no, "페어링 화면": no, registered: true,
  });
});

test("T92 단축키가 터미널로 새지 않는다 — 릴레이 xterm 은 이 조합만 claude 로 보내지 않고(attachCustomKeyEventHandler), 로컬 터미널 창(iframe)은 뜰 때마다 같은 감시를 그 창에 붙인다. 실제로 누르면 그 화면으로 간다", async () => {
  const k = (o) => ({ type: "keydown", altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, code: "Digit1", key: "1", ...o });
  const got = {};
  { const { ctx } = await boot({ mem: {} });                                      // 릴레이 — 내장 xterm
    ev(ctx, "globalThis.__ckh = []; { const X = Terminal; Terminal = class { constructor(o) { const t = new X(o); t.attachCustomKeyEventHandler = (f) => __ckh.push(f); return t; } }; }");
    await press(ctx, "terminal");
    const h = ev(ctx, "__ckh[0]");
    got.relay = { handlers: ev(ctx, "__ckh.length"), alt1to4: [1, 2, 3, 4].map((n) => h(k({ code: "Digit" + n, key: String(n) }))),
                  others: [h(k({ altKey: false, code: "KeyA", key: "a" })), h(k({ code: "Digit5", key: "5" })), h(k({ ctrlKey: true })), h(k({ code: "Numpad1" })), h(k({ type: "keyup" }))] };
    const e = { ...k({ code: "Digit2", key: "2" }), target: Object.assign(stubEl("textarea"), { closest: (s) => (s === ".xterm" ? stubEl("div") : null) }), preventDefault() {}, stopPropagation() {} };
    ctx.onQuickKey(e); await tick(QS);
    got.relayGo = at(ctx);
  }
  { const { ctx } = await boot({ local: true, mem: {} });                         // 로컬 — 터미널 창(iframe, 같은 출처)
    await press(ctx, "terminal");
    const fr = ev(ctx, "termView.cur().frame");
    const wins = [0, 1].map(() => { const w = { added: [], addEventListener(t, f, c) { w.added.push([t, f, c]); } }; return w; });
    fr.contentWindow = wins[0]; fr.onload();
    fr.contentWindow = wins[1]; fr.onload();                                       // 창을 다시 읽음(되찾기·재연결) — 새 창에 다시 붙는다
    got.local = wins.map((w) => w.added.map(([t, f, c]) => [t, f === ctx.onQuickKey, c]));
    const [, f] = wins[1].added[0];
    const e = { ...k({ code: "Digit3", key: "3" }), target: Object.assign(stubEl("textarea"), { closest: (s) => (s === ".xterm" ? stubEl("div") : null) }),
                prevented: 0, preventDefault() { this.prevented++; }, stopPropagation() {} };
    f(e); await tick(QS);
    got.localGo = { at: at(ctx), prevented: e.prevented };
  }
  assert.deepEqual(got, {
    relay: { handlers: 1, alt1to4: [false, false, false, false], others: [true, true, true, true, false] },   // keyup 도 같은 조합이면 삼킨다
    relayGo: "files:a.py",
    // 두 번째 감시는 목록 칸 접기(Ctrl+Shift+B, onSideKey — 0.32.3 qs-polish T112)
    local: [[["keydown", true, true], ["keydown", false, undefined]], [["keydown", true, true], ["keydown", false, undefined]]],
    localGo: { at: "viewer:b", prevented: 1 },
  });
});

// ---------------------------------------------------------------- 빈 상세 칸 '최근 화면'
test("T93 빈 상세 칸(PC) '최근 화면': 갈 수 있는 기억만(흐림 빼고), 마지막 화면이 맨 앞 — 첫 목록 전엔 없음. 목록 칸이 접혀 있으면 '◧ …' 안내, 바깥 상자는 'detail-empty' 하나 그대로, 누르면 그 화면", async () => {
  const pane = (pd) => { const box = pd.children[0], r = box.children.find((c) => String(c.className) === "qs-recent");
    return { kids: pd.children.length, cls: String(box.className), msg: box.children[0].textContent,
             recent: r ? r.children[1].children.map((b) => b.dataset.k + ":" + b.children[1].children[0].textContent) : [] }; };
  const got = {};
  { const { ctx, pd } = await boot({ mem: {} });
    got.first = pane(pd);
    await press(ctx, "monitor"); ev(ctx, "detailPlaceholder()"); got.afterMonitor = pane(pd).recent;
    await press(ctx, "files"); ev(ctx, "detailPlaceholder()"); got.afterFiles = pane(pd).recent;
    ev(ctx, "setSideOff(true)"); got.folded = pane(pd).msg;
    ev(ctx, "setSideOff(false)"); got.unfolded = pane(pd).msg;
    ev(ctx, "detailPlaceholder('삭제됨'); setSideOff(true)"); got.deleted = pane(pd).msg; ev(ctx, "setSideOff(false)");
    const r = pd.children[0].children[1].children[1].children;
    r.find((b) => b.dataset.k === "viewer").onclick(); await tick(QS); got.click = at(ctx);
  }
  { const { pd } = await boot({ mem: { tabs: null, viewer: null } }); got.two = pane(pd).recent; }
  { const { pd } = await boot({ mem: { viewer: { sid: C, kind: "viewer", name: "C" } } }); got.goneViewer = pane(pd).recent; }   // 목록에 없는 세션은 흐림 — 빼고
  { const { ctx, pd } = await boot({ mem: { tabs: null, files: null, viewer: null, monitor: null } });
    got.none = pane(pd);
    ev(ctx, "setSideOff(true)"); got.noneFolded = pane(pd).msg;
    ev(ctx, "detailPlaceholder('왼쪽에서 팀을 고르세요')"); got.teamFolded = pane(pd).msg; }
  { const { ctx, pd } = await boot({ ready: false, mem: {} }); ev(ctx, "detailPlaceholder()"); got.beforeList = pane(pd); }
  const R4 = ["terminal:A", "files:a.py", "viewer:B", "monitor:팀1"];
  assert.deepEqual(got, {
    first: { kids: 1, cls: "detail-empty", msg: "왼쪽에서 세션을 선택하세요", recent: R4 },
    afterMonitor: ["monitor:팀1", "terminal:A", "files:a.py", "viewer:B"],
    afterFiles: ["files:a.py", "terminal:A", "viewer:B", "monitor:팀1"],
    folded: "◧ 로 목록 칸을 펼치거나 최근 화면으로 가세요", unfolded: "왼쪽에서 세션을 선택하세요", deleted: "삭제됨",
    click: "viewer:b",
    two: ["files:a.py", "monitor:팀1"],
    goneViewer: ["terminal:A", "files:a.py", "monitor:팀1"],
    none: { kids: 1, cls: "detail-empty", msg: "왼쪽에서 세션을 선택하세요", recent: [] },
    noneFolded: "◧ 로 목록 칸을 펼쳐 세션을 선택하세요", teamFolded: "◧ 로 목록 칸을 펼쳐 팀을 고르세요",
    beforeList: { kids: 1, cls: "detail-empty", msg: "왼쪽에서 세션을 선택하세요", recent: [] },
  });
});

// ---------------------------------------------------------------- 뷰어 막대 이름 · 강조 갱신 시점
test("T94 뷰어 막대에 세션 이름(‹ 목록 다음) · 단추 강조는 화면 진입·목록 끝·20초 갱신 끝에서 100ms 로 모아 한 번 그린다", async () => {
  const { ctx, pd, nav } = await boot({ mem: {} });
  await ev(ctx, "loadViewer(SESSIONS[1])");
  const bar = pd.children[0];
  const name = [bar.children[0].textContent, String(bar.children[1].className), bar.children[1].textContent, bar.children[1].title];
  ev(ctx, "globalThis.__rq = 0; { const f = renderQuick; renderQuick = () => { __rq++; f(); }; }");
  await tick(QS); ev(ctx, "__rq = 0");
  ev(ctx, "detailPlaceholder(); detailPlaceholder(); loadDetail(SESSIONS[0])");   // 화면 진입 세 번 → 한 번
  const soon = ev(ctx, "__rq"); await tick(QS);
  const enter = { soon, later: ev(ctx, "__rq"), on: btns(nav).map((b) => b[1]) };
  ev(ctx, "__rq = 0"); await ev(ctx, "loadList()"); await tick(QS);
  const list = ev(ctx, "__rq");
  ev(ctx, "__rq = 0"); await ctx.__intervals.find((x) => x.ms === 20000).f(); await tick(QS);
  const tickN = ev(ctx, "__rq");
  assert.deepEqual({ name, enter, list, tick: tickN }, {
    name: ["‹ 목록", "vtitle", "B", "B · E:/q"],
    enter: { soon: 0, later: 1, on: ["", "", "on", ""] },
    list: 1, tick: 1,
  });
});

// ---------------------------------------------------------------- 🏠 홈(2026-10-26 사장님) — 머리 줄·시트 맨 앞, 아무것도 고르지 않은 처음 화면
test("T95 🏠 홈: PC 머리 줄 맨 앞 단추(빈 상세 칸이면 파랑) · 누르면 어떤 화면에서든 빈 상세 칸(최근 화면)·선택 해제·목록 칸 · 폰 시트 맨 앞 줄 · Alt+0 · 첫 목록 전에도 된다", async () => {
  const realNav = (ctx) => ctx.document.getElementById("qs");
  const got = {};
  { const { ctx } = await boot({ mem: {} });
    ev(ctx, "renderQuick()");
    const n = realNav(ctx), h = n.children[0];
    got.first = [h.dataset.k, h.children[0].textContent, h.children[1].textContent, h.title, n.children.length];
    got.emptyOn = String(h.className);
    await press(ctx, "terminal"); ev(ctx, "renderQuick()"); got.termOff = String(realNav(ctx).children[0].className);
    ev(ctx, "SELECTED_ID = SESSIONS[0].session_id");
    h.onclick(); await tick(QS);
    got.afterTerm = [at(ctx), ev(ctx, "SELECTED_ID"), ev(ctx, "paneDetail().classList.contains('term-mode')"),
      ev(ctx, "paneDetail().children[0] && paneDetail().children[0].className"), String(realNav(ctx).children[0].className)];
    got.tabsKept = ev(ctx, "TABS.list.length") > 0;                              // 터미널 탭은 그대로(화면만 떠남)
    await press(ctx, "files"); h.onclick(); await tick(QS); got.afterFiles = [at(ctx), ev(ctx, "paneDetail().classList.contains('fb-mode')")];
    await press(ctx, "viewer");
    ctx.onQuickKey({ altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, isComposing: false, repeat: false, code: "Digit0", key: "0",
                     target: stubEl("body"), preventDefault() {}, stopPropagation() {} }); await tick(QS);
    got.alt0 = at(ctx);
  }
  { const { ctx, app, title } = await boot({ width: 390, mem: {} });
    await press(ctx, "terminal"); app.setAttribute("data-view", "detail");
    title.onclick(); await tick();
    const r = ev(ctx, "POP.box.children[1].children");
    got.sheet = [r[0].dataset.k, r[0].children[1].children[0].textContent, r.length];
    r[0].onclick(); await tick(QS);
    got.phone = [at(ctx), app.getAttribute("data-view")];
  }
  { const { ctx } = await boot({ ready: false, mem: {} });
    ev(ctx, "renderQuick()"); realNav(ctx).children[0].onclick(); await tick(QS);
    got.beforeList = [at(ctx), ev(ctx, "__toasts.length")];
  }
  assert.deepEqual(got, {
    first: ["home", "🏠", "홈", "홈 — 아무것도 고르지 않은 처음 화면 · Alt+0", 5],
    emptyOn: "on", termOff: "",
    afterTerm: ["empty", null, false, "detail-empty", "on"], tabsKept: true,
    afterFiles: ["empty", false], alt0: "empty",
    sheet: ["home", "홈", 5], phone: ["empty", "list"],
    beforeList: ["empty", 0],
  });
});
